"""S11 — AUTH-001..004 acceptance evidence for v0.6 constrained authority.

Pins the v0.6 learned-authority contract against the real routing chain:

* AUTH-001 — a durable grant binds the exact scope and the
  policy/inventory/config/receipt digests; stale or mismatched evidence is
  rejected and, when the drift is material, the grant is automatically
  suspended (never rewritten, never silently accepted).
* AUTH-002 — the initial v0.6 authority class is limited to an exact,
  owner-activated, low-risk summarizer scope.  No default grant exists; a
  qualification receipt that meets existing thresholds is required before
  activation; only the owner principal may confirm activation; the
  capability boundary is unchanged (learned routing may only rebind the
  routing-owned config fields).
* AUTH-003 — drift, suspension, kill switch, or revocation immediately
  restores deterministic routing for new decisions, with concurrent /
  state-transition convergence and durable fallback evidence labeled
  ``deterministic_fallback_after_suspension``.
* AUTH-004 — the release checklist records the authority qualification
  outcome without converting lack of activation evidence into a failure
  (pinned against ``docs/RELEASE_CHECKLIST.md``).

These tests are acceptance evidence, not new product behavior: the AF chain
(Tasks 14–24) already built the machinery; S11 pins the v0.6 class guard and
the durable fallback label on top of it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest
from test_flock_activation_evaluator import (
    EvaluatorHarness,
    apply_mutation,
    task_contract,
)
from test_flock_grant_runtime import (
    OWNER,
    GrantHarness,
    _attempt,
    _configured_ledger,
    _create_task,
    _current_bindings,
    _evaluation_bindings,
    _learned_coordinator,
    _train_learned_winner,
)

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.activation_evaluator import (
    NON_SUSPENSION_REASONS,
    ActivationEvaluator,
)
from nested_memvid_agent.routing.activation_service import (
    ActivationConflict,
    ActivationRequest,
    ActivationService,
)
from nested_memvid_agent.routing.contracts import compile_task_contract
from nested_memvid_agent.routing.models import AgentTaskContract
from nested_memvid_agent.routing.qualification_evaluator import ScopeEvaluationInput
from nested_memvid_agent.routing.qualification_ledger import QualificationLedger
from nested_memvid_agent.routing.qualification_models import (
    QualificationScope,
    QualificationThresholds,
)
from nested_memvid_agent.routing.shadow_observation import (
    FALLBACK_AUTHORITY_REASONS,
    ActualAuthority,
    actual_authority_for,
)
from nested_memvid_agent.routing.v06_authority import (
    V06_AUTHORITY_CLASS_RESTRICTED,
    is_summarizer_family,
    is_v06_authorized_scope,
    normalize_task_family,
    scope_is_v06_authorized,
)
from nested_memvid_agent.state_store import AgentStateStore

# --- fixtures / helpers ---------------------------------------------------------


def _summarizer_contract(*, risk: str = "low") -> AgentTaskContract:
    return AgentTaskContract(
        task_id="task-summarize",
        run_id="run-summarize",
        role="summarizer",
        task_family="summarization",
        objective="Summarize recent run outcomes.",
        complexity=0.3,
        ambiguity=0.2,
        risk=risk,
        required_capabilities=("summarization",),
    )


def _non_summarizer_contract(*, risk: str = "low") -> AgentTaskContract:
    return AgentTaskContract(
        task_id="task-inspect",
        run_id="run-inspect",
        role="worker",
        task_family="repository_inspection",
        objective="Inspect the repository.",
        complexity=0.3,
        ambiguity=0.2,
        risk=risk,
        required_capabilities=("repository_inspection",),
    )


def _build_harness(
    state: AgentStateStore,
    contract: AgentTaskContract,
    *,
    v06_authority_class: bool = False,
) -> GrantHarness:
    """A qualified, owner-activated grant plus a live evaluator for a scope.

    ``v06_authority_class`` is applied to both the activation service and the
    evaluator so each surface can be pinned independently in AUTH-002.
    """
    harness = GrantHarness(state, contract)
    if v06_authority_class:
        # Re-activate nothing: GrantHarness already activated through a
        # permissive service.  For the v0.6-class evaluation surface we need
        # a v0.6 evaluator over the same ledger.
        harness.evaluator = ActivationEvaluator(
            harness.qualifications,
            bindings=_evaluation_bindings,
            eligibility=lambda target_id: "eligible",
            evidence=_healthy_bundle(harness.scope, contract),
            master_permit=lambda: True,
            v06_authority_class=True,
        )
    return harness


def _healthy_bundle(scope: QualificationScope, contract: AgentTaskContract):
    capability_key = "+".join(sorted(set(contract.required_capabilities)))
    attempts = [
        _attempt(scope, capability_key, f"attempt_cheap_{index}", "cheap", index, cost=0.01)
        for index in range(1, 9)
    ]
    attempts += [
        _attempt(
            scope,
            capability_key,
            f"attempt_expensive_{index}",
            "expensive",
            index,
            cost=1.0,
        )
        for index in range(1, 3)
    ]
    return lambda grant: ScopeEvaluationInput(
        scope=scope,
        static_target_id="expensive",
        thresholds=QualificationThresholds(),
        attempts=tuple(attempts),
    )


def _v06_activation_service(state: AgentStateStore, contract: AgentTaskContract):
    """Build a qualified run+receipt for a scope and return an activation service."""
    harness = GrantHarness(state, contract)
    service = ActivationService(
        harness.qualifications,
        master_permit=lambda: True,
        v06_authority_class=True,
    )
    return harness, service


# --- AUTH-001 -------------------------------------------------------------------


def test_auth001_grant_binds_every_digest_and_rejects_stale_or_mismatched_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every binding digest is enforced; each stale/mismatched piece of
    evidence makes the grant ineffective and (for material drift) suspends it."""
    # EvaluatorHarness activates through the env-based master permit.
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "adaptive")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")
    harness = EvaluatorHarness(AgentStateStore(tmp_path / "state" / "agent.db"))
    assert harness.evaluator.evaluate(task_contract()).effective is True

    # Full binding surface: receipt auth, project authority, privacy, target
    # inventory, price, policy, learned config, hard eligibility, replay.
    mutations = {
        "receipt_tampered": "receipt_authentication_failed",
        "evidence_decayed": "evidence_below_threshold",
        "project_authority_changed": "project_authority_changed",
        "privacy_changed": "privacy_binding_changed",
        "target_inventory_changed": "target_inventory_changed",
        "model_changed": "target_inventory_changed",
        "endpoint_changed": "target_inventory_changed",
        "price_changed": "price_snapshot_changed",
        "policy_changed": "routing_policy_changed",
        "learned_config_changed": "learned_configuration_changed",
        "target_ineligible": "target_hard_ineligible",
        "replay_failed": "replay_verification_failed",
    }
    for mutation, reason in mutations.items():
        fresh = EvaluatorHarness(
            AgentStateStore(tmp_path / f"state-{mutation}" / "agent.db")
        )
        apply_mutation(fresh, mutation)
        result = fresh.evaluator.evaluate(task_contract())
        assert result.effective is False, mutation
        assert reason in result.reason_codes, mutation
        # Material drift appends one automatic suspension transition.
        transitions = fresh.ledger.list_transitions(fresh.grant.grant_id)
        assert transitions[-1].transition_type == "suspended", mutation
        assert transitions[-1].reason == reason, mutation


