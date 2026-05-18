"""Common interface for all memory backends being benchmarked."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class RetrievalResult:
    doc_id: str
    text: str
    score: float
    metadata: dict[str, Any]


class MemoryBackend(Protocol):
    """Minimal interface any memory system must implement for benchmarking."""

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        ...

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        ...

    def name(self) -> str:
        ...
