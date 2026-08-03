from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.projects import ProjectRecord
from nested_memvid_agent.routing.activation_evaluator import (
    ActivationEvaluator,
    EvaluationBindings,
)
from nested_memvid_agent.routing.activation_service import (
    ActivationBindings,
    ActivationRequest,
    ActivationService,
)
from nested_memvid_agent.routing.contracts import compile_task_contract
from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
from nested_memvid_agent.routing.learned_router import (
    LearnedRouterConfig,
    LearnedRouterState,
)
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import (
    AgentTaskContract,
    ModelTarget,
    ProviderProfile,
    RoutePolicy,
)
from nested_memvid_agent.routing.qualification_evaluator import (
    ScopeAttemptEvidence,
    ScopeEvaluationInput,
    ScopeQualificationResult,
)
from nested_memvid_agent.routing.qualification_ledger import QualificationLedger
from nested_memvid_agent.routing.qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    QualificationScope,
    QualificationThresholds,
    RiskLevel,
)
from nested_memvid_agent.routing.qualification_receipt import build_terminal_receipt
from nested_memvid_agent.routing.qualification_records import (
    ActivationGrant,
    QualificationReceipt,
    QualificationRunDraft,
)
from nested_memvid_agent.routing.qualification_replay import ReplayResult
from nested_memvid_agent.routing.router import RoutingUnavailableError
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord

OWNER = "owner@example.test"
RECENT = datetime(2026, 8, 1, tzinfo=UTC)

TARGET_SNAPSHOT: dict[str, Any] = {
    "targets": [
        {
            "target_id": "cheap",
            "model": "cheap-model",
            "endpoint": "https://cheap.example.test",
            "trust_class": "standard",
            "capabilities": ["tag:repository_inspection"],
            "privacy_class": "approved_cloud",
            "locality": "cloud",
            "network_constraints": [],
        },
        {
            "target_id": "expensive",
            "model": "expensive-model",
            "endpoint": "https://expensive.example.test",
            "trust_class": "standard",
            "capabilities": ["tag:repository_inspection"],
            "privacy_class": "approved_cloud",
            "locality": "cloud",
            "network_constraints": [],
        },
    ]
}
PRICE_SNAPSHOT: dict[str, Any] = {"source": "operator_verified"}
POLICY_PAYLOAD: dict[str, Any] = {"policy_id": "balanced", "revision": 1}
LEARNED_PAYLOAD: dict[str, Any] = {"state": "disabled"}
PROJECT_AUTHORITY: dict[str, Any] = {"principal": OWNER}


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

    project_a_task = _create_task(
        state,
        workspace=Path(project_a.repository_path),
        project_id=project_a.project_id,
        project_revision=project_a.revision,
        suffix="activate",
    )
    harness = _GrantHarness(state, compile_task_contract(project_a_task))
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
        activation_evaluator=harness.evaluator,
    )
    activated = learned.assign(
        AgentConfig(),
        project_a_task,
        subagent_id=None,
        attempt=1,
    )

    assert activated.record.selected_target_id == "cheap"
    assert activated.record.selection_kind == "learned_constrained"
    assert activated.record.activation_effective is True
    assert activated.record.activation_grant_id == harness.grant.grant_id
    assert activated.record.activation_receipt_id == harness.receipt.receipt_id
    assert activated.record.activation_reason is None
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
    assert isolated.record.activation_effective is False
    assert isolated.record.activation_grant_id is None
    assert isolated.record.activation_reason is None
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
    assert durable.record.activation_effective is False
    assert durable.record.activation_grant_id is None
    assert durable.record.activation_reason is None
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


def _project(state: AgentStateStore, tmp_path: Path, suffix: str) -> ProjectRecord:
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

    task = _create_task(
        state,
        workspace=Path(project.repository_path),
        project_id=project.project_id,
        project_revision=project.revision,
        suffix="mapped-activate",
    )
    harness = _GrantHarness(state, compile_task_contract(task))
    learned = DurableRoutingCoordinator(
        ledger,
        mode="adaptive",
        learned_config=mapped,
        activation_evaluator=harness.evaluator,
    )
    activated = learned.assign(AgentConfig(), task, subagent_id=None, attempt=1)

    assert activated.record.selected_target_id == "cheap"
    assert activated.record.selection_kind == "learned_constrained"
    assert activated.record.activation_effective is True
    assert activated.record.activation_grant_id == harness.grant.grant_id
    assert activated.record.activation_receipt_id == harness.receipt.receipt_id
    assert activated.record.activation_reason is None
    shadow = ledger.get_shadow(activated.record.decision_id)
    assert shadow is not None
    assert shadow.static_target_id == "expensive"
    assert shadow.learned_target_id == "cheap"
    assert shadow.actual_target_id == "cheap"
    assert shadow.activated is True
    assert shadow.evidence_count == 10


# --- durable-grant seeding (Adaptive Flock plan, Task 15) ------------------------


def _attempt_evidence(
    scope: QualificationScope,
    capability_key: str,
    attempt_id: str,
    target_id: str,
    ordinal: int,
    *,
    cost: float,
) -> ScopeAttemptEvidence:
    return ScopeAttemptEvidence(
        attempt_id=attempt_id,
        case_id="case_1",
        scope_digest=scope.digest,
        target_id=target_id,
        attempt_ordinal=ordinal,
        validation_passed=True,
        execution_status="completed",
        failure_category=None,
        actual_cost_usd=cost,
        latency_seconds=1.0,
        guardrail_state="clear",
        evidence_kind="real_project",
        trusted_acceptance=True,
        task_family=scope.task_family,
        risk=scope.risk,
        contract_digest="a" * 64,
        project_id=scope.project_id,
        capability_key=capability_key,
        created_at=RECENT.isoformat(),
    )


