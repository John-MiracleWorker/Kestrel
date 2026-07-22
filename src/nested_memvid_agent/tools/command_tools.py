from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from typing import Any

from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..security_boundary import redact_text
from ..validation_runner import ValidationIsolationError
from .base import AgentTool, ToolContext
from .patch_helpers import _validate_patch_paths
from .process_tools import (
    WorkspaceSecretIsolationError,
    _cancel_running_subprocess,
    _normalize_python_command,
    _redact_subprocess_text,
    _run_subprocess,
    _SubprocessToolTimeout,
    _truncate,
)
from .validation_helpers import (
    _authenticated_validation_evidence_payload,
    _tool_validation_evidence_payload,
)
from .workspace_tools import (
    _assert_workspace_path_allowed,
    _open_workspace_regular_file,
    _safe_path,
    _workspace_path_is_private,
)

_MAX_UTILITY_OUTPUT_CHARS = 200_000
_MAX_UTILITY_PATHS = 16
_MAX_UTILITY_LIST_ENTRIES = 1_000


def _tool_call_from_runtime_arguments(name: str, arguments: dict[str, Any]) -> ToolCall:
    public_arguments = {
        key: value for key, value in arguments.items() if not str(key).startswith("_")
    }
    call_id = str(arguments.get("_tool_call_id") or "").strip()
    if call_id:
        return ToolCall(name=name, arguments=public_arguments, id=call_id)
    return ToolCall(name=name, arguments=public_arguments)


def _remote_mutation_violation(command: list[str]) -> str | None:
    if not command:
        return None
    executable = Path(command[0]).name.lower()
    rest = [part.lower() for part in command[1:]]
    if executable == "git" and rest:
        if rest[0] == "push":
            return "git push"
        if rest[0] == "tag":
            return "git tag"
        if rest[:2] == ["remote", "set-url"]:
            return "git remote set-url"
    if executable == "gh" and len(rest) >= 2:
        pair = tuple(rest[:2])
        if pair == ("repo", "edit"):
            return "gh repo edit"
        if pair == ("secret", "set"):
            return "gh secret set"
        if pair == ("workflow", "enable"):
            return "gh workflow enable"
    return None


def _is_python_executable_name(name: str) -> bool:
    name = name.removesuffix(".exe")
    if name in {"python", "python3"}:
        return True
    suffix = name.removeprefix("python")
    return (
        bool(suffix) and suffix[0].isdigit() and all(part.isdigit() for part in suffix.split("."))
    )


def _is_allowlisted_command(command: list[str], allowed_first_tokens: set[str]) -> bool:
    if not command:
        return False
    requested = command[0]
    # An allowlisted basename must never authorize a caller-selected executable
    # such as ``./echo`` or ``/tmp/ls``.
    executable = Path(requested).name.lower()
    if requested != Path(requested).name or "/" in requested or "\\" in requested:
        # Validation tools normalize every Python spelling to the controlled
        # interpreter before entering the OCI snapshot. Other caller-selected
        # executable paths remain forbidden.
        return "python" in allowed_first_tokens and _is_python_executable_name(executable)
    if executable in allowed_first_tokens:
        return True
    return "python" in allowed_first_tokens and _is_python_executable_name(executable)


