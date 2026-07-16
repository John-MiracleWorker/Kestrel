from __future__ import annotations

import json
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from hashlib import sha256
from pathlib import Path

from .backends.base import MemoryBackend
from .context_frames import MV2ContextFrame, make_conflict_set_frame, to_memory_record
from .models import MemoryHit, MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from .promotion_ledger import PromotionEntry, PromotionLedger, make_outcome
from .security_boundary import sanitize_memory_record
from .vector_sidecar import TextEmbedder, VectorSidecar, VectorSidecarStatus, make_local_embedder


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
    provisional_threshold: float | None = None
    search_mode: str = "auto"
    vector_search_enabled: bool = False
    vector_embedding_provider: str | None = None
    vector_embedding_model: str | None = None
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
        provisional_threshold=None,
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
        provisional_threshold=None,
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
        provisional_threshold=None,
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
        provisional_threshold=None,
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
        provisional_threshold=None,
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
        provisional_threshold=None,
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
        ledger: PromotionLedger | None = None,
        vector_sidecars: dict[MemoryLayer, VectorSidecar] | None = None,
    ) -> None:
        self.specs = specs or DEFAULT_LAYER_SPECS
        missing = set(self.specs) - set(backends)
        if missing:
            missing_names = ", ".join(layer.value for layer in sorted(missing, key=str))
            raise ValueError(f"Missing backends for layers: {missing_names}")
        self.backends = backends
        self.ledger = ledger
        self.vector_sidecars = vector_sidecars or {}
        self._writes_since_seal = 0
        self._dirty_layers: set[MemoryLayer] = set()
        self._last_seal_monotonic = time.monotonic()

    @classmethod
    def from_backend_factory(
        cls,
        memory_dir: Path,
        backend_factory: type[MemoryBackend],
        specs: dict[MemoryLayer, LayerSpec] | None = None,
        ledger: PromotionLedger | None = None,
        vector_embedder: TextEmbedder | None = None,
        **backend_kwargs: object,
    ) -> LayeredMemorySystem:
        layer_specs = specs or DEFAULT_LAYER_SPECS
        memory_dir.mkdir(parents=True, exist_ok=True)
        backends: dict[MemoryLayer, MemoryBackend] = {}
        vector_sidecars: dict[MemoryLayer, VectorSidecar] = {}
        try:
            for layer, spec in layer_specs.items():
                path = memory_dir / spec.mv2_file
                layer_backend_kwargs = dict(backend_kwargs)
                backend = backend_factory(path=path, layer=layer, **layer_backend_kwargs)
                backends[layer] = backend
                backend.open()
                sidecar = _make_vector_sidecar(
                    memory_dir=memory_dir,
                    mv2_path=path,
                    spec=spec,
                    embedder=vector_embedder,
                )
                if sidecar is not None:
                    vector_sidecars[layer] = sidecar
                    sidecar.open()
        except Exception:
            for sidecar in reversed(tuple(vector_sidecars.values())):
                try:
                    sidecar.close()
                except Exception:
                    pass
            for backend in reversed(tuple(backends.values())):
                try:
                    backend.close()
                except Exception:
                    pass
            raise
        return cls(backends=backends, specs=layer_specs, ledger=ledger, vector_sidecars=vector_sidecars)

    def put(self, record: MemoryRecord) -> str:
        record = sanitize_memory_record(record)
        record, conflict_frame, conflicts = self._with_conflict_metadata(record)
        spec = self.specs[record.layer]
        if record.confidence < spec.min_write_confidence:
            raise ValueError(
                f"Record confidence {record.confidence:.2f} is below {record.layer} write threshold "
                f"{spec.min_write_confidence:.2f}"
            )
        confirmed_twin = self._confirmed_record_matches_provisional(record)
        if confirmed_twin is not None:
            self._record_promotion(record, record_id=confirmed_twin.id)
            entry = _promotion_entry_from_record(record, record_id=confirmed_twin.id)
            if entry is not None:
                self.confirm_provisional(confirmed_twin.id, entry)
            return confirmed_twin.id
        record_id = self.backends[record.layer].put(record)
        self._note_write(record.layer)
        self._update_vector_sidecar(record)
        self._record_promotion(record, record_id=record_id)
        self._record_conflict_outcomes(record, conflicts)
        if conflict_frame is not None:
            conflict_frame.confidence = max(conflict_frame.confidence, spec.min_write_confidence)
            conflict_record = to_memory_record(conflict_frame)
            self.backends[conflict_frame.layer].put(conflict_record)
            self._note_write(conflict_frame.layer)
            self._update_vector_sidecar(conflict_record)
        return record_id

    def upsert(self, record: MemoryRecord) -> str:
        record = sanitize_memory_record(record)
        record, conflict_frame, conflicts = self._with_conflict_metadata(record)
        spec = self.specs[record.layer]
        if record.confidence < spec.min_write_confidence:
            raise ValueError(
                f"Record confidence {record.confidence:.2f} is below {record.layer} write threshold "
                f"{spec.min_write_confidence:.2f}"
            )
        confirmed_twin = self._confirmed_record_matches_provisional(record)
        if confirmed_twin is not None and confirmed_twin.id != record.id:
            self._record_promotion(record, record_id=confirmed_twin.id)
            entry = _promotion_entry_from_record(record, record_id=confirmed_twin.id)
            if entry is not None:
                self.confirm_provisional(confirmed_twin.id, entry)
            return confirmed_twin.id
        record_id = self.backends[record.layer].upsert(record)
        self._note_write(record.layer)
        self._update_vector_sidecar(record)
        self._record_promotion(record, record_id=record_id)
        self._record_conflict_outcomes(record, conflicts)
        if conflict_frame is not None:
            conflict_frame.confidence = max(conflict_frame.confidence, spec.min_write_confidence)
            conflict_record = to_memory_record(conflict_frame)
            self.backends[conflict_frame.layer].upsert(conflict_record)
            self._note_write(conflict_frame.layer)
            self._update_vector_sidecar(conflict_record)
        return record_id

    def tombstone(
        self,
        layer: MemoryLayer,
        record_id: str,
        *,
        reason: str,
        superseded_by: str | None = None,
    ) -> bool:
        record = self.backends[layer].get_record(record_id, include_inactive=True)
        changed = self.backends[layer].tombstone(record_id, reason=reason, superseded_by=superseded_by)
        if changed:
            self._note_write(layer)
            self._tombstone_vector_sidecar(layer, record_id)
            promotion_id = None if record is None else _promotion_id(record)
            if promotion_id:
                if reason == "corrected":
                    self.record_promotion_outcome(
                        promotion_id,
                        "corrected",
                        evidence_record_id=superseded_by,
                        notes="correction frame superseded this promoted record",
                    )
                elif superseded_by:
                    self.record_promotion_outcome(
                        promotion_id,
                        "superseded",
                        evidence_record_id=superseded_by,
                        notes=f"record superseded during tombstone: {reason}",
                    )
                self.record_promotion_outcome(
                    promotion_id,
                    "tombstoned",
                    evidence_record_id=superseded_by,
                    notes=reason,
                )
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
                self._find_layer_hits(
                    layer=layer,
                    query=query.query,
                    k=k,
                    mode=_resolved_search_mode(spec, query.mode),
                    min_relevancy=query.min_relevancy,
                    include_inactive=query.include_inactive,
                )
            )
        ordered = sorted(hits, key=lambda hit: (hit.score, hit.record.importance), reverse=True)
        self._write_back_retrieval_hits(ordered)
        return ordered

    def record_promotion_outcome(
        self,
        promotion_id: str,
        outcome: str,
        *,
        evidence_record_id: str | None = None,
        notes: str = "",
    ) -> None:
        if self.ledger is None:
            return
        self.ledger.record_outcome(
            make_outcome(
                promotion_id,
                outcome,  # type: ignore[arg-type]
                evidence_record_id=evidence_record_id,
                notes=notes,
            )
        )

    def confirm_provisional(self, record_id: str, evidence: PromotionEntry) -> bool:
        record = self.get_record(None, record_id, include_inactive=True)
        if record is None or record.metadata.get("promotion_status") != "provisional":
            return False
        record.metadata["promotion_status"] = "confirmed"
        record.metadata["confirmed_at"] = datetime.now(UTC).isoformat()
        record.metadata["confirmation_promotion_id"] = evidence.promotion_id
        record.metadata["confirmation_evidence_record_id"] = evidence.record_id
        record.expires_at = None
        record.updated_at = datetime.now(UTC)
        self.backends[record.layer].upsert(record)
        self._note_write(record.layer)
        old_promotion_id = _promotion_id(record)
        if old_promotion_id:
            self.record_promotion_outcome(
                old_promotion_id,
                "useful",
                evidence_record_id=evidence.record_id,
                notes="provisional record confirmed by later full-threshold evidence",
            )
        return True

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
        first_error: Exception | None = None
        try:
            self.maybe_seal_all(force=True)
        except Exception as exc:
            first_error = exc
        for backend in self.backends.values():
            try:
                backend.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        for sidecar in self.vector_sidecars.values():
            try:
                sidecar.close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def iter_layers(self) -> Iterable[tuple[MemoryLayer, LayerSpec]]:
        return self.specs.items()

    def vector_index_status(self) -> dict[MemoryLayer, VectorSidecarStatus]:
        statuses: dict[MemoryLayer, VectorSidecarStatus] = {}
        for layer in self.specs:
            sidecar = self.vector_sidecars.get(layer)
            if sidecar is None:
                reason = "policy memory is lexical-only" if layer == MemoryLayer.POLICY else "vector sidecar not configured"
                statuses[layer] = VectorSidecarStatus.disabled(layer, reason)
                continue
            statuses[layer] = sidecar.status(records=tuple(self.backends[layer].iter_records(include_inactive=True)))
        return statuses

    def rebuild_vector_indexes(
        self,
        layers: tuple[MemoryLayer, ...] | None = None,
    ) -> dict[MemoryLayer, VectorSidecarStatus]:
        selected_layers = layers or tuple(self.vector_sidecars)
        rebuilt: dict[MemoryLayer, VectorSidecarStatus] = {}
        for layer in selected_layers:
            sidecar = self.vector_sidecars.get(layer)
            if sidecar is None:
                rebuilt[layer] = VectorSidecarStatus.disabled(layer, "vector sidecar not configured")
                continue
            rebuilt[layer] = sidecar.rebuild(tuple(self.backends[layer].iter_records(include_inactive=True)))
        return rebuilt

    def _with_conflict_metadata(self, record: MemoryRecord) -> tuple[MemoryRecord, MV2ContextFrame | None, list[MemoryRecord]]:
        if not _eligible_for_conflict_detection(record):
            return record, None, []
        conflicts = [
            existing
            for existing in self.iter_records(record.layer)
            if existing.id != record.id and _records_conflict(existing, record)
        ]
        if not conflicts:
            return record, None, []
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
        return record, conflict_frame, conflicts

    def _note_write(self, layer: MemoryLayer) -> None:
        self._writes_since_seal += 1
        self._dirty_layers.add(layer)

    def _update_vector_sidecar(self, record: MemoryRecord) -> None:
        sidecar = self.vector_sidecars.get(record.layer)
        if sidecar is not None:
            sidecar.upsert(record)

    def _tombstone_vector_sidecar(self, layer: MemoryLayer, record_id: str) -> None:
        sidecar = self.vector_sidecars.get(layer)
        if sidecar is not None:
            sidecar.tombstone(record_id)

    def _find_layer_hits(
        self,
        *,
        layer: MemoryLayer,
        query: str,
        k: int,
        mode: str,
        min_relevancy: float,
        include_inactive: bool,
    ) -> list[MemoryHit]:
        sidecar = self.vector_sidecars.get(layer)
        if mode in {"vec", "vector"}:
            return self._find_vector_sidecar_hits(
                layer=layer,
                query=query,
                k=k,
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
            )
        if mode == "hybrid" and sidecar is not None:
            lexical_hits = self.backends[layer].find(
                query=query,
                k=k,
                mode="lex",
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
            )
            vector_hits = self._find_vector_sidecar_hits(
                layer=layer,
                query=query,
                k=k,
                min_relevancy=min_relevancy,
                include_inactive=include_inactive,
            )
            return _fuse_memory_hits(lexical_hits, vector_hits, k=k)
        backend_mode = "lex" if mode == "hybrid" else mode
        return self.backends[layer].find(
            query=query,
            k=k,
            mode=backend_mode,
            min_relevancy=min_relevancy,
            include_inactive=include_inactive,
        )

    def _find_vector_sidecar_hits(
        self,
        *,
        layer: MemoryLayer,
        query: str,
        k: int,
        min_relevancy: float,
        include_inactive: bool,
    ) -> list[MemoryHit]:
        sidecar = self.vector_sidecars.get(layer)
        if sidecar is None:
            return []
        hits: list[MemoryHit] = []
        for vector_hit in sidecar.search(query, k=k, min_score=min_relevancy, include_inactive=include_inactive):
            record = self.backends[layer].get_record(vector_hit.record_id, include_inactive=include_inactive)
            if record is None:
                continue
            hits.append(
                MemoryHit(
                    record=record,
                    score=vector_hit.score,
                    source_backend="vector_sidecar",
                    frame_id=record.id,
                    snippet=record.content[:220],
                )
            )
        return hits

    def _requires_eager_seal(self) -> bool:
        return any(backend.__class__.__name__ == "InMemoryBackend" for backend in self.backends.values())

    def _record_promotion(self, record: MemoryRecord, *, record_id: str) -> None:
        if self.ledger is None:
            return
        entry = _promotion_entry_from_record(record, record_id=record_id)
        if entry is not None:
            self.ledger.record_promotion(entry)

    def _record_conflict_outcomes(self, record: MemoryRecord, conflicts: list[MemoryRecord]) -> None:
        if not conflicts:
            return
        for existing in conflicts:
            promotion_id = _promotion_id(existing)
            if not promotion_id:
                continue
            self.record_promotion_outcome(
                promotion_id,
                "contradicted",
                evidence_record_id=record.id,
                notes="deterministic conflict detection after memory write",
            )

    def _write_back_retrieval_hits(self, hits: list[MemoryHit]) -> None:
        now = datetime.now(UTC)
        for hit in hits:
            record = hit.record
            previous = _metadata_datetime(record.metadata.get("last_retrieved_at"))
            if previous is not None and (now - previous).total_seconds() < 3600:
                continue
            record.metadata["last_retrieved_at"] = now.isoformat()
            record.updated_at = now
            self.backends[record.layer].upsert(record)
            self._note_write(record.layer)

    def _confirmed_record_matches_provisional(self, record: MemoryRecord) -> MemoryRecord | None:
        if record.metadata.get("promotion_status", "confirmed") != "confirmed":
            return None
        if record.metadata.get("active", True) is False:
            return None
        for existing in self.iter_records(record.layer):
            if existing.id == record.id:
                continue
            if existing.metadata.get("promotion_status") != "provisional":
                continue
            if _conceptually_same_record(existing, record):
                return existing
        return None


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
        embedding_model = _optional_str(vector.get("embedding_model", payload.get("vector_embedding_model")))
        index_path = _optional_str(vector.get("index_path", payload.get("vector_index_path")))
        local_vector_enabled = bool(vector_enabled and provider == "local" and index_path)
        search_mode = str(payload.get("search_mode", base.search_mode))
        if layer == MemoryLayer.PROCEDURAL and local_vector_enabled and "search_mode" not in payload:
            search_mode = "hybrid"
        if layer == MemoryLayer.POLICY or not local_vector_enabled:
            search_mode = "lex" if layer == MemoryLayer.POLICY else ("lex" if search_mode in {"vec", "vector", "hybrid"} else search_mode)
            local_vector_enabled = False
        provisional_threshold_raw = payload.get("provisional_threshold", base.provisional_threshold)
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
            provisional_threshold=None if provisional_threshold_raw is None else float(provisional_threshold_raw),
            search_mode=search_mode,
            vector_search_enabled=local_vector_enabled,
            vector_embedding_provider=provider if local_vector_enabled else None,
            vector_embedding_model=embedding_model if local_vector_enabled else None,
            vector_index_path=index_path if local_vector_enabled else None,
            hybrid_search_enabled=local_vector_enabled and search_mode == "hybrid",
        )
    return specs


