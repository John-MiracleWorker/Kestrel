from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaStatus,
    behavior_delta_from_metadata,
)
from .models import EvidenceRef, MemoryLayer
from .state_store import AgentStateStore, utc_now

BehaviorDeltaOutcomeKind = Literal[
    "useful",
    "ignored",
    "caused_failure",
    "corrected",
    "contradicted",
    "superseded",
    "rolled_back",
    "expired",
    "never_activated",
]
OUTCOME_KINDS: tuple[BehaviorDeltaOutcomeKind, ...] = (
    "useful",
    "ignored",
    "caused_failure",
    "corrected",
    "contradicted",
    "superseded",
    "rolled_back",
    "expired",
    "never_activated",
)
_TERMINAL_STATUSES = {
    BehaviorDeltaStatus.REJECTED,
    BehaviorDeltaStatus.ROLLED_BACK,
    BehaviorDeltaStatus.EXPIRED,
}


@dataclass(frozen=True)
class BehaviorDeltaActivation:
    id: str
    delta_id: str
    run_id: str | None
    task_id: str | None
    objective: str | None
    activated_at: str
    activation_reason: str
    compiled_section: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "delta_id": self.delta_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "activated_at": self.activated_at,
            "activation_reason": self.activation_reason,
            "compiled_section": self.compiled_section,
        }


@dataclass(frozen=True)
class BehaviorDeltaOutcome:
    id: str
    delta_id: str
    run_id: str | None
    outcome: BehaviorDeltaOutcomeKind
    recorded_at: str
    evidence_ref: EvidenceRef | None = None
    notes: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "delta_id": self.delta_id,
            "run_id": self.run_id,
            "outcome": self.outcome,
            "evidence_ref": None if self.evidence_ref is None else _evidence_to_payload(self.evidence_ref),
            "notes": self.notes,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class BehaviorDeltaSummary:
    total_deltas: int
    active_deltas: int
    activated_deltas: int
    never_activated: int
    outcome_counts: dict[str, int]

    @property
    def useful_rate(self) -> float:
        return _rate(self.outcome_counts.get("useful", 0), self.total_deltas)

    @property
    def failure_rate(self) -> float:
        return _rate(
            self.outcome_counts.get("caused_failure", 0) + self.outcome_counts.get("contradicted", 0),
            self.total_deltas,
        )

    @property
    def rollback_rate(self) -> float:
        return _rate(self.outcome_counts.get("rolled_back", 0), self.total_deltas)

    @property
    def never_activated_rate(self) -> float:
        return _rate(self.never_activated, self.total_deltas)

    def to_payload(self) -> dict[str, Any]:
        return {
            "total_deltas": self.total_deltas,
            "active_deltas": self.active_deltas,
            "activated_deltas": self.activated_deltas,
            "never_activated": self.never_activated,
            "useful_rate": self.useful_rate,
            "failure_rate": self.failure_rate,
            "rollback_rate": self.rollback_rate,
            "never_activated_rate": self.never_activated_rate,
            "outcomes": dict(self.outcome_counts),
        }


@dataclass(frozen=True)
class BehaviorDeltaReportRow:
    delta_id: str
    title: str
    kind: str
    target_layer: str
    risk: str
    status: str
    activation_count: int
    outcome_counts: dict[str, int]
    never_activated: bool
    last_activated_at: str | None = None
    last_outcome_at: str | None = None

    @property
    def useful_rate(self) -> float:
        return _rate(self.outcome_counts.get("useful", 0), max(1, sum(self.outcome_counts.values())))

    @property
    def failure_rate(self) -> float:
        return _rate(
            self.outcome_counts.get("caused_failure", 0) + self.outcome_counts.get("contradicted", 0),
            max(1, sum(self.outcome_counts.values())),
        )

    @property
    def rollback_rate(self) -> float:
        return _rate(self.outcome_counts.get("rolled_back", 0), max(1, sum(self.outcome_counts.values())))

    def to_payload(self) -> dict[str, Any]:
        return {
            "delta_id": self.delta_id,
            "title": self.title,
            "kind": self.kind,
            "target_layer": self.target_layer,
            "risk": self.risk,
            "status": self.status,
            "activation_count": self.activation_count,
            "outcome_counts": dict(self.outcome_counts),
            "useful_rate": self.useful_rate,
            "failure_rate": self.failure_rate,
            "rollback_rate": self.rollback_rate,
            "never_activated": self.never_activated,
            "last_activated_at": self.last_activated_at,
            "last_outcome_at": self.last_outcome_at,
        }


@dataclass(frozen=True)
class BehaviorDeltaReport:
    summary: BehaviorDeltaSummary
    rows: tuple[BehaviorDeltaReportRow, ...]
    recommendations: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "summary": self.summary.to_payload(),
            "deltas": [row.to_payload() for row in self.rows],
            "recommendations": list(self.recommendations),
        }


