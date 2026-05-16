from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from .context_frames import default_frame_type_for_memory
from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .nested_learning import ContextFlow, NestedLearningKernel, OptimizerTrace


@dataclass(frozen=True)
class ConsolidationCandidate:
    source: MemoryRecord
    target_layer: MemoryLayer
    reason: str
    promoted_confidence: float
    flow: ContextFlow
    optimizer_trace: OptimizerTrace


class Consolidator:
    """Promotion rules between nested memory layers.

    This is deliberately conservative. Permanent memory is earned, not shoveled.
    """

    def propose(
        self,
        record: MemoryRecord,
        validation_score: float,
        repeat_count: int = 1,
        *,
        explicit_instruction: bool = False,
    ) -> ConsolidationCandidate | None:
        decision = NestedLearningKernel().from_record(
            record,
            validation_score=validation_score,
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
        )

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
                "frame_type": default_frame_type_for_memory(_target_kind(src.kind, candidate.target_layer), candidate.target_layer),
                "source_record_ids": [src.id],
                "source_layer": src.layer.value,
                "destination_layer": candidate.target_layer.value,
                "evidence_refs": [ref.__dict__ for ref in evidence],
                "promotion_confidence": candidate.promoted_confidence,
                "validation_method": "score_threshold",
                "promotion_reason": candidate.reason,
                "promoted_at": datetime.now(UTC).isoformat(),
                "nested_learning": {
                    "context_flow": candidate.flow.to_metadata(),
                    "optimizer_trace": candidate.optimizer_trace.to_metadata(),
                    "source_record_ids": [src.id],
                },
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
