from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nested_memvid_agent.capability_policy import (
    parent_resource_digest,
    tool_spec_digest,
)
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.plugin_manager import PluginManager
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.server import create_app
from nested_memvid_agent.server_capability_routes import register_capability_routes
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import ToolContext


def _config(tmp_path: Path, **overrides: object) -> AgentConfig:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        secret_store_path=tmp_path / "secrets" / "vault.json",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        mcp_config_path=tmp_path / "config" / "mcp_servers.json",
        channel_config_path=tmp_path / "config" / "channels.json",
        workspace=workspace,
        require_api_auth=False,
        tool_retry_max_attempts=0,
    )
    return replace(config, **overrides)


def _runtime(
    config: AgentConfig,
    *,
    state: AgentStateStore | None = None,
) -> tuple[AgentStateStore, MCPManager, SkillManager, PluginManager, RunManager]:
    state = state or AgentStateStore(config.state_path)
    mcp = MCPManager(state, timeout_seconds=0.05)
    skills = SkillManager(config.skills_dir, state)
    plugins = PluginManager(config.plugins_dir, state)
    runs = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=mcp,
        skills=skills,
        plugins=plugins,
    )
    return state, mcp, skills, plugins, runs


def _capability(
    payload: dict[str, Any], kind: str, capability_id: str
) -> dict[str, Any]:
    return next(
        item
        for item in cast(list[dict[str, Any]], payload["items"])
        if item["kind"] == kind and item["id"] == capability_id
    )


def _create_manual_run(
    state: AgentStateStore,
    config: AgentConfig,
    run_id: str,
) -> None:
    state.create_run(
        run_id=run_id,
        message="adversarial capability test",
        session_id="test",
        workspace=str(config.workspace),
        provider="mock",
        model="mock",
    )


