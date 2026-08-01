from __future__ import annotations

import json
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
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.ledger_records import (
    ModelTargetEntry,
    ProviderProfileEntry,
)
from nested_memvid_agent.routing.models import (
    AgentTaskContract,
    ModelTarget,
    ProviderProfile,
)
from nested_memvid_agent.routing.router import RoutingUnavailableError, route_task
from nested_memvid_agent.routing.runtime import AdaptiveFlockRuntimeConfig, build_run_manager
from nested_memvid_agent.server import create_app
from nested_memvid_agent.server_routing_routes import register_routing_routes
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore

LAN_OWNER = "owner:local-runtime:v1"


def _real_uvicorn_requests(
    app: Any,
    requests: tuple[tuple[str, str, bytes, dict[str, str]], ...],
) -> tuple[tuple[tuple[int, dict[str, str], bytes], ...], str]:
    import http.client
    import io
    import logging
    import socket
    import threading
    import time

    uvicorn = pytest.importorskip("uvicorn")
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            http="h11",
            lifespan="on",
            log_config=None,
            access_log=True,
        )
    )
    access_logger = logging.getLogger("uvicorn.access")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    previous_level = access_logger.level
    previous_disabled = access_logger.disabled
    access_logger.addHandler(handler)
    access_logger.setLevel(logging.INFO)
    access_logger.disabled = False
    failures: list[BaseException] = []
    results: list[tuple[int, dict[str, str], bytes]] = []

    def run_server() -> None:
        try:
            server.run(sockets=[listener])
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    thread = threading.Thread(target=run_server, name="lan-access-log-test-server")
    try:
        thread.start()
        deadline = time.monotonic() + 5.0
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started and thread.is_alive()
        for method, target, body, headers in requests:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
            try:
                connection.request(method, target, body=body, headers=headers)
                response = connection.getresponse()
                response_body = response.read()
                results.append(
                    (
                        response.status,
                        {name.lower(): value for name, value in response.getheaders()},
                        response_body,
                    )
                )
            finally:
                connection.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10.0)
        listener.close()
        access_logger.removeHandler(handler)
        access_logger.setLevel(previous_level)
        access_logger.disabled = previous_disabled
        handler.close()

    assert not thread.is_alive()
    assert failures == []
    return tuple(results), output.getvalue()


def _typed_lan_result_entries(
    *,
    reviewed: bool,
) -> tuple[ProviderProfileEntry, ModelTargetEntry]:
    provider_id = "lan-provider-" + "1" * 64
    target_id = "lan-target-" + "2" * 64
    timestamp = "2026-08-01T12:00:00Z"
    profile = ProviderProfileEntry(
        profile=ProviderProfile(
            profile_id=provider_id,
            display_name="Private LAN model server",
            adapter="lan-openai-compatible",
            base_url="http://raw-evidence-secret.invalid/v1",
            secret_ref="secret://raw-provider-secret",
            enabled=False,
            locality="local",
            trust_class=("operator_confirmed" if reviewed else "unconfirmed"),
            metadata={"lan_discovery": {"managed": True}},
        ),
        revision=2 if reviewed else 1,
        created_at=timestamp,
        updated_at=timestamp,
    )
    target = ModelTargetEntry(
        target=ModelTarget(
            target_id=target_id,
            provider_profile_id=provider_id,
            provider="lan-openai-compatible",
            model="alpha",
            enabled=False,
            locality="local",
            trust_class=("operator_confirmed" if reviewed else "unconfirmed"),
            capability_tags=("generation",),
            role_affinities=("worker",) if reviewed else (),
            task_family_affinities=("code-repair",) if reviewed else (),
            health="unknown",
            metadata={
                "lan_discovery": {
                    "managed": True,
                    "reviewed": reviewed,
                }
            },
        ),
        revision=2 if reviewed else 1,
        created_at=timestamp,
        updated_at=timestamp,
    )
    return profile, target


def _routing_app(
    tmp_path: Path,
    *,
    models: list[str],
    catalog_ok: bool = True,
    catalog_complete: list[bool] | None = None,
    probe_evidence: list[CapabilityEvidence] | None = None,
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
            catalog_complete=(catalog_complete[0] if catalog_complete is not None else True),
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
                capabilities=tuple(
                    probe_evidence
                    if probe_evidence is not None
                    else (
                        CapabilityEvidence.observed_pass("generation"),
                        CapabilityEvidence.observed_pass("streaming"),
                        CapabilityEvidence.observed_pass("structured_output"),
                        CapabilityEvidence.observed_pass("tools"),
                    )
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


@pytest.mark.parametrize("managed_by", ["reserved_id", "nested_metadata"])
def test_generic_discovery_fences_lan_profile_before_any_probe_or_write(
    tmp_path: Path,
    managed_by: str,
) -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = RoutingLedger(state)
    profile_id = (
        "lan-target-" + "a" * 64 if managed_by == "reserved_id" else "ordinary-managed-profile"
    )
    metadata = (
        {"purpose": "reserved-id probe fence"}
        if managed_by == "reserved_id"
        else {"nested": {"lan-discovery": {"managed": True}}}
    )
    with state._connect() as connection:
        connection.execute(
            """
            INSERT INTO routing_provider_profiles (
                profile_id, display_name, adapter, base_url, secret_ref, enabled,
                locality, trust_class, max_concurrency, metadata_json, revision,
                created_at, updated_at
            ) VALUES (?, 'forged LAN profile', 'mock', NULL, NULL, 0,
                      'local', 'unconfirmed', 1, ?, 1, ?, ?)
            """,
            (
                profile_id,
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                "2026-08-01T12:00:00Z",
                "2026-08-01T12:00:00Z",
            ),
        )
    before = ledger.get_provider_profile(profile_id)
    assert before is not None
    calls: list[str] = []

    class NeverProbe:
        def discover(self, *_args: object, **_kwargs: object) -> object:
            calls.append("probe")
            raise AssertionError("managed LAN profile reached generic discovery")

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=ledger,
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
        provider_probe_service=NeverProbe(),  # type: ignore[arg-type]
    )
    with testclient.TestClient(app) as client:
        response = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": profile_id,
                "expected_profile_revision": before.revision,
                "max_models": 1,
                "timeout_seconds": 1.0,
                "probe_capabilities": False,
            },
        )
    assert response.status_code == 409
    assert calls == []
    assert ledger.get_provider_profile(profile_id) == before
    assert ledger.list_model_targets() == []


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
            draft["metadata"]["discovery"]["capabilities"]["generation"]["provenance"] == "observed"
        )
        assert "secret_ref" not in str(payload)
        assert "local-key" not in str(payload)

        stored = build.routing_ledger.get_provider_profile("local")
        assert stored is not None
        assert stored.revision == 2
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
                "expected_profile_revision": 2,
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


