from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
_TASK_CAPSULE_ARTIFACT_NAMES = (
    "complete.mv2",
    "complete.memory.json",
    "complete.mv2.records.json",
)


def ensure_private_directory(path: Path) -> None:
    """Create a 0700 sensitive leaf without changing an existing custom directory."""

    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    created = False
    try:
        resolved.mkdir(mode=PRIVATE_DIRECTORY_MODE)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise ValueError(f"Unable to prepare sensitive artifact directory: {resolved}") from exc
    before_open = os.lstat(resolved)
    _validate_directory(before_open, resolved)
    if os.name == "nt" or not created:
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(resolved)
        _validate_directory(opened, resolved)
        _validate_directory(after_open, resolved)
        if not os.path.samestat(before_open, opened) or not os.path.samestat(opened, after_open):
            raise ValueError(f"Sensitive artifact directory changed during validation: {resolved}")
        os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)
    finally:
        os.close(descriptor)


def ensure_owner_only_directory(path: Path) -> None:
    """Create or repair a dedicated Kestrel directory as owner-only."""

    resolved = Path(path)
    ensure_private_directory(resolved)
    _harden_owner_only_directory(resolved)


def harden_private_file(path: Path, *, missing_ok: bool = False) -> bool:
    """Verify a sensitive file without following links, then tighten it to 0600."""

    resolved = Path(path)
    try:
        before_open = os.lstat(resolved)
    except FileNotFoundError:
        if missing_ok:
            return False
        raise
    _validate_file(before_open, resolved)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(resolved)
        _validate_file(opened, resolved)
        _validate_file(after_open, resolved)
        if not os.path.samestat(before_open, opened) or not os.path.samestat(opened, after_open):
            raise ValueError(f"Sensitive artifact changed during validation: {resolved}")
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)
    return True


def read_private_text(
    path: Path,
    *,
    encoding: str = "utf-8",
    missing_ok: bool = False,
) -> str | None:
    """Read a sensitive file through the same verified descriptor that is hardened."""

    resolved = Path(path)
    try:
        before_open = os.lstat(resolved)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    _validate_file(before_open, resolved)

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(resolved)
        _validate_file(opened, resolved)
        _validate_file(after_open, resolved)
        if not os.path.samestat(before_open, opened) or not os.path.samestat(opened, after_open):
            raise ValueError(f"Sensitive artifact changed during validation: {resolved}")
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "r", encoding=encoding) as handle:
            descriptor = -1
            return handle.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def create_private_empty_file(path: Path) -> None:
    """Create a non-Memvid owner-only file, or safely tighten an existing one."""

    resolved = Path(path)
    ensure_private_directory(resolved.parent)
    if harden_private_file(resolved, missing_ok=True):
        return
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags, PRIVATE_FILE_MODE)
    except FileExistsError:
        harden_private_file(resolved)
        return
    try:
        metadata = os.fstat(descriptor)
        _validate_file(metadata, resolved)
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
    finally:
        os.close(descriptor)


