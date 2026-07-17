from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

_PROCESS_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87


def process_is_alive(pid: int) -> bool | None:
    """Return whether *pid* is alive, or ``None`` when it cannot be determined."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _windows_process_is_alive(pid: int) -> bool | None:
    """Probe a Windows process without sending it a console or termination signal."""
    win_dll = getattr(ctypes, "WinDLL", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if win_dll is None or get_last_error is None:
        return None

    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_PROCESS_SYNCHRONIZE, False, pid)
    if not handle:
        error_code = int(get_last_error())
        if error_code == _ERROR_INVALID_PARAMETER:
            return False
        if error_code == _ERROR_ACCESS_DENIED:
            return True
        return None
    try:
        wait_result = int(kernel32.WaitForSingleObject(handle, 0))
    finally:
        kernel32.CloseHandle(handle)
    if wait_result == _WAIT_TIMEOUT:
        return True
    if wait_result == _WAIT_OBJECT_0:
        return False
    return None
