from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
from nested_memvid_agent.routing.learned_router import LearnedRouterConfig
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.routing.router import RoutingUnavailableError
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord


def test_outcome_cost_calibration_and_shadow_are_bound_to_decision_snapshot(
    tmp_path: Path,
) -> None:
    state, task = _state_and_task(tmp_path, suffix="cost")
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="constrained")

    durable = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    coordinator.mark_started(durable)
    outcome = coordinator.record_outcome(
        durable,
        execution_status="completed",
        validation_passed=True,
        validation_codes=("accepted",),
        input_tokens=1_000,
        output_tokens=500,
        latency_seconds=2.5,
        outcome_labels=("validated_success",),
        evidence_refs=("validation:unit",),
    )

    assert outcome.actual_cost_usd == 0.02
    assert {"usage_attributed", "cost_attributed"} <= set(outcome.outcome_labels)
    assert durable.record.input_cost_per_million_usd == 10.0
    assert durable.record.output_cost_per_million_usd == 20.0
    shadow = ledger.get_shadow(durable.record.decision_id)
    assert shadow is not None
    assert shadow.actual_target_id == "expensive"
    assert shadow.actual_validation_passed is True
    assert shadow.actual_cost_usd == 0.02
    calibrations = ledger.list_calibrations(target_id="expensive")
    assert len(calibrations) == 1
    assert calibrations[0].validation_rate == 1.0
    assert calibrations[0].cost_coverage == 1.0


def test_adaptive_activation_is_evidence_gated_and_project_scoped(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    project_a = _project(state, tmp_path, "a")
    project_b = _project(state, tmp_path, "b")
    ledger = _configured_ledger(state)
    trainer = DurableRoutingCoordinator(ledger, mode="constrained")

    # Seven successful cheap routes and three successful expensive routes give
    # the cheap target enough scoped support and a measurable cost advantage.
    for index in range(10):
        target_id = "cheap" if index < 7 else "expensive"
        task = _create_task(
            state,
            workspace=Path(project_a.repository_path),
            project_id=project_a.project_id,
            project_revision=project_a.revision,
            suffix=f"train-{index}",
        )
        durable = trainer.assign(
            AgentConfig(),
            task,
            subagent_id=None,
            attempt=1,
            direct_target_id=target_id,
        )
        trainer.record_outcome(
            durable,
            execution_status="completed",
            validation_passed=True,
            validation_codes=("accepted",),
            input_tokens=1_000,
            output_tokens=500,
            latency_seconds=1.0,
            outcome_labels=("validated_success",),
        )

    learned = DurableRoutingCoordinator(
        ledger,
        mode="adaptive",
        learned_config=LearnedRouterConfig(
            min_examples=5,
            min_target_examples=3,
            confidence_threshold=0.65,
            activation_margin=0.001,
            cost_coverage_threshold=0.8,
            replay_gate_enabled=True,
        ),
    )
    project_a_task = _create_task(
        state,
        workspace=Path(project_a.repository_path),
        project_id=project_a.project_id,
        project_revision=project_a.revision,
        suffix="activate",
    )
    activated = learned.assign(
        AgentConfig(),
        project_a_task,
        subagent_id=None,
        attempt=1,
    )

    assert activated.record.selected_target_id == "cheap"
    assert activated.record.selection_kind == "learned_constrained"
    activated_shadow = ledger.get_shadow(activated.record.decision_id)
    assert activated_shadow is not None
    assert activated_shadow.static_target_id == "expensive"
    assert activated_shadow.learned_target_id == "cheap"
    assert activated_shadow.actual_target_id == "cheap"
    assert activated_shadow.activated is True
    assert activated_shadow.evidence_count == 10

    # A different project has no authority to consume project A's outcomes.
    project_b_task = _create_task(
        state,
        workspace=Path(project_b.repository_path),
        project_id=project_b.project_id,
        project_revision=project_b.revision,
        suffix="isolated",
    )
    isolated = learned.assign(
        AgentConfig(),
        project_b_task,
        subagent_id=None,
        attempt=1,
    )
    assert isolated.record.selected_target_id == "expensive"
    isolated_shadow = ledger.get_shadow(isolated.record.decision_id)
    assert isolated_shadow is not None
    assert isolated_shadow.evidence_count == 0
    assert isolated_shadow.activated is False
    assert isolated_shadow.abstention_reason == "sparse_evidence"


def test_adaptive_mode_cannot_activate_without_explicit_replay_gate(
    tmp_path: Path,
) -> None:
    state, task = _state_and_task(tmp_path, suffix="closed-gate")
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(
        ledger,
        mode="adaptive",
        learned_config=LearnedRouterConfig(replay_gate_enabled=False),
    )

    durable = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)

    assert durable.record.selected_target_id == "expensive"
    shadow = ledger.get_shadow(durable.record.decision_id)
    assert shadow is not None
    assert shadow.activated is False
    assert shadow.abstention_reason in {"sparse_evidence", "replay_gate_not_enabled"}


