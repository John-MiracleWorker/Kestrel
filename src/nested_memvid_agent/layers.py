from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .backends.base import MemoryBackend
from .context_frames import MV2ContextFrame, to_memory_record
from .models import MemoryHit, MemoryLayer, MemoryRecord, RetrievalQuery


@dataclass(frozen=True)
class LayerSpec:
    layer: MemoryLayer
    description: str
    mv2_file: str
    update_cadence: str
    retrieval_k: int
    context_budget_chars: int
    min_write_confidence: float
    promotion_threshold: float
    retention_days: int
    search_mode: str = "auto"


DEFAULT_LAYER_SPECS: dict[MemoryLayer, LayerSpec] = {
    MemoryLayer.WORKING: LayerSpec(
        layer=MemoryLayer.WORKING,
        description="Current task state, volatile scratch memory, active assumptions.",
        mv2_file="working.mv2",
        update_cadence="every_step",
        retrieval_k=6,
        context_budget_chars=3500,
        min_write_confidence=0.2,
        promotion_threshold=0.65,
        retention_days=2,
        search_mode="lex",
    ),
    MemoryLayer.EPISODIC: LayerSpec(
        layer=MemoryLayer.EPISODIC,
        description="Meaningful events, tool results, failures, decisions, session summaries.",
        mv2_file="episodic.mv2",
        update_cadence="event_or_session",
        retrieval_k=8,
        context_budget_chars=4500,
        min_write_confidence=0.5,
        promotion_threshold=0.75,
        retention_days=90,
        search_mode="auto",
    ),
    MemoryLayer.SEMANTIC: LayerSpec(
        layer=MemoryLayer.SEMANTIC,
        description="Stable facts about codebases, APIs, users, projects, and domains.",
        mv2_file="semantic.mv2",
        update_cadence="validated_fact",
        retrieval_k=8,
        context_budget_chars=4500,
        min_write_confidence=0.75,
        promotion_threshold=0.85,
        retention_days=365,
        search_mode="auto",
    ),
    MemoryLayer.PROCEDURAL: LayerSpec(
        layer=MemoryLayer.PROCEDURAL,
        description="Reusable methods, debug recipes, known workflows, tool-use skills.",
        mv2_file="procedural.mv2",
        update_cadence="validated_repeated_success",
        retrieval_k=6,
        context_budget_chars=4000,
        min_write_confidence=0.82,
        promotion_threshold=0.9,
        retention_days=365,
        search_mode="auto",
    ),
    MemoryLayer.SELF: LayerSpec(
        layer=MemoryLayer.SELF,
        description="Validated self-model, identity, capability snapshots, and user-specific workflow preferences.",
        mv2_file="self.mv2",
        update_cadence="validated_self_reflection",
        retrieval_k=6,
        context_budget_chars=3500,
        min_write_confidence=0.78,
        promotion_threshold=0.88,
        retention_days=365,
        search_mode="auto",
    ),
    MemoryLayer.POLICY: LayerSpec(
        layer=MemoryLayer.POLICY,
        description="Slow-changing behavior rules and safety constraints. Write rarely.",
        mv2_file="policy.mv2",
        update_cadence="rare_manual_or_high_confidence",
        retrieval_k=5,
        context_budget_chars=3000,
        min_write_confidence=0.95,
        promotion_threshold=0.97,
        retention_days=730,
        search_mode="lex",
    ),
}


class LayeredMemorySystem:
    """Routes reads/writes across nested memory layers."""

    def __init__(
        self,
        backends: dict[MemoryLayer, MemoryBackend],
        specs: dict[MemoryLayer, LayerSpec] | None = None,
    ) -> None:
        self.specs = specs or DEFAULT_LAYER_SPECS
        missing = set(self.specs) - set(backends)
        if missing:
            missing_names = ", ".join(layer.value for layer in sorted(missing, key=str))
            raise ValueError(f"Missing backends for layers: {missing_names}")
        self.backends = backends

    @classmethod
    def from_backend_factory(
        cls,
        memory_dir: Path,
        backend_factory: type[MemoryBackend],
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        **backend_kwargs: object,
    ) -> LayeredMemorySystem:
        layer_specs = specs or DEFAULT_LAYER_SPECS
        memory_dir.mkdir(parents=True, exist_ok=True)
        backends: dict[MemoryLayer, MemoryBackend] = {}
        for layer, spec in layer_specs.items():
            path = memory_dir / spec.mv2_file
            backend = backend_factory(path=path, layer=layer, **backend_kwargs)
            backend.open()
            backends[layer] = backend
        return cls(backends=backends, specs=layer_specs)

    def put(self, record: MemoryRecord) -> str:
        spec = self.specs[record.layer]
        if record.confidence < spec.min_write_confidence:
            raise ValueError(
                f"Record confidence {record.confidence:.2f} is below {record.layer} write threshold "
                f"{spec.min_write_confidence:.2f}"
            )
        return self.backends[record.layer].put(record)

    def put_frame(self, frame: MV2ContextFrame) -> str:
        spec = self.specs[frame.layer]
        if frame.confidence < spec.min_write_confidence:
            raise ValueError(
                f"Frame confidence {frame.confidence:.2f} is below {frame.layer} write threshold "
                f"{spec.min_write_confidence:.2f}"
            )
        backend = self.backends[frame.layer]
        put_frame = getattr(backend, "put_frame", None)
        if callable(put_frame):
            result = put_frame(frame)
            return str(result)
        return backend.put(to_memory_record(frame))

    def retrieve(self, query: RetrievalQuery) -> list[MemoryHit]:
        hits: list[MemoryHit] = []
        for layer in query.layers:
            spec = self.specs[layer]
            k = min(query.k_per_layer, spec.retrieval_k)
            hits.extend(
                self.backends[layer].find(
                    query=query.query,
                    k=k,
                    mode=query.mode if query.mode != "auto" else spec.search_mode,
                    min_relevancy=query.min_relevancy,
                )
            )
        return sorted(hits, key=lambda hit: (hit.score, hit.record.importance), reverse=True)

    def seal_all(self) -> None:
        for backend in self.backends.values():
            backend.seal()

    def verify_all(self) -> dict[MemoryLayer, bool]:
        return {layer: backend.verify() for layer, backend in self.backends.items()}

    def close_all(self) -> None:
        for backend in self.backends.values():
            backend.close()

    def iter_layers(self) -> Iterable[tuple[MemoryLayer, LayerSpec]]:
        return self.specs.items()
