from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, cast
from uuid import uuid4

from .file_lock import lock_exclusive, lock_shared, unlock
from .security_boundary import redact_secrets as redact_secrets

_LOG_DIRECTORY_MODE = 0o700
_EVENT_FILE_MODE = 0o600
_EVENT_TAIL_CHUNK_BYTES = 64 * 1024
_EVENT_TAIL_MAX_BYTES = 1024 * 1024
_EVENT_TAIL_MAX_LINES = 500


@dataclass(frozen=True)
class AgentEvent:
    type: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: f"evt_{uuid4().hex}")
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class JsonlEventLog:
    """Raw audit log. This is intentionally not a retrieval database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        _prepare_event_log_storage(self.path)

    def append(self, event: AgentEvent) -> None:
        event = AgentEvent(
            type=event.type,
            payload=redact_secrets(event.payload),
            id=event.id,
            created_at=event.created_at,
        )
        line = json.dumps(asdict(event), ensure_ascii=False) + "\n"
        if os.name == "nt":
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
            return
        descriptor = _open_private_event_file(
            self.path,
            access_flags=os.O_WRONLY | os.O_APPEND,
            create=True,
        )
        if descriptor is None:
            raise RuntimeError("event log file creation did not persist")
        with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
            lock_exclusive(handle)
            try:
                handle.write(line)
                handle.flush()
            finally:
                unlock(handle)

    def tail(self, limit: int = 50) -> list[AgentEvent]:
        lines = read_bounded_jsonl_tail(self.path, limit=limit)
        events: list[AgentEvent] = []
        for line in lines:
            raw = json.loads(line)
            events.append(
                AgentEvent(
                    id=raw["id"],
                    type=raw["type"],
                    payload=raw["payload"],
                    created_at=raw["created_at"],
                )
            )
        return events


def read_bounded_jsonl_tail(
    path: Path,
    *,
    limit: int,
    max_bytes: int | None = None,
) -> list[str]:
    """Read complete trailing JSONL lines without loading an unbounded log.

    A leading partial line is discarded when the byte cap is reached. This
    keeps diagnostics bounded even if an old event is unusually large.
    """

    bounded_lines = max(0, min(int(limit), _EVENT_TAIL_MAX_LINES))
    bounded_bytes = _EVENT_TAIL_MAX_BYTES if max_bytes is None else int(max_bytes)
    bounded_bytes = max(0, min(bounded_bytes, _EVENT_TAIL_MAX_BYTES))
    if bounded_lines == 0 or bounded_bytes == 0:
        return []
    descriptor: int | None
    if os.name == "nt":
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return []
    else:
        descriptor = _open_private_event_file(
            path,
            access_flags=os.O_RDONLY,
            create=False,
        )
        if descriptor is None:
            return []
    with os.fdopen(descriptor, "rb") as handle:
        lock_handle = cast(IO[str], handle)
        if os.name != "nt":
            lock_shared(lock_handle)
        try:
            return _read_bounded_tail_lines(
                handle,
                limit=bounded_lines,
                max_bytes=bounded_bytes,
            )
        finally:
            if os.name != "nt":
                unlock(lock_handle)


def _read_bounded_tail_lines(
    handle: Any,
    *,
    limit: int,
    max_bytes: int,
) -> list[str]:
    handle.seek(0, os.SEEK_END)
    position = int(handle.tell())
    remaining = min(position, max_bytes)
    chunks: list[bytes] = []
    newline_count = 0
    while position > 0 and remaining > 0 and newline_count <= limit:
        chunk_size = min(_EVENT_TAIL_CHUNK_BYTES, position, remaining)
        position -= chunk_size
        chunk = _read_tail_chunk(handle, position, chunk_size)
        if not chunk:
            break
        chunks.append(chunk)
        newline_count += chunk.count(b"\n")
        remaining -= len(chunk)

    payload = b"".join(reversed(chunks))
    if position > 0:
        first_boundary = payload.find(b"\n")
        if first_boundary < 0:
            return []
        payload = payload[first_boundary + 1 :]
    return [line.decode("utf-8") for line in payload.splitlines()[-limit:]]


def _read_tail_chunk(handle: Any, offset: int, size: int) -> bytes:
    handle.seek(offset)
    return bytes(handle.read(size))


def _prepare_event_log_storage(path: Path) -> None:
    created_directory = _create_log_directory(path.parent)
    if os.name == "nt":
        return
    directory_fd = _open_owned_log_directory(path.parent)
    try:
        if created_directory:
            os.fchmod(directory_fd, _LOG_DIRECTORY_MODE)
        descriptor = _open_event_entry(
            directory_fd,
            path.name,
            display_path=path,
            access_flags=os.O_RDONLY,
            create=False,
        )
        if descriptor is not None:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def _create_log_directory(directory: Path) -> bool:
    try:
        directory.mkdir(mode=_LOG_DIRECTORY_MODE)
    except FileNotFoundError:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir(mode=_LOG_DIRECTORY_MODE)
        except FileExistsError:
            return False
    except FileExistsError:
        return False
    return True


def _open_private_event_file(
    path: Path,
    *,
    access_flags: int,
    create: bool,
) -> int | None:
    directory_fd = _open_owned_log_directory(path.parent)
    try:
        return _open_event_entry(
            directory_fd,
            path.name,
            display_path=path,
            access_flags=access_flags,
            create=create,
        )
    finally:
        os.close(directory_fd)


def _open_owned_log_directory(directory: Path) -> int:
    if directory.is_symlink():
        raise ValueError(f"event log directory must not be a symbolic link: {directory}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(directory, flags)
    except OSError as exc:
        if directory.is_symlink():
            raise ValueError(
                f"event log directory must not be a symbolic link: {directory}"
            ) from exc
        raise
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"event log directory must be a directory: {directory}")
        _require_current_owner(metadata, directory)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _open_event_entry(
    directory_fd: int,
    name: str,
    *,
    display_path: Path,
    access_flags: int,
    create: bool,
) -> int | None:
    flags = (
        access_flags
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except FileNotFoundError:
        if not create:
            return None
        try:
            descriptor = os.open(
                name,
                flags | os.O_CREAT | os.O_EXCL,
                _EVENT_FILE_MODE,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            descriptor = os.open(name, flags, dir_fd=directory_fd)
    except PermissionError:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _validate_event_file(metadata, display_path)
        os.chmod(
            name,
            _EVENT_FILE_MODE,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        descriptor = os.open(name, flags, dir_fd=directory_fd)
    except OSError as exc:
        try:
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError:
            raise exc from None
        _validate_event_file(metadata, display_path)
        raise
    try:
        metadata = os.fstat(descriptor)
        _validate_event_file(metadata, display_path)
        os.fchmod(descriptor, _EVENT_FILE_MODE)
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _validate_event_file(metadata: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"event log must not be a symbolic link: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"event log must be a regular file: {path}")
    if metadata.st_nlink > 1:
        raise ValueError(f"event log must not be hard-linked: {path}")
    _require_current_owner(metadata, path)


def _require_current_owner(metadata: os.stat_result, path: Path) -> None:
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and metadata.st_uid != geteuid():
        raise PermissionError(f"event log storage must be owned by the current user: {path}")
