"""Transactional snapshots used by transactional project updates."""

from __future__ import annotations

import filecmp
import shutil
import stat
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from ._tools import handle_remove_readonly
from ._vcs import get_git

__all__ = ["_UpdateTransaction"]

_EntryKind = Literal["file", "symlink", "directory", "other"]
_GIT_DIR_NAME = ".git"


@dataclass(frozen=True)
class _SnapshotEntry:
    """A single path recorded in the update snapshot."""

    kind: _EntryKind
    mode: int
    target: str | None = None


class _UpdateTransaction:
    """Snapshot a project before updating it, restoring it if the update fails.

    The snapshot records every path in the project directory (file contents,
    symlinks, executable bits and the Git index). Existing content is only
    touched on rollback if it changed after the snapshot was taken; newly
    created paths are removed, while paths outside the project are never
    modified.

    Args:
        project_root: Absolute path of the project directory to snapshot.
        tmp_root: Optional directory where the snapshot is stored. A temporary
            directory is used when omitted.
    """

    def __init__(self, project_root: Path, tmp_root: Path | None = None) -> None:
        self._project_root = project_root
        self._snapshot_dir: Path | None = None
        self._entries: dict[Path, _SnapshotEntry] = {}
        self._index_path: Path | None = None
        self._index_contents: bytes | None = None
        self._index_existed = False
        self._index_mode: int | None = None
        self._tmp: TemporaryDirectory[str] | None = None
        if tmp_root is not None:
            self._snapshot_dir = tmp_root / "snapshot"
            self._snapshot_dir.mkdir(parents=True)
        else:
            self._tmp = TemporaryDirectory(prefix=f"{__name__}.transaction.")
            self._snapshot_dir = Path(self._tmp.name) / "snapshot"
            self._snapshot_dir.mkdir(parents=True)

    @property
    def _files_root(self) -> Path:
        assert self._snapshot_dir is not None
        return self._snapshot_dir / "files"

    def begin(self) -> None:
        """Record the current project state.

        Raises:
            OSError: If part of the project cannot be read while recording it.
        """
        assert self._snapshot_dir is not None
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._files_root.mkdir(parents=True, exist_ok=True)
        self._entries.clear()
        self._record_directory(self._project_root, Path())
        self._record_git_index()

    def _record_directory(self, directory: Path, rel_directory: Path) -> None:
        for child in sorted(directory.iterdir()):
            rel_path = rel_directory / child.name
            st = child.lstat()
            if stat.S_ISLNK(st.st_mode):
                self._entries[rel_path] = _SnapshotEntry(
                    "symlink", st.st_mode, str(child.readlink())
                )
            elif stat.S_ISDIR(st.st_mode):
                if child.name == _GIT_DIR_NAME:
                    continue
                self._entries[rel_path] = _SnapshotEntry("directory", st.st_mode)
                self._record_directory(child, rel_path)
            elif stat.S_ISREG(st.st_mode):
                backup = self._files_root / rel_path
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(child, backup)
                self._entries[rel_path] = _SnapshotEntry("file", st.st_mode)
            else:
                # Special files cannot be backed up, but they must stay in the
                # snapshot scope so rollback does not delete them.
                self._entries[rel_path] = _SnapshotEntry("other", st.st_mode)

    def _record_git_index(self) -> None:
        git = get_git(self._project_root)
        try:
            raw_path = git("rev-parse", "--git-path", "index").strip()
        except Exception:  # noqa: BLE001
            return
        index_path = Path(raw_path)
        if not index_path.is_absolute():
            index_path = (self._project_root / index_path).resolve()
        self._index_path = index_path
        self._index_existed = index_path.exists()
        try:
            self._index_contents = index_path.read_bytes()
        except OSError:
            self._index_contents = None
        else:
            self._index_mode = index_path.stat().st_mode

    def commit(self) -> None:
        """Publish the updated state and discard the snapshot."""
        self.cleanup()

    def cleanup(self) -> None:
        """Discard the snapshot, keeping the project untouched."""
        if self._tmp is not None:
            suppress_errors = suppress(OSError, PermissionError)
            with suppress_errors:
                self._tmp.cleanup()
            self._tmp = None
        elif self._snapshot_dir is not None:
            with suppress(OSError):
                shutil.rmtree(self._snapshot_dir, ignore_errors=True)
        self._entries.clear()

    def rollback(self) -> list[Path]:
        """Restore the project state recorded by `begin`.

        Only paths that changed after the snapshot was taken are considered.
        Paths that did not exist before the update are removed, and existing
        content is never deleted.

        Returns:
            Relative paths that could not be restored.
        """
        try:
            return self._rollback()
        except BaseException:  # noqa: BLE001
            return list(self._entries)

    def _rollback(self) -> list[Path]:
        failures: list[Path] = []
        snapshot_paths = set(self._entries)
        current_paths = set(self._iter_current_paths())
        # Restore or remove paths that were added after the snapshot.
        for rel_path in sorted(
            current_paths - snapshot_paths, key=_depth, reverse=True
        ):
            try:
                self._remove_path(rel_path)
            except BaseException:  # noqa: BLE001
                failures.append(rel_path)
        # Restore paths from the snapshot that changed or disappeared.
        for rel_path, entry in sorted(
            self._entries.items(), key=lambda item: _depth(item[0])
        ):
            try:
                self._restore_entry(rel_path, entry)
            except BaseException:  # noqa: BLE001
                failures.append(rel_path)
        if not self._restore_git_index():
            failures.append(
                self._index_path if self._index_path is not None else Path("index")
            )
        return failures

    def _iter_current_paths(self) -> Iterable[Path]:
        yield from self._walk_directory(self._project_root, Path())

    def _walk_directory(self, directory: Path, rel_directory: Path) -> Iterable[Path]:
        children: list[Path] = []
        with suppress(FileNotFoundError):
            children = sorted(directory.iterdir())
        for child in children:
            rel_path = rel_directory / child.name
            st = child.lstat()
            if stat.S_ISDIR(st.st_mode) and not stat.S_ISLNK(st.st_mode):
                if child.name == _GIT_DIR_NAME:
                    continue
                yield rel_path
                yield from self._walk_directory(child, rel_path)
            else:
                yield rel_path

    def _restore_entry(self, rel_path: Path, entry: _SnapshotEntry) -> None:
        target = self._project_root / rel_path
        if entry.kind == "other":
            # Special files are outside the restorable scope.
            return
        if entry.kind == "directory":
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                self._remove_path(rel_path)
            target.mkdir(parents=True, exist_ok=True)
            return
        if entry.kind == "file":
            if (
                target.is_file()
                and not target.is_symlink()
                and filecmp.cmp(self._files_root / rel_path, target, shallow=False)
            ):
                if _mode_bits(target.stat().st_mode) != _mode_bits(entry.mode):
                    target.chmod(stat.S_IMODE(entry.mode))
                return
            self._remove_path(rel_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self._files_root / rel_path, target)
            target.chmod(stat.S_IMODE(entry.mode))
            return
        # Symlink
        assert entry.target is not None
        if target.is_symlink() and str(target.readlink()) == entry.target:
            return
        self._remove_path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(entry.target)

    def _remove_path(self, rel_path: Path) -> None:
        target = self._project_root / rel_path
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target, onerror=handle_remove_readonly)

    def _restore_git_index(self) -> bool:
        if self._index_path is None:
            return True
        if self._index_contents is None:
            if not self._index_existed:
                # The index did not exist before the update; remove any index
                # created while updating.
                with suppress(OSError):
                    self._index_path.unlink(missing_ok=True)
                return not self._index_path.exists()
            # The index existed but could not be read; do not touch it.
            return False
        try:
            current = self._index_path.read_bytes()
        except OSError:
            current = None
        if current == self._index_contents:
            return True
        try:
            self._index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._index_path.with_name(f"{self._index_path.name}.tmp")
            tmp_path.write_bytes(self._index_contents)
            tmp_path.replace(self._index_path)
            if self._index_mode is not None:
                self._index_path.chmod(stat.S_IMODE(self._index_mode))
        except OSError:
            return False
        return True


def _depth(path: Path) -> int:
    return len(path.parts)


def _mode_bits(mode: int) -> int:
    return stat.S_IMODE(mode) & 0o777
