from __future__ import annotations

import json
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


def test_generic_http_routes_reject_reserved_lan_ids_and_metadata(
    tmp_path: Path,
) -> None:
    client, build, _state = _routing_app(tmp_path)
    try:
        reserved = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "lan-provider-" + "1" * 64,
                "display_name": "forged LAN provider",
                "adapter": "lan-openai-compatible",
                "locality": "local",
                "expected_revision": 0,
            },
        )
        assert reserved.status_code == 409
        assert build.routing_ledger.list_provider_profiles() == []
        malformed_reserved = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "lan-provider-not-a-digest",
                "display_name": "forged LAN prefix",
                "adapter": "mock",
                "expected_revision": 0,
            },
        )
        assert malformed_reserved.status_code == 409
        assert build.routing_ledger.list_provider_profiles() == []
        cross_reserved = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "lan-target-" + "1" * 64,
                "display_name": "cross-prefix forged provider",
                "adapter": "mock",
                "expected_revision": 0,
            },
        )
        assert cross_reserved.status_code == 409
        assert build.routing_ledger.list_provider_profiles() == []

        metadata = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "ordinary-provider",
                "display_name": "forged metadata",
                "adapter": "mock",
                "metadata": {"nested": {"lan_discovery": {"managed": True}}},
                "expected_revision": 0,
            },
        )
        assert metadata.status_code == 409
        assert build.routing_ledger.list_provider_profiles() == []

        assert (
            client.post(
                "/api/routing/providers",
                json={
                    "profile_id": "ordinary-target-profile",
                    "display_name": "ordinary",
                    "adapter": "mock",
                },
            ).status_code
            == 200
        )
        reserved_target = client.post(
            "/api/routing/targets",
            json={
                "target_id": "lan-target-" + "2" * 64,
                "provider_profile_id": "ordinary-target-profile",
                "provider": "mock",
                "model": "forged",
                "expected_revision": 0,
            },
        )
        assert reserved_target.status_code == 409
        malformed_reserved_target = client.post(
            "/api/routing/targets",
            json={
                "target_id": "lan-target-not-a-digest",
                "provider_profile_id": "ordinary-target-profile",
                "provider": "mock",
                "model": "forged",
                "expected_revision": 0,
            },
        )
        assert malformed_reserved_target.status_code == 409
        cross_reserved_target = client.post(
            "/api/routing/targets",
            json={
                "target_id": "lan-provider-" + "2" * 64,
                "provider_profile_id": "ordinary-target-profile",
                "provider": "mock",
                "model": "cross-prefix-forged",
                "expected_revision": 0,
            },
        )
        assert cross_reserved_target.status_code == 409
        cross_reserved_provider = client.post(
            "/api/routing/targets",
            json={
                "target_id": "ordinary-cross-prefix-target",
                "provider_profile_id": "lan-target-" + "3" * 64,
                "provider": "mock",
                "model": "cross-prefix-provider",
                "expected_revision": 0,
            },
        )
        assert cross_reserved_provider.status_code == 409
        nested_target = client.post(
            "/api/routing/targets",
            json={
                "target_id": "ordinary-looking-target",
                "provider_profile_id": "ordinary-target-profile",
                "provider": "mock",
                "model": "forged",
                "metadata": {"outer": {"lan_discovery": {"managed": True}}},
                "expected_revision": 0,
            },
        )
        assert nested_target.status_code == 409
        assert build.routing_ledger.list_model_targets() == []
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