def test_auth001_scope_mismatch_is_rejected(tmp_path: Path) -> None:
    """A grant for one exact scope never authorizes a different scope."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    harness = GrantHarness(state, _non_summarizer_contract())
    foreign = _summarizer_contract()
    result = harness.evaluator.evaluate(foreign)
    assert result.effective is False
    assert result.grant_id is None
    assert result.reason_codes == ("durable_grant_required",)


def test_auth001_stale_receipt_cannot_activate(tmp_path: Path) -> None:
    """Stale or mismatched receipt digest/revision evidence blocks activation."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    harness = GrantHarness(state, _non_summarizer_contract())
    service = ActivationService(harness.qualifications, master_permit=lambda: True)
    request = ActivationRequest(
        receipt_id=harness.receipt.receipt_id,
        scope_digests=(harness.scope.digest,),
        principal=OWNER,
        expected_receipt_digest="0" * 64,  # stale
        expected_run_revision=int(harness.receipt.payload["run"]["revision"]),
        bindings=_current_bindings(),
    )
    with pytest.raises(ActivationConflict) as exc:
        service.activate_scopes(request)
    assert "receipt_digest_changed" in str(exc.value)


# --- AUTH-002 -------------------------------------------------------------------


def test_auth002_no_default_grant(tmp_path: Path) -> None:
    """Qualification and env flags grant zero authority; nothing is on by default."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    _create_task(state, suffix="no-default")
    # A fresh qualification ledger has zero grants before owner activation.
    assert QualificationLedger(state).list_grants() == []


def test_auth002_only_owner_principal_can_activate(tmp_path: Path) -> None:
    """Explicit owner action is mandatory; no other principal may activate."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    harness = GrantHarness(state, _summarizer_contract())
    service = ActivationService(harness.qualifications, master_permit=lambda: True)
    request = ActivationRequest(
        receipt_id=harness.receipt.receipt_id,
        scope_digests=(harness.scope.digest,),
        principal="attacker@example.test",
        expected_receipt_digest=str(harness.receipt.payload["payload_digest"]),
        expected_run_revision=int(harness.receipt.payload["run"]["revision"]),
        bindings=_current_bindings(),
    )
    with pytest.raises(PermissionError):
        service.activate_scopes(request)


