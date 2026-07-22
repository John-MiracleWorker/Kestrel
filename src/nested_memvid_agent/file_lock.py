from __future__ import annotations

import errno
import sys
from typing import IO, Any

_WINDOWS_LOCK_CONTENTION_ERRORS = frozenset({32, 33})

if sys.platform == "win32":
    import ctypes as _ctypes
    import msvcrt as _msvcrt
    from ctypes import wintypes as _wintypes

    class _Overlapped(_ctypes.Structure):
        _fields_ = (
            ("Internal", _ctypes.c_size_t),
            ("InternalHigh", _ctypes.c_size_t),
            ("Offset", _wintypes.DWORD),
            ("OffsetHigh", _wintypes.DWORD),
            ("hEvent", _wintypes.HANDLE),
        )

    _kernel32: Any = _ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file_ex: Any = _kernel32.LockFileEx
    _lock_file_ex.argtypes = (
        _wintypes.HANDLE,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _ctypes.POINTER(_Overlapped),
    )
    _lock_file_ex.restype = _wintypes.BOOL
    _unlock_file_ex: Any = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = (
        _wintypes.HANDLE,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _wintypes.DWORD,
        _ctypes.POINTER(_Overlapped),
    )
    _unlock_file_ex.restype = _wintypes.BOOL

    _fcntl: Any = None
else:
    import fcntl as _fcntl

    _msvcrt: Any = None


def lock_shared(handle: IO[str], *, blocking: bool = True) -> None:
    if sys.platform == "win32":
        _windows_lock(handle, exclusive=False, blocking=blocking)
        return
    operation = _fcntl.LOCK_SH | (0 if blocking else _fcntl.LOCK_NB)
    _fcntl.flock(handle.fileno(), operation)


def lock_exclusive(handle: IO[str], *, blocking: bool = True) -> None:
    if sys.platform == "win32":
        _windows_lock(handle, exclusive=True, blocking=blocking)
        return
    operation = _fcntl.LOCK_EX | (0 if blocking else _fcntl.LOCK_NB)
    _fcntl.flock(handle.fileno(), operation)


def unlock(handle: IO[str]) -> None:
    if sys.platform == "win32":
        handle.seek(0)
        _windows_unlock(handle)
        return
    _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)


if sys.platform == "win32":

    def _windows_lock(handle: IO[str], *, exclusive: bool, blocking: bool) -> None:
        """Acquire a real Windows shared or exclusive byte-range lock.

        The CRT ``msvcrt.locking`` read-lock constants are implemented as
        exclusive locks on modern Windows. Kestrel opens one root lock for
        every Memvid layer, so that API self-deadlocks a single runtime while
        opening its second layer. ``LockFileEx`` preserves the shared-reader
        contract used by ``flock`` on POSIX.
        """

        flags = 0x2 if exclusive else 0
        if not blocking:
            flags |= 0x1
        overlapped = _Overlapped()
        os_handle = _wintypes.HANDLE(_msvcrt.get_osfhandle(handle.fileno()))
        if _lock_file_ex(os_handle, flags, 0, 1, 0, _ctypes.byref(overlapped)):
            return
        error = _ctypes.get_last_error()
        raise _windows_lock_error(
            error,
            blocking=blocking,
            message=_ctypes.FormatError(error),
        )


    def _windows_unlock(handle: IO[str]) -> None:
        overlapped = _Overlapped()
        os_handle = _wintypes.HANDLE(_msvcrt.get_osfhandle(handle.fileno()))
        if _unlock_file_ex(os_handle, 0, 1, 0, _ctypes.byref(overlapped)):
            return
        error = _ctypes.get_last_error()
        raise OSError(error, _ctypes.FormatError(error))


def _windows_lock_error(error: int, *, blocking: bool, message: str) -> OSError:
    """Normalize an immediate Windows lock miss to Python's lock contract."""

    if not blocking and error in _WINDOWS_LOCK_CONTENTION_ERRORS:
        return BlockingIOError(errno.EAGAIN, message)
    return OSError(error, message)
