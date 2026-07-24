from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.routing import AdaptiveFlockRunManager
from nested_memvid_agent.routing.runtime import (
    AdaptiveFlockRuntimeConfig,
    build_run_manager,
)
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.server import create_app
from nested_memvid_agent.server_routing_routes import register_routing_routes
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


def _runtime_parts(tmp_path: Path) -> tuple[AgentConfig, AgentStateStore, RunEventBus, MCPManager, SkillManager]:
    config = AgentConfig(
        state_path=tmp_path / "state" / "agent.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
    )
    state = AgentStateStore(config.state_path)
    events = RunEventBus(state)
    return config, state, events, MCPManager(state), SkillManager(config.skills_dir, state)


def test_runtime_config_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", raising=False)
    monkeypatch.delenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", raising=False)
    monkeypatch.delenv("NEST_AGENT_ADAPTIVE_FLOCK_POLICY", raising=False)

    config = AdaptiveFlockRuntimeConfig.from_env()

    assert config == AdaptiveFlockRuntimeConfig(enabled=False, mode="off", policy_id="balanced")


def test_runtime_config_enables_shadow_mode_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.delenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", raising=False)

    config = AdaptiveFlockRuntimeConfig.from_env()

    assert config.enabled is True
    assert config.mode == "shadow"


def test_runtime_config_rejects_inconsistent_mode() -> None:
    with pytest.raises(ValueError, match="must not be off"):
        AdaptiveFlockRuntimeConfig(enabled=True, mode="off")
    with pytest.raises(ValueError, match="must be off"):
        AdaptiveFlockRuntimeConfig(enabled=False, mode="shadow")


def test_manager_factory_preserves_default_run_manager(tmp_path: Path) -> None:
    config, state, events, mcp, skills = _runtime_parts(tmp_path)

    build = build_run_manager(
        config=config,
        state=state,
        events=events,
        mcp=mcp,
        skills=skills,
        auto_start=False,
        routing_config=AdaptiveFlockRuntimeConfig(),
    )
    try:
        assert build.runs.__class__ is RunManager
        assert build.routing_ledger.get_policy("balanced") is not None
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert mcp.shutdown()


def test_manager_factory_selects_adaptive_flock_only_when_enabled(tmp_path: Path) -> None:
    config, state, events, mcp, skills = _runtime_parts(tmp_path)

    build = build_run_manager(
        config=config,
        state=state,
        events=events,
        mcp=mcp,
        skills=skills,
        auto_start=False,
        routing_config=AdaptiveFlockRuntimeConfig(enabled=True, mode="shadow"),
    )
    try:
        assert isinstance(build.runs, AdaptiveFlockRunManager)
        assert build.routing_config.mode == "shadow"
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert mcp.shutdown()


def test_routing_api_round_trips_inventory_and_hides_secret_reference(tmp_path: Path) -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    build = build_run_manager(
        config=AgentConfig(state_path=state.path, workspace=tmp_path),
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(tmp_path / "skills", state),
        auto_start=False,
        routing_config=AdaptiveFlockRuntimeConfig(),
    )
    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=build.routing_ledger,
        runtime=build.routing_config,
        http_exception=fastapi.HTTPException,
    )
    client = testclient.TestClient(app)
    try:
        profile = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "local",
                "display_name": "Local server",
                "adapter": "openai-compatible",
                "base_url": "http://127.0.0.1:1234/v1",
                "secret_ref": "secret://local-key",
                "locality": "local",
                "metadata": {"max_context_tokens": 131072},
            },
        )
        assert profile.status_code == 200
        assert profile.json()["secret_configured"] is True
        assert "secret_ref" not in profile.json()

        target = client.post(
            "/api/routing/targets",
            json={
                "target_id": "local-worker",
                "provider_profile_id": "local",
                "provider": "openai-compatible",
                "model": "qwen-coder",
                "locality": "local",
                "capability_tags": ["worker", "coding"],
                "max_context_tokens": 131072,
                "supports_tools": True,
                "quality_tier": 3,
                "health": "healthy",
            },
        )
        assert target.status_code == 200
        assert target.json()["model"] == "qwen-coder"

        status = client.get("/api/routing/status")
        assert status.status_code == 200
        assert status.json()["runtime"]["enabled"] is False
        assert status.json()["counts"]["provider_profiles"] == 1
        assert status.json()["counts"]["model_targets"] == 1
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_routing_api_returns_revision_conflict(tmp_path: Path) -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    build = build_run_manager(
        config=AgentConfig(state_path=state.path, workspace=tmp_path),
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(tmp_path / "skills", state),
        auto_start=False,
        routing_config=AdaptiveFlockRuntimeConfig(),
    )
    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=build.routing_ledger,
        runtime=build.routing_config,
        http_exception=fastapi.HTTPException,
    )
    client = testclient.TestClient(app)
    payload = {
        "profile_id": "local",
        "display_name": "Local server",
        "adapter": "openai-compatible",
        "locality": "local",
    }
    try:
        assert client.post("/api/routing/providers", json=payload).status_code == 200
        conflict = client.post(
            "/api/routing/providers",
            json={**payload, "display_name": "Stale update"},
        )
        assert conflict.status_code == 409
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_create_app_registers_default_off_routing_control_plane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fastapi_testclient = pytest.importorskip("fastapi.testclient")
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "false")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "shadow")
    config = AgentConfig(
        provider="mock",
        model="mock",
        state_path=tmp_path / "state" / "agent.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        require_api_auth=False,
    )

    with fastapi_testclient.TestClient(create_app(config)) as client:
        response = client.get("/api/routing/status")

    assert response.status_code == 200
    assert response.json()["runtime"] == {
        "enabled": False,
        "mode": "off",
        "policy_id": "balanced",
    }
