from __future__ import annotations

import os
import signal
import subprocess  # nosec B404
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from ..runtime_models import ChatMessage, LLMOptions, LLMResponse, ToolSpec
from ..tools.process_tools import (
    WorkspaceSecretIsolationError,
    assert_arbitrary_subprocess_safe,
)
from .base import LLMProvider, ProviderCapabilities, ProviderError
from .parser import parse_agent_response


class CodexCLIProvider(LLMProvider):
    """Use the local Codex CLI as Kestrel's response engine.

    This is intentionally separate from the high-risk `codex.exec` tool. The provider
    runs Codex in read-only mode by default and asks it to return Kestrel tool-call
    envelopes instead of mutating the workspace itself.
    """

    def __init__(
        self,
        *,
        model: str | None,
        workspace: Path,
        sandbox: str = "read-only",
        profile: str | None = None,
        skip_git_repo_check: bool = False,
        ephemeral: bool = True,
        secret_store_path: Path = Path(".nest/secrets/local_vault.json"),
        secret_backend: str = "json",
    ) -> None:
        self.model = None if model in {None, "", "mock"} else model
        self.workspace = workspace
        self.sandbox = sandbox
        self.profile = profile
        self.skip_git_repo_check = skip_git_repo_check
        self.ephemeral = ephemeral
        self.secret_store_path = secret_store_path
        self.secret_backend = secret_backend

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="codex-cli",
            supports_native_tools=False,
            supports_streaming=False,
            supports_json_mode=True,
            supports_system_messages=True,
            token_usage_available=False,
        )

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        active_options = options or LLMOptions()
        prompt = _format_prompt(messages, tools)
        try:
            assert_arbitrary_subprocess_safe(
                workspace=self.workspace,
                secret_store_path=self.secret_store_path,
                secret_backend=self.secret_backend,
            )
        except WorkspaceSecretIsolationError as exc:
            raise ProviderError(str(exc), code=exc.code) from exc
        with TemporaryDirectory(prefix="kestrel-codex-") as tmpdir:
            output_path = Path(tmpdir) / "last-message.txt"
            command = self._command(output_path)
            process: subprocess.Popen[str] | None = None
            try:
                process = _start_process(command, workspace=self.workspace)
                stdout, stderr = process.communicate(
                    input=prompt,
                    timeout=active_options.timeout_seconds,
                )
            except FileNotFoundError as exc:
                raise ProviderError("Codex CLI not found on PATH.", code="codex_cli_not_found") from exc
            except subprocess.TimeoutExpired as exc:
                if process is not None:
                    _terminate_process_group(process)
                raise ProviderError(
                    f"Codex CLI timed out after {active_options.timeout_seconds}s.",
                    code="codex_cli_timeout",
                    retryable=True,
                ) from exc

            if process is None:
                raise ProviderError("Codex CLI failed to start.", code="codex_cli_start_failed")
            output_text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else stdout.strip()
            if process.returncode != 0:
                detail = stderr.strip() or stdout.strip() or f"exit_code={process.returncode}"
                raise ProviderError(detail, code="codex_cli_nonzero_exit", retryable=True)
            return parse_agent_response(output_text, tools=tools, strict=True)

    def _command(self, output_path: Path) -> list[str]:
        command = [
            "codex",
            "exec",
            "--cd",
            str(self.workspace.resolve()),
            "--sandbox",
            self.sandbox,
            "--color",
            "never",
            "--ignore-user-config",
            "--output-last-message",
            str(output_path),
        ]
        if self.model:
            command.extend(["--model", self.model])
        if self.profile:
            command.extend(["--profile", self.profile])
        if self.ephemeral:
            command.append("--ephemeral")
        if self.skip_git_repo_check:
            command.append("--skip-git-repo-check")
        command.append("-")
        return command


def _start_process(command: list[str], *, workspace: Path) -> subprocess.Popen[str]:
    if sys.platform == "win32":
        return subprocess.Popen(  # noqa: S603 - fixed executable and argument vector  # nosec B603
            command,
            cwd=workspace,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(  # noqa: S603 - fixed executable and argument vector  # nosec B603
        command,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        subprocess.run(  # noqa: S603,S607 - fixed Windows process-tree termination  # nosec B603 B607
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        process.wait(timeout=2.0)
        return
    group_id = process.pid
    try:
        os.killpg(group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        process.kill()


def _format_prompt(messages: list[ChatMessage], tools: list[ToolSpec]) -> str:
    rendered_messages = "\n\n".join(
        f"## {message.role.upper()}{f' {message.name}' if message.name else ''}\n{message.content}"
        for message in messages
    )
    rendered_tools = "\n\n".join(spec.to_prompt_block() for spec in tools)
    return (
        "You are the response engine for Kestrel, a local nested-learning agent runtime.\n"
        "Kestrel owns memory, tools, approvals, MCP connections, skills, and file writes.\n"
        "In this Codex subprocess, do not modify files or run risky commands. If you need a Kestrel tool, return exactly this JSON envelope:\n"
        '{"message":"brief user-visible note","tool_calls":[{"name":"tool.name","arguments":{}}]}\n'
        "If no tool is needed, answer normally in plain text.\n\n"
        f"# Kestrel Messages\n{rendered_messages}\n\n"
        f"# Kestrel Tool Registry\n{rendered_tools or 'No tools registered.'}"
    )