def _make_vector_sidecar(
    *,
    memory_dir: Path,
    mv2_path: Path,
    spec: LayerSpec,
    embedder: TextEmbedder | None,
) -> VectorSidecar | None:
    if spec.layer == MemoryLayer.POLICY:
        return None
    if not spec.vector_search_enabled or spec.vector_embedding_provider != "local" or not spec.vector_index_path:
        return None
    index_path = Path(spec.vector_index_path)
    if not index_path.is_absolute():
        index_path = memory_dir / index_path
    return VectorSidecar(
        path=index_path,
        layer=spec.layer,
        embedder=embedder or make_local_embedder(spec.vector_embedding_model),
        mv2_path=mv2_path,
        provider=spec.vector_embedding_provider,
    )


def _resolved_search_mode(spec: LayerSpec, requested_mode: str) -> str:
    mode = requested_mode if requested_mode != "auto" else spec.search_mode
    if spec.layer == MemoryLayer.POLICY:
        return "lex"
    if mode in {"vec", "vector", "hybrid"} and not spec.vector_search_enabled:
        return "lex"
    return mode


def _fuse_memory_hits(lexical_hits: list[MemoryHit], vector_hits: list[MemoryHit], *, k: int) -> list[MemoryHit]:
    by_id: dict[str, MemoryHit] = {}
    for hit in lexical_hits:
        by_id[hit.record.id] = hit
    for hit in vector_hits:
        existing = by_id.get(hit.record.id)
        if existing is None or hit.score > existing.score:
            by_id[hit.record.id] = hit
    return sorted(by_id.values(), key=lambda hit: (hit.score, hit.record.importance), reverse=True)[:k]


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