def test_retry_ladder_retries_transport_escalates_capability_and_replans_contract(
    tmp_path: Path,
) -> None:
    state, task = _state_and_task(tmp_path, suffix="ladder")
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="constrained")

    transport = coordinator.assign(
        AgentConfig(),
        task,
        subagent_id=None,
        attempt=1,
        direct_target_id="cheap",
    )
    coordinator.record_outcome(
        transport,
        execution_status="provider_error",
        validation_passed=False,
        failure_category="provider_outage",
        provider_failure_code="timeout",
        outcome_labels=("acceptance_failed",),
    )
    transport_retry = coordinator.assign(
        AgentConfig(),
        task,
        subagent_id=None,
        attempt=2,
    )
    assert transport_retry.record.selected_target_id == "cheap"
    assert transport_retry.record.selection_kind == "transport_retry_same_target"

    coordinator.record_outcome(
        transport_retry,
        execution_status="failed",
        validation_passed=False,
        failure_category="capability_failure",
        provider_failure_code="unsupported_tool",
        outcome_labels=("acceptance_failed",),
    )
    capability_retry = coordinator.assign(
        AgentConfig(),
        task,
        subagent_id=None,
        attempt=3,
    )
    assert capability_retry.record.selected_target_id == "expensive"
    assert capability_retry.record.selection_kind == "capability_escalation"

    coordinator.record_outcome(
        capability_retry,
        execution_status="failed",
        validation_passed=False,
        failure_category="contract_failure",
        outcome_labels=("acceptance_failed",),
    )
    with pytest.raises(RoutingUnavailableError, match="requires replanning"):
        coordinator.assign(
            AgentConfig(),
            task,
            subagent_id=None,
            attempt=4,
        )


def _state_and_task(
    tmp_path: Path,
    *,
    suffix: str,
) -> tuple[AgentStateStore, TaskNodeRecord]:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    return state, _create_task(
        state,
        workspace=tmp_path,
        project_id=None,
        project_revision=None,
        suffix=suffix,
    )


def _create_task(
    state: AgentStateStore,
    *,
    workspace: Path,
    project_id: str | None,
    project_revision: int | None,
    suffix: str,
) -> TaskNodeRecord:
    run_id = f"run-{suffix}"
    state.create_run(
        run_id=run_id,
        message="Inspect repository context",
        session_id=f"session-{suffix}",
        workspace=str(workspace),
        provider="mock",
        model="mock",
        project_id=project_id,
        expected_project_revision=project_revision,
    )
    return state.create_task_node(
        task_id=f"task-{suffix}",
        run_id=run_id,
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=("repo.search", "repo.map"),
        risk="low",
        acceptance_criteria=("Relevant code is located.",),
    )


def _project(state: AgentStateStore, tmp_path: Path, suffix: str):
    root = tmp_path / f"project-{suffix}"
    root.mkdir()
    return state.create_project(
        project_id=f"project_{suffix}",
        display_name=f"Project {suffix.upper()}",
        repository_path=root,
        privacy_class="approved_cloud",
    )


