from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO
from uuid import uuid4

from .file_lock import lock_exclusive, unlock


class ExtensionTransactionError(RuntimeError):
    """Raised when an extension filesystem transaction cannot be completed."""


class ExtensionCleanupIncompleteError(ExtensionTransactionError):
    """Raised when rollback or cleanup cannot be proven complete."""


def path_exists(path: Path) -> bool:
    """Return true for any directory entry, including a broken symlink."""

    return os.path.lexists(path)


def ensure_real_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ExtensionTransactionError(f"Extension directory is unavailable: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ExtensionTransactionError(f"Extension path must be a real directory: {path}")


@contextmanager
def extension_lock(root: Path, name: str) -> Iterator[None]:
    """Serialize extension swaps across processes using a no-follow lock file."""

    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_real_directory(root)
    lock_path = root / name
    try:
        lock_metadata = lock_path.lstat()
    except FileNotFoundError:
        lock_metadata = None
    if lock_metadata is not None and (
        stat.S_ISLNK(lock_metadata.st_mode) or not stat.S_ISREG(lock_metadata.st_mode)
    ):
        raise ExtensionTransactionError("Extension transaction lock must be a regular file.")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    handle: IO[str] | None = None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ExtensionTransactionError("Extension transaction lock must be a regular file.")
        if lock_metadata is not None and _stat_identity(lock_metadata) != _stat_identity(metadata):
            raise ExtensionTransactionError("Extension transaction lock changed while opening.")
        if _stat_identity(lock_path.lstat()) != _stat_identity(metadata):
            raise ExtensionTransactionError("Extension transaction lock was replaced while opening.")
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        descriptor = -1
        lock_exclusive(handle)
        yield
    finally:
        if handle is not None:
            try:
                unlock(handle)
            finally:
                handle.close()
        elif descriptor >= 0:
            os.close(descriptor)


def create_sibling_stage(parent: Path, *, prefix: str) -> Path:
    ensure_real_directory(parent)
    return Path(tempfile.mkdtemp(prefix=f".{prefix}.stage-", dir=parent))


def copy_regular_tree(source: Path, destination: Path) -> None:
    """Copy a real extension tree into an existing empty sibling stage."""

    ensure_real_directory(source)
    ensure_real_directory(destination)
    with os.scandir(destination) as entries:
        if next(entries, None) is not None:
            raise ExtensionTransactionError("Extension copy destination must be empty.")
    _copy_regular_tree_contents(source, destination)
    fsync_tree(destination)


def write_regular_file(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Create and fsync one staged file without following links."""

    ensure_real_directory(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    completed = False
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ExtensionTransactionError(f"Staged extension file is not regular: {path}")
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short extension file write")
            view = view[written:]
        os.fsync(descriptor)
        completed = True
    finally:
        os.close(descriptor)
        if not completed:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def read_regular_file(path: Path) -> bytes:
    """Read one immutable snapshot from a no-follow regular-file descriptor."""

    path_before = path.lstat()
    if stat.S_ISLNK(path_before.st_mode) or not stat.S_ISREG(path_before.st_mode):
        raise ExtensionTransactionError(f"Extension file is not a regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ExtensionTransactionError(f"Extension file is not a regular file: {path}")
        if _stat_identity(path_before) != _stat_identity(before):
            raise ExtensionTransactionError(f"Extension file changed while opening: {path}")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 1024 * 1024):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if _stat_identity(before) != _stat_identity(after):
            raise ExtensionTransactionError(f"Extension file changed during validation: {path}")
        path_after = path.lstat()
        if _stat_identity(after) != _stat_identity(path_after):
            raise ExtensionTransactionError(f"Extension file changed after validation: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def read_regular_text(path: Path) -> str:
    return read_regular_file(path).decode("utf-8")


def fsync_tree(root: Path) -> None:
    """Reject links/special files and durably flush an extension tree."""

    ensure_real_directory(root)
    directories = _walk_real_tree(root)
    for directory in reversed(directories):
        fsync_directory(directory)


def fsync_directory(path: Path) -> None:
    if os.name == "nt":
        # CPython cannot open directory handles with backup semantics. File
        # contents are flushed individually and directory replacement remains
        # atomic, while the parent metadata flush is unavailable on Windows.
        ensure_real_directory(path)
        return
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ExtensionTransactionError(f"Extension path is not a directory: {path}")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def remove_tree_verified(path: Path) -> None:
    if not path_exists(path):
        return
    ensure_real_directory(path)
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise ExtensionCleanupIncompleteError(
            f"Extension cleanup could not remove staged tree: {path}"
        ) from exc
    if path_exists(path):
        raise ExtensionCleanupIncompleteError(
            f"Extension cleanup could not prove staged tree removal: {path}"
        )
    fsync_directory(path.parent)


@dataclass
class DirectorySwap:
    """Atomic sibling directory replacement with a recoverable old generation."""

    live: Path
    stage: Path
    rollback: Path | None = None
    displaced: bool = False
    published: bool = False

    def publish(self) -> None:
        ensure_real_directory(self.live.parent)
        ensure_real_directory(self.stage)
        self.rollback = self.live.parent / f".{self.live.name}.rollback-{uuid4().hex}"
        try:
            if path_exists(self.live):
                ensure_real_directory(self.live)
                os.replace(self.live, self.rollback)
                self.displaced = True
                fsync_directory(self.live.parent)
            os.replace(self.stage, self.live)
            self.published = True
            fsync_directory(self.live.parent)
        except BaseException as exc:
            try:
                self.restore()
            except BaseException as rollback_exc:
                raise ExtensionCleanupIncompleteError(
                    "Extension publish failed and exact filesystem rollback could not be proven."
                ) from rollback_exc
            raise exc

    def restore(self) -> None:
        failed_tree: Path | None = None
        if self.published and path_exists(self.live):
            failed_tree = self.live.parent / f".{self.live.name}.failed-{uuid4().hex}"
            os.replace(self.live, failed_tree)
            self.published = False
        if self.displaced:
            if self.rollback is None or not path_exists(self.rollback):
                raise ExtensionCleanupIncompleteError("Extension rollback generation is missing.")
            os.replace(self.rollback, self.live)
            self.displaced = False
        fsync_directory(self.live.parent)
        if failed_tree is not None:
            remove_tree_verified(failed_tree)
        if self.displaced or self.published:
            raise ExtensionCleanupIncompleteError("Extension rollback state remains unresolved.")

    def finalize(self) -> None:
        if self.rollback is not None and path_exists(self.rollback):
            remove_tree_verified(self.rollback)
        self.displaced = False
        self.published = False


@dataclass
class DirectoryRemoval:
    """Atomically hide a live tree until its state-row deletion commits."""

    live: Path
    rollback: Path | None = None
    displaced: bool = False

    def hide(self) -> None:
        if not path_exists(self.live):
            return
        ensure_real_directory(self.live)
        self.rollback = self.live.parent / f".{self.live.name}.rollback-{uuid4().hex}"
        os.replace(self.live, self.rollback)
        self.displaced = True
        try:
            fsync_directory(self.live.parent)
        except BaseException as exc:
            try:
                self.restore()
            except BaseException as rollback_exc:
                raise ExtensionCleanupIncompleteError(
                    "Extension removal could not restore the live generation."
                ) from rollback_exc
            raise exc

    def restore(self) -> None:
        if not self.displaced:
            return
        if self.rollback is None or not path_exists(self.rollback):
            raise ExtensionCleanupIncompleteError("Extension removal rollback is missing.")
        if path_exists(self.live):
            raise ExtensionCleanupIncompleteError("Extension live path was recreated during removal.")
        os.replace(self.rollback, self.live)
        fsync_directory(self.live.parent)
        self.displaced = False

    def finalize(self) -> None:
        if self.rollback is not None and path_exists(self.rollback):
            remove_tree_verified(self.rollback)
        self.displaced = False


def _walk_real_tree(root: Path) -> list[Path]:
    ensure_real_directory(root)
    directories = [root]
    try:
        with os.scandir(root) as scanned:
            entries = sorted(scanned, key=lambda entry: entry.name)
    except OSError as exc:
        raise ExtensionTransactionError(f"Extension tree is unreadable: {root}") from exc
    for entry in entries:
        path = Path(entry.path)
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ExtensionTransactionError(f"Extension tree contains a symbolic link: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            directories.extend(_walk_real_tree(path))
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ExtensionTransactionError(f"Extension tree contains a non-regular file: {path}")
        open_mode = os.O_RDWR if os.name == "nt" else os.O_RDONLY
        descriptor = os.open(
            path,
            open_mode | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            opened = os.fstat(descriptor)
            if _stat_identity(metadata) != _stat_identity(opened):
                raise ExtensionTransactionError(f"Extension tree changed during fsync: {path}")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return directories


def _copy_regular_tree_contents(source: Path, destination: Path) -> None:
    ensure_real_directory(source)
    ensure_real_directory(destination)
    with os.scandir(source) as scanned:
        entries = sorted(scanned, key=lambda entry: entry.name)
    for entry in entries:
        source_path = Path(entry.path)
        destination_path = destination / entry.name
        metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(metadata.st_mode):
            raise ExtensionTransactionError(
                f"Extension tree contains a symbolic link: {source_path}"
            )
        if stat.S_ISDIR(metadata.st_mode):
            destination_path.mkdir(mode=0o700)
            _copy_regular_tree_contents(source_path, destination_path)
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ExtensionTransactionError(
                f"Extension tree contains a non-regular file: {source_path}"
            )
        content = read_regular_file(source_path)
        mode = stat.S_IMODE(metadata.st_mode) & 0o700
        write_regular_file(destination_path, content, mode=mode or 0o600)


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
    )