def open_private_file_descriptor(path: Path) -> int:
    """Open or create a private file without following or mutating aliases."""

    resolved = Path(path)
    ensure_private_directory(resolved.parent)
    try:
        before_open = os.lstat(resolved)
    except FileNotFoundError:
        before_open = None
    if before_open is not None:
        _validate_file(before_open, resolved)

    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(resolved, flags, PRIVATE_FILE_MODE)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(resolved)
        _validate_file(opened, resolved)
        _validate_file(after_open, resolved)
        if not os.path.samestat(opened, after_open) or (
            before_open is not None and not os.path.samestat(before_open, opened)
        ):
            raise ValueError(f"Sensitive artifact changed during validation: {resolved}")
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def write_private_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Atomically replace a sensitive text artifact from an owner-only temp file."""

    resolved = Path(path)
    ensure_private_directory(resolved.parent)
    harden_private_file(resolved, missing_ok=True)
    temporary = resolved.with_name(f".{resolved.name}.{uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, PRIVATE_FILE_MODE)
    try:
        metadata = os.fstat(descriptor)
        _validate_file(metadata, temporary)
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "w", encoding=encoding) as handle:
            descriptor = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        harden_private_file(resolved, missing_ok=True)
        os.replace(temporary, resolved)
        harden_private_file(resolved)
        _fsync_directory(resolved.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_private_text_exclusive(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    """Create an immutable-by-convention private text artifact exactly once.

    Unlike :func:`write_private_text`, this helper never replaces an existing
    path.  It is intended for signed receipts whose identifier is part of the
    approval boundary, so an accidental collision or concurrent writer must
    fail closed instead of silently changing prior evidence.
    """

    resolved = Path(path)
    ensure_private_directory(resolved.parent)
    temporary = resolved.with_name(f"{resolved.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temporary, flags, PRIVATE_FILE_MODE)
    try:
        metadata = os.fstat(descriptor)
        _validate_file(metadata, temporary)
        if os.name != "nt":
            os.fchmod(descriptor, PRIVATE_FILE_MODE)
        _write_private_bytes(descriptor, text.encode(encoding))
        _sync_private_file(descriptor)
        os.close(descriptor)
        descriptor = -1
        _publish_private_file_exclusive(temporary, resolved)
        temporary.unlink()
        harden_private_file(resolved)
        _fsync_directory(resolved.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_private_bytes(descriptor: int, payload: bytes) -> None:
    remaining = memoryview(payload)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Private artifact write made no progress.")
        remaining = remaining[written:]


def _sync_private_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _publish_private_file_exclusive(temporary: Path, resolved: Path) -> None:
    os.link(temporary, resolved, follow_symlinks=False)


def prepare_private_sqlite_file(path: Path) -> None:
    """Validate SQLite targets before connect and privately create the main file."""

    resolved = Path(path)
    if resolved.suffix.lower() == ".mv2":
        raise ValueError("Refusing to precreate a Memvid .mv2 path as SQLite storage")
    ensure_private_directory(resolved.parent)
    for candidate in sqlite_artifact_paths(resolved):
        harden_private_file(candidate, missing_ok=True)
    create_private_empty_file(resolved)


def harden_private_sqlite_files(path: Path) -> None:
    for candidate in sqlite_artifact_paths(Path(path)):
        harden_private_file(candidate, missing_ok=True)


def reset_disposable_private_sqlite_files(path: Path) -> None:
    """Remove one disposable SQLite database and its exact transient siblings.

    This helper is intentionally narrower than a generic recursive cleanup.  It
    validates every existing artifact as a current-user-owned, single-link
    regular file before unlinking any of them.  The caller can then recreate the
    index from its canonical source of truth.
    """

    resolved = Path(path)
    if resolved.suffix.lower() == ".mv2":
        raise ValueError("Refusing to reset a Memvid .mv2 path as SQLite storage")
    ensure_private_directory(resolved.parent)
    candidates = sqlite_artifact_paths(resolved)
    if any(candidate.parent != resolved.parent for candidate in candidates):
        raise ValueError("SQLite cleanup candidates must share one direct parent")

    if os.name == "nt":
        # Windows cannot unlink an open file. Validate the complete set first,
        # then remove only the four exact SQLite artifact names.
        for candidate in candidates:
            harden_private_file(candidate, missing_ok=True)
        for candidate in candidates:
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass
        return

    before_directory = os.lstat(resolved.parent)
    _validate_directory(before_directory, resolved.parent)
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_descriptor = os.open(resolved.parent, directory_flags)
    opened: list[tuple[str, int, os.stat_result]] = []
    try:
        opened_directory = os.fstat(directory_descriptor)
        after_directory = os.lstat(resolved.parent)
        _validate_directory(opened_directory, resolved.parent)
        _validate_directory(after_directory, resolved.parent)
        if not os.path.samestat(before_directory, opened_directory) or not os.path.samestat(
            opened_directory, after_directory
        ):
            raise ValueError(
                f"Sensitive artifact directory changed during validation: {resolved.parent}"
            )

        file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        file_flags |= getattr(os, "O_NOFOLLOW", 0)
        for candidate in candidates:
            try:
                visible_before_open = os.stat(
                    candidate.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            _validate_file(visible_before_open, candidate)
            try:
                descriptor = os.open(candidate.name, file_flags, dir_fd=directory_descriptor)
            except FileNotFoundError:
                raise ValueError(
                    f"Sensitive artifact changed during validation: {candidate}"
                ) from None
            try:
                metadata = os.fstat(descriptor)
                visible = os.stat(
                    candidate.name,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                _validate_file(metadata, candidate)
                _validate_file(visible, candidate)
                if not os.path.samestat(visible_before_open, metadata) or not os.path.samestat(
                    metadata, visible
                ):
                    raise ValueError(
                        f"Sensitive artifact changed during validation: {candidate}"
                    )
            except Exception:
                os.close(descriptor)
                raise
            opened.append((candidate.name, descriptor, metadata))

        for name, _descriptor, metadata in opened:
            visible = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
            if not os.path.samestat(metadata, visible):
                raise ValueError(
                    f"Sensitive artifact changed before disposable reset: {resolved.parent / name}"
                )
            os.unlink(name, dir_fd=directory_descriptor)
        os.fsync(directory_descriptor)
    finally:
        for _name, descriptor, _metadata in opened:
            os.close(descriptor)
        os.close(directory_descriptor)


def harden_memory_artifact_files(path: Path) -> bool:
    """Harden every known backend variant for one logical memory layer.

    Returns whether the canonical path already exists. Missing paths are never
    created, which is critical for Memvid's create-once `.mv2` lifecycle.
    """

    canonical = Path(path)
    canonical_exists = False
    for index, candidate in enumerate(memory_artifact_paths(canonical)):
        exists = harden_private_file(candidate, missing_ok=True)
        if index == 0:
            canonical_exists = exists
    return canonical_exists


def memory_artifact_paths(path: Path) -> tuple[Path, ...]:
    return (
        path,
        path.with_suffix(".memory.json"),
        path.with_suffix(f"{path.suffix}.records.json"),
    )


def harden_task_capsule_run(run_directory: Path) -> bool:
    """Harden only the known artifacts for one accessed direct run directory."""

    resolved = Path(run_directory)
    try:
        metadata = os.lstat(resolved)
    except FileNotFoundError:
        return False
    _validate_directory(metadata, resolved)
    _harden_owner_only_directory(resolved)
    for name in _TASK_CAPSULE_ARTIFACT_NAMES:
        harden_private_file(resolved / name, missing_ok=True)
    return True


def sqlite_artifact_paths(path: Path) -> tuple[Path, ...]:
    return (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    )


def _validate_directory(metadata: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"Sensitive artifact directories must not be symbolic links: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Sensitive artifact directory must be a directory: {path}")
    _require_current_owner(metadata, path)


def _harden_owner_only_directory(path: Path) -> None:
    before_open = os.lstat(path)
    _validate_directory(before_open, path)
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(path)
        _validate_directory(opened, path)
        _validate_directory(after_open, path)
        if not os.path.samestat(before_open, opened) or not os.path.samestat(opened, after_open):
            raise ValueError(f"Sensitive artifact directory changed during validation: {path}")
        os.fchmod(descriptor, PRIVATE_DIRECTORY_MODE)
    finally:
        os.close(descriptor)


def _validate_file(metadata: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"Sensitive artifacts must not be symbolic links: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Sensitive artifacts must be regular files: {path}")
    if metadata.st_nlink != 1:
        raise ValueError(f"Sensitive artifacts must not be hard-linked: {path}")
    _require_current_owner(metadata, path)


def _require_current_owner(metadata: os.stat_result, path: Path) -> None:
    if os.name == "nt":
        return
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and metadata.st_uid != geteuid():
        raise PermissionError(f"Sensitive artifacts must be owned by the current user: {path}")


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