def test_operator_confirmation_preserves_discovery_provenance_when_omitted(
    tmp_path: Path,
) -> None:
    client, build, _service = _routing_app(tmp_path, models=["qwen-coder"])
    try:
        _create_profile(client)
        discovered = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 1,
                "probe_capabilities": False,
            },
        )
        assert discovered.status_code == 200
        draft = discovered.json()["targets"][0]

        confirmed = client.post(
            "/api/routing/targets",
            json={
                "target_id": draft["target_id"],
                "provider_profile_id": "local",
                "provider": "openai-compatible",
                "model": "qwen-coder",
                "enabled": True,
                "locality": "local",
                "trust_class": "operator_confirmed",
                "expected_revision": draft["revision"],
            },
        )

        assert confirmed.status_code == 200
        assert confirmed.json()["metadata"]["discovery"]["managed"] is True
        assert confirmed.json()["enabled"] is True
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_failed_reprobe_revokes_confirmed_target_health_and_capability_authority(
    tmp_path: Path,
) -> None:
    evidence = [
        CapabilityEvidence.observed_pass("generation"),
        CapabilityEvidence.observed_pass("streaming"),
        CapabilityEvidence.observed_pass("structured_output"),
        CapabilityEvidence.observed_pass("tools"),
    ]
    client, build, _service = _routing_app(
        tmp_path,
        models=["qwen-coder"],
        probe_evidence=evidence,
    )
    try:
        _create_profile(client)
        discovered = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 1,
                "probe_capabilities": True,
            },
        )
        assert discovered.status_code == 200
        draft = discovered.json()["targets"][0]

        confirmed = client.post(
            "/api/routing/targets",
            json={
                "target_id": draft["target_id"],
                "provider_profile_id": "local",
                "provider": "openai-compatible",
                "model": "qwen-coder",
                "enabled": True,
                "locality": "local",
                "trust_class": "operator_confirmed",
                "capability_tags": [
                    "frontier-review",
                    "generation",
                    "streaming",
                    "structured_output",
                    "tools",
                ],
                "role_affinities": ["reviewer"],
                "task_family_affinities": ["code-repair"],
                "supports_tools": True,
                "supports_json": True,
                "supports_vision": True,
                "supports_streaming": True,
                "operator_priority": 7,
                "estimated_cost_usd": 0.004,
                "health": "healthy",
                "expected_revision": draft["revision"],
            },
        )
        assert confirmed.status_code == 200

        evidence[:] = [
            CapabilityEvidence.observed_failure("generation", "probe timed out"),
            CapabilityEvidence.observed_failure("streaming", "probe timed out"),
            CapabilityEvidence.observed_failure("structured_output", "invalid response"),
            CapabilityEvidence.observed_failure("tools", "invalid tool call"),
            CapabilityEvidence.observed_failure("vision", "not observed"),
        ]
        refreshed = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 2,
                "probe_capabilities": True,
            },
        )

        assert refreshed.status_code == 200
        target_payload = refreshed.json()["targets"][0]
        assert target_payload["enabled"] is True
        assert target_payload["health"] == "unavailable"
        assert target_payload["supports_tools"] is False
        assert target_payload["supports_json"] is False
        assert target_payload["supports_vision"] is False
        assert target_payload["supports_streaming"] is False
        assert target_payload["capability_tags"] == ["frontier-review"]
        assert target_payload["trust_class"] == "operator_confirmed"
        assert target_payload["locality"] == "local"
        assert target_payload["estimated_cost_usd"] == 0.004
        assert target_payload["operator_priority"] == 7
        assert target_payload["role_affinities"] == ["reviewer"]
        assert target_payload["task_family_affinities"] == ["code-repair"]

        persisted = build.routing_ledger.get_model_target(target_payload["target_id"])
        assert persisted is not None
        with pytest.raises(RoutingUnavailableError) as raised:
            route_task(
                AgentTaskContract(
                    task_id="task-1",
                    run_id="run-1",
                    role="implementer",
                    task_family="code-repair",
                    objective="repair the failing test",
                    complexity=0.4,
                    ambiguity=0.2,
                    risk="low",
                ),
                [persisted.target],
            )
        assert "target_health_unavailable" in raised.value.reason_codes
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_incomplete_catalog_never_stales_a_missing_target(tmp_path: Path) -> None:
    models = ["qwen-coder"]
    complete = [True]
    client, build, _service = _routing_app(
        tmp_path,
        models=models,
        catalog_complete=complete,
    )
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
        complete[0] = False
        refresh = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 2,
                "probe_capabilities": False,
            },
        )

        assert refresh.status_code == 200
        assert refresh.json()["catalog_complete"] is False
        assert refresh.json()["stale_target_ids"] == []
        preserved = build.routing_ledger.get_model_target(target_id)
        assert preserved is not None
        assert preserved.target.enabled is True
        assert preserved.target.health == "healthy"
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_inventory_apply_rolls_back_atomically_on_injected_crash_and_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nested_memvid_agent.routing.ledger_registry as ledger_registry

    models = ["qwen-coder"]
    client, build, _service = _routing_app(tmp_path, models=models)
    try:
        _create_profile(client)

        def crash() -> None:
            raise RuntimeError("injected inventory crash")

        monkeypatch.setattr(ledger_registry, "_before_inventory_commit", crash)
        with pytest.raises(RuntimeError, match="injected inventory crash"):
            client.post(
                "/api/routing/discovery",
                json={
                    "provider_profile_id": "local",
                    "expected_profile_revision": 1,
                    "probe_capabilities": False,
                },
            )

        restarted = RoutingLedger(build.routing_ledger.state)
        profile = restarted.get_provider_profile("local")
        assert profile is not None
        assert profile.revision == 1
        assert "discovery" not in profile.profile.metadata
        assert restarted.list_model_targets() == []

        monkeypatch.setattr(ledger_registry, "_before_inventory_commit", lambda: None)
        retry = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "local",
                "expected_profile_revision": 1,
                "probe_capabilities": False,
            },
        )
        assert retry.status_code == 200
        assert retry.json()["provider_profile_revision"] == 2
        assert len(restarted.list_model_targets()) == 1
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_create_app_discovery_resolves_broker_secret_without_exposing_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testclient = pytest.importorskip("fastapi.testclient")
    raw_secret = "broker-only-provider-secret"
    observed_api_keys: list[str | None] = []

    def fake_fetch_json(_url: str, **kwargs: Any) -> Any:
        observed_api_keys.append(kwargs.get("api_key"))
        return {"data": [{"id": "private-local-model"}]}

    monkeypatch.setattr(
        "nested_memvid_agent.llm.model_catalog._fetch_json",
        fake_fetch_json,
    )
    config = AgentConfig(
        provider="mock",
        model="mock",
        state_path=tmp_path / "state" / "agent.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        secret_store_path=tmp_path / "secrets" / "vault.json",
        require_api_auth=False,
    )

    with testclient.TestClient(create_app(config)) as client:
        stored = client.post(
            "/api/secrets",
            json={
                "id": "provider-key",
                "name": "Provider key",
                "purpose": "routing discovery",
                "value": raw_secret,
            },
        )
        assert stored.status_code == 200
        assert raw_secret not in stored.text

        profile = client.post(
            "/api/routing/providers",
            json={
                "profile_id": "private-local",
                "display_name": "Private local",
                "adapter": "openai-compatible",
                "base_url": "http://127.0.0.1:1234/v1",
                "secret_ref": "secret://provider-key",
                "locality": "local",
            },
        )
        assert profile.status_code == 200
        discovery = client.post(
            "/api/routing/discovery",
            json={
                "provider_profile_id": "private-local",
                "expected_profile_revision": 1,
                "probe_capabilities": False,
            },
        )

    assert discovery.status_code == 200
    assert observed_api_keys == [raw_secret]
    assert raw_secret not in discovery.text
    assert "provider-key" not in discovery.text


