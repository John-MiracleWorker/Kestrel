"""Run a subprocess tree behind an explicit monotonic deadline."""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - callers supply fixed local commands
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep


@dataclass(frozen=True)
class BoundedProcessResult:
    """Machine-readable result for a deadline-bounded subprocess tree."""

    returncode: int | None
    stdout: str
    stderr: str
    elapsed_seconds: float
    timed_out: bool
    cleanup_attempted: bool
    cleanup_succeeded: bool
    termination_method: str | None
    deadline_clock: str = "monotonic"


def _drain(stream: object, chunks: list[str]) -> None:
    if not hasattr(stream, "read"):
        return
    try:
        value = stream.read()
    except (OSError, ValueError):
        return
    if isinstance(value, str):
        chunks.append(value)


def _wait_until(process: subprocess.Popen[str], deadline: float) -> bool:
    while process.poll() is None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(0.05, remaining))
    return True


def _posix_process_group_quiesced(process_group: int) -> bool:
    """Treat a group containing only zombies as unable to mutate further."""

    try:
        completed = subprocess.run(  # noqa: S603
            ["ps", "-axo", "pgid=,stat="],
            capture_output=True,
            check=False,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    for line in completed.stdout.splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            continue
        try:
            observed_group = int(fields[0])
        except ValueError:
            continue
        if observed_group == process_group and not fields[1].startswith("Z"):
            return False
    return True


def _cleanup_posix_tree(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float,
) -> tuple[bool, str]:
    process_group = process.pid
    try:
        os.killpg(process_group, signal.SIGTERM)
    except ProcessLookupError:
        process.wait()
        return True, "posix_process_group_already_exited"
    except OSError:
        return False, "posix_process_group_sigterm_failed"

    if not _wait_until(process, monotonic() + grace_seconds):
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError:
            return False, "posix_process_group_sigkill_failed"
        if not _wait_until(process, monotonic() + grace_seconds):
            return False, "posix_process_group_did_not_exit"

    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True, "posix_process_group_terminated"
    except PermissionError:
        return False, "posix_process_group_unverifiable"
    except OSError:
        return True, "posix_process_group_terminated"

    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True, "posix_process_group_terminated"
    except OSError:
        return False, "posix_process_group_descendants_remain"
    sleep(min(grace_seconds, 0.1))
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return True, "posix_process_group_terminated"
    except OSError:
        return True, "posix_process_group_terminated"
    if _posix_process_group_quiesced(process_group):
        return True, "posix_process_group_zombies_only"
    return False, "posix_process_group_descendants_remain"


def _cleanup_windows_tree(
    process: subprocess.Popen[str],
    *,
    grace_seconds: float,
) -> tuple[bool, str]:
    try:
        taskkill = subprocess.run(  # noqa: S603
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            text=True,
            timeout=max(grace_seconds, 1.0),
        )
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            process.kill()
        _wait_until(process, monotonic() + grace_seconds)
        return False, "windows_taskkill_tree_unavailable"
    if taskkill.returncode != 0 and process.poll() is None:
        process.kill()
    exited = _wait_until(process, monotonic() + grace_seconds)
    return (
        taskkill.returncode == 0 and exited,
        (
            "windows_taskkill_tree"
            if taskkill.returncode == 0 and exited
            else "windows_taskkill_tree_unverified"
        ),
    )


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    termination_grace_seconds: float = 1.0,
) -> BoundedProcessResult:
    """Run ``command`` and terminate its complete process tree at the deadline."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("bounded process command must contain non-empty strings")
    if timeout_seconds <= 0:
        raise ValueError("bounded process timeout must be greater than zero")
    if termination_grace_seconds <= 0:
        raise ValueError("termination grace must be greater than zero")

    creationflags = 0
    start_new_session = False
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        start_new_session = True

    started = monotonic()
    process = subprocess.Popen(  # noqa: S603
        list(command),
        cwd=cwd,
        env=dict(environment),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=creationflags,
        start_new_session=start_new_session,
    )
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    stdout_thread = threading.Thread(
        target=_drain,
        args=(process.stdout, stdout_chunks),
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain,
        args=(process.stderr, stderr_chunks),
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    completed = _wait_until(process, started + timeout_seconds)
    cleanup_attempted = not completed
    cleanup_succeeded = True
    termination_method: str | None = None
    if not completed:
        if os.name == "nt":
            cleanup_succeeded, termination_method = _cleanup_windows_tree(
                process,
                grace_seconds=termination_grace_seconds,
            )
        else:
            cleanup_succeeded, termination_method = _cleanup_posix_tree(
                process,
                grace_seconds=termination_grace_seconds,
            )

    if process.poll() is None:
        process.kill()
        process.wait()
        cleanup_succeeded = False
        termination_method = f"{termination_method or 'fallback'}_parent_only"
    else:
        process.wait()
    if not cleanup_attempted and os.name != "nt":
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            cleanup_attempted = True
            cleanup_succeeded = False
            termination_method = "posix_process_group_unverifiable"
        except OSError:
            pass
        else:
            cleanup_attempted = True
            cleanup_succeeded, termination_method = _cleanup_posix_tree(
                process,
                grace_seconds=termination_grace_seconds,
            )
    stdout_thread.join(timeout=termination_grace_seconds)
    stderr_thread.join(timeout=termination_grace_seconds)
    streams_drained = not stdout_thread.is_alive() and not stderr_thread.is_alive()
    if not streams_drained:
        if not cleanup_attempted and os.name == "nt":
            cleanup_attempted = True
            cleanup_succeeded, termination_method = _cleanup_windows_tree(
                process,
                grace_seconds=termination_grace_seconds,
            )
            stdout_thread.join(timeout=termination_grace_seconds)
            stderr_thread.join(timeout=termination_grace_seconds)
            streams_drained = not stdout_thread.is_alive() and not stderr_thread.is_alive()
        if not streams_drained:
            cleanup_succeeded = False

    return BoundedProcessResult(
        returncode=process.returncode,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        elapsed_seconds=round(monotonic() - started, 6),
        timed_out=not completed,
        cleanup_attempted=cleanup_attempted,
        cleanup_succeeded=cleanup_succeeded,
        termination_method=termination_method,
    )
