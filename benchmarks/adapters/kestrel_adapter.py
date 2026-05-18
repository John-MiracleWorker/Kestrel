"""Kestrel LayeredMemory adapter for unified benchmarking."""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayerSpec, LayeredMemorySystem
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery

from .base import RetrievalResult


def _hybrid_specs() -> dict[MemoryLayer, LayerSpec]:
    """Return layer specs with hybrid vector search enabled for benchmark layers."""
    specs = copy.deepcopy(DEFAULT_LAYER_SPECS)
    for layer in (MemoryLayer.SEMANTIC, MemoryLayer.EPISODIC, MemoryLayer.PROCEDURAL, MemoryLayer.SELF):
        if layer in specs:
            spec = specs[layer]
            specs[layer] = LayerSpec(
                layer=spec.layer,
                description=spec.description,
                mv2_file=spec.mv2_file,
                update_cadence=spec.update_cadence,
                retrieval_k=spec.retrieval_k,
                context_budget_chars=spec.context_budget_chars,
                min_write_confidence=spec.min_write_confidence,
                promotion_threshold=spec.promotion_threshold,
                provisional_threshold=spec.provisional_threshold,
                min_repeat_count_for_promotion=spec.min_repeat_count_for_promotion,
                retention_days=spec.retention_days,
                search_mode="hybrid",
                vector_search_enabled=True,
                vector_embedding_provider="local",
                vector_index_path=None,
                hybrid_search_enabled=True,
            )
    return specs


class KestrelAdapter:
    """Kestrel layered memory system with optional hybrid vector search."""

    def __init__(self, hybrid: bool = True) -> None:
        specs = _hybrid_specs() if hybrid else None
        self.memory = LayeredMemorySystem.from_backend_factory(
            Path("/tmp/kestrel-bench-adapter"),
            InMemoryBackend,
            specs=specs,
            enable_vec=hybrid,
        )
        self._hybrid = hybrid

    def name(self) -> str:
        if self._hybrid:
            return "Kestrel (Layered Memvid v2 + Hybrid BM25+Vector)"
        return "Kestrel (Layered Memvid v2)"

    def ingest(self, doc_id: str, text: str, layer: str | None = None) -> None:
        layer_enum = MemoryLayer(layer or "semantic")
        kind = MemoryKind.FACT
        if layer_enum == MemoryLayer.EPISODIC:
            kind = MemoryKind.EVENT
        elif layer_enum == MemoryLayer.PROCEDURAL:
            kind = MemoryKind.PROCEDURE
        record = MemoryRecord(
            id=doc_id,
            title=doc_id,
            content=text,
            layer=layer_enum,
            kind=kind,
            confidence=max(self.memory.specs[layer_enum].min_write_confidence, 0.85),
        )
        self.memory.put(record)

    def retrieve(self, query: str, k: int = 5, layer: str | None = None) -> list[RetrievalResult]:
        layer_enum = MemoryLayer(layer or "semantic")
        hits = self.memory.retrieve(
            RetrievalQuery(query=query, k_per_layer=k, layers=(layer_enum,))
        )
        return [
            RetrievalResult(
                doc_id=hit.record.id,
                text=hit.record.content,
                score=hit.score or 0.0,
                metadata={"layer": hit.record.layer.value}
            )
            for hit in hits
        ]
