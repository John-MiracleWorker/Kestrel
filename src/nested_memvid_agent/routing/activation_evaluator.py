"""Effective-grant evaluation and automatic suspensions (Adaptive Flock plan, Task 14).

An ``activated`` grant is authority only while every binding the owner
approved still holds at route-decision time.  The evaluator re-verifies, in
one deterministic order, the grant and every current binding:

1. current grant transition;
2. global/scope kill switches;
3. exact project/family/risk/capability scope;
4. low/medium risk;
5. receipt authentication and raw evidence links;
6. project/privacy authority;
7. policy/learned configuration;
8. inventory/endpoint/model/trust/capabilities/prices;
9. current hard eligibility;
10. decayed support/confidence/utility/cost coverage;
11. deterministic replay.

Material binding drift appends an automatic ``suspended`` transition with
the expected latest-transition revision; racing evaluators converge on the
same terminal state (one appends, the others reload).  Only safe reason
codes are returned -- never secret material.  An ephemeral provider outage
yields normal routing failure/fallback downstream and never suspends a grant
or rewrites historical quality by itself.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..control_plane_integrity import ControlPlaneIntegrity
from .activation_service import (
    env_master_permit,
    load_verification_integrity,
    receipt_authenticates,
)
from .learned_router import LearnedRouterState
from .models import AgentTaskContract
from .qualification_digest import canonical_digest
from .qualification_evaluator import (
    ScopeEvaluationInput,
    evaluate_scope,
)
from .qualification_ledger import QualificationLedger
from .qualification_models import QualificationThresholds
from .qualification_records import (
    ActivationGrant,
    QualificationRevisionConflict,
    QualificationRun,
)
from .qualification_replay import QualificationReplayer

__all__ = [
    "NON_SUSPENSION_REASONS",
    "SUSPENSION_REASONS",
    "TARGET_ELIGIBILITY_STATES",
    "ActivationEvaluation",
    "ActivationEvaluator",
    "EvaluationBindings",
]

#: Reason codes that append an automatic ``suspended`` transition (the
#: Task 13 SUSPENSION_CONDITIONS vocabulary plus the scope kill switch).
SUSPENSION_REASONS: tuple[str, ...] = (
    "receipt_authentication_failed",
    "evidence_below_threshold",
    "project_authority_changed",
    "privacy_binding_changed",
    "target_inventory_changed",
    "price_snapshot_changed",
    "routing_policy_changed",
    "learned_configuration_changed",
    "target_hard_ineligible",
    "replay_verification_failed",
    "global_learned_authority_disabled",
    "scope_learned_authority_disabled",
)

#: Safe reason codes that make a grant ineffective without suspending it.
NON_SUSPENSION_REASONS: tuple[str, ...] = (
    "durable_grant_required",
    "grant_suspended",
    "grant_revoked",
    "high_risk_deterministic_only",
)

TARGET_ELIGIBILITY_STATES: tuple[str, ...] = (
    "eligible",
    "hard_ineligible",
    "outage",
)

_DETERMINISTIC_ONLY_RISKS: tuple[str, ...] = ("high", "critical")


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


@dataclass(frozen=True)
class EvaluationBindings:
    """Current binding payloads recomputed at route-decision time."""

    project_authority: Mapping[str, Any]
    privacy: Mapping[str, Any]
    target_snapshot: Mapping[str, Any]
    price_snapshot: Mapping[str, Any]
    policy_payload: Mapping[str, Any]
    learned_payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        for name in (
            "project_authority",
            "privacy",
            "target_snapshot",
            "price_snapshot",
            "policy_payload",
            "learned_payload",
        ):
            if not isinstance(getattr(self, name), Mapping):
                raise ValueError(f"{name} must be a mapping payload")


@dataclass(frozen=True)
class ActivationEvaluation:
    """Effective-grant verdict for one route decision."""

    effective: bool
    grant_id: str | None
    receipt_id: str | None
    reason_codes: tuple[str, ...]
    learned_state: LearnedRouterState | None


def _capability_key(capabilities: Sequence[str]) -> str:
    return "+".join(sorted({str(capability) for capability in capabilities}))


def _scope_matches_contract(scope: Mapping[str, Any], contract: AgentTaskContract) -> bool:
    return (
        str(scope.get("task_family") or "") == contract.task_family
        and str(scope.get("risk") or "") == contract.risk
        and str(scope.get("capability_key") or "")
        == _capability_key(contract.required_capabilities)
    )


def _qualification_privacy_binding(
    run: QualificationRun,
    target_id: str,
) -> Mapping[str, Any] | None:
    """Privacy binding the grant target carried at qualification time."""

    try:
        snapshot = json.loads(run.target_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(snapshot, Mapping):
        return None
    targets = snapshot.get("targets")
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        return None
    for entry in targets:
        if isinstance(entry, Mapping) and entry.get("target_id") == target_id:
            return {
                "target_id": target_id,
                "privacy_class": entry.get("privacy_class"),
                "locality": entry.get("locality"),
                "network_constraints": entry.get("network_constraints"),
            }
    return None


def _receipt_scope_link(receipt_payload: Mapping[str, Any], grant: ActivationGrant) -> bool:
    """The receipt must bind the grant scope as qualified with the grant target."""

    scopes = receipt_payload.get("scopes")
    if not isinstance(scopes, Sequence) or isinstance(scopes, (str, bytes)):
        return False
    for entry in scopes:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("scope_digest") != grant.scope_digest:
            continue
        return entry.get("qualified") is True and entry.get("selected_target_id") == grant.target_id
    return False


class ActivationEvaluator:
    """Evaluate effective grants and append automatic suspensions."""

    def __init__(
        self,
        ledger: QualificationLedger,
        *,
        bindings: Callable[[], EvaluationBindings],
        eligibility: Callable[[str], str],
        evidence: Callable[[ActivationGrant], ScopeEvaluationInput],
        integrity: ControlPlaneIntegrity | None = None,
        master_permit: Callable[[], bool] | None = None,
        disabled_scopes: Callable[[], frozenset[str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(ledger, QualificationLedger):
            raise ValueError("ledger must be a QualificationLedger")
        for name, provider in (
            ("bindings", bindings),
            ("eligibility", eligibility),
            ("evidence", evidence),
            ("master_permit", master_permit),
            ("disabled_scopes", disabled_scopes),
            ("clock", clock),
        ):
            if provider is not None and not callable(provider):
                raise ValueError(f"{name} must be callable")
        self._ledger = ledger
        self._bindings = bindings
        self._eligibility = eligibility
        self._evidence = evidence
        self._integrity = integrity
        self._master_permit = master_permit or env_master_permit
        self._disabled_scopes = disabled_scopes or (lambda: frozenset())
        self._clock = clock or (lambda: datetime.now(UTC))

    def evaluate(self, contract: AgentTaskContract) -> ActivationEvaluation:
        """Evaluate the effective grant for one new route decision."""

        if not isinstance(contract, AgentTaskContract):
            raise ValueError("contract must be an AgentTaskContract")
        grant = self._resolve_grant(contract)
        if grant is None:
            return ActivationEvaluation(
                effective=False,
                grant_id=None,
                receipt_id=None,
                reason_codes=("durable_grant_required",),
                learned_state=None,
            )
        receipt_id = grant.qualification_receipt_id
        transitions = self._ledger.list_transitions(grant.grant_id)
        latest = transitions[-1] if transitions else None

        # 1. current grant transition.
        if latest is None or latest.transition_type != "activated":
            code = (
                "grant_revoked"
                if latest is not None and latest.transition_type == "revoked"
                else "grant_suspended"
            )
            return ActivationEvaluation(
                effective=False,
                grant_id=grant.grant_id,
                receipt_id=receipt_id,
                reason_codes=(code,),
                learned_state=None,
            )

        reasons: list[str] = []

        # 2. global/scope kill switches.
        if not self._master_permit():
            reasons.append("global_learned_authority_disabled")
        if grant.scope_digest in self._disabled_scopes():
            reasons.append("scope_learned_authority_disabled")

        # 3. exact project/family/risk/capability scope: grant resolution is
        #    the exact match, so a resolved grant always holds this step.
        # 4. low/medium risk: high/critical scopes stay deterministic-only.
        if contract.risk in _DETERMINISTIC_ONLY_RISKS:
            reasons.append("high_risk_deterministic_only")

        run = self._ledger.get_run(grant.run_id)
        receipt = self._ledger.get_receipt(receipt_id)
        bundle: ScopeEvaluationInput | None = None
        learned_state: LearnedRouterState | None = None

        # 5. receipt authentication and raw evidence links (fail-closed).
        if run is None or receipt is None:
            reasons.append("receipt_authentication_failed")
        else:
            try:
                integrity = load_verification_integrity(self._ledger, self._integrity)
            except (OSError, ValueError):
                integrity = None
            if integrity is None or not receipt_authenticates(receipt, integrity=integrity):
                reasons.append("receipt_authentication_failed")
            elif not _receipt_scope_link(receipt.payload, grant):
                reasons.append("receipt_authentication_failed")

        if run is not None:
            bindings = self._bindings()
            if not isinstance(bindings, EvaluationBindings):
                raise ValueError("bindings provider must return EvaluationBindings")
            self._check_binding_digests(run, grant, bindings, reasons)
            self._check_hard_eligibility(grant, reasons)
            bundle, learned_state = self._check_decayed_metrics(grant, run, reasons)
            self._check_replay(grant, run, bundle, reasons)

        effective = not reasons
        if not effective:
            self._suspend_on_drift(grant, latest.sequence, receipt_id, reasons)
        return ActivationEvaluation(
            effective=effective,
            grant_id=grant.grant_id,
            receipt_id=receipt_id,
            reason_codes=tuple(reasons),
            learned_state=learned_state,
        )

    # -- deterministic binding steps ------------------------------------------

    def _check_binding_digests(
        self,
        run: QualificationRun,
        grant: ActivationGrant,
        bindings: EvaluationBindings,
        reasons: list[str],
    ) -> None:
        # 6. project/privacy authority.
        if canonical_digest(bindings.project_authority) != run.project_authority_digest:
            reasons.append("project_authority_changed")
        baseline_privacy = _qualification_privacy_binding(run, grant.target_id)
        if baseline_privacy is None or (
            canonical_digest(bindings.privacy) != canonical_digest(baseline_privacy)
        ):
            reasons.append("privacy_binding_changed")
        # 7. policy/learned configuration.
        if canonical_digest(bindings.policy_payload) != run.policy_digest:
            reasons.append("routing_policy_changed")
        if canonical_digest(bindings.learned_payload) != run.learned_digest:
            reasons.append("learned_configuration_changed")
        # 8. inventory/endpoint/model/trust/capabilities/prices.
        if canonical_digest(bindings.target_snapshot) != run.target_digest:
            reasons.append("target_inventory_changed")
        if canonical_digest(bindings.price_snapshot) != run.price_digest:
            reasons.append("price_snapshot_changed")

    def _check_hard_eligibility(self, grant: ActivationGrant, reasons: list[str]) -> None:
        # 9. current hard eligibility; an ephemeral outage never suspends.
        state = self._eligibility(grant.target_id)
        if state not in TARGET_ELIGIBILITY_STATES:
            raise ValueError(f"unsupported target eligibility state: {state}")
        if state == "hard_ineligible":
            reasons.append("target_hard_ineligible")

    def _check_decayed_metrics(
        self,
        grant: ActivationGrant,
        run: QualificationRun,
        reasons: list[str],
    ) -> tuple[ScopeEvaluationInput | None, LearnedRouterState | None]:
        # 10. decayed support/confidence/utility/cost coverage.
        try:
            bundle = self._evidence(grant)
        except Exception:  # noqa: BLE001 - unreadable evidence fails closed
            reasons.append("evidence_below_threshold")
            return None, None
        if not isinstance(bundle, ScopeEvaluationInput):
            raise ValueError("evidence provider must return ScopeEvaluationInput")
        try:
            result = evaluate_scope(bundle)
        except Exception:  # noqa: BLE001 - unevaluable evidence fails closed
            reasons.append("evidence_below_threshold")
            return bundle, None
        if not result.qualified or result.selected_target_id != grant.target_id:
            reasons.append("evidence_below_threshold")
        return bundle, result.router_state

    def _check_replay(
        self,
        grant: ActivationGrant,
        run: QualificationRun,
        bundle: ScopeEvaluationInput | None,
        reasons: list[str],
    ) -> None:
        # 11. deterministic replay over the exact ordered evidence set.
        if bundle is None:
            return
        thresholds = QualificationThresholds(**json.loads(run.thresholds_json))
        reference_time = self._clock()
        replayer = QualificationReplayer(clock=lambda: reference_time)
        try:
            replay = replayer.replay(
                [bundle],
                repeats=thresholds.replay_runs,
                successes_required=thresholds.replay_successes_required,
            )
        except (TypeError, ValueError):
            replay = None
        if replay is None or not replay.passed:
            reasons.append("replay_verification_failed")

    # -- automatic suspension ---------------------------------------------------

    def _suspend_on_drift(
        self,
        grant: ActivationGrant,
        expected_sequence: int,
        receipt_id: str,
        reasons: Sequence[str],
    ) -> None:
        suspension_reasons = [reason for reason in reasons if reason in SUSPENSION_REASONS]
        if not suspension_reasons:
            return
        try:
            self._ledger.suspend_grant(
                grant.grant_id,
                reason=suspension_reasons[0],
                expected_sequence=expected_sequence,
                receipt_id=receipt_id,
            )
        except (QualificationRevisionConflict, ValueError):
            # A racing evaluator already moved the grant; reloading converges
            # both evaluators on the same terminal state.
            pass

    def _resolve_grant(self, contract: AgentTaskContract) -> ActivationGrant | None:
        matches = [
            grant
            for grant in self._ledger.list_grants()
            if _scope_matches_contract(json.loads(grant.scope_json), contract)
        ]
        if not matches:
            return None
        return matches[-1]
