"""Effective-grant evaluation and automatic suspensions (Adaptive Flock plan, Task 14).

An activated grant is authority only while every binding it was approved
under still holds at route-decision time.  The activation evaluator
re-verifies, in a deterministic order, the current grant transition, the
global/scope kill switches, the exact scope, the risk gate, receipt
authentication and raw evidence links, project/privacy authority, policy and
learned configuration, inventory and prices, current hard eligibility,
decayed support/confidence/utility/cost coverage, and deterministic replay.
Material binding drift appends an automatic ``suspended`` transition with an
expected latest-transition revision; an ephemeral provider outage never
suspends by itself.
"""

from __future__ import annotations

import base64
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.routing.activation_evaluator import (
    ActivationEvaluator,
    EvaluationBindings,
)
from nested_memvid_agent.routing.activation_service import (
    ActivationBindings,
    ActivationRequest,
    ActivationService,
)
from nested_memvid_agent.routing.models import AgentTaskContract
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
)
from nested_memvid_agent.routing.qualification_receipt import build_terminal_receipt
from nested_memvid_agent.routing.qualification_records import (
    ActivationGrant,
    QualificationReceipt,
    QualificationRevisionConflict,
    QualificationRunDraft,
)
from nested_memvid_agent.routing.qualification_replay import ReplayResult
from nested_memvid_agent.state_store import AgentStateStore

OWNER = "owner@example.test"