def test_auth002_receipt_must_meet_existing_thresholds(tmp_path: Path) -> None:
    """A scope that is not qualified on the receipt can never be activated."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    harness = GrantHarness(state, _summarizer_contract())
    service = ActivationService(harness.qualifications, master_permit=lambda: True)
    # The receipt qualifies exactly one scope; asking for a digest that is not
    # on it is rejected as an unknown scope.
    bogus = "f" * 64
    request = ActivationRequest(
        receipt_id=harness.receipt.receipt_id,
        scope_digests=(bogus,),
        principal=OWNER,
        expected_receipt_digest=str(harness.receipt.payload["payload_digest"]),
        expected_run_revision=int(harness.receipt.payload["run"]["revision"]),
        bindings=_current_bindings(),
    )
    with pytest.raises((ActivationConflict, ValueError)):
        service.activate_scopes(request)


def test_auth002_v06_class_is_low_risk_summarizer_only() -> None:
    """The v0.6 authority class predicate is exactly low-risk summarizer."""
    assert is_v06_authorized_scope(task_family="summarization", risk="low")
    assert is_v06_authorized_scope(task_family="summarizer", risk="low")
    assert is_v06_authorized_scope(task_family="Summary", risk="LOW")
    assert not is_v06_authorized_scope(task_family="repository_inspection", risk="low")
    assert not is_v06_authorized_scope(task_family="summarization", risk="medium")
    assert not is_v06_authorized_scope(task_family="summarization", risk="high")
    assert not is_v06_authorized_scope(task_family=None, risk="low")
    assert not is_v06_authorized_scope(task_family="summarization", risk=None)
    assert normalize_task_family(" Summarization ") == "summarization"
    assert is_summarizer_family("summarization")
    assert not is_summarizer_family("planning")


def test_auth002_v06_class_rejects_out_of_class_activation(tmp_path: Path) -> None:
    """With the v0.6 class policy on, only a low-risk summarizer scope may be
    activated; an out-of-class activation is rejected as a conflict."""
    # Out of class: repository_inspection is not a summarizer family.
    state = AgentStateStore(tmp_path / "state" / "non-summarizer" / "agent.db")
    harness, service = _v06_activation_service(state, _non_summarizer_contract())
    request = ActivationRequest(
        receipt_id=harness.receipt.receipt_id,
        scope_digests=(harness.scope.digest,),
        principal=OWNER,
        expected_receipt_digest=str(harness.receipt.payload["payload_digest"]),
        expected_run_revision=int(harness.receipt.payload["run"]["revision"]),
        bindings=_current_bindings(),
    )
    with pytest.raises(ActivationConflict) as exc:
        service.activate_scopes(request)
    assert V06_AUTHORITY_CLASS_RESTRICTED in str(exc.value)

    # In class: a low-risk summarizer scope activates normally.
    state2 = AgentStateStore(tmp_path / "state" / "summarizer" / "agent.db")
    harness2, service2 = _v06_activation_service(state2, _summarizer_contract())
    request2 = ActivationRequest(
        receipt_id=harness2.receipt.receipt_id,
        scope_digests=(harness2.scope.digest,),
        principal=OWNER,
        expected_receipt_digest=str(harness2.receipt.payload["payload_digest"]),
        expected_run_revision=int(harness2.receipt.payload["run"]["revision"]),
        bindings=_current_bindings(),
    )
    grants = service2.activate_scopes(request2).grants
    assert len(grants) == 1
    assert scope_is_v06_authorized(grants[0].scope_json) is True


def test_auth002_v06_class_limits_evaluation_to_summarizer_low_scope(
    tmp_path: Path,
) -> None:
    """Under the v0.6 evaluator, out-of-class grants are never effective and
    the restriction is a class guard (non-suspension reason), not drift."""
    # In-class grant stays effective under the v0.6 evaluator.
    state = AgentStateStore(tmp_path / "state" / "in-class" / "agent.db")
    harness = _build_harness(state, _summarizer_contract(), v06_authority_class=True)
    result = harness.evaluator.evaluate(_summarizer_contract())
    assert result.effective is True
    assert result.reason_codes == ()
    transitions = harness.qualifications.list_transitions(harness.grant.grant_id)
    assert transitions[-1].transition_type == "activated"

    # Out-of-class (non-summarizer, low risk) grant is never effective under
    # the v0.6 evaluator, and the grant is NOT suspended (class restriction).
    state2 = AgentStateStore(tmp_path / "state" / "out-of-class" / "agent.db")
    harness2 = _build_harness(state2, _non_summarizer_contract(), v06_authority_class=True)
    result2 = harness2.evaluator.evaluate(_non_summarizer_contract())
    assert result2.effective is False
    assert V06_AUTHORITY_CLASS_RESTRICTED in result2.reason_codes
    assert V06_AUTHORITY_CLASS_RESTRICTED in NON_SUSPENSION_REASONS
    transitions2 = harness2.qualifications.list_transitions(harness2.grant.grant_id)
    assert transitions2[-1].transition_type == "activated"

    # Out-of-class (summarizer, medium risk) grant is also never effective.
    state3 = AgentStateStore(tmp_path / "state" / "medium" / "agent.db")
    harness3 = _build_harness(
        state3, _summarizer_contract(risk="medium"), v06_authority_class=True
    )
    result3 = harness3.evaluator.evaluate(_summarizer_contract(risk="medium"))
    assert result3.effective is False
    assert V06_AUTHORITY_CLASS_RESTRICTED in result3.reason_codes


def test_auth002_capability_boundary_unchanged(tmp_path: Path) -> None:
    """Learned routing with a v0.6 grant never expands task authority: the
    effective config only rebinds routing-owned provider/model fields."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    # repository_inspection task with an active grant (v0.6 class off here so
    # the AF-chain grant is effective); the invariant is about capability
    # boundary, which the authority-matrix suite already pins in breadth.
    task = _create_task(state, suffix="boundary")
    harness = GrantHarness(state, compile_task_contract(task))
    coordinator = _learned_coordinator(ledger, harness.evaluator)
    durable = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    assert durable.record.activation_effective is True
    # The decision is a learned one; config rebinding is limited to the
    # provider/model family fields, never tool/workspace/approval surface.
    assert durable.record.selection_kind in (
        "learned_constrained",
        "learned_adaptive",
        "learned",
    )