def test_lan_mutation_routes_require_service_and_fixed_principal_as_a_pair(
    tmp_path: Path,
) -> None:
    fastapi = pytest.importorskip("fastapi")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = RoutingLedger(state)

    with pytest.raises(ValueError, match="both|pair"):
        register_routing_routes(
            fastapi.FastAPI(),
            ledger=ledger,
            runtime=AdaptiveFlockRuntimeConfig(),
            http_exception=fastapi.HTTPException,
            lan_discovery_service=object(),
        )
    with pytest.raises(ValueError, match="both|pair"):
        register_routing_routes(
            fastapi.FastAPI(),
            ledger=ledger,
            runtime=AdaptiveFlockRuntimeConfig(),
            http_exception=fastapi.HTTPException,
            lan_owner_principal="owner:local-runtime:v1",
        )
    with pytest.raises(ValueError, match="fixed local-runtime owner"):
        register_routing_routes(
            fastapi.FastAPI(),
            ledger=ledger,
            runtime=AdaptiveFlockRuntimeConfig(),
            http_exception=fastapi.HTTPException,
            lan_discovery_service=object(),
            lan_owner_principal="owner:arbitrary",
        )


def test_lan_mutation_routes_are_absent_when_authenticated_context_is_omitted(
    tmp_path: Path,
) -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=RoutingLedger(state),
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
    )

    with testclient.TestClient(app) as client:
        assert client.post("/api/routing/lan/import", json={}).status_code == 404
        assert (
            client.post(
                "/api/routing/lan/targets/anything/review",
                json={},
            ).status_code
            == 404
        )


def test_lan_import_route_uses_only_fixed_owner_and_rejects_aliases_and_query(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.lan_discovery_service import LanDiscoveryConflict

    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    calls: list[tuple[object, str]] = []

    class StubService:
        def import_observation(
            self,
            request: object,
            *,
            authenticated_owner_principal: str,
        ) -> object:
            calls.append((request, authenticated_owner_principal))
            raise LanDiscoveryConflict("lan_evidence_conflict")

        def review_lan_target(self, request: object, **kwargs: object) -> object:
            del request, kwargs
            raise AssertionError("review was not expected")

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=RoutingLedger(state),
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
        lan_discovery_service=StubService(),
        lan_owner_principal="owner:local-runtime:v1",
    )
    body = {
        "scan_id": "scan-1",
        "endpoint_binding_digest": "sha256:" + "1" * 64,
        "expected_terminal_receipt_digest": "sha256:" + "2" * 64,
        "expected_observation_digest": "sha256:" + "3" * 64,
        "expected_profile_revision": 0,
        "expected_target_revisions": [],
        "replacement": None,
    }

    with testclient.TestClient(app) as client:
        response = client.post(
            "/api/routing/lan/import",
            json=body,
            headers={"X-Ignored-Untrusted-Identity": "some-user"},
        )
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "lan_evidence_conflict"}}
        assert calls[-1][1] == "owner:local-runtime:v1"

        for alias in (
            "X-Kestrel-Owner-Principal",
            "X-Owner-Principal",
            "X-Authenticated-Principal",
        ):
            count = len(calls)
            rejected = client.post(
                "/api/routing/lan/import",
                json=body,
                headers={alias: "forged-owner"},
            )
            assert rejected.status_code in {400, 409, 422}
            assert "forged-owner" not in rejected.text
            assert len(calls) == count

        count = len(calls)
        rejected_query = client.post(
            "/api/routing/lan/import?owner_principal=forged-owner",
            json=body,
        )
        assert rejected_query.status_code in {400, 422}
        assert "forged-owner" not in rejected_query.text
        assert len(calls) == count


