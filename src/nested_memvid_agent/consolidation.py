from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .nested_learning import (
    ContextFlow,
    LearningDecision,
    LearningSignal,
    NestedLearningKernel,
    OptimizerTrace,
    ValidationEvidence,
)


@dataclass(frozen=True)
class ConsolidationCandidate:
    source: MemoryRecord
    target_layer: MemoryLayer
    reason: str
    promoted_confidence: float
    flow: ContextFlow
    optimizer_trace: OptimizerTrace
    signal: LearningSignal
    decision: LearningDecision


class Consolidator:
    """Promotion rules between nested memory layers.

    This is deliberately conservative. Permanent memory is earned, not shoveled.
    """

    def propose(
        self,
        record: MemoryRecord,
        validation_score: float | None,
        repeat_count: int = 1,
        *,
        validation_evidence: ValidationEvidence | None = None,
        explicit_instruction: bool = False,
    ) -> ConsolidationCandidate | None:
        decision = NestedLearningKernel().from_record(
            record,
            validation_score=validation_score,
            validation_evidence=validation_evidence,
            repeat_count=repeat_count,
            explicit_instruction=explicit_instruction,
        )
        if not decision.accepted or decision.target_layer is None:
            return None
        return ConsolidationCandidate(
            source=record,
            target_layer=decision.target_layer,
            reason=decision.reason,
            promoted_confidence=decision.confidence,
            flow=decision.flow,
            optimizer_trace=decision.optimizer_trace,
            signal=LearningSignal(
                title=record.title,
                content=record.content,
                kind=record.kind,
                source_layer=record.layer,
                confidence=record.confidence,
                importance=record.importance,
                validation_score=None if validation_evidence is not None else validation_score,
                validation_evidence=validation_evidence,
                repeat_count=repeat_count,
                explicit_instruction=explicit_instruction,
                source="consolidator",
                locator=record.id,
                tags=record.tags,
                metadata=record.metadata,
            ),
            decision=decision,
        )

    def promote(self, candidate: ConsolidationCandidate) -> MemoryRecord:
        src = candidate.source
        promoted = NestedLearningKernel().to_memory_record(candidate.signal, candidate.decision)
        validation_source_ids = tuple(
            dict.fromkeys(
                ref.locator.strip()
                for ref in (
                    candidate.signal.validation_evidence.all_refs()
                    if candidate.signal.validation_evidence is not None
                    else ()
                )
                if ref.source.strip() == "memory_record" and ref.locator.strip()
            )
        )
        source_record_ids = tuple(dict.fromkeys((src.id, *validation_source_ids)))
        # Keep the validated claim title byte-for-byte so receipt subject
        # digests remain bound across the episodic -> stable transition.
        promoted.title = src.title
        promoted.tags = {**promoted.tags, "promoted_from": src.layer.value}
        promoted.metadata.update(
            {
                "source_record_ids": list(source_record_ids),
                "destination_layer": candidate.target_layer.value,
                "promotion_confidence": candidate.promoted_confidence,
                "promotion_reason": candidate.reason,
                "promoted_at": datetime.now(UTC).isoformat(),
            }
        )
        promoted.evidence = [
            *src.evidence,
            *promoted.evidence,
            *(
                EvidenceRef(source="memory_record", locator=source_id)
                for source_id in source_record_ids
                if not any(ref.locator == source_id for ref in promoted.evidence)
            ),
        ]
        return promoted


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