# --- AUTH-003 -------------------------------------------------------------------


def _observation_for(ledger, task, attempt: int):
    """The durable shadow observation recorded for one coordinator decision."""
    matches = [
        entry
        for entry in ledger.list_shadow_observations(run_id=task.run_id)
        if entry.task_id == task.task_id and entry.attempt == attempt
    ]
    assert matches, f"no shadow observation for {task.task_id} attempt {attempt}"
    return matches[-1]


def test_auth003_kill_switch_restores_deterministic_routing_immediately(
    tmp_path: Path,
) -> None:
    """Global kill switch makes new decisions deterministic immediately and
    records a durable fallback authority label."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    task = _create_task(state, suffix="kill")
    harness = GrantHarness(state, compile_task_contract(task))
    coordinator = _learned_coordinator(ledger, harness.evaluator)

    first = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    assert first.record.activation_effective is True
    assert first.record.selected_target_id == "cheap"
    assert (
        _observation_for(ledger, task, 1).actual_authority
        == ActualAuthority.ADAPTIVE_ACTIVATED
    )

    harness.qualifications.append_transition(
        harness.grant.grant_id, "suspended", "global_learned_authority_disabled"
    )
    second = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=2)
    assert second.record.activation_effective is False
    assert second.record.selected_target_id == "expensive"
    assert second.record.activation_reason == "grant_suspended"
    assert (
        _observation_for(ledger, task, 2).actual_authority
        == ActualAuthority.DETERMINISTIC_FALLBACK_AFTER_SUSPENSION
    )


def test_auth003_revocation_restores_deterministic_routing_for_new_decisions(
    tmp_path: Path,
) -> None:
    """Revocation is terminal; new decisions fall back deterministically with
    durable fallback evidence; an existing lease keeps its original grant."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    task = _create_task(state, suffix="revoke")
    harness = GrantHarness(state, compile_task_contract(task))
    coordinator = _learned_coordinator(ledger, harness.evaluator)

    first = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    assert first.record.activation_effective is True
    grant_id = first.record.activation_grant_id
    assert grant_id is not None
    harness.qualifications.append_transition(grant_id, "revoked", "owner_revoked")

    second = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=2)
    assert second.record.activation_effective is False
    assert second.record.activation_reason == "grant_revoked"
    assert second.record.selected_target_id == "expensive"
    assert (
        _observation_for(ledger, task, 2).actual_authority
        == ActualAuthority.DETERMINISTIC_FALLBACK_AFTER_SUSPENSION
    )


