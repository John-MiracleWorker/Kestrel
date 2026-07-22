from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from pytest import MonkeyPatch

from nested_memvid_agent.llm.base import ProviderError
from nested_memvid_agent.llm.codex_cli_provider import CodexCLIProvider
from nested_memvid_agent.runtime_models import ChatMessage, LLMOptions, ToolSpec


def test_codex_cli_provider_reads_output_last_message(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: object) -> None:
            captured["command"] = command
            captured["kwargs"] = kwargs
            self.command = command

        def communicate(self, *, input: str, timeout: int) -> tuple[str, str]:
            captured["input"] = input
            captured["timeout"] = timeout
            output_path = Path(self.command[self.command.index("--output-last-message") + 1])
            output_path.write_text("hello from codex", encoding="utf-8")
            return "ignored stdout", ""

    monkeypatch.setattr("nested_memvid_agent.llm.codex_cli_provider.subprocess.Popen", FakeProcess)
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
    assert "--ignore-user-config" in command
    assert ["--model", "gpt-test"] == command[command.index("--model") : command.index("--model") + 2]
    assert "--ephemeral" in command
    assert command[-1] == "-"
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    if os.name == "nt":
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        assert "start_new_session" not in kwargs
    else:
        assert kwargs["start_new_session"] is True
        assert "creationflags" not in kwargs
    assert captured["timeout"] == 123
    assert "Kestrel Tool Registry" in str(captured["input"])


def test_codex_cli_provider_parses_tool_envelope(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: object) -> None:
            del kwargs
            self.command = command

        def communicate(self, *, input: str, timeout: int) -> tuple[str, str]:
            del input, timeout
            output_path = Path(self.command[self.command.index("--output-last-message") + 1])
            output_path.write_text(
                '{"message":"I need memory.","tool_calls":[{"name":"memory.search","arguments":{"query":"needle"}}]}',
                encoding="utf-8",
            )
            return "", ""

    monkeypatch.setattr("nested_memvid_agent.llm.codex_cli_provider.subprocess.Popen", FakeProcess)
    provider = CodexCLIProvider(model=None, workspace=tmp_path)

    response = provider.generate(
        [ChatMessage(role="user", content="find needle")],
        tools=[ToolSpec(name="memory.search", description="Search memory", parameters={"type": "object"})],
    )

    assert response.content == "I need memory."
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "memory.search"
    assert response.tool_calls[0].arguments == {"query": "needle"}


def test_codex_cli_provider_blocks_keyring_records_before_launch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    metadata = tmp_path / "keyring-metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "secrets": {
                    "provider": {
                        "id": "provider",
                        "keyring_username": "provider:v1",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("Codex process must not launch")

    monkeypatch.setattr(
        "nested_memvid_agent.llm.codex_cli_provider.subprocess.Popen",
        forbidden_launch,
    )
    provider = CodexCLIProvider(
        model=None,
        workspace=tmp_path,
        secret_store_path=metadata,
        secret_backend="keyring",
    )

    with pytest.raises(ProviderError) as raised:
        provider.generate([ChatMessage(role="user", content="hello")], tools=[])

    assert raised.value.code == "keyring_process_isolation_required"


def test_codex_cli_provider_blocks_repair_trust_before_launch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    trust_root = tmp_path / ".nest"
    trust_root.mkdir()
    key = trust_root / "repair_receipt_signing.v2.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("Codex process must not launch")

    monkeypatch.setattr(
        "nested_memvid_agent.llm.codex_cli_provider.subprocess.Popen",
        forbidden_launch,
    )
    provider = CodexCLIProvider(model=None, workspace=tmp_path)

    with pytest.raises(ProviderError) as raised:
        provider.generate([ChatMessage(role="user", content="hello")], tools=[])

    assert raised.value.code == "repair_trust_process_isolation_required"


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
def test_codex_cli_timeout_terminates_descendant_process_group(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    executable = tmp_path / "codex"
    executable.write_text(
        "#!/bin/sh\n"
        "trap '' TERM\n"
        "(trap '' TERM; sleep 30) &\n"
        f"echo $! > {child_pid_path}\n"
        "sleep 30\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    provider = CodexCLIProvider(model="gpt-test", workspace=tmp_path)

    with pytest.raises(RuntimeError, match="timed out"):
        provider.generate(
            [ChatMessage(role="user", content="Hello")],
            tools=[],
            options=LLMOptions(timeout_seconds=1),
        )

    deadline = time.monotonic() + 2.0
    while not child_pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    child_pid = int(child_pid_path.read_text(encoding="utf-8").strip())
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("Codex CLI descendant survived provider timeout")
