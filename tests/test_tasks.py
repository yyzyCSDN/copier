from __future__ import annotations

import time
from pathlib import Path
from typing import Literal

import pytest
import yaml

import copier
from copier._cli import CopierApp
from copier.errors import TaskError, TaskTimeoutError

from .helpers import BRACKET_ENVOPS_JSON, SUFFIX_TMPL, build_file_tree


@pytest.fixture(scope="module")
def template_path(tmp_path_factory: pytest.TempPathFactory) -> str:
    root = tmp_path_factory.mktemp("demo_tasks")
    build_file_tree(
        {
            (root / "copier.yaml"): (
                f"""\
                _templates_suffix: {SUFFIX_TMPL}
                _envops: {BRACKET_ENVOPS_JSON}

                other_file: bye
                condition: true

                # This tests two things:
                # 1. That the tasks are being executed in the destination folder; and
                # 2. That the tasks are being executed in order, one after another
                _tasks:
                    -   mkdir hello
                    -   command: touch world
                        working_directory: ./hello
                    -   touch [[ other_file ]]
                    -   ["[[ _copier_python ]]", "-c", "open('pyfile', 'w').close()"]
                    -   command: touch true
                        when: "[[ condition ]]"
                    -   command: touch false
                        when: "[[ not condition ]]"
                """
            )
        }
    )
    return str(root)


def test_render_tasks(template_path: str, tmp_path: Path) -> None:
    copier.run_copy(
        template_path,
        tmp_path,
        data={"other_file": "custom", "condition": "true"},
        unsafe=True,
    )
    assert (tmp_path / "custom").is_file()


def test_copy_tasks(template_path: str, tmp_path: Path) -> None:
    copier.run_copy(
        template_path, tmp_path, quiet=True, defaults=True, overwrite=True, unsafe=True
    )
    assert (tmp_path / "hello").exists()
    assert (tmp_path / "hello").is_dir()
    assert (tmp_path / "hello" / "world").exists()
    assert (tmp_path / "bye").is_file()
    assert (tmp_path / "pyfile").is_file()
    assert (tmp_path / "true").is_file()
    assert not (tmp_path / "false").exists()


def test_task_with_blank_rendered_when_is_skipped(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            src / "copier.yml": """\
            _tasks:
                -   command: touch should-not-exist
                    when: |
                        {% if false %}
                        true
                        {% endif %}
            """
        }
    )

    copier.run_copy(
        str(src), dst, quiet=True, defaults=True, overwrite=True, unsafe=True
    )

    assert not (dst / "should-not-exist").exists()


def test_copy_skip_tasks(template_path: str, tmp_path: Path) -> None:
    copier.run_copy(
        template_path,
        tmp_path,
        quiet=True,
        defaults=True,
        overwrite=True,
        skip_tasks=True,
    )
    assert not (tmp_path / "hello").exists()
    assert not (tmp_path / "hello").is_dir()
    assert not (tmp_path / "hello" / "world").exists()
    assert not (tmp_path / "bye").is_file()
    assert not (tmp_path / "pyfile").is_file()


@pytest.mark.parametrize("skip_tasks", [False, True])
def test_copy_cli_skip_tasks(
    tmp_path_factory: pytest.TempPathFactory,
    skip_tasks: bool,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ["src", "dst"])
    build_file_tree(
        {(src / "copier.yaml"): yaml.safe_dump({"_tasks": ["touch task.txt"]})}
    )
    _, retcode = CopierApp.run(
        [
            "copier",
            "copy",
            "--UNSAFE",
            *(["--skip-tasks"] if skip_tasks else []),
            str(src),
            str(dst),
        ],
        exit=False,
    )
    assert retcode == 0
    assert (dst / "task.txt").exists() is (not skip_tasks)


def test_pretend_mode(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): (
                """
                _tasks:
                    - touch created-by-task.txt
                """
            )
        }
    )
    copier.run_copy(str(src), dst, pretend=True, unsafe=True)
    assert not (dst / "created-by-task.txt").exists()


@pytest.mark.parametrize(
    "os, filename",
    [
        ("linux", "linux.txt"),
        ("macos", "macos.txt"),
        ("windows", "windows.txt"),
        (None, "unsupported.txt"),
    ],
)
def test_os_specific_tasks(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    os: Literal["linux", "macos", "windows"] | None,
    filename: str,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): (
                """\
                _tasks:
                -   >-
                    {% if _copier_conf.os == 'linux' %}
                    touch linux.txt
                    {% elif _copier_conf.os == 'macos' %}
                    touch macos.txt
                    {% elif _copier_conf.os == 'windows' %}
                    touch windows.txt
                    {% elif _copier_conf.os is none %}
                    touch unsupported.txt
                    {% else %}
                    touch never.txt
                    {% endif %}
                """
            )
        }
    )
    monkeypatch.setattr("copier._main.OS", os)
    copier.run_copy(str(src), dst, unsafe=True)
    assert (dst / filename).exists()


