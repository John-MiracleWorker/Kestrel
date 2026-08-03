"""Exact owner-approved activation grants (Adaptive Flock plan, Task 13).

A completed, authenticated qualification receipt is evidence, not authority:
it creates no routing power by itself.  Authority appears only when the owner
principal confirms an activation preview, at which point one immutable base
grant and one append-only ``activated`` transition (the activation event) are
inserted per exact scope inside a single all-or-nothing transaction.

The activation transaction:

1. verifies the confirming principal is the run owner and that the expected
   receipt revision/digest still match the stored authenticated receipt;
2. verifies the receipt HMAC authentication envelope (fail-closed);
3. verifies the run and receipt are ``completed`` and every selected scope is
   qualified with a selected learned target;
4. recomputes the project authority, target inventory, price, policy, and
   learned configuration digests against the qualification-time bindings;
5. requires the global master permit (the learned-replay-verified flag) but
   never derives authority from it -- authority comes only from the owner
   confirmation bound to the authenticated receipt;
6. inserts one base grant and one ``activated`` transition per exact scope;
7. supersedes any older exact-scope grant in the same transaction by
   appending a terminal ``revoked`` transition with a
   ``superseded_by_grant:<grant_id>`` reason (the schema v4 transition
   vocabulary has no ``superseded`` type; supersession is an append-only
   revocation carrying the supersession reason);
8. the ``activated`` transition row is the durable activation event: the
   schema v4 terminal-run evidence trigger bars new rows on
   ``routing_qualification_events`` once the run is completed, so the
   append-only transition chain is the event log.

Task 16 adds owner revocation and resume on top of the same append-only
chain: :meth:`ActivationService.revoke` appends a terminal ``revoked``
transition checked against the expected latest-transition revision, and a
revoked grant can never return to active -- reactivation requires fresh
qualification plus owner confirmation.  :meth:`ActivationService.activate_existing`
resumes a suspended grant through an append-only ``resumed`` transition;
the route-time evaluator re-verifies every binding, so resuming a grant
whose drift persists simply re-suspends on the next decision.  The
environment master flag remains only a global permit: it never undoes a
revocation or a suspension.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..control_plane_integrity import ControlPlaneIntegrity
from .qualification_digest import canonical_digest
from .qualification_ledger import QualificationLedger
from .qualification_receipt import verify_terminal_receipt
from .qualification_records import (
    ActivationGrant,
    ActivationGrantDraft,
    ActivationTransition,
    QualificationReceipt,
    QualificationRevisionConflict,
    QualificationRun,
)

__all__ = [
    "REVOCATION_BEHAVIOR",
    "SUSPENSION_CONDITIONS",
    "ActivationBindings",
    "ActivationConflict",
    "ActivationPreview",
    "ActivationRequest",
    "ActivationResult",
    "ActivationScopePreview",
    "ActivationService",
    "env_master_permit",
    "load_verification_integrity",
    "receipt_authenticates",
]

#: Material drift conditions that suspend a grant after activation (the
#: deterministic evaluator vocabulary shared with the Task 14 evaluator).
SUSPENSION_CONDITIONS: tuple[str, ...] = (
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
)

REVOCATION_BEHAVIOR = (
    "revocation is append-only and terminal; a revoked grant never returns "
    "to active and reactivation requires fresh qualification plus owner "
    "confirmation"
)

_ACTIVATION_REASON = "owner_confirmed_activation"
_REVOCATION_REASON = "owner_revocation"
_RESUME_REASON = "owner_resumed_activation"

_BINDING_CHECK_ORDER: tuple[str, ...] = (
    "project_authority",
    "target_inventory",
    "price",
    "policy",
    "learned",
)

_BINDING_CONFLICT_REASONS: dict[str, str] = {
    "project_authority": "project_authority_changed",
    "target_inventory": "target_inventory_changed",
    "price": "price_snapshot_changed",
    "policy": "routing_policy_changed",
    "learned": "learned_configuration_changed",
}


class ActivationConflict(RuntimeError):
    """All-or-nothing activation conflict with a stable machine reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ActivationBindings:
    """Current binding payloads recomputed against qualification-time digests."""

    project_authority: Mapping[str, Any]
    target_snapshot: Mapping[str, Any]
    price_snapshot: Mapping[str, Any]
    policy_payload: Mapping[str, Any]
    learned_payload: Mapping[str, Any]

    def digest_changes(self, run: QualificationRun) -> dict[str, bool]:
        return {
            "project_authority": (
                canonical_digest(self.project_authority) != run.project_authority_digest
            ),
            "target_inventory": (canonical_digest(self.target_snapshot) != run.target_digest),
            "price": canonical_digest(self.price_snapshot) != run.price_digest,
            "policy": canonical_digest(self.policy_payload) != run.policy_digest,
            "learned": canonical_digest(self.learned_payload) != run.learned_digest,
        }


