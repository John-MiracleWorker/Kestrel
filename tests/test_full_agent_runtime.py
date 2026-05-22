from __future__ import annotations

import asyncio
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any

import pytest

import nested_memvid_agent.mcp_manager as mcp_module
from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.event_log import JsonlEventLog
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import AgentTurnResult, LLMResponse, ToolCall
from nested_memvid_agent.server import create_app
from nested_memvid_agent.skill_manager import SkillManager, validate_skill_manifest
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import ToolContext
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


def test_state_store_approval_decisions_are_immutable_after_first_decision(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_approval_once",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_approval(
        approval_id="approval_once",
        run_id="run_approval_once",
        tool_call_id="tool_shell",
        tool_name="shell.run",
        arguments={"command": ["echo", "hi"]},
        risk="high",
    )

    approved = state.decide_approval("approval_once", status="approved", decision={"approved": True})
    replayed_denial = state.decide_approval("approval_once", status="denied", decision={"approved": False})

    assert approved["status"] == "approved"
    assert replayed_denial["status"] == "approved"
    assert replayed_denial["decision"] == {"approved": True}


def test_state_store_records_approval_result_without_flipping_decision(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_approval(
        approval_id="approval_test",
        run_id="run_test",
        tool_call_id="call_test",
        tool_name="test.run",
        arguments={"command": ["python3", "-c", "print('ok')"]},
        risk="high",
    )

    decided = state.decide_approval("approval_test", status="approved", decision={"approved": True})
    assert decided["result"] is None

    recorded = state.record_approval_result("approval_test", {"success": True, "content": "ok"})
    assert recorded["status"] == "approved"
    assert recorded["decision"] == {"approved": True}
    assert recorded["result"] == {"success": True, "content": "ok"}

    replay = state.record_approval_result("approval_test", {"success": False, "content": "late failure"})
    assert replay["result"] == {"success": True, "content": "ok"}


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


def test_state_store_terminal_run_states_are_immutable_even_for_same_status(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    for terminal_status in ("completed", "failed", "cancelled"):
        run_id = f"run_{terminal_status}"
        state.create_run(
            run_id=run_id,
            message="hello",
            session_id="session",
            workspace=str(tmp_path),
            model="mock",
        )
        if terminal_status == "completed":
            state.transition_run(run_id, "running")
            terminal = state.transition_run(run_id, terminal_status, stop_reason="original", assistant_message="original message")
        else:
            terminal = state.transition_run(run_id, terminal_status, stop_reason="original", error="original error")
        assert terminal.status == terminal_status

        late_same_status = state.transition_run(
            run_id,
            terminal_status,
            stop_reason="late overwrite",
            assistant_message="late message",
            error="late error",
        )

        assert late_same_status.status == terminal_status
        assert late_same_status.stop_reason == "original"
        assert late_same_status.assistant_message != "late message"
        assert late_same_status.error != "late error"


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


def test_state_store_lists_runs_for_session_chronologically(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_a_first",
        message="first alpha",
        session_id="session-a",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_run(
        run_id="run_b",
        message="beta",
        session_id="session-b",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_run(
        run_id="run_a_second",
        message="second alpha",
        session_id="session-a",
        workspace=str(tmp_path),
        model="mock",
    )

    runs = state.list_runs_for_session("session-a")

    assert [run.run_id for run in runs] == ["run_a_first", "run_a_second"]
    assert [run.session_id for run in runs] == ["session-a", "session-a"]


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


def test_state_store_persists_task_diagnosis_and_retry_strategy(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_retry",
        message="fix failing tests",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_task_node(
        task_id="task_retry",
        run_id="run_retry",
        title="Validate repair",
        goal="Run targeted validation",
        profile="reviewer",
        status="running",
    )

    failed = state.record_task_failure(
        "task_retry",
        failure_reason="pytest failed",
        diagnosis={"classification": "test_failure", "confidence": 0.9},
        retry_strategy={
            "previous_command": "pytest tests/test_widget.py -q",
            "next_command": "pytest tests/test_widget.py::test_edge -q",
            "changed_strategy": "narrow to failing edge case",
            "retry_allowed": True,
        },
    )

    assert failed.status == "failed"
    assert failed.attempt_count == 1
    assert failed.failure_reason == "pytest failed"
    assert failed.diagnosis == {"classification": "test_failure", "confidence": 0.9}
    assert failed.retry_strategy["previous_command"] == "pytest tests/test_widget.py -q"
    assert failed.retry_strategy["changed_strategy"] == "narrow to failing edge case"


def test_state_store_record_task_failure_increments_attempts_and_preserves_latest_strategy(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_retry_twice",
        message="fix failing tests",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    state.create_task_node(
        task_id="task_retry_twice",
        run_id="run_retry_twice",
        title="Validate repair",
        goal="Run targeted validation",
        profile="reviewer",
        status="running",
        attempt_count=2,
    )

    failed = state.record_task_failure(
        "task_retry_twice",
        failure_reason="pytest still failed",
        diagnosis={"classification": "test_failure"},
        retry_strategy={"changed_strategy": "inspect fixture setup", "retry_allowed": False},
    )

    assert failed.attempt_count == 3
    assert failed.diagnosis == {"classification": "test_failure"}
    assert failed.retry_strategy == {"changed_strategy": "inspect fixture setup", "retry_allowed": False}


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

    assert state.schema_version() == 11
    with sqlite3.connect(db_path) as conn:
        run_indexes = {row[1] for row in conn.execute("PRAGMA index_list('runs')").fetchall()}
        approval_indexes = {row[1] for row in conn.execute("PRAGMA index_list('approval_requests')").fetchall()}
        step_indexes = {row[1] for row in conn.execute("PRAGMA index_list('run_steps')").fetchall()}
        promotion_indexes = {row[1] for row in conn.execute("PRAGMA index_list('promotion_ledger')").fetchall()}
        outcome_indexes = {row[1] for row in conn.execute("PRAGMA index_list('promotion_outcomes')").fetchall()}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        mcp_columns = {row[1] for row in conn.execute("PRAGMA table_info('mcp_servers')").fetchall()}
        task_columns = {row[1] for row in conn.execute("PRAGMA table_info('task_nodes')").fetchall()}
        span_columns = {row[1] for row in conn.execute("PRAGMA table_info('trace_spans')").fetchall()}

    assert "idx_runs_status" in run_indexes
    assert "idx_approval_requests_status" in approval_indexes
    assert "idx_run_steps_run_id_id" in step_indexes
    assert "idx_promotion_ledger_target_layer" in promotion_indexes
    assert "idx_promotion_outcomes_promotion_id" in outcome_indexes
    assert {"task_nodes", "subagent_runs", "plugin_registry", "trace_spans", "promotion_ledger", "promotion_outcomes"} <= tables
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
    assert {"diagnosis_json", "retry_strategy_json"} <= task_columns
    assert {"span_type", "parent_span_id", "metadata_json", "output_json", "started_at", "ended_at"} <= span_columns


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
    manager = MCPManager(state, allow_network_endpoints=True)

    row = manager.add_server(
        {
            "id": "github",
            "transport": "sse",
            "url": "https://mcp.example.test/sse",
            "env": {"LOG_LEVEL": "debug"},
            "secret_env": {"GITHUB_TOKEN": "GITHUB_TOKEN"},
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


def test_mcp_env_rejects_raw_secret_keys(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)

    with pytest.raises(ValueError, match="secret_env"):
        manager.add_server(
            {
                "id": "raw-secret",
                "transport": "stdio",
                "command": "fake-mcp",
                "env": {"GITHUB_TOKEN": "raw-token"},
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/socket",
        "https://user:pass@mcp.example.test/sse",
        "https://169.254.169.254/latest/meta-data",
    ],
)
def test_mcp_network_endpoint_validation_rejects_unsafe_urls(tmp_path: Path, url: str) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state, allow_network_endpoints=True)

    with pytest.raises(ValueError):
        manager.add_server({"id": "unsafe-url", "transport": "sse", "url": url})


def test_mcp_stdio_validation_rejects_shell_launchers(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)

    with pytest.raises(ValueError):
        manager.add_server({"id": "shell", "transport": "stdio", "command": "/bin/sh", "args": ["-c", "echo unsafe"]})


def test_mcp_trusted_flags_do_not_bypass_approval_by_default(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)

    row = manager.add_server(
        {
            "id": "trusted",
            "transport": "stdio",
            "tools": [
                {
                    "name": "read_safe",
                    "description": "Read safe data.",
                    "risk": "low",
                    "requires_approval": False,
                    "trusted": True,
                    "allow_autonomous": True,
                }
            ],
        }
    )

    assert row["tools"][0]["risk"] == "medium"
    assert row["tools"][0]["requires_approval"] is True


def test_mcp_secret_env_is_resolved_only_at_runtime(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("MCP_FIXTURE_TOKEN", "super-secret-token")
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    row = manager.add_server(
        {
            "id": "secret-stdio",
            "transport": "stdio",
            "command": "fake-mcp",
            "env": {"LOG_LEVEL": "debug"},
            "secret_env": {"MCP_API_TOKEN": "MCP_FIXTURE_TOKEN"},
        }
    )
    server = mcp_module._server_from_state(row)

    runtime_env = mcp_module._runtime_env(server)

    assert runtime_env["LOG_LEVEL"] == "debug"
    assert runtime_env["MCP_API_TOKEN"] == "super-secret-token"
    assert "super-secret-token" not in json.dumps(row)
    assert row["secret_env"] == {"MCP_API_TOKEN": "MCP_FIXTURE_TOKEN"}


def test_mcp_secret_ref_is_resolved_by_broker_only_at_runtime(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state, secret_resolver=lambda ref: "broker-secret-token" if ref == "secret://github_pat" else None)
    row = manager.add_server(
        {
            "id": "broker-secret-stdio",
            "transport": "stdio",
            "command": "fake-mcp",
            "secret_env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "secret://github_pat"},
        }
    )
    server = mcp_module._server_from_state(row)

    runtime_env = mcp_module._runtime_env(server, secret_resolver=manager.secret_resolver)

    assert runtime_env["GITHUB_PERSONAL_ACCESS_TOKEN"] == "broker-secret-token"
    assert "broker-secret-token" not in json.dumps(row)
    assert row["secret_env"] == {"GITHUB_PERSONAL_ACCESS_TOKEN": "secret://github_pat"}


def test_secret_broker_api_returns_metadata_only_for_channels_and_mcp(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    config = AgentConfig(
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        channel_config_path=tmp_path / "channels.json",
        secret_store_path=tmp_path / "secrets.json",
    )
    client = TestClient(create_app(config))

    created = client.post(
        "/api/secrets",
        json={
            "name": "TELEGRAM_BOT_TOKEN",
            "purpose": "Enable Telegram channel delivery.",
            "value": "123456:ABC-super-secret",
            "validate": True,
        },
    )
    assert created.status_code == 200
    secret_payload = created.json()
    assert secret_payload["secret_ref"] == "secret://telegram_bot_token"
    assert secret_payload["configured"] is True
    assert secret_payload["validated"] is True
    assert "123456:ABC-super-secret" not in json.dumps(secret_payload)

    secrets = client.get("/api/secrets")
    assert secrets.status_code == 200
    assert "123456:ABC-super-secret" not in json.dumps(secrets.json())

    channel = client.post(
        "/api/channels",
        json={"id": "telegram", "provider": "telegram", "token_env": "TELEGRAM_BOT_TOKEN"},
    )
    assert channel.status_code == 200
    assert channel.json()["env_status"]["token_env_configured"] is True
    assert "123456:ABC-super-secret" not in json.dumps(channel.json())

    mcp_payload = {
        "id": "broker-mcp",
        "transport": "stdio",
        "command": "fake-mcp",
        "secret_env": {"MCP_API_TOKEN": "secret://telegram_bot_token"},
    }
    mcp_created = client.post("/api/mcp/servers", json=mcp_payload)
    assert mcp_created.status_code == 200
    mcp_detail = client.get("/api/mcp/servers/broker-mcp")
    assert mcp_detail.status_code == 200
    payload = mcp_detail.json()
    assert "secret_env" not in payload
    assert payload["secret_env_status"]["MCP_API_TOKEN"]["configured"] is True
    assert payload["secret_env_status"]["MCP_API_TOKEN"]["secret_ref"] == "secret://telegram_bot_token"
    assert "123456:ABC-super-secret" not in json.dumps(payload)


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
    assert health.json()["server"]["session_state"] == "disconnected"
    checked = client.post("/api/mcp/servers/static/test")
    assert checked.status_code == 200
    assert checked.json()["server"]["session_state"] == "static"
    disconnected = client.post("/api/mcp/servers/static/disconnect")
    assert disconnected.status_code == 200
    assert disconnected.json()["server"]["session_state"] == "disconnected"
    restarted = client.post("/api/mcp/servers/static/restart")
    assert restarted.status_code == 200
    assert restarted.json()["server"]["session_state"] == "static"


def test_server_exposes_mcp_connect_approval_route(tmp_path: Path) -> None:
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
    state = AgentStateStore(config.state_path)
    state.upsert_mcp_server(
        {
            "id": "approval-static",
            "name": "Approval Static",
            "transport": "stdio",
            "command": "fake-mcp",
            "enabled": True,
            "tools": [{"name": "echo", "description": "Echo"}],
            "vetting": {"connect_requires_approval": True},
        }
    )
    client = TestClient(create_app(config))

    blocked = client.post("/api/mcp/servers/approval-static/connect")
    approved = client.post("/api/mcp/servers/approval-static/approve-connect")
    connected = client.post("/api/mcp/servers/approval-static/connect")

    assert blocked.status_code == 200
    assert blocked.json()["ok"] is False
    assert blocked.json()["server"]["session_state"] == "approval_required"
    assert approved.status_code == 200
    assert approved.json()["vetting"]["connect_approved"] is True
    assert connected.status_code == 200
    assert connected.json()["ok"] is True
    assert connected.json()["server"]["session_state"] == "static"


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


def test_server_lists_runs_for_a_session_in_chronological_order(tmp_path: Path) -> None:
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

    first = client.post("/api/runs", json={"message": "first alpha", "session_id": "session-a"})
    other = client.post("/api/runs", json={"message": "beta", "session_id": "session-b"})
    second = client.post("/api/runs", json={"message": "second alpha", "session_id": "session-a"})
    assert first.status_code == 200
    assert other.status_code == 200
    assert second.status_code == 200

    session_runs = client.get("/api/sessions/session-a/runs")
    assert session_runs.status_code == 200
    assert [run["run_id"] for run in session_runs.json()] == [first.json()["run_id"], second.json()["run_id"]]
    assert [run["message"] for run in session_runs.json()] == ["first alpha", "second alpha"]

    empty_session = client.get("/api/sessions/missing/runs")
    assert empty_session.status_code == 200
    assert empty_session.json() == []

    global_runs = client.get("/api/runs")
    assert global_runs.status_code == 200
    assert {run["run_id"] for run in global_runs.json()} >= {
        first.json()["run_id"],
        other.json()["run_id"],
        second.json()["run_id"],
    }


def test_server_exposes_self_and_web_routes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        workspace=tmp_path,
        allow_web=True,
        web_backend="mock",
    )
    client = TestClient(create_app(config))

    inspected = client.get("/api/self")
    assert inspected.status_code == 200
    assert inspected.json()["identity"]["display_name"] == "Soul"
    assert "self" in {layer["layer"] for layer in inspected.json()["memory_layers"]}

    remembered = client.post(
        "/api/self/remember",
        json={
            "title": "API user preference",
            "content": "The user wants visible self-awareness controls.",
            "schema": "user_workflow_preference",
            "validation_status": "user_confirmed",
            "confidence": 0.9,
        },
    )
    assert remembered.status_code == 200
    assert remembered.json()["success"] is True

    onboarding_before = client.get("/api/self/onboarding")
    assert onboarding_before.status_code == 200
    assert onboarding_before.json()["completed"] is False
    assert {persona["id"] for persona in onboarding_before.json()["personas"]} >= {"steady", "mentor", "spark", "operator"}

    onboarded = client.post(
        "/api/self/onboarding",
        json={
            "agent_name": "Northstar",
            "user_name": "Taylor",
            "preferred_name": "Tay",
            "persona": "spark",
            "working_style": "Prefer short plans before edits.",
            "goals": ["build a local-first agent"],
            "interests": ["interface craft"],
            "communication_notes": "Keep it warm but concrete.",
        },
    )
    assert onboarded.status_code == 200
    assert onboarded.json()["success"] is True
    assert onboarded.json()["profile"]["agent_name"] == "Northstar"
    assert onboarded.json()["profile"]["preferred_name"] == "Tay"
    assert onboarded.json()["memory"]["success"] is True

    onboarding_after = client.get("/api/self/onboarding")
    assert onboarding_after.status_code == 200
    assert onboarding_after.json()["completed"] is True
    assert onboarding_after.json()["profile"]["persona"] == "spark"

    proposed = client.post("/api/self/propose-change", json={"request": "Rewrite Kestrel without approval."})
    assert proposed.status_code == 200
    assert proposed.json()["success"] is False
    assert proposed.json()["error"] == "tool_disabled"

    searched = client.post("/api/web/search", json={"query": "kestrel soul"})
    assert searched.status_code == 200
    assert searched.json()["success"] is True
    assert searched.json()["data"]["results"][0]["url"].startswith("https://mock.kestrel.local/search/")

    fetched = client.post("/api/web/fetch", json={"url": searched.json()["data"]["results"][0]["url"]})
    assert fetched.status_code == 200
    assert fetched.json()["success"] is True

    unsafe = client.post("/api/web/fetch", json={"url": "http://169.254.169.254/latest/meta-data"})
    assert unsafe.status_code == 200
    assert unsafe.json()["error"] == "unsafe_url"


def test_server_exposes_local_operator_api_parity(tmp_path: Path, monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("KESTREL_OPERATOR_TEST_KEY", "secret-token")
    memory_dir = tmp_path / "memory"
    memory = build_memory_system("memory", memory_dir)
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.PROCEDURAL,
            kind=MemoryKind.PROCEDURE,
            title="LessonCard: pytest import layout",
            content=json.dumps({"id": "lesson_ui", "corrected_strategy": "Check PYTHONPATH first."}),
            confidence=0.84,
            metadata={"cognition_schema": "lesson_card.v1"},
        )
    )
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.EPISODIC,
            kind=MemoryKind.FAILURE,
            title="FailureEpisode: test failure",
            content=json.dumps({"failure_id": "failure_ui", "diagnosis": "Focused test failed."}),
            confidence=0.76,
            metadata={"cognition_schema": "failure_episode.v1"},
        )
    )
    memory.seal_all()
    memory.close_all()

    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        api_key_env="KESTREL_OPERATOR_TEST_KEY",
        memory_dir=memory_dir,
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        workspace=tmp_path,
        channel_config_path=tmp_path / "channels.json",
    )
    state = AgentStateStore(config.state_path)
    state.upsert_plugin(
        {
            "id": "plugin_local",
            "name": "Local Plugin",
            "description": "Local plugin row",
            "source_url": "https://github.com/example/plugin",
            "commit_sha": "abc1234",
            "install_path": str(tmp_path / "plugins" / "plugin_local"),
            "manifest": {"id": "plugin_local", "skills": [], "mcp_servers": []},
            "capabilities": ["skill"],
            "enabled": False,
            "risk_report": {"risk": "medium"},
            "install_status": "installed",
            "format": "kestrel",
        }
    )
    skill_dir = config.skills_dir / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.json").write_text(
        json.dumps({"id": "review", "name": "Review", "description": "Review skill", "risk": "medium"}),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("Review with memory.", encoding="utf-8")

    client = TestClient(create_app(config))

    runtime = client.get("/api/runtime/config")
    assert runtime.status_code == 200
    runtime_payload = runtime.json()
    assert runtime_payload["provider"]["api_key_env"] == "KESTREL_OPERATOR_TEST_KEY"
    assert runtime_payload["provider"]["api_key_configured"] is True
    assert "secret-token" not in json.dumps(runtime_payload)

    run = client.post(
        "/api/runs",
        json={"message": "operator run", "provider": "mock", "model": "ui-model", "autonomy_mode": "manual"},
    )
    assert run.status_code == 200
    assert run.json()["provider"] == "mock"
    assert run.json()["model"] == "ui-model"
    run_id = run.json()["run_id"]

    task = state.create_task_node(
        task_id="task_operator_review",
        run_id=run_id,
        title="Operator review",
        goal="Wait for human approval.",
        profile="reviewer",
        approved=False,
        risk="medium",
    )
    graph = client.get(f"/api/runs/{run_id}/task-graph")
    assert graph.status_code == 200
    assert any(item["task_id"] == task.task_id for item in graph.json()["approval_blocked_tasks"])
    approved = client.post(f"/api/runs/{run_id}/approve-task", json={"task_id": task.task_id})
    assert approved.status_code == 200
    assert approved.json()["approved"] is True

    channel = client.post(
        "/api/channels",
        json={
            "id": "signed-webhook",
            "provider": "webhook",
            "settings": {"signature_secret_env": "KESTREL_WEBHOOK_SECRET"},
        },
    )
    assert channel.status_code == 200
    assert channel.json()["env_status"]["signature_secret_env"] == "KESTREL_WEBHOOK_SECRET"
    updated_channel = client.put(
        "/api/channels/signed-webhook",
        json={"id": "ignored", "provider": "webhook", "enabled": False},
    )
    assert updated_channel.status_code == 200
    assert updated_channel.json()["id"] == "signed-webhook"
    assert updated_channel.json()["enabled"] is False

    mcp_payload = {
        "id": "operator-static",
        "transport": "stdio",
        "tools": [{"name": "echo", "description": "Echo"}],
        "secret_env": {"MCP_API_KEY": "MCP_API_KEY"},
    }
    assert client.post("/api/mcp/servers", json=mcp_payload).status_code == 200
    mcp_detail = client.get("/api/mcp/servers/operator-static")
    assert mcp_detail.status_code == 200
    assert "secret_env" not in mcp_detail.json()
    assert mcp_detail.json()["secret_env_status"]["MCP_API_KEY"]["configured"] is False
    mcp_updated = client.put("/api/mcp/servers/operator-static", json={**mcp_payload, "name": "Operator Static"})
    assert mcp_updated.status_code == 200
    assert mcp_updated.json()["name"] == "Operator Static"

    discovered = client.post("/api/skills/discover")
    assert discovered.status_code == 200
    assert discovered.json()["discovered_count"] == 1
    assert discovered.json()["enabled_count"] == 1
    assert discovered.json()["skills_dir"] == str(config.skills_dir)
    assert discovered.json()["validation_errors"] == []
    skill = client.get("/api/skills/review")
    assert skill.status_code == 200
    assert skill.json()["manifest"]["validation"]["ok"] is True
    plugin = client.get("/api/plugins/plugin_local")
    assert plugin.status_code == 200
    assert plugin.json()["id"] == "plugin_local"

    lessons = client.get("/api/cognition/lessons")
    failures = client.get("/api/cognition/failures")
    assert lessons.status_code == 200
    assert failures.status_code == 200
    assert lessons.json()["items"][0]["record"]["metadata"]["cognition_schema"] == "lesson_card.v1"
    assert failures.json()["items"][0]["record"]["metadata"]["cognition_schema"] == "failure_episode.v1"

    diagnosis = client.post(
        "/api/diagnosis/classify",
        json={"failure_text": "ModuleNotFoundError: No module named nested_memvid_agent", "source": "pytest"},
    )
    assert diagnosis.status_code == 200
    assert diagnosis.json()["classification"] in {"missing_dependency", "import_error", "unknown"}


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


def test_server_api_auth_requires_configured_token(tmp_path: Path, monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("KESTREL_TEST_API_TOKEN", "secret-token")
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
        require_api_auth=True,
        api_auth_token_env="KESTREL_TEST_API_TOKEN",
    )
    client = TestClient(create_app(config))

    web_dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    if web_dist.exists():
        index = client.get("/")
        assert index.status_code == 200
        assert "text/html" in index.headers["content-type"]
        asset = next((web_dist / "assets").iterdir(), None)
        if asset is not None:
            assert client.get(f"/assets/{asset.name}").status_code == 200
    assert client.get("/api/health").status_code == 401
    authorized = client.get("/api/health", headers={"Authorization": "Bearer secret-token"})
    assert authorized.status_code == 200
    assert authorized.json()["ok"] is True
    assert client.get("/api/does-not-exist", headers={"Authorization": "Bearer secret-token"}).status_code == 404


def test_api_plugin_install_enable_update_require_plugin_install_flag(tmp_path: Path, monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    plugin_repo = tmp_path / "plugin-repo"
    plugin_repo.mkdir()
    (plugin_repo / "kestrel.plugin.json").write_text(
        json.dumps(
            {
                "id": "api-blocked",
                "name": "API Blocked",
                "description": "Must not install without plugin enablement.",
                "skills": [{"id": "hello", "description": "Hello.", "instructions": "Hello."}],
            }
        ),
        encoding="utf-8",
    )

    def fake_fetch(self: object, source: object, destination: Path, ref: str | None = None) -> str:
        del self, source, ref
        shutil.copytree(plugin_repo, destination)
        return "e" * 40

    monkeypatch.setattr("nested_memvid_agent.plugin_manager.GitPluginFetcher.fetch", fake_fetch)
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        workspace=tmp_path,
        allow_plugin_install=False,
    )
    state = AgentStateStore(config.state_path)
    state.upsert_plugin(
        {
            "id": "existing",
            "name": "Existing",
            "description": "Existing plugin.",
            "source_url": "https://github.com/owner/repo",
            "commit_sha": "a" * 40,
            "install_path": str(tmp_path / "plugins" / "existing"),
            "manifest": {"id": "existing", "skills": [], "mcp_servers": []},
            "capabilities": ["plugin"],
            "enabled": False,
            "risk_report": {"risk": "medium"},
            "install_status": "installed",
            "format": "kestrel",
        }
    )
    client = TestClient(create_app(config))

    install = client.post("/api/plugins/install", json={"source": "owner/repo"})
    enable = client.post("/api/plugins/existing/enable")
    update = client.post("/api/plugins/existing/update", json={})

    assert install.status_code == 403
    assert install.json()["detail"] == "plugin_install_disabled"
    assert enable.status_code == 403
    assert update.status_code == 403
    assert not (tmp_path / "plugins" / "api-blocked").exists()
    assert state.get_plugin("existing")["enabled"] is False


def test_api_plugin_review_and_enable_blockers(tmp_path: Path, monkeypatch: Any) -> None:
    from fastapi.testclient import TestClient

    plugin_repo = tmp_path / "plugin-repo"
    plugin_repo.mkdir()
    (plugin_repo / "kestrel.plugin.json").write_text(
        json.dumps(
            {
                "id": "reviewed",
                "name": "Reviewed Plugin",
                "description": "Review before install.",
                "dependencies": {"python": ["requests>=2"]},
                "isolation": {"mode": "container", "required": True},
                "skills": [{"id": "hello", "description": "Hello.", "instructions": "Hello."}],
            }
        ),
        encoding="utf-8",
    )

    def fake_fetch(self: object, source: object, destination: Path, ref: str | None = None) -> str:
        del self, source, ref
        shutil.copytree(plugin_repo, destination)
        return "e" * 40

    monkeypatch.setattr("nested_memvid_agent.plugin_manager.GitPluginFetcher.fetch", fake_fetch)
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        workspace=tmp_path,
        allow_plugin_install=True,
    )
    client = TestClient(create_app(config))

    review = client.post("/api/plugins/review", json={"source": "owner/repo", "ref": "main"})

    assert review.status_code == 200
    assert review.json()["source_ref"] == "main"
    assert review.json()["dependency_review"]["declared"]["python"] == ["requests>=2"]
    assert review.json()["enable_blockers"] == [
        "plugin_dependencies_unmanaged",
        "plugin_isolation_unavailable",
    ]
    with pytest.raises(KeyError):
        AgentStateStore(config.state_path).get_plugin("reviewed")

    installed = client.post("/api/plugins/install", json={"source": "owner/repo"})
    assert installed.status_code == 200
    assert installed.json()["enabled"] is False

    enabled = client.post("/api/plugins/reviewed/enable")
    assert enabled.status_code == 400
    assert "enable blocked" in enabled.json()["detail"]


def test_api_mcp_invoke_uses_unified_approval_gate(tmp_path: Path) -> None:
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
    add = client.post(
        "/api/mcp/servers",
        json={
            "id": "static",
            "transport": "stdio",
            "enabled": True,
            "tools": [{"name": "echo", "description": "Echo", "parameters": {"type": "object", "properties": {}}}],
        },
    )
    assert add.status_code == 200

    invoked = client.post("/api/mcp/servers/static/tools/echo/invoke", json={"arguments": {"message": "hello"}})

    assert invoked.status_code == 200
    assert invoked.json()["success"] is False
    assert invoked.json()["error"] == "approval_required"


def test_get_plugin_routes_do_not_materialize_extensions(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    install_path = tmp_path / "plugins" / "readonly"
    install_path.mkdir(parents=True)
    state = AgentStateStore(tmp_path / "state.db")
    state.upsert_plugin(
        {
            "id": "readonly",
            "name": "Readonly",
            "description": "Readonly plugin.",
            "source_url": "https://github.com/owner/readonly",
            "commit_sha": "f" * 40,
            "install_path": str(install_path),
            "manifest": {
                "id": "readonly",
                "skills": [
                    {
                        "id": "hello",
                        "namespaced_id": "plugin.readonly.hello",
                        "name": "Hello",
                        "description": "Hello.",
                        "enabled": True,
                        "manifest": {"id": "plugin.readonly.hello", "description": "Hello.", "runtime": {"type": "instruction"}},
                        "instructions": "Hello.",
                    }
                ],
                "mcp_servers": [],
            },
            "capabilities": ["skill"],
            "enabled": True,
            "risk_report": {"risk": "medium"},
            "install_status": "installed",
            "format": "kestrel",
        }
    )
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        workspace=tmp_path,
    )
    client = TestClient(create_app(config))

    assert client.get("/api/plugins").status_code == 200
    assert client.get("/api/plugins/readonly").status_code == 200

    with pytest.raises(KeyError):
        state.get_skill("plugin.readonly.hello")


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


def test_skill_manifest_validation_records_provenance_and_rejects_invalid_skill(tmp_path: Path) -> None:
    valid_dir = tmp_path / "skills" / "safe"
    valid_dir.mkdir(parents=True)
    (valid_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": "safe",
                "name": "Safe Skill",
                "description": "Safe instruction-only skill.",
                "version": "1.0.0",
                "risk": "low",
                "capabilities": ["skill"],
                "permissions": [],
                "runtime": {"type": "instruction"},
                "tests": [],
            }
        ),
        encoding="utf-8",
    )
    (valid_dir / "SKILL.md").write_text("Do safe things only.", encoding="utf-8")
    invalid_dir = tmp_path / "skills" / "invalid"
    invalid_dir.mkdir()
    (invalid_dir / "skill.json").write_text(json.dumps({"id": "invalid", "risk": "spicy"}), encoding="utf-8")
    (invalid_dir / "SKILL.md").write_text("No description.", encoding="utf-8")

    state = AgentStateStore(tmp_path / "state.db")
    manager = SkillManager(tmp_path / "skills", state)
    discovered = manager.discover()

    assert [skill["id"] for skill in discovered] == ["safe"]
    manifest = discovered[0]["manifest"]
    assert manifest["validation"]["ok"] is True
    assert len(manifest["provenance"]["manifest_sha256"]) == 64
    assert manager.validation_errors[0]["errors"] == ["missing_description", "invalid_risk"]
    assert manager.tool_adapters()[0].spec.risk == "low"


def test_skill_discovery_skips_symlinked_directories_outside_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    (outside / "skill.json").write_text(
        json.dumps({"id": "outside", "name": "Outside", "description": "Outside skill.", "risk": "low"}),
        encoding="utf-8",
    )
    (outside / "SKILL.md").write_text("Do not load through symlink.", encoding="utf-8")
    skills_root = tmp_path / "skills"
    skills_root.mkdir()
    (skills_root / "outside").symlink_to(outside, target_is_directory=True)
    state = AgentStateStore(tmp_path / "state.db")
    manager = SkillManager(skills_root, state)

    discovered = manager.discover()

    assert discovered == []
    with pytest.raises(KeyError):
        state.get_skill("outside")


def test_python_skill_runtime_executes_in_skill_directory(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    skill_dir = tmp_path / "skills" / "python-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": "python-review",
                "name": "Python Review",
                "description": "Run a tiny Python skill.",
                "risk": "low",
                "runtime": {"type": "python", "entrypoint": "skill.py", "timeout": 5},
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("Read stdin JSON and respond.", encoding="utf-8")
    (skill_dir / "skill.py").write_text(
        "import json, sys\npayload=json.loads(sys.stdin.read())\nprint('skill saw ' + payload['task'])\n",
        encoding="utf-8",
    )
    manager = SkillManager(tmp_path / "skills", state)
    manager.discover()
    adapter = manager.tool_adapters()[0]
    memory = build_memory_system("memory", tmp_path / "memory")

    assert adapter.spec.risk == "high"
    assert adapter.spec.requires_approval is True
    registry = build_default_tools()
    registry.register(adapter)
    call = ToolCall(name=adapter.spec.name, arguments={"task": "scheduler output"}, id="python_skill")

    blocked = registry.execute(call, ToolContext(memory=memory, config=AgentConfig(), workspace=tmp_path))
    assert blocked.success is False
    assert blocked.error == "tool_disabled"

    result = registry.execute(
        call,
        ToolContext(
            memory=memory,
            config=AgentConfig(allow_executable_skills=True),
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"python_skill"}),
            approved_tool_call_arguments={"python_skill": {"task": "scheduler output"}},
        ),
    )

    assert result.success is True
    assert "skill saw scheduler output" in result.content
    assert result.data["runtime"] == "python"