def test_copier_phase_variable(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): (
                """
                _tasks:
                    - touch {{ _copier_phase }}
                """
            )
        }
    )
    copier.run_copy(str(src), dst, unsafe=True)
    assert (dst / "tasks").exists()


def _python_task(code: str) -> list[str]:
    return ["{{ _copier_python }}", "-c", code]


def _touch_task(path: str) -> list[str]:
    return _python_task(f"open({path!r}, 'w').close()")


def _sleep_task(seconds: int = 30) -> list[str]:
    return _python_task(f"import time; time.sleep({seconds})")


def test_task_timeout_stops_following_tasks(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): f"""\
            _tasks:
                -   command: {_sleep_task()!r}
                -   command: {_touch_task("after-timeout")!r}
            """
        }
    )
    with pytest.raises(TaskTimeoutError) as error:
        copier.run_copy(
            str(src),
            dst,
            quiet=True,
            unsafe=True,
            cleanup_on_error=False,
            task_timeout=0.5,
        )
    assert error.value.stage == "tasks"
    assert error.value.task_number == 1
    assert error.value.timeout == 0.5
    assert error.value.command[1:] == _sleep_task()[1:]
    assert "timed out after 0.5 seconds" in str(error.value)
    assert not (dst / "after-timeout").exists()


def test_task_timeout_kills_child_holding_output(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    script = (
        "import subprocess, sys, time; "
        "subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)']); "
        "time.sleep(30)"
    )
    build_file_tree(
        {
            (src / "copier.yml"): f"""\
            _tasks:
                -   command: ["{{{{ _copier_python }}}}", "-c", "{script}"]
            """
        }
    )
    start = time.monotonic()
    with pytest.raises(TaskTimeoutError):
        copier.run_copy(
            str(src),
            dst,
            quiet=True,
            unsafe=True,
            cleanup_on_error=False,
            task_timeout=0.5,
        )
    assert time.monotonic() - start < 30


def test_template_task_timeout(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): f"""\
            _task_timeout: 0.5
            _tasks:
                -   command: {_sleep_task()!r}
            """
        }
    )
    with pytest.raises(TaskTimeoutError):
        copier.run_copy(str(src), dst, quiet=True, unsafe=True, cleanup_on_error=False)


def test_task_timeout_overrides_cli_and_template(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): f"""\
            _task_timeout: 30
            _tasks:
                -   command: {_sleep_task(1)!r}
                    timeout: 5
                -   command: {_sleep_task()!r}
            """
        }
    )
    with pytest.raises(TaskTimeoutError) as error:
        copier.run_copy(
            str(src),
            dst,
            quiet=True,
            unsafe=True,
            cleanup_on_error=False,
            task_timeout=0.2,
        )
    assert error.value.task_number == 2


def test_task_completes_within_timeout(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): f"""\
            _task_timeout: 5
            _tasks:
                -   command: {_touch_task("completed")!r}
            """
        }
    )
    copier.run_copy(str(src), dst, quiet=True, unsafe=True)
    assert (dst / "completed").exists()


def test_nonzero_exit_with_timeout_raises_task_error(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): """\
            _tasks:
                -   command: ["{{ _copier_python }}", "-c", "raise SystemExit(3)"]
            """
        }
    )
    with pytest.raises(TaskError) as error:
        copier.run_copy(
            str(src),
            dst,
            quiet=True,
            unsafe=True,
            cleanup_on_error=False,
            task_timeout=5,
        )
    assert not isinstance(error.value, TaskTimeoutError)
    assert error.value.returncode == 3


def test_pretend_ignores_task_timeout(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): f"""\
            _tasks:
                -   command: {_sleep_task()!r}
            """
        }
    )
    start = time.monotonic()
    copier.run_copy(str(src), dst, pretend=True, unsafe=True, task_timeout=0.01)
    assert time.monotonic() - start < 30


def test_task_timeout_cli(tmp_path_factory: pytest.TempPathFactory) -> None:
    src, dst = map(tmp_path_factory.mktemp, ("src", "dst"))
    build_file_tree(
        {
            (src / "copier.yml"): f"""\
            _tasks:
                -   command: {_sleep_task()!r}
            """
        }
    )
    _, retcode = CopierApp.run(
        [
            "copier",
            "copy",
            "--UNSAFE",
            "--task-timeout",
            "0.5",
            str(src),
            str(dst),
        ],
        exit=False,
    )
    assert retcode == 1
