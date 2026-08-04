"""Owner revocation, supersession, and kill switches (Adaptive Flock plan, Task 16).

Revocation is append-only and terminal: a revoked grant never returns to
active, and reactivation requires fresh qualification plus owner confirmation.
The environment master flag is only a global permit -- it never undoes a
revocation or a suspension.  Scope and global kill switches change effective
routing immediately for new lease decisions without rewriting grant history;
re-enabling restores only grants whose latest transition is still
activated/resumed and whose bindings still verify.  Requalifying the same
exact scope supersedes the old grant through append-only transitions.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from nested_memvid_agent.routing.learned_router import LearnedRouterState
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
        created_at=RECENT.isoformat(),
    )


def healthy_bundle(scope: QualificationScope) -> ScopeEvaluationInput:
    """Evidence that qualifies with target_b selected and full cost coverage."""

    attempts = [
        _attempt(scope, f"attempt_b_{index}", "target_b", index, cost=0.01)
        for index in range(1, 9)
    ]
    attempts += [
        _attempt(scope, f"attempt_a_{index}", "target_a", index, cost=1.0)
        for index in range(1, 3)
    ]
    return ScopeEvaluationInput(
        scope=scope,
        static_target_id="target_a",
        thresholds=QualificationThresholds(),
        attempts=tuple(attempts),
    )


def scope_result(scope: QualificationScope) -> ScopeQualificationResult:
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


def run_draft(run_id: str = "qual_transitions") -> QualificationRunDraft:
    return QualificationRunDraft(
        run_id=run_id,
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


def evaluation_bindings(**changes: Any) -> EvaluationBindings:
    values: dict[str, Any] = {
        "project_authority": PROJECT_AUTHORITY,
        "privacy": baseline_privacy(),
        "target_snapshot": TARGET_SNAPSHOT,
        "price_snapshot": PRICE_SNAPSHOT,
        "policy_payload": POLICY_PAYLOAD,
        "learned_payload": LEARNED_PAYLOAD,
    }
    values.update(changes)
    return EvaluationBindings(**values)


def complete_qualified_run(ledger: QualificationLedger, run_id: str) -> QualificationReceipt:
    """Fresh qualification: a new completed run with an authenticated receipt."""

    run = ledger.create_run(run_draft(run_id))
    result = scope_result(run_scope())
    payload = build_terminal_receipt(
        status="completed",
        run=run,
        terminal_reason="matrix_exhausted",
        scopes=[result],
        replay=_replay((result,)),
    )
    ledger.finalize_run_terminal(
        run.run_id,
        expected_revision=run.revision,
        terminal_status="completed",
        terminal_reason="matrix_exhausted",
        actual_spend=run.actual_spend,
        receipt_payload=payload,
    )
    receipts = ledger.list_receipts(run.run_id)
    assert len(receipts) == 1
    return receipts[0]


def activation_request(receipt: QualificationReceipt) -> ActivationRequest:
    return ActivationRequest(
        receipt_id=receipt.receipt_id,
        scope_digests=(run_scope().digest,),
        principal=OWNER,
        expected_receipt_digest=str(receipt.payload["payload_digest"]),
        expected_run_revision=int(receipt.payload["run"]["revision"]),
        bindings=current_bindings(),
    )


class TransitionHarness:
    """One active grant plus the mutable current-state sources it is checked against."""

    def __init__(self, state: AgentStateStore) -> None:
        self.ledger = QualificationLedger(state)
        self.service = ActivationService(self.ledger)
        self.receipt = complete_qualified_run(self.ledger, "qual_transitions")
        self.grant: ActivationGrant = self.service.activate_scopes(
            activation_request(self.receipt)
        ).grants[0]
        self.env: dict[str, Any] = {
            "bindings": evaluation_bindings(),
            "eligibility": {"target_a": "eligible", "target_b": "eligible"},
            "evidence": healthy_bundle(run_scope()),
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

    def history(self) -> list[str]:
        transitions = self.ledger.list_transitions(self.grant.grant_id)
        return [transition.transition_type for transition in transitions]

    def suspend_via_authority_drift(self) -> None:
        """Drift one binding and evaluate: appends an automatic suspension."""

        self.env["bindings"] = evaluation_bindings(
            project_authority={"principal": "other@example.test"}
        )
        result = self.evaluator.evaluate(task_contract())
        assert result.effective is False
        assert "project_authority_changed" in result.reason_codes
        assert self.latest_transition_type() == "suspended"
        self.env["bindings"] = evaluation_bindings()


@pytest.fixture
def master_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "adaptive")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")


@pytest.fixture
def harness(tmp_path: Path, master_permit: None) -> TransitionHarness:
    return TransitionHarness(AgentStateStore(tmp_path / "state" / "agent.db"))


# --- owner revocation ------------------------------------------------------------


def test_revoked_grant_cannot_be_reactivated(harness: TransitionHarness) -> None:
    harness.service.revoke(harness.grant.grant_id, expected_revision=1)
    with pytest.raises(ValueError, match="fresh qualification"):
        harness.service.activate_existing(harness.grant.grant_id)
    assert harness.history() == ["activated", "revoked"]


def test_revoke_is_revision_checked(harness: TransitionHarness) -> None:
    with pytest.raises(QualificationRevisionConflict):
        harness.service.revoke(harness.grant.grant_id, expected_revision=99)
    assert harness.latest_transition_type() == "activated"


def test_revoke_appends_terminal_transition_without_rewriting_history(
    harness: TransitionHarness,
) -> None:
    transition = harness.service.revoke(harness.grant.grant_id, expected_revision=1)
    assert transition.transition_type == "revoked"
    assert transition.sequence == 2
    assert transition.reason == "owner_revocation"
    assert transition.receipt_id == harness.receipt.receipt_id
    assert harness.history() == ["activated", "revoked"]
    # Revocation is terminal: a second revoke cannot extend the chain.
    with pytest.raises(ValueError, match="revoked"):
        harness.service.revoke(harness.grant.grant_id, expected_revision=2)
    assert harness.history() == ["activated", "revoked"]


def test_revoke_requires_expected_revision(harness: TransitionHarness) -> None:
    # An unchecked revoke is never reachable through the service API.
    with pytest.raises(ValueError, match="expected_revision"):
        harness.service.revoke(harness.grant.grant_id, expected_revision=None)
    assert harness.history() == ["activated"]
    # A wrong revision still conflicts and appends nothing.
    with pytest.raises(QualificationRevisionConflict):
        harness.service.revoke(harness.grant.grant_id, expected_revision=99)
    assert harness.history() == ["activated"]
    # The correct revision still revokes.
    transition = harness.service.revoke(harness.grant.grant_id, expected_revision=1)
    assert transition.transition_type == "revoked"
    assert harness.history() == ["activated", "revoked"]


def test_revoke_unknown_grant_raises(harness: TransitionHarness) -> None:
    with pytest.raises(ValueError, match="unknown activation grant"):
        harness.service.revoke("grant_missing", expected_revision=1)


def test_revoked_grant_evaluates_ineffective(harness: TransitionHarness) -> None:
    harness.service.revoke(harness.grant.grant_id, expected_revision=1)
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is False
    assert result.grant_id == harness.grant.grant_id
    assert result.reason_codes == ("grant_revoked",)


def test_environment_change_cannot_undo_revocation(
    harness: TransitionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.service.revoke(harness.grant.grant_id, expected_revision=1)
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")
    # Even a fresh evaluator reading the environment permit directly must keep
    # the revoked grant ineffective: the flag is a permit, never an undo.
    evaluator = ActivationEvaluator(
        harness.ledger,
        bindings=lambda: harness.env["bindings"],
        eligibility=lambda target_id: harness.env["eligibility"][target_id],
        evidence=lambda grant: harness.env["evidence"],
    )
    result = evaluator.evaluate(task_contract())
    assert result.effective is False
    assert result.reason_codes == ("grant_revoked",)
    assert harness.history() == ["activated", "revoked"]


def test_environment_change_cannot_undo_suspension(
    harness: TransitionHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness.suspend_via_authority_drift()
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is False
    assert result.reason_codes == ("grant_suspended",)


# --- kill switches ----------------------------------------------------------------


def test_scope_kill_switch_suspends_and_stays_ineffective_after_re_enable(
    harness: TransitionHarness,
) -> None:
    harness.env["disabled_scopes"] = frozenset({harness.grant.scope_digest})
    first = harness.evaluator.evaluate(task_contract())
    assert first.effective is False
    assert "scope_learned_authority_disabled" in first.reason_codes
    assert harness.latest_transition_type() == "suspended"
    # Re-enabling the scope does not resurrect the suspended grant.
    harness.env["disabled_scopes"] = frozenset()
    second = harness.evaluator.evaluate(task_contract())
    assert second.effective is False
    assert second.reason_codes == ("grant_suspended",)
    assert harness.history() == ["activated", "suspended"]


def test_global_off_changes_routing_immediately_without_rewriting_history(
    harness: TransitionHarness,
) -> None:
    harness.env["master"] = False
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is False
    assert "global_learned_authority_disabled" in result.reason_codes
    # History is append-only: the activation event is preserved, not rewritten.
    assert harness.history() == ["activated", "suspended"]


def test_global_reenable_restores_still_current_grant(harness: TransitionHarness) -> None:
    harness.env["master"] = False
    harness.env["master"] = True
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is True
    assert result.reason_codes == ()
    assert harness.latest_transition_type() == "activated"


def test_global_reenable_does_not_restore_suspended_grant(
    harness: TransitionHarness,
) -> None:
    harness.env["master"] = False
    first = harness.evaluator.evaluate(task_contract())
    assert first.effective is False
    harness.env["master"] = True
    second = harness.evaluator.evaluate(task_contract())
    assert second.effective is False
    assert second.reason_codes == ("grant_suspended",)
    assert harness.history() == ["activated", "suspended"]


# --- resume and supersession -------------------------------------------------------


def test_owner_resume_reactivates_suspended_grant(harness: TransitionHarness) -> None:
    harness.suspend_via_authority_drift()
    transition = harness.service.activate_existing(
        harness.grant.grant_id,
        expected_revision=2,
    )
    assert transition.transition_type == "resumed"
    assert transition.sequence == 3
    # The evaluator treats a resumed grant as active and re-verifies bindings.
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is True
    assert result.reason_codes == ()


def test_resumed_grant_with_persistent_drift_is_re_suspended(
    harness: TransitionHarness,
) -> None:
    harness.suspend_via_authority_drift()
    harness.service.activate_existing(harness.grant.grant_id, expected_revision=2)
    harness.env["bindings"] = evaluation_bindings(
        project_authority={"principal": "other@example.test"}
    )
    result = harness.evaluator.evaluate(task_contract())
    assert result.effective is False
    assert "project_authority_changed" in result.reason_codes
    assert harness.history() == ["activated", "suspended", "resumed", "suspended"]


def test_resume_is_revision_checked(harness: TransitionHarness) -> None:
    harness.suspend_via_authority_drift()
    with pytest.raises(QualificationRevisionConflict):
        harness.service.activate_existing(harness.grant.grant_id, expected_revision=99)
    assert harness.latest_transition_type() == "suspended"


def test_active_grant_cannot_be_resumed(harness: TransitionHarness) -> None:
    with pytest.raises(ValueError, match="already active"):
        harness.service.activate_existing(harness.grant.grant_id)


def test_requalification_supersedes_active_grant_append_only(
    harness: TransitionHarness,
) -> None:
    fresh_receipt = complete_qualified_run(harness.ledger, "qual_requalification")
    result = harness.service.activate_scopes(activation_request(fresh_receipt))
    new_grant = result.grants[0]
    assert new_grant.grant_id != harness.grant.grant_id
    assert [transition.grant_id for transition in result.superseded] == [
        harness.grant.grant_id
    ]
    old_transitions = harness.ledger.list_transitions(harness.grant.grant_id)
    assert [transition.transition_type for transition in old_transitions] == [
        "activated",
        "revoked",
    ]
    assert old_transitions[1].reason == f"superseded_by_grant:{new_grant.grant_id}"
    new_transitions = harness.ledger.list_transitions(new_grant.grant_id)
    assert [transition.transition_type for transition in new_transitions] == ["activated"]


def test_fresh_qualification_after_revoke_creates_new_grant(
    harness: TransitionHarness,
) -> None:
    harness.service.revoke(harness.grant.grant_id, expected_revision=1)
    fresh_receipt = complete_qualified_run(harness.ledger, "qual_requalification")
    result = harness.service.activate_scopes(activation_request(fresh_receipt))
    new_grant = result.grants[0]
    assert new_grant.grant_id != harness.grant.grant_id
    # The revoked grant was already terminal: supersession never rewrites it.
    assert result.superseded == ()
    assert harness.history() == ["activated", "revoked"]
    new_transitions = harness.ledger.list_transitions(new_grant.grant_id)
    assert [transition.transition_type for transition in new_transitions] == ["activated"]
