from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path

from .backends.base import MemoryBackend
from .context_frames import MV2ContextFrame, make_conflict_set_frame, to_memory_record
from .models import MemoryHit, MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery


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
    min_repeat_count_for_promotion: int
    retention_days: int
    search_mode: str = "auto"
    vector_search_enabled: bool = False
    vector_embedding_provider: str | None = None
    vector_index_path: str | None = None
    hybrid_search_enabled: bool = False


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
        min_repeat_count_for_promotion=1,
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
        promotion_threshold=0.65,
        min_repeat_count_for_promotion=1,
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
        promotion_threshold=0.78,
        min_repeat_count_for_promotion=1,
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
        promotion_threshold=0.78,
        min_repeat_count_for_promotion=2,
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
        promotion_threshold=0.78,
        min_repeat_count_for_promotion=1,
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
        min_repeat_count_for_promotion=5,
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
        self._writes_since_seal = 0
        self._dirty_layers: set[MemoryLayer] = set()
        self._last_seal_monotonic = time.monotonic()

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
            layer_backend_kwargs = dict(backend_kwargs)
            if spec.vector_search_enabled and spec.vector_embedding_provider == "local":
                layer_backend_kwargs.setdefault("enable_vec", True)
            backend = backend_factory(path=path, layer=layer, **layer_backend_kwargs)
            backend.open()
            backends[layer] = backend
        return cls(backends=backends, specs=layer_specs)

    def put(self, record: MemoryRecord) -> str:
        record, conflict_frame = self._with_conflict_metadata(record)
        spec = self.specs[record.layer]
        if record.confidence < spec.min_write_confidence:
            raise ValueError(
                f"Record confidence {record.confidence:.2f} is below {record.layer} write threshold "
                f"{spec.min_write_confidence:.2f}"
            )
        record_id = self.backends[record.layer].put(record)
        self._note_write(record.layer)
        if conflict_frame is not None:
            conflict_frame.confidence = max(conflict_frame.confidence, spec.min_write_confidence)
            self.backends[conflict_frame.layer].put(to_memory_record(conflict_frame))
            self._note_write(conflict_frame.layer)
        return record_id

    def upsert(self, record: MemoryRecord) -> str:
        record, conflict_frame = self._with_conflict_metadata(record)
        spec = self.specs[record.layer]
        if record.confidence < spec.min_write_confidence:
            raise ValueError(
                f"Record confidence {record.confidence:.2f} is below {record.layer} write threshold "
                f"{spec.min_write_confidence:.2f}"
            )
        record_id = self.backends[record.layer].upsert(record)
        self._note_write(record.layer)
        if conflict_frame is not None:
            conflict_frame.confidence = max(conflict_frame.confidence, spec.min_write_confidence)
            self.backends[conflict_frame.layer].upsert(to_memory_record(conflict_frame))
            self._note_write(conflict_frame.layer)
        return record_id

    def tombstone(
        self,
        layer: MemoryLayer,
        record_id: str,
        *,
        reason: str,
        superseded_by: str | None = None,
    ) -> bool:
        changed = self.backends[layer].tombstone(record_id, reason=reason, superseded_by=superseded_by)
        if changed:
            self._note_write(layer)
        return changed

    def iter_records(self, layer: MemoryLayer | None = None, *, include_inactive: bool = False) -> Iterable[MemoryRecord]:
        layers = (layer,) if layer is not None else tuple(self.backends)
        for selected in layers:
            yield from self.backends[selected].iter_records(include_inactive=include_inactive)

    def get_record(
        self,
        layer: MemoryLayer | None,
        record_id: str,
        *,
        include_inactive: bool = True,
    ) -> MemoryRecord | None:
        layers = (layer,) if layer is not None else tuple(self.backends)
        for selected in layers:
            record = self.backends[selected].get_record(record_id, include_inactive=include_inactive)
            if record is not None:
                return record
        return None

    def put_frame(self, frame: MV2ContextFrame) -> str:
        return self.put(to_memory_record(frame))

    def retrieve(self, query: RetrievalQuery) -> list[MemoryHit]:
        hits: list[MemoryHit] = []
        for layer in query.layers:
            spec = self.specs[layer]
            k = min(query.k_per_layer, spec.retrieval_k)
            hits.extend(
                self.backends[layer].find(
                    query=query.query,
                    k=k,
                    mode=_resolved_search_mode(spec, query.mode),
                    min_relevancy=query.min_relevancy,
                    include_inactive=query.include_inactive,
                )
            )
        return sorted(hits, key=lambda hit: (hit.score, hit.record.importance), reverse=True)

    def seal_all(self) -> None:
        for backend in self.backends.values():
            backend.seal()
        self._writes_since_seal = 0
        self._dirty_layers.clear()
        self._last_seal_monotonic = time.monotonic()

    def maybe_seal_all(
        self,
        *,
        write_threshold: int = 50,
        interval_seconds: float = 10.0,
        force: bool = False,
    ) -> bool:
        if force or self._requires_eager_seal():
            self.seal_all()
            return True
        if not self._dirty_layers:
            return False
        durable_layers = {MemoryLayer.SEMANTIC, MemoryLayer.PROCEDURAL, MemoryLayer.SELF, MemoryLayer.POLICY}
        if self._dirty_layers & durable_layers:
            self.seal_all()
            return True
        elapsed = time.monotonic() - self._last_seal_monotonic
        if self._writes_since_seal >= max(write_threshold, 1) or elapsed >= max(interval_seconds, 0.001):
            self.seal_all()
            return True
        return False

    def verify_all(self) -> dict[MemoryLayer, bool]:
        return {layer: backend.verify() for layer, backend in self.backends.items()}

    def close_all(self) -> None:
        self.maybe_seal_all(force=True)
        for backend in self.backends.values():
            backend.close()

    def iter_layers(self) -> Iterable[tuple[MemoryLayer, LayerSpec]]:
        return self.specs.items()

    def _with_conflict_metadata(self, record: MemoryRecord) -> tuple[MemoryRecord, MV2ContextFrame | None]:
        if not _eligible_for_conflict_detection(record):
            return record, None
        conflicts = [
            existing
            for existing in self.iter_records(record.layer)
            if existing.id != record.id and _records_conflict(existing, record)
        ]
        if not conflicts:
            return record, None
        group_id = _conflict_group_id(record, conflicts)
        record.metadata["conflict_group_id"] = group_id
        member_ids = tuple(existing.id for existing in conflicts) + (record.id,)
        for existing in conflicts:
            if existing.metadata.get("conflict_group_id") == group_id:
                continue
            existing.metadata["conflict_group_id"] = group_id
            self.backends[existing.layer].upsert(existing)
        conflict_frame = make_conflict_set_frame(
            layer=record.layer,
            conflict_group_id=group_id,
            member_ids=member_ids,
            reason="deterministic conflict detection after memory write",
        )
        return record, conflict_frame

    def _note_write(self, layer: MemoryLayer) -> None:
        self._writes_since_seal += 1
        self._dirty_layers.add(layer)

    def _requires_eager_seal(self) -> bool:
        return any(backend.__class__.__name__ == "InMemoryBackend" for backend in self.backends.values())


