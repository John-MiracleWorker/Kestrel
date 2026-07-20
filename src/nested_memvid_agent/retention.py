from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Protocol

from .layers import LayeredMemorySystem
from .models import MemoryKind, MemoryLayer, MemoryRecord


class Summarizer(Protocol):
    def __call__(self, records: list[MemoryRecord]) -> str: ...


class RetentionCompactor:
    """TTL compaction for volatile memory layers.

    Stable layers are skipped by default; corrections can still tombstone records
    through the mutation contract.
    """

    ttl_layers = frozenset({MemoryLayer.WORKING, MemoryLayer.EPISODIC})

    def __init__(
        self,
        memory: LayeredMemorySystem,
        summarizer: Summarizer | None = None,
        *,
        max_candidates_per_run: int = 1_000,
        max_summary_chars: int = 12_000,
    ) -> None:
        if max_candidates_per_run < 1:
            raise ValueError("max_candidates_per_run must be >= 1")
        if max_summary_chars < 256:
            raise ValueError("max_summary_chars must be >= 256")
        self.memory = memory
        self.summarizer = summarizer or deterministic_summary
        self.max_candidates_per_run = max_candidates_per_run
        self.max_summary_chars = max_summary_chars

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
        all_candidates = sorted(
            (
                record
                for record in self.memory.iter_records(layer)
                if _retention_due(record, now=now, cutoff=cutoff)
            ),
            key=lambda record: (_as_utc(record.created_at), record.id),
        )
        candidates = all_candidates[: self.max_candidates_per_run]
        summarizable = [
            record for record in candidates if not _is_generated_memory_artifact(record)
        ]
        promoted_ids: list[str] = []
        tombstoned_ids: list[str] = []
        summary_record_id: str | None = None
        if summarizable and not dry_run:
            raw_summary = self.summarizer(summarizable)
            summary = MemoryRecord(
                title=f"Compacted {layer.value} summary",
                content=_bounded_summary(
                    raw_summary,
                    max_chars=self.max_summary_chars,
                    record_count=len(summarizable),
                ),
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                confidence=0.72,
                importance=max((record.importance for record in summarizable), default=0.5),
                metadata={
                    "frame_type": "session_summary",
                    "source_record_ids": [record.id for record in summarizable],
                    "retention_compaction": True,
                    "validation_status": "deterministic_compaction",
                },
            )
            summary_record_id = self.memory.put(summary)
        if candidates and not dry_run:
            for record in candidates:
                if record.metadata.get("promotion_id") and not record.metadata.get(
                    "last_retrieved_at"
                ):
                    self.memory.record_promotion_outcome(
                        str(record.metadata["promotion_id"]),
                        "never_retrieved",
                        evidence_record_id=summary_record_id,
                        notes="retention compaction summarized record before any debounced retrieval write-back",
                    )
                if self.memory.tombstone(
                    layer, record.id, reason="retention_compacted", superseded_by=summary_record_id
                ):
                    tombstoned_ids.append(record.id)
        return {
            "layer": layer.value,
            "dry_run": dry_run,
            "skipped": False,
            "reason": "ttl",
            "candidate_count": len(all_candidates),
            "processed_count": len(candidates),
            "deferred_count": max(len(all_candidates) - len(candidates), 0),
            "candidate_ids": [record.id for record in candidates],
            "summarized_count": len(summarizable),
            "artifact_count": len(candidates) - len(summarizable),
            "promoted_ids": promoted_ids,
            "tombstoned_ids": tombstoned_ids,
            "summary_record_id": summary_record_id,
        }


def deterministic_summary(records: list[MemoryRecord]) -> str:
    lines = ["Deterministic retention summary:"]
    for record in sorted(records, key=lambda item: (_as_utc(item.created_at), item.id)):
        compact = " ".join(record.content.split())
        lines.append(f"- {record.title}: {compact[:240]}")
    return "\n".join(lines)


def _is_generated_memory_artifact(record: MemoryRecord) -> bool:
    metadata = record.metadata
    return bool(
        metadata.get("retrieval_artifact") is True
        or metadata.get("retention_compaction") is True
        or metadata.get("retention_artifact") is True
    )


def _retention_due(
    record: MemoryRecord,
    *,
    now: datetime,
    cutoff: datetime,
) -> bool:
    expires_at = _as_utc(record.expires_at) if record.expires_at is not None else None
    return bool(
        (expires_at is not None and expires_at <= now) or _as_utc(record.created_at) <= cutoff
    )


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _bounded_summary(text: str, *, max_chars: int, record_count: int) -> str:
    if len(text) <= max_chars:
        return text
    digest = sha256(text.encode("utf-8")).hexdigest()
    marker = (
        "\n[TRUNCATED_RETENTION_SUMMARY "
        f"records={record_count} total_chars={len(text)} sha256={digest}]"
    )
    return text[: max(max_chars - len(marker), 1)].rstrip() + marker
