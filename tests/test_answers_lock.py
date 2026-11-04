from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

import copier
from copier.errors import AnswersDriftError, InvalidAnswersLockError

from .helpers import build_file_tree, git_save

ANSWERS_FILE = Path(".copier-answers.yml")
LOCK_FILE = Path(".copier-answers.yml.lock")


def _build_template(root: Path, *, migration: bool = False) -> None:
    migration_config = (
        dedent(
            """\
            _migrations:
              - version: v2
                command:
                  - "{{ _copier_python }}"
                  - "-c"
                  - "import pathlib, yaml; p=pathlib.Path('.copier-answers.yml'); d=yaml.safe_load(p.read_text()) or {}; d['project_name']='migrated'; p.write_text(yaml.safe_dump(d))"
            """
        )
        if migration
        else ""
    )
    build_file_tree(
        {
            root / "copier.yml": migration_config
            + dedent(
                """\
                project_name:
                  type: str
                  default: demo

                unused:
                  type: str
                  default: default

                token:
                  type: str
                  default: s3cr3t
                  secret: true
                """
            ),
            root / "{{ _copier_conf.answers_file }}.jinja": (
                "{{ _copier_answers|to_nice_yaml }}"
            ),
            root / "name.txt.jinja": "{{ project_name }}\n",
        }
    )


def _save_template_v2(root: Path) -> None:
    (root / "name.txt.jinja").write_text("{{ project_name }} v2\n")
    git_save(root, message="v2", tag="v2")


def _read_answers(dst: Path, answers_file: Path = ANSWERS_FILE) -> dict[str, object]:
    return yaml.safe_load((dst / answers_file).read_text()) or {}


def _read_lock(dst: Path, answers_file: Path = ANSWERS_FILE) -> dict[str, object]:
    return yaml.safe_load(
        (dst / answers_file.with_name(answers_file.name + ".lock")).read_text()
    )


@pytest.fixture
def template(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, Path]:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    _build_template(src)
    git_save(src, tag="v1")
    return str(src), dst


def test_copy_writes_answers_lock_without_secret_answers(
    template: tuple[str, Path],
) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True, protect_answers=True)

    lock_path = dst / LOCK_FILE
    assert lock_path.is_file()
    lock = yaml.safe_load(lock_path.read_text())
    assert lock["version"] == 1
    assert lock["source"] == src
    assert lock["commit"] == "v1"
    assert set(lock["digests"]) == {"project_name", "unused"}
    assert "token" not in lock["digests"]
    assert "s3cr3t" not in lock_path.read_text()


def test_manual_answers_edit_requires_accept_drift(
    template: tuple[str, Path],
) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True, protect_answers=True)
    git_save(dst, message="v1")

    answers = _read_answers(dst)
    answers["unused"] = "manual"
    (dst / ANSWERS_FILE).write_text(yaml.safe_dump(answers))
    git_save(dst, message="manual answers")

    _save_template_v2(Path(src))

    with pytest.raises(AnswersDriftError, match="unused"):
        copier.run_update(dst, defaults=True, overwrite=True, protect_answers=True)

    copier.run_update(
        dst,
        defaults=True,
        overwrite=True,
        protect_answers=True,
        accept_answers_drift=True,
    )
    assert (dst / "name.txt").read_text() == "demo v2\n"
    lock = _read_lock(dst)
    assert set(lock["digests"]) == {"project_name", "unused"}

    git_save(dst, message="accepted")
    copier.run_update(dst, defaults=True, overwrite=True, protect_answers=True)


def test_migration_can_modify_answers_and_lock_tracks_result(
    template: tuple[str, Path],
) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True, protect_answers=True)
    git_save(dst, message="v1")

    _build_template(Path(src), migration=True)
    _save_template_v2(Path(src))

    copier.run_update(
        dst,
        defaults=True,
        overwrite=True,
        unsafe=True,
        protect_answers=True,
    )
    assert _read_answers(dst)["project_name"] == "migrated"
    assert (dst / "name.txt").read_text() == "demo v2\n"

    git_save(dst, message="migrated")
    copier.run_update(
        dst,
        defaults=True,
        overwrite=True,
        unsafe=True,
        protect_answers=True,
    )


