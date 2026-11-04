from __future__ import annotations

from pathlib import Path
from unicodedata import normalize

import pytest
from plumbum import local

import copier
from copier.errors import PathCollisionError

from .helpers import build_file_tree, git_save


def _build_colliding_template(src: Path) -> None:
    build_file_tree(
        {
            src / "copier.yml": "",
            src / "{{ _copier_conf.answers_file }}.jinja": (
                "{{ _copier_answers|to_nice_yaml }}"
            ),
            src / "{% if true %}same.txt{% endif %}.jinja": "first",
            src / "{{ 'same' }}.txt.jinja": "second",
        }
    )


def test_safe_paths_is_disabled_by_default(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    _build_colliding_template(src)

    copier.run_copy(str(src), dst, defaults=True, overwrite=True)

    assert (dst / "same.txt").is_file()


def test_safe_paths_detects_identical_jinja_paths(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    _build_colliding_template(src)

    with pytest.raises(PathCollisionError) as error:
        copier.run_copy(str(src), dst, defaults=True, overwrite=True, safe_paths=True)

    message = str(error.value)
    assert "[identical]" in message
    assert '"same.txt"' in message
    assert "{% if true %}same.txt{% endif %}.jinja" in message
    assert "{{ 'same' }}.txt.jinja" in message
    assert not any(dst.iterdir())


def test_safe_paths_detects_yield_collisions(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            src / "copier.yml": "",
            src
            / "{% yield name from names %}{{ name }}{% endyield %}.jinja": "{{ name }}",
        }
    )

    with pytest.raises(PathCollisionError) as error:
        copier.run_copy(
            str(src),
            dst,
            data={"names": ["a.txt", "a.txt"]},
            defaults=True,
            overwrite=True,
            safe_paths=True,
        )

    message = str(error.value)
    assert "[identical]" in message
    assert '"a.txt"' in message
    assert "{% yield name from names %}{{ name }}{% endyield %}.jinja" in message


@pytest.mark.parametrize(
    ("names", "collision_type"),
    [
        (["Foo.txt", "foo.txt"], "[case-insensitive]"),
        (
            ["café.txt", normalize("NFD", "café.txt")],
            "[unicode-normalized]",
        ),
    ],
)
def test_safe_paths_detects_case_and_unicode_collisions(
    tmp_path_factory: pytest.TempPathFactory,
    names: list[str],
    collision_type: str,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            src / "copier.yml": "",
            src
            / "{% yield name from names %}{{ name }}{% endyield %}.jinja": "{{ name }}",
        }
    )

    with pytest.raises(PathCollisionError) as error:
        copier.run_copy(
            str(src),
            dst,
            data={"names": names},
            defaults=True,
            overwrite=True,
            safe_paths=True,
        )

    message = str(error.value)
    assert collision_type in message
    for name in names:
        assert name in message


def test_safe_paths_detects_file_directory_collisions(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    conflict_dir = src / "{% if true %}conflict{% endif %}"
    build_file_tree(
        {
            src / "copier.yml": "",
            conflict_dir / "child.txt.jinja": "child",
            src / "{% if true %}conflict{% endif %}.jinja": "file",
        }
    )

    with pytest.raises(PathCollisionError) as error:
        copier.run_copy(str(src), dst, defaults=True, overwrite=True, safe_paths=True)

    message = str(error.value)
    assert "[file-directory]" in message
    assert '"conflict"' in message
    assert "{% if true %}conflict{% endif %}" in message


def test_safe_paths_ignores_excluded_collisions(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    _build_colliding_template(src)

    copier.run_copy(
        str(src),
        dst,
        defaults=True,
        overwrite=True,
        safe_paths=True,
        exclude=["same.txt"],
    )

    assert not (dst / "same.txt").exists()


def test_safe_paths_detects_collisions_before_tasks(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            src / "copier.yml": """
                _tasks:
                  - - "{{ _copier_python }}"
                    - "-c"
                    - "import pathlib, sys; pathlib.Path(sys.argv[1]).write_text('ran')"
                    - "{{ _copier_conf.dst_path }}/task-ran.txt"
                """,
            src / "{% if true %}same.txt{% endif %}.jinja": "first",
            src / "{{ 'same' }}.txt.jinja": "second",
        }
    )

    with pytest.raises(PathCollisionError):
        copier.run_copy(
            str(src),
            dst,
            defaults=True,
            overwrite=True,
            unsafe=True,
            safe_paths=True,
        )

    assert not (dst / "task-ran.txt").exists()
    assert not (dst / "same.txt").exists()


def test_safe_paths_recopy(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    _build_colliding_template(src)
    copier.run_copy(str(src), dst, defaults=True, overwrite=True)

    before = {
        path.relative_to(dst): path.read_text()
        for path in dst.rglob("*")
        if path.is_file()
    }

    with pytest.raises(PathCollisionError):
        copier.run_recopy(dst, defaults=True, overwrite=True, safe_paths=True)

    after = {
        path.relative_to(dst): path.read_text()
        for path in dst.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_safe_paths_update(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    with local.cwd(src):
        build_file_tree(
            {
                "copier.yml": "",
                "{{ _copier_conf.answers_file }}.jinja": (
                    "{{ _copier_answers|to_nice_yaml }}"
                ),
                "a.txt.jinja": "a",
            }
        )
        git_save(tag="v1")
    copier.run_copy(str(src), dst, defaults=True, overwrite=True)
    with local.cwd(dst):
        git_save()

    with local.cwd(src):
        build_file_tree({"{{ 'a' }}.txt.jinja": "a from a second source"})
        git_save(tag="v2")

    with pytest.raises(PathCollisionError):
        copier.run_update(dst, defaults=True, overwrite=True, safe_paths=True)

    assert (dst / "a.txt").read_text() == "a"
