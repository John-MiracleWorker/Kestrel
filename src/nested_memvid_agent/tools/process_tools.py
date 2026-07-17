from __future__ import annotations

import os
import signal as signal_module
import subprocess  # nosec B404
import sys
import threading
from pathlib import Path
from typing import Any

from ..security_boundary import sanitized_subprocess_environment
from .base import ToolContext


def _normalize_python_command(command: list[str]) -> list[str]:
    if command and Path(command[0]).name in {"python", "python3"}:
        return [sys.executable, *command[1:]]
    return command


class _SubprocessToolTimeout(RuntimeError):
    pass


_RUNNING_PROCESSES: dict[str, tuple[subprocess.Popen[str], str | None, int | None]] = {}
_RUNNING_PROCESS_LOCK = threading.RLock()


def _run_subprocess(
    command: list[str],
    *,
    context: ToolContext,
    arguments: dict[str, Any],
    default_timeout: int,
    sanitize_environment: bool = False,
) -> subprocess.CompletedProcess[str]:
    timeout = _effective_timeout(arguments, context, default_timeout)
    call_id = str(arguments.get("_tool_call_id") or "")
    process = _start_subprocess(
        command,
        context=context,
        environment=(sanitized_subprocess_environment() if sanitize_environment else None),
    )
    process_group_id = process.pid if sys.platform != "win32" else None
    if call_id:
        with _RUNNING_PROCESS_LOCK:
            _RUNNING_PROCESSES[call_id] = (process, context.run_id, process_group_id)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            command, process.returncode, stdout=stdout, stderr=stderr
        )
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process, process_group_id=process_group_id)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process_group(process, process_group_id=process_group_id)
            stdout, stderr = process.communicate()
        content = (
            f"Command timed out after {timeout:g} seconds.\n"
            f"STDOUT:\n{_process_output_text(stdout, exc.stdout)}\n"
            f"STDERR:\n{_process_output_text(stderr, exc.stderr)}"
        )
        raise _SubprocessToolTimeout(content) from exc
    finally:
        if call_id:
            with _RUNNING_PROCESS_LOCK:
                tracked = _RUNNING_PROCESSES.get(call_id)
                if tracked is not None and tracked[0] is process:
                    del _RUNNING_PROCESSES[call_id]


def _start_subprocess(
    command: list[str],
    *,
    context: ToolContext,
    environment: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    common: dict[str, Any] = {
        "cwd": context.workspace,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if environment is not None:
        common["env"] = environment
    if sys.platform == "win32":
        return subprocess.Popen(  # noqa: S603  # nosec B603
            command,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            **common,
        )
    return subprocess.Popen(  # noqa: S603  # nosec B603
        command,
        start_new_session=True,
        **common,
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
        _terminate_process_group(tracked[0], process_group_id=tracked[2])


def cancel_subprocesses_for_run(run_id: str) -> int:
    with _RUNNING_PROCESS_LOCK:
        processes = [
            (process, process_group_id)
            for process, owner_run_id, process_group_id in _RUNNING_PROCESSES.values()
            if owner_run_id == run_id
        ]
    for process, process_group_id in processes:
        _terminate_process_group(process, process_group_id=process_group_id)
    return len(processes)


def _terminate_process_group(
    process: subprocess.Popen[str],
    *,
    process_group_id: int | None = None,
) -> None:
    if sys.platform == "win32":
        _terminate_windows_process_tree(process)
        return
    group_id = process_group_id or process.pid
    try:
        os.killpg(group_id, signal_module.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        if process.poll() is None:
            process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    _kill_process_group(process, process_group_id=group_id)


def _kill_process_group(
    process: subprocess.Popen[str],
    *,
    process_group_id: int | None = None,
) -> None:
    if sys.platform == "win32":
        _terminate_windows_process_tree(process)
        return
    try:
        os.killpg(process_group_id or process.pid, signal_module.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
        if process.poll() is None:
            process.kill()


def _terminate_windows_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        subprocess.run(  # noqa: S603  # nosec B603
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        process.kill()


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
