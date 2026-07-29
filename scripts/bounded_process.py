# ruff: noqa: E402 - this standalone script adds the repository source root explicitly
"""Run a subprocess tree behind an explicit monotonic deadline."""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - callers supply fixed local commands
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from time import monotonic, sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nested_memvid_agent.windows_process_job import (
    WindowsProcessJob,
    create_windows_process_job,
)

DEFAULT_CAPTURE_LIMIT_BYTES = 256 * 1024
_STREAM_READ_SIZE = 64 * 1024
_create_windows_process_job = create_windows_process_job


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
    stdout_total_bytes: int = 0
    stderr_total_bytes: int = 0
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES


class _BoundedTailBuffer:
    """Continuously drain a stream while retaining only its bounded byte tail."""

    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        self.total_bytes = 0
        self._tail = bytearray()

    def append(self, value: bytes) -> None:
        self.total_bytes += len(value)
        if len(value) >= self.limit_bytes:
            self._tail[:] = value[-self.limit_bytes :]
            return
        overflow = len(self._tail) + len(value) - self.limit_bytes
        if overflow > 0:
            del self._tail[:overflow]
        self._tail.extend(value)

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self._tail)

    def text(self) -> str:
        return bytes(self._tail).decode("utf-8", errors="replace")


def _drain(stream: object, buffer: _BoundedTailBuffer) -> None:
    if not hasattr(stream, "read"):
        return
    while True:
        try:
            value = stream.read(_STREAM_READ_SIZE)
        except (OSError, ValueError):
            return
        if not value:
            return
        if isinstance(value, str):
            buffer.append(value.encode("utf-8", errors="replace"))
        elif isinstance(value, bytes):
            buffer.append(value)


def _wait_until(process: subprocess.Popen[bytes], deadline: float) -> bool:
    while process.poll() is None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(0.05, remaining))
    return True


def _kill_parent_and_wait(
    process: subprocess.Popen[bytes],
    *,
    grace_seconds: float,
) -> bool:
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    if not _wait_until(process, monotonic() + grace_seconds):
        return False
    process.wait()
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
    process: subprocess.Popen[bytes],
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
        if _posix_process_group_quiesced(process_group):
            return True, "posix_process_group_zombies_only"
        return False, "posix_process_group_unverifiable"
    except OSError:
        return True, "posix_process_group_terminated"

    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return True, "posix_process_group_terminated"
    except PermissionError:
        if _posix_process_group_quiesced(process_group):
            return True, "posix_process_group_zombies_only"
        return False, "posix_process_group_unverifiable"
    except OSError:
        return False, "posix_process_group_descendants_remain"
    quiescence_deadline = monotonic() + grace_seconds
    while monotonic() < quiescence_deadline:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True, "posix_process_group_terminated"
        except PermissionError:
            if _posix_process_group_quiesced(process_group):
                return True, "posix_process_group_zombies_only"
            return False, "posix_process_group_unverifiable"
        except OSError:
            return True, "posix_process_group_terminated"
        if _posix_process_group_quiesced(process_group):
            return True, "posix_process_group_zombies_only"
        sleep(min(0.05, max(0.0, quiescence_deadline - monotonic())))
    return False, "posix_process_group_descendants_remain"


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float,
    termination_grace_seconds: float = 3.0,
    capture_limit_bytes: int = DEFAULT_CAPTURE_LIMIT_BYTES,
) -> BoundedProcessResult:
    """Run ``command`` and terminate its complete process tree at the deadline."""

    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("bounded process command must contain non-empty strings")
    if timeout_seconds <= 0:
        raise ValueError("bounded process timeout must be greater than zero")
    if termination_grace_seconds <= 0:
        raise ValueError("termination grace must be greater than zero")
    if capture_limit_bytes <= 0:
        raise ValueError("capture limit must be greater than zero")

    creationflags = 0
    start_new_session = False
    windows_job: WindowsProcessJob | None = None
    if os.name == "nt":
        windows_job = _create_windows_process_job()
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | 0x00000004
    else:
        start_new_session = True

    started = monotonic()
    process: subprocess.Popen[bytes] | None = None
    stdout_buffer = _BoundedTailBuffer(capture_limit_bytes)
    stderr_buffer = _BoundedTailBuffer(capture_limit_bytes)
    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    cleanup_attempted = False
    cleanup_succeeded = True
    termination_method: str | None = None
    completed = False
    try:
        process = subprocess.Popen(  # noqa: S603
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            creationflags=creationflags,
            start_new_session=start_new_session,
        )
        if windows_job is not None and not windows_job.assign(process.pid):
            parent_exited = _kill_parent_and_wait(
                process,
                grace_seconds=termination_grace_seconds,
            )
            raise RuntimeError(
                "Windows process containment could not assign the suspended leader "
                f"to its Job Object; parent cleanup verified={parent_exited}"
            )

        stdout_thread = threading.Thread(
            target=_drain,
            args=(process.stdout, stdout_buffer),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain,
            args=(process.stderr, stderr_buffer),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        if windows_job is not None and not windows_job.resume(process.pid):
            job_quiesced = windows_job.terminate_and_wait(
                timeout_seconds=termination_grace_seconds
            )
            parent_exited = _kill_parent_and_wait(
                process,
                grace_seconds=termination_grace_seconds,
            )
            raise RuntimeError(
                "Windows process containment could not resume the suspended Job Object "
                f"member; job cleanup verified={job_quiesced}; "
                f"parent cleanup verified={parent_exited}"
            )

        completed = _wait_until(process, started + timeout_seconds)
        cleanup_attempted = not completed
        if windows_job is not None:
            cleanup_attempted = True
            cleanup_succeeded = windows_job.terminate_and_wait(
                timeout_seconds=termination_grace_seconds
            )
            termination_method = (
                "windows_job_object_quiesced"
                if completed and cleanup_succeeded
                else "windows_job_object_terminated"
                if cleanup_succeeded
                else "windows_job_object_unverified"
            )
        elif not completed:
            cleanup_succeeded, termination_method = _cleanup_posix_tree(
                process,
                grace_seconds=termination_grace_seconds,
            )

        if process.poll() is None:
            parent_exited = _kill_parent_and_wait(
                process,
                grace_seconds=termination_grace_seconds,
            )
            cleanup_succeeded = False
            termination_method = (
                f"{termination_method or 'fallback'}_"
                f"{'parent_only' if parent_exited else 'parent_unverified'}"
            )
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
        if stdout_thread.is_alive() or stderr_thread.is_alive():
            cleanup_succeeded = False
    finally:
        if windows_job is not None and not windows_job.close():
            cleanup_succeeded = False
            termination_method = "windows_job_object_close_unverified"

    assert process is not None
    return BoundedProcessResult(
        returncode=process.returncode,
        stdout=stdout_buffer.text(),
        stderr=stderr_buffer.text(),
        elapsed_seconds=round(monotonic() - started, 6),
        timed_out=not completed,
        cleanup_attempted=cleanup_attempted,
        cleanup_succeeded=cleanup_succeeded,
        termination_method=termination_method,
        stdout_total_bytes=stdout_buffer.total_bytes,
        stderr_total_bytes=stderr_buffer.total_bytes,
        stdout_truncated=stdout_buffer.truncated,
        stderr_truncated=stderr_buffer.truncated,
        capture_limit_bytes=capture_limit_bytes,
    )
