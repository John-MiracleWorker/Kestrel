from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.engineering.outcomes import OutcomeAnalyticsService
from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.state_store import AgentStateStore


def _project(state: AgentStateStore, root: Path, project_id: str = "project_one") -> None:
    repository = root / project_id
    repository.mkdir()
    state.create_project(
        project_id=project_id,
        display_name=project_id,
        repository_path=repository,
        active_capability_keys=(),
    )


def _routed_run(
    state: AgentStateStore,
    tmp_path: Path,
    *,
    run_id: str = "run_outcome",
    target_id: str = "local-coder",
    model: str = "qwen-coder",
    quality_tier: int = 3,
    validation_passed: bool = True,
) -> RoutingLedger:
    state.create_run(
        run_id=run_id,
        message="Repair parser",
        session_id="session",
        workspace=str(tmp_path / "project_one"),
        provider="mock",
        model="mock",
        project_id="project_one",
    )
    task = state.create_task_node(
        task_id=f"task_{run_id}",
        run_id=run_id,
        title="Repair parser",
        goal="Fix the parser and validate it.",
        profile="worker",
        approved=True,
        required_tools=("test.run",),
        risk="low",
        acceptance_criteria=("The parser test passes.",),
    )
    ledger = RoutingLedger(state)
    if ledger.get_provider_profile("local") is None:
        ledger.put_provider_profile(
            ProviderProfile(
                profile_id="local",
                display_name="Local",
                adapter="openai-compatible",
                base_url="http://127.0.0.1:1234/v1",
                locality="local",
            )
        )
    ledger.put_model_target(
        ModelTarget(
            target_id=target_id,
            provider_profile_id="local",
            provider="openai-compatible",
            model=model,
            locality="local",
            capability_tags=("worker", "test_and_validation"),
            role_affinities=("worker",),
            task_family_affinities=("test_and_validation",),
            max_context_tokens=64_000,
            supports_tools=True,
            supports_json=True,
            supports_reasoning=True,
            quality_tier=quality_tier,
            latency_tier=1,
            predicted_success=0.8,
            estimated_cost_usd=0.05,
            health="healthy",
        )
    )
    policy = RoutePolicy()
    if ledger.get_policy(policy.policy_id) is None:
        ledger.put_policy(policy)
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    durable = coordinator.assign(
        AgentConfig(provider="mock", model="mock"),
        task,
        subagent_id=None,
        attempt=1,
    )
    coordinator.mark_started(durable)
    coordinator.record_outcome(
        durable,
        execution_status="complete",
        validation_passed=validation_passed,
        validation_codes=("accepted",) if validation_passed else ("failed",),
        latency_seconds=30.0,
        input_tokens=100,
        output_tokens=50,
        actual_cost_usd=0.25,
        tool_count=2,
        retry_count=1,
        reward_components={"completion": 1.0},
        outcome_labels=(
            ("validated_success", "usage_complete")
            if validation_passed
            else ("validation_failed", "usage_complete")
        ),
        evidence_refs=("repair_validation_abc",),
    )
    state.update_run(run_id, status="completed", stop_reason="completed")
    return ledger


def test_outcome_report_distinguishes_missing_data_and_computes_success_value(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.sqlite3")
    _project(state, tmp_path)
    _routed_run(state, tmp_path)
    service = OutcomeAnalyticsService(state)

    report = service.report(project_id="project_one")

    assert report["summary"]["validated_completion_rate"]["value"] == 1.0
    assert report["summary"]["actual_cost_usd"]["value"] == 0.25
    assert report["summary"]["validated_success_per_dollar"]["value"] == 4.0
    assert report["evidence_coverage"]["provider_usage"]["rate"] == 1.0
    assert report["groups"][0]["provider"] == "openai-compatible"
    assert report["groups"][0]["retry_count"] == 1

    empty = service.report(project_id="project_one", provider="not-configured")
    assert empty["summary"]["actual_cost_usd"]["value"] is None
    assert empty["summary"]["actual_cost_usd"]["missing"] is True
    assert empty["evidence_coverage"]["cost_attribution"]["rate"] is None


def test_strongest_model_baseline_uses_declared_quality_not_hindsight(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.sqlite3")
    _project(state, tmp_path)
    _routed_run(state, tmp_path, run_id="run_tier_three")
    _routed_run(
        state,
        tmp_path,
        run_id="run_tier_five",
        target_id="local-frontier",
        model="frontier-local",
        quality_tier=5,
        validation_passed=False,
    )

    report = OutcomeAnalyticsService(state).report(project_id="project_one")
    strongest = next(
        item
        for item in report["baselines"]
        if item["baseline"] == "strongest_model_only"
    )

    assert strongest["sample_count"] == 1
    assert strongest["validated_completion_rate"]["value"] == 0.0


def test_private_benchmark_rejects_secrets_and_links_only_same_project_runs(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.sqlite3")
    _project(state, tmp_path)
    _project(state, tmp_path, "project_two")
    _routed_run(state, tmp_path)
    service = OutcomeAnalyticsService(state)
    secret = "benchmark-secret-material-950c"
    register_secret_value(secret)

    with pytest.raises(ValueError, match="sensitive"):
        service.create_benchmark(
            case_id="case_secret",
            project_id="project_one",
            name="Secret case",
            task_family="implementation",
            risk="low",
            fixture={"objective": f"Use {secret}"},
            acceptance_criteria=("Tests pass.",),
            actor="owner",
        )

    case = service.create_benchmark(
        case_id="case_parser",
        project_id="project_one",
        name="Parser repair",
        task_family="implementation",
        risk="low",
        fixture={"objective": "Repair the parser.", "files": ["src/parser.py"]},
        acceptance_criteria=("The parser test passes.",),
        actor="owner",
    )
    replay = service.link_replay(
        replay_id="replay_parser",
        case_id=case.case_id,
        run_id="run_outcome",
        route_policy_id="balanced",
        context_strategy="repository_index",
        baseline="live",
        actor="owner",
    )

    assert replay.run_status == "completed"
    assert replay.metrics["validated_completion_rate"]["value"] == 1.0
    assert service.list_benchmarks(project_id="project_one") == [case]
    assert service.list_replays(case_id=case.case_id)[0].run_id == "run_outcome"

    state.create_run(
        run_id="run_other_project",
        message="Other",
        session_id="other",
        workspace=str(tmp_path / "project_two"),
        provider="mock",
        model="mock",
        project_id="project_two",
    )
    with pytest.raises(ValueError, match="different project"):
        service.link_replay(
            replay_id="replay_wrong",
            case_id=case.case_id,
            run_id="run_other_project",
            route_policy_id=None,
            context_strategy="default",
            baseline="live",
            actor="owner",
        )