def _scope_result(scope: QualificationScope) -> ScopeQualificationResult:
    return ScopeQualificationResult(
        scope_digest=scope.digest,
        state="qualified",
        static_target_id="expensive",
        selected_target_id="cheap",
        total_support=10,
        selected_target_support=8,
        confidence=0.8,
        static_utility=0.5,
        learned_utility=0.99,
        utility_delta=0.49,
        cost_coverage=1.0,
        estimated_savings_usd=0.99,
        estimated_regret_usd=None,
        guardrail_violations=0,
        evaluated_target_ids=("cheap", "expensive"),
        reasons=(),
        router_state=LearnedRouterState(config_digest="6" * 64),
        thresholds_digest=QualificationThresholds().digest,
    )


def _replay(results: tuple[ScopeQualificationResult, ...]) -> ReplayResult:
    return ReplayResult(
        repeats=20,
        completed_repeats=20,
        successes_required=20,
        projection_digests=("c" * 64,) * 20,
        results=results,
        reasons=(),
    )


def _evaluation_bindings() -> EvaluationBindings:
    return EvaluationBindings(
        project_authority=PROJECT_AUTHORITY,
        privacy={
            "target_id": "cheap",
            "privacy_class": "approved_cloud",
            "locality": "cloud",
            "network_constraints": [],
        },
        target_snapshot=TARGET_SNAPSHOT,
        price_snapshot=PRICE_SNAPSHOT,
        policy_payload=POLICY_PAYLOAD,
        learned_payload=LEARNED_PAYLOAD,
    )


class _GrantHarness:
    """One active grant for the exact contract scope plus its evaluator."""

    def __init__(self, state: AgentStateStore, contract: AgentTaskContract) -> None:
        self.qualifications = QualificationLedger(state)
        capability_key = "+".join(sorted(set(contract.required_capabilities)))
        scope = QualificationScope(
            project_id="project-alpha",
            task_family=contract.task_family,
            risk=cast(RiskLevel, contract.risk),
            capabilities=contract.required_capabilities,
            policy_id="balanced",
            policy_revision=1,
            target_ids=("cheap", "expensive"),
            target_inventory_digest="1" * 64,
            price_digest="2" * 64,
            learned_config_digest="3" * 64,
            project_authority_digest="4" * 64,
        )
        self.scope = scope
        service = ActivationService(self.qualifications, master_permit=lambda: True)
        draft = QualificationRunDraft(
            run_id="qual_learned_runtime",
            owner_principal=OWNER,
            scope=scope,
            corpus=CorpusManifest(
                schema_version=1,
                items=(
                    CorpusItem(
                        item_id="corpus_item_1",
                        task_family=contract.task_family,
                        risk=cast(RiskLevel, contract.risk),
                        capabilities=contract.required_capabilities,
                        task_contract_digest="a" * 64,
                        acceptance_plan_digest="b" * 64,
                        evidence_kind="real_project",
                    ),
                ),
            ),
            thresholds=QualificationThresholds(),
            target_snapshot=TARGET_SNAPSHOT,
            price_snapshot=PRICE_SNAPSHOT,
            policy_payload=POLICY_PAYLOAD,
            learned_payload=LEARNED_PAYLOAD,
            project_authority=PROJECT_AUTHORITY,
            build={"version": "0.5.0", "git": "bd2c182"},
            max_spend=MoneyMicros.from_usd_text("50.00"),
            effective_stop_cap=MoneyMicros.from_usd_text("25.00"),
            attempt_ceiling=MoneyMicros.from_usd_text("5.00"),
        )
        run = self.qualifications.create_run(draft)
        result = _scope_result(scope)
        payload = build_terminal_receipt(
            status="completed",
            run=run,
            terminal_reason="matrix_exhausted",
            scopes=[result],
            replay=_replay((result,)),
        )
        self.qualifications.finalize_run_terminal(
            run.run_id,
            expected_revision=run.revision,
            terminal_status="completed",
            terminal_reason="matrix_exhausted",
            actual_spend=run.actual_spend,
            receipt_payload=payload,
        )
        self.receipt: QualificationReceipt = self.qualifications.list_receipts(run.run_id)[0]
        request = ActivationRequest(
            receipt_id=self.receipt.receipt_id,
            scope_digests=(scope.digest,),
            principal=OWNER,
            expected_receipt_digest=str(self.receipt.payload["payload_digest"]),
            expected_run_revision=int(self.receipt.payload["run"]["revision"]),
            bindings=ActivationBindings(
                project_authority=PROJECT_AUTHORITY,
                target_snapshot=TARGET_SNAPSHOT,
                price_snapshot=PRICE_SNAPSHOT,
                policy_payload=POLICY_PAYLOAD,
                learned_payload=LEARNED_PAYLOAD,
            ),
        )
        self.grant: ActivationGrant = service.activate_scopes(request).grants[0]
        attempts = [
            _attempt_evidence(
                scope, capability_key, f"attempt_cheap_{index}", "cheap", index, cost=0.01
            )
            for index in range(1, 9)
        ]
        attempts += [
            _attempt_evidence(
                scope, capability_key, f"attempt_expensive_{index}", "expensive", index, cost=1.0
            )
            for index in range(1, 3)
        ]
        bundle = ScopeEvaluationInput(
            scope=scope,
            static_target_id="expensive",
            thresholds=QualificationThresholds(),
            attempts=tuple(attempts),
        )
        self.evaluator = ActivationEvaluator(
            self.qualifications,
            bindings=_evaluation_bindings,
            eligibility=lambda target_id: "eligible",
            evidence=lambda grant: bundle,
            master_permit=lambda: True,
        )
