from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.llm.model_catalog import ProviderModelCatalog
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.provider_probe import (
    CapabilityEvidence,
    ModelProbeObservation,
    ProviderProbeService,
)
from nested_memvid_agent.routing.models import ProviderProfile
from nested_memvid_agent.routing.runtime import AdaptiveFlockRuntimeConfig, build_run_manager
from nested_memvid_agent.server_routing_routes import register_routing_routes
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


def _routing_app(
    tmp_path: Path,
    *,
    models: list[str],
    catalog_ok: bool = True,
) -> tuple[Any, Any, ProviderProbeService]:
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

    def load_catalog(_profile: ProviderProfile, _timeout: float) -> ProviderModelCatalog:
        return ProviderModelCatalog(
            provider="openai-compatible",
            models=tuple(models),
            fallback_models=("fallback-model",),
            source="provider" if catalog_ok else "fallback",
            ok=catalog_ok,
            fetchable=True,
            error=None if catalog_ok else "catalog unavailable",
            fetched_at="2026-07-28T12:00:00+00:00",
        )

    class Backend:
        def probe(
            self,
            profile: ProviderProfile,
            model: str,
            *,
            timeout_seconds: float,
        ) -> ModelProbeObservation:
            del profile, timeout_seconds
            return ModelProbeObservation(
                model=model,
                model_identity=model,
                latency_ms=25.0,
                capabilities=(
                    CapabilityEvidence.observed_pass("generation"),
                    CapabilityEvidence.observed_pass("streaming"),
                    CapabilityEvidence.observed_pass("structured_output"),
                    CapabilityEvidence.observed_pass("tools"),
                ),
            )

    service = ProviderProbeService(
        catalog_loader=load_catalog,
        probe_backend=Backend(),
        clock=lambda: datetime(2026, 7, 28, 12, 1, tzinfo=UTC),
    )
    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=build.routing_ledger,
        runtime=build.routing_config,
        http_exception=fastapi.HTTPException,
        provider_probe_service=service,
    )
    return testclient.TestClient(app), build, service


def _create_profile(client: Any) -> None:
    response = client.post(
        "/api/routing/providers",
        json={
            "profile_id": "local",
            "display_name": "Local models",
            "adapter": "openai-compatible",
            "base_url": "http://127.0.0.1:1234/v1",
            "secret_ref": "secret://local-key",
            "locality": "local",
        },
    )
    assert response.status_code == 200


def test_discovery_creates_disabled_unconfirmed_target_drafts(tmp_path: Path) -> None:
    models = ["qwen-coder"]
    client, build, _service = _routing_app(tmp_path, models=models)
    try:
        _create_profile(client)
        response = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 1,
                "max_models": 2,
                "timeout_seconds": 2.0,
                "probe_capabilities": True,
            },
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["schema"] == "kestrel.routing.provider_discovery.v1"
        assert payload["created_draft_count"] == 1
        assert payload["stale_target_ids"] == []
        draft = payload["targets"][0]
        assert draft["enabled"] is False
        assert draft["trust_class"] == "unconfirmed"
        assert draft["supports_tools"] is True
        assert draft["supports_json"] is True
        assert draft["metadata"]["discovery"]["stale"] is False
        assert (
            draft["metadata"]["discovery"]["capabilities"]["generation"]["provenance"]
            == "observed"
        )
        assert "secret_ref" not in str(payload)
        assert "local-key" not in str(payload)

        stored = build.routing_ledger.get_provider_profile("local")
        assert stored is not None
        assert stored.revision == 3
        assert stored.profile.metadata["discovery"]["status"] == "complete"
        assert stored.profile.metadata["discovery"]["catalog_digest"] == payload["catalog_digest"]
        assert build.routing_ledger.list_model_targets(enabled_only=True) == []
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_removed_discovery_model_is_staled_and_excluded(tmp_path: Path) -> None:
    models = ["qwen-coder"]
    client, build, _service = _routing_app(tmp_path, models=models)
    try:
        _create_profile(client)
        first = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 1,
                "probe_capabilities": False,
            },
        )
        assert first.status_code == 200
        target_id = first.json()["targets"][0]["target_id"]

        target = build.routing_ledger.get_model_target(target_id)
        assert target is not None
        build.routing_ledger.put_model_target(
            target.target.__class__(
                **{
                    **target.target.__dict__,
                    "enabled": True,
                    "trust_class": "operator_confirmed",
                    "health": "healthy",
                }
            ),
            expected_revision=target.revision,
        )

        models.clear()
        second = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 3,
                "probe_capabilities": False,
            },
        )

        assert second.status_code == 200
        assert second.json()["stale_target_ids"] == [target_id]
        stale = build.routing_ledger.get_model_target(target_id)
        assert stale is not None
        assert stale.target.enabled is False
        assert stale.target.health == "unavailable"
        assert stale.target.metadata["discovery"]["stale"] is True
        assert build.routing_ledger.list_model_targets(enabled_only=True) == []
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_discovery_request_is_strict_and_revision_bound(tmp_path: Path) -> None:
    models = ["qwen-coder"]
    client, build, _service = _routing_app(tmp_path, models=models)
    try:
        _create_profile(client)
        unknown = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 1,
                "timeout_seconds": 20,
                "raw_api_key": "must-not-be-accepted",
            },
        )
        assert unknown.status_code == 422

        stale_revision = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 99,
            },
        )
        assert stale_revision.status_code == 409

        presets = client.get("/api/routing/discovery/presets")
        assert presets.status_code == 200
        assert all(item["can_enable_targets"] is False for item in presets.json()["presets"])
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_failed_catalog_refresh_does_not_stale_or_mutate_inventory(tmp_path: Path) -> None:
    models = ["fallback-model"]
    client, build, _service = _routing_app(
        tmp_path,
        models=models,
        catalog_ok=False,
    )
    try:
        _create_profile(client)
        response = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 1,
            },
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "provider_catalog_unavailable"
        profile = build.routing_ledger.get_provider_profile("local")
        assert profile is not None
        assert profile.revision == 1
        assert build.routing_ledger.list_model_targets() == []
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_manual_capability_claim_is_stored_as_operator_supplied(tmp_path: Path) -> None:
    client, build, _service = _routing_app(tmp_path, models=["qwen-coder"])
    try:
        _create_profile(client)
        response = client.post(
            "/api/routing/targets",
            json={
                "target_id": "manual-qwen",
                "provider_profile_id": "local",
                "provider": "openai-compatible",
                "model": "qwen-coder",
                "locality": "local",
                "supports_tools": True,
            },
        )

        assert response.status_code == 200
        evidence = response.json()["metadata"]["capability_evidence"]
        assert evidence["tools"]["provenance"] == "operator_supplied"
        assert "streaming" not in evidence
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()
