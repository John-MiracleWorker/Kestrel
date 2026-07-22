from __future__ import annotations

import json
import os
import signal as signal_module
import stat as stat_module
import subprocess  # nosec B404
import sys
import threading
from pathlib import Path
from typing import Any

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


class WorkspaceSecretIsolationError(RuntimeError):
    """Raised when arbitrary code could read a configured raw secret vault."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


_RUNNING_PROCESSES: dict[str, tuple[subprocess.Popen[str], str | None, int | None]] = {}
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
    requires_workspace_secret_isolation: bool = False,
    require_container_isolation: bool = False,
    expected_repair_snapshot: dict[str, Any] | None = None,
) -> subprocess.CompletedProcess[str] | IsolatedValidationResult:
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
    execution_id = str(
        arguments.get("_tool_execution_id")
        or arguments.get("_tool_call_id")
        or ""
    )
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
    process = _start_subprocess(
        command,
        context=context,
        environment=process_environment,
        pipe_stdin=input_text is not None,
    )
    process_group_id: int | None = None
    try:
        process_group_id = process.pid if sys.platform != "win32" else None
        if execution_id:
            with _RUNNING_PROCESS_LOCK:
                _RUNNING_PROCESSES[execution_id] = (
                    process,
                    context.run_id,
                    process_group_id,
                )
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
        # A successful leader exit is not proof that the bounded tool is done:
        # hooks or child processes may have detached their stdio while staying
        # in the supervised process group. Quiesce that group before publishing
        # the result so no ordinary background descendant can mutate later.
        if process_group_id is not None:
            _terminate_process_group(process, process_group_id=process_group_id)
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=_redact_subprocess_text(stdout or ""),
            stderr=_redact_subprocess_text(stderr or ""),
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
        raise _SubprocessToolTimeout(_redact_subprocess_text(content)) from exc
    except BaseException:
        # No exception path may drop the tracking entry while a process tree is
        # still live (including communicate/decoding/interrupt failures).
        _terminate_process_group(process, process_group_id=process_group_id)
        try:
            process.communicate(timeout=2)
        except BaseException:
            _kill_process_group(process, process_group_id=process_group_id)
            try:
                process.wait(timeout=2)
            except BaseException:
                pass
        raise
    finally:
        if execution_id:
            with _RUNNING_PROCESS_LOCK:
                tracked = _RUNNING_PROCESSES.get(execution_id)
                if tracked is not None and tracked[0] is process:
                    del _RUNNING_PROCESSES[execution_id]


def _start_subprocess(
    command: list[str],
    *,
    context: ToolContext,
    environment: dict[str, str] | None = None,
    pipe_stdin: bool = False,
) -> subprocess.Popen[str]:
    common: dict[str, Any] = {
        "cwd": context.workspace,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if environment is not None:
        common["env"] = environment
    if pipe_stdin:
        common["stdin"] = subprocess.PIPE
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
    lexical_path = (
        configured
        if configured.is_absolute()
        else workspace.resolve() / configured
    )
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
            "configure a digest-pinned validation container before enabling this tool."
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
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
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
    taskkill_executable = _windows_taskkill_executable()
    if taskkill_executable is None:
        process.kill()
        return
    try:
        subprocess.run(  # noqa: S603  # nosec B603
            [taskkill_executable, "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        process.kill()


def _windows_taskkill_executable() -> str | None:
    windows_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR")
    if not windows_root:
        return None
    try:
        executable = (
            Path(windows_root).expanduser() / "System32" / "taskkill.exe"
        ).resolve(strict=True)
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
