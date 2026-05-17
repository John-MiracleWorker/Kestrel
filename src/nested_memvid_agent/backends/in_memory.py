from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..context_frames import MV2ContextFrame, to_memory_record
from ..models import MemoryHit, MemoryKind, MemoryLayer, MemoryRecord
from .base import MemoryBackend

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")


class InMemoryBackend(MemoryBackend):
    """Deterministic backend for local tests and Codex-safe development."""

    _global_records: dict[str, list[MemoryRecord]] = {}

    def __init__(self, path: Path, layer: MemoryLayer, **kwargs: object) -> None:
        super().__init__(path, layer, **kwargs)
        self.records: list[MemoryRecord] = []

    def open(self) -> None:
        key = str(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if key in self._global_records:
            self.records = self._global_records[key]
            return
        snapshot_path = self.path.with_suffix(".memory.json")
        if snapshot_path.exists():
            loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
            self.records = [_record_from_snapshot(item, self.layer) for item in loaded]
        else:
            self.records = []
        self._global_records[key] = self.records

    def put(self, record: MemoryRecord) -> str:
        if record.layer != self.layer:
            raise ValueError(f"Cannot write {record.layer} record to {self.layer} backend")
        self.records.append(record)
        return record.id

    def upsert(self, record: MemoryRecord) -> str:
        if record.layer != self.layer:
            raise ValueError(f"Cannot write {record.layer} record to {self.layer} backend")
        for index, existing in enumerate(self.records):
            if existing.id == record.id:
                self.records[index] = record
                return record.id
        self.records.append(record)
        return record.id

    def tombstone(self, record_id: str, *, reason: str, superseded_by: str | None = None) -> bool:
        record = self.get_record(record_id, include_inactive=True)
        if record is None:
            return False
        record.metadata["active"] = False
        record.metadata["tombstone_reason"] = reason
        record.metadata["tombstoned_at"] = datetime.now(UTC).isoformat()
        if superseded_by:
            record.metadata["superseded_by"] = superseded_by
        record.updated_at = datetime.now(UTC)
        return True

    def iter_records(self, *, include_inactive: bool = False) -> Iterable[MemoryRecord]:
        return tuple(record for record in self.records if include_inactive or _is_active(record))

    def get_record(self, record_id: str, *, include_inactive: bool = True) -> MemoryRecord | None:
        for record in self.records:
            metadata = record.metadata
            if record.id == record_id or str(metadata.get("frame_id", "")) == record_id:
                if include_inactive or _is_active(record):
                    return record
                return None
        return None

    def put_frame(self, frame: MV2ContextFrame) -> str:
        return self.put(to_memory_record(frame))

    def find(
        self,
        query: str,
        k: int = 8,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        del mode
        query_tokens = set(_tokens(query))
        if not query_tokens:
            return []
        hits: list[MemoryHit] = []
        for record in self.records:
            if not include_inactive and not _is_active(record):
                continue
            record_text = f"{record.title} {record.content} {' '.join(record.tags.values())}"
            record_tokens = set(_tokens(record_text))
            overlap = query_tokens & record_tokens
            if not overlap:
                continue
            precision = len(overlap) / max(len(query_tokens), 1)
            coverage = len(overlap) / max(len(record_tokens), 1)
            score = (0.7 * precision) + (0.3 * coverage) + (0.05 * record.importance)
            score = min(score, 1.0)
            if score >= min_relevancy:
                hits.append(
                    MemoryHit(
                        record=record,
                        score=score,
                        source_backend="memory",
                        frame_id=record.id,
                        snippet=_snippet(record.content, query_tokens),
                    )
                )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:k]

    def find_frames(
        self,
        query: str,
        k: int = 8,
        layers: tuple[MemoryLayer, ...] | None = None,
        frame_types: tuple[str, ...] | None = None,
        mode: str = "auto",
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        if layers is not None and self.layer not in layers:
            return []
        hits = self.find(query=query, k=k, mode=mode, include_inactive=include_inactive)
        if frame_types is None:
            return hits
        allowed = set(frame_types)
        return [hit for hit in hits if str(hit.record.metadata.get("frame_type", "raw_chunk")) in allowed]

    def seal(self) -> None:
        # Write a readable snapshot to disk to aid debugging. This is not a database.
        snapshot = [
            {
                "id": rec.id,
                "title": rec.title,
                "layer": rec.layer.value,
                "kind": rec.kind.value,
                "content": rec.content,
                "confidence": rec.confidence,
                "importance": rec.importance,
                "tags": rec.tags,
                "metadata": rec.metadata,
                "evidence": [ref.__dict__ for ref in rec.evidence],
                "created_at": rec.created_at.isoformat(),
                "updated_at": rec.updated_at.isoformat(),
                "expires_at": rec.expires_at.isoformat() if rec.expires_at else None,
            }
            for rec in self.records
        ]
        self.path.with_suffix(".memory.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    def verify(self) -> bool:
        return all(record.layer == self.layer and bool(record.content.strip()) for record in self.records)

    def close(self) -> None:
        return None


def _tokens(text: str) -> list[str]:
    return [match.group(0).lower() for match in _TOKEN_RE.finditer(text)]


def _snippet(text: str, query_tokens: set[str], window: int = 220) -> str:
    lower = text.lower()
    first_idx = min((lower.find(token) for token in query_tokens if token in lower), default=0)
    start = max(first_idx - 60, 0)
    snippet = text[start : start + window]
    return snippet.strip()


def _is_active(record: MemoryRecord) -> bool:
    return record.metadata.get("active", True) is not False


def _record_from_snapshot(item: dict[str, object], expected_layer: MemoryLayer) -> MemoryRecord:
    layer_value = str(item.get("layer", expected_layer.value))
    try:
        layer = MemoryLayer(layer_value)
    except ValueError:
        layer = expected_layer
    kind_value = str(item.get("kind", MemoryKind.OBSERVATION.value))
    try:
        kind = MemoryKind(kind_value)
    except ValueError:
        kind = MemoryKind.OBSERVATION
    return MemoryRecord(
        id=str(item.get("id", "mem_loaded")),
        title=str(item.get("title", "Loaded memory")),
        content=str(item.get("content", "")),
        layer=layer,
        kind=kind,
        confidence=_as_float(item.get("confidence"), 0.5),
        importance=_as_float(item.get("importance"), 0.5),
        tags=_as_str_dict(item.get("tags")),
        metadata=_as_any_dict(item.get("metadata")),
    )


def _as_float(value: object, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _as_str_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(val) for key, val in value.items()}


def _as_any_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {str(key): val for key, val in value.items()}