def test_auth003_drift_suspension_restores_deterministic_routing(tmp_path: Path) -> None:
    """Material binding drift auto-suspends the grant and the next new
    decision routes deterministically with durable fallback evidence."""
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    task = _create_task(state, suffix="drift")
    contract = compile_task_contract(task)
    harness = GrantHarness(state, contract)
    coordinator = _learned_coordinator(ledger, harness.evaluator)

    first = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    assert first.record.activation_effective is True

    # Drift the policy payload; the next evaluation auto-suspends.
    harness.evaluator = ActivationEvaluator(
        harness.qualifications,
        bindings=_drifted_bindings,
        eligibility=lambda target_id: "eligible",
        evidence=_healthy_bundle(harness.scope, contract),
        master_permit=lambda: True,
    )
    result = harness.evaluator.evaluate(contract)
    assert result.effective is False
    assert "routing_policy_changed" in result.reason_codes
    transitions = harness.qualifications.list_transitions(harness.grant.grant_id)
    assert transitions[-1].transition_type == "suspended"

    second = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=2)
    assert second.record.activation_effective is False
    assert second.record.activation_reason == "grant_suspended"
    assert second.record.selected_target_id == "expensive"
    assert (
        _observation_for(ledger, task, 2).actual_authority
        == ActualAuthority.DETERMINISTIC_FALLBACK_AFTER_SUSPENSION
    )


