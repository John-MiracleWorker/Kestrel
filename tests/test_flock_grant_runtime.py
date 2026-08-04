"""Durable-grant gating for learned routing (Adaptive Flock plan, Task 15).

The routing coordinator may apply a learned target only when the activation
evaluator resolves a durable effective grant for the exact contract scope.
The environment flag is only a global permit inside the evaluator: adaptive
mode without a grant falls back to the static assignment and records
``durable_grant_required``.  Route-decision reuse preserves the original
grant/lease binding even after the grant is revoked; a malformed active
grant fails closed and can never choose learned routing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from nested_memvid_agent.config import AgentConfig
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
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord, utc_now

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


def _create_task(state: AgentStateStore, *, suffix: str) -> TaskNodeRecord:
    run_id = f"run-{suffix}"
    state.create_run(
        run_id=run_id,
        message="Inspect repository context",
        session_id=f"session-{suffix}",
        workspace="/tmp/workspace",
        provider="mock",
        model="mock",
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


def _train_learned_winner(state: AgentStateStore, ledger: RoutingLedger) -> None:
    """Seven cheap and three expensive successes make cheap the learned winner."""

    trainer = DurableRoutingCoordinator(ledger, mode="constrained")
    for index in range(10):
        target_id = "cheap" if index < 7 else "expensive"
        task = _create_task(state, suffix=f"train-{index}")
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


def _learned_config() -> LearnedRouterConfig:
    return LearnedRouterConfig(
        min_examples=5,
        min_target_examples=3,
        confidence_threshold=0.65,
        activation_margin=0.001,
        cost_coverage_threshold=0.8,
        replay_gate_enabled=True,
    )


def _learned_coordinator(
    ledger: RoutingLedger,
    evaluator: ActivationEvaluator | None,
) -> DurableRoutingCoordinator:
    return DurableRoutingCoordinator(
        ledger,
        mode="adaptive",
        learned_config=_learned_config(),
        activation_evaluator=evaluator,
    )


def _grantless_evaluator(state: AgentStateStore) -> ActivationEvaluator:
    return ActivationEvaluator(
        QualificationLedger(state),
        bindings=_evaluation_bindings,
        eligibility=lambda target_id: "eligible",
        evidence=_unreachable_evidence,
        master_permit=lambda: True,
    )


def _unreachable_evidence(grant: ActivationGrant) -> ScopeEvaluationInput:
    raise AssertionError(f"no grant must resolve, got {grant.grant_id}")


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


def _current_bindings() -> ActivationBindings:
    return ActivationBindings(
        project_authority=PROJECT_AUTHORITY,
        target_snapshot=TARGET_SNAPSHOT,
        price_snapshot=PRICE_SNAPSHOT,
        policy_payload=POLICY_PAYLOAD,
        learned_payload=LEARNED_PAYLOAD,
    )


def _attempt(
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


class GrantHarness:
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
            run_id="qual_grant_runtime",
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
        self.run_id = run.run_id
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
            bindings=_current_bindings(),
        )
        self.grant: ActivationGrant = service.activate_scopes(request).grants[0]
        attempts = [
            _attempt(scope, capability_key, f"attempt_cheap_{index}", "cheap", index, cost=0.01)
            for index in range(1, 9)
        ]
        attempts += [
            _attempt(
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


def _insert_malformed_grant(
    state: AgentStateStore,
    harness: GrantHarness,
    *,
    scope_json: str,
) -> str:
    """Insert an active grant row whose scope payload is malformed."""

    grant_id = "grant_malformed"
    with state._connect() as conn:
        conn.execute(
            """
            INSERT INTO routing_activation_grants (
                grant_id, run_id, target_id, scope_json, scope_digest,
                policy_id, policy_revision, qualification_receipt_id,
                created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                grant_id,
                harness.run_id,
                "cheap",
                scope_json,
                "f" * 64,
                "balanced",
                1,
                harness.receipt.receipt_id,
                OWNER,
                utc_now(),
            ),
        )
    harness.qualifications.append_transition(grant_id, "activated", "owner_confirmed_activation")
    return grant_id