def test_validate_skill_manifest_rejects_bad_shapes() -> None:
    result = validate_skill_manifest(
        {
            "id": "bad",
            "description": "Bad shape",
            "risk": "medium",
            "capabilities": "skill",
            "permissions": {},
            "runtime": {"type": "spaceship"},
        }
    )

    assert result["ok"] is False
    assert {"invalid_capabilities", "invalid_permissions", "unsupported_runtime"} <= set(result["errors"])


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
    assert graph["tasks"][0]["plan"]["graph_runtime"]["reviewer_gate"] is True
    trace = manager.run_trace(run.run_id)
    span_types = {span["span_type"] for span in trace["spans"]}
    assert {"run", "plan", "llm.request", "review", "memory.write"} <= span_types
    assert trace["summary"]["span_counts"]["plan"] >= 1
    event_types = [event["type"] for event in manager.state.list_run_steps(run.run_id)]
    assert "orchestration.plan" in event_types
    assert "review.completed" in event_types


def test_full_agent_flow_blocks_approves_resumes_traces_and_capsules(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config = AgentConfig(
        **{
            **manager.config.__dict__,
            "allow_shell": True,
            "enable_task_capsules": True,
        }
    )
    scripted = [
        LLMResponse(
            content="I need approval to run validation.",
            tool_calls=(
                ToolCall(
                    name="test.run",
                    arguments={"command": ["python3", "-c", "print('full-flow-ok')"]},
                ),
            ),
        ),
        LLMResponse(content="Validation completed after approval."),
    ]

    def build_scripted_agent(config: AgentConfig) -> NestedMV2Agent:
        response = scripted.pop(0)
        return NestedMV2Agent(
            AgentDependencies(
                memory=build_memory_system(config.backend, config.memory_dir),
                llm=MockLLMProvider(canned=[response]),
                tools=manager.build_registry(),
                config=config,
                event_log=JsonlEventLog(config.log_dir / "events.jsonl"),
            )
        )

    manager._build_agent = build_scripted_agent  # type: ignore[method-assign]

    run = manager.create_run(message="Run the full validation flow", session_id="session")
    blocked = _wait_for_status(manager, run.run_id, {"blocked", "failed"})
    assert blocked["status"] == "blocked"
    assert blocked["stop_reason"] == "approval_required"
    blocked_trace = manager.run_trace(run.run_id)
    assert "approval.wait" in blocked_trace["summary"]["span_counts"]

    approvals = manager.state.list_approvals(status="pending")
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["tool_name"] == "test.run"

    manager.decide_approval(approval["approval_id"], approved=True, arguments=approval["arguments"])
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})

    assert final["status"] == "completed"
    assert final["assistant_message"] == "Validation completed after approval."
    decided = manager.state.list_approvals(status="approved")[0]
    assert decided["result"]["success"] is True
    assert "full-flow-ok" in decided["result"]["content"]

    graph = manager.task_graph(run.run_id)
    assert graph["tasks"]
    trace = manager.run_trace(run.run_id)
    event_types = [event["type"] for event in manager.state.list_run_steps(run.run_id)]
    assert "approval.requested" in event_types
    assert "tool.completed" in event_types
    assert "run.completed" in event_types
    assert trace["run"]["status"] == "completed"
    assert (manager.config.memory_dir.parent / "runs" / run.run_id / "complete.mv2").exists()


