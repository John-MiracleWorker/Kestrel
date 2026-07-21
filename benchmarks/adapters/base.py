"""Common interface for all memory backends being benchmarked."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class RetrievalResult:
    doc_id: str
    text: str
    score: float
    metadata: dict[str, Any]


class OptionalDependencyUnavailable(RuntimeError):
    """Signal that an optional benchmark backend cannot be constructed locally."""

    def __init__(
        self,
        backend_name: str,
        *,
        missing_dependency: str,
        install_hint: str,
    ) -> None:
        self.backend_name = backend_name
        self.missing_dependency = missing_dependency
        self.install_hint = install_hint
        super().__init__(
            f"{backend_name} skipped: missing optional dependency "
            f"{missing_dependency!r}. Install with: {install_hint}"
        )


class MemoryBackend(Protocol):
    """Minimal interface any memory system must implement for benchmarking."""

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        ...

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        ...

    def name(self) -> str:
        ...
