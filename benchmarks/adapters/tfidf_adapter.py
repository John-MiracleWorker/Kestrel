"""TF-IDF baseline adapter with unified interface."""
from __future__ import annotations

from typing import Any

from .base import RetrievalResult
from .baseline_rag_flat import BaselineRAG as _BaselineRAGInner


class TFIDFAdapter:
    """Flat TF-IDF RAG baseline wrapper."""

    def __init__(self) -> None:
        self._inner = _BaselineRAGInner()
        self._doc_map: dict[str, str] = {}

    def name(self) -> str:
        return "TF-IDF Baseline (Flat RAG)"

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        inner_id = self._inner.ingest(text, metadata={"id": doc_id, "layer": layer})
        self._doc_map[inner_id] = doc_id

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        results = self._inner.retrieve(query, k=k)
        out = []
        for r in results:
            out.append(RetrievalResult(
                doc_id=r.doc.metadata.get("id", r.doc.id),
                text=r.doc.text,
                score=r.score,
                metadata={"layer": r.doc.metadata.get("layer")}
            ))
        return out