def test_lan_import_route_serializes_typed_success_without_raw_evidence(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.lan_discovery_service import (
        LanImportRequest,
        LanImportResult,
    )

    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    profile, target = _typed_lan_result_entries(reviewed=False)
    observation_digest = "sha256:" + "3" * 64
    endpoint_fingerprint = "sha256:" + "4" * 64
    invalidated_binding = "sha256:" + "5" * 64
    typed_result = LanImportResult(
        profile=profile,
        targets=(target,),
        observation_digest=observation_digest,
        endpoint_fingerprint=endpoint_fingerprint,
        outage_observed=False,
        affected_target_ids=(target.target.target_id,),
        invalidated_binding_digests=(invalidated_binding,),
        stale_reasons_by_target=((target.target.target_id, ("catalog_changed",)),),
    )
    calls: list[tuple[LanImportRequest, str]] = []

    class StubService:
        def import_observation(
            self,
            request: LanImportRequest,
            *,
            authenticated_owner_principal: str,
        ) -> LanImportResult:
            calls.append((request, authenticated_owner_principal))
            return typed_result

        def review_lan_target(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("review was not expected")

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=RoutingLedger(state),
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
        lan_discovery_service=StubService(),
        lan_owner_principal=LAN_OWNER,
    )
    body = {
        "scan_id": "scan-safe-success",
        "endpoint_binding_digest": observation_digest,
        "expected_terminal_receipt_digest": observation_digest,
        "expected_observation_digest": observation_digest,
        "expected_profile_revision": 0,
        "expected_target_revisions": [{"resource_id": target.target.target_id, "revision": 0}],
        "replacement": None,
    }

    with testclient.TestClient(app) as client:
        response = client.post(
            "/api/routing/lan/import",
            json=body,
            headers={"X-Ignored-Untrusted-Identity": "raw-forged-owner"},
        )

    assert response.status_code == 200
    assert len(calls) == 1
    request, owner = calls[0]
    assert type(request) is LanImportRequest
    assert request.scan_id == "scan-safe-success"
    assert owner == LAN_OWNER
    assert response.json() == {
        "profile": profile.to_public_payload(),
        "targets": [target.to_public_payload()],
        "observation_digest": observation_digest,
        "endpoint_fingerprint": endpoint_fingerprint,
        "outage_observed": False,
        "affected_target_ids": [target.target.target_id],
        "invalidated_binding_digests": [invalidated_binding],
        "stale_reasons_by_target": [
            {
                "target_id": target.target.target_id,
                "reasons": ["catalog_changed"],
            }
        ],
    }
    serialized = response.text
    assert "raw-evidence-secret" not in serialized
    assert "raw-provider-secret" not in serialized
    assert "raw-forged-owner" not in serialized
    assert "base_url" not in response.json()["profile"]
    assert "secret_ref" not in response.json()["profile"]


def test_lan_routes_reject_oversized_duplicate_or_raw_evidence_without_echo(
    tmp_path: Path,
) -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")

    class UnreachableService:
        def import_observation(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("invalid request reached service")

        def review_lan_target(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("invalid request reached service")

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=RoutingLedger(state),
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
        lan_discovery_service=UnreachableService(),
        lan_owner_principal="owner:local-runtime:v1",
    )
    hostile = "raw-secret-address-192.168.50.2"

    with testclient.TestClient(app) as client:
        extra = client.post(
            "/api/routing/lan/import",
            json={"scan_id": "scan", "address": hostile},
        )
        assert extra.status_code in {400, 422}
        assert hostile not in extra.text

        duplicate = client.post(
            "/api/routing/lan/import",
            content=(
                b'{"scan_id":"a","scan_id":"b",'
                b'"endpoint_binding_digest":"sha256:' + b"1" * 64 + b'",'
                b'"expected_terminal_receipt_digest":"sha256:' + b"2" * 64 + b'",'
                b'"expected_observation_digest":"sha256:' + b"3" * 64 + b'",'
                b'"expected_profile_revision":0,"expected_target_revisions":[]}'
            ),
            headers={"content-type": "application/json"},
        )
        assert duplicate.status_code in {400, 422}

        oversized = client.post(
            "/api/routing/lan/import",
            content=b"{" + b'"padding":"' + b"x" * (32 * 1024) + b'"}',
            headers={"content-type": "application/json"},
        )
        assert oversized.status_code == 413

        negative_length = client.post(
            "/api/routing/lan/import",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "content-length": "-1",
            },
        )
        assert negative_length.status_code == 400
        assert negative_length.json() == {"detail": {"code": "lan_request_rejected"}}

        deeply_nested = client.post(
            "/api/routing/lan/import",
            content=b"[" * 10_000 + b"0" + b"]" * 10_000,
            headers={"content-type": "application/json"},
        )
        assert deeply_nested.status_code == 400
        assert deeply_nested.json() == {"detail": {"code": "lan_request_invalid_json"}}


def test_lan_review_route_binds_path_target_and_fixed_owner_without_echo(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.lan_discovery_service import LanDiscoveryConflict

    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    calls: list[tuple[object, str]] = []

    class StubService:
        def import_observation(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("import was not expected")

        def review_lan_target(
            self,
            request: object,
            *,
            authenticated_owner_principal: str,
        ) -> object:
            calls.append((request, authenticated_owner_principal))
            raise LanDiscoveryConflict("lan_review_conflict")

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=RoutingLedger(state),
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
        lan_discovery_service=StubService(),
        lan_owner_principal="owner:local-runtime:v1",
    )
    target_id = "lan-target-" + "4" * 64
    body = {
        "expected_profile_revision": 1,
        "expected_target_revision": 1,
        "expected_terminal_receipt_digest": "sha256:" + "1" * 64,
        "expected_observation_digest": "sha256:" + "2" * 64,
        "expected_endpoint_fingerprint": "sha256:" + "3" * 64,
        "expected_material_binding_digest": "sha256:" + "4" * 64,
        "expected_review_digest": "sha256:" + "5" * 64,
        "expected_stale_reasons": [],
        "trust_class": "operator_confirmed",
        "intended_roles": ["worker"],
        "task_family_affinities": ["code-repair"],
        "privacy_acknowledged": True,
        "enabled": False,
    }

    with testclient.TestClient(app) as client:
        response = client.post(
            f"/api/routing/lan/targets/{target_id}/review",
            json=body,
            headers={"X-Ignored-Untrusted-Identity": "not-an-owner"},
        )
        assert response.status_code == 409
        assert response.json() == {"detail": {"code": "lan_review_conflict"}}
        request, principal = calls[-1]
        assert request.target_id == target_id
        assert principal == "owner:local-runtime:v1"

        count = len(calls)
        forged_target = client.post(
            f"/api/routing/lan/targets/{target_id}/review",
            json={**body, "target_id": "raw-secret-target"},
        )
        assert forged_target.status_code in {400, 422}
        assert "raw-secret-target" not in forged_target.text
        assert len(calls) == count

        for alias in (
            "X-Kestrel-Owner-Principal",
            "X-Owner-Principal",
            "X-Authenticated-Principal",
        ):
            rejected = client.post(
                f"/api/routing/lan/targets/{target_id}/review",
                json=body,
                headers={alias: "raw-forged-owner"},
            )
            assert rejected.status_code in {400, 409, 422}
            assert "raw-forged-owner" not in rejected.text
        assert len(calls) == count


def test_lan_review_route_serializes_typed_success_without_secret_echo(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.lan_discovery_service import (
        LanReviewRequest,
        LanReviewResult,
    )

    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    profile, target = _typed_lan_result_entries(reviewed=True)
    privacy_digest = "sha256:" + "6" * 64
    material_digest = "sha256:" + "7" * 64
    typed_result = LanReviewResult(
        profile=profile,
        target=target,
        privacy_acknowledgement_digest=privacy_digest,
        material_binding_digest=material_digest,
    )
    calls: list[tuple[LanReviewRequest, str]] = []

    class StubService:
        def import_observation(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("import was not expected")

        def review_lan_target(
            self,
            request: LanReviewRequest,
            *,
            authenticated_owner_principal: str,
        ) -> LanReviewResult:
            calls.append((request, authenticated_owner_principal))
            return typed_result

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=RoutingLedger(state),
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
        lan_discovery_service=StubService(),
        lan_owner_principal=LAN_OWNER,
    )
    digest = "sha256:" + "3" * 64
    body = {
        "expected_profile_revision": 1,
        "expected_target_revision": 1,
        "expected_terminal_receipt_digest": digest,
        "expected_observation_digest": digest,
        "expected_endpoint_fingerprint": digest,
        "expected_material_binding_digest": digest,
        "expected_review_digest": digest,
        "expected_stale_reasons": [],
        "trust_class": "operator_confirmed",
        "intended_roles": ["worker"],
        "task_family_affinities": ["code-repair"],
        "privacy_acknowledged": True,
        "enabled": False,
    }

    with testclient.TestClient(app) as client:
        response = client.post(
            f"/api/routing/lan/targets/{target.target.target_id}/review",
            json=body,
            headers={"X-Ignored-Untrusted-Identity": "raw-forged-owner"},
        )

    assert response.status_code == 200
    assert len(calls) == 1
    request, owner = calls[0]
    assert type(request) is LanReviewRequest
    assert request.target_id == target.target.target_id
    assert owner == LAN_OWNER
    assert response.json() == {
        "profile": profile.to_public_payload(),
        "target": target.to_public_payload(),
        "privacy_acknowledgement_digest": privacy_digest,
        "material_binding_digest": material_digest,
    }
    serialized = response.text
    assert "raw-evidence-secret" not in serialized
    assert "raw-provider-secret" not in serialized
    assert "raw-forged-owner" not in serialized
    assert "base_url" not in response.json()["profile"]
    assert "secret_ref" not in response.json()["profile"]


def test_lan_review_route_has_strict_bounded_duplicate_free_redacted_parity(
    tmp_path: Path,
) -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    calls: list[str] = []

    class UnreachableService:
        def import_observation(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            calls.append("import")
            raise AssertionError("invalid import reached service")

        def review_lan_target(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            calls.append("review")
            raise AssertionError("invalid review reached service")

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=RoutingLedger(state),
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
        lan_discovery_service=UnreachableService(),
        lan_owner_principal="owner:local-runtime:v1",
    )
    target_id = "lan-target-" + "6" * 64
    digest = "sha256:" + "1" * 64
    valid_review = {
        "expected_profile_revision": 1,
        "expected_target_revision": 1,
        "expected_terminal_receipt_digest": digest,
        "expected_observation_digest": digest,
        "expected_endpoint_fingerprint": digest,
        "expected_material_binding_digest": digest,
        "expected_review_digest": digest,
        "expected_stale_reasons": [],
        "trust_class": "operator_confirmed",
        "intended_roles": ["worker"],
        "task_family_affinities": ["code-repair"],
        "privacy_acknowledged": True,
        "enabled": False,
    }
    valid_import = {
        "scan_id": "scan-1",
        "endpoint_binding_digest": digest,
        "expected_terminal_receipt_digest": digest,
        "expected_observation_digest": digest,
        "expected_profile_revision": 0,
        "expected_target_revisions": [{"resource_id": target_id, "revision": 0}],
        "replacement": None,
    }
    hostile = "raw-evidence-secret-192.168.50.2"

    with testclient.TestClient(app) as client:
        query = client.post(
            f"/api/routing/lan/targets/{target_id}/review?principal={hostile}",
            json=valid_review,
        )
        assert query.status_code in {400, 422}
        assert hostile not in query.text

        for field in (
            "owner_principal",
            "authenticated_owner_principal",
            "lan_owner_principal",
            "address",
            "port",
            "url",
            "interface_id",
            "model_ids",
            "capabilities",
            "secret",
            "provider_adapter",
            "observation",
        ):
            response = client.post(
                f"/api/routing/lan/targets/{target_id}/review",
                json={**valid_review, field: hostile},
            )
            assert response.status_code in {400, 422}
            assert hostile not in response.text

        strict_review_cases = (
            {**valid_review, "expected_profile_revision": True},
            {**valid_review, "expected_target_revision": 1.0},
            {**valid_review, "expected_target_revision": "1"},
            {**valid_review, "expected_profile_revision": -1},
            {**valid_review, "privacy_acknowledged": 1},
            {**valid_review, "enabled": "false"},
            {**valid_review, "expected_review_digest": digest + "\n"},
            {**valid_review, "intended_roles": ["e\u0301"]},
            {**valid_review, "task_family_affinities": ["code\nrepair"]},
        )
        for body in strict_review_cases:
            assert (
                client.post(
                    f"/api/routing/lan/targets/{target_id}/review",
                    json=body,
                ).status_code
                == 422
            )

        strict_import_cases = (
            {**valid_import, "expected_profile_revision": False},
            {**valid_import, "expected_profile_revision": 0.0},
            {**valid_import, "expected_profile_revision": "0"},
            {**valid_import, "expected_profile_revision": -1},
            {**valid_import, "expected_observation_digest": digest + "\n"},
            {
                **valid_import,
                "expected_target_revisions": [{"resource_id": target_id, "revision": True}],
            },
            {
                **valid_import,
                "replacement": {
                    "provider_profile_id": "lan-provider-" + "1" * 64,
                    "expected_profile_revision": 1.0,
                    "expected_endpoint_fingerprint": digest,
                    "expected_material_binding_digests": [digest],
                },
            },
        )
        for body in strict_import_cases:
            assert client.post("/api/routing/lan/import", json=body).status_code == 422

        duplicate = client.post(
            f"/api/routing/lan/targets/{target_id}/review",
            content=(
                b'{"expected_profile_revision":1,"expected_profile_revision":2,'
                b'"expected_target_revision":1}'
            ),
            headers={"content-type": "application/json"},
        )
        assert duplicate.status_code in {400, 422}
        invalid_utf8 = client.post(
            f"/api/routing/lan/targets/{target_id}/review",
            content=b'{"invalid":"\xff"}',
            headers={"content-type": "application/json"},
        )
        assert invalid_utf8.status_code in {400, 422}
        for literal in (b"NaN", b"Infinity", b"-Infinity"):
            nonfinite = client.post(
                f"/api/routing/lan/targets/{target_id}/review",
                content=b'{"expected_profile_revision":' + literal + b"}",
                headers={"content-type": "application/json"},
            )
            assert nonfinite.status_code in {400, 422}
            assert literal.decode() not in nonfinite.text
        oversized = client.post(
            f"/api/routing/lan/targets/{target_id}/review",
            content=b"{" + b'"padding":"' + b"x" * (32 * 1024) + b'"}',
            headers={"content-type": "application/json"},
        )
        assert oversized.status_code == 413

    assert calls == []


def test_lan_routes_map_unknown_durable_resources_to_closed_404(tmp_path: Path) -> None:
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    state = AgentStateStore(tmp_path / "state" / "agent.db")

    class MissingService:
        def import_observation(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise KeyError("raw-secret-missing-scan")

        def review_lan_target(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise KeyError("raw-secret-missing-target")

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=RoutingLedger(state),
        runtime=AdaptiveFlockRuntimeConfig(),
        http_exception=fastapi.HTTPException,
        lan_discovery_service=MissingService(),
        lan_owner_principal="owner:local-runtime:v1",
    )
    target_id = "lan-target-" + "7" * 64
    digest = "sha256:" + "1" * 64
    import_body = {
        "scan_id": "missing-scan",
        "endpoint_binding_digest": digest,
        "expected_terminal_receipt_digest": digest,
        "expected_observation_digest": digest,
        "expected_profile_revision": 0,
        "expected_target_revisions": [],
        "replacement": None,
    }
    review_body = {
        "expected_profile_revision": 1,
        "expected_target_revision": 1,
        "expected_terminal_receipt_digest": digest,
        "expected_observation_digest": digest,
        "expected_endpoint_fingerprint": digest,
        "expected_material_binding_digest": digest,
        "expected_review_digest": digest,
        "expected_stale_reasons": [],
        "trust_class": "operator_confirmed",
        "intended_roles": [],
        "task_family_affinities": [],
        "privacy_acknowledged": True,
        "enabled": False,
    }

    with testclient.TestClient(app) as client:
        missing_import = client.post("/api/routing/lan/import", json=import_body)
        missing_review = client.post(
            f"/api/routing/lan/targets/{target_id}/review",
            json=review_body,
        )
    assert missing_import.status_code == 404
    assert missing_review.status_code == 404
    assert missing_import.json() == {"detail": {"code": "lan_resource_not_found"}}
    assert missing_review.json() == {"detail": {"code": "lan_resource_not_found"}}
    assert "raw-secret" not in missing_import.text
    assert "raw-secret" not in missing_review.text


def test_create_app_registers_lan_mutations_only_with_authenticated_ingress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    testclient = pytest.importorskip("fastapi.testclient")

    def config(root: Path, *, authenticated: bool) -> AgentConfig:
        return AgentConfig(
            provider="mock",
            model="mock",
            state_path=root / "state" / "agent.db",
            memory_dir=root / "memory",
            log_dir=root / "logs",
            workspace=root,
            skills_dir=root / "skills",
            plugins_dir=root / "plugins",
            secret_store_path=root / "secrets" / "vault.json",
            require_api_auth=authenticated,
            api_auth_token_env="KESTREL_LAN_ROUTE_TEST_TOKEN",
        )

    with testclient.TestClient(
        create_app(config(tmp_path / "unauthenticated", authenticated=False))
    ) as client:
        assert client.post("/api/routing/lan/import", json={}).status_code == 404
        assert (
            client.post(
                "/api/routing/lan/targets/missing/review",
                json={},
            ).status_code
            == 404
        )

    monkeypatch.setenv("KESTREL_LAN_ROUTE_TEST_TOKEN", "lan-route-test-token")
    with testclient.TestClient(
        create_app(config(tmp_path / "authenticated", authenticated=True))
    ) as client:
        unauthorized = client.post("/api/routing/lan/import", json={})
        assert unauthorized.status_code == 401
        unauthorized_review = client.post(
            "/api/routing/lan/targets/missing/review",
            json={},
        )
        assert unauthorized_review.status_code == 401
        authorized = client.post(
            "/api/routing/lan/import",
            json={},
            headers={"X-Kestrel-API-Key": "lan-route-test-token"},
        )
        assert authorized.status_code in {400, 422}
        assert authorized.status_code != 404
        authorized_review = client.post(
            "/api/routing/lan/targets/missing/review",
            json={},
            headers={"X-Kestrel-API-Key": "lan-route-test-token"},
        )
        assert authorized_review.status_code in {400, 422}
        assert authorized_review.status_code != 404


def test_unauthorized_lan_query_is_redacted_from_real_uvicorn_access_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_env = "KESTREL_LAN_ACCESS_LOG_TOKEN"
    monkeypatch.setenv(token_env, "lan-access-log-token")
    root = tmp_path / "access-log"
    app = create_app(
        AgentConfig(
            provider="mock",
            model="mock",
            state_path=root / "state" / "agent.db",
            memory_dir=root / "memory",
            log_dir=root / "logs",
            workspace=root,
            skills_dir=root / "skills",
            plugins_dir=root / "plugins",
            secret_store_path=root / "secrets" / "vault.json",
            require_api_auth=True,
            api_auth_token_env=token_env,
        )
    )
    hostile = "raw-secret-address-192.168.50.2"
    responses, access_record = _real_uvicorn_requests(
        app,
        (
            (
                "POST",
                f"/api/routing/lan/import?address={hostile}",
                b"{}",
                {"content-type": "application/json"},
            ),
        ),
    )

    assert responses[0][0] == 401
    assert "/api/routing/lan/import" in access_record
    assert hostile not in access_record


def test_invalid_lan_review_path_is_redacted_before_real_uvicorn_access_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nested_memvid_agent.lan_discovery_service import (
        LanDiscoveryService,
        LanReviewRequest,
    )

    token = "lan-path-redaction-token"
    token_env = "KESTREL_LAN_PATH_REDACTION_TOKEN"
    monkeypatch.setenv(token_env, token)
    calls: list[LanReviewRequest] = []

    def review_lan_target(
        _service: LanDiscoveryService,
        request: LanReviewRequest,
        *,
        authenticated_owner_principal: str,
    ) -> object:
        assert authenticated_owner_principal == LAN_OWNER
        calls.append(request)
        raise KeyError(request.target_id)

    monkeypatch.setattr(LanDiscoveryService, "review_lan_target", review_lan_target)
    root = tmp_path / "path-redaction"
    app = create_app(
        AgentConfig(
            provider="mock",
            model="mock",
            state_path=root / "state" / "agent.db",
            memory_dir=root / "memory",
            log_dir=root / "logs",
            workspace=root,
            skills_dir=root / "skills",
            plugins_dir=root / "plugins",
            secret_store_path=root / "secrets" / "vault.json",
            require_api_auth=True,
            api_auth_token_env=token_env,
        )
    )
    digest = "sha256:" + "8" * 64
    review_body = json.dumps(
        {
            "expected_profile_revision": 1,
            "expected_target_revision": 1,
            "expected_terminal_receipt_digest": digest,
            "expected_observation_digest": digest,
            "expected_endpoint_fingerprint": digest,
            "expected_material_binding_digest": digest,
            "expected_review_digest": digest,
            "expected_stale_reasons": [],
            "trust_class": "operator_confirmed",
            "intended_roles": [],
            "task_family_affinities": [],
            "privacy_acknowledged": True,
            "enabled": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    hostile = "raw-secret-address-192.168.50.2"
    valid_target_id = "lan-target-" + "8" * 64
    invalid_path = f"/api/routing/lan/targets/{hostile}/review"
    responses, access_record = _real_uvicorn_requests(
        app,
        (
            (
                "POST",
                invalid_path,
                review_body,
                {"content-type": "application/json"},
            ),
            (
                "POST",
                invalid_path,
                review_body,
                {
                    "content-type": "application/json",
                    "x-kestrel-api-key": token,
                },
            ),
            (
                "POST",
                f"/api/routing/lan/targets/{valid_target_id}/review",
                review_body,
                {
                    "content-type": "application/json",
                    "x-kestrel-api-key": token,
                },
            ),
        ),
    )

    assert tuple(status for status, _headers, _body in responses) == (401, 400, 404)
    assert [request.target_id for request in calls] == [valid_target_id]
    assert json.loads(responses[1][2]) == {"detail": {"code": "lan_request_rejected"}}
    assert "location" not in responses[1][1]
    assert hostile not in access_record
    for _status, headers, body in responses:
        assert hostile not in body.decode("utf-8")
        assert all(hostile not in value for value in headers.values())


def test_lan_query_is_closed_and_redacted_for_canonical_and_slash_alias_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from nested_memvid_agent.lan_discovery_service import LanDiscoveryService

    token = "lan-query-alias-token"
    token_env = "KESTREL_LAN_QUERY_ALIAS_TOKEN"
    monkeypatch.setenv(token_env, token)
    calls: list[str] = []

    def unreachable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        calls.append("service")
        raise AssertionError("query-bearing LAN request reached service")

    monkeypatch.setattr(LanDiscoveryService, "import_observation", unreachable)
    monkeypatch.setattr(LanDiscoveryService, "review_lan_target", unreachable)
    root = tmp_path / "query-alias"
    app = create_app(
        AgentConfig(
            provider="mock",
            model="mock",
            state_path=root / "state" / "agent.db",
            memory_dir=root / "memory",
            log_dir=root / "logs",
            workspace=root,
            skills_dir=root / "skills",
            plugins_dir=root / "plugins",
            secret_store_path=root / "secrets" / "vault.json",
            require_api_auth=True,
            api_auth_token_env=token_env,
        )
    )
    hostile = "raw-secret-address-192.168.50.2"
    target_id = "lan-target-" + "9" * 64
    paths = (
        "/api/routing/lan/import",
        "/api/routing/lan/import/",
        f"/api/routing/lan/targets/{target_id}/review",
        f"/api/routing/lan/targets/{target_id}/review/",
    )
    requests: list[tuple[str, str, bytes, dict[str, str]]] = []
    for path in paths:
        target = f"{path}?address={hostile}"
        requests.append(
            (
                "POST",
                target,
                b"{}",
                {"content-type": "application/json"},
            )
        )
        requests.append(
            (
                "POST",
                target,
                b"{}",
                {
                    "content-type": "application/json",
                    "x-kestrel-api-key": token,
                },
            )
        )
    responses, access_record = _real_uvicorn_requests(app, tuple(requests))

    assert tuple(status for status, _headers, _body in responses) == (
        401,
        400,
        401,
        400,
        401,
        400,
        401,
        400,
    )
    assert calls == []
    assert hostile not in access_record
    for index, (_status, headers, body) in enumerate(responses):
        assert hostile not in body.decode("utf-8")
        assert all(hostile not in value for value in headers.values())
        if index % 2 == 1:
            assert "location" not in headers
            assert json.loads(body) == {"detail": {"code": "lan_request_rejected"}}


@pytest.mark.parametrize(
    "request_path",
    (
        "/api/routing/lan/import",
        "/api/routing/lan/import/",
        "/api/routing/lan/targets/lan-target-" + "a" * 64 + "/review",
        "/api/routing/lan/targets/lan-target-" + "a" * 64 + "/review/",
    ),
)
def test_chunked_lan_body_honors_smaller_configured_global_limit_before_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_path: str,
) -> None:
    from nested_memvid_agent.lan_discovery_service import (
        LanDiscoveryService,
        LanImportRequest,
        LanImportResult,
    )

    testclient = pytest.importorskip("fastapi.testclient")
    token = "lan-chunked-limit-token"
    token_env = "KESTREL_LAN_CHUNKED_LIMIT_TOKEN"
    monkeypatch.setenv(token_env, token)
    calls: list[LanImportRequest] = []
    profile, target = _typed_lan_result_entries(reviewed=False)
    digest = "sha256:" + "7" * 64
    result = LanImportResult(
        profile=profile,
        targets=(target,),
        affected_target_ids=(target.target.target_id,),
        invalidated_binding_digests=(),
        stale_reasons_by_target=(),
        observation_digest=digest,
        endpoint_fingerprint=digest,
        outage_observed=False,
    )

    def import_observation(
        _service: LanDiscoveryService,
        request: LanImportRequest,
        *,
        authenticated_owner_principal: str,
    ) -> LanImportResult:
        assert authenticated_owner_principal == LAN_OWNER
        calls.append(request)
        return result

    monkeypatch.setattr(LanDiscoveryService, "import_observation", import_observation)
    root = tmp_path / "chunked-limit"
    app = create_app(
        AgentConfig(
            provider="mock",
            model="mock",
            state_path=root / "state" / "agent.db",
            memory_dir=root / "memory",
            log_dir=root / "logs",
            workspace=root,
            skills_dir=root / "skills",
            plugins_dir=root / "plugins",
            secret_store_path=root / "secrets" / "vault.json",
            require_api_auth=True,
            api_auth_token_env=token_env,
            max_request_body_bytes=80,
        )
    )
    body = json.dumps(
        {
            "scan_id": "scan-chunked-limit",
            "endpoint_binding_digest": digest,
            "expected_terminal_receipt_digest": digest,
            "expected_observation_digest": digest,
            "expected_profile_revision": 0,
            "expected_target_revisions": [],
            "replacement": None,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    assert 80 < len(body) < 32 * 1024

    def chunked_body():
        yield body[:64]
        yield body[64:]

    with testclient.TestClient(app) as client:
        response = client.post(
            request_path,
            content=chunked_body(),
            follow_redirects=False,
            headers={
                "content-type": "application/json",
                "x-kestrel-api-key": token,
            },
        )

    assert response.status_code == 413
    assert response.json() == {"detail": {"code": "lan_request_too_large"}}
    assert calls == []


@pytest.mark.parametrize(
    "request_path",
    (
        "/api/routing/lan/import/",
        "/api/routing/lan/targets/lan-target-" + "b" * 64 + "/review/",
    ),
)
def test_chunked_lan_slash_alias_honors_32k_ceiling_before_redirect_or_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request_path: str,
) -> None:
    from nested_memvid_agent.lan_discovery_service import LanDiscoveryService

    testclient = pytest.importorskip("fastapi.testclient")
    token = "lan-alias-ceiling-token"
    token_env = "KESTREL_LAN_ALIAS_CEILING_TOKEN"
    monkeypatch.setenv(token_env, token)
    calls: list[str] = []

    def unreachable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        calls.append("service")
        raise AssertionError("oversized LAN alias request reached service")

    monkeypatch.setattr(LanDiscoveryService, "import_observation", unreachable)
    monkeypatch.setattr(LanDiscoveryService, "review_lan_target", unreachable)
    root = tmp_path / "alias-ceiling"
    app = create_app(
        AgentConfig(
            provider="mock",
            model="mock",
            state_path=root / "state" / "agent.db",
            memory_dir=root / "memory",
            log_dir=root / "logs",
            workspace=root,
            skills_dir=root / "skills",
            plugins_dir=root / "plugins",
            secret_store_path=root / "secrets" / "vault.json",
            require_api_auth=True,
            api_auth_token_env=token_env,
            max_request_body_bytes=64 * 1024,
        )
    )
    body = b"x" * (32 * 1024 + 1)

    def chunked_body():
        yield body[:16_384]
        yield body[16_384:]

    with testclient.TestClient(app) as client:
        response = client.post(
            request_path,
            content=chunked_body(),
            follow_redirects=False,
            headers={
                "content-type": "application/json",
                "x-kestrel-api-key": token,
            },
        )

    assert response.status_code == 413
    assert response.json() == {"detail": {"code": "lan_request_too_large"}}
    assert "location" not in response.headers
    assert calls == []


@pytest.mark.parametrize(
    "request_path",
    (
        "/api/routing/lan/import",
        "/api/routing/lan/import/",
        "/api/routing/lan/targets/lan-target-" + "c" * 64 + "/review",
        "/api/routing/lan/targets/lan-target-" + "c" * 64 + "/review/",
    ),
)
def test_unregistered_lan_shaped_paths_use_ordinary_global_body_ingress_then_404(
    tmp_path: Path,
    request_path: str,
) -> None:
    testclient = pytest.importorskip("fastapi.testclient")
    root = tmp_path / "unregistered-body"
    app = create_app(
        AgentConfig(
            provider="mock",
            model="mock",
            state_path=root / "state" / "agent.db",
            memory_dir=root / "memory",
            log_dir=root / "logs",
            workspace=root,
            skills_dir=root / "skills",
            plugins_dir=root / "plugins",
            secret_store_path=root / "secrets" / "vault.json",
            require_api_auth=False,
            max_request_body_bytes=64 * 1024,
        )
    )
    body = b"x" * (32 * 1024 + 1)

    def chunked_body():
        yield body[:16_384]
        yield body[16_384:]

    with testclient.TestClient(app) as client:
        response = client.post(
            request_path,
            content=chunked_body(),
            follow_redirects=False,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert "location" not in response.headers


def test_create_app_desktop_launch_registers_lan_mutations_behind_launch_auth(
    tmp_path: Path,
) -> None:
    import test_server_desktop_routes as desktop_cases

    testclient = pytest.importorskip("fastapi.testclient")
    profile_root = tmp_path / "desktop-profile"
    profile_root.mkdir()
    launch = desktop_cases._desktop_context(profile_root)
    app = create_app(
        desktop_cases._config(profile_root),
        desktop_context=launch,
    )

    with testclient.TestClient(app) as client:
        unauthorized_import = client.post("/api/routing/lan/import", json={})
        unauthorized_review = client.post(
            "/api/routing/lan/targets/missing/review",
            json={},
        )
        assert unauthorized_import.status_code == 401
        assert unauthorized_review.status_code == 401

        headers = {"Authorization": f"Bearer {launch.api_token}"}
        authorized_import = client.post(
            "/api/routing/lan/import",
            json={},
            headers=headers,
        )
        authorized_review = client.post(
            "/api/routing/lan/targets/missing/review",
            json={},
            headers=headers,
        )
        assert authorized_import.status_code in {400, 422}
        assert authorized_review.status_code in {400, 422}
        assert authorized_import.status_code != 404
        assert authorized_review.status_code != 404