def test_approval_decision_cannot_replace_requested_arguments(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config = AgentConfig(**{**manager.config.__dict__, "allow_shell": True})
    manager.state.create_run(
        run_id="run_changed_args",
        message="approval argument change",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    approval = manager.state.create_approval(
        approval_id="approval_changed_args",
        run_id="run_changed_args",
        tool_call_id="tool_shell",
        tool_name="shell.run",
        arguments={"command": ["echo", "safe"]},
        risk="high",
    )

    with pytest.raises(ValueError, match="exact requested arguments"):
        manager.decide_approval(
            approval["approval_id"],
            approved=True,
            arguments={"command": ["echo", "changed"]},
        )

    assert manager.state.get_approval("approval_changed_args")["status"] == "pending"


def test_manual_run_tool_invocation_uses_run_workspace(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "manager")
    run_workspace = tmp_path / "run-workspace"
    run_workspace.mkdir()
    (run_workspace / "note.txt").write_text("run workspace only", encoding="utf-8")
    run = manager.state.create_run(
        run_id="run_workspace_tool",
        message="manual tool",
        session_id="session",
        workspace=str(run_workspace),
        model="mock",
    )

    result = manager.invoke_tool(tool_name="file.read", arguments={"path": "note.txt"}, run_id=run.run_id)

    assert result.success is True
    assert result.content == "run workspace only"


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


def test_run_manager_repair_plan_inserts_review_gate_before_commit(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="Fix the failing test, validate it, review the repair, and commit it", session_id="session")

    graph = manager.task_graph(run.run_id)
    child_tasks = graph["tasks"][1:]
    titles = [task["title"] for task in child_tasks]

    assert "Review repair before commit" in titles
    assert "Commit reviewed repair" in titles
    review_task = child_tasks[titles.index("Review repair before commit")]
    commit_task = child_tasks[titles.index("Commit reviewed repair")]
    validate_task = next(task for task in child_tasks if task["title"] == "Validate repair")
    assert "repair.review" in review_task["required_tools"]
    assert "git.commit" in commit_task["required_tools"]
    assert validate_task["task_id"] in review_task["dependencies"]
    assert review_task["task_id"] in commit_task["dependencies"]
    assert any("repair.review" in criterion for criterion in commit_task["acceptance_criteria"])


def test_run_manager_ready_tasks_respect_dependencies_and_approval(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="run_ready_dependencies",
        message="Schedule dependent tasks",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    root = manager.state.create_task_node(
        task_id="task_root_ready",
        run_id=run.run_id,
        title="Ready root",
        goal="No dependencies",
        profile="worker",
        status="queued",
        approved=True,
    )
    dependent = manager.state.create_task_node(
        task_id="task_dependent_ready",
        run_id=run.run_id,
        title="Dependent task",
        goal="Waits for root",
        profile="worker",
        status="queued",
        approved=True,
        dependencies=[root.task_id],
    )
    manager.state.create_task_node(
        task_id="task_unapproved",
        run_id=run.run_id,
        title="Needs approval",
        goal="Risky task",
        profile="worker",
        status="queued",
        approved=False,
    )

    ready = manager.ready_tasks(run.run_id)
    assert [task["task_id"] for task in ready] == [root.task_id]

    manager.state.update_task_node(root.task_id, status="completed")
    ready_after_dependency = manager.ready_tasks(run.run_id)
    assert dependent.task_id in [task["task_id"] for task in ready_after_dependency]
    assert "task_unapproved" not in [task["task_id"] for task in ready_after_dependency]


def test_run_manager_ready_tasks_block_failed_retries_until_strategy_changes(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.state.create_run(
        run_id="run_retry_gate",
        message="Retry failed validation",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    task = manager.state.create_task_node(
        task_id="task_retry_gate",
        run_id=run.run_id,
        title="Retry gated task",
        goal="Validate repair",
        profile="worker",
        status="failed",
        approved=True,
        retry_strategy={
            "requires_changed_strategy": True,
            "retry_allowed": False,
            "reason": "same validation command already failed",
        },
    )

    assert task.task_id not in [candidate["task_id"] for candidate in manager.ready_tasks(run.run_id)]

    manager.state.update_task_node(
        task.task_id,
        status="queued",
        retry_strategy={
            "requires_changed_strategy": True,
            "retry_allowed": True,
            "changed_strategy": "narrow validation to failing test before full suite",
        },
    )

    ready = manager.ready_tasks(run.run_id)
    assert task.task_id in [candidate["task_id"] for candidate in ready]
    retry_candidate = next(candidate for candidate in ready if candidate["task_id"] == task.task_id)
    assert retry_candidate["scheduler_reason"] == "retry_strategy_changed"


def test_run_manager_scheduler_step_executes_ready_child_task(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="Inspect the repo with the scheduler", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed"

    step = manager.run_scheduler_step(run.run_id, max_tasks=1)

    assert len(step["executed"]) == 1
    executed = step["executed"][0]
    assert executed["status"] == "completed"
    task = manager.state.get_task_node(str(executed["task_id"]))
    assert task.status == "completed"
    assert "Mock response" in str(task.result)
    graph = manager.task_graph(run.run_id)
    assert graph["subagents"]


def test_scheduler_task_uses_git_worktree_when_worker_isolation_enabled(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        raise AssertionError("git is required for worker isolation tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "kestrel@example.test")
    _git(repo, "config", "user.name", "Kestrel Test")
    (repo / "README.md").write_text("worker isolation\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    manager = _manager(tmp_path)
    manager.config = AgentConfig(
        **{
            **manager.config.__dict__,
            "workspace": repo,
            "enable_worker_isolation": True,
            "worker_worktree_dir": tmp_path / "worker-worktrees",
        }
    )
    run = manager.create_run(message="Complete an isolated scheduler task", session_id="session", workspace=repo)
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed"

    step = manager.run_scheduler_step(run.run_id, max_tasks=1)

    assert step["executed"][0]["status"] == "completed"
    isolation = step["executed"][0]["worker_isolation"]
    assert isolation["mode"] == "git-worktree"
    assert Path(isolation["workspace"]).exists()
    assert (Path(isolation["workspace"]) / ".git").exists()
    assert isolation["branch"].startswith("kestrel/worker/")
    task = manager.state.get_task_node(str(step["executed"][0]["task_id"]))
    assert task.result is not None
    assert task.result["worker_isolation"]["workspace"] == isolation["workspace"]
    event_types = [event["type"] for event in manager.state.list_run_steps(run.run_id)]
    assert "worker.isolated" in event_types


def test_run_manager_scheduler_step_drains_newly_ready_dependencies(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="Complete a low-risk autonomous chain", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed"

    step = manager.run_scheduler_step(run.run_id, max_tasks=3)

    assert [item["status"] for item in step["executed"]] == ["completed", "completed", "completed"]
    executed_titles = [
        manager.state.get_task_node(str(item["task_id"])).title
        for item in step["executed"]
    ]
    assert executed_titles == ["Inspect context", "Execute and validate", "Review outcome"]
    assert step["remaining_ready_tasks"] == []
    root = next(task for task in manager.state.list_task_nodes(run.run_id) if task.parent_id is None)
    assert root.status == "completed"


def test_run_manager_scheduler_until_idle_spans_bounded_cycles(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="Drain a low-risk chain over cycles", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed"

    scheduler = manager.run_scheduler_until_idle(run.run_id, max_tasks=1, max_cycles=5)

    assert scheduler["stop_reason"] == "idle"
    assert scheduler["cycles"] == 3
    assert [item["status"] for item in scheduler["executed"]] == ["completed", "completed", "completed"]
    assert scheduler["remaining_ready_tasks"] == []


def test_autonomous_scheduler_completes_low_risk_run_without_manual_steps(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        enable_autonomous_scheduler=True,
        max_scheduler_tasks=1,
        max_scheduler_cycles=5,
    )
    run = manager.create_run(message="Complete the autonomous low-risk chain", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed", "blocked"})

    assert final["status"] == "completed"
    graph = manager.task_graph(run.run_id)
    child_statuses = [task["status"] for task in graph["tasks"] if task["parent_id"] is not None]
    assert child_statuses == ["completed", "completed", "completed"]
    assert graph["approval_blocked_tasks"] == []


def test_autonomous_scheduler_blocks_for_task_approval_and_resumes(tmp_path: Path) -> None:
    manager = _manager(
        tmp_path,
        enable_autonomous_scheduler=True,
        max_scheduler_tasks=2,
        max_scheduler_cycles=5,
    )
    run = manager.create_run(message="Fix a failing test, validate it, and commit it", session_id="session")
    blocked = _wait_for_status(manager, run.run_id, {"completed", "failed", "blocked"})

    assert blocked["status"] == "blocked"
    graph = manager.task_graph(run.run_id)
    blocked_titles = [task["title"] for task in graph["approval_blocked_tasks"]]
    assert blocked_titles == ["Prepare repair isolation"]

    approved = manager.approve_task(run.run_id, str(graph["approval_blocked_tasks"][0]["task_id"]))

    assert approved["scheduler"]["stop_reason"] == "task_approval_required"
    resumed = manager.get_run(run.run_id)
    assert resumed["status"] == "blocked"
    next_graph = manager.task_graph(run.run_id)
    next_blocked_titles = [task["title"] for task in next_graph["approval_blocked_tasks"]]
    assert next_blocked_titles == ["Apply repair patch"]


def test_repair_scheduler_tasks_use_git_worktree_by_default(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        raise AssertionError("git is required for repair isolation tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "kestrel@example.test")
    _git(repo, "config", "user.name", "Kestrel Test")
    (repo / "README.md").write_text("repair isolation\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    manager = _manager(
        tmp_path,
        enable_autonomous_scheduler=True,
        max_scheduler_tasks=2,
        max_scheduler_cycles=5,
    )
    manager.config = AgentConfig(
        **{
            **manager.config.__dict__,
            "workspace": repo,
            "worker_worktree_dir": tmp_path / "worker-worktrees",
            "enable_worker_isolation": False,
        }
    )
    run = manager.create_run(
        message="Fix a failing test, validate it, and commit it",
        session_id="session",
        workspace=repo,
    )
    blocked = _wait_for_status(manager, run.run_id, {"completed", "failed", "blocked"})
    assert blocked["status"] == "blocked"
    graph = manager.task_graph(run.run_id)
    prepare_task = graph["approval_blocked_tasks"][0]
    assert prepare_task["title"] == "Prepare repair isolation"

    approved = manager.approve_task(run.run_id, str(prepare_task["task_id"]))

    executed = approved["scheduler"]["executed"]
    assert executed[0]["status"] == "completed"
    isolation = executed[0]["worker_isolation"]
    assert isolation["mode"] == "git-worktree"
    assert Path(isolation["workspace"]).exists()
    assert isolation["branch"].startswith("kestrel/worker/")
    persisted = manager.state.get_task_node(str(executed[0]["task_id"]))
    assert persisted.result is not None
    assert persisted.result["worker_isolation"]["workspace"] == isolation["workspace"]


def test_repair_scheduler_tasks_reuse_one_git_worktree_across_repair_dag(tmp_path: Path) -> None:
    if shutil.which("git") is None:
        raise AssertionError("git is required for repair isolation tests")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "kestrel@example.test")
    _git(repo, "config", "user.name", "Kestrel Test")
    (repo / "README.md").write_text("shared repair workspace\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")

    manager = _manager(
        tmp_path,
        enable_autonomous_scheduler=True,
        max_scheduler_tasks=2,
        max_scheduler_cycles=5,
    )
    manager.config = AgentConfig(
        **{
            **manager.config.__dict__,
            "workspace": repo,
            "worker_worktree_dir": tmp_path / "worker-worktrees",
            "enable_worker_isolation": False,
        }
    )
    run = manager.create_run(
        message="Fix a failing test, validate it, and commit it",
        session_id="session",
        workspace=repo,
    )
    blocked = _wait_for_status(manager, run.run_id, {"completed", "failed", "blocked"})
    assert blocked["status"] == "blocked"
    graph = manager.task_graph(run.run_id)
    prepare_task = graph["approval_blocked_tasks"][0]
    assert prepare_task["title"] == "Prepare repair isolation"

    prepare_result = manager.approve_task(run.run_id, str(prepare_task["task_id"]))
    prepare_isolation = prepare_result["scheduler"]["executed"][0]["worker_isolation"]
    next_graph = manager.task_graph(run.run_id)
    patch_task = next_graph["approval_blocked_tasks"][0]
    assert patch_task["title"] == "Apply repair patch"

    patch_result = manager.approve_task(run.run_id, str(patch_task["task_id"]))
    patch_isolation = patch_result["scheduler"]["executed"][0]["worker_isolation"]

    assert patch_isolation["workspace"] == prepare_isolation["workspace"]
    assert patch_isolation["branch"] == prepare_isolation["branch"]
    assert patch_isolation["worker_id"] == prepare_isolation["worker_id"] == "repair"
    assert len(list((tmp_path / "worker-worktrees" / run.run_id).iterdir())) == 1


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
    assert trace["summary"]["span_count"] >= 1
    assert "tool.call" in trace["summary"]["span_counts"]
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


def test_run_manager_records_subagent_failure_diagnosis_on_task_node(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    run = manager.create_run(message="main run", session_id="session")
    task = manager.state.create_task_node(
        task_id="task_subagent_failure",
        run_id=run.run_id,
        title="Failing reviewer",
        goal="Run failing validation",
        profile="reviewer",
        status="queued",
        approved=True,
    )
    subagent = manager.state.create_subagent_run(
        subagent_id="subagent_failure",
        run_id=run.run_id,
        task_id=task.task_id,
        profile="reviewer",
        goal="Run failing validation",
        status="queued",
    )

    class FailingAgent:
        def chat(self, *args: object, **kwargs: object) -> AgentTurnResult:
            raise AssertionError("pytest failed for widget edge case")

        def close(self) -> None:
            return None

    manager._build_agent = lambda config: FailingAgent()  # type: ignore[method-assign]

    manager._run_subagent("thread", manager.config, subagent.subagent_id, run.run_id, "session")

    failed_task = manager.state.get_task_node(task.task_id)
    assert failed_task.status == "failed"
    assert failed_task.attempt_count == 1
    assert failed_task.diagnosis is not None
    assert failed_task.diagnosis["classification"] == "test_failure"
    assert failed_task.retry_strategy is not None
    assert failed_task.retry_strategy["requires_changed_strategy"] is True
    assert "AssertionError" in failed_task.failure_reason


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


def test_run_manager_executes_manual_terminal_run_approval_without_continuation(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config = AgentConfig(
        **{**manager.config.__dict__, "allow_file_write": True}
    )
    run = manager.state.create_run(
        run_id="run_completed_manual_tool",
        message="manual",
        session_id="session",
        workspace=str(tmp_path),
        model="mock",
    )
    manager.state.transition_run(run.run_id, "running")
    manager.state.transition_run(run.run_id, "completed", assistant_message="done", stop_reason="complete")

    execution = manager.invoke_tool(
        tool_name="file.write",
        arguments={"path": "approved.txt", "content": "approved\n"},
        session_id="session",
        run_id=run.run_id,
    )
    assert execution.error == "approval_pending"
    approval = manager.state.list_approvals(status="pending")[0]

    decided = manager.decide_approval(approval["approval_id"], approved=True, arguments=approval["arguments"])

    assert decided["status"] == "approved"
    assert decided["result"] is not None
    assert decided["result"]["success"] is True
    assert (tmp_path / "approved.txt").read_text(encoding="utf-8") == "approved\n"
    final = manager.get_run(run.run_id)
    assert final["status"] == "completed"
    assert final["assistant_message"] == "done"


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


def _manager(
    tmp_path: Path,
    *,
    stream: bool = False,
    enable_autonomous_scheduler: bool = False,
    max_scheduler_tasks: int = 3,
    max_scheduler_cycles: int = 5,
) -> RunManager:
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
        enable_autonomous_scheduler=enable_autonomous_scheduler,
        max_scheduler_tasks=max_scheduler_tasks,
        max_scheduler_cycles=max_scheduler_cycles,
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


def _git(cwd: Path, *args: str) -> None:
    completed = subprocess.run(  # noqa: S603 - fixed executable and test-controlled args
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
