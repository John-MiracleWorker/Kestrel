from __future__ import annotations

import subprocess
from pathlib import Path

from pytest import MonkeyPatch

from nested_memvid_agent.llm.codex_cli_provider import CodexCLIProvider
from nested_memvid_agent.runtime_models import ChatMessage, LLMOptions, ToolSpec


def test_codex_cli_provider_reads_output_last_message(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("hello from codex", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ignored stdout", stderr="")

    monkeypatch.setattr("nested_memvid_agent.llm.codex_cli_provider.subprocess.run", fake_run)
    provider = CodexCLIProvider(model="gpt-test", workspace=tmp_path)

    response = provider.generate(
        [ChatMessage(role="user", content="hello")],
        tools=[],
        options=LLMOptions(timeout_seconds=123),
    )

    assert response.content == "hello from codex"
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["codex", "exec"]
    assert ["--cd", str(tmp_path.resolve())] == command[2:4]
    assert ["--sandbox", "read-only"] == command[4:6]
    assert ["--model", "gpt-test"] == command[command.index("--model") : command.index("--model") + 2]
    assert "--ephemeral" in command
    assert command[-1] == "-"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["timeout"] == 123
    assert "Kestrel Tool Registry" in str(kwargs["input"])


def test_codex_cli_provider_parses_tool_envelope(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text(
            '{"message":"I need memory.","tool_calls":[{"name":"memory.search","arguments":{"query":"needle"}}]}',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("nested_memvid_agent.llm.codex_cli_provider.subprocess.run", fake_run)
    provider = CodexCLIProvider(model=None, workspace=tmp_path)

    response = provider.generate(
        [ChatMessage(role="user", content="find needle")],
        tools=[ToolSpec(name="memory.search", description="Search memory", parameters={"type": "object"})],
    )

    assert response.content == "I need memory."
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "memory.search"
    assert response.tool_calls[0].arguments == {"query": "needle"}
