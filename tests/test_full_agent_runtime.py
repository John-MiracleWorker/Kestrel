from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any

import nested_memvid_agent.mcp_manager as mcp_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import AgentTurnResult
from nested_memvid_agent.server import create_app
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


def test_state_store_enforces_run_lifecycle_transitions(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_lifecycle",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )

    running = state.transition_run("run_lifecycle", "running")
    assert running.status == "running"
    blocked = state.transition_run("run_lifecycle", "blocked", stop_reason="approval_required")
    assert blocked.status == "blocked"
    resumed = state.transition_run("run_lifecycle", "running", stop_reason="resuming_after_approval")
    assert resumed.status == "running"
    cancelled = state.transition_run("run_lifecycle", "cancelled", stop_reason="cancelled")
    assert cancelled.status == "cancelled"

    blocked_after_cancel = state.transition_run("run_lifecycle", "blocked", stop_reason="approval_required")
    assert blocked_after_cancel.status == "cancelled"
    completed_after_cancel = state.transition_run("run_lifecycle", "completed", stop_reason="complete")
    assert completed_after_cancel.status == "cancelled"
    assert completed_after_cancel.stop_reason == "cancelled"


def test_state_store_lists_sessions_from_runs(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_first",
        message="first",
        session_id="session-a",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_run(
        run_id="run_second",
        message="second",
        session_id="session-a",
        workspace=str(tmp_path),
        model="mock",
    )

    sessions = state.list_sessions()

    assert sessions[0]["session_id"] == "session-a"
    assert sessions[0]["run_count"] == 2
    assert sessions[0]["latest_run_id"] == "run_second"
    assert sessions[0]["status_counts"] == {"queued": 2}


def test_state_store_persists_durable_task_graph_metadata(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_tasks",
        message="inspect repo",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )

    task = state.create_task_node(
        task_id="task_one",
        run_id="run_tasks",
        title="Inspect repo",
        goal="Map source files",
        profile="worker",
        status="queued",
        approved=False,
        dependencies=["task_root"],
        required_tools=["repo.map", "repo.search"],
        risk="medium",
        acceptance_criteria=["source map is attached", "risky tools are not used"],
        attempt_count=2,
        failure_reason="previous timeout",
    )

    assert task.dependencies == ("task_root",)
    assert task.required_tools == ("repo.map", "repo.search")
    assert task.risk == "medium"
    assert task.acceptance_criteria == ("source map is attached", "risky tools are not used")
    assert task.attempt_count == 2
    assert task.failure_reason == "previous timeout"

    updated = state.update_task_node("task_one", attempt_count=3, failure_reason="test failure")
    assert updated.attempt_count == 3
    assert updated.failure_reason == "test failure"


def test_run_event_bus_redacts_persistent_and_live_payloads(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_secret",
        message="secret",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    bus = RunEventBus(state)

    event = bus.publish(
        "run_secret",
        "provider.trace",
        {
            "authorization": "Bearer abcdefghijklmnopqrstuvwxyz",
            "env": "OPENAI_API_KEY=sk-fakeOpenAIKey123456789",
        },
    )

    stored = state.list_run_steps("run_secret")[0]["payload"]
    assert "abcdefghijklmnopqrstuvwxyz" not in json.dumps(stored)
    assert "fakeOpenAIKey" not in json.dumps(stored)
    assert event.payload == stored
    assert "<redacted>" in json.dumps(stored)


def test_state_store_initializes_version_and_indexes(tmp_path: Path) -> None:
    db_path = tmp_path / "state.db"
    state = AgentStateStore(db_path)

    assert state.schema_version() == 5
    with sqlite3.connect(db_path) as conn:
        run_indexes = {row[1] for row in conn.execute("PRAGMA index_list('runs')").fetchall()}
        approval_indexes = {row[1] for row in conn.execute("PRAGMA index_list('approval_requests')").fetchall()}
        step_indexes = {row[1] for row in conn.execute("PRAGMA index_list('run_steps')").fetchall()}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        mcp_columns = {row[1] for row in conn.execute("PRAGMA table_info('mcp_servers')").fetchall()}

    assert "idx_runs_status" in run_indexes
    assert "idx_approval_requests_status" in approval_indexes
    assert "idx_run_steps_run_id_id" in step_indexes
    assert {"task_nodes", "subagent_runs"} <= tables
    assert {
        "last_seen_at",
        "tool_count",
        "capabilities_json",
        "session_state",
        "last_call_at",
        "last_error_at",
        "failure_count",
        "last_latency_ms",
        "vetting_json",
    } <= mcp_columns


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
    assert specs["mcp.demo.echo"].risk == "medium"
    assert specs["mcp.demo.echo"].requires_approval is True


def test_mcp_vetting_metadata_identifies_secrets_network_and_high_risk_tools(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)

    row = manager.add_server(
        {
            "id": "github",
            "transport": "sse",
            "url": "https://mcp.example.test/sse",
            "env": {"GITHUB_TOKEN": "dummy-token", "LOG_LEVEL": "debug"},
            "tools": [
                {"name": "list_issues", "description": "List issues", "risk": "low"},
                {"name": "write_file", "description": "Write a file", "risk": "low"},
            ],
        }
    )

    vetting = row["vetting"]
    assert vetting["transport"] == "sse"
    assert vetting["network_access"] is True
    assert vetting["secrets_required"] == ["GITHUB_TOKEN"]
    assert vetting["recommended_trust"] == "approval_required"
    assert "network" in vetting["risk_reasons"]
    high_risk = {tool["name"]: tool for tool in vetting["tools"]}
    assert high_risk["write_file"]["risk"] == "high"
    assert high_risk["write_file"]["requires_approval"] is True
    assert row["tools"][1]["risk"] == "high"
    assert row["tools"][1]["requires_approval"] is True


def test_mcp_static_server_test_updates_health(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    manager.add_server(
        {
            "id": "static",
            "transport": "stdio",
            "tools": [{"name": "echo", "description": "Echo", "capabilities": ["test"]}],
        }
    )

    result = manager.test_server("static")
    server = result["server"]

    assert result["ok"] is True
    assert server["status"] == "online"
    assert server["session_state"] == "static"
    assert server["last_seen_at"]


def test_mcp_live_session_reuses_worker_and_tracks_calls(tmp_path: Path, monkeypatch: Any) -> None:
    factory = _FakeMCPFactory()
    monkeypatch.setattr(mcp_module, "_session_context", factory)
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    manager.add_server({"id": "live", "transport": "stdio", "command": "fake-mcp"})

    connected = manager.connect_server("live")
    assert connected["ok"] is True
    assert connected["server"]["session_state"] == "connected"
    assert connected["server"]["tools"][0]["requires_approval"] is True

    first = manager.invoke_tool("live", "echo", {"message": "one"})
    second = manager.invoke_tool("live", "mcp.live.echo", {"message": "two"})
    row = state.get_mcp_server("live")

    assert first.success is True
    assert second.success is True
    assert "echo:one" in first.content
    assert "echo:two" in second.content
    assert factory.enter_count == 1
    assert row["status"] == "online"
    assert row["session_state"] == "connected"
    assert row["last_call_at"]
    assert row["failure_count"] == 0

    disconnected = manager.disconnect_server("live")
    assert disconnected["server"]["session_state"] == "disconnected"
    assert factory.exit_count == 1


def test_mcp_config_change_tears_down_existing_session(tmp_path: Path, monkeypatch: Any) -> None:
    factory = _FakeMCPFactory()
    monkeypatch.setattr(mcp_module, "_session_context", factory)
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    manager.add_server({"id": "live", "transport": "stdio", "command": "fake-mcp"})
    assert manager.connect_server("live")["ok"] is True

    updated = manager.add_server({"id": "live", "transport": "stdio", "command": "replacement-mcp"})

    assert factory.exit_count == 1
    assert updated["session_state"] == "disconnected"
    assert updated["command"] == "replacement-mcp"


def test_mcp_live_timeout_marks_server_unhealthy(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setattr(mcp_module, "_session_context", lambda server: _SlowMCPContext())
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state, timeout_seconds=0.01)
    manager.add_server({"id": "slow", "transport": "stdio", "command": "slow-mcp"})

    result = manager.connect_server("slow")
    row = state.get_mcp_server("slow")

    assert result["ok"] is False
    assert row["status"] == "error"
    assert row["session_state"] == "error"
    assert row["failure_count"] == 1
    assert "timed out" in str(row["error"])
    manager.shutdown()


def test_server_exposes_mcp_lifecycle_routes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
    )
    client = TestClient(create_app(config))
    payload = {
        "id": "static",
        "transport": "stdio",
        "tools": [{"name": "echo", "description": "Echo", "capabilities": ["test"]}],
    }

    added = client.post("/api/mcp/servers", json=payload)
    assert added.status_code == 200
    health = client.get("/api/mcp/servers/static/health")
    assert health.status_code == 200
    assert health.json()["server"]["session_state"] == "static"
    disconnected = client.post("/api/mcp/servers/static/disconnect")
    assert disconnected.status_code == 200
    assert disconnected.json()["server"]["session_state"] == "disconnected"
    restarted = client.post("/api/mcp/servers/static/restart")
    assert restarted.status_code == 200
    assert restarted.json()["server"]["session_state"] == "static"


def test_server_exposes_prompt_api_routes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    memory_dir = tmp_path / "memory"
    memory = build_memory_system("memory", memory_dir)
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            title="API search fact",
            content="Compiled context API routes should expose retrieved memory.",
            confidence=0.8,
        )
    )
    memory.seal_all()
    memory.close_all()

    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=memory_dir,
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
    )
    client = TestClient(create_app(config))

    run = client.post("/api/runs", json={"message": "hello", "session_id": "api-session"})
    assert run.status_code == 200
    sessions = client.get("/api/sessions")
    assert sessions.status_code == 200
    assert sessions.json()[0]["session_id"] == "api-session"

    search = client.get("/api/memory/search", params={"query": "compiled context api"})
    assert search.status_code == 200
    assert search.json()[0]["title"] == "API search fact"

    context = client.get("/api/context", params={"query": "compiled context api", "token_budget": 1200})
    assert context.status_code == 200
    assert "MV2 PSEUDO-CONTEXT PACK" in context.json()["packed_prompt"]
    assert context.json()["selected_item_count"] >= 1