def _promotion_id(record: MemoryRecord) -> str | None:
    value = record.metadata.get("promotion_id")
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _promotion_entry_from_record(record: MemoryRecord, *, record_id: str) -> PromotionEntry | None:
    promotion_id = _promotion_id(record)
    nested = record.metadata.get("nested_learning")
    if not promotion_id or not isinstance(nested, dict):
        return None
    decision = nested.get("decision")
    flow = nested.get("context_flow")
    optimizer_trace = nested.get("optimizer_trace")
    if not isinstance(decision, dict) or not isinstance(flow, dict):
        return None
    source_layer_value = record.metadata.get("source_layer")
    if not source_layer_value:
        source_layers = flow.get("source_layers")
        if isinstance(source_layers, list) and source_layers:
            source_layer_value = source_layers[0]
    try:
        source_layer = MemoryLayer(str(source_layer_value or record.layer.value))
    except ValueError:
        source_layer = record.layer
    return PromotionEntry(
        promotion_id=promotion_id,
        record_id=record_id,
        source_layer=source_layer,
        target_layer=record.layer,
        decision_reason=str(decision.get("reason") or ""),
        validation_score=float(record.metadata.get("validation_score", 0.0)),
        repeat_count=int(record.metadata.get("repeat_count", 1)),
        explicit_instruction=bool(record.metadata.get("explicit_instruction", False)),
        optimizer_trace=dict(optimizer_trace) if isinstance(optimizer_trace, dict) else {},
        promoted_at=record.created_at.isoformat(),
    )


def _metadata_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _conceptually_same_record(left: MemoryRecord, right: MemoryRecord) -> bool:
    left_category = str(left.metadata.get("failure_category") or left.tags.get("failure_category") or "")
    right_category = str(right.metadata.get("failure_category") or right.tags.get("failure_category") or "")
    if left_category or right_category:
        if left_category != right_category:
            return False
        return SequenceMatcher(None, _normalize_content(left.content), _normalize_content(right.content)).ratio() >= 0.60
    if _normalize_claim_key(left.title) != _normalize_claim_key(right.title):
        return False
    return SequenceMatcher(None, _normalize_content(left.content), _normalize_content(right.content)).ratio() >= 0.65


def _normalize_content(text: str) -> str:
    return " ".join(re.findall(r"[a-zA-Z0-9_]+", text.lower()))