def _run_bounded_utility(
    command: list[str],
    context: ToolContext,
) -> tuple[int, str, str]:
    """Implement the tiny shell allowlist without launching a host executable."""

    executable = command[0].lower()
    arguments = command[1:]
    if executable == "echo":
        newline = True
        if arguments[:1] == ["-n"]:
            newline = False
            arguments = arguments[1:]
        output = " ".join(arguments) + ("\n" if newline else "")
        return 0, _bounded_utility_output(output), ""
    if executable == "pwd":
        if arguments not in ([], ["--"]):
            raise ValueError("pwd accepts no operands")
        return 0, f"{context.workspace.resolve()}\n", ""
    if executable == "cat":
        paths = _utility_path_operands(arguments, allowed_flags=frozenset())
        if not paths:
            raise ValueError("cat requires at least one workspace file")
        if len(paths) > _MAX_UTILITY_PATHS:
            raise ValueError("cat path limit exceeded")
        chunks: list[bytes] = []
        total = 0
        for requested_path in paths:
            path = _safe_path(context.workspace, requested_path)
            _assert_workspace_path_allowed(
                context,
                path,
                requested_path=requested_path,
            )
            with _open_workspace_regular_file(context, path) as (handle, _):
                raw = handle.read(_MAX_UTILITY_OUTPUT_CHARS * 4 - total + 1)
            total += len(raw)
            if total > _MAX_UTILITY_OUTPUT_CHARS * 4:
                raise ValueError("cat output limit exceeded")
            chunks.append(raw)
        output = b"".join(chunks).decode("utf-8", errors="replace")
        return 0, _bounded_utility_output(output), ""
    if executable == "ls":
        paths, flags = _utility_ls_arguments(arguments)
        return 0, _bounded_utility_output(_list_workspace_paths(context, paths, flags)), ""
    raise ValueError("Command is not allowlisted")


def _utility_path_operands(
    arguments: list[str],
    *,
    allowed_flags: frozenset[str],
) -> list[str]:
    operands: list[str] = []
    after_options = False
    for argument in arguments:
        if argument == "--" and not after_options:
            after_options = True
            continue
        if not after_options and argument.startswith("-"):
            if argument == "-" or any(flag not in allowed_flags for flag in argument[1:]):
                raise ValueError(f"Unsupported utility option: {argument}")
            continue
        operands.append(argument)
    return operands


def _utility_ls_arguments(arguments: list[str]) -> tuple[list[str], frozenset[str]]:
    flags: set[str] = set()
    operands: list[str] = []
    after_options = False
    for argument in arguments:
        if argument == "--" and not after_options:
            after_options = True
            continue
        if not after_options and argument.startswith("-"):
            if argument == "-" or any(flag not in {"1", "a", "A", "l"} for flag in argument[1:]):
                raise ValueError(f"Unsupported ls option: {argument}")
            flags.update(argument[1:])
            continue
        operands.append(argument)
    if len(operands) > _MAX_UTILITY_PATHS:
        raise ValueError("ls path limit exceeded")
    return operands or ["."], frozenset(flags)


def _list_workspace_paths(
    context: ToolContext,
    requested_paths: list[str],
    flags: frozenset[str],
) -> str:
    rows: list[str] = []
    remaining = _MAX_UTILITY_LIST_ENTRIES
    show_hidden = "a" in flags or "A" in flags
    long_format = "l" in flags
    multiple = len(requested_paths) > 1
    for requested_path in requested_paths:
        path = _safe_path(context.workspace, requested_path)
        _assert_workspace_path_allowed(context, path, requested_path=requested_path)
        if multiple:
            if rows:
                rows.append("")
            rows.append(f"{requested_path}:")
        if path.is_file():
            entries = [path]
        elif path.is_dir():
            entries = sorted(path.iterdir(), key=lambda item: item.name.casefold())
        else:
            raise ValueError(f"ls target is not a regular file or directory: {requested_path}")
        for entry in entries:
            if remaining <= 0:
                raise ValueError("ls entry limit exceeded")
            if not show_hidden and entry.name.startswith("."):
                continue
            if _workspace_path_is_private(context, entry):
                continue
            remaining -= 1
            name = entry.name + ("/" if entry.is_dir() else "")
            if long_format:
                metadata = entry.lstat()
                rows.append(f"{stat.filemode(metadata.st_mode)} {metadata.st_size:>10} {name}")
            else:
                rows.append(name)
    return "\n".join(rows) + ("\n" if rows else "")


def _bounded_utility_output(value: str) -> str:
    if len(value) > _MAX_UTILITY_OUTPUT_CHARS:
        raise ValueError("utility output limit exceeded")
    return _redact_subprocess_text(value)