def test_server_exposes_observability_routes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
    )
    client = TestClient(create_app(config))

    created = client.post("/api/runs", json={"message": "observe this", "session_id": "observability"})
    assert created.status_code == 200
    run_id = created.json()["run_id"]
    final = _wait_for_client_status(client, run_id, {"completed", "failed"})
    assert final["status"] == "completed"

    trace = client.get(f"/api/runs/{run_id}/trace", params={"limit": 200})
    assert trace.status_code == 200
    trace_payload = trace.json()
    assert trace_payload["summary"]["trace_counts"]["context"] >= 1
    assert trace_payload["summary"]["trace_counts"]["memory"] >= 1
    assert any(event["type"] == "memory.write" for event in trace_payload["traces"]["memory"])

    logs = client.get("/api/logs", params={"limit": 50})
    assert logs.status_code == 200
    log_types = {event["type"] for event in logs.json()}
    assert "turn.start" in log_types
    assert "memory.write" in log_types


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
    disabled = manager.set_enabled("review", False)
    assert disabled["enabled"] is False


def test_cancelled_run_cannot_be_overwritten_completed_after_agent_returns(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.state.create_run(
        run_id="run_cancel_race",
        message="cancel me",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )

    class CancellingAgent:
        memory = None
        config = manager.config

        def chat(self, *args: object, **kwargs: object) -> AgentTurnResult:
            manager.cancel_run("run_cancel_race")
            return AgentTurnResult(
                session_id="session",
                user_message="cancel me",
                assistant_message="done after cancel",
                tool_executions=(),
                context_chars=0,
                memory_writes=(),
                stop_reason="complete",
            )

        def close(self) -> None:
            return None

    manager._build_agent = lambda config: CancellingAgent()  # type: ignore[method-assign]

    manager._run_agent_turn("run_cancel_race", manager.config, "cancel me", "session")

    final = manager.get_run("run_cancel_race")
    assert final["status"] == "cancelled"
    assert final["stop_reason"] == "cancelled"


def test_run_manager_completes_background_mock_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="hello", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed"
    assert "Mock response: hello" in final["assistant_message"]
    graph = manager.task_graph(run.run_id)
    assert graph["tasks"]
    assert graph["tasks"][0]["title"] == "Root objective"


def test_run_manager_creates_durable_child_plan_for_multi_step_goal(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="Inspect the repo and run targeted tests", session_id="session")

    graph = manager.task_graph(run.run_id)
    tasks = graph["tasks"]

    assert len(tasks) >= 3
    root_id = tasks[0]["task_id"]
    child_tasks = tasks[1:]
    assert {task["parent_id"] for task in child_tasks} == {root_id}
    assert child_tasks[0]["dependencies"] == []
    assert child_tasks[1]["dependencies"] == [child_tasks[0]["task_id"]]
    assert child_tasks[0]["acceptance_criteria"]
    assert child_tasks[1]["required_tools"]


def test_run_manager_trace_includes_context_memory_and_tool_events(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="hello", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed"

    execution = manager.invoke_tool(
        tool_name="memory.search",
        arguments={"query": "hello", "k": 1},
        session_id="session",
        run_id=run.run_id,
    )
    assert execution.success is True

    trace = manager.run_trace(run.run_id)

    assert trace["summary"]["trace_counts"]["context"] >= 1
    assert trace["summary"]["trace_counts"]["memory"] >= 1
    assert trace["summary"]["trace_counts"]["tool"] >= 1
    assert any(event["type"] == "memory.write" for event in trace["traces"]["memory"])
    assert any(event["type"] == "tool.completed" for event in trace["traces"]["tool"])


def test_run_manager_runs_mock_subagent(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="main run", session_id="session")
    subagent = manager.create_subagent(run_id=run.run_id, profile="reviewer", goal="Review the mock output.")
    final = _wait_for_subagent(manager, run.run_id, str(subagent["subagent_id"]), {"completed", "failed"})

    assert final["status"] == "completed"
    assert "Mock response" in str(final["result"])
    graph = manager.task_graph(run.run_id)
    assert graph["subagents"]


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
    manager.config = AgentConfig(
        **{**manager.config.__dict__, "allow_shell": True}
    )
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
    manager.config = AgentConfig(
        **{**manager.config.__dict__, "allow_shell": True}
    )
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


class _FakeMCPSession:
    async def list_tools(self) -> Any:
        return SimpleNamespace(
            tools=[
                SimpleNamespace(
                    name="echo",
                    description="Echo test tool",
                    inputSchema={"type": "object", "properties": {"message": {"type": "string"}}},
                )
            ]
        )

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        return SimpleNamespace(content=[SimpleNamespace(text=f"{tool_name}:{arguments.get('message', '')}")])


class _FakeMCPContext:
    def __init__(self, factory: _FakeMCPFactory) -> None:
        self.factory = factory
        self.session = _FakeMCPSession()

    async def __aenter__(self) -> _FakeMCPSession:
        self.factory.enter_count += 1
        return self.session

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.factory.exit_count += 1


class _FakeMCPFactory:
    def __init__(self) -> None:
        self.enter_count = 0
        self.exit_count = 0

    def __call__(self, server: object) -> _FakeMCPContext:
        del server
        return _FakeMCPContext(self)


class _SlowMCPSession:
    async def list_tools(self) -> Any:
        await asyncio.sleep(1)
        return SimpleNamespace(tools=[])


class _SlowMCPContext:
    async def __aenter__(self) -> _SlowMCPSession:
        return _SlowMCPSession()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


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


def _wait_for_client_status(client: Any, run_id: str, statuses: set[str]) -> dict[str, object]:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        response = client.get(f"/api/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if str(run["status"]) in statuses:
            return dict(run)
        sleep(0.05)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def _wait_for_subagent(manager: RunManager, run_id: str, subagent_id: str, statuses: set[str]) -> dict[str, object]:
    deadline = monotonic() + 5
    while monotonic() < deadline:
        graph = manager.task_graph(run_id)
        for subagent in graph["subagents"]:
            if str(subagent["subagent_id"]) == subagent_id and str(subagent["status"]) in statuses:
                return subagent
        sleep(0.05)
    raise AssertionError(f"subagent {subagent_id} did not reach {statuses}")
