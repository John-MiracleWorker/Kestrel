from __future__ import annotations

import os
import sys
from typing import IO

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def lock_shared(handle: IO[str], *, blocking: bool = True) -> None:
    if sys.platform == "win32":
        _ensure_lock_byte(handle)
        mode = msvcrt.LK_RLCK if blocking else msvcrt.LK_NBRLCK
        msvcrt.locking(handle.fileno(), mode, 1)
        return
    operation = fcntl.LOCK_SH | (0 if blocking else fcntl.LOCK_NB)
    fcntl.flock(handle.fileno(), operation)


def lock_exclusive(handle: IO[str], *, blocking: bool = True) -> None:
    if sys.platform == "win32":
        _ensure_lock_byte(handle)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(handle.fileno(), mode, 1)
        return
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    fcntl.flock(handle.fileno(), operation)


def unlock(handle: IO[str]) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ensure_lock_byte(handle: IO[str]) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
