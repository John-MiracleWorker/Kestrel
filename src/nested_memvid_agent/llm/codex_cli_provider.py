from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from ..runtime_models import ChatMessage, LLMOptions, LLMResponse, ToolSpec
from .base import LLMProvider, ProviderError
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
    ) -> None:
        self.model = None if model in {None, "", "mock"} else model
        self.workspace = workspace
        self.sandbox = sandbox
        self.profile = profile
        self.skip_git_repo_check = skip_git_repo_check
        self.ephemeral = ephemeral

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        active_options = options or LLMOptions()
        prompt = _format_prompt(messages, tools)
        with TemporaryDirectory(prefix="kestrel-codex-") as tmpdir:
            output_path = Path(tmpdir) / "last-message.txt"
            command = self._command(output_path)
            try:
                completed = subprocess.run(  # noqa: S603 - fixed executable and argument vector
                    command,
                    cwd=self.workspace,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=active_options.timeout_seconds,
                    check=False,
                )
            except FileNotFoundError as exc:
                raise ProviderError("Codex CLI not found on PATH.", code="codex_cli_not_found") from exc
            except subprocess.TimeoutExpired as exc:
                raise ProviderError(
                    f"Codex CLI timed out after {active_options.timeout_seconds}s.",
                    code="codex_cli_timeout",
                    retryable=True,
                ) from exc

            output_text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else completed.stdout.strip()
            if completed.returncode != 0:
                detail = completed.stderr.strip() or completed.stdout.strip() or f"exit_code={completed.returncode}"
                raise ProviderError(detail, code="codex_cli_nonzero_exit", retryable=True)
            return parse_agent_response(output_text)

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
