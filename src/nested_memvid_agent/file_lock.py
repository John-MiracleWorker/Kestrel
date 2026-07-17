from __future__ import annotations

import os
import sys
from typing import IO, Any

if sys.platform == "win32":
    import msvcrt as _msvcrt

    _fcntl: Any = None
else:
    import fcntl as _fcntl

    _msvcrt: Any = None


def lock_shared(handle: IO[str], *, blocking: bool = True) -> None:
    if sys.platform == "win32":
        _ensure_lock_byte(handle)
        mode = _msvcrt.LK_RLCK if blocking else _msvcrt.LK_NBRLCK
        _msvcrt.locking(handle.fileno(), mode, 1)
        return
    operation = _fcntl.LOCK_SH | (0 if blocking else _fcntl.LOCK_NB)
    _fcntl.flock(handle.fileno(), operation)


def lock_exclusive(handle: IO[str], *, blocking: bool = True) -> None:
    if sys.platform == "win32":
        _ensure_lock_byte(handle)
        mode = _msvcrt.LK_LOCK if blocking else _msvcrt.LK_NBLCK
        _msvcrt.locking(handle.fileno(), mode, 1)
        return
    operation = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
    _fcntl.flock(handle.fileno(), operation)


def unlock(handle: IO[str]) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        return
    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


def _ensure_lock_byte(handle: IO[str]) -> None:
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)