@pytest.mark.parametrize("expected_revision", [False, True, 0.0, "0"])
def test_generic_http_revision_fields_are_strict_integers(
    tmp_path: Path,
    expected_revision: object,
) -> None:
    client, build, _state = _routing_app(tmp_path)
    try:
        response = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "strict-provider",
                "display_name": "strict provider",
                "adapter": "mock",
                "expected_revision": expected_revision,
            },
        )
        assert response.status_code == 422
        assert build.routing_ledger.get_provider_profile("strict-provider") is None
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_generic_http_route_cannot_mutate_existing_lan_managed_profile(
    tmp_path: Path,
) -> None:
    client, build, state = _routing_app(tmp_path)
    profile_id = "lan-provider-malformed-existing-row"
    metadata = {
        "lan_discovery": {
            "schema": "kestrel.lan.provider-profile.v1",
            "managed": True,
        }
    }
    with state._connect() as connection:
        connection.execute(
            """
            INSERT INTO routing_provider_profiles (
                profile_id, display_name, adapter, base_url, secret_ref, enabled,
                locality, trust_class, max_concurrency, metadata_json, revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 0, 'local', 'unconfirmed', 1, ?, 1, ?, ?)
            """,
            (
                profile_id,
                "LAN managed",
                "lan-openai-compatible",
                "http://192.168.50.2:1234/v1",
                json.dumps(
                    metadata,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "2026-08-01T12:00:00Z",
                "2026-08-01T12:00:00Z",
            ),
        )
    before = build.routing_ledger.get_provider_profile(profile_id)
    assert before is not None
    try:
        response = client.post(
            "/api/routing/providers",
            json={
                "profile_id": profile_id,
                "display_name": "generic overwrite",
                "adapter": "lan-openai-compatible",
                "base_url": "http://192.168.50.2:1234/v1",
                "enabled": True,
                "locality": "local",
                "trust_class": "operator_confirmed",
                "metadata": metadata,
                "expected_revision": before.revision,
            },
        )
        assert response.status_code == 409
        assert build.routing_ledger.get_provider_profile(profile_id) == before
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


@pytest.mark.parametrize("expected_revision", [False, True, -1, 0.0, "0"])
def test_generic_target_http_revision_is_a_strict_nonnegative_int(
    tmp_path: Path,
    expected_revision: object,
) -> None:
    client, build, _state = _routing_app(tmp_path)
    try:
        assert (
            client.post(
                "/api/routing/providers",
                json={
                    "profile_id": "strict-target-profile",
                    "display_name": "ordinary",
                    "adapter": "mock",
                },
            ).status_code
            == 200
        )
        response = client.post(
            "/api/routing/targets",
            json={
                "target_id": "strict-target",
                "provider_profile_id": "strict-target-profile",
                "provider": "mock",
                "model": "strict-model",
                "expected_revision": expected_revision,
            },
        )
        assert response.status_code == 422
        assert build.routing_ledger.get_model_target("strict-target") is None
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_generic_target_http_fences_existing_managed_target_but_not_ordinary(
    tmp_path: Path,
) -> None:
    client, build, state = _routing_app(tmp_path)
    profile_id = "managed-target-container"
    target_id = "lan-target-malformed-existing-row"
    try:
        assert (
            client.post(
                "/api/routing/providers",
                json={
                    "profile_id": profile_id,
                    "display_name": "ordinary container",
                    "adapter": "mock",
                },
            ).status_code
            == 200
        )
        protected = {
            "lan_discovery": {
                "schema": "kestrel.lan.model-target-binding.v1",
                "managed": True,
            }
        }
        with state._connect() as connection:
            connection.execute(
                """
                INSERT INTO routing_model_targets (
                    target_id, provider_profile_id, provider, model, enabled, locality,
                    trust_class, capability_tags_json, role_affinities_json,
                    task_family_affinities_json, max_context_tokens, supports_tools,
                    supports_json, supports_vision, supports_reasoning, supports_streaming,
                    quality_tier, latency_tier, operator_priority, estimated_cost_usd,
                    input_cost_per_million_usd, output_cost_per_million_usd, health,
                    recent_failure_rate, predicted_success, metadata_json, revision,
                    created_at, updated_at
                ) VALUES (
                    ?, ?, 'mock', 'managed-model', 0, 'cloud', 'unconfirmed', '[]',
                    '[]', '[]', NULL, 0, 0, 0, 0, 0, 1, 3, 0, NULL, NULL, NULL,
                    'unknown', 0.0, NULL, ?, 1, ?, ?
                )
                """,
                (
                    target_id,
                    profile_id,
                    json.dumps(protected, sort_keys=True, separators=(",", ":")),
                    "2026-08-01T12:00:00Z",
                    "2026-08-01T12:00:00Z",
                ),
            )
        before = build.routing_ledger.get_model_target(target_id)
        assert before is not None
        rejected = client.post(
            "/api/routing/targets",
            json={
                "target_id": target_id,
                "provider_profile_id": profile_id,
                "provider": "mock",
                "model": "managed-model",
                "enabled": True,
                "trust_class": "operator_confirmed",
                "metadata": protected,
                "expected_revision": before.revision,
            },
        )
        assert rejected.status_code == 409
        assert build.routing_ledger.get_model_target(target_id) == before

        ordinary = client.post(
            "/api/routing/targets",
            json={
                "target_id": "ordinary-control-target",
                "provider_profile_id": profile_id,
                "provider": "mock",
                "model": "ordinary-model",
                "expected_revision": 0,
            },
        )
        assert ordinary.status_code == 200
        ordinary_update = client.post(
            "/api/routing/targets",
            json={
                "target_id": "ordinary-control-target",
                "provider_profile_id": profile_id,
                "provider": "mock",
                "model": "ordinary-model",
                "enabled": False,
                "expected_revision": 1,
            },
        )
        assert ordinary_update.status_code == 200
        assert ordinary_update.json()["revision"] == 2
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()
