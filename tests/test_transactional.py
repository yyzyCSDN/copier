"""Tests for the transactional update mode."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from plumbum import local

from copier import run_copy, run_update
from copier._user_data import load_answersfile_data
from copier.errors import TaskError

from .helpers import COPIER_ANSWERS_FILE, build_file_tree, git


def _migration(artifact: str, stage: str | None) -> str:
    condition = f"    when: \"{{{{ _stage == '{stage}' }}}}\"\n" if stage else ""
    return (
        "_migrations:\n"
        "-   command:\n"
        '    -   "{{ _copier_python }}"\n'
        "    -   -c\n"
        "    -   |\n"
        "        from pathlib import Path\n"
        f'        Path("{artifact}").write_text("partial")\n'
        "        raise SystemExit(1)\n" + condition
    )


def _make_project(tmp_path: Path) -> tuple[Path, Path]:
    src = tmp_path / "src"
    dst = tmp_path / "dst"
    src.mkdir()
    with local.cwd(src):
        build_file_tree(
            {
                **COPIER_ANSWERS_FILE,
                "copier.yml": "",
                "original.txt": "version 1\n",
                "to_delete.txt": "delete me\n",
                "to_rename.txt": "rename me\n",
            }
        )
        git("init")
        git("add", ".")
        git("commit", "-m", "template v1")
        git("tag", "v1")
    run_copy(str(src), dst, defaults=True)
    with local.cwd(dst):
        build_file_tree({".gitignore": "ignored.txt\n", "user.txt": "user content\n"})
        git("init")
        git("add", ".")
        git("commit", "-m", "project baseline")
    # The ignored file is untracked but must survive updates and rollbacks.
    (dst / "ignored.txt").write_text("ignored\n")
    (dst / "untracked.txt").write_text("untracked\n")
    # An unstaged modification to a path the update does not touch.
    (dst / "user.txt").write_text("modified by user\n")
    return src, dst


def _upgrade_template(src: Path, copier_yml: str) -> None:
    with local.cwd(src):
        build_file_tree(
            {
                "copier.yml": copier_yml,
                "original.txt": "version 2\n",
                "new_file.txt": "new\n",
                "renamed.txt": "rename me\n",
            }
        )
        (src / "to_delete.txt").unlink()
        (src / "to_rename.txt").unlink()
        git("add", "-A")
        git("commit", "-m", "template v2")
        git("tag", "v2")


def test_transactional_restores_after_failed_pre_migration(tmp_path: Path) -> None:
    src, dst = _make_project(tmp_path)
    _upgrade_template(src, _migration("before-migration-artifact", "before"))

    with pytest.raises(TaskError):
        run_update(
            dst,
            defaults=True,
            overwrite=True,
            unsafe=True,
            transactional=True,
            quiet=True,
        )

    assert not (dst / "before-migration-artifact").exists()
    assert not (dst / "new_file.txt").exists()
    assert not (dst / "renamed.txt").exists()
    assert (dst / "original.txt").read_text() == "version 1\n"
    assert (dst / "to_delete.txt").is_file()
    assert (dst / "to_rename.txt").is_file()
    assert (dst / "user.txt").read_text() == "modified by user\n"
    assert (dst / "ignored.txt").read_text() == "ignored\n"
    assert (dst / "untracked.txt").read_text() == "untracked\n"
    assert load_answersfile_data(dst)["_commit"] == "v1"


def test_transactional_restores_after_failed_post_migration(tmp_path: Path) -> None:
    src, dst = _make_project(tmp_path)
    _upgrade_template(src, _migration("after-migration-artifact", None))

    with pytest.raises(TaskError):
        run_update(
            dst,
            defaults=True,
            overwrite=True,
            unsafe=True,
            transactional=True,
            quiet=True,
        )

    assert not (dst / "after-migration-artifact").exists()
    assert not (dst / "new_file.txt").exists()
    assert not (dst / "renamed.txt").exists()
    assert (dst / "original.txt").read_text() == "version 1\n"
    assert (dst / "to_delete.txt").read_text() == "delete me\n"
    assert (dst / "to_rename.txt").read_text() == "rename me\n"
    assert (dst / "user.txt").read_text() == "modified by user\n"
    assert (dst / "ignored.txt").read_text() == "ignored\n"
    assert (dst / "untracked.txt").read_text() == "untracked\n"
    assert load_answersfile_data(dst)["_commit"] == "v1"


def test_transactional_applies_deletions_and_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = _make_project(tmp_path)
    _upgrade_template(src, "")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    run_update(
        dst,
        defaults=True,
        overwrite=True,
        unsafe=True,
        transactional=True,
        quiet=True,
    )

    assert (dst / "original.txt").read_text() == "version 2\n"
    assert not (dst / "to_delete.txt").exists()
    assert not (dst / "to_rename.txt").exists()
    assert (dst / "renamed.txt").read_text() == "rename me\n"
    assert (dst / "new_file.txt").read_text() == "new\n"
    assert (dst / "user.txt").read_text() == "modified by user\n"
    assert (dst / "ignored.txt").read_text() == "ignored\n"
    assert (dst / "untracked.txt").read_text() == "untracked\n"
    assert load_answersfile_data(dst)["_commit"] == "v2"
    # The transaction snapshot is cleaned up after a successful update.
    assert list(temp_root.iterdir()) == []


def test_transactional_restores_git_index(tmp_path: Path) -> None:
    src, dst = _make_project(tmp_path)
    index = dst / ".git" / "index"
    before = index.read_bytes()
    _upgrade_template(
        src,
        """\
        _migrations:
        -   command:
            -   "{{ _copier_python }}"
            -   -c
            -   |
                import subprocess
                subprocess.run(["git", "add", "-A"], check=True)
                raise SystemExit(1)
        """,
    )

    with pytest.raises(TaskError):
        run_update(
            dst,
            defaults=True,
            overwrite=True,
            unsafe=True,
            transactional=True,
            quiet=True,
        )

    assert index.read_bytes() == before
    assert (dst / "original.txt").read_text() == "version 1\n"


def test_transactional_reports_unrestored_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copier import _transaction

    src, dst = _make_project(tmp_path)
    _upgrade_template(src, _migration("after-migration-artifact", None))
    restore_entry = _transaction._UpdateTransaction._restore_entry

    def failing_restore(
        transaction: _transaction._UpdateTransaction,
        rel_path: Path,
        entry: _transaction._SnapshotEntry,
    ) -> None:
        if rel_path.name == "original.txt":
            raise OSError("cannot restore")
        restore_entry(transaction, rel_path, entry)

    monkeypatch.setattr(
        _transaction._UpdateTransaction, "_restore_entry", failing_restore
    )

    with pytest.raises(TaskError) as exc_info:
        run_update(
            dst,
            defaults=True,
            overwrite=True,
            unsafe=True,
            transactional=True,
            quiet=True,
        )

    # The original task error is preserved and mentions the unrestored path.
    assert "returned non-zero exit status" in exc_info.value.message
    assert "original.txt" in exc_info.value.message


def test_transactional_does_not_create_files_in_pretend_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src, dst = _make_project(tmp_path)
    _upgrade_template(src, "")
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))

    run_update(
        dst,
        defaults=True,
        overwrite=True,
        unsafe=True,
        transactional=True,
        pretend=True,
        quiet=True,
    )

    assert list(temp_root.iterdir()) == []
    assert (dst / "original.txt").read_text() == "version 1\n"
