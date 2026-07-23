from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.coordinator import (
    DurableRoutingCoordinator,
    RoutingLeaseConflict,
)
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.ledger_records import RoutingRevisionConflict
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord


def _state_and_task(tmp_path: Path) -> tuple[AgentStateStore, TaskNodeRecord]:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    state.create_run(
        run_id="run-routing",
        message="Inspect the repository",
        session_id="session-routing",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = state.create_task_node(
        task_id="task-routing",
        run_id="run-routing",
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=("repo.search", "repo.map"),
        risk="low",
        acceptance_criteria=(),
    )
    return state, task


def _profile() -> ProviderProfile:
    return ProviderProfile(
        profile_id="local",
        display_name="Local model server",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        secret_ref="secret://routing-local-key",
        locality="local",
    )


def _target() -> ModelTarget:
    return ModelTarget(
        target_id="local-scout",
        provider_profile_id="local",
        provider="openai-compatible",
        model="qwen-coder",
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
    ledger.put_model_target(_target())
    ledger.put_policy(RoutePolicy())
    return ledger


def test_routing_ledger_uses_additive_schema_and_round_trips_inventory(tmp_path: Path) -> None:
    state, _task = _state_and_task(tmp_path)

    ledger = _configured_ledger(state)

    assert state.schema_version() >= 19
    assert ledger.schema_version() == 1
    profile = ledger.get_provider_profile("local")
    target = ledger.get_model_target("local-scout")
    policy = ledger.get_policy("balanced")
    assert profile is not None and profile.profile.secret_ref == "secret://routing-local-key"
    assert target is not None and target.target.model == "qwen-coder"
    assert policy is not None and policy.policy.policy_id == "balanced"


def test_routing_inventory_rejects_raw_secrets_and_secret_bearing_metadata(
    tmp_path: Path,
) -> None:
    state, _task = _state_and_task(tmp_path)
    ledger = RoutingLedger(state)

    with pytest.raises(ValueError, match="secret://"):
        ledger.put_provider_profile(replace(_profile(), secret_ref="sk-live-secret"))
    with pytest.raises(ValueError, match="must not embed credentials"):
        ledger.put_provider_profile(
            replace(_profile(), profile_id="embedded", base_url="https://user:pass@example.test/v1")
        )
    with pytest.raises(ValueError, match="secret-bearing key"):
        ledger.put_provider_profile(
            replace(_profile(), profile_id="metadata", metadata={"api_key": "not-allowed"})
        )


def test_routing_inventory_updates_require_revision_compare_and_swap(tmp_path: Path) -> None:
    state, _task = _state_and_task(tmp_path)
    ledger = RoutingLedger(state)
    first = ledger.put_provider_profile(_profile())

    with pytest.raises(RoutingRevisionConflict):
        ledger.put_provider_profile(replace(_profile(), display_name="Changed without revision"))

    updated = ledger.put_provider_profile(
        replace(_profile(), display_name="Revised local model server"),
        expected_revision=first.revision,
    )
    assert updated.revision == first.revision + 1


def test_shadow_coordinator_persists_decision_without_switching_provider(tmp_path: Path) -> None:
    state, task = _state_and_task(tmp_path)
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    base = AgentConfig(
        provider="mock",
        model="orchestrator",
        workspace=tmp_path,
        allow_shell=True,
        allow_file_write=False,
    )

    durable = coordinator.assign(base, task, subagent_id=None, attempt=1)
    replay = coordinator.assign(base, task, subagent_id=None, attempt=1)

    assert durable.assignment.executes_selected_target is False
    assert durable.assignment.config is base
    assert durable.record.selected_target_id == "local-scout"
    assert replay.reused is True
    assert replay.record.decision_id == durable.record.decision_id
    assert len(ledger.list_decisions(run_id=task.run_id)) == 1


def test_constrained_coordinator_switches_only_provider_connection_fields(tmp_path: Path) -> None:
    state, task = _state_and_task(tmp_path)
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="constrained")
    base = AgentConfig(
        provider="mock",
        model="orchestrator",
        fallback_provider="openai",
        fallback_model="fallback",
        workspace=tmp_path,
        allow_shell=True,
        allow_file_write=False,
        allow_git_commit=False,
        require_approval_for_high_risk_tools=True,
    )

    durable = coordinator.assign(base, task, subagent_id=None, attempt=1)

    routed = durable.assignment.config
    assert durable.assignment.executes_selected_target is True
    assert routed.provider == "openai-compatible"
    assert routed.model == "qwen-coder"
    assert routed.base_url == "http://127.0.0.1:1234/v1"
    assert routed.api_key_env == "secret://routing-local-key"
    assert routed.fallback_provider is None
    assert routed.workspace == tmp_path
    assert routed.allow_shell is True
    assert routed.allow_file_write is False
    assert routed.allow_git_commit is False
    assert routed.require_approval_for_high_risk_tools is True


def test_route_outcome_is_create_once_and_terminalizes_decision(tmp_path: Path) -> None:
    state, task = _state_and_task(tmp_path)
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    durable = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    coordinator.mark_started(durable)

    outcome = coordinator.record_outcome(
        durable,
        execution_status="complete",
        validation_passed=True,
        validation_codes=("accepted",),
        latency_seconds=0.25,
        tool_count=2,
        reward_components={"completion": 1.0},
        outcome_labels=("validated_success",),
        evidence_refs=("tool:test",),
    )
    replay = coordinator.record_outcome(
        durable,
        execution_status="complete",
        validation_passed=True,
        validation_codes=("accepted",),
        latency_seconds=0.25,
        tool_count=2,
        reward_components={"completion": 1.0},
        outcome_labels=("validated_success",),
        evidence_refs=("tool:test",),
    )

    assert replay == outcome
    decision = ledger.get_decision(durable.record.decision_id)
    assert decision is not None and decision.status == "completed"
    assert len(ledger.list_outcomes(run_id=task.run_id)) == 1


def test_route_lease_fails_closed_after_target_revision_changes(tmp_path: Path) -> None:
    state, task = _state_and_task(tmp_path)
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    base = AgentConfig()
    coordinator.assign(base, task, subagent_id=None, attempt=1)
    current = ledger.get_model_target("local-scout")
    assert current is not None
    ledger.put_model_target(
        replace(current.target, operator_priority=1),
        expected_revision=current.revision,
    )

    with pytest.raises(RoutingRevisionConflict):
        coordinator.assign(base, task, subagent_id=None, attempt=1)


def test_route_decision_rejects_task_from_another_run(tmp_path: Path) -> None:
    state, task = _state_and_task(tmp_path)
    ledger = _configured_ledger(state)
    state.create_run(
        run_id="run-other",
        message="Other run",
        session_id="session-other",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    mismatched = replace(task, run_id="run-other")
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")

    with pytest.raises(ValueError, match="does not belong to run"):
        coordinator.assign(AgentConfig(), mismatched, subagent_id=None, attempt=1)


def test_route_lease_rejects_changed_task_contract(tmp_path: Path) -> None:
    state, task = _state_and_task(tmp_path)
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    base = AgentConfig()
    coordinator.assign(base, task, subagent_id=None, attempt=1)
    changed = replace(task, goal="A materially different objective requiring a new route.")

    with pytest.raises(RoutingLeaseConflict, match="contract changed"):
        coordinator.assign(base, changed, subagent_id=None, attempt=1)