def _validate_allowlisted_path_operands(
    command: list[str], context: ToolContext
) -> str | None:
    """Keep allowlisted file-inspection commands inside the non-sensitive workspace."""

    if not command:
        return "Missing command"
    executable = Path(command[0]).name.lower()
    if executable not in {"cat", "ls"}:
        return None
    operands: list[str] = []
    after_options = False
    for argument in command[1:]:
        if argument == "--" and not after_options:
            after_options = True
            continue
        if not after_options and argument.startswith("-"):
            continue
        operands.append(argument)
    if executable == "cat" and not operands:
        return "cat requires at least one workspace file"
    for operand in operands:
        if operand == "-":
            return "stdin operands are not allowed"
        candidate = Path(operand)
        if not candidate.is_absolute():
            candidate = context.workspace / candidate
        try:
            _assert_workspace_path_allowed(context, candidate, requested_path=operand)
        except ValueError as exc:
            return str(exc)
    return None


class ShellRunTool(AgentTool):
    spec = ToolSpec(
        name="shell.run",
        description="Run an allowlisted shell command in the workspace. Disabled unless allow_shell is true.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
            },
            "required": ["command"],
        },
        risk="high",
        requires_approval=True,
    )
    allowed_first_tokens = {"echo", "pwd", "ls", "cat"}
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(
            isinstance(item, str) for item in command_raw
        ):
            return self._result(
                call, success=False, content="command must be list[str]", error="bad_command"
            )
        command = list(command_raw)
        if (
            command
            and _is_python_executable_name(Path(command[0]).name.lower())
            and "-c" in command
        ):
            return self._result(
                call,
                success=False,
                content="Inline Python shell commands are not allowlisted",
                error="command_not_allowlisted",
            )
        remote_mutation = _remote_mutation_violation(command)
        git_push_blocked = remote_mutation == "git push" and not context.config.allow_git_push
        if remote_mutation and (git_push_blocked or not context.config.allow_remote_mutation):
            return self._result(
                call,
                success=False,
                content=f"Remote mutation command blocked: {remote_mutation}",
                error="remote_mutation_blocked",
                data={
                    "violation": remote_mutation,
                    "allow_git_push": context.config.allow_git_push,
                    "allow_remote_mutation": context.config.allow_remote_mutation,
                },
            )
        if not _is_allowlisted_command(command, self.allowed_first_tokens):
            return self._result(
                call,
                success=False,
                content="Command is not allowlisted",
                error="command_not_allowlisted",
            )
        path_error = _validate_allowlisted_path_operands(command, context)
        if path_error is not None:
            return self._result(
                call,
                success=False,
                content=path_error,
                error="path_not_allowed",
            )
        try:
            returncode, stdout, stderr = _run_bounded_utility(command, context)
            content = _redact_subprocess_text(
                f"exit_code={returncode}\nSTDOUT:\n{stdout}\n"
                f"STDERR:\n{stderr}"
            )
            return self._result(
                call,
                success=returncode == 0,
                content=content,
                data={"returncode": returncode, "execution_mode": "bounded_utility"},
                error=None if returncode == 0 else "nonzero_exit",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="shell_failed"
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class CodexExecTool(AgentTool):
    spec = ToolSpec(
        name="codex.exec",
        description=(
            "Delegate a bounded non-interactive task to a Codex CLI installed in the configured "
            "digest-pinned, networkless OCI validation image. There is no host fallback."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "model": {"type": "string"},
                "sandbox": {
                    "type": "string",
                    "enum": ["read-only", "workspace-write"],
                    "default": "read-only",
                },
                "timeout": {"type": "integer", "minimum": 30, "maximum": 3600},
                "ephemeral": {"type": "boolean", "default": True},
                "json_events": {"type": "boolean", "default": False},
                "skip_git_repo_check": {"type": "boolean", "default": False},
                "max_output_chars": {"type": "integer", "minimum": 1000, "maximum": 100000},
            },
            "required": ["prompt"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("codex-cli", "delegation"),
    )
    needs_call_id = True
    wait_for_completion_on_timeout = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return self._result(
                call, success=False, content="Missing prompt", error="missing_prompt"
            )

        sandbox = str(arguments.get("sandbox", "read-only"))
        if sandbox not in {"read-only", "workspace-write"}:
            return self._result(
                call, success=False, content="Unsupported Codex sandbox", error="bad_sandbox"
            )
        if sandbox == "workspace-write":
            return self._result(
                call,
                success=False,
                content=(
                    "Workspace-write Codex execution is outside the current read-only OCI boundary. "
                    "Use the staged repair.prepare/apply/validate/review pipeline instead."
                ),
                error="codex_workspace_write_uncontained",
            )

        timeout = max(30, min(int(arguments.get("timeout", 600)), 3600))
        max_output_chars = max(1000, min(int(arguments.get("max_output_chars", 40_000)), 100_000))
        command = [
            "codex",
            "exec",
            "--cd",
            "/extension",
            "--sandbox",
            sandbox,
            "--color",
            "never",
        ]
        model = str(arguments.get("model", "")).strip()
        if model:
            command.extend(["--model", model])
        if bool(arguments.get("ephemeral", True)):
            command.append("--ephemeral")
        if bool(arguments.get("json_events", False)):
            command.append("--json")
        if bool(arguments.get("skip_git_repo_check", False)):
            command.append("--skip-git-repo-check")
        command.append(prompt)

        try:
            completed = _run_subprocess(
                command,
                context=context,
                arguments=arguments,
                default_timeout=timeout,
                sanitize_environment=True,
                require_container_isolation=True,
            )
            safe_stdout = _redact_subprocess_text(completed.stdout)
            safe_stderr = _redact_subprocess_text(completed.stderr)
            stdout = _truncate(safe_stdout, max_output_chars)
            stderr = _truncate(safe_stderr, max_output_chars)
            content = _redact_subprocess_text(
                f"exit_code={completed.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            )
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "returncode": completed.returncode,
                    "sandbox": sandbox,
                    "model": model or None,
                    "stdout_truncated": len(safe_stdout) > max_output_chars,
                    "stderr_truncated": len(safe_stderr) > max_output_chars,
                },
                error=None if completed.returncode == 0 else "codex_nonzero_exit",
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except ValidationIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except FileNotFoundError:
            return self._result(
                call,
                success=False,
                content="Codex CLI not found on PATH.",
                error="codex_cli_not_found",
            )
        except _SubprocessToolTimeout as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(f"Codex CLI timed out: {exc}"),
                error="codex_timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="codex_cli_failed"
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class PatchApplyTool(AgentTool):
    spec = ToolSpec(
        name="patch.apply",
        description="Apply a unified diff inside the workspace. Disabled unless file writes are enabled.",
        parameters={
            "type": "object",
            "properties": {
                "patch": {"type": "string"},
                "check": {"type": "boolean"},
            },
            "required": ["patch"],
        },
        risk="high",
        requires_approval=True,
    )
    needs_call_id = True
    wait_for_completion_on_timeout = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        patch_text = str(arguments.get("patch", ""))
        check_only = bool(arguments.get("check", False))
        if not patch_text.strip():
            return self._result(call, success=False, content="Missing patch", error="missing_patch")
        try:
            _validate_patch_paths(context.workspace, patch_text, context=context)
            command = (
                ["git", "apply", "--check"]
                if check_only
                else ["git", "apply", "--whitespace=nowarn"]
            )
            completed = _run_subprocess(
                command,
                context=context,
                arguments=arguments,
                default_timeout=30,
                sanitize_environment=True,
                input_text=patch_text,
            )
            content = _redact_subprocess_text(
                f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode, "check": check_only},
                error=None if completed.returncode == 0 else "patch_apply_failed",
            )
        except _SubprocessToolTimeout as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="tool_timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="patch_apply_failed"
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class TestRunTool(AgentTool):
    spec = ToolSpec(
        name="test.run",
        description=(
            "Run a bounded test command against a private workspace snapshot in the configured "
            "digest-pinned OCI validation image. There is no host fallback."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
                "subject_record_id": {"type": "string"},
            },
        },
        risk="high",
        requires_approval=True,
        produces_validation=True,
    )
    allowed_first_tokens = {"pytest", "python", "python3"}
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call_from_runtime_arguments(self.spec.name, arguments)
        command_raw = arguments.get("command", ["pytest", "-q"])
        if not isinstance(command_raw, list) or not all(
            isinstance(item, str) for item in command_raw
        ):
            return self._result(
                call, success=False, content="command must be list[str]", error="bad_command"
            )
        command = list(command_raw)
        if not _is_allowlisted_command(command, self.allowed_first_tokens):
            return self._result(
                call,
                success=False,
                content="Command is not allowlisted",
                error="command_not_allowlisted",
            )
        command = _normalize_python_command(command)
        try:
            completed = _run_subprocess(
                command,
                context=context,
                arguments=arguments,
                default_timeout=120,
                sanitize_environment=True,
                requires_workspace_secret_isolation=True,
                require_container_isolation=True,
            )
            content = _redact_subprocess_text(
                f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
            validation_evidence = _tool_validation_evidence_payload(
                "test_refs", "test.run", command, content, completed.returncode == 0
            )
            if completed.returncode == 0:
                receipt_id = context.memory.put_runtime_validation_receipt(
                    tool_name=self.spec.name,
                    tool_call_id=call.id,
                    evidence_bucket="test",
                    command=command,
                    output_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    session_id=context.session_id,
                    run_id=context.run_id,
                    subject_record_id=str(arguments.get("subject_record_id") or "").strip()
                    or None,
                )
                validation_evidence = _authenticated_validation_evidence_payload(
                    "test_refs",
                    receipt_id=receipt_id,
                    quote=content,
                    source_evidence_chars=len(content),
                )
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "returncode": completed.returncode,
                    "validation_evidence": validation_evidence,
                },
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except ValidationIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except _SubprocessToolTimeout as exc:
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="tool_timeout"
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="test_run_failed"
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class LintRunTool(AgentTool):
    spec = ToolSpec(
        name="lint.run",
        description=(
            "Run a bounded lint/typecheck command against a private workspace snapshot in the "
            "configured digest-pinned OCI validation image. There is no host fallback."
        ),
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
                "subject_record_id": {"type": "string"},
            },
        },
        risk="high",
        requires_approval=True,
        produces_validation=True,
    )
    allowed_first_tokens = {"ruff", "mypy", "python", "python3"}
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call_from_runtime_arguments(self.spec.name, arguments)
        command_raw = arguments.get("command", ["ruff", "check", "."])
        if not isinstance(command_raw, list) or not all(
            isinstance(item, str) for item in command_raw
        ):
            return self._result(
                call, success=False, content="command must be list[str]", error="bad_command"
            )
        command = list(command_raw)
        if not _is_allowlisted_command(command, self.allowed_first_tokens):
            return self._result(
                call,
                success=False,
                content="Command is not allowlisted",
                error="command_not_allowlisted",
            )
        command = _normalize_python_command(command)
        try:
            completed = _run_subprocess(
                command,
                context=context,
                arguments=arguments,
                default_timeout=120,
                sanitize_environment=True,
                requires_workspace_secret_isolation=True,
                require_container_isolation=True,
            )
            content = _redact_subprocess_text(
                f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
            validation_evidence = _tool_validation_evidence_payload(
                "lint_refs", "lint.run", command, content, completed.returncode == 0
            )
            if completed.returncode == 0:
                receipt_id = context.memory.put_runtime_validation_receipt(
                    tool_name=self.spec.name,
                    tool_call_id=call.id,
                    evidence_bucket="lint",
                    command=command,
                    output_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    session_id=context.session_id,
                    run_id=context.run_id,
                    subject_record_id=str(arguments.get("subject_record_id") or "").strip()
                    or None,
                )
                validation_evidence = _authenticated_validation_evidence_payload(
                    "lint_refs",
                    receipt_id=receipt_id,
                    quote=content,
                    source_evidence_chars=len(content),
                )
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "returncode": completed.returncode,
                    "validation_evidence": validation_evidence,
                },
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except ValidationIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except _SubprocessToolTimeout as exc:
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="tool_timeout"
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="lint_run_failed"
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)
