from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .layers import LayeredMemorySystem
from .models import MemoryKind, MemoryLayer, MemoryRecord
from .nested_learning import NestedLearningKernel


class Summarizer(Protocol):
    def __call__(self, records: list[MemoryRecord]) -> str: ...


class RetentionCompactor:
    """TTL compaction for volatile memory layers.

    Stable layers are skipped by default; corrections can still tombstone records
    through the mutation contract.
    """

    ttl_layers = frozenset({MemoryLayer.WORKING, MemoryLayer.EPISODIC})

    def __init__(self, memory: LayeredMemorySystem, summarizer: Summarizer | None = None) -> None:
        self.memory = memory
        self.summarizer = summarizer or deterministic_summary

    def compact_layer(self, layer: MemoryLayer, dry_run: bool = True) -> dict[str, Any]:
        if layer not in self.ttl_layers:
            return {
                "layer": layer.value,
                "dry_run": dry_run,
                "skipped": True,
                "reason": "stable_layer",
                "candidate_count": 0,
                "promoted_ids": [],
                "tombstoned_ids": [],
                "summary_record_id": None,
            }
        now = datetime.now(UTC)
        spec = self.memory.specs[layer]
        cutoff = now - timedelta(days=spec.retention_days)
        candidates = [
            record
            for record in self.memory.iter_records(layer)
            if record.expires_at is not None and record.expires_at <= now or record.created_at <= cutoff
        ]
        promoted_ids: list[str] = []
        tombstoned_ids: list[str] = []
        summary_record_id: str | None = None
        if candidates and not dry_run:
            summary = MemoryRecord(
                title=f"Compacted {layer.value} summary",
                content=self.summarizer(candidates),
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                confidence=0.72,
                importance=max((record.importance for record in candidates), default=0.5),
                metadata={
                    "frame_type": "session_summary",
                    "source_record_ids": [record.id for record in candidates],
                    "retention_compaction": True,
                },
            )
            summary_record_id = self.memory.put(summary)
            kernel = NestedLearningKernel(specs=self.memory.specs, memory=self.memory)
            for record in candidates:
                validation_score = float(record.metadata.get("validation_score", record.confidence))
                repeat_count = int(record.metadata.get("repeat_count", 1))
                decision = kernel.from_record(record, validation_score=validation_score, repeat_count=repeat_count)
                if decision.accepted and decision.target_layer is not None and decision.target_layer != record.layer:
                    promoted = kernel.to_memory_record(
                        # The consolidator keeps raw-score compatibility for retained records.
                        _signal_from_record(record, validation_score=validation_score, repeat_count=repeat_count),
                        decision,
                    )
                    promoted_ids.append(self.memory.put(promoted))
                if self.memory.tombstone(layer, record.id, reason="retention_compacted", superseded_by=summary_record_id):
                    tombstoned_ids.append(record.id)
        return {
            "layer": layer.value,
            "dry_run": dry_run,
            "skipped": False,
            "reason": "ttl",
            "candidate_count": len(candidates),
            "candidate_ids": [record.id for record in candidates],
            "promoted_ids": promoted_ids,
            "tombstoned_ids": tombstoned_ids,
            "summary_record_id": summary_record_id,
        }


def deterministic_summary(records: list[MemoryRecord]) -> str:
    lines = ["Deterministic retention summary:"]
    for record in sorted(records, key=lambda item: (item.created_at, item.id)):
        compact = " ".join(record.content.split())
        lines.append(f"- {record.title}: {compact[:240]}")
    return "\n".join(lines)


def _signal_from_record(record: MemoryRecord, *, validation_score: float, repeat_count: int) -> Any:
    from .nested_learning import LearningSignal

    return LearningSignal(
        title=record.title,
        content=record.content,
        kind=record.kind,
        source_layer=record.layer,
        confidence=record.confidence,
        importance=record.importance,
        validation_score=validation_score,
        repeat_count=repeat_count,
        source="retention_compactor",
        locator=record.id,
        tags=record.tags,
        metadata=record.metadata,
    )
