from __future__ import annotations

import subprocess
from pathlib import Path

from pytest import MonkeyPatch

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools


def test_memory_search_tool_returns_hits(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            title="Needle fact",
            content="The needle lives in semantic memory.",
            confidence=0.8,
        )
    )
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="memory.search", arguments={"query": "needle", "k": 3}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert result.success
    assert "Needle fact" in result.content


def test_file_read_rejects_path_escape(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="file.read", arguments={"path": "../outside.txt"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "file_read_failed"


def test_high_risk_tool_requires_enablement(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}),
        ToolContext(memory=memory, config=AgentConfig(allow_shell=False), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "approval_required"


def test_shell_tool_runs_when_enabled(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}),
        ToolContext(memory=memory, config=AgentConfig(allow_shell=True), workspace=tmp_path),
    )
    assert result.success
    assert "hi" in result.content


def test_default_registry_includes_spec_tools() -> None:
    registry = build_default_tools()
    names = {spec.name for spec in registry.specs()}
    assert {
        "repo.search",
        "repo.map",
        "patch.apply",
        "test.run",
        "git.status",
        "git.diff",
        "memvid.verify",
        "memvid.doctor",
        "memvid.stats",
        "memory.learn",
        "memory.consolidate",
        "context.pack",
        "context.expand",
        "capsule.summarize",
        "capsule.apply",
        "memory.conflicts",
        "codex.exec",
    } <= names


def test_codex_exec_requires_approval_by_default(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="codex.exec", arguments={"prompt": "summarize this repo"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "approval_required"


def test_codex_exec_runs_when_enabled(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    captured: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(command, 0, stdout="codex done", stderr="")

    monkeypatch.setattr("nested_memvid_agent.tools.builtin.subprocess.run", fake_run)

    result = registry.execute(
        ToolCall(
            name="codex.exec",
            arguments={
                "prompt": "summarize this repo",
                "model": "gpt-test",
                "sandbox": "workspace-write",
                "timeout": 45,
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(allow_codex_cli=True), workspace=tmp_path),
    )

    assert result.success
    assert "codex done" in result.content
    command = captured["command"]
    assert isinstance(command, list)
    assert command[:2] == ["codex", "exec"]
    assert ["--cd", str(tmp_path.resolve())] == command[2:4]
    assert ["--sandbox", "workspace-write"] == command[4:6]
    assert "--ephemeral" in command
    assert command[-1] == "summarize this repo"


def test_repo_search_stays_in_workspace(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("needle lives here", encoding="utf-8")
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="repo.search", arguments={"query": "needle"}),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert result.success
    assert "note.txt" in result.content


def test_patch_apply_requires_file_write_enablement(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(name="patch.apply", arguments={"patch": "diff --git a/a.txt b/a.txt\n"}),
        ToolContext(memory=memory, config=AgentConfig(allow_file_write=False), workspace=tmp_path),
    )
    assert not result.success
    assert result.error == "approval_required"


def test_memory_consolidate_promotes_repeated_procedure(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.PROCEDURE,
            title="Repeatable test recipe",
            content="Repeatable test recipe: run pytest -q after tool changes.",
            confidence=0.9,
            importance=0.8,
        )
    )
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(
            name="memory.consolidate",
            arguments={
                "query": "Repeatable test recipe",
                "source_layer": "episodic",
                "validation_score": 0.9,
                "repeat_count": 2,
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )
    assert result.success
    assert '"target_layer": "procedural"' in result.content
    assert memory.retrieve(
        RetrievalQuery(query="Repeatable test recipe", layers=(MemoryLayer.PROCEDURAL,), k_per_layer=3)
    )


def test_memory_learn_routes_validated_signal(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": "Validated project fact",
                "content": "The agent stores one .mv2 file per memory layer.",
                "kind": "fact",
                "source_layer": "episodic",
                "validation_score": 0.84,
                "repeat_count": 1,
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path),
    )

    assert result.success
    assert result.data["target_layer"] == "semantic"
    hits = memory.retrieve(RetrievalQuery(query=".mv2 file per memory layer", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3))
    assert hits
    assert hits[0].record.metadata["nested_learning"]["context_flow"]["id"] == "episode_to_semantic"


def test_memory_learn_blocks_policy_without_config_enablement(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = build_default_tools()
    result = registry.execute(
        ToolCall(
            name="memory.learn",
            arguments={
                "title": "Policy candidate",
                "content": "Always require explicit review before changing policy memory.",
                "kind": "policy",
                "source_layer": "procedural",
                "target_layer": "policy",
                "validation_score": 0.99,
                "repeat_count": 5,
                "explicit_instruction": True,
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(allow_policy_writes=False), workspace=tmp_path),
    )

    assert not result.success
    assert result.error == "policy_write_disabled"
    assert not memory.retrieve(RetrievalQuery(query="explicit review before changing policy", layers=(MemoryLayer.POLICY,), k_per_layer=3))
