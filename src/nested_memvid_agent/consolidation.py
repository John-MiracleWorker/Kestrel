from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord


@dataclass(frozen=True)
class ConsolidationCandidate:
    source: MemoryRecord
    target_layer: MemoryLayer
    reason: str
    promoted_confidence: float


class Consolidator:
    """Promotion rules between nested memory layers.

    This is deliberately conservative. Permanent memory is earned, not shoveled.
    """

    def propose(self, record: MemoryRecord, validation_score: float, repeat_count: int = 1) -> ConsolidationCandidate | None:
        if validation_score < 0.65:
            return None

        if record.layer == MemoryLayer.WORKING and validation_score >= 0.65:
            return ConsolidationCandidate(
                source=record,
                target_layer=MemoryLayer.EPISODIC,
                reason="Working memory survived validation and became a meaningful event.",
                promoted_confidence=min(max(record.confidence, validation_score), 0.9),
            )

        if record.layer == MemoryLayer.EPISODIC and validation_score >= 0.78:
            target = MemoryLayer.SEMANTIC
            reason = "Episodic memory produced a stable fact."
            if record.kind in {MemoryKind.FAILURE, MemoryKind.PROCEDURE} and repeat_count >= 2:
                target = MemoryLayer.PROCEDURAL
                reason = "Repeated validated failure/procedure became a reusable skill."
            return ConsolidationCandidate(
                source=record,
                target_layer=target,
                reason=reason,
                promoted_confidence=min(max(record.confidence, validation_score), 0.95),
            )

        if record.layer == MemoryLayer.PROCEDURAL and validation_score >= 0.95 and repeat_count >= 5:
            return ConsolidationCandidate(
                source=record,
                target_layer=MemoryLayer.POLICY,
                reason="Procedure succeeded repeatedly enough to become policy candidate.",
                promoted_confidence=min(max(record.confidence, validation_score), 0.99),
            )

        return None

    def promote(self, candidate: ConsolidationCandidate) -> MemoryRecord:
        src = candidate.source
        evidence = list(src.evidence)
        evidence.append(
            EvidenceRef(
                source="consolidator",
                locator=src.id,
                quote=candidate.reason,
            )
        )
        return MemoryRecord(
            title=f"Promoted: {src.title}",
            content=src.content,
            layer=candidate.target_layer,
            kind=_target_kind(src.kind, candidate.target_layer),
            tags={**src.tags, "promoted_from": src.layer.value},
            metadata={
                **src.metadata,
                "source_record_ids": [src.id],
                "source_layer": src.layer.value,
                "destination_layer": candidate.target_layer.value,
                "evidence_refs": [ref.__dict__ for ref in evidence],
                "promotion_confidence": candidate.promoted_confidence,
                "validation_method": "score_threshold",
                "promotion_reason": candidate.reason,
                "promoted_at": datetime.now(UTC).isoformat(),
            },
            evidence=evidence,
            confidence=candidate.promoted_confidence,
            importance=max(src.importance, 0.7),
        )


def _target_kind(kind: MemoryKind, target_layer: MemoryLayer) -> MemoryKind:
    if target_layer == MemoryLayer.POLICY:
        return MemoryKind.POLICY
    if target_layer == MemoryLayer.PROCEDURAL:
        return MemoryKind.PROCEDURE
    if target_layer == MemoryLayer.SEMANTIC:
        return MemoryKind.FACT
    if target_layer == MemoryLayer.EPISODIC:
        return MemoryKind.EVENT
    return kind
