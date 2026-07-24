from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.routing.runtime import AdaptiveFlockRuntimeConfig, build_run_manager
from nested_memvid_agent.server_routing_routes import register_routing_routes
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


def _routing_app(tmp_path: Path) -> tuple[Any, Any, AgentStateStore]:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    mcp = MCPManager(state)
    build = build_run_manager(
        config=AgentConfig(state_path=state.path, workspace=tmp_path),
        state=state,
        events=RunEventBus(state),
        mcp=mcp,
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
    return testclient.TestClient(app), build, state


def test_revisioned_provider_edit_preserves_omitted_configured_fields(tmp_path: Path) -> None:
    client, build, _state = _routing_app(tmp_path)
    try:
        created = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "local",
                "display_name": "Local server",
                "adapter": "openai-compatible",
                "base_url": "http://127.0.0.1:1234/v1",
                "secret_ref": "secret://local-key",
                "locality": "local",
            },
        )
        assert created.status_code == 200
        assert created.json()["revision"] == 1

        updated = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "local",
                "display_name": "Renamed local server",
                "adapter": "openai-compatible",
                "locality": "local",
                "expected_revision": 1,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        assert updated.json()["secret_configured"] is True
        assert updated.json()["base_url_configured"] is True

        stored = build.routing_ledger.get_provider_profile("local")
        assert stored is not None
        assert stored.profile.base_url == "http://127.0.0.1:1234/v1"
        assert stored.profile.secret_ref == "secret://local-key"
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_explicit_null_clears_provider_fields(tmp_path: Path) -> None:
    client, build, _state = _routing_app(tmp_path)
    try:
        assert (
            client.post(
                "/api/routing/providers",
                json={
                    "profile_id": "cloud",
                    "display_name": "Cloud account",
                    "adapter": "openai-compatible",
                    "base_url": "https://example.invalid/v1",
                    "secret_ref": "secret://cloud-key",
                    "locality": "cloud",
                },
            ).status_code
            == 200
        )
        cleared = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "cloud",
                "display_name": "Cloud account",
                "adapter": "openai-compatible",
                "base_url": None,
                "secret_ref": None,
                "locality": "cloud",
                "expected_revision": 1,
            },
        )
        assert cleared.status_code == 200
        assert cleared.json()["secret_configured"] is False
        assert cleared.json()["base_url_configured"] is False

        stored = build.routing_ledger.get_provider_profile("cloud")
        assert stored is not None
        assert stored.profile.base_url is None
        assert stored.profile.secret_ref is None
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_preview_response_includes_durable_task_summary(tmp_path: Path) -> None:
    client, build, state = _routing_app(tmp_path)
    state.create_run(
        run_id="run-preview-summary",
        message="Inspect the repository",
        session_id="session-preview-summary",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    state.create_task_node(
        task_id="task-preview-summary",
        run_id="run-preview-summary",
        title="Inspect repository context",
        goal="Gather repository context without mutation.",
        profile="worker",
        approved=True,
        required_tools=("repo.search",),
        risk="low",
        acceptance_criteria=(),
    )
    try:
        assert (
            client.post(
                "/api/routing/providers",
                json={
                    "profile_id": "local",
                    "display_name": "Local server",
                    "adapter": "openai-compatible",
                    "locality": "local",
                },
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/routing/targets",
                json={
                    "target_id": "local-scout",
                    "provider_profile_id": "local",
                    "provider": "openai-compatible",
                    "model": "qwen-coder",
                    "locality": "local",
                    "capability_tags": ["worker", "scout", "repository_inspection"],
                    "role_affinities": ["worker"],
                    "task_family_affinities": ["repository_inspection"],
                    "max_context_tokens": 131072,
                    "supports_tools": True,
                    "quality_tier": 3,
                    "health": "healthy",
                },
            ).status_code
            == 200
        )
        response = client.post(
            "/api/routing/preview",
            json={
                "run_id": "run-preview-summary",
                "task_id": "task-preview-summary",
                "local_required": True,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["task"] == {
            "task_id": "task-preview-summary",
            "run_id": "run-preview-summary",
            "title": "Inspect repository context",
            "status": "queued",
        }
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()
