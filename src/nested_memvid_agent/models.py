from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any
from uuid import uuid4


class MemoryLayer(StrEnum):
    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    SELF = "self"
    POLICY = "policy"


class MemoryKind(StrEnum):
    OBSERVATION = "observation"
    FACT = "fact"
    EVENT = "event"
    DECISION = "decision"
    FAILURE = "failure"
    PROCEDURE = "procedure"
    POLICY = "policy"
    SUMMARY = "summary"
    CORRECTION = "correction"


@dataclass(frozen=True)
class EvidenceRef:
    """Pointer to evidence that supports a memory record."""

    source: str
    locator: str
    quote: str | None = None


@dataclass
class MemoryRecord:
    """Canonical unit that can be stored in Memvid or a test backend.

    Promotion metadata keys used by the learning loop include
    `promotion_id`, `promotion_status`, `provisional_admitted_at`, and
    `last_retrieved_at`.
    """

    content: str
    layer: MemoryLayer
    title: str
    kind: MemoryKind = MemoryKind.OBSERVATION
    id: str = field(default_factory=lambda: f"mem_{uuid4().hex}")
    tags: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    evidence: list[EvidenceRef] = field(default_factory=list)
    confidence: float = 0.5
    importance: float = 0.5
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("MemoryRecord.content cannot be empty")
        if not self.title.strip():
            raise ValueError("MemoryRecord.title cannot be empty")
        self.confidence = _bounded(self.confidence, "confidence")
        self.importance = _bounded(self.importance, "importance")

    @property
    def content_hash(self) -> str:
        payload = f"{self.layer}:{self.kind}:{self.title}:{self.content}".encode()
        return sha256(payload).hexdigest()

    def to_text_block(self) -> str:
        evidence_lines = []
        for ref in self.evidence:
            quote = f" — {ref.quote}" if ref.quote else ""
            evidence_lines.append(f"- {ref.source}:{ref.locator}{quote}")
        evidence_text = "\n".join(evidence_lines) if evidence_lines else "- none"
        return (
            f"Title: {self.title}\n"
            f"Layer: {self.layer}\n"
            f"Kind: {self.kind}\n"
            f"Confidence: {self.confidence:.2f}\n"
            f"Importance: {self.importance:.2f}\n"
            f"Content:\n{self.content}\n"
            f"Evidence:\n{evidence_text}"
        )

    def to_metadata(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "layer": self.layer.value,
            "kind": self.kind.value,
            "confidence": self.confidence,
            "importance": self.importance,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "content_hash": self.content_hash,
            "evidence": [ref.__dict__ for ref in self.evidence],
            **self.metadata,
        }


@dataclass(frozen=True)
class RetrievalQuery:
    query: str
    layers: tuple[MemoryLayer, ...] = tuple(MemoryLayer)
    k_per_layer: int = 8
    mode: str = "auto"
    min_relevancy: float = 0.0
    objective: str | None = None
    include_inactive: bool = False

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("RetrievalQuery.query cannot be empty")
        if self.k_per_layer < 1:
            raise ValueError("RetrievalQuery.k_per_layer must be >= 1")


@dataclass(frozen=True)
class MemoryHit:
    record: MemoryRecord
    score: float
    source_backend: str
    frame_id: str | None = None
    snippet: str | None = None


@dataclass(frozen=True)
class CompiledContext:
    objective: str
    prompt: str
    hits: tuple[MemoryHit, ...]
    total_chars: int
    budget_chars: int
    warnings: tuple[str, ...] = ()


def _bounded(value: float, name: str) -> float:
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0.0 and 1.0")
    return value