@dataclass(frozen=True)
class ActivationRequest:
    """Owner confirmation for activating exact qualified scopes from one receipt."""

    receipt_id: str
    scope_digests: tuple[str, ...]
    principal: str
    expected_receipt_digest: str
    expected_run_revision: int
    bindings: ActivationBindings

    def __post_init__(self) -> None:
        _require_text(self.receipt_id, "receipt_id")
        _require_text(self.principal, "principal")
        _require_text(self.expected_receipt_digest, "expected_receipt_digest")
        if isinstance(self.expected_run_revision, bool) or self.expected_run_revision < 1:
            raise ValueError("expected_run_revision must be a positive integer")
        if not isinstance(self.scope_digests, tuple) or not self.scope_digests:
            raise ValueError("scope_digests must be a non-empty tuple")
        for digest in self.scope_digests:
            _require_text(digest, "scope_digest")
        if len(set(self.scope_digests)) != len(self.scope_digests):
            raise ValueError("scope_digests must not contain duplicates")
        if not isinstance(self.bindings, ActivationBindings):
            raise ValueError("bindings must be an ActivationBindings value")


@dataclass(frozen=True)
class ActivationScopePreview:
    scope_digest: str
    project_id: str
    task_family: str
    risk: str
    capabilities: tuple[str, ...]
    static_target_id: str
    selected_target_id: str | None
    alternative_target_ids: tuple[str, ...]
    total_support: int
    selected_target_support: int
    confidence: float
    static_utility: float | None
    learned_utility: float | None
    utility_delta: float
    cost_coverage: float
    estimated_savings_usd: float | None
    guardrail_violations: int
    reasons: tuple[str, ...]
    qualified: bool


@dataclass(frozen=True)
class ActivationPreview:
    """What the owner confirms: every binding behind the requested grants."""

    receipt_id: str
    run_id: str
    run_revision: int
    owner_principal: str
    receipt_digest: str
    scopes: tuple[ActivationScopePreview, ...]
    replay: Mapping[str, Any] | None
    target_snapshot: Mapping[str, Any]
    price_snapshot: Mapping[str, Any]
    binding_digests: Mapping[str, str]
    binding_changes: Mapping[str, bool]
    authority_changed: bool
    suspension_conditions: tuple[str, ...]
    revocation_behavior: str


@dataclass(frozen=True)
class ActivationResult:
    grants: tuple[ActivationGrant, ...]
    transitions: tuple[ActivationTransition, ...]
    superseded: tuple[ActivationTransition, ...]


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def env_master_permit() -> bool:
    """Global master permit projection; never sufficient authority by itself."""

    from .runtime import AdaptiveFlockRuntimeConfig

    try:
        return AdaptiveFlockRuntimeConfig.from_env().learned_activation_replay_verified
    except ValueError:
        return False


def load_verification_integrity(
    ledger: QualificationLedger,
    integrity: ControlPlaneIntegrity | None,
) -> ControlPlaneIntegrity:
    """Load the receipt verification key fail-closed; never mint key material.

    Shared with the Task 14 activation evaluator: a missing or unsafe owner
    key raises, and the caller fails closed as an authentication failure.
    """

    if integrity is not None:
        return integrity
    return ControlPlaneIntegrity(
        Path(ledger.state.path).parent,
        create_if_missing=False,
    )


def receipt_authenticates(
    receipt: QualificationReceipt,
    *,
    integrity: ControlPlaneIntegrity,
) -> bool:
    """Fail-closed terminal receipt authentication (never mints key material)."""

    return verify_terminal_receipt(receipt.payload, integrity=integrity)


