from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from time import monotonic, sleep

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.builtin import build_default_tools


def test_state_store_tracks_runs_and_approvals(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    run = state.create_run(
        run_id="run_test",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    assert run.status == "queued"

    approval = state.create_approval(
        approval_id="approval_test",
        run_id="run_test",
        tool_call_id="tool_1",
        tool_name="shell.run",
        arguments={"command": ["echo", "hi"]},
        risk="high",
    )
    assert approval["status"] == "pending"

    decided = state.decide_approval("approval_test", status="approved", decision={"approved": True})
    assert decided["status"] == "approved"


def test_state_store_initializes_version_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    state = AgentStateStore(db_path)

    assert state.schema_version() == 1
    with sqlite3.connect(db_path) as conn:
        run_indexes = {row[1] for row in conn.execute("PRAGMA index_list('runs')").fetchall()}
        approval_indexes = {row[1] for row in conn.execute("PRAGMA index_list('approval_requests')").fetchall()}
        step_indexes = {row[1] for row in conn.execute("PRAGMA index_list('run_steps')").fetchall()}

    assert "idx_runs_status" in run_indexes
    assert "idx_approval_requests_status" in approval_indexes
    assert "idx_run_steps_run_id_id" in step_indexes


def test_mcp_static_tools_enter_unified_registry(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    manager.add_server(
        {
            "id": "demo",
            "name": "Demo MCP",
            "transport": "stdio",
            "command": "demo",
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo test tool",
                    "parameters": {"type": "object", "properties": {"message": {"type": "string"}}},
                }
            ],
        }
    )
    synced = manager.sync_server("demo")
    assert synced["status"] == "synced"

    registry = build_default_tools()
    for adapter in manager.tool_adapters():
        registry.register(adapter)
    specs = {spec.name: spec for spec in registry.specs()}
    assert "mcp.demo.echo" in specs
    assert specs["mcp.demo.echo"].source == "mcp"


def test_skill_discovery_exposes_nested_learning_skill(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    skill_dir = tmp_path / "skills" / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": "review",
                "name": "Review Skill",
                "description": "Review changes through nested memory.",
                "risk": "medium",
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("Use episodic failures before suggesting fixes.", encoding="utf-8")

    manager = SkillManager(tmp_path / "skills", state)
    discovered = manager.discover()
    assert discovered[0]["id"] == "review"
    adapters = manager.tool_adapters()
    assert adapters[0].spec.name == "skill.review.run"
    assert adapters[0].spec.source == "skill"


def test_run_manager_completes_background_mock_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="hello", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed"
    assert "Mock response: hello" in final["assistant_message"]


def test_run_manager_publishes_stream_tokens(tmp_path: Path) -> None:
    manager = _manager(tmp_path, stream=True)
    run = manager.create_run(message="hello", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed"

    events = manager.state.list_run_steps(run.run_id)
    token_events = [event for event in events if event["type"] == "assistant.token"]
    assert token_events
    assert "Mock response: hello" in str(token_events[0]["payload"]["content"])


def test_run_manager_pauses_and_resumes_approved_tool(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.state.create_run(
        run_id="run_manual",
        message="manual",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )

    execution = manager.invoke_tool(
        tool_name="shell.run",
        arguments={"command": ["echo", "approved"]},
        session_id="session",
        run_id="run_manual",
    )
    assert execution.error == "approval_pending"
    approval = manager.state.list_approvals(status="pending")[0]

    manager.decide_approval(approval["approval_id"], approved=True, arguments=approval["arguments"])
    final = _wait_for_status(manager, "run_manual", {"completed", "failed"})
    assert final["status"] == "completed"
    assert final["tool_count"] >= 1


def test_run_manager_marks_denied_approval_failed(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.state.create_run(
        run_id="run_manual",
        message="manual",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )

    execution = manager.invoke_tool(
        tool_name="shell.run",
        arguments={"command": ["echo", "denied"]},
        session_id="session",
        run_id="run_manual",
    )
    assert execution.error == "approval_pending"
    approval = manager.state.list_approvals(status="pending")[0]

    manager.decide_approval(approval["approval_id"], approved=False, arguments=approval["arguments"])
    final = manager.get_run("run_manual")
    assert final["status"] == "failed"
    assert final["stop_reason"] == "approval_denied"
    assert final["error"] == "Approval denied"


def _manager(tmp_path: Path, *, stream: bool = False) -> RunManager:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
        stream=stream,
    )
    state = AgentStateStore(config.state_path)
    events = RunEventBus(state)
    mcp = MCPManager(state)
    skills = SkillManager(config.skills_dir, state)
    return RunManager(config=config, state=state, events=events, mcp=mcp, skills=skills)


def _wait_for_status(manager: RunManager, run_id: str, statuses: set[str]) -> dict[str, object]:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        run = manager.get_run(run_id)
        if str(run["status"]) in statuses:
            return run
        sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {statuses}")