def load_layer_specs(path: Path) -> dict[MemoryLayer, LayerSpec]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Layer spec config must be a JSON object")
    specs = dict(DEFAULT_LAYER_SPECS)
    for layer_name, payload in raw.items():
        layer = MemoryLayer(str(layer_name))
        if not isinstance(payload, dict):
            raise ValueError(f"Layer spec for {layer.value} must be an object")
        base = specs[layer]
        vector_payload = payload.get("vector")
        vector: dict[str, object] = vector_payload if isinstance(vector_payload, dict) else {}
        vector_enabled = bool(vector.get("enabled", payload.get("vector_search_enabled", False)))
        provider = _optional_str(vector.get("embedding_provider", payload.get("vector_embedding_provider")))
        index_path = _optional_str(vector.get("index_path", payload.get("vector_index_path")))
        local_vector_enabled = bool(vector_enabled and provider == "local" and index_path)
        search_mode = str(payload.get("search_mode", base.search_mode))
        if layer == MemoryLayer.POLICY or not local_vector_enabled:
            search_mode = "lex" if layer == MemoryLayer.POLICY else ("lex" if search_mode in {"vec", "vector", "hybrid"} else search_mode)
            local_vector_enabled = False
        specs[layer] = LayerSpec(
            layer=layer,
            description=str(payload.get("description", base.description)),
            mv2_file=str(payload.get("mv2_file", base.mv2_file)),
            update_cadence=str(payload.get("update_cadence", base.update_cadence)),
            retrieval_k=int(payload.get("retrieval_k", base.retrieval_k)),
            context_budget_chars=int(payload.get("context_budget_chars", base.context_budget_chars)),
            min_write_confidence=float(payload.get("min_write_confidence", base.min_write_confidence)),
            promotion_threshold=float(payload.get("promotion_threshold", base.promotion_threshold)),
            min_repeat_count_for_promotion=int(
                payload.get("min_repeat_count_for_promotion", base.min_repeat_count_for_promotion)
            ),
            retention_days=int(payload.get("retention_days", base.retention_days)),
            search_mode=search_mode,
            vector_search_enabled=local_vector_enabled,
            vector_embedding_provider=provider if local_vector_enabled else None,
            vector_index_path=index_path if local_vector_enabled else None,
            hybrid_search_enabled=local_vector_enabled and search_mode == "hybrid",
        )
    return specs


