from __future__ import annotations

import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from .base import AgentTool, ToolContext
from .patch_helpers import _validate_patch_paths
from .process_tools import (
    _cancel_running_subprocess,
    _normalize_python_command,
    _run_subprocess,
    _SubprocessToolTimeout,
    _truncate,
)
from .validation_helpers import _tool_validation_evidence_payload


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
    if name in {"python", "python3"}:
        return True
    suffix = name.removeprefix("python")
    return bool(suffix) and suffix[0].isdigit() and all(part.isdigit() for part in suffix.split("."))


def _is_allowlisted_command(command: list[str], allowed_first_tokens: set[str]) -> bool:
    if not command:
        return False
    executable = Path(command[0]).name.lower()
    if executable in allowed_first_tokens:
        return True
    return "python" in allowed_first_tokens and _is_python_executable_name(executable)


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
        if command and Path(command[0]).name in {"python", "python3"} and "-c" in command:
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
        try:
            completed = _run_subprocess(
                command, context=context, arguments=arguments, default_timeout=30
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode},
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except _SubprocessToolTimeout as exc:
            return self._result(call, success=False, content=str(exc), error="tool_timeout")
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="shell_failed")

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class CodexExecTool(AgentTool):
    spec = ToolSpec(
        name="codex.exec",
        description="Delegate a bounded non-interactive task to the local Codex CLI in this workspace.",
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

        timeout = max(30, min(int(arguments.get("timeout", 600)), 3600))
        max_output_chars = max(1000, min(int(arguments.get("max_output_chars", 40_000)), 100_000))
        command = [
            "codex",
            "exec",
            "--cd",
            str(context.workspace.resolve()),
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
            completed = subprocess.run(  # noqa: S603 - fixed executable and argument vector  # nosec
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
            stdout = _truncate(completed.stdout, max_output_chars)
            stderr = _truncate(completed.stderr, max_output_chars)
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{stdout}\nSTDERR:\n{stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "returncode": completed.returncode,
                    "sandbox": sandbox,
                    "model": model or None,
                    "stdout_truncated": len(completed.stdout) > max_output_chars,
                    "stderr_truncated": len(completed.stderr) > max_output_chars,
                },
                error=None if completed.returncode == 0 else "codex_nonzero_exit",
            )
        except FileNotFoundError:
            return self._result(
                call,
                success=False,
                content="Codex CLI not found on PATH.",
                error="codex_cli_not_found",
            )
        except subprocess.TimeoutExpired as exc:
            return self._result(
                call,
                success=False,
                content=f"Codex CLI timed out after {timeout}s: {exc}",
                error="codex_timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="codex_cli_failed")


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

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        patch_text = str(arguments.get("patch", ""))
        check_only = bool(arguments.get("check", False))
        if not patch_text.strip():
            return self._result(call, success=False, content="Missing patch", error="missing_patch")
        try:
            _validate_patch_paths(context.workspace, patch_text)
            command = (
                ["git", "apply", "--check"]
                if check_only
                else ["git", "apply", "--whitespace=nowarn"]
            )
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments  # nosec
                command,
                cwd=context.workspace,
                input=patch_text,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={"returncode": completed.returncode, "check": check_only},
                error=None if completed.returncode == 0 else "patch_apply_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="patch_apply_failed")


class TestRunTool(AgentTool):
    spec = ToolSpec(
        name="test.run",
        description="Run a bounded test command in the workspace. Disabled unless shell execution is enabled.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
            },
        },
        risk="high",
        requires_approval=True,
        produces_validation=True,
    )
    allowed_first_tokens = {"pytest", "python", "python3"}
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
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
                command, context=context, arguments=arguments, default_timeout=120
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "returncode": completed.returncode,
                    "validation_evidence": _tool_validation_evidence_payload(
                        "test_refs", "test.run", command, content, completed.returncode == 0
                    ),
                },
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except _SubprocessToolTimeout as exc:
            return self._result(call, success=False, content=str(exc), error="tool_timeout")
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="test_run_failed")

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class LintRunTool(AgentTool):
    spec = ToolSpec(
        name="lint.run",
        description="Run a bounded lint/typecheck command in the workspace. Disabled unless shell execution is enabled.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
            },
        },
        risk="high",
        requires_approval=True,
        produces_validation=True,
    )
    allowed_first_tokens = {"ruff", "mypy", "python", "python3"}
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
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
                command, context=context, arguments=arguments, default_timeout=120
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "returncode": completed.returncode,
                    "validation_evidence": _tool_validation_evidence_payload(
                        "lint_refs", "lint.run", command, content, completed.returncode == 0
                    ),
                },
                error=None if completed.returncode == 0 else "nonzero_exit",
            )
        except _SubprocessToolTimeout as exc:
            return self._result(call, success=False, content=str(exc), error="tool_timeout")
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="lint_run_failed")

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)
