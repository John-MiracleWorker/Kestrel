"""Windows Job Object containment for supervised subprocess trees."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class WindowsProcessJob:
    """Kill-on-close Windows Job Object around one supervised process tree.

    The callables are injected so lifecycle behavior can be tested on non-Windows
    hosts. Processes must be created suspended, assigned, and only then resumed.
    """

    def __init__(
        self,
        *,
        assign_process: Callable[[int], bool],
        resume_process: Callable[[int], bool],
        terminate_job: Callable[[], bool],
        active_processes: Callable[[], int | None],
        close_job: Callable[[], bool],
    ) -> None:
        self._assign_process = assign_process
        self._resume_process = resume_process
        self._terminate_job = terminate_job
        self._active_processes = active_processes
        self._close_job = close_job
        self._lock = threading.RLock()
        self._closed = False

    def assign(self, process_id: int) -> bool:
        with self._lock:
            if self._closed:
                return False
            return bool(self._assign_process(process_id))

    def resume(self, process_id: int) -> bool:
        with self._lock:
            if self._closed:
                return False
            return bool(self._resume_process(process_id))

    def terminate_and_wait(self, *, timeout_seconds: float = 2.0) -> bool:
        """Terminate the job and prove its active-process count reached zero."""

        deadline = time.monotonic() + max(timeout_seconds, 0.001)
        with self._lock:
            if self._closed or not self._terminate_job():
                return False
            while True:
                active = self._active_processes()
                if active == 0:
                    return True
                if active is None or time.monotonic() >= deadline:
                    return False
                time.sleep(0.01)

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            closed = bool(self._close_job())
            if closed:
                self._closed = True
            return closed


def create_windows_process_job() -> WindowsProcessJob:
    """Create a kill-on-close Job Object using only the Python standard library."""

    import ctypes
    from ctypes import wintypes

    job_object_extended_limit_information = 9
    job_object_basic_accounting_information = 1
    job_object_limit_kill_on_job_close = 0x00002000
    process_terminate = 0x0001
    process_set_quota = 0x0100
    thread_suspend_resume = 0x0002
    th32cs_snapthread = 0x00000004

    class JobObjectBasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JobObjectExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JobObjectBasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    class JobObjectBasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    win_dll = getattr(ctypes, "WinDLL", None)
    win_error = getattr(ctypes, "WinError", None)
    get_last_error = getattr(ctypes, "get_last_error", None)
    if not callable(win_dll) or not callable(win_error) or not callable(get_last_error):
        raise OSError("Windows Job Object APIs are unavailable in this Python runtime")
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    job_handle = kernel32.CreateJobObjectW(None, None)
    if not job_handle:
        raise win_error(get_last_error())
    limits = JobObjectExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_job_close
    configured = kernel32.SetInformationJobObject(
        job_handle,
        job_object_extended_limit_information,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    if not configured:
        error = get_last_error()
        kernel32.CloseHandle(job_handle)
        raise win_error(error)

    def assign_process(process_id: int) -> bool:
        process_handle = kernel32.OpenProcess(
            process_terminate | process_set_quota,
            False,
            process_id,
        )
        if not process_handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job_handle, process_handle))
        finally:
            kernel32.CloseHandle(process_handle)

    def resume_process(process_id: int) -> bool:
        snapshot = kernel32.CreateToolhelp32Snapshot(th32cs_snapthread, 0)
        invalid_handle_value = ctypes.c_void_p(-1).value
        if not snapshot or int(snapshot) == invalid_handle_value:
            return False
        thread_ids: list[int] = []
        enumeration_error = 0
        try:
            entry = ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while has_entry:
                if int(entry.th32OwnerProcessID) == process_id:
                    thread_ids.append(int(entry.th32ThreadID))
                entry.dwSize = ctypes.sizeof(entry)
                has_entry = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
            enumeration_error = int(get_last_error())
        finally:
            kernel32.CloseHandle(snapshot)
        if enumeration_error != 18 or len(thread_ids) != 1:
            return False
        thread_handle = kernel32.OpenThread(
            thread_suspend_resume,
            False,
            thread_ids[0],
        )
        if not thread_handle:
            return False
        try:
            previous_suspend_count = int(kernel32.ResumeThread(thread_handle))
            return previous_suspend_count == 1
        finally:
            kernel32.CloseHandle(thread_handle)

    def terminate_job() -> bool:
        return bool(kernel32.TerminateJobObject(job_handle, 1))

    def active_processes() -> int | None:
        accounting = JobObjectBasicAccountingInformation()
        queried = kernel32.QueryInformationJobObject(
            job_handle,
            job_object_basic_accounting_information,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        )
        return int(accounting.ActiveProcesses) if queried else None

    def close_job() -> bool:
        return bool(kernel32.CloseHandle(job_handle))

    return WindowsProcessJob(
        assign_process=assign_process,
        resume_process=resume_process,
        terminate_job=terminate_job,
        active_processes=active_processes,
        close_job=close_job,
    )
