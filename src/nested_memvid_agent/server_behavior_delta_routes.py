from __future__ import annotations

from typing import Any
from uuid import uuid4

from starlette.responses import JSONResponse

from .behavior_delta import BehaviorDeltaStatus
from .behavior_delta_ledger import BehaviorDeltaLedger, BehaviorDeltaOutcome
from .behavior_delta_skill import render_skill_candidate_preview
from .mutation_gate import MutationDecision, MutationGate, MutationGateEvidence
from .state_store import utc_now


def register_behavior_delta_routes(app: Any, *, http_exception: Any, ledger: BehaviorDeltaLedger) -> None:
    """Register behavior-delta review routes.

    Review actions are intentionally narrow operator actions. Mutating actions
    require exact-call approval in the request body and activation is still
    adjudicated by MutationGate before a status can become ACTIVE.
    """

    @app.get("/api/memory/deltas")  # type: ignore[untyped-decorator]
    def list_behavior_deltas(since: str = "30d") -> dict[str, object]:
        report = ledger.report_deltas(since=_since_value(since))
        return report.to_payload()

    @app.get("/api/memory/deltas/{delta_id}")  # type: ignore[untyped-decorator]
    def show_behavior_delta(delta_id: str) -> dict[str, object]:
        delta = ledger.get_delta(delta_id)
        if delta is None:
            raise http_exception(status_code=404, detail="behavior_delta_not_found")
        return {
            "delta": delta.to_metadata(),
            "activations": [item.to_payload() for item in ledger.list_activations(delta_id)],
            "outcomes": [item.to_payload() for item in ledger.list_outcomes(delta_id)],
            "review_actions": _review_actions(delta.status),
        }

    @app.get("/api/memory/deltas/{delta_id}/skill-preview")  # type: ignore[untyped-decorator]
    def behavior_delta_skill_preview(delta_id: str) -> dict[str, object]:
        delta = ledger.get_delta(delta_id)
        if delta is None:
            raise http_exception(status_code=404, detail="behavior_delta_not_found")
        try:
            return render_skill_candidate_preview(delta).to_payload()
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.post("/api/memory/deltas/{delta_id}/activate")  # type: ignore[untyped-decorator]
    def activate_behavior_delta(delta_id: str, request: dict[str, Any] | None = None) -> Any:
        payload = request or {}
        _require_exact_call(payload, http_exception)
        delta = _require_delta(ledger, http_exception, delta_id)
        decision = MutationGate().evaluate(delta, _gate_evidence(payload))
        decision_payload = _decision_payload(decision)
        if decision.status != BehaviorDeltaStatus.ACTIVE:
            return JSONResponse(
                {"delta": delta.to_metadata(), "decision": decision_payload},
                status_code=409,
            )
        updated = ledger.update_delta_status(
            delta_id,
            BehaviorDeltaStatus.ACTIVE,
            reason=str(payload.get("reason") or decision.reason),
        )
        return {"delta": updated.to_metadata(), "decision": decision_payload}

    @app.post("/api/memory/deltas/{delta_id}/reject")  # type: ignore[untyped-decorator]
    def reject_behavior_delta(delta_id: str, request: dict[str, Any] | None = None) -> dict[str, object]:
        payload = request or {}
        _require_exact_call(payload, http_exception)
        _require_delta(ledger, http_exception, delta_id)
        reason = str(payload.get("reason") or "operator rejected behavior delta")
        updated = ledger.update_delta_status(delta_id, BehaviorDeltaStatus.REJECTED, reason=reason)
        return {"delta": updated.to_metadata(), "decision": {"status": "rejected", "reason": reason}}

    @app.post("/api/memory/deltas/{delta_id}/rollback")  # type: ignore[untyped-decorator]
    def rollback_behavior_delta(delta_id: str, request: dict[str, Any] | None = None) -> dict[str, object]:
        payload = request or {}
        _require_exact_call(payload, http_exception)
        delta = _require_delta(ledger, http_exception, delta_id)
        reason = str(payload.get("reason") or "operator rolled back behavior delta")
        if not delta.rollback_plan.can_disable:
            raise http_exception(status_code=409, detail="rollback_not_disableable")
        updated = ledger.update_delta_status(delta_id, BehaviorDeltaStatus.ROLLED_BACK, reason=reason)
        ledger.record_outcome(
            BehaviorDeltaOutcome(
                id=f"outcome_{uuid4().hex}",
                delta_id=delta_id,
                run_id=None if payload.get("run_id") is None else str(payload.get("run_id")),
                outcome="rolled_back",
                notes=reason,
                recorded_at=utc_now(),
            )
        )
        return {"delta": updated.to_metadata(), "decision": {"status": "rolled_back", "reason": reason}}


def _review_actions(status: BehaviorDeltaStatus) -> dict[str, object]:
    terminal = status in {BehaviorDeltaStatus.REJECTED, BehaviorDeltaStatus.ROLLED_BACK, BehaviorDeltaStatus.EXPIRED}
    return {
        "can_activate": not terminal and status != BehaviorDeltaStatus.ACTIVE,
        "can_reject": not terminal,
        "can_rollback": status == BehaviorDeltaStatus.ACTIVE,
        "requires_exact_call_approval": True,
        "authority": "mutation_gate",
    }


def _require_delta(ledger: BehaviorDeltaLedger, http_exception: Any, delta_id: str) -> Any:
    delta = ledger.get_delta(delta_id)
    if delta is None:
        raise http_exception(status_code=404, detail="behavior_delta_not_found")
    return delta


def _require_exact_call(payload: dict[str, Any], http_exception: Any) -> None:
    if payload.get("exact_call_approved") is not True:
        raise http_exception(status_code=403, detail="exact_call_approval_required")


def _gate_evidence(payload: dict[str, Any]) -> MutationGateEvidence:
    return MutationGateEvidence(
        validation_score=float(payload.get("validation_score", 0.0)),
        repeat_count=int(payload.get("repeat_count", 1)),
        explicit_instruction=bool(payload.get("explicit_instruction", False)),
        reviewed_rule=bool(payload.get("reviewed_rule", False)),
        replay_passed=bool(payload.get("replay_passed", False)),
        policy_delta_activation_enabled=bool(payload.get("policy_delta_activation_enabled", False)),
        critical_delta_activation_enabled=bool(payload.get("critical_delta_activation_enabled", False)),
        exact_call_approved=bool(payload.get("exact_call_approved", False)),
        human_approved=bool(payload.get("human_approved", False)),
    )


def _decision_payload(decision: MutationDecision) -> dict[str, object]:
    return {
        "accepted": decision.accepted,
        "status": decision.status.value,
        "reason": decision.reason,
        "requires_replay": decision.requires_replay,
        "requires_human_approval": decision.requires_human_approval,
        "requires_exact_call_approval": decision.requires_exact_call_approval,
        "blocked_by": list(decision.blocked_by),
    }


def _since_value(raw: str | None) -> str | None:
    if raw is None:
        return "30d"
    value = raw.strip()
    if value.lower() in {"all", "all-time", "all_time"}:
        return "all"
    return value or "30d"