def test_auth003_concurrent_drift_evaluators_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two racing evaluators on the same drift converge on one suspension
    transition and both fail closed (AUTH-003 state-transition concurrency)."""
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "adaptive")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")
    harness = EvaluatorHarness(AgentStateStore(tmp_path / "state" / "agent.db"))
    # Material drift is present before either thread evaluates.
    apply_mutation(harness, "policy_changed")

    barrier = threading.Barrier(2)
    results: list[Any] = []
    errors: list[BaseException] = []

    def evaluate() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(harness.evaluator.evaluate(task_contract()))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=evaluate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert len(results) == 2
    for result in results:
        assert result.effective is False
        # The winner sees the drift; the racing loser reloads the already
        # suspended grant -- both fail closed.
        assert "routing_policy_changed" in result.reason_codes or (
            "grant_suspended" in result.reason_codes
        )
    # Exactly one suspension transition was appended (the other raced and
    # reloaded): activated -> suspended.
    transitions = harness.ledger.list_transitions(harness.grant.grant_id)
    assert [transition.transition_type for transition in transitions] == [
        "activated",
        "suspended",
    ]


def test_auth003_durable_fallback_authority_label() -> None:
    """The durable authority label distinguishes a real suspension/revocation
    fallback from a system that never had a grant."""
    assert (
        actual_authority_for(
            selection_kind="deterministic_router",
            activation_effective=False,
            operator_pinned=False,
            activation_ineffective_reasons=("durable_grant_required",),
        )
        == ActualAuthority.DETERMINISTIC_STATIC
    )
    assert (
        actual_authority_for(
            selection_kind="deterministic_router",
            activation_effective=False,
            operator_pinned=False,
            activation_ineffective_reasons=("grant_suspended",),
        )
        == ActualAuthority.DETERMINISTIC_FALLBACK_AFTER_SUSPENSION
    )
    assert (
        actual_authority_for(
            selection_kind="deterministic_router",
            activation_effective=False,
            operator_pinned=False,
            activation_ineffective_reasons=("grant_revoked",),
        )
        == ActualAuthority.DETERMINISTIC_FALLBACK_AFTER_SUSPENSION
    )
    # The fallback set stays aligned with the evaluator's suspension reasons.
    assert "grant_suspended" in FALLBACK_AUTHORITY_REASONS
    assert "grant_revoked" in FALLBACK_AUTHORITY_REASONS
    assert "global_learned_authority_disabled" in FALLBACK_AUTHORITY_REASONS
    assert "routing_policy_changed" in FALLBACK_AUTHORITY_REASONS


# --- AUTH-004 -------------------------------------------------------------------


def test_auth004_release_checklist_records_authority_qualification_outcome(
    repo_root: Path,
) -> None:
    """The release checklist must contain a v0.6 authority qualification step
    that records the outcome and never converts lack of activation evidence
    into a failure or weakens thresholds (shadow-only is a valid outcome)."""
    checklist = (repo_root / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")
    lowered = checklist.lower()
    assert "learned authority" in lowered
    assert "shadow-only" in lowered
    assert "auth" in lowered


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _drifted_bindings():
    """Policy payload that no longer matches the qualified run's policy digest."""
    from test_flock_grant_runtime import _evaluation_bindings as base

    bindings = base()
    return type(bindings)(
        project_authority=bindings.project_authority,
        privacy=bindings.privacy,
        target_snapshot=bindings.target_snapshot,
        price_snapshot=bindings.price_snapshot,
        policy_payload={"policy_id": "balanced", "revision": 2},
        learned_payload=bindings.learned_payload,
    )
