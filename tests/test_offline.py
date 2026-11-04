from __future__ import annotations

from pathlib import Path

import pytest
from plumbum import local

from copier import run_copy, run_update
from copier._user_data import load_answersfile_data
from copier._vcs import _force_rmtree, _get_mirror_path, clone
from copier.errors import UserMessageError

from .helpers import build_file_tree, git, git_save


def _make_remote(path: Path, version: str) -> str:
    """Create a tagged template repository and return its `file://` URL."""
    build_file_tree(
        {
            path / "version.txt": version,
            path / "{{ _copier_conf.answers_file }}.jinja": (
                "{{ _copier_answers|to_nice_yaml }}"
            ),
        }
    )
    git_save(path, message=version, tag=version)
    return path.as_uri()


def _corrupt_mirror(url: str) -> None:
    """Leave only the `objects` directory so the mirror fails validation."""
    mirror = _get_mirror_path(url)
    for child in mirror.iterdir():
        if child.name != "objects":
            if child.is_dir():
                _force_rmtree(child)
            else:
                child.unlink()


def test_offline_copy_after_remote_deleted(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    url = _make_remote(remote, "v1.0.0")

    clone(url, location=str(tmp_path / "cache-warmup"))
    _force_rmtree(remote)

    dst = tmp_path / "dst"
    run_copy(f"git+{url}", dst, offline=True, defaults=True, quiet=True)

    assert (dst / "version.txt").read_text() == "v1.0.0"
    assert load_answersfile_data(dst)["_commit"] == "v1.0.0"


def test_offline_update_uses_cached_commit_and_source(tmp_path: Path) -> None:
    remote = tmp_path / "remote"
    url = _make_remote(remote, "v1.0.0")

    dst = tmp_path / "dst"
    run_copy(f"git+{url}", dst, defaults=True, quiet=True)
    git_save(dst, message="initial copy")

    # Publish a newer version and warm the cache with it.
    build_file_tree({remote / "version.txt": "v1.1.0"})
    with local.cwd(remote):
        git("add", "-A")
        git("commit", "-m", "v1.1.0")
        git("tag", "v1.1.0")
    clone(url, "v1.1.0", location=str(tmp_path / "cache-refresh"))
    _force_rmtree(remote)

    run_update(dst, offline=True, defaults=True, overwrite=True, quiet=True)

    assert (dst / "version.txt").read_text() == "v1.1.0"
    answers = load_answersfile_data(dst)
    assert answers["_commit"] == "v1.1.0"
    assert answers["_src_path"] == f"git+{url}"


@pytest.mark.parametrize(
    ("use_prereleases", "expected"),
    [(False, "v1.0.0"), (True, "v2.0.0rc1")],
)
def test_offline_latest_tag_respects_prereleases(
    tmp_path: Path, use_prereleases: bool, expected: str
) -> None:
    remote = tmp_path / "remote"
    url = _make_remote(remote, "v1.0.0")
    build_file_tree({remote / "version.txt": "v2.0.0rc1"})
    with local.cwd(remote):
        git("add", "-A")
        git("commit", "-m", "v2.0.0rc1")
        git("tag", "v2.0.0rc1")

    clone(url, location=str(tmp_path / "cache-warmup"))
    _force_rmtree(remote)

    dst = tmp_path / "dst"
    run_copy(
        f"git+{url}",
        dst,
        offline=True,
        use_prereleases=use_prereleases,
        defaults=True,
        quiet=True,
    )
    assert (dst / "version.txt").read_text() == expected


def test_offline_missing_ref_fails_before_creating_destination(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote"
    url = _make_remote(remote, "v1.0.0")
    clone(url, location=str(tmp_path / "cache-warmup"))
    _force_rmtree(remote)

    dst = tmp_path / "dst"
    with pytest.raises(UserMessageError, match="Offline mode.*does not exist"):
        run_copy(
            f"git+{url}",
            dst,
            vcs_ref="v9.9.9",
            offline=True,
            defaults=True,
            quiet=True,
        )
    assert not dst.exists()


def test_offline_corrupt_cache_fails_before_creating_destination(
    tmp_path: Path,
) -> None:
    remote = tmp_path / "remote"
    url = _make_remote(remote, "v1.0.0")
    clone(url, location=str(tmp_path / "cache-warmup"))
    _force_rmtree(remote)
    _corrupt_mirror(url)

    dst = tmp_path / "dst"
    with pytest.raises(UserMessageError, match="Offline mode.*valid cached"):
        run_copy(f"git+{url}", dst, offline=True, defaults=True, quiet=True)
    assert not dst.exists()