def test_recopy_verifies_and_refreshes_lock(template: tuple[str, Path]) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True, protect_answers=True)
    git_save(dst, message="v1")

    answers = _read_answers(dst)
    answers["unused"] = "manual"
    (dst / ANSWERS_FILE).write_text(yaml.safe_dump(answers))
    git_save(dst, message="manual answers")

    with pytest.raises(AnswersDriftError, match="unused"):
        copier.run_recopy(dst, defaults=True, overwrite=True, protect_answers=True)

    copier.run_recopy(
        dst,
        defaults=True,
        overwrite=True,
        protect_answers=True,
        accept_answers_drift=True,
    )
    assert set(_read_lock(dst)["digests"]) == {"project_name", "unused"}

    git_save(dst, message="accepted")
    copier.run_recopy(dst, defaults=True, overwrite=True, protect_answers=True)


def test_corrupt_lock_requires_accept_drift(template: tuple[str, Path]) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True, protect_answers=True)
    git_save(dst, message="v1")

    (dst / LOCK_FILE).write_text("not: [valid")
    git_save(dst, message="corrupt lock")

    _save_template_v2(Path(src))

    with pytest.raises(InvalidAnswersLockError, match="corrupt"):
        copier.run_update(dst, defaults=True, overwrite=True, protect_answers=True)

    copier.run_update(
        dst,
        defaults=True,
        overwrite=True,
        protect_answers=True,
        accept_answers_drift=True,
    )
    lock = _read_lock(dst)
    assert lock["version"] == 1
    assert lock["commit"] == "v2"


def test_custom_answers_file_path_uses_companion_lock(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    _build_template(src)
    git_save(src, tag="v1")
    answers_file = Path(".config") / "custom-answers.yml"

    copier.run_copy(
        str(src),
        dst,
        defaults=True,
        answers_file=answers_file,
        protect_answers=True,
    )
    lock_path = dst / answers_file.with_name(answers_file.name + ".lock")
    assert lock_path.is_file()
    assert not (dst / LOCK_FILE).exists()

    git_save(dst, message="v1")
    _save_template_v2(src)
    copier.run_update(
        dst,
        defaults=True,
        overwrite=True,
        answers_file=answers_file,
        protect_answers=True,
    )
    assert _read_lock(dst, answers_file)["commit"] == "v2"


def test_old_project_without_lock_gets_one_after_update(
    template: tuple[str, Path],
) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True)
    assert not (dst / LOCK_FILE).exists()

    git_save(dst, message="v1")
    _save_template_v2(Path(src))
    copier.run_update(dst, defaults=True, overwrite=True, protect_answers=True)
    assert (dst / LOCK_FILE).is_file()


def test_unprotected_operation_does_not_write_lock(
    template: tuple[str, Path],
) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True)
    assert not (dst / LOCK_FILE).exists()


def test_semantically_equal_yaml_reformat_does_not_trigger_drift(
    template: tuple[str, Path],
) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True, protect_answers=True)
    git_save(dst, message="v1")

    reformatted = yaml.safe_dump(_read_answers(dst), default_flow_style=True)
    (dst / ANSWERS_FILE).write_text(reformatted)
    git_save(dst, message="reformatted")

    _save_template_v2(Path(src))
    copier.run_update(dst, defaults=True, overwrite=True, protect_answers=True)


def test_pretend_ignores_drift_and_keeps_lock(template: tuple[str, Path]) -> None:
    src, dst = template
    copier.run_copy(src, dst, defaults=True, protect_answers=True)
    git_save(dst, message="v1")

    answers = _read_answers(dst)
    answers["project_name"] = "manual"
    (dst / ANSWERS_FILE).write_text(yaml.safe_dump(answers))
    git_save(dst, message="manual")
    original_lock = (dst / LOCK_FILE).read_text()

    copier.run_copy(
        src,
        dst,
        defaults=True,
        overwrite=True,
        protect_answers=True,
        pretend=True,
    )
    assert (dst / LOCK_FILE).read_text() == original_lock
