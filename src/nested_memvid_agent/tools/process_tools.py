from __future__ import annotations

import os
import signal as signal_module
import subprocess  # nosec B404
import sys
import threading
from pathlib import Path
from typing import Any

from .base import ToolContext


def _normalize_python_command(command: list[str]) -> list[str]:
    if command and Path(command[0]).name in {"python", "python3"}:
        return [sys.executable, *command[1:]]
    return command


class _SubprocessToolTimeout(RuntimeError):
    pass


_RUNNING_PROCESSES: dict[str, subprocess.Popen[str]] = {}
_RUNNING_PROCESS_LOCK = threading.RLock()


def _run_subprocess(
    command: list[str],
    *,
    context: ToolContext,
    arguments: dict[str, Any],
    default_timeout: int,
) -> subprocess.CompletedProcess[str]:
    timeout = _effective_timeout(arguments, context, default_timeout)
    call_id = str(arguments.get("_tool_call_id") or "")
    process = subprocess.Popen(  # noqa: S603 - caller already validated executable and argv shape  # nosec
        command,
        cwd=context.workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if call_id:
        with _RUNNING_PROCESS_LOCK:
            _RUNNING_PROCESSES[call_id] = process
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            command, process.returncode, stdout=stdout, stderr=stderr
        )
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
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
                if _RUNNING_PROCESSES.get(call_id) is process:
                    del _RUNNING_PROCESSES[call_id]


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
        process = _RUNNING_PROCESSES.get(call_id)
    if process is not None:
        _terminate_process_group(process)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal_module.SIGTERM)
    except ProcessLookupError:
        return
    except Exception:
        process.terminate()


def _kill_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal_module.SIGKILL)
    except ProcessLookupError:
        return
    except Exception:
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