TARGET_SNAPSHOT: dict[str, Any] = {
    "targets": [
        {
            "target_id": "target_a",
            "model": "model-a",
            "endpoint": "https://a.example.test",
            "trust_class": "standard",
            "capabilities": ["tag:repository_inspection"],
            "privacy_class": "approved_cloud",
            "locality": "cloud",
            "network_constraints": [],
        },
        {
            "target_id": "target_b",
            "model": "model-b",
            "endpoint": "https://b.example.test",
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

RECENT = datetime(2026, 8, 1, tzinfo=UTC)
OLD = RECENT - timedelta(days=60)


def run_scope() -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk="low",
        capabilities=("repository_inspection",),
        policy_id="balanced",
        policy_revision=1,
        target_ids=("target_a", "target_b"),
        target_inventory_digest="1" * 64,
        price_digest="2" * 64,
        learned_config_digest="3" * 64,
        project_authority_digest="4" * 64,
    )


def task_contract() -> AgentTaskContract:
    return AgentTaskContract(
        task_id="task_1",
        run_id="run_1",
        role="worker",
        task_family="repository_inspection",
        objective="inspect the repository",
        complexity=0.3,
        ambiguity=0.2,
        risk="low",
        required_capabilities=("repository_inspection",),
    )


def _attempt(
    scope: QualificationScope,
    attempt_id: str,
    target_id: str,
    ordinal: int,
    *,
    cost: float | None,
    created_at: datetime,
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
        task_family="repository_inspection",
        risk="low",
        contract_digest="a" * 64,
        project_id="project-alpha",
        capability_key="repository_inspection",
        created_at=created_at.isoformat(),
    )


def healthy_bundle(scope: QualificationScope) -> ScopeEvaluationInput:
    """Evidence that qualifies with target_b selected and full cost coverage."""

    attempts = [
        _attempt(scope, f"attempt_b_{index}", "target_b", index, cost=0.01, created_at=RECENT)
        for index in range(1, 9)
    ]
    attempts += [
        _attempt(scope, f"attempt_a_{index}", "target_a", index, cost=1.0, created_at=RECENT)
        for index in range(1, 3)
    ]
    return ScopeEvaluationInput(
        scope=scope,
        static_target_id="target_a",
        thresholds=QualificationThresholds(),
        attempts=tuple(attempts),
    )


def decayed_bundle(scope: QualificationScope) -> ScopeEvaluationInput:
    """Same evidence, but cost-bearing examples decayed below the coverage gate."""

    attempts = [
        _attempt(scope, f"attempt_b_{index}", "target_b", index, cost=0.01, created_at=RECENT)
        for index in range(1, 3)
    ]
    attempts += [
        _attempt(scope, f"attempt_b_{index}", "target_b", index, cost=None, created_at=OLD)
        for index in range(3, 9)
    ]
    attempts += [
        _attempt(scope, f"attempt_a_{index}", "target_a", index, cost=1.0, created_at=RECENT)
        for index in range(1, 3)
    ]
    return ScopeEvaluationInput(
        scope=scope,
        static_target_id="target_a",
        thresholds=QualificationThresholds(),
        attempts=tuple(attempts),
    )


def scope_result(scope: QualificationScope) -> ScopeQualificationResult:
    from nested_memvid_agent.routing.learned_router import LearnedRouterState

    return ScopeQualificationResult(
        scope_digest=scope.digest,
        state="qualified",
        static_target_id="target_a",
        selected_target_id="target_b",
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
        evaluated_target_ids=("target_a", "target_b"),
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


def run_draft() -> QualificationRunDraft:
    return QualificationRunDraft(
        run_id="qual_evaluation",
        owner_principal=OWNER,
        scope=run_scope(),
        corpus=CorpusManifest(
            schema_version=1,
            items=(
                CorpusItem(
                    item_id="corpus_item_1",
                    task_family="repository_inspection",
                    risk="low",
                    capabilities=("repository_inspection",),
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


def current_bindings() -> ActivationBindings:
    return ActivationBindings(
        project_authority=PROJECT_AUTHORITY,
        target_snapshot=TARGET_SNAPSHOT,
        price_snapshot=PRICE_SNAPSHOT,
        policy_payload=POLICY_PAYLOAD,
        learned_payload=LEARNED_PAYLOAD,
    )


def baseline_privacy() -> dict[str, Any]:
    entry = TARGET_SNAPSHOT["targets"][1]
    return {
        "target_id": "target_b",
        "privacy_class": entry["privacy_class"],
        "locality": entry["locality"],
        "network_constraints": entry["network_constraints"],
    }


class EvaluatorHarness:
    """One active grant plus the mutable current-state sources it is checked against."""

    def __init__(self, state: AgentStateStore) -> None:
        self.ledger = QualificationLedger(state)
        self.service = ActivationService(self.ledger)
        self.key_path = Path(state.path).parent / ".routing-integrity.key"
        run = self.ledger.create_run(run_draft())
        scope = run_scope()
        result = scope_result(scope)
        payload = build_terminal_receipt(
            status="completed",
            run=run,
            terminal_reason="matrix_exhausted",
            scopes=[result],
            replay=_replay((result,)),
        )
        self.ledger.finalize_run_terminal(
            run.run_id,
            expected_revision=run.revision,
            terminal_status="completed",
            terminal_reason="matrix_exhausted",
            actual_spend=run.actual_spend,
            receipt_payload=payload,
        )
        self.receipt: QualificationReceipt = self.ledger.list_receipts(run.run_id)[0]
        request = ActivationRequest(
            receipt_id=self.receipt.receipt_id,
            scope_digests=(scope.digest,),
            principal=OWNER,
            expected_receipt_digest=str(self.receipt.payload["payload_digest"]),
            expected_run_revision=int(self.receipt.payload["run"]["revision"]),
            bindings=current_bindings(),
        )
        self.grant: ActivationGrant = self.service.activate_scopes(request).grants[0]
        self.env: dict[str, Any] = {
            "bindings": EvaluationBindings(
                project_authority=PROJECT_AUTHORITY,
                privacy=baseline_privacy(),
                target_snapshot=TARGET_SNAPSHOT,
                price_snapshot=PRICE_SNAPSHOT,
                policy_payload=POLICY_PAYLOAD,
                learned_payload=LEARNED_PAYLOAD,
            ),
            "eligibility": {"target_a": "eligible", "target_b": "eligible"},
            "evidence": healthy_bundle(scope),
            "master": True,
            "disabled_scopes": frozenset(),
            "clock": lambda: datetime.now(UTC),
        }
        self.evaluator = ActivationEvaluator(
            self.ledger,
            bindings=lambda: self.env["bindings"],
            eligibility=lambda target_id: self.env["eligibility"][target_id],
            evidence=lambda grant: self.env["evidence"],
            master_permit=lambda: bool(self.env["master"]),
            disabled_scopes=lambda: self.env["disabled_scopes"],
            clock=lambda: self.env["clock"](),
        )

    def latest_transition_type(self) -> str:
        transitions = self.ledger.list_transitions(self.grant.grant_id)
        return transitions[-1].transition_type


def _tamper_receipt_key(harness: EvaluatorHarness) -> None:
    """Replace the owner routing key so receipt authentication fails closed."""

    forged = base64.b64encode(b"\x01" * 32).decode("ascii")
    harness.key_path.write_text(forged, encoding="utf-8")
    os.chmod(harness.key_path, 0o600)


def _replace_target_entry(**changes: Any) -> dict[str, Any]:
    entry = dict(TARGET_SNAPSHOT["targets"][1])
    entry.update(changes)
    return {"targets": [TARGET_SNAPSHOT["targets"][0], entry]}


def apply_mutation(harness: EvaluatorHarness, mutation: str) -> None:
    if mutation == "receipt_tampered":
        _tamper_receipt_key(harness)
    elif mutation == "evidence_decayed":
        harness.env["evidence"] = decayed_bundle(run_scope())
    elif mutation == "project_authority_changed":
        harness.env["bindings"] = _bindings(
            harness, project_authority={"principal": "other@example.test"}
        )
    elif mutation == "privacy_changed":
        harness.env["bindings"] = _bindings(
            harness, privacy={**baseline_privacy(), "privacy_class": "local_required"}
        )
    elif mutation == "target_inventory_changed":
        harness.env["bindings"] = _bindings(
            harness, target_snapshot=_replace_target_entry(trust_class="unconfirmed")
        )
    elif mutation == "model_changed":
        harness.env["bindings"] = _bindings(
            harness, target_snapshot=_replace_target_entry(model="model-c")
        )
    elif mutation == "endpoint_changed":
        harness.env["bindings"] = _bindings(
            harness, target_snapshot=_replace_target_entry(endpoint="https://c.example.test")
        )
    elif mutation == "price_changed":
        harness.env["bindings"] = _bindings(harness, price_snapshot={"source": "spot_estimate"})
    elif mutation == "policy_changed":
        harness.env["bindings"] = _bindings(
            harness, policy_payload={"policy_id": "balanced", "revision": 2}
        )
    elif mutation == "learned_config_changed":
        harness.env["bindings"] = _bindings(harness, learned_payload={"state": "enabled"})
    elif mutation == "target_ineligible":
        harness.env["eligibility"]["target_b"] = "hard_ineligible"
    elif mutation == "replay_failed":
        harness.env["clock"] = lambda: datetime(2026, 8, 3)  # naive: replay fails closed
    elif mutation == "global_kill_switch":
        harness.env["master"] = False
    else:
        raise AssertionError(f"unknown mutation: {mutation}")


def _bindings(harness: EvaluatorHarness, **changes: Any) -> EvaluationBindings:
    current = harness.env["bindings"]
    values = {
        "project_authority": current.project_authority,
        "privacy": current.privacy,
        "target_snapshot": current.target_snapshot,
        "price_snapshot": current.price_snapshot,
        "policy_payload": current.policy_payload,
        "learned_payload": current.learned_payload,
    }
    values.update(changes)
    return EvaluationBindings(**values)


@pytest.fixture
def master_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "adaptive")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")


@pytest.fixture
def harness(tmp_path: Path, master_permit: None) -> EvaluatorHarness:
    return EvaluatorHarness(AgentStateStore(tmp_path / "state" / "agent.db"))


# --- material drift suspends -------------------------------------------------


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("receipt_tampered", "receipt_authentication_failed"),
        ("evidence_decayed", "evidence_below_threshold"),
        ("project_authority_changed", "project_authority_changed"),
        ("privacy_changed", "privacy_binding_changed"),
        ("target_inventory_changed", "target_inventory_changed"),
        ("model_changed", "target_inventory_changed"),
        ("endpoint_changed", "target_inventory_changed"),
        ("price_changed", "price_snapshot_changed"),
        ("policy_changed", "routing_policy_changed"),
        ("learned_config_changed", "learned_configuration_changed"),
        ("target_ineligible", "target_hard_ineligible"),
        ("replay_failed", "replay_verification_failed"),
        ("global_kill_switch", "global_learned_authority_disabled"),
    ],
)
def test_material_drift_suspends_grant(
    harness: EvaluatorHarness, mutation: str, reason: str
) -> None:
    apply_mutation(harness, mutation)
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is False
    assert reason in result.reason_codes
    assert harness.latest_transition_type() == "suspended"
    transition = harness.ledger.list_transitions(harness.grant.grant_id)[-1]
    assert transition.reason == reason
    assert transition.receipt_id == harness.receipt.receipt_id


# --- intact bindings keep the grant effective ---------------------------------


def test_intact_grant_is_effective(harness: EvaluatorHarness) -> None:
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is True
    assert result.reason_codes == ()
    assert result.grant_id == harness.grant.grant_id
    assert result.receipt_id == harness.receipt.receipt_id
    assert result.learned_state is not None
    assert harness.latest_transition_type() == "activated"


def test_ephemeral_provider_outage_does_not_suspend(harness: EvaluatorHarness) -> None:
    harness.env["eligibility"]["target_b"] = "outage"
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is True
    assert harness.latest_transition_type() == "activated"


# --- convergence and revision checking ----------------------------------------


def test_repeated_evaluation_converges_without_duplicate_suspension(
    harness: EvaluatorHarness,
) -> None:
    apply_mutation(harness, "project_authority_changed")
    first = harness.evaluator.evaluate(task_contract())
    second = harness.evaluator.evaluate(task_contract())
    assert first.effective is False
    assert second.effective is False
    assert "grant_suspended" in second.reason_codes
    transitions = harness.ledger.list_transitions(harness.grant.grant_id)
    assert [transition.transition_type for transition in transitions] == [
        "activated",
        "suspended",
    ]


def test_suspension_requires_expected_latest_sequence(harness: EvaluatorHarness) -> None:
    with pytest.raises(QualificationRevisionConflict):
        harness.ledger.suspend_grant(
            harness.grant.grant_id,
            reason="project_authority_changed",
            expected_sequence=99,
        )
    assert harness.latest_transition_type() == "activated"


# --- scope without a grant -----------------------------------------------------


def test_scope_without_grant_has_no_authority(harness: EvaluatorHarness) -> None:
    foreign = AgentTaskContract(
        task_id="task_2",
        run_id="run_2",
        role="worker",
        task_family="code_modification",
        objective="modify code",
        complexity=0.3,
        ambiguity=0.2,
        risk="low",
        required_capabilities=("code_modification",),
    )
    result = harness.evaluator.evaluate(foreign)
    assert result.effective is False
    assert result.grant_id is None
    assert result.reason_codes == ("durable_grant_required",)
    assert harness.latest_transition_type() == "activated"