class BehaviorDeltaLedger:
    """SQLite-backed control-plane ledger for behavior-delta proposals and outcomes."""

    def __init__(self, state: AgentStateStore) -> None:
        self.state = state

    def record_delta(self, delta: BehaviorDelta) -> None:
        with self.state._connect() as conn:
            conn.execute(
                """
                INSERT INTO behavior_delta_ledger (
                    delta_id, kind, target_layer, risk, status, title, trigger_json,
                    behavior_change, validation_plan_json, rollback_plan_json,
                    evidence_json, confidence, importance, created_from_run_id,
                    created_at, updated_at, expires_at, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(delta_id) DO UPDATE SET
                    kind = excluded.kind,
                    target_layer = excluded.target_layer,
                    risk = excluded.risk,
                    status = excluded.status,
                    title = excluded.title,
                    trigger_json = excluded.trigger_json,
                    behavior_change = excluded.behavior_change,
                    validation_plan_json = excluded.validation_plan_json,
                    rollback_plan_json = excluded.rollback_plan_json,
                    evidence_json = excluded.evidence_json,
                    confidence = excluded.confidence,
                    importance = excluded.importance,
                    created_from_run_id = excluded.created_from_run_id,
                    created_at = excluded.created_at,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at,
                    metadata_json = excluded.metadata_json
                """,
                _delta_values(delta),
            )

    def get_delta(self, delta_id: str) -> BehaviorDelta | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM behavior_delta_ledger WHERE delta_id = ?",
                (delta_id,),
            ).fetchone()
        return None if row is None else _delta_from_row(row)

    def list_deltas(
        self,
        *,
        status: BehaviorDeltaStatus | None = None,
        kind: BehaviorDeltaKind | None = None,
        target_layer: MemoryLayer | None = None,
    ) -> list[BehaviorDelta]:
        clauses: list[str] = []
        params: list[object] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if target_layer is not None:
            clauses.append("target_layer = ?")
            params.append(target_layer.value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.state._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM behavior_delta_ledger {where} ORDER BY created_at ASC, delta_id ASC",
                params,
            ).fetchall()
        return [_delta_from_row(row) for row in rows]

    def update_delta_status(
        self,
        delta_id: str,
        status: BehaviorDeltaStatus,
        *,
        reason: str,
    ) -> BehaviorDelta:
        current = self.get_delta(delta_id)
        if current is None:
            raise KeyError(f"Unknown behavior delta: {delta_id}")
        if current.status in _TERMINAL_STATUSES and status == BehaviorDeltaStatus.ACTIVE:
            raise ValueError("Cannot activate a terminal behavior delta without an explicit override path")
        metadata = {
            **current.metadata,
            "previous_status": current.status.value,
            "status_reason": reason,
            "status_updated_at": utc_now(),
        }
        updated = BehaviorDelta(
            id=current.id,
            title=current.title,
            kind=current.kind,
            target_layer=current.target_layer,
            risk=current.risk,
            status=status,
            trigger=current.trigger,
            behavior_change=current.behavior_change,
            evidence_refs=current.evidence_refs,
            validation_plan=current.validation_plan,
            rollback_plan=current.rollback_plan,
            activation_stats=current.activation_stats,
            confidence=current.confidence,
            importance=current.importance,
            created_from_run_id=current.created_from_run_id,
            created_at=current.created_at,
            updated_at=metadata["status_updated_at"],
            expires_at=current.expires_at,
            metadata=metadata,
        )
        self.record_delta(updated)
        return updated

    def record_activation(self, activation: BehaviorDeltaActivation) -> None:
        with self.state._connect() as conn:
            conn.execute(
                """
                INSERT INTO behavior_delta_activations (
                    id, delta_id, run_id, task_id, objective, activated_at,
                    activation_reason, compiled_section
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    activation.id,
                    activation.delta_id,
                    activation.run_id,
                    activation.task_id,
                    activation.objective,
                    activation.activated_at,
                    activation.activation_reason,
                    activation.compiled_section,
                ),
            )

    def list_activations(self, delta_id: str) -> list[BehaviorDeltaActivation]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM behavior_delta_activations
                WHERE delta_id = ?
                ORDER BY activated_at ASC, id ASC
                """,
                (delta_id,),
            ).fetchall()
        return [_activation_from_row(row) for row in rows]

    def record_outcome(self, outcome: BehaviorDeltaOutcome) -> None:
        if outcome.outcome not in OUTCOME_KINDS:
            raise ValueError(f"Unknown behavior delta outcome: {outcome.outcome}")
        with self.state._connect() as conn:
            conn.execute(
                """
                INSERT INTO behavior_delta_outcomes (
                    id, delta_id, run_id, outcome, evidence_ref_json, notes, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.id,
                    outcome.delta_id,
                    outcome.run_id,
                    outcome.outcome,
                    json.dumps(
                        None if outcome.evidence_ref is None else _evidence_to_payload(outcome.evidence_ref),
                        sort_keys=True,
                    ),
                    outcome.notes,
                    outcome.recorded_at,
                ),
            )

    def list_outcomes(self, delta_id: str) -> list[BehaviorDeltaOutcome]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM behavior_delta_outcomes
                WHERE delta_id = ?
                ORDER BY recorded_at ASC, id ASC
                """,
                (delta_id,),
            ).fetchall()
        return [_outcome_from_row(row) for row in rows]

    def summarize_deltas(self) -> BehaviorDeltaSummary:
        deltas = self.list_deltas()
        if not deltas:
            return BehaviorDeltaSummary(
                total_deltas=0,
                active_deltas=0,
                activated_deltas=0,
                never_activated=0,
                outcome_counts={kind: 0 for kind in OUTCOME_KINDS},
            )
        delta_ids = [delta.id for delta in deltas]
        placeholders = ", ".join("?" for _ in delta_ids)
        with self.state._connect() as conn:
            activation_rows = conn.execute(
                f"""
                SELECT delta_id, COUNT(*) AS count
                FROM behavior_delta_activations
                WHERE delta_id IN ({placeholders})
                GROUP BY delta_id
                """,
                delta_ids,
            ).fetchall()
            outcome_rows = conn.execute(
                f"""
                SELECT outcome, COUNT(*) AS count
                FROM behavior_delta_outcomes
                WHERE delta_id IN ({placeholders})
                GROUP BY outcome
                """,
                delta_ids,
            ).fetchall()
        activated_delta_ids = {str(row["delta_id"]) for row in activation_rows if int(row["count"]) > 0}
        outcome_counts = Counter({kind: 0 for kind in OUTCOME_KINDS})
        for row in outcome_rows:
            outcome_counts[str(row["outcome"])] += int(row["count"])
        return BehaviorDeltaSummary(
            total_deltas=len(deltas),
            active_deltas=sum(1 for delta in deltas if delta.status == BehaviorDeltaStatus.ACTIVE),
            activated_deltas=len(activated_delta_ids),
            never_activated=len(deltas) - len(activated_delta_ids),
            outcome_counts=dict(outcome_counts),
        )
    def report_deltas(self, *, since: datetime | str | None = None) -> BehaviorDeltaReport:
        cutoff = _coerce_datetime(since)
        rows: list[BehaviorDeltaReportRow] = []
        for delta in self.list_deltas():
            activations = [item for item in self.list_activations(delta.id) if _is_at_or_after(item.activated_at, cutoff)]
            outcomes = [item for item in self.list_outcomes(delta.id) if _is_at_or_after(item.recorded_at, cutoff)]
            in_scope = _is_at_or_after(delta.created_at, cutoff) or bool(activations) or bool(outcomes)
            if not in_scope:
                continue
            counts = Counter({kind: 0 for kind in OUTCOME_KINDS})
            for outcome in outcomes:
                counts[outcome.outcome] += 1
            rows.append(
                BehaviorDeltaReportRow(
                    delta_id=delta.id,
                    title=delta.title,
                    kind=delta.kind.value,
                    target_layer=delta.target_layer.value,
                    risk=delta.risk.value,
                    status=delta.status.value,
                    activation_count=len(activations),
                    outcome_counts=dict(counts),
                    never_activated=len(activations) == 0,
                    last_activated_at=activations[-1].activated_at if activations else None,
                    last_outcome_at=outcomes[-1].recorded_at if outcomes else None,
                )
            )
        summary = _summary_from_report_rows(rows)
        return BehaviorDeltaReport(
            summary=summary,
            rows=tuple(rows),
            recommendations=tuple(_behavior_delta_recommendations(rows)),
        )


