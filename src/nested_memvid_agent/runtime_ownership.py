from __future__ import annotations

import errno
import os
from pathlib import Path
from threading import Lock
from typing import IO

from .file_lock import lock_exclusive, unlock
from .private_artifacts import open_private_file_descriptor

RUNTIME_OWNERSHIP_ERROR = "state_runtime_already_owned"


class RuntimeOwnershipError(RuntimeError):
    """Raised when another primary runtime owns the same state database."""

    code = RUNTIME_OWNERSHIP_ERROR

    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        self.lock_path = runtime_ownership_lock_path(self.state_path)
        super().__init__(self.code)


class PrimaryRuntimeOwnership:
    """Process-scoped, advisory ownership of one Kestrel state database."""

    def __init__(self, state_path: Path) -> None:
        self.state_path = Path(state_path)
        self.lock_path = runtime_ownership_lock_path(self.state_path)
        self._guard = Lock()
        self._handle: IO[str] | None = None

    @property
    def acquired(self) -> bool:
        with self._guard:
            return self._handle is not None

    def acquire(self) -> None:
        """Acquire ownership immediately or fail with a stable error code."""

        with self._guard:
            if self._handle is not None:
                return
            descriptor = open_private_file_descriptor(self.lock_path)
            try:
                handle = os.fdopen(descriptor, "r+", encoding="utf-8")
            except BaseException:
                os.close(descriptor)
                raise
            try:
                lock_exclusive(handle, blocking=False)
                self._handle = handle
            except OSError as exc:
                self._handle = None
                handle.close()
                if _is_lock_contention(exc):
                    raise RuntimeOwnershipError(self.state_path) from exc
                raise
            except BaseException:
                self._handle = None
                handle.close()
                raise

    def release(self) -> None:
        """Release ownership; repeated calls are harmless."""

        with self._guard:
            handle = self._handle
            if handle is None:
                return
            try:
                self._handle = None
                unlock(handle)
            finally:
                self._handle = None
                handle.close()


def runtime_ownership_lock_path(state_path: Path) -> Path:
    path = Path(state_path)
    return path.with_name(f".{path.name}.kestrel-runtime-owner.lock")


def _is_lock_contention(error: OSError) -> bool:
    return isinstance(error, BlockingIOError) or error.errno in {
        errno.EACCES,
        errno.EAGAIN,
        errno.EDEADLK,
        errno.EWOULDBLOCK,
    }
