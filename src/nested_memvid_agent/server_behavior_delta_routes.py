from __future__ import annotations

from typing import Any

from .behavior_delta_ledger import BehaviorDeltaLedger
from .behavior_delta_skill import render_skill_candidate_preview


def register_behavior_delta_routes(app: Any, *, http_exception: Any, ledger: BehaviorDeltaLedger) -> None:
    """Register read-only behavior-delta review routes.

    These endpoints intentionally expose review data only. Activation, rejection,
    rollback, and skill installation remain absent from this API slice.
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
            "review_actions": _read_only_review_actions(),
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
    def activate_behavior_delta(delta_id: str) -> None:
        del delta_id
        raise http_exception(status_code=405, detail="read_only_review_api")

    @app.post("/api/memory/deltas/{delta_id}/reject")  # type: ignore[untyped-decorator]
    def reject_behavior_delta(delta_id: str) -> None:
        del delta_id
        raise http_exception(status_code=405, detail="read_only_review_api")

    @app.post("/api/memory/deltas/{delta_id}/rollback")  # type: ignore[untyped-decorator]
    def rollback_behavior_delta(delta_id: str) -> None:
        del delta_id
        raise http_exception(status_code=405, detail="read_only_review_api")


def _read_only_review_actions() -> dict[str, object]:
    return {
        "can_activate": False,
        "can_reject": False,
        "can_rollback": False,
        "reason": "read_only_review_api",
    }


def _since_value(raw: str | None) -> str | None:
    if raw is None:
        return "30d"
    value = raw.strip()
    if value.lower() in {"all", "all-time", "all_time"}:
        return "all"
    return value or "30d"