def test_full_app_catalog_toggle_live_deny_and_stale_cas(tmp_path: Path) -> None:
    config = _config(tmp_path)

    with TestClient(create_app(config)) as client:
        listed = client.get("/api/capabilities")
        assert listed.status_code == 200
        initial = _capability(listed.json(), "tool", "memory.search")
        assert initial["configured_enabled"] is True
        assert initial["effective_enabled"] is True
        assert initial["revision"] == 0

        disabled = client.put(
            "/api/capabilities/tool/memory.search",
            json={"enabled": False, "expected_revision": 0},
        )
        assert disabled.status_code == 200
        assert disabled.json()["capability"]["configured_enabled"] is False
        assert disabled.json()["capability"]["effective_enabled"] is False
        assert disabled.json()["capability"]["revision"] == 1
        assert disabled.json()["applies_to"] == "future_invocations"

        tools = client.get("/api/tools")
        assert tools.status_code == 200
        memory_search = next(
            item for item in tools.json() if item["name"] == "memory.search"
        )
        assert memory_search["enabled"] is False

        invocation = client.post(
            "/api/tools/memory.search/invoke",
            json={"arguments": {"query": "must not execute"}},
        )
        assert invocation.status_code == 200
        assert invocation.json()["success"] is False
        assert invocation.json()["error"] == "tool_disabled"

        stale = client.put(
            "/api/capabilities/tool/memory.search",
            json={"enabled": True, "expected_revision": 0},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["error"] == "capability_revision_conflict"
        assert stale.json()["detail"]["current"]["revision"] == 1


def test_alias_toggle_is_canonical_and_stale_registry_denies_live(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state, _mcp, _skills, _plugins, runs = _runtime(config)
    (config.workspace / "note.txt").write_text("sensitive\n", encoding="utf-8")
    stale_registry = runs.build_registry()
    spec = stale_registry.spec_for("read")
    assert spec is not None
    assert spec.name == "file.read"
    assert stale_registry.canonical_name("read") == "file.read"

    state.set_capability_override(
        "tool",
        "file.read",
        False,
        expected_revision=0,
        default_enabled=True,
        resource_digest=tool_spec_digest(spec),
    )

    result = stale_registry.execute(
        ToolCall(name="read", arguments={"path": "note.txt"}),
        ToolContext(
            memory=cast(Any, None),
            config=config,
            workspace=config.workspace,
        ),
    )
    assert result.success is False
    assert result.error == "tool_disabled"
    assert "file.read" in result.content

    app = FastAPI()
    register_capability_routes(
        app,
        http_exception=HTTPException,
        state=state,
        runs=runs,
        mcp=runs.mcp,
        skills=runs.skills,
    )
    with TestClient(app) as client:
        enabled = client.put(
            "/api/capabilities/tool/read",
            json={"enabled": True, "expected_revision": 1},
        )
    assert enabled.status_code == 200
    assert enabled.json()["capability"]["id"] == "file.read"
    assert enabled.json()["capability"]["revision"] == 2


def test_high_risk_default_off_and_enable_never_bypasses_approval(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    with TestClient(create_app(config)) as client:
        catalog = client.get("/api/capabilities").json()
        branch_tool = _capability(catalog, "tool", "git.create_local_branch")
        assert branch_tool["risk"] == "high"
        assert branch_tool["requires_approval"] is True
        assert branch_tool["default_enabled"] is False
        assert branch_tool["effective_enabled"] is False

        enabled = client.put(
            "/api/capabilities/tool/git.create_local_branch",
            json={"enabled": True, "expected_revision": 0},
        )
        assert enabled.status_code == 200
        capability = enabled.json()["capability"]
        assert capability["effective_enabled"] is True
        assert capability["requires_approval"] is True

        invocation = client.post(
            "/api/tools/git.create_local_branch/invoke",
            json={
                "arguments": {
                    "branch": "kestrel/adversarial-must-not-exist",
                    "checkout": False,
                }
            },
        )
        assert invocation.status_code == 200
        assert invocation.json()["success"] is False
        assert invocation.json()["error"] == "approval_required"


def test_disabled_skill_survives_discover_registry_build_and_restart(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    skill_dir = config.skills_dir / "review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": "review",
                "name": "Review",
                "description": "Review a result without side effects.",
                "version": "1.0.0",
                "risk": "low",
                "runtime": {"type": "instruction"},
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "Review the supplied result.", encoding="utf-8"
    )

    with TestClient(create_app(config)) as client:
        initial = _capability(client.get("/api/capabilities").json(), "skill", "review")
        assert initial["effective_enabled"] is False

        enabled = client.put(
            "/api/capabilities/skill/review",
            json={"enabled": True, "expected_revision": 0},
        )
        assert enabled.status_code == 200
        assert enabled.json()["capability"]["effective_enabled"] is True

        disabled = client.put(
            "/api/capabilities/skill/review",
            json={"enabled": False, "expected_revision": 1},
        )
        assert disabled.status_code == 200
        assert disabled.json()["capability"]["effective_enabled"] is False

        discovered = client.post("/api/skills/discover")
        assert discovered.status_code == 200
        assert discovered.json()["skills"][0]["enabled"] is False
        assert _capability(
            client.get("/api/capabilities").json(), "skill", "review"
        )["effective_enabled"] is False
        assert any(
            item["name"] == "skill.review.run" and item["enabled"] is False
            for item in client.get("/api/tools").json()
        )

    with TestClient(create_app(config)) as restarted:
        skill = _capability(
            restarted.get("/api/capabilities").json(), "skill", "review"
        )
        assert skill["configured_enabled"] is False
        assert skill["effective_enabled"] is False
        assert skill["revision"] == 2
        invocation = restarted.post(
            "/api/skills/review/run",
            json={"arguments": {"task": "must not run"}},
        )
        assert invocation.status_code == 200
        assert invocation.json()["success"] is False
        assert invocation.json()["error"] == "tool_disabled"


def test_legacy_skill_toggle_is_revisioned_and_audited(tmp_path: Path) -> None:
    config = _config(tmp_path)
    skill_dir = config.skills_dir / "legacy"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.json").write_text(
        json.dumps(
            {
                "id": "legacy",
                "name": "Legacy",
                "description": "Compatibility endpoint fixture.",
                "version": "1.0.0",
                "risk": "low",
                "runtime": {"type": "instruction"},
            }
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text("Review the input.", encoding="utf-8")

    with TestClient(create_app(config)) as client:
        enabled = client.post("/api/skills/legacy/enable")
        assert enabled.status_code == 200
        capability = _capability(
            client.get("/api/capabilities").json(), "skill", "legacy"
        )
        assert capability["configured_enabled"] is True
        assert capability["revision"] == 1

        disabled = client.post("/api/skills/legacy/disable")
        assert disabled.status_code == 200
        capability = _capability(
            client.get("/api/capabilities").json(), "skill", "legacy"
        )
        assert capability["configured_enabled"] is False
        assert capability["revision"] == 2

    history = AgentStateStore(config.state_path).list_capability_changes(
        kind="skill", capability_id="legacy"
    )
    assert len(history) == 2
    assert {row["updated_by"] for row in history} == {
        "owner:legacy-skill-endpoint"
    }


def test_mcp_configuration_routes_cannot_bypass_capability_policy(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    payload = {
        "id": "configured",
        "name": "Configured",
        "transport": "stdio",
        "command": "does-not-run",
        "enabled": True,
        "tools": [{"name": "echo", "description": "Echo a value."}],
    }

    with TestClient(create_app(config)) as client:
        created = client.post("/api/mcp/servers", json=payload)
        assert created.status_code == 200
        assert created.json()["enabled"] is False
        capability = _capability(
            client.get("/api/capabilities").json(), "mcp_server", "configured"
        )
        assert capability["configured_enabled"] is False
        assert capability["revision"] == 0

        enabled = client.put(
            "/api/capabilities/mcp_server/configured",
            json={"enabled": True, "expected_revision": 0},
        )
        assert enabled.status_code == 200
        assert enabled.json()["capability"]["effective_enabled"] is True

        edited = client.put(
            "/api/mcp/servers/configured",
            json={"id": "configured", "enabled": False},
        )
        assert edited.status_code == 200
        assert edited.json()["enabled"] is True
        capability = _capability(
            client.get("/api/capabilities").json(), "mcp_server", "configured"
        )
        assert capability["configured_enabled"] is True
        assert capability["effective_enabled"] is True
        assert capability["revision"] == 1


def test_plugin_child_overrides_survive_manifest_resync(tmp_path: Path) -> None:
    config = _config(tmp_path)
    state = AgentStateStore(config.state_path)
    install_path = config.plugins_dir / "demo"
    install_path.mkdir(parents=True)
    state.upsert_plugin(
        {
            "id": "demo",
            "name": "Demo",
            "description": "Deterministic plugin fixture.",
            "source_url": "https://github.com/example/demo",
            "commit_sha": "a" * 40,
            "install_path": str(install_path),
            "enabled": True,
            "risk_report": {},
            "install_status": "installed",
            "format": "kestrel",
            "capabilities": [],
            "manifest": {
                "risk": "low",
                "skills": [
                    {
                        "id": "review",
                        "namespaced_id": "plugin.demo.review",
                        "name": "Review",
                        "description": "Review a value.",
                        "enabled": True,
                        "instructions": "Review the supplied value.",
                        "manifest": {
                            "id": "plugin.demo.review",
                            "description": "Review a value.",
                            "risk": "low",
                            "runtime": {"type": "instruction"},
                        },
                    }
                ],
                "mcp_servers": [
                    {
                        "id": "static",
                        "namespaced_id": "plugin.demo.static",
                        "config": {
                            "transport": "stdio",
                            "enabled": True,
                            "tools": [
                                {
                                    "name": "mcp.plugin.demo.static.echo",
                                    "remote_name": "echo",
                                    "description": "Echo a value.",
                                    "parameters": {"type": "object"},
                                    "risk": "medium",
                                    "requires_approval": True,
                                }
                            ],
                        },
                    }
                ],
            },
        }
    )
    state, _mcp, _skills, plugins, runs = _runtime(config, state=state)

    for kind, capability_id in (
        ("skill", "plugin.demo.review"),
        ("mcp_server", "plugin.demo.static"),
    ):
        state.set_capability_override(
            kind,
            capability_id,
            False,
            expected_revision=0,
            default_enabled=True,
            resource_digest=parent_resource_digest(state, kind, capability_id),
        )

    plugins.sync_all()

    for kind, capability_id in (
        ("skill", "plugin.demo.review"),
        ("mcp_server", "plugin.demo.static"),
    ):
        entity = (
            state.get_skill(capability_id)
            if kind == "skill"
            else state.get_mcp_server(capability_id)
        )
        assert entity["enabled"] is True
        decision = runs.capabilities.parent_decision(
            kind, capability_id, entity_enabled=bool(entity["enabled"])
        )
        assert decision.configured_enabled is False
        assert decision.effective_enabled is False
        assert decision.revision == 1
        assert "resource_changed" not in decision.blocked_by

    specs = {spec.name: spec for spec in runs.build_registry().all_specs()}
    assert "skill:plugin.demo.review" in runs.capabilities.tool_decision(
        specs["skill.plugin.demo.review.run"]
    ).blocked_by
    assert "mcp_server:plugin.demo.static" in runs.capabilities.tool_decision(
        specs["mcp.plugin.demo.static.echo"]
    ).blocked_by


class _FakeMCPSession:
    def __init__(self) -> None:
        self.closed_with: list[float] = []

    def close(self, *, timeout: float) -> bool:
        self.closed_with.append(timeout)
        return True


def test_disabled_mcp_closes_session_and_rejects_every_entrypoint(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state, mcp, _skills, _plugins, _runs = _runtime(config)
    mcp.add_server(
        {
            "id": "local",
            "name": "Local",
            "transport": "stdio",
            "enabled": True,
            "tools": [
                {
                    "name": "echo",
                    "description": "Echo a value.",
                    "parameters": {"type": "object"},
                    "risk": "low",
                }
            ],
        }
    )
    state.set_capability_override(
        "mcp_server",
        "local",
        False,
        expected_revision=0,
        default_enabled=True,
        resource_digest=parent_resource_digest(state, "mcp_server", "local"),
    )

    session = _FakeMCPSession()
    mcp._sessions["local"] = cast(Any, session)
    assert mcp.close_disabled_sessions() == ["local"]
    assert session.closed_with == [mcp.timeout_seconds]
    assert "local" not in mcp._sessions

    for operation in (
        mcp.connect_server,
        mcp.server_health,
        mcp.test_server,
        mcp.restart_server,
    ):
        result = operation("local")
        assert result["ok"] is False
        assert result["message"] == "MCP server is disabled."
        assert result["server"]["session_state"] == "disconnected"

    synced = mcp.sync_server("local")
    assert synced["session_state"] == "disconnected"
    assert synced["status"] == "configured"
    with pytest.raises(ValueError, match="disabled"):
        mcp.approve_server_connect("local")

    invocation = mcp.invoke_tool("local", "echo", {"value": "no"})
    assert invocation.success is False
    assert invocation.error == "mcp_server_disabled"
    assert invocation.data["session_state"] == "disconnected"


def test_disabling_tool_revokes_pending_approval_and_fails_run(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, allow_file_write=True)
    state, mcp, skills, _plugins, runs = _runtime(config)
    run_id = "run_pending_capability"
    arguments = {"path": "must-not-exist.txt", "content": "unsafe\n"}
    _create_manual_run(state, config, run_id)

    execution = runs.invoke_tool(
        tool_name="file.write",
        arguments=arguments,
        session_id="test",
        run_id=run_id,
    )
    assert execution.error == "approval_pending"
    pending = state.list_approvals(status="pending")
    assert len(pending) == 1
    assert pending[0]["resource_digest"].startswith("sha256:")

    app = FastAPI()
    register_capability_routes(
        app,
        http_exception=HTTPException,
        state=state,
        runs=runs,
        mcp=mcp,
        skills=skills,
    )
    with TestClient(app) as client:
        response = client.put(
            "/api/capabilities/tool/file.write",
            json={"enabled": False, "expected_revision": 0},
        )

    assert response.status_code == 200
    assert response.json()["revoked_approvals"] == 1
    revoked = state.get_approval(pending[0]["approval_id"], expire=False)
    assert revoked["status"] == "denied"
    assert revoked["decision"]["reason"] == "capability_disabled"
    run = state.get_run(run_id)
    assert run.status == "failed"
    assert run.stop_reason == "capability_disabled"
    assert not (config.workspace / "must-not-exist.txt").exists()


def test_approved_grant_is_bound_to_policy_revision(tmp_path: Path) -> None:
    config = _config(tmp_path, allow_file_write=True)
    state, _mcp, _skills, _plugins, runs = _runtime(config)
    run_id = "run_policy_bound_approval"
    arguments = {"path": "policy-must-not-exist.txt", "content": "unsafe\n"}
    _create_manual_run(state, config, run_id)

    execution = runs.invoke_tool(
        tool_name="file.write",
        arguments=arguments,
        session_id="test",
        run_id=run_id,
    )
    assert execution.error == "approval_pending"
    pending = state.list_approvals(status="pending")[0]
    assert pending["capability_revision"] == 0
    assert pending["resource_digest"] == runs.tool_resource_digest(
        cast(Any, runs.build_registry().spec_for("file.write"))
    )
    approved, applied = state.decide_approval_once(
        pending["approval_id"],
        status="approved",
        decision={
            "approved": True,
            "arguments": arguments,
            "principal": "owner",
        },
        principal="owner",
    )
    assert applied is True
    assert runs._validated_approval_continuation(approved, arguments) is not None

    spec = runs.build_registry().spec_for("file.write")
    assert spec is not None
    state.set_capability_override(
        "tool",
        "file.write",
        False,
        expected_revision=0,
        default_enabled=True,
        resource_digest=tool_spec_digest(spec),
    )

    assert runs._validated_approval_continuation(approved, arguments) is None
    assert not (config.workspace / "policy-must-not-exist.txt").exists()


def test_approved_skill_grant_is_invalidated_when_parent_resource_changes(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    state, _mcp, _skills, _plugins, runs = _runtime(config)
    skill_path = config.skills_dir / "mutable"
    skill_path.mkdir(parents=True)
    base_skill = {
        "id": "mutable",
        "name": "Mutable",
        "description": "Review mutable input.",
        "path": str(skill_path),
        "manifest": {
            "id": "mutable",
            "description": "Review mutable input.",
            "version": "1.0.0",
            "risk": "medium",
            "runtime": {"type": "instruction"},
        },
        "enabled": True,
    }
    state.upsert_skill(base_skill)
    spec = runs.build_registry().spec_for("skill.mutable.run")
    assert spec is not None
    state.set_capability_override(
        "tool",
        spec.name,
        True,
        expected_revision=0,
        default_enabled=False,
        resource_digest=tool_spec_digest(spec),
    )
    run_id = "run_resource_bound_approval"
    arguments = {"task": "review"}
    _create_manual_run(state, config, run_id)

    execution = runs.invoke_tool(
        tool_name=spec.name,
        arguments=arguments,
        session_id="test",
        run_id=run_id,
    )
    assert execution.error == "approval_pending"
    pending = state.list_approvals(status="pending")[0]
    approved, applied = state.decide_approval_once(
        pending["approval_id"],
        status="approved",
        decision={
            "approved": True,
            "arguments": arguments,
            "principal": "owner",
        },
        principal="owner",
    )
    assert applied is True
    assert runs._validated_approval_continuation(approved, arguments) is not None

    changed_skill = dict(base_skill)
    changed_skill["manifest"] = {
        **cast(dict[str, Any], base_skill["manifest"]),
        "version": "2.0.0",
    }
    state.upsert_skill(changed_skill)

    assert runs._validated_approval_continuation(approved, arguments) is None