# --- static fallback without a durable grant -----------------------------------


def test_adaptive_mode_without_grant_uses_static_and_records_reason(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    coordinator = _learned_coordinator(ledger, _grantless_evaluator(state))

    durable = coordinator.assign(
        AgentConfig(),
        _create_task(state, suffix="activate"),
        subagent_id=None,
        attempt=1,
    )

    assert durable.assignment.decision.selected_target.target_id == "expensive"
    assert durable.record.selected_target_id == "expensive"
    assert durable.record.activation_effective is False
    assert durable.record.activation_grant_id is None
    assert durable.record.activation_receipt_id is None
    assert durable.record.activation_reason == "durable_grant_required"
    shadow = ledger.get_shadow(durable.record.decision_id)
    assert shadow is not None
    assert shadow.learned_target_id == "cheap"
    assert shadow.activated is False
    assert shadow.abstention_reason == "durable_grant_required"


def test_env_flag_alone_does_not_authorize_learned_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "adaptive")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    evaluator = ActivationEvaluator(
        QualificationLedger(state),
        bindings=_evaluation_bindings,
        eligibility=lambda target_id: "eligible",
        evidence=_unreachable_evidence,
    )
    coordinator = _learned_coordinator(ledger, evaluator)

    durable = coordinator.assign(
        AgentConfig(),
        _create_task(state, suffix="env-only"),
        subagent_id=None,
        attempt=1,
    )

    assert durable.record.selected_target_id == "expensive"
    assert durable.record.activation_effective is False
    assert durable.record.activation_reason == "durable_grant_required"


# --- sticky leases preserve the original grant binding ---------------------------


def test_revocation_affects_new_lease_not_existing_attempt(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    task = _create_task(state, suffix="lease")
    harness = GrantHarness(state, compile_task_contract(task))
    coordinator = _learned_coordinator(ledger, harness.evaluator)

    first = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    assert first.assignment.decision.selected_target.target_id == "cheap"
    assert first.record.selection_kind == "learned_constrained"
    assert first.record.activation_effective is True
    assert first.record.activation_grant_id == harness.grant.grant_id
    assert first.record.activation_receipt_id == harness.receipt.receipt_id
    assert first.record.activation_reason is None

    grant_id = first.record.activation_grant_id
    assert grant_id is not None
    harness.qualifications.append_transition(grant_id, "revoked", "owner_revoked")

    reused = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    second = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=2)

    assert reused.record.decision_id == first.record.decision_id
    assert reused.assignment.decision.selected_target == first.assignment.decision.selected_target
    assert reused.record.activation_grant_id == first.record.activation_grant_id
    assert reused.record.activation_receipt_id == first.record.activation_receipt_id
    assert reused.record.activation_effective is True
    assert second.record.activation_effective is False
    assert second.record.activation_grant_id == grant_id
    assert second.record.activation_reason == "grant_revoked"
    assert second.assignment.decision.selected_target.target_id == "expensive"


# --- high risk: a malformed active grant fails closed ----------------------------


@pytest.mark.parametrize(
    "scope_json",
    ["{not-json", '["task_family", "repository_inspection"]'],
    ids=("invalid_json", "wrong_shape"),
)
def test_active_malformed_grant_cannot_choose_learned_routing(
    tmp_path: Path,
    scope_json: str,
) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    task = _create_task(state, suffix="malformed")
    harness = GrantHarness(state, compile_task_contract(task))
    _insert_malformed_grant(state, harness, scope_json=scope_json)
    coordinator = _learned_coordinator(ledger, harness.evaluator)

    durable = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)

    # The mission continues on the static route; even the valid sibling grant
    # must not authorize learned routing while a malformed grant poisons
    # resolution.
    assert durable.assignment.decision.selected_target.target_id == "expensive"
    assert durable.record.activation_effective is False
    assert durable.record.activation_grant_id is None
    assert durable.record.activation_reason == "activation_evaluation_failed"
    transitions = harness.qualifications.list_transitions(harness.grant.grant_id)
    assert [transition.transition_type for transition in transitions] == ["activated"]