def _delta_values(delta: BehaviorDelta) -> tuple[Any, ...]:
    payload = delta.to_metadata()
    metadata = {key: value for key, value in delta.metadata.items()}
    metadata["activation_stats"] = payload["activation_stats"]
    return (
        delta.id,
        delta.kind.value,
        delta.target_layer.value,
        delta.risk.value,
        delta.status.value,
        delta.title,
        json.dumps(payload["trigger"], sort_keys=True),
        delta.behavior_change,
        json.dumps(payload["validation_plan"], sort_keys=True),
        json.dumps(payload["rollback_plan"], sort_keys=True),
        json.dumps(payload["evidence"], sort_keys=True),
        delta.confidence,
        delta.importance,
        delta.created_from_run_id,
        delta.created_at,
        delta.updated_at,
        delta.expires_at,
        json.dumps(metadata, sort_keys=True),
    )


def _delta_from_row(row: Any) -> BehaviorDelta:
    metadata = json.loads(str(row["metadata_json"]))
    payload = {
        "id": str(row["delta_id"]),
        "kind": str(row["kind"]),
        "target_layer": str(row["target_layer"]),
        "risk": str(row["risk"]),
        "status": str(row["status"]),
        "title": str(row["title"]),
        "trigger": json.loads(str(row["trigger_json"])),
        "behavior_change": str(row["behavior_change"]),
        "validation_plan": json.loads(str(row["validation_plan_json"])),
        "rollback_plan": json.loads(str(row["rollback_plan_json"])),
        "activation_stats": metadata.pop("activation_stats", {}),
        "confidence": float(row["confidence"]),
        "importance": float(row["importance"]),
        "created_from_run_id": row["created_from_run_id"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "expires_at": row["expires_at"],
        "evidence": json.loads(str(row["evidence_json"])),
        **metadata,
    }
    return behavior_delta_from_metadata(payload)


def _activation_from_row(row: Any) -> BehaviorDeltaActivation:
    return BehaviorDeltaActivation(
        id=str(row["id"]),
        delta_id=str(row["delta_id"]),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
        task_id=None if row["task_id"] is None else str(row["task_id"]),
        objective=None if row["objective"] is None else str(row["objective"]),
        activated_at=str(row["activated_at"]),
        activation_reason=str(row["activation_reason"]),
        compiled_section=str(row["compiled_section"]),
    )


def _outcome_from_row(row: Any) -> BehaviorDeltaOutcome:
    evidence_payload = json.loads(str(row["evidence_ref_json"]))
    return BehaviorDeltaOutcome(
        id=str(row["id"]),
        delta_id=str(row["delta_id"]),
        run_id=None if row["run_id"] is None else str(row["run_id"]),
        outcome=str(row["outcome"]),  # type: ignore[arg-type]
        evidence_ref=None if evidence_payload is None else _evidence_from_payload(evidence_payload),
        notes=str(row["notes"]),
        recorded_at=str(row["recorded_at"]),
    )


def _evidence_to_payload(ref: EvidenceRef) -> dict[str, Any]:
    return {"source": ref.source, "locator": ref.locator, "quote": ref.quote}


def _evidence_from_payload(payload: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        source=str(payload["source"]),
        locator=str(payload["locator"]),
        quote=None if payload.get("quote") is None else str(payload["quote"]),
    )


def _summary_from_report_rows(rows: list[BehaviorDeltaReportRow]) -> BehaviorDeltaSummary:
    if not rows:
        return BehaviorDeltaSummary(
            total_deltas=0,
            active_deltas=0,
            activated_deltas=0,
            never_activated=0,
            outcome_counts={kind: 0 for kind in OUTCOME_KINDS},
        )
    counts = Counter({kind: 0 for kind in OUTCOME_KINDS})
    for row in rows:
        counts.update(row.outcome_counts)
    return BehaviorDeltaSummary(
        total_deltas=len(rows),
        active_deltas=sum(1 for row in rows if row.status == BehaviorDeltaStatus.ACTIVE.value),
        activated_deltas=sum(1 for row in rows if row.activation_count > 0),
        never_activated=sum(1 for row in rows if row.never_activated),
        outcome_counts=dict(counts),
    )


def _behavior_delta_recommendations(rows: list[BehaviorDeltaReportRow]) -> list[str]:
    recommendations: list[str] = []
    for row in rows:
        failures = row.outcome_counts.get("caused_failure", 0) + row.outcome_counts.get("contradicted", 0)
        useful = row.outcome_counts.get("useful", 0)
        if failures > useful:
            recommendations.append(
                f"Review behavior delta {row.delta_id}: failure outcomes exceed useful outcomes; consider validation, rollback, or keeping it staged."
            )
        elif row.status == BehaviorDeltaStatus.ACTIVE.value and row.never_activated:
            recommendations.append(
                f"Review behavior delta {row.delta_id}: active but never activated in this reporting window."
            )
    return recommendations


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = value.strip()
        if not raw or raw.lower() in {"all", "all-time", "all_time"}:
            return None
        parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _is_at_or_after(raw: str | None, cutoff: datetime | None) -> bool:
    if cutoff is None:
        return True
    if raw is None:
        return False
    return _coerce_datetime(raw) >= cutoff  # type: ignore[operator]


def _rate(count: int, total: int) -> float:
    if total == 0:
        return 0.0
    return round(count / total, 4)
