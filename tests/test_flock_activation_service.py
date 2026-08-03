"""Activation preview and transactional grant creation tests (Adaptive Flock plan, Task 13).

A completed, authenticated qualification receipt creates no authority by
itself: durable routing grants appear only through owner-confirmed,
transactional activation that revalidates the receipt HMAC, the expected
receipt revision/digest, the qualified scopes, and every current binding
digest.  Multi-scope activation is all-or-nothing.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.routing.activation_service import (
    ActivationBindings,
    ActivationConflict,
    ActivationRequest,
    ActivationService,
)
from nested_memvid_agent.routing.learned_router import LearnedRouterState
from nested_memvid_agent.routing.qualification_digest import canonical_digest
from nested_memvid_agent.routing.qualification_evaluator import ScopeQualificationResult
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
    QualificationReceipt,
    QualificationRun,
    QualificationRunDraft,
)
from nested_memvid_agent.routing.qualification_replay import ReplayResult
from nested_memvid_agent.state_store import AgentStateStore

OWNER = "owner@example.test"


def run_scope(
    capabilities: tuple[str, ...] = ("repository_inspection",),
) -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk="low",
        capabilities=capabilities,
        policy_id="balanced",
        policy_revision=1,
        target_ids=("target_a", "target_b"),
        target_inventory_digest="1" * 64,
        price_digest="2" * 64,
        learned_config_digest="3" * 64,
        project_authority_digest="4" * 64,
    )


def second_scope() -> QualificationScope:
    return run_scope(("repository_inspection", "testing"))


def run_draft(run_id: str = "qual_activation") -> QualificationRunDraft:
    scope = run_scope()
    return QualificationRunDraft(
        run_id=run_id,
        owner_principal=OWNER,
        scope=scope,
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
        target_snapshot={"targets": list(scope.target_ids)},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
        project_authority={"principal": OWNER},
        build={"version": "0.5.0", "git": "bd2c182"},
        max_spend=MoneyMicros.from_usd_text("50.00"),
        effective_stop_cap=MoneyMicros.from_usd_text("25.00"),
        attempt_ceiling=MoneyMicros.from_usd_text("5.00"),
    )


def scope_result(
    scope: QualificationScope,
    state: str,
    *,
    selected: str | None = None,
) -> ScopeQualificationResult:
    return ScopeQualificationResult(
        scope_digest=scope.digest,
        state=state,  # type: ignore[arg-type]
        static_target_id="target_a",
        selected_target_id=selected,
        total_support=10,
        selected_target_support=5,
        confidence=0.9,
        static_utility=0.5,
        learned_utility=0.7,
        utility_delta=0.2,
        cost_coverage=0.9,
        estimated_savings_usd=0.001,
        estimated_regret_usd=None,
        guardrail_violations=0,
        evaluated_target_ids=("target_a", "target_b"),
        reasons=() if state == "qualified" else ("sparse_evidence",),
        router_state=LearnedRouterState(config_digest="6" * 64),
        thresholds_digest=QualificationThresholds().digest,
    )


def qualified_scope() -> ScopeQualificationResult:
    return scope_result(run_scope(), "qualified", selected="target_b")


def qualified_second_scope() -> ScopeQualificationResult:
    return scope_result(second_scope(), "qualified", selected="target_a")


def _replay(passed: bool, results: tuple[ScopeQualificationResult, ...] = ()) -> ReplayResult:
    digests = ("c" * 64,) * 20 if passed else ("c" * 64,) * 19 + ("d" * 64,)
    return ReplayResult(
        repeats=20,
        completed_repeats=20,
        successes_required=20,
        projection_digests=digests,
        results=results,
        reasons=() if passed else ("replay_drift",),
    )


def current_bindings() -> ActivationBindings:
    scope = run_scope()
    return ActivationBindings(
        project_authority={"principal": OWNER},
        target_snapshot={"targets": list(scope.target_ids)},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
    )


@pytest.fixture
def state(tmp_path: Path) -> AgentStateStore:
    return AgentStateStore(tmp_path / "state" / "agent.db")


@pytest.fixture
def qualification_ledger(state: AgentStateStore) -> QualificationLedger:
    return QualificationLedger(state)


@pytest.fixture
def service(qualification_ledger: QualificationLedger) -> ActivationService:
    return ActivationService(qualification_ledger)


@pytest.fixture
def master_permit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "adaptive")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")


def completed_qualified_receipt(
    ledger: QualificationLedger,
    scopes: tuple[ScopeQualificationResult, ...] = (qualified_scope(),),
) -> QualificationReceipt:
    run = ledger.create_run(run_draft())
    replay = _replay(True, scopes)
    payload = build_terminal_receipt(
        status="completed",
        run=run,
        terminal_reason="matrix_exhausted",
        scopes=replay.results,
        replay=replay,
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


def activation_request(
    receipt: QualificationReceipt,
    *,
    scope_digests: tuple[str, ...] | None = None,
    principal: str = OWNER,
    bindings: ActivationBindings | None = None,
) -> ActivationRequest:
    payload = receipt.payload
    return ActivationRequest(
        receipt_id=receipt.receipt_id,
        scope_digests=scope_digests or (qualified_scope().scope_digest,),
        principal=principal,
        expected_receipt_digest=str(payload["payload_digest"]),
        expected_run_revision=int(payload["run"]["revision"]),
        bindings=bindings or current_bindings(),
    )


def _run(ledger: QualificationLedger) -> QualificationRun:
    run = ledger.get_run("qual_activation")
    assert run is not None
    return run


# --- receipt creates no authority -------------------------------------------------


def test_qualification_receipt_does_not_create_authority(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    assert service.list_grants(receipt_id=receipt.receipt_id) == []
    assert service.list_grants() == []


# --- preview ---------------------------------------------------------------------


def test_preview_shows_exact_qualification_bindings(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    run = _run(qualification_ledger)
    preview = service.preview_activation(
        receipt.receipt_id,
        (qualified_scope().scope_digest,),
        current=current_bindings(),
    )
    assert preview.receipt_id == receipt.receipt_id
    assert preview.run_id == run.run_id
    assert preview.owner_principal == OWNER
    assert preview.receipt_digest == receipt.payload["payload_digest"]
    assert len(preview.scopes) == 1
    scope = preview.scopes[0]
    assert scope.project_id == "project-alpha"
    assert scope.task_family == "repository_inspection"
    assert scope.risk == "low"
    assert scope.capabilities == ("repository_inspection",)
    assert scope.static_target_id == "target_a"
    assert scope.selected_target_id == "target_b"
    assert scope.alternative_target_ids == ("target_a",)
    assert scope.total_support == 10
    assert scope.selected_target_support == 5
    assert scope.confidence == pytest.approx(0.9)
    assert scope.static_utility == pytest.approx(0.5)
    assert scope.learned_utility == pytest.approx(0.7)
    assert scope.utility_delta == pytest.approx(0.2)
    assert scope.cost_coverage == pytest.approx(0.9)
    assert scope.guardrail_violations == 0
    assert scope.qualified is True
    assert preview.replay is not None
    assert preview.replay["passed"] is True
    assert preview.replay["unique_projection_digests"] == 1
    assert preview.target_snapshot == {"targets": ["target_a", "target_b"]}
    assert preview.price_snapshot == {"source": "operator_verified"}
    assert preview.binding_digests["project_authority"] == run.project_authority_digest
    assert preview.binding_digests["target"] == run.target_digest
    assert preview.binding_digests["price"] == run.price_digest
    assert preview.binding_digests["policy"] == run.policy_digest
    assert preview.binding_digests["learned"] == run.learned_digest
    assert preview.binding_changes == {
        "project_authority": False,
        "target_inventory": False,
        "price": False,
        "policy": False,
        "learned": False,
    }
    assert preview.authority_changed is False
    assert "project_authority_changed" in preview.suspension_conditions
    assert "target_inventory_changed" in preview.suspension_conditions
    assert "revocation" in preview.revocation_behavior
    assert service.list_grants() == []


def test_preview_flags_authority_and_inventory_drift(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    drifted = ActivationBindings(
        project_authority={"principal": "other@example.test"},
        target_snapshot={"targets": ["target_a", "target_c"]},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
    )
    preview = service.preview_activation(
        receipt.receipt_id,
        (qualified_scope().scope_digest,),
        current=drifted,
    )
    assert preview.binding_changes["project_authority"] is True
    assert preview.binding_changes["target_inventory"] is True
    assert preview.binding_changes["price"] is False
    assert preview.authority_changed is True


def test_preview_rejects_tampered_receipt(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
) -> None:
    run = qualification_ledger.create_run(run_draft())
    payload = build_terminal_receipt(
        status="completed",
        run=run,
        terminal_reason="matrix_exhausted",
        scopes=[qualified_scope()],
        replay=_replay(True, (qualified_scope(),)),
    )
    stored = qualification_ledger.append_receipt(run.run_id, "run_terminal", payload)
    with pytest.raises(ActivationConflict, match="receipt_authentication_failed"):
        service.preview_activation(
            stored.receipt_id,
            (qualified_scope().scope_digest,),
        )


def test_preview_rejects_unknown_scope(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    with pytest.raises(ValueError, match="unknown receipt scope"):
        service.preview_activation(receipt.receipt_id, ("f" * 64,))


# --- owner confirmation ------------------------------------------------------------


def test_only_owner_principal_can_confirm(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    with pytest.raises(PermissionError, match="owner confirmation required"):
        service.activate_scopes(activation_request(receipt, principal="subagent@example.test"))
    assert service.list_grants() == []


def test_stale_expected_receipt_digest_is_a_conflict(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    request = ActivationRequest(
        receipt_id=receipt.receipt_id,
        scope_digests=(qualified_scope().scope_digest,),
        principal=OWNER,
        expected_receipt_digest="0" * 64,
        expected_run_revision=int(receipt.payload["run"]["revision"]),
        bindings=current_bindings(),
    )
    with pytest.raises(ActivationConflict, match="receipt_digest_changed"):
        service.activate_scopes(request)
    assert service.list_grants() == []


def test_stale_expected_receipt_revision_is_a_conflict(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    request = ActivationRequest(
        receipt_id=receipt.receipt_id,
        scope_digests=(qualified_scope().scope_digest,),
        principal=OWNER,
        expected_receipt_digest=str(receipt.payload["payload_digest"]),
        expected_run_revision=99,
        bindings=current_bindings(),
    )
    with pytest.raises(ActivationConflict, match="receipt_revision_changed"):
        service.activate_scopes(request)
    assert service.list_grants() == []


def test_activation_rejects_tampered_receipt(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    run = qualification_ledger.create_run(run_draft())
    payload = build_terminal_receipt(
        status="completed",
        run=run,
        terminal_reason="matrix_exhausted",
        scopes=[qualified_scope()],
        replay=_replay(True, (qualified_scope(),)),
    )
    forged = {
        **payload,
        "payload_digest": canonical_digest(payload),
        "authentication": {"algorithm": "hmac-sha256", "tag": "forged"},
    }
    stored = qualification_ledger.append_receipt(run.run_id, "run_terminal", forged)
    request = ActivationRequest(
        receipt_id=stored.receipt_id,
        scope_digests=(qualified_scope().scope_digest,),
        principal=OWNER,
        expected_receipt_digest=str(forged["payload_digest"]),
        expected_run_revision=int(payload["run"]["revision"]),
        bindings=current_bindings(),
    )
    with pytest.raises(ActivationConflict, match="receipt_authentication_failed"):
        service.activate_scopes(request)
    assert service.list_grants() == []


# --- qualified scopes and current bindings -----------------------------------------


def test_unqualified_scope_cannot_activate(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    abstained = scope_result(run_scope(), "abstained")
    receipt = completed_qualified_receipt(qualification_ledger, scopes=(abstained,))
    with pytest.raises(ActivationConflict, match="scope_not_qualified"):
        service.activate_scopes(
            activation_request(receipt, scope_digests=(abstained.scope_digest,))
        )
    assert service.list_grants() == []


def test_scope_absent_from_receipt_cannot_activate(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    with pytest.raises(ActivationConflict, match="scope_not_in_receipt"):
        service.activate_scopes(activation_request(receipt, scope_digests=("f" * 64,)))
    assert service.list_grants() == []


def test_multi_scope_activation_is_all_or_nothing(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(
        qualification_ledger,
        scopes=(qualified_scope(), qualified_second_scope()),
    )
    stale_bindings = ActivationBindings(
        project_authority={"principal": "other@example.test"},
        target_snapshot={"targets": ["target_a", "target_b"]},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
    )
    with pytest.raises(ActivationConflict, match="project_authority_changed"):
        service.activate_scopes(
            activation_request(
                receipt,
                scope_digests=(
                    qualified_scope().scope_digest,
                    qualified_second_scope().scope_digest,
                ),
                bindings=stale_bindings,
            )
        )
    assert service.list_grants() == []


@pytest.mark.parametrize(
    ("binding", "value", "reason"),
    [
        ("project_authority", {"principal": "other@example.test"}, "project_authority_changed"),
        ("target_snapshot", {"targets": ["target_a", "target_c"]}, "target_inventory_changed"),
        ("price_snapshot", {"source": "spot_estimate"}, "price_snapshot_changed"),
        ("policy_payload", {"policy_id": "balanced", "revision": 2}, "routing_policy_changed"),
        ("learned_payload", {"state": "enabled"}, "learned_configuration_changed"),
    ],
)
def test_stale_binding_blocks_activation(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
    binding: str,
    value: Any,
    reason: str,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    bindings = ActivationBindings(**{**current_bindings().__dict__, binding: value})
    with pytest.raises(ActivationConflict, match=reason):
        service.activate_scopes(activation_request(receipt, bindings=bindings))
    assert service.list_grants() == []


def test_activation_requires_global_master_permit(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    with pytest.raises(ActivationConflict, match="global_master_permit_required"):
        service.activate_scopes(activation_request(receipt))
    assert service.list_grants() == []


# --- transactional grant creation ---------------------------------------------------


def test_activation_creates_one_grant_and_active_transition_per_scope(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    scopes = (qualified_scope(), qualified_second_scope())
    receipt = completed_qualified_receipt(qualification_ledger, scopes=scopes)
    result = service.activate_scopes(
        activation_request(
            receipt,
            scope_digests=(scopes[0].scope_digest, scopes[1].scope_digest),
        )
    )
    assert len(result.grants) == 2
    assert len(result.transitions) == 2
    assert result.superseded == ()
    grants = service.list_grants(receipt_id=receipt.receipt_id)
    assert len(grants) == 2
    by_target = {grant.target_id: grant for grant in grants}
    assert set(by_target) == {"target_a", "target_b"}
    for grant in grants:
        assert grant.run_id == receipt.run_id
        assert grant.qualification_receipt_id == receipt.receipt_id
        assert grant.created_by == OWNER
        assert grant.policy_id == "balanced"
        assert grant.policy_revision == 1
        transitions = qualification_ledger.list_transitions(grant.grant_id)
        assert [transition.transition_type for transition in transitions] == ["activated"]
        assert transitions[0].receipt_id == receipt.receipt_id
        assert transitions[0].reason == "owner_confirmed_activation"


def test_reactivation_supersedes_prior_grant_in_same_transaction(
    service: ActivationService,
    qualification_ledger: QualificationLedger,
    master_permit: None,
) -> None:
    receipt = completed_qualified_receipt(qualification_ledger)
    first = service.activate_scopes(activation_request(receipt))
    second = service.activate_scopes(activation_request(receipt))
    assert len(first.grants) == 1
    assert len(second.grants) == 1
    old_grant = first.grants[0]
    new_grant = second.grants[0]
    assert old_grant.grant_id != new_grant.grant_id
    old_transitions = qualification_ledger.list_transitions(old_grant.grant_id)
    assert [transition.transition_type for transition in old_transitions] == [
        "activated",
        "revoked",
    ]
    assert old_transitions[1].reason == f"superseded_by_grant:{new_grant.grant_id}"
    assert [transition.grant_id for transition in second.superseded] == [old_grant.grant_id]
    new_transitions = qualification_ledger.list_transitions(new_grant.grant_id)
    assert [transition.transition_type for transition in new_transitions] == ["activated"]
    assert len(service.list_grants()) == 2