class ActivationService:
    """Preview and transactionally confirm exact owner-approved routing grants."""

    def __init__(
        self,
        ledger: QualificationLedger,
        *,
        integrity: ControlPlaneIntegrity | None = None,
        master_permit: Callable[[], bool] | None = None,
    ) -> None:
        self._ledger = ledger
        self._integrity = integrity
        self._master_permit = master_permit or env_master_permit

    def list_grants(self, *, receipt_id: str | None = None) -> list[ActivationGrant]:
        return self._ledger.list_grants(receipt_id=receipt_id)

    def preview_activation(
        self,
        receipt_id: str,
        scope_digests: Sequence[str],
        *,
        current: ActivationBindings | None = None,
    ) -> ActivationPreview:
        """Show the exact authority one owner confirmation would create."""

        receipt, run, entries = self._receipt_scopes(receipt_id, scope_digests)
        self._verify_receipt(receipt)
        run_scope = json.loads(run.scope_json)
        binding_changes = current.digest_changes(run) if current is not None else {}
        return ActivationPreview(
            receipt_id=receipt.receipt_id,
            run_id=run.run_id,
            run_revision=run.revision,
            owner_principal=run.owner_principal,
            receipt_digest=str(receipt.payload.get("payload_digest") or ""),
            scopes=tuple(self._scope_preview(run_scope, entry) for entry in entries),
            replay=self._replay_section(receipt),
            target_snapshot=json.loads(run.target_json),
            price_snapshot=json.loads(run.price_json),
            binding_digests={
                "scope": run.scope_digest,
                "corpus": run.corpus_digest,
                "target": run.target_digest,
                "price": run.price_digest,
                "policy": run.policy_digest,
                "learned": run.learned_digest,
                "project_authority": run.project_authority_digest,
                "thresholds": run.thresholds_digest,
            },
            binding_changes=binding_changes,
            authority_changed=any(binding_changes.values()),
            suspension_conditions=SUSPENSION_CONDITIONS,
            revocation_behavior=REVOCATION_BEHAVIOR,
        )

    def activate_scopes(self, request: ActivationRequest) -> ActivationResult:
        """Transactionally create one grant per confirmed exact scope.

        Every check runs before the transaction opens, and the transaction
        itself is a single ``BEGIN IMMEDIATE`` batch: any conflict rolls the
        whole activation back, so multi-scope activation is all-or-nothing.
        """

        receipt, run, entries = self._receipt_context(request)
        # 1. owner principal and expected receipt revision/digest.
        if request.principal != run.owner_principal:
            raise PermissionError("owner confirmation required")
        run_section = receipt.payload.get("run")
        bound_revision = 0
        if isinstance(run_section, Mapping):
            revision = run_section.get("revision")
            if isinstance(revision, int) and not isinstance(revision, bool):
                bound_revision = revision
        if bound_revision != request.expected_run_revision:
            raise ActivationConflict("receipt_revision_changed")
        bound_digest = str(receipt.payload.get("payload_digest") or "")
        if not hmac.compare_digest(bound_digest, request.expected_receipt_digest):
            raise ActivationConflict("receipt_digest_changed")
        # 2. receipt HMAC authentication (fail-closed).
        self._verify_receipt(receipt)
        # 3. completed run/receipt and qualified selected scopes.
        if run.status != "completed" or str(receipt.payload.get("status")) != "completed":
            raise ActivationConflict("qualification_run_not_completed")
        selected: list[tuple[str, str]] = []
        for digest, entry in zip(request.scope_digests, entries, strict=True):
            if entry.get("state") != "qualified" or entry.get("qualified") is not True:
                raise ActivationConflict("scope_not_qualified")
            target = entry.get("selected_target_id")
            if not isinstance(target, str) or not target.strip():
                raise ActivationConflict("scope_not_qualified")
            selected.append((digest, target))
        # 4. recompute every current binding digest.
        changes = request.bindings.digest_changes(run)
        for key in _BINDING_CHECK_ORDER:
            if changes[key]:
                raise ActivationConflict(_BINDING_CONFLICT_REASONS[key])
        # 5. global master permit; never a source of authority by itself.
        if not self._master_permit():
            raise ActivationConflict("global_master_permit_required")
        # 6-8. one transaction: base grants, activated transitions (the
        # activation events), and same-transaction supersession.
        drafts = tuple(
            self._grant_draft(run, receipt, request.principal, scope_digest, target)
            for scope_digest, target in selected
        )
        activations = self._ledger.activate_grants(drafts, reason=_ACTIVATION_REASON)
        return ActivationResult(
            grants=tuple(activation[0] for activation in activations),
            transitions=tuple(activation[1] for activation in activations),
            superseded=tuple(
                transition for activation in activations for transition in activation[2]
            ),
        )

    def revoke(
        self,
        grant_id: str,
        *,
        expected_revision: int | None = None,
        reason: str = _REVOCATION_REASON,
    ) -> ActivationTransition:
        """Append a terminal ``revoked`` transition (Task 16).

        The revocation is checked against the expected latest-transition
        revision when one is given: a stale expectation raises
        :class:`QualificationRevisionConflict` and appends nothing.  A
        revoked grant is terminal -- the schema v4 transition vocabulary
        bars every follow-up transition, so reactivation requires fresh
        qualification plus owner confirmation.
        """

        grant, latest = self._grant_state(grant_id)
        if latest is not None and latest.transition_type == "revoked":
            raise ValueError(f"activation grant {grant_id} is already revoked")
        self._check_expected_revision(grant_id, latest, expected_revision)
        return self._ledger.append_transition(
            grant_id,
            "revoked",
            reason,
            receipt_id=grant.qualification_receipt_id,
        )

    def activate_existing(
        self,
        grant_id: str,
        *,
        expected_revision: int | None = None,
        reason: str = _RESUME_REASON,
    ) -> ActivationTransition:
        """Resume a suspended grant through an append-only transition (Task 16).

        Only a grant whose latest transition is ``suspended`` can resume,
        and only when the expected latest-transition revision still matches.
        A revoked grant can never return to active: reactivation requires
        fresh qualification plus owner confirmation.  Resuming does not
        re-verify bindings here -- the route-time evaluator re-verifies
        every binding on the next decision and re-suspends on drift.
        """

        grant, latest = self._grant_state(grant_id)
        if latest is None:
            raise ValueError(f"activation grant {grant_id} has no transitions")
        if latest.transition_type == "revoked":
            raise ValueError(
                f"activation grant {grant_id} is revoked; reactivation "
                "requires fresh qualification plus owner confirmation"
            )
        if latest.transition_type in ("activated", "resumed"):
            raise ValueError(f"activation grant {grant_id} is already active")
        self._check_expected_revision(grant_id, latest, expected_revision)
        return self._ledger.append_transition(
            grant_id,
            "resumed",
            reason,
            receipt_id=grant.qualification_receipt_id,
        )

    # -- internals ------------------------------------------------------------

    def _grant_state(
        self,
        grant_id: str,
    ) -> tuple[ActivationGrant, ActivationTransition | None]:
        _require_text(grant_id, "grant_id")
        grant = self._ledger.get_grant(grant_id)
        if grant is None:
            raise ValueError(f"unknown activation grant: {grant_id}")
        transitions = self._ledger.list_transitions(grant_id)
        return grant, transitions[-1] if transitions else None

    def _check_expected_revision(
        self,
        grant_id: str,
        latest: ActivationTransition | None,
        expected_revision: int | None,
    ) -> None:
        if expected_revision is None:
            return
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValueError("expected_revision must be a positive integer")
        current = 0 if latest is None else latest.sequence
        if current != expected_revision:
            raise QualificationRevisionConflict(
                "activation_grant_transition", grant_id, current
            )

    def _receipt_context(
        self,
        request: ActivationRequest,
    ) -> tuple[QualificationReceipt, QualificationRun, list[Mapping[str, Any]]]:
        try:
            receipt, run, entries = self._receipt_scopes(
                request.receipt_id,
                request.scope_digests,
            )
        except ValueError as exc:
            if str(exc).startswith("unknown receipt scope"):
                raise ActivationConflict("scope_not_in_receipt") from exc
            raise
        return receipt, run, entries

    def _receipt_scopes(
        self,
        receipt_id: str,
        scope_digests: Sequence[str],
    ) -> tuple[QualificationReceipt, QualificationRun, list[Mapping[str, Any]]]:
        _require_text(receipt_id, "receipt_id")
        receipt = self._ledger.get_receipt(receipt_id)
        if receipt is None:
            raise ValueError(f"unknown qualification receipt: {receipt_id}")
        run = self._ledger.get_run(receipt.run_id)
        if run is None:
            raise ValueError(f"unknown qualification run: {receipt.run_id}")
        scopes = receipt.payload.get("scopes")
        by_digest: dict[str, Mapping[str, Any]] = {}
        if isinstance(scopes, Sequence) and not isinstance(scopes, str):
            for entry in scopes:
                if isinstance(entry, Mapping) and isinstance(entry.get("scope_digest"), str):
                    by_digest[entry["scope_digest"]] = entry
        entries: list[Mapping[str, Any]] = []
        for digest in scope_digests:
            entry = by_digest.get(digest)
            if entry is None:
                raise ValueError(f"unknown receipt scope: {digest}")
            entries.append(entry)
        return receipt, run, entries

    def _verify_receipt(self, receipt: QualificationReceipt) -> None:
        # Verification never mints key material: a missing or unsafe owner
        # key fails closed as an authentication failure.
        try:
            integrity = self._verification_integrity()
        except (OSError, ValueError) as exc:
            raise ActivationConflict("receipt_authentication_failed") from exc
        if not receipt_authenticates(receipt, integrity=integrity):
            raise ActivationConflict("receipt_authentication_failed")

    def _verification_integrity(self) -> ControlPlaneIntegrity:
        # Verification never mints key material: a missing or unsafe owner key
        # fails closed as an authentication failure.
        if self._integrity is None:
            self._integrity = load_verification_integrity(self._ledger, None)
        return self._integrity

    def _scope_preview(
        self,
        run_scope: Mapping[str, Any],
        entry: Mapping[str, Any],
    ) -> ActivationScopePreview:
        capability_key = str(run_scope.get("capability_key") or "")
        selected = entry.get("selected_target_id")
        evaluated = entry.get("evaluated_target_ids")
        alternatives = tuple(
            str(target)
            for target in (evaluated if isinstance(evaluated, Sequence) else ())
            if isinstance(target, str) and target != selected
        )
        return ActivationScopePreview(
            scope_digest=str(entry["scope_digest"]),
            project_id=str(run_scope.get("project_id") or ""),
            task_family=str(run_scope.get("task_family") or ""),
            risk=str(run_scope.get("risk") or ""),
            capabilities=tuple(part for part in capability_key.split("+") if part),
            static_target_id=str(entry.get("static_target_id") or ""),
            selected_target_id=str(selected) if isinstance(selected, str) else None,
            alternative_target_ids=alternatives,
            total_support=_as_int(entry.get("total_support")),
            selected_target_support=_as_int(entry.get("selected_target_support")),
            confidence=_as_float(entry.get("confidence")),
            static_utility=_as_optional_float(entry.get("static_utility")),
            learned_utility=_as_optional_float(entry.get("learned_utility")),
            utility_delta=_as_float(entry.get("utility_delta")),
            cost_coverage=_as_float(entry.get("cost_coverage")),
            estimated_savings_usd=_as_optional_float(entry.get("estimated_savings_usd")),
            guardrail_violations=_as_int(entry.get("guardrail_violations")),
            reasons=tuple(
                str(reason) for reason in entry.get("reasons") or () if isinstance(reason, str)
            ),
            qualified=entry.get("qualified") is True,
        )

    def _replay_section(self, receipt: QualificationReceipt) -> Mapping[str, Any] | None:
        replay = receipt.payload.get("replay")
        if isinstance(replay, Mapping):
            return replay
        return None

    def _grant_draft(
        self,
        run: QualificationRun,
        receipt: QualificationReceipt,
        principal: str,
        scope_digest: str,
        target_id: str,
    ) -> ActivationGrantDraft:
        # The grant identity binds the receipt, the exact scope, the selected
        # target, and the grant it supersedes, so re-activation of the same
        # exact scope derives a fresh immutable grant id while a raced retry
        # of the identical request deterministically conflicts instead of
        # double-writing.
        supersedes = ""
        active = self._active_exact_scope_grants(run, target_id)
        if active:
            supersedes = active[-1].grant_id
        grant_id = (
            "grant_"
            + canonical_digest(
                {
                    "receipt_id": receipt.receipt_id,
                    "scope_digest": scope_digest,
                    "supersedes": supersedes,
                    "target_id": target_id,
                }
            )[:24]
        )
        return ActivationGrantDraft(
            grant_id=grant_id,
            run_id=run.run_id,
            target_id=target_id,
            qualification_receipt_id=receipt.receipt_id,
            created_by=principal,
        )

    def _active_exact_scope_grants(
        self,
        run: QualificationRun,
        target_id: str,
    ) -> list[ActivationGrant]:
        candidates = self._ledger.list_grants(
            scope_digest=run.scope_digest,
            target_id=target_id,
        )
        active: list[ActivationGrant] = []
        for grant in candidates:
            transitions = self._ledger.list_transitions(grant.grant_id)
            if transitions and transitions[-1].transition_type != "revoked":
                active.append(grant)
        return active


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _as_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    return float(value)


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)
