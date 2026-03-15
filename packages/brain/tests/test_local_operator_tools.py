from __future__ import annotations

import asyncio
import json

from core.local_operator import derive_workspace_agent_profile_sync
from agent.tools import ToolRegistry
from agent.tools import daemon_control as daemon_control_impl


def test_daemon_status_falls_back_to_local_snapshot(tmp_path, monkeypatch):
    home = tmp_path / ".kestrel"
    state_dir = home / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KESTREL_HOME", str(home))
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"status": "running", "pending_suggestions": 2}),
        encoding="utf-8",
    )
    (state_dir / "runtime_profile.json").write_text(
        json.dumps({"runtime_mode": "native", "autonomy_policy": {"mode": "suggest_first"}}),
        encoding="utf-8",
    )

    result = asyncio.run(daemon_control_impl.handle_daemon_status())

    assert result["success"] is True
    assert result["source"] == "snapshot"
    assert result["status"]["pending_suggestions"] == 2
    assert result["runtime_profile"]["autonomy_policy"]["mode"] == "suggest_first"


def test_daemon_suggestions_accept_uses_local_operator_control(tmp_path, monkeypatch):
    home = tmp_path / ".kestrel"
    run_dir = home / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KESTREL_HOME", str(home))
    (run_dir / "control.sock").write_text("", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_send(method, params=None, *, paths=None, timeout_seconds=30):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"suggestion": {"id": "suggestion-1", "status": "accepted"}}

    monkeypatch.setattr(daemon_control_impl, "send_local_operator_request", fake_send)

    result = asyncio.run(
        daemon_control_impl.handle_daemon_suggestions(
            action="accept",
            suggestion_id="suggestion-1",
        )
    )

    assert result["success"] is True
    assert captured["method"] == "suggestion.resolve"
    assert captured["params"] == {"suggestion_id": "suggestion-1", "action": "accept"}
    assert result["suggestion"]["status"] == "accepted"


def test_daemon_research_start_uses_local_operator_control(tmp_path, monkeypatch):
    home = tmp_path / ".kestrel"
    run_dir = home / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("KESTREL_HOME", str(home))
    (run_dir / "control.sock").write_text("", encoding="utf-8")
    captured: dict[str, object] = {}

    async def fake_send(method, params=None, *, paths=None, timeout_seconds=30):
        captured["method"] = method
        captured["params"] = dict(params or {})
        return {"task": {"id": "task-1", "kind": "research"}}

    monkeypatch.setattr(daemon_control_impl, "send_local_operator_request", fake_send)

    result = asyncio.run(
        daemon_control_impl.handle_daemon_research(
            action="start",
            prompt="Research local-first operator routing",
        )
    )

    assert result["success"] is True
    assert captured["method"] == "research.start"
    assert captured["params"] == {"prompt": "Research local-first operator routing"}
    assert result["task"]["kind"] == "research"


def test_register_daemon_tools_exposes_local_operator_bridge_tools():
    registry = ToolRegistry()

    daemon_control_impl.register_daemon_tools(registry)

    names = {tool.name for tool in registry.list_tools()}
    assert "daemon_status" in names
    assert "daemon_suggestions" in names
    assert "daemon_research" in names
    assert "daemon_procedures" in names
    assert "daemon_learning" in names


def test_derive_workspace_agent_profile_sync_uses_daemon_autonomy_defaults():
    sync = derive_workspace_agent_profile_sync(
        fallback_autonomy_policy="moderate",
        runtime_profile={
            "runtime_mode": "native",
            "autonomy_policy": {
                "mode": "notify_only",
                "local_first": True,
                "reasoning_escalation": True,
                "require_approval_for_mutations": True,
            },
            "control_plane": {"pending_suggestions": 4},
            "agent_profile": {"profile_id": "local-workspace-1"},
        },
        status_snapshot={"status": "running"},
    )

    assert sync["autonomy_policy"] == "conservative"
    assert sync["runtime_defaults"]["local_operator"]["brain_autonomy_policy"] == "conservative"
    assert sync["runtime_defaults"]["local_operator"]["control_plane"]["pending_suggestions"] == 4
    assert sync["kernel_policy_json"]["routing_strategy"] == "local_first"
    assert sync["kernel_policy_json"]["reasoning_escalation"] is True
