from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

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

    def find(self, query: str, k: int = 8, mode: str = "auto", min_relevancy: float = 0.0) -> list[MemoryHit]:
        del mode
        query_tokens = set(_tokens(query))
        if not query_tokens:
            return []
        hits: list[MemoryHit] = []
        for record in self.records:
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