def _resolved_search_mode(spec: LayerSpec, requested_mode: str) -> str:
    mode = requested_mode if requested_mode != "auto" else spec.search_mode
    if spec.layer == MemoryLayer.POLICY:
        return "lex"
    if mode in {"vec", "vector", "hybrid"} and not spec.vector_search_enabled:
        return "lex"
    return mode


def _eligible_for_conflict_detection(record: MemoryRecord) -> bool:
    if record.metadata.get("active", True) is False:
        return False
    if record.metadata.get("frame_type") in {"correction", "conflict_set"}:
        return False
    if record.layer not in {MemoryLayer.SEMANTIC, MemoryLayer.PROCEDURAL, MemoryLayer.SELF}:
        return False
    return record.kind in {MemoryKind.FACT, MemoryKind.PROCEDURE}


def _records_conflict(left: MemoryRecord, right: MemoryRecord) -> bool:
    if left.kind == MemoryKind.FACT and right.kind == MemoryKind.FACT:
        return _normalize_claim_key(left.title) == _normalize_claim_key(right.title) and _polarity(left.content) != _polarity(right.content)
    if left.kind == MemoryKind.PROCEDURE and right.kind == MemoryKind.PROCEDURE:
        left_category = str(left.metadata.get("failure_category") or left.tags.get("failure_category") or "")
        right_category = str(right.metadata.get("failure_category") or right.tags.get("failure_category") or "")
        if left_category and left_category == right_category:
            return SequenceMatcher(None, left.content, right.content).ratio() >= 0.85 and left.content != right.content
    return False


def _conflict_group_id(record: MemoryRecord, conflicts: list[MemoryRecord]) -> str:
    for existing in conflicts:
        group_id = existing.metadata.get("conflict_group_id")
        if group_id:
            return str(group_id)
    key = "|".join(sorted([record.id, *(item.id for item in conflicts)]))
    return f"conflict_{sha256(key.encode('utf-8')).hexdigest()[:16]}"


def _normalize_claim_key(title: str) -> str:
    return " ".join(re.findall(r"[a-zA-Z0-9_]+", title.lower()))[:80] or "untitled"


def _polarity(text: str) -> str:
    lowered = text.lower()
    negative_markers = (" not ", " never ", " no longer ", " incorrect", " false", " avoid ", " do not ")
    return "negative" if any(marker in f" {lowered} " for marker in negative_markers) else "positive"


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    stripped = str(value).strip()
    return stripped or None
