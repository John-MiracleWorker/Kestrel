"""S6 — shadow verdict API and Workbench/Mission evidence (SHADOW-005).

Covers the backward-compatible routing APIs that expose the zero-authority
shadow observation rows persisted by S5, and the honest authority/verdict
vocabulary the Workbench/Mission Control UI reads to distinguish deterministic,
shadow, activated, and suspended-fallback states.

* SHADOW-005 — make routing evidence and authority inspectable: additive
  read-only endpoints (``/api/runs/{run_id}/routing`` gains
  ``shadow_observations``; new ``/api/routing/shadow-observations``) that
  surface explicit authority, evidence basis, observational verdict, and
  missing terminal data without fabricating counterfactual proof.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.routing.shadow_observation import (
    ActualAuthority,
    ShadowVerdict,
)
from nested_memvid_agent.server_routing_routes import register_routing_routes
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord


def _state_and_task(tmp_path: Path) -> tuple[AgentStateStore, TaskNodeRecord]:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    state.create_run(
        run_id="run-shadow-api",
        message="Inspect the repository",
        session_id="session-shadow-api",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = state.create_task_node(
        task_id="task-shadow-api",
        run_id="run-shadow-api",
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=("repo.search", "repo.map"),
        risk="low",
        acceptance_criteria=(),
    )
    return state, task


def _profile(profile_id: str = "local") -> ProviderProfile:
    return ProviderProfile(
        profile_id=profile_id,
        display_name=f"Profile {profile_id}",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        secret_ref=f"secret://routing-{profile_id}-key",
        locality="local",
    )


def _target(target_id: str, *, model: str = "model-a") -> ModelTarget:
    return ModelTarget(
        target_id=target_id,
        provider_profile_id="local",
        provider="openai-compatible",
        model=model,
        locality="local",
        capability_tags=("repository_inspection", "scout", "worker"),
        role_affinities=("worker",),
        task_family_affinities=("repository_inspection",),
        max_context_tokens=64_000,
        supports_tools=True,
        supports_json=True,
        supports_reasoning=True,
        quality_tier=3,
        latency_tier=1,
        estimated_cost_usd=0.0,
        health="healthy",
    )


def _configured_ledger(state: AgentStateStore) -> RoutingLedger:
    ledger = RoutingLedger(state)
    ledger.put_provider_profile(_profile())
    ledger.put_model_target(_target("local-scout"))
    ledger.put_policy(RoutePolicy())
    return ledger


def _setup(tmp_path: Path) -> tuple[Any, dict[str, object]]:
    """One shared state + configured ledger + a recorded observation.

    Returns ``(client, observation_payload)`` where the client is bound to the
    same ledger that recorded the observation, so no revision conflicts arise.
    """
    fastapi = pytest.importorskip("fastapi")
    testclient = pytest.importorskip("fastapi.testclient")
    from nested_memvid_agent.routing.runtime import AdaptiveFlockRuntimeConfig

    state, task = _state_and_task(tmp_path)
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    observations = ledger.list_shadow_observations(run_id=task.run_id)
    assert len(observations) == 1
    observation = observations[0].to_payload()

    app = fastapi.FastAPI()
    register_routing_routes(
        app,
        ledger=ledger,
        runtime=AdaptiveFlockRuntimeConfig(enabled=False, mode="off"),
        http_exception=fastapi.HTTPException,
    )
    return testclient.TestClient(app), observation


class TestShadowObservationEndpoints:
    def test_run_routing_exposes_shadow_observations_additively(
        self, tmp_path: Path
    ) -> None:
        client, observation = _setup(tmp_path)
        try:
            response = client.get("/api/runs/run-shadow-api/routing")
        finally:
            client.close()
        assert response.status_code == 200
        body = response.json()
        # Existing keys are preserved (backward compatible).
        assert "decisions" in body
        assert "outcomes" in body
        assert "shadows" in body
        assert "calibrations" in body
        # New additive key.
        assert body["shadow_observations"] == [observation]

    def test_shadow_observations_endpoint_returns_schema_and_rows(
        self, tmp_path: Path
    ) -> None:
        client, observation = _setup(tmp_path)
        try:
            response = client.get("/api/routing/shadow-observations?run_id=run-shadow-api")
        finally:
            client.close()
        assert response.status_code == 200
        body = response.json()
        assert body["schema"] == "kestrel.routing.shadow_observations.v1"
        assert body["run_id"] == "run-shadow-api"
        assert body["observations"] == [observation]

    def test_observation_surfaces_explicit_authority_verdict_and_missing_data(
        self, tmp_path: Path
    ) -> None:
        client, _observation = _setup(tmp_path)
        try:
            response = client.get("/api/routing/shadow-observations?run_id=run-shadow-api")
        finally:
            client.close()
        body = response.json()["observations"][0]
        # Explicit authority is one of the closed durable labels.
        assert body["actual_authority"] == ActualAuthority.DETERMINISTIC_STATIC.value
        # Observational verdict is honest and never claims counterfactual proof
        # for an unexecuted target.
        assert body["verdict"] == ShadowVerdict.INCONCLUSIVE.value
        assert body["counterfactual_proven"] is False
        assert isinstance(body["evidence_basis"], list)
        # Missing terminal data is explicit, not invented.
        assert body["validation_passed"] is None
        assert body["resolved_at"] is None
        # Links evidence to the durable run/task.
        assert body["run_id"] == "run-shadow-api"
        assert body["task_id"] == "task-shadow-api"
        assert body["role"] == "executor"

    def test_shadow_observations_endpoint_filters_by_task_and_role(
        self, tmp_path: Path
    ) -> None:
        client, _observation = _setup(tmp_path)
        try:
            matching = client.get(
                "/api/routing/shadow-observations"
                "?run_id=run-shadow-api&task_id=task-shadow-api&role=executor"
            )
            wrong_role = client.get(
                "/api/routing/shadow-observations?run_id=run-shadow-api&role=reviewer"
            )
            unknown_run = client.get(
                "/api/routing/shadow-observations?run_id=run-does-not-exist"
            )
        finally:
            client.close()
        assert len(matching.json()["observations"]) == 1
        assert wrong_role.json()["observations"] == []
        assert unknown_run.json()["observations"] == []

    def test_shadow_observations_endpoints_are_read_only(self, tmp_path: Path) -> None:
        client, _observation = _setup(tmp_path)
        try:
            get_response = client.get("/api/routing/shadow-observations?run_id=run-shadow-api")
            # The only shadow-observation surface is a GET; a POST is not routed.
            post_response = client.post("/api/routing/shadow-observations", json={})
        finally:
            client.close()
        assert get_response.status_code == 200
        assert post_response.status_code in (404, 405)
