from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .models import MemoryLayer
from .state_store import AgentStateStore, utc_now

PromotionOutcomeKind = Literal["useful", "corrected", "contradicted", "tombstoned", "superseded", "never_retrieved"]
OUTCOME_KINDS: tuple[PromotionOutcomeKind, ...] = (
    "useful",
    "corrected",
    "contradicted",
    "tombstoned",
    "superseded",
    "never_retrieved",
)


@dataclass(frozen=True)
class PromotionEntry:
    promotion_id: str
    record_id: str
    source_layer: MemoryLayer
    target_layer: MemoryLayer
    decision_reason: str
    validation_score: float
    repeat_count: int
    explicit_instruction: bool
    optimizer_trace: dict[str, Any]
    promoted_at: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "promotion_id": self.promotion_id,
            "record_id": self.record_id,
            "source_layer": self.source_layer.value,
            "target_layer": self.target_layer.value,
            "decision_reason": self.decision_reason,
            "validation_score": self.validation_score,
            "repeat_count": self.repeat_count,
            "explicit_instruction": self.explicit_instruction,
            "optimizer_trace": self.optimizer_trace,
            "promoted_at": self.promoted_at,
        }


@dataclass(frozen=True)
class PromotionOutcome:
    promotion_id: str
    outcome: PromotionOutcomeKind
    recorded_at: str
    evidence_record_id: str | None = None
    notes: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "promotion_id": self.promotion_id,
            "outcome": self.outcome,
            "recorded_at": self.recorded_at,
            "evidence_record_id": self.evidence_record_id,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class LedgerRow:
    source_layer: MemoryLayer
    target_layer: MemoryLayer
    promoted: int
    outcome_counts: dict[str, int]
    average_time_to_outcome_hours: float | None

    @property
    def label(self) -> str:
        return f"{self.source_layer.value}->{self.target_layer.value}"

    @property
    def false_positive_rate(self) -> float:
        if self.promoted == 0:
            return 0.0
        return (self.outcome_counts.get("corrected", 0) + self.outcome_counts.get("contradicted", 0)) / self.promoted

    @property
    def never_retrieved_rate(self) -> float:
        if self.promoted == 0:
            return 0.0
        return self.outcome_counts.get("never_retrieved", 0) / self.promoted

    @property
    def useful_rate(self) -> float:
        if self.promoted == 0:
            return 0.0
        return self.outcome_counts.get("useful", 0) / self.promoted

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_layer": self.source_layer.value,
            "target_layer": self.target_layer.value,
            "gate": self.label,
            "promoted": self.promoted,
            "outcomes": dict(self.outcome_counts),
            "false_positive_rate": round(self.false_positive_rate, 4),
            "never_retrieved_rate": round(self.never_retrieved_rate, 4),
            "useful_rate": round(self.useful_rate, 4),
            "average_time_to_outcome_hours": self.average_time_to_outcome_hours,
        }


@dataclass(frozen=True)
class LedgerSummary:
    since: str | None
    target_layer: MemoryLayer | None
    outcome_filter: str | None
    rows: tuple[LedgerRow, ...]
    recommendations: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "since": self.since,
            "target_layer": None if self.target_layer is None else self.target_layer.value,
            "outcome_filter": self.outcome_filter,
            "rows": [row.to_payload() for row in self.rows],
            "recommendations": list(self.recommendations),
        }


