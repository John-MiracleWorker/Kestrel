from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord

ALLOWED_FRAME_TYPES = frozenset(
    {
        "raw_chunk",
        "section_summary",
        "task_summary",
        "session_summary",
        "skill_card",
        "failure_note",
        "correction",
        "conflict_set",
        "trace_stub",
    }
)


@dataclass
class MV2ContextFrame:
    """Structured context unit stored in or derived from a Memvid `.mv2` record."""

    id: str
    frame_type: str
    title: str
    content: str
    layer: MemoryLayer
    kind: MemoryKind
    parent_ids: tuple[str, ...] = ()
    child_ids: tuple[str, ...] = ()
    source_uri: str | None = None
    source_span: dict[str, object] = field(default_factory=dict)
    content_hash: str = ""
    token_count: int = 0
    confidence: float = 0.5
    importance: float = 0.5
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tags: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.frame_type not in ALLOWED_FRAME_TYPES:
            allowed = ", ".join(sorted(ALLOWED_FRAME_TYPES))
            raise ValueError(f"frame_type must be one of: {allowed}")
        if not self.title.strip():
            raise ValueError("MV2ContextFrame.title cannot be empty")
        if not self.content.strip():
            raise ValueError("MV2ContextFrame.content cannot be empty")
        self.confidence = _bounded(self.confidence, "confidence")
        self.importance = _bounded(self.importance, "importance")
        if not self.content_hash:
            self.content_hash = content_hash_for(self.content)
        if self.token_count <= 0:
            self.token_count = estimate_tokens(self.content)


def from_memory_record(record: MemoryRecord, frame_type: str = "raw_chunk") -> MV2ContextFrame:
    """Build a context frame from the canonical memory record shape."""

    metadata = dict(record.metadata)
    resolved_frame_type = str(metadata.get("frame_type") or frame_type)
    frame_id = str(metadata.get("frame_id") or record.id)
    return MV2ContextFrame(
        id=frame_id,
        frame_type=resolved_frame_type,
        title=record.title,
        content=record.content,
        layer=record.layer,
        kind=record.kind,
        parent_ids=_tuple_str(metadata.get("parent_ids")),
        child_ids=_tuple_str(metadata.get("child_ids")),
        source_uri=_optional_str(metadata.get("source_uri")),
        source_span=_dict_object(metadata.get("source_span")),
        content_hash=str(metadata.get("content_hash") or content_hash_for(record.content)),
        token_count=_int_value(metadata.get("token_count"), estimate_tokens(record.content)),
        confidence=record.confidence,
        importance=record.importance,
        created_at=record.created_at,
        updated_at=record.updated_at,
        tags=dict(record.tags),
        metadata=metadata,
    )


def to_memory_record(frame: MV2ContextFrame) -> MemoryRecord:
    """Convert a context frame back into the backend-neutral memory record shape."""

    metadata: dict[str, Any] = {
        **dict(frame.metadata),
        "mv2_ctx_version": "0.1",
        "frame_type": frame.frame_type,
        "frame_id": frame.id,
        "parent_ids": list(frame.parent_ids),
        "child_ids": list(frame.child_ids),
        "source_uri": frame.source_uri,
        "source_span": dict(frame.source_span),
        "token_count": frame.token_count,
        "content_hash": frame.content_hash,
        "nested_layer": frame.layer.value,
        "nested_kind": frame.kind.value,
        "nested_confidence": frame.confidence,
        "nested_importance": frame.importance,
    }
    evidence = []
    if frame.source_uri:
        evidence.append(EvidenceRef(source=frame.source_uri, locator=_span_locator(frame.source_span)))
    return MemoryRecord(
        id=frame.id,
        title=frame.title,
        content=frame.content,
        layer=frame.layer,
        kind=frame.kind,
        tags=dict(frame.tags),
        metadata=metadata,
        evidence=evidence,
        confidence=frame.confidence,
        importance=frame.importance,
        created_at=frame.created_at,
        updated_at=frame.updated_at,
    )


def estimate_tokens(text: str, model_hint: str | None = None) -> int:
    """Approximate tokens without adding a tokenizer dependency.

    The model hint is accepted so callers can swap in model-specific tokenizers later.
    """

    del model_hint
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, (len(stripped) + 3) // 4)


def content_hash_for(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _bounded(value: float, name: str) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return value


def _tuple_str(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return (str(value),)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dict_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}


def _int_value(value: object, default: int) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _span_locator(span: dict[str, object]) -> str:
    if not span:
        return "unknown"
    if "path" in span:
        return str(span["path"])
    if "start" in span or "end" in span:
        return f"{span.get('start', '?')}:{span.get('end', '?')}"
    return "source_span"
