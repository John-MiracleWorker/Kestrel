from __future__ import annotations

import json
import os
import signal as signal_module
import stat as stat_module
import subprocess  # nosec B404
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_models import ToolCall, ToolExecution
from ..security_boundary import redact_text, sanitized_subprocess_environment
from ..validation_runner import (
    IsolatedValidationResult,
    run_isolated_validation,
)
from .base import ToolContext


def _normalize_python_command(command: list[str]) -> list[str]:
    if command:
        executable = Path(command[0]).name.casefold().removesuffix(".exe")
        suffix = executable.removeprefix("python")
        is_python = executable in {"python", "python3"} or (
            bool(suffix)
            and suffix[0].isdigit()
            and all(part.isdigit() for part in suffix.split("."))
        )
        if is_python:
            # Validation commands execute inside a Linux OCI snapshot. Never
            # persist or pass a host interpreter path into that boundary.
            return ["python", *command[1:]]
    return command


class _SubprocessToolTimeout(RuntimeError):
    pass


class _SubprocessToolOutcomeIndeterminate(RuntimeError):
    """Raised when Kestrel cannot prove that a launched process tree is gone."""


class WorkspaceSecretIsolationError(RuntimeError):
    """Raised when arbitrary code could read a configured raw secret vault."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class _WindowsProcessJob:
    """Kill-on-close Windows Job Object around one supervised process tree.

    ``subprocess`` can create a new process group on Windows, but a process group
    is not a containment primitive: descendants can outlive a successful leader,
    and ``taskkill /T`` cannot reliably rediscover the tree once that leader has
    exited.  A Job Object retains the descendant set independently of the leader.
    The callables are injected so the lifecycle can be tested on non-Windows CI.
    """

    def __init__(
        self,
        *,
        assign_process: Any,
        resume_process: Any,
        terminate_job: Any,
        active_processes: Any,
        close_job: Any,
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
        """Terminate the entire job and prove that its active count reached zero."""

        deadline = time.monotonic() + max(timeout_seconds, 0.001)
        with self._lock:
            if self._closed:
                return False
            self._terminate_job()
            while True:
                active = self._active_processes()
                if active == 0:
                    return True
                if active is None or time.monotonic() >= deadline:
                    return False
                # A very short bounded poll avoids treating asynchronous process
                # teardown as verified merely because TerminateJobObject returned.
                time.sleep(0.01)

    def close(self) -> bool:
        with self._lock:
            if self._closed:
                return True
            closed = bool(self._close_job())
            if closed:
                self._closed = True
            return closed


@dataclass
class _TrackedProcess:
    process: subprocess.Popen[Any]
    run_id: str | None
    process_group_id: int | None
    cancellation_requested: threading.Event
    windows_job: _WindowsProcessJob | None = None


_RUNNING_PROCESSES: dict[str, _TrackedProcess] = {}
_RUNNING_PROCESS_LOCK = threading.RLock()


def _run_subprocess(
    command: list[str],
    *,
    context: ToolContext,
    arguments: dict[str, Any],
    default_timeout: int,
    sanitize_environment: bool = False,
    environment: dict[str, str] | None = None,
    environment_overrides: dict[str, str] | None = None,
    input_text: str | None = None,
    input_bytes: bytes | None = None,
    requires_workspace_secret_isolation: bool = False,
    require_container_isolation: bool = False,
    expected_repair_snapshot: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str] | IsolatedValidationResult:
    if input_text is not None and input_bytes is not None:
        raise ValueError("Subprocess input must be either text or bytes, not both.")
    isolation_error: WorkspaceSecretIsolationError | None = None
    if requires_workspace_secret_isolation and not require_container_isolation:
        try:
            _assert_arbitrary_subprocess_workspace_safe(context)
        except WorkspaceSecretIsolationError as exc:
            isolation_error = exc
    if require_container_isolation or isolation_error is not None:
        image = getattr(context.config, "validation_container_image", None)
        if not image and isolation_error is not None and not require_container_isolation:
            raise isolation_error
        return run_isolated_validation(
            workspace=context.workspace,
            image=image,
            command=command,
            timeout_seconds=_effective_timeout(arguments, context, default_timeout),
            expected_repair_snapshot=expected_repair_snapshot,
        )
    timeout = _effective_timeout(arguments, context, default_timeout)
    execution_id = str(arguments.get("_tool_execution_id") or arguments.get("_tool_call_id") or "")
    process_environment = (
        dict(environment)
        if environment is not None
        else sanitized_subprocess_environment()
        if sanitize_environment
        else None
    )
    if environment_overrides:
        process_environment = dict(
            os.environ if process_environment is None else process_environment
        )
        process_environment.update(environment_overrides)
    windows_job = _create_windows_process_job() if sys.platform == "win32" else None
    process: subprocess.Popen[Any] | None = None
    process_group_id: int | None = None
    cancellation_requested = threading.Event()
    try:
        if input_bytes is not None:
            process = _start_subprocess(
                command,
                context=context,
                environment=process_environment,
                pipe_stdin=True,
                binary_stdio=True,
            )
        else:
            # Keep the original call shape for text/no-input execution. Some
            # embedders replace this private launch seam with a fixed-signature
            # supervisor that predates binary stdin support.
            process = _start_subprocess(
                command,
                context=context,
                environment=process_environment,
                pipe_stdin=input_text is not None,
            )
        process_group_id = process.pid if sys.platform != "win32" else None
        if windows_job is not None and not windows_job.assign(process.pid):
            # Windows launches are created suspended, so user code cannot spawn
            # an escaping child before successful Job Object assignment.
            _terminate_windows_process_tree(process)
            raise _SubprocessToolOutcomeIndeterminate(
                "Windows process containment could not attach the launched process to "
                "its kill-on-close Job Object; the final outcome is unknown."
            )
        if execution_id:
            with _RUNNING_PROCESS_LOCK:
                _RUNNING_PROCESSES[execution_id] = _TrackedProcess(
                    process=process,
                    run_id=context.run_id,
                    process_group_id=process_group_id,
                    cancellation_requested=cancellation_requested,
                    windows_job=windows_job,
                )
        if windows_job is not None and not windows_job.resume(process.pid):
            raise _SubprocessToolOutcomeIndeterminate(
                "Windows process containment could not safely resume the suspended "
                "Job Object member; the final outcome is unknown."
            )
        stdin_payload = input_bytes if input_bytes is not None else input_text
        stdout, stderr = process.communicate(input=stdin_payload, timeout=timeout)
        if cancellation_requested.is_set():
            if not _terminate_process_group(
                process,
                process_group_id=process_group_id,
                windows_job=windows_job,
            ):
                raise _SubprocessToolOutcomeIndeterminate(
                    "Windows process cancellation could not prove that the supervised "
                    "process tree terminated; the final outcome is unknown."
                )
            raise _SubprocessToolTimeout(
                f"Command was cancelled after exceeding its {timeout:g} second deadline."
            )
        # A successful leader exit is not proof that the bounded tool is done:
        # hooks or child processes may have detached their stdio while staying
        # in the supervised process group. Quiesce that group before publishing
        # the result so no ordinary background descendant can mutate later.
        if process_group_id is not None or windows_job is not None:
            if not _terminate_process_group(
                process,
                process_group_id=process_group_id,
                windows_job=windows_job,
            ):
                raise _SubprocessToolOutcomeIndeterminate(
                    "Windows process completion could not prove that every supervised "
                    "descendant terminated; the final outcome is unknown."
                )
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=_redact_subprocess_text(_process_output_text(stdout, None)),
            stderr=_redact_subprocess_text(_process_output_text(stderr, None)),
        )
    except subprocess.TimeoutExpired as exc:
        assert process is not None
        if not _terminate_process_group(
            process,
            process_group_id=process_group_id,
            windows_job=windows_job,
        ):
            raise _SubprocessToolOutcomeIndeterminate(
                "Windows process timeout could not prove that the supervised process "
                "tree terminated; the final outcome is unknown."
            ) from exc
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            if not _kill_process_group(
                process,
                process_group_id=process_group_id,
                windows_job=windows_job,
            ):
                raise _SubprocessToolOutcomeIndeterminate(
                    "Windows process timeout cleanup remained unverifiable; the final "
                    "outcome is unknown."
                ) from exc
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired as cleanup_exc:
                raise _SubprocessToolOutcomeIndeterminate(
                    "Process timeout cleanup did not settle before the hard cleanup "
                    "deadline; the final outcome is unknown."
                ) from cleanup_exc
        content = (
            f"Command timed out after {timeout:g} seconds.\n"
            f"STDOUT:\n{_process_output_text(stdout, exc.stdout)}\n"
            f"STDERR:\n{_process_output_text(stderr, exc.stderr)}"
        )
        raise _SubprocessToolTimeout(_redact_subprocess_text(content)) from exc
    except _SubprocessToolOutcomeIndeterminate:
        if process is not None:
            _terminate_process_group(
                process,
                process_group_id=process_group_id,
                windows_job=windows_job,
            )
        raise
    except BaseException as exc:
        # No exception path may drop the tracking entry while a process tree is
        # still live (including communicate/decoding/interrupt failures).
        if process is None:
            raise
        quiesced = _terminate_process_group(
            process,
            process_group_id=process_group_id,
            windows_job=windows_job,
        )
        try:
            process.communicate(timeout=2)
        except BaseException:
            quiesced = (
                _kill_process_group(
                    process,
                    process_group_id=process_group_id,
                    windows_job=windows_job,
                )
                and quiesced
            )
            try:
                process.wait(timeout=2)
            except BaseException:
                quiesced = False
        if sys.platform == "win32" and not quiesced:
            raise _SubprocessToolOutcomeIndeterminate(
                "Windows process cleanup could not prove that the supervised process "
                "tree terminated; the final outcome is unknown."
            ) from exc
        raise
    finally:
        if execution_id:
            with _RUNNING_PROCESS_LOCK:
                tracked = _RUNNING_PROCESSES.get(execution_id)
                if tracked is not None and tracked.process is process:
                    del _RUNNING_PROCESSES[execution_id]
        if windows_job is not None:
            if process is not None and not windows_job.close():
                raise _SubprocessToolOutcomeIndeterminate(
                    "Windows process cleanup could not close the kill-on-close Job Object; "
                    "descendant ownership remains unverified."
                )


def _start_subprocess(
    command: list[str],
    *,
    context: ToolContext,
    environment: dict[str, str] | None = None,
    pipe_stdin: bool = False,
    binary_stdio: bool = False,
) -> subprocess.Popen[Any]:
    common: dict[str, Any] = {
        "cwd": context.workspace,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": not binary_stdio,
    }
    if environment is not None:
        common["env"] = environment
    if pipe_stdin:
        common["stdin"] = subprocess.PIPE
    if sys.platform == "win32":
        # CREATE_SUSPENDED closes the child-escape window between CreateProcess
        # and AssignProcessToJobObject. The initial thread is resumed only after
        # the kill-on-close Job Object owns the process.
        create_suspended = 0x00000004
        create_new_process_group = getattr(
            subprocess,
            "CREATE_NEW_PROCESS_GROUP",
            0x00000200,
        )
        return subprocess.Popen(  # noqa: S603  # nosec B603
            command,
            creationflags=create_new_process_group | create_suspended,
            **common,
        )
    return subprocess.Popen(  # noqa: S603  # nosec B603
        command,
        start_new_session=True,
        **common,
    )


def _assert_arbitrary_subprocess_workspace_safe(context: ToolContext) -> None:
    """Fail closed when same-UID arbitrary code could cross a trust boundary.

    A keyring changes where a secret is stored; it does not isolate another
    process running as the same account. Likewise, owner-only repair signing
    material is not protected from same-UID code. Agent validation tools use
    OCI unconditionally; this guard remains for explicitly host-backed surfaces
    such as the separate Codex CLI response provider.
    """

    assert_arbitrary_subprocess_safe(
        workspace=context.workspace,
        secret_store_path=Path(context.config.secret_store_path),
        secret_backend=str(getattr(context.config, "secret_backend", "json") or "json"),
    )


def assert_arbitrary_subprocess_safe(
    *,
    workspace: Path,
    secret_store_path: Path,
    secret_backend: str,
) -> None:
    """Enforce the same-account boundary for any general local subprocess."""

    _assert_raw_secret_store_absent(
        workspace=workspace,
        secret_store_path=secret_store_path,
        secret_backend=secret_backend,
    )
    backend = str(secret_backend or "json")
    normalized_backend = backend.strip().lower()
    if normalized_backend == "keyring" and _keyring_metadata_has_material(
        workspace=workspace,
        secret_store_path=secret_store_path,
    ):
        raise WorkspaceSecretIsolationError(
            "keyring_process_isolation_required",
            "Arbitrary host-process execution is blocked while Kestrel has keyring "
            "secret records. OS keyring storage is not process isolation; configure "
            "a digest-pinned validation container.",
        )
    if _repair_trust_material_exists_or_indeterminate(workspace):
        raise WorkspaceSecretIsolationError(
            "repair_trust_process_isolation_required",
            "Arbitrary host-process execution is blocked after repair trust material "
            "exists. Owner-only files do not isolate same-account code; configure a "
            "digest-pinned validation container.",
        )


def _assert_repair_snapshot_workspace_safe(context: ToolContext) -> None:
    """Keep raw JSON vault bytes outside Git-backed repair inspection."""

    _assert_raw_secret_store_absent(
        workspace=context.workspace,
        secret_store_path=Path(context.config.secret_store_path),
        secret_backend=str(getattr(context.config, "secret_backend", "json") or "json"),
    )


def _assert_raw_secret_store_absent(
    *,
    workspace: Path,
    secret_store_path: Path,
    secret_backend: str,
) -> None:
    """Keep raw JSON vault bytes outside Git-backed or arbitrary processes."""

    backend = str(secret_backend or "json")
    if backend.strip().lower() not in {"", "file", "json", "local"}:
        return
    configured = Path(secret_store_path).expanduser()
    lexical_path = configured if configured.is_absolute() else workspace.resolve() / configured
    try:
        secret_store = lexical_path.resolve()
    except OSError:
        # An indeterminate configured JSON path is not a safe basis for
        # executing same-UID arbitrary code.
        secret_store = Path(os.path.abspath(lexical_path))
    exists = any(
        _raw_json_vault_artifact_exists_or_indeterminate(candidate)
        for candidate in {lexical_path, secret_store}
    )
    if exists:
        raise WorkspaceSecretIsolationError(
            "workspace_secret_isolation_required",
            "Arbitrary-code execution is blocked while the configured JSON secret store "
            "exists. Moving it outside the workspace does not contain same-account code; "
            "configure a digest-pinned validation container before enabling this tool.",
        )


def _keyring_metadata_has_material(
    *,
    workspace: Path,
    secret_store_path: Path,
) -> bool:
    """Inspect metadata only; never enumerate or resolve OS-keyring values."""

    configured = Path(secret_store_path).expanduser()
    path = configured if configured.is_absolute() else workspace.resolve() / configured
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat_module.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return True
    descriptor = -1
    try:
        if metadata.st_size > 4 * 1024 * 1024:
            return True
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat_module.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > 4 * 1024 * 1024
        ):
            return True
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            payload = json.loads(handle.read(4 * 1024 * 1024 + 1))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        return True
    records = payload.get("secrets", {})
    pending = payload.get("keyring_pending_cleanup", {})
    return bool(records) or bool(pending)


def _repair_trust_material_exists_or_indeterminate(workspace: Path) -> bool:
    root = workspace.expanduser().resolve() / ".nest"
    candidates = (
        root / "repair_receipt_signing.key",
        root / ".repair_receipt_signing.key.tmp",
        root / "repair_receipt_signing.v2.key",
        root / ".repair_receipt_signing.v2.key.tmp",
    )
    if any(_path_exists_or_indeterminate(path) for path in candidates):
        return True
    for directory_name in ("repair_validations", "repair_reviews"):
        directory = root / directory_name
        try:
            with os.scandir(directory) as entries:
                if any(True for _entry in entries):
                    return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _path_exists_or_indeterminate(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _raw_json_vault_artifact_exists_or_indeterminate(vault_path: Path) -> bool:
    """Treat an interrupted atomic-write artifact as raw vault material too."""

    if _path_exists_or_indeterminate(vault_path):
        return True
    temporary_prefix = f".{vault_path.name}."
    try:
        with os.scandir(vault_path.parent) as entries:
            return any(
                entry.name.startswith(temporary_prefix) and entry.name.endswith(".tmp")
                for entry in entries
            )
    except FileNotFoundError:
        return False
    except OSError:
        # Same-account code must not launch when the raw-vault directory cannot
        # be inspected well enough to establish that it is clean.
        return True


def _redact_subprocess_text(text: str) -> str:
    """Apply central redaction without corrupting non-secret JSON primitives."""

    return redact_text(text)


def _subprocess_outcome_unresolved(
    call: ToolCall,
    error: _SubprocessToolOutcomeIndeterminate,
) -> ToolExecution:
    """Return the common non-retryable receipt for unverifiable process cleanup."""

    return ToolExecution(
        call=call,
        success=False,
        content=redact_text(str(error)),
        data={
            "outcome_indeterminate": True,
            "retryable": False,
            "reconciliation_required": True,
            "process_tree_quiescence_verified": False,
        },
        error="tool_outcome_unresolved",
    )


def _effective_timeout(
    arguments: dict[str, Any], context: ToolContext, default_timeout: int
) -> float:
    max_timeout = max(
        float(getattr(context.config, "tool_timeout_seconds", default_timeout)), 0.001
    )
    try:
        requested = float(arguments.get("timeout", default_timeout))
    except (TypeError, ValueError):
        requested = float(default_timeout)
    requested = min(max(requested, 0.001), max_timeout)
    return requested


def _cancel_running_subprocess(call_id: str) -> None:
    with _RUNNING_PROCESS_LOCK:
        tracked = _RUNNING_PROCESSES.get(call_id)
    if tracked is not None:
        tracked.cancellation_requested.set()
        _terminate_process_group(
            tracked.process,
            process_group_id=tracked.process_group_id,
            windows_job=tracked.windows_job,
        )


def cancel_subprocesses_for_run(run_id: str) -> int:
    with _RUNNING_PROCESS_LOCK:
        processes = [tracked for tracked in _RUNNING_PROCESSES.values() if tracked.run_id == run_id]
    for tracked in processes:
        tracked.cancellation_requested.set()
        _terminate_process_group(
            tracked.process,
            process_group_id=tracked.process_group_id,
            windows_job=tracked.windows_job,
        )
    return len(processes)


def _terminate_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int | None = None,
    windows_job: _WindowsProcessJob | None = None,
) -> bool:
    if sys.platform == "win32":
        if windows_job is not None:
            return windows_job.terminate_and_wait()
        return _terminate_windows_process_tree(process)
    group_id = process_group_id or process.pid
    try:
        os.killpg(group_id, signal_module.SIGTERM)
    except ProcessLookupError:
        return True
    except Exception:
        if process.poll() is None:
            process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    return _kill_process_group(process, process_group_id=group_id)


def _kill_process_group(
    process: subprocess.Popen[Any],
    *,
    process_group_id: int | None = None,
    windows_job: _WindowsProcessJob | None = None,
) -> bool:
    if sys.platform == "win32":
        if windows_job is not None:
            return windows_job.terminate_and_wait()
        return _terminate_windows_process_tree(process)
    try:
        os.killpg(process_group_id or process.pid, signal_module.SIGKILL)
    except ProcessLookupError:
        return True
    except Exception:
        if process.poll() is None:
            process.kill()
    return True


def _create_windows_process_job() -> _WindowsProcessJob:
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
        # CREATE_SUSPENDED yields one initial thread. Any other shape is not a
        # safe basis for claiming that no user code ran before containment.
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

    return _WindowsProcessJob(
        assign_process=assign_process,
        resume_process=resume_process,
        terminate_job=terminate_job,
        active_processes=active_processes,
        close_job=close_job,
    )


def _terminate_windows_process_tree(process: subprocess.Popen[Any]) -> bool:
    """Best-effort fallback when no Job Object owns the tree.

    A successful taskkill call is considered verifiable only while the leader is
    still live. Once the leader exits, PID-based tree discovery cannot establish
    that detached descendants are gone, even if taskkill happens to return zero.
    Normal runtime execution always uses a Job Object; this path is cleanup for a
    failed assignment and deliberately returns false when containment is unknown.
    """

    leader_was_running = process.poll() is None
    taskkill_executable = _windows_taskkill_executable()
    if taskkill_executable is None:
        try:
            process.kill()
        except OSError:
            pass
        return False
    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603
            [taskkill_executable, "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        try:
            process.kill()
        except OSError:
            pass
        return False
    if completed.returncode != 0:
        try:
            process.kill()
        except OSError:
            pass
        return False
    return leader_was_running


def _windows_taskkill_executable() -> str | None:
    windows_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not windows_root:
        return None
    try:
        executable = (Path(windows_root).expanduser() / "System32" / "taskkill.exe").resolve(
            strict=True
        )
    except OSError:
        return None
    if not executable.is_file() or not executable.is_absolute():
        return None
    return str(executable)


def _process_output_text(primary: str | bytes | None, fallback: str | bytes | None) -> str:
    if isinstance(primary, bytes):
        return primary.decode("utf-8", errors="replace")
    if primary:
        return primary
    if isinstance(fallback, bytes):
        return fallback.decode("utf-8", errors="replace")
    return fallback or ""


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... truncated ..."
