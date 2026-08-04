from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.routing.runtime import (
    AdaptiveFlockRuntimeConfig,
    build_run_manager,
)
from nested_memvid_agent.server_routing_routes import register_routing_routes
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


def _routing_test_app(tmp_path: Path) -> tuple[Any, Any, AgentStateStore]:
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


def _configure_local_scout(client: Any) -> None:
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
    target = client.post(
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
    )
    assert target.status_code == 200


def _create_preview_task(state: AgentStateStore, tmp_path: Path, *, suffix: str) -> None:
    run_id = f"run-preview-{suffix}"
    state.create_run(
        run_id=run_id,
        message="Inspect the repository",
        session_id=f"session-preview-{suffix}",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    state.create_task_node(
        task_id=f"task-preview-{suffix}",
        run_id=run_id,
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=("repo.search", "repo.map"),
        risk="low",
        acceptance_criteria=(),
    )


def test_routing_preview_explains_task_without_executing_model(tmp_path: Path) -> None:
    client, build, state = _routing_test_app(tmp_path)
    _create_preview_task(state, tmp_path, suffix="success")
    try:
        _configure_local_scout(client)
        preview = client.post(
            "/api/routing/preview",
            json={
                "run_id": "run-preview-success",
                "task_id": "task-preview-success",
                "local_required": True,
            },
        )
        assert preview.status_code == 200
        payload = preview.json()
        assert payload["contract"]["task_family"] == "repository_inspection"
        assert payload["decision"]["selected_target_id"] == "local-scout"
        assert payload["decision"]["mode"] == "shadow"
        assert payload["decision"]["actionable"] is False
        omitted_run = client.post(
            "/api/routing/preview",
            json={
                "task_id": "task-preview-success",
                "local_required": True,
            },
        )
        assert omitted_run.status_code == 200
        assert omitted_run.json()["run_id"] == "run-preview-success"
        assert state.get_task_node("task-preview-success").status == "queued"
        assert build.routing_ledger.list_decisions(run_id="run-preview-success") == []
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()


def test_service_evaluate_targets_matches_route_task_candidates() -> None:
    from datetime import UTC, datetime

    from nested_memvid_agent.routing import (
        AdaptiveFlockRoutingService,
        AgentTaskContract,
        ModelTarget,
        ProviderProfile,
        route_task,
    )

    clock = lambda: datetime(2026, 8, 1, 12, 0, tzinfo=UTC)  # noqa: E731
    profile = ProviderProfile(
        profile_id="local",
        display_name="Local server",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        locality="local",
    )
    healthy = ModelTarget(
        target_id="local-healthy",
        provider_profile_id="local",
        provider="openai-compatible",
        model="qwen-coder",
        locality="local",
        health="healthy",
        quality_tier=3,
    )
    disabled = ModelTarget(
        target_id="local-disabled",
        provider_profile_id="local",
        provider="openai-compatible",
        model="qwen-coder",
        enabled=False,
        locality="local",
        health="healthy",
        quality_tier=3,
    )
    service = AdaptiveFlockRoutingService(
        profiles=[profile],
        targets=[healthy, disabled],
        mode="shadow",
        clock=clock,
    )
    contract = AgentTaskContract(
        task_id="task-parity",
        run_id="run-parity",
        role="worker",
        task_family="repository_inspection",
        objective="Compare eligibility evaluation with ordinary routing.",
        complexity=0.2,
        ambiguity=0.1,
        risk="low",
        local_required=True,
    )
    evaluations = service.evaluate_targets(contract)
    decision = route_task(contract, [healthy, disabled], clock=clock)
    candidates = {candidate.target.target_id: candidate for candidate in decision.candidates}
    assert {evaluation.target.target_id for evaluation in evaluations} == set(candidates)
    for evaluation in evaluations:
        candidate = candidates[evaluation.target.target_id]
        assert evaluation.eligible == candidate.eligible
        assert evaluation.reason_codes == candidate.reason_codes


def test_routing_preview_returns_rejection_reasons_when_no_target_is_eligible(
    tmp_path: Path,
) -> None:
    client, build, state = _routing_test_app(tmp_path)
    _create_preview_task(state, tmp_path, suffix="reject")
    try:
        preview = client.post(
            "/api/routing/preview",
            json={
                "run_id": "run-preview-reject",
                "task_id": "task-preview-reject",
                "local_required": True,
            },
        )
        assert preview.status_code == 409
        detail = preview.json()["detail"]
        assert detail["code"] == "routing_unavailable"
        assert isinstance(detail["reason_codes"], list)
    finally:
        assert build.runs.shutdown(timeout_seconds=1.0)
        assert build.runs.mcp.shutdown()