def _configured_ledger(state: AgentStateStore) -> RoutingLedger:
    ledger = RoutingLedger(state)
    ledger.put_provider_profile(
        ProviderProfile(
            profile_id="cloud",
            display_name="Cloud",
            adapter="openai-compatible",
            locality="cloud",
        )
    )
    ledger.put_model_target(
        ModelTarget(
            target_id="cheap",
            provider_profile_id="cloud",
            provider="openai-compatible",
            model="cheap-model",
            locality="cloud",
            capability_tags=("repository_inspection", "scout", "worker"),
            role_affinities=("worker",),
            task_family_affinities=("repository_inspection",),
            max_context_tokens=64_000,
            supports_tools=True,
            quality_tier=2,
            latency_tier=1,
            estimated_cost_usd=0.002,
            input_cost_per_million_usd=1.0,
            output_cost_per_million_usd=2.0,
            health="healthy",
        )
    )
    ledger.put_model_target(
        ModelTarget(
            target_id="expensive",
            provider_profile_id="cloud",
            provider="openai-compatible",
            model="expensive-model",
            locality="cloud",
            capability_tags=("repository_inspection", "scout", "worker"),
            role_affinities=("worker",),
            task_family_affinities=("repository_inspection",),
            max_context_tokens=64_000,
            supports_tools=True,
            quality_tier=5,
            latency_tier=2,
            estimated_cost_usd=0.02,
            input_cost_per_million_usd=10.0,
            output_cost_per_million_usd=20.0,
            health="healthy",
        )
    )
    ledger.put_policy(RoutePolicy())
    return ledger


def test_threshold_mapped_config_keeps_route_selection_snapshots_stable(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from nested_memvid_agent.routing.qualification_models import QualificationThresholds

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    project = _project(state, tmp_path, "mapped")
    ledger = _configured_ledger(state)
    trainer = DurableRoutingCoordinator(ledger, mode="constrained")

    for index in range(10):
        target_id = "cheap" if index < 7 else "expensive"
        task = _create_task(
            state,
            workspace=Path(project.repository_path),
            project_id=project.project_id,
            project_revision=project.revision,
            suffix=f"mapped-train-{index}",
        )
        durable = trainer.assign(
            AgentConfig(),
            task,
            subagent_id=None,
            attempt=1,
            direct_target_id=target_id,
        )
        trainer.record_outcome(
            durable,
            execution_status="completed",
            validation_passed=True,
            validation_codes=("accepted",),
            input_tokens=1_000,
            output_tokens=500,
            latency_seconds=1.0,
            outcome_labels=("validated_success",),
        )

    mapped = replace(
        LearnedRouterConfig.from_qualification_thresholds(
            QualificationThresholds(
                min_examples_per_scope=5,
                min_examples_per_target=3,
                confidence_threshold=0.65,
                utility_margin=0.001,
                cost_coverage_threshold=0.8,
            )
        ),
        replay_gate_enabled=True,
    )
    assert mapped == LearnedRouterConfig(
        min_examples=5,
        min_target_examples=3,
        confidence_threshold=0.65,
        activation_margin=0.001,
        cost_coverage_threshold=0.8,
        replay_gate_enabled=True,
    )

    learned = DurableRoutingCoordinator(ledger, mode="adaptive", learned_config=mapped)
    task = _create_task(
        state,
        workspace=Path(project.repository_path),
        project_id=project.project_id,
        project_revision=project.revision,
        suffix="mapped-activate",
    )
    activated = learned.assign(AgentConfig(), task, subagent_id=None, attempt=1)

    assert activated.record.selected_target_id == "cheap"
    assert activated.record.selection_kind == "learned_constrained"
    shadow = ledger.get_shadow(activated.record.decision_id)
    assert shadow is not None
    assert shadow.static_target_id == "expensive"
    assert shadow.learned_target_id == "cheap"
    assert shadow.actual_target_id == "cheap"
    assert shadow.activated is True
    assert shadow.evidence_count == 10