class PromotionLedger:
    """SQLite-backed promotion ledger stored in the existing AgentStateStore DB."""

    def __init__(self, state: AgentStateStore) -> None:
        self.state = state

    def record_promotion(self, entry: PromotionEntry) -> None:
        with self.state._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO promotion_ledger (
                    promotion_id, record_id, source_layer, target_layer, decision_reason,
                    validation_score, repeat_count, explicit_instruction, optimizer_trace_json, promoted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.promotion_id,
                    entry.record_id,
                    entry.source_layer.value,
                    entry.target_layer.value,
                    entry.decision_reason,
                    entry.validation_score,
                    entry.repeat_count,
                    1 if entry.explicit_instruction else 0,
                    json.dumps(entry.optimizer_trace, sort_keys=True),
                    entry.promoted_at,
                ),
            )

    def record_outcome(self, outcome: PromotionOutcome) -> None:
        if outcome.outcome not in OUTCOME_KINDS:
            raise ValueError(f"Unknown promotion outcome: {outcome.outcome}")
        with self.state._connect() as conn:
            conn.execute(
                """
                INSERT INTO promotion_outcomes (
                    promotion_id, outcome, evidence_record_id, notes, recorded_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    outcome.promotion_id,
                    outcome.outcome,
                    outcome.evidence_record_id,
                    outcome.notes,
                    outcome.recorded_at,
                ),
            )

    def get_promotion(self, promotion_id: str) -> PromotionEntry | None:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM promotion_ledger WHERE promotion_id = ?",
                (promotion_id,),
            ).fetchone()
        return None if row is None else _entry_from_row(row)

    def summarize(
        self,
        since: datetime | None = None,
        target_layer: MemoryLayer | None = None,
        outcome: str | None = None,
    ) -> LedgerSummary:
        entries = self._entries(since=since, target_layer=target_layer, outcome=outcome)
        outcomes = self._outcomes_for_entries([entry.promotion_id for entry in entries], outcome=outcome)
        grouped: dict[tuple[MemoryLayer, MemoryLayer], list[PromotionEntry]] = defaultdict(list)
        for entry in entries:
            grouped[(entry.source_layer, entry.target_layer)].append(entry)

        rows: list[LedgerRow] = []
        for (source_layer, row_target_layer), group_entries in sorted(
            grouped.items(),
            key=lambda item: (item[0][0].value, item[0][1].value),
        ):
            outcome_counts: dict[str, int] = {kind: 0 for kind in OUTCOME_KINDS}
            hours_to_outcome: list[float] = []
            for entry in group_entries:
                entry_outcomes = outcomes.get(entry.promotion_id, [])
                for item in entry_outcomes:
                    outcome_counts[item.outcome] += 1
                first = min(entry_outcomes, key=lambda item: item.recorded_at, default=None)
                if first is not None:
                    delta = _parse_time(first.recorded_at) - _parse_time(entry.promoted_at)
                    hours_to_outcome.append(max(delta.total_seconds() / 3600, 0.0))
            rows.append(
                LedgerRow(
                    source_layer=source_layer,
                    target_layer=row_target_layer,
                    promoted=len(group_entries),
                    outcome_counts=outcome_counts,
                    average_time_to_outcome_hours=(
                        round(sum(hours_to_outcome) / len(hours_to_outcome), 2) if hours_to_outcome else None
                    ),
                )
            )

        return LedgerSummary(
            since=since.isoformat() if since else None,
            target_layer=target_layer,
            outcome_filter=outcome,
            rows=tuple(rows),
            recommendations=tuple(_recommendations(rows)),
        )

    def _entries(
        self,
        *,
        since: datetime | None,
        target_layer: MemoryLayer | None,
        outcome: str | None,
    ) -> list[PromotionEntry]:
        clauses: list[str] = []
        params: list[object] = []
        if since is not None:
            clauses.append("promoted_at >= ?")
            params.append(since.isoformat())
        if target_layer is not None:
            clauses.append("target_layer = ?")
            params.append(target_layer.value)
        if outcome is not None:
            clauses.append(
                """
                promotion_id IN (
                    SELECT promotion_id FROM promotion_outcomes WHERE outcome = ?
                )
                """
            )
            params.append(outcome)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.state._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM promotion_ledger {where} ORDER BY promoted_at ASC, promotion_id ASC",
                params,
            ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def _outcomes_for_entries(
        self,
        promotion_ids: list[str],
        *,
        outcome: str | None,
    ) -> dict[str, list[PromotionOutcome]]:
        if not promotion_ids:
            return {}
        placeholders = ", ".join("?" for _ in promotion_ids)
        params: list[object] = list(promotion_ids)
        outcome_clause = ""
        if outcome is not None:
            outcome_clause = " AND outcome = ?"
            params.append(outcome)
        with self.state._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM promotion_outcomes
                WHERE promotion_id IN ({placeholders}){outcome_clause}
                ORDER BY recorded_at ASC, id ASC
                """,
                params,
            ).fetchall()
        grouped: dict[str, list[PromotionOutcome]] = defaultdict(list)
        for row in rows:
            item = _outcome_from_row(row)
            grouped[item.promotion_id].append(item)
        return grouped


def make_outcome(
    promotion_id: str,
    outcome: PromotionOutcomeKind,
    *,
    evidence_record_id: str | None = None,
    notes: str = "",
) -> PromotionOutcome:
    return PromotionOutcome(
        promotion_id=promotion_id,
        outcome=outcome,
        evidence_record_id=evidence_record_id,
        notes=notes,
        recorded_at=utc_now(),
    )


def _entry_from_row(row: Any) -> PromotionEntry:
    return PromotionEntry(
        promotion_id=str(row["promotion_id"]),
        record_id=str(row["record_id"]),
        source_layer=MemoryLayer(str(row["source_layer"])),
        target_layer=MemoryLayer(str(row["target_layer"])),
        decision_reason=str(row["decision_reason"]),
        validation_score=float(row["validation_score"]),
        repeat_count=int(row["repeat_count"]),
        explicit_instruction=bool(row["explicit_instruction"]),
        optimizer_trace=json.loads(str(row["optimizer_trace_json"])),
        promoted_at=str(row["promoted_at"]),
    )


def _outcome_from_row(row: Any) -> PromotionOutcome:
    return PromotionOutcome(
        promotion_id=str(row["promotion_id"]),
        outcome=str(row["outcome"]),  # type: ignore[arg-type]
        evidence_record_id=None if row["evidence_record_id"] is None else str(row["evidence_record_id"]),
        notes=str(row["notes"]),
        recorded_at=str(row["recorded_at"]),
    )


def _recommendations(rows: list[LedgerRow]) -> list[str]:
    recommendations: list[str] = []
    for row in rows:
        if row.promoted == 0:
            continue
        gate = row.label
        if row.false_positive_rate > 0.05:
            recommendations.append(
                f"{gate} false-positive rate is above 5%; consider raising promotion_threshold by 0.03 if it persists."
            )
        if row.never_retrieved_rate > 0.40:
            recommendations.append(
                f"{gate} never-retrieved rate is above 40%; the gate may be admitting too eagerly."
            )
        if row.promoted < 10 and row.useful_rate > 0.90:
            recommendations.append(
                f"{gate} useful rate is above 90% on low volume; the gate may be too strict."
            )
    return recommendations


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
