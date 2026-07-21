from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock, RLock
from typing import IO, Any, cast

import numpy as np

from ..context_frames import MV2ContextFrame, to_memory_record
from ..file_lock import lock_exclusive, lock_shared, unlock
from ..models import EvidenceRef, MemoryHit, MemoryKind, MemoryLayer, MemoryRecord
from ..private_artifacts import (
    ensure_private_directory,
    harden_memory_artifact_files,
    open_private_file_descriptor,
    read_private_text,
    write_private_text,
)
from .base import MemoryBackend

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+")

# BM25 hyperparameters (standard Okapi BM25)
_K1 = 1.2
_B = 0.75

# RRF constant — standard value from literature
_RRF_K = 60

# Normalization heuristics
_BM25_SCORE_CAP = 10.0

_EMBEDDING_MODEL_CACHE: dict[str, Any] = {}
_EMBEDDING_MODEL_LOCK = Lock()


def _get_embedding_model(model_name: str = "all-MiniLM-L6-v2") -> Any:
    """Lazy singleton for the sentence-transformers embedding model."""

    with _EMBEDDING_MODEL_LOCK:
        if model_name not in _EMBEDDING_MODEL_CACHE:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise RuntimeError("sentence-transformers is required for vector search") from exc
            _EMBEDDING_MODEL_CACHE[model_name] = SentenceTransformer(model_name)
        return _EMBEDDING_MODEL_CACHE[model_name]


class _BM25Index:
    """In-memory Okapi BM25 index for a single layer."""

    def __init__(self) -> None:
        self._doc_tokens: list[list[str]] = []
        self._doc_ids: list[str] = []
        self._doc_lens: list[int] = []
        self._avg_doc_len: float = 0.0
        self._df: dict[str, int] = {}
        self._total_docs: int = 0

    def add(self, doc_id: str, tokens: list[str]) -> None:
        self._doc_ids.append(doc_id)
        self._doc_tokens.append(tokens)
        self._doc_lens.append(len(tokens))
        self._total_docs += 1

        seen = set()
        for token in tokens:
            if token not in seen:
                self._df[token] = self._df.get(token, 0) + 1
                seen.add(token)

        self._avg_doc_len = sum(self._doc_lens) / self._total_docs if self._total_docs > 0 else 0.0

    def remove(self, doc_id: str) -> None:
        try:
            idx = self._doc_ids.index(doc_id)
        except ValueError:
            return

        tokens = self._doc_tokens[idx]
        seen = set(tokens)
        for token in seen:
            self._df[token] = max(0, self._df.get(token, 0) - 1)
            if self._df[token] == 0:
                del self._df[token]

        del self._doc_ids[idx]
        del self._doc_tokens[idx]
        del self._doc_lens[idx]
        self._total_docs -= 1
        self._avg_doc_len = sum(self._doc_lens) / self._total_docs if self._total_docs > 0 else 0.0

    def _idf(self, token: str) -> float:
        df = self._df.get(token, 0)
        if df == 0:
            return 0.0
        return math.log((self._total_docs - df + 0.5) / (df + 0.5) + 1.0)

    def score(self, query_tokens: list[str], doc_idx: int) -> float:
        if self._avg_doc_len == 0:
            return 0.0
        doc_tokens = self._doc_tokens[doc_idx]
        doc_len = self._doc_lens[doc_idx]
        token_counts = Counter(doc_tokens)

        score = 0.0
        for token in query_tokens:
            f = token_counts.get(token, 0)
            if f == 0:
                continue
            idf = self._idf(token)
            denom = f + _K1 * (1 - _B + _B * (doc_len / self._avg_doc_len))
            score += idf * (f * (_K1 + 1)) / denom
        return score

    def search(self, query_tokens: list[str], k: int = 8) -> list[tuple[str, float]]:
        if not query_tokens or self._total_docs == 0 or k <= 0:
            return []
        scores: list[tuple[str, float]] = []
        for idx in range(self._total_docs):
            s = self.score(query_tokens, idx)
            if s > 0:
                scores.append((self._doc_ids[idx], s))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]


class _VectorIndex:
    """In-memory dense vector index with cosine similarity."""

    def __init__(self) -> None:
        self._doc_ids: list[str] = []
        self._vectors: list[np.ndarray] = []
        self._id_to_idx: dict[str, int] = {}

    def add(self, doc_id: str, vector: np.ndarray) -> None:
        if doc_id in self._id_to_idx:
            self.remove(doc_id)
        normed = vector / (np.linalg.norm(vector) + 1e-12)
        self._doc_ids.append(doc_id)
        self._vectors.append(normed)
        self._id_to_idx[doc_id] = len(self._doc_ids) - 1

    def remove(self, doc_id: str) -> None:
        if doc_id not in self._id_to_idx:
            return
        idx = self._id_to_idx[doc_id]
        del self._doc_ids[idx]
        del self._vectors[idx]
        del self._id_to_idx[doc_id]
        # Rebuild indices after idx
        for i, did in enumerate(self._doc_ids[idx:], start=idx):
            self._id_to_idx[did] = i

    def search(self, query_vector: np.ndarray, k: int = 8) -> list[tuple[str, float]]:
        if not self._vectors or k <= 0:
            return []
        effective_k = min(k, len(self._vectors))
        q_normed = query_vector / (np.linalg.norm(query_vector) + 1e-12)
        # Stack and compute dot product (cosine because both are normalized)
        matrix = np.stack(self._vectors)
        similarities = matrix @ q_normed
        top_k_idx = np.argpartition(similarities, -effective_k)[-effective_k:]
        top_k_idx = top_k_idx[np.argsort(similarities[top_k_idx])[::-1]]
        return [(self._doc_ids[i], float(similarities[i])) for i in top_k_idx if similarities[i] > 0]


class InMemoryBackend(MemoryBackend):
    """Deterministic backend for local tests and Codex-safe development.

    Supports optional dense vector search via sentence-transformers.
    """

    _global_records: dict[str, list[MemoryRecord]] = {}
    _global_versions: dict[str, int] = {}
    _global_locks: dict[str, RLock] = {}
    _global_locks_guard = Lock()

    def __init__(
        self,
        path: Path,
        layer: MemoryLayer,
        **kwargs: object,
    ) -> None:
        super().__init__(path, layer, **kwargs)
        self.records: list[MemoryRecord] = []
        self._bm25 = _BM25Index()

        self._enable_vec = bool(kwargs.get("enable_vec", False))
        self._embedding_model_name = str(kwargs.get("embedding_model", "all-MiniLM-L6-v2"))
        self._vector_index: _VectorIndex | None = _VectorIndex() if self._enable_vec else None
        self._vector_cache: dict[str, np.ndarray] = {}
        self._path_key = os.path.abspath(self.path)
        self._state_lock = self._lock_for_path(self._path_key)
        self._indexed_version = -1
        self._snapshot_path = self.path.with_suffix(".memory.json")
        self._snapshot_lock_path = self.path.parent / f".{self.path.name}.kestrel.lock"

    @classmethod
    def _lock_for_path(cls, key: str) -> RLock:
        with cls._global_locks_guard:
            return cls._global_locks.setdefault(key, RLock())

    def _text_for_record(self, record: MemoryRecord) -> str:
        return f"{record.title} {record.content} {' '.join(record.tags.values())}"

    def _encode(self, text: str) -> np.ndarray:
        model = _get_embedding_model(self._embedding_model_name)
        return cast(np.ndarray, model.encode(text, convert_to_numpy=True, normalize_embeddings=False))

    def _maybe_index_vector(self, record: MemoryRecord) -> None:
        if self._vector_index is None:
            return
        text = self._text_for_record(record)
        vec = self._encode(text)
        self._vector_index.add(record.id, vec)
        self._vector_cache[record.id] = vec

    def _maybe_remove_vector(self, record_id: str) -> None:
        if self._vector_index is None:
            return
        self._vector_index.remove(record_id)
        self._vector_cache.pop(record_id, None)

    def open(self) -> None:
        ensure_private_directory(self.path.parent)
        with self._state_lock, self._snapshot_file_lock(exclusive=False):
            harden_memory_artifact_files(self.path)
            disk_records = self._load_snapshot_records()
            shared_records = self._global_records.get(self._path_key)
            if shared_records is None:
                shared_records = disk_records or []
                self._global_records[self._path_key] = shared_records
                self._global_versions[self._path_key] = (
                    self._global_versions.get(self._path_key, 0) + 1
                )
            elif disk_records is not None:
                shared_records[:] = _merge_records(disk_records, shared_records)
                self._global_versions[self._path_key] = (
                    self._global_versions.get(self._path_key, 0) + 1
                )
            self.records = shared_records
            self._rebuild_indices()
            self._indexed_version = self._global_versions[self._path_key]

    @contextmanager
    def _snapshot_file_lock(self, *, exclusive: bool) -> Iterator[None]:
        descriptor = open_private_file_descriptor(self._snapshot_lock_path)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            typed_handle = cast(IO[str], handle)
            if exclusive:
                lock_exclusive(typed_handle)
            else:
                lock_shared(typed_handle)
            try:
                yield
            finally:
                unlock(typed_handle)

    def _load_snapshot_records(self) -> list[MemoryRecord] | None:
        raw = read_private_text(self._snapshot_path, missing_ok=True)
        if raw is None:
            return None
        loaded = json.loads(raw)
        if not isinstance(loaded, list):
            raise ValueError(f"Memory snapshot must contain a JSON array: {self._snapshot_path}")
        if any(not isinstance(item, dict) for item in loaded):
            raise ValueError(f"Memory snapshot records must be JSON objects: {self._snapshot_path}")
        return [_record_from_snapshot(item, self.layer) for item in loaded]

    def _mark_mutation(self, *, indices_current: bool) -> None:
        version = self._global_versions.get(self._path_key, 0) + 1
        self._global_versions[self._path_key] = version
        self._global_records[self._path_key] = self.records
        if indices_current:
            self._indexed_version = version

    def _sync_indices(self) -> None:
        version = self._global_versions.get(self._path_key, 0)
        if self._indexed_version == version:
            return
        self._rebuild_indices()
        self._indexed_version = version

    def _rebuild_indices(self) -> None:
        self._bm25 = _BM25Index()
        if self._vector_index is not None:
            self._vector_index = _VectorIndex()
            self._vector_cache = {}
        for record in self.records:
            text = self._text_for_record(record)
            tokens = _tokens(text)
            self._bm25.add(record.id, tokens)
            if self._vector_index is not None:
                vec = self._encode(text)
                self._vector_index.add(record.id, vec)
                self._vector_cache[record.id] = vec

    def put(self, record: MemoryRecord) -> str:
        if record.layer != self.layer:
            raise ValueError(f"Cannot write {record.layer} record to {self.layer} backend")
        with self._state_lock:
            self._sync_indices()
            self.records.append(record)
            text = self._text_for_record(record)
            tokens = _tokens(text)
            self._bm25.add(record.id, tokens)
            self._maybe_index_vector(record)
            self._mark_mutation(indices_current=True)
        return record.id

    def upsert(self, record: MemoryRecord) -> str:
        if record.layer != self.layer:
            raise ValueError(f"Cannot write {record.layer} record to {self.layer} backend")
        with self._state_lock:
            self._sync_indices()
            for index, existing in enumerate(self.records):
                if existing.id == record.id:
                    old_text = self._text_for_record(existing)
                    new_text = self._text_for_record(record)
                    if old_text != new_text:
                        self.records[index] = record
                        self._bm25.remove(record.id)
                        tokens = _tokens(new_text)
                        self._bm25.add(record.id, tokens)
                        self._maybe_remove_vector(record.id)
                        self._maybe_index_vector(record)
                    else:
                        self.records[index] = record
                    self._mark_mutation(indices_current=True)
                    return record.id
            self.records.append(record)
            text = self._text_for_record(record)
            tokens = _tokens(text)
            self._bm25.add(record.id, tokens)
            self._maybe_index_vector(record)
            self._mark_mutation(indices_current=True)
        return record.id

    def tombstone(self, record_id: str, *, reason: str, superseded_by: str | None = None) -> bool:
        with self._state_lock:
            record = self.get_record(record_id, include_inactive=True)
            if record is None:
                return False
            record.metadata["active"] = False
            record.metadata["tombstone_reason"] = reason
            record.metadata["tombstoned_at"] = datetime.now(UTC).isoformat()
            if superseded_by:
                record.metadata["superseded_by"] = superseded_by
            record.updated_at = datetime.now(UTC)
            self._mark_mutation(indices_current=True)
            return True

    def iter_records(self, *, include_inactive: bool = False) -> Iterable[MemoryRecord]:
        with self._state_lock:
            return tuple(record for record in self.records if include_inactive or _is_active(record))

    def get_record(self, record_id: str, *, include_inactive: bool = True) -> MemoryRecord | None:
        with self._state_lock:
            for record in self.records:
                metadata = record.metadata
                if record.id == record_id or str(metadata.get("frame_id", "")) == record_id:
                    if include_inactive or _is_active(record):
                        return record
                    return None
            return None

    def put_frame(self, frame: MV2ContextFrame) -> str:
        return self.put(to_memory_record(frame))

    def _find_lexical(self, query: str, k: int, min_relevancy: float, include_inactive: bool) -> list[MemoryHit]:
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        bm25_results = self._bm25.search(query_tokens, k=k * 2)
        hits: list[MemoryHit] = []
        query_token_set = set(query_tokens)
        for doc_id, score in bm25_results:
            record = self.get_record(doc_id, include_inactive=include_inactive)
            if record is None:
                continue
            normalized_score = min(score / _BM25_SCORE_CAP, 1.0)
            normalized_score = max(normalized_score, 0.0)
            if normalized_score >= min_relevancy:
                hits.append(
                    MemoryHit(
                        record=record,
                        score=normalized_score,
                        source_backend="memory",
                        frame_id=record.id,
                        snippet=_snippet(record.content, query_token_set),
                    )
                )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:k]

    def _find_vector(self, query: str, k: int, min_relevancy: float, include_inactive: bool) -> list[MemoryHit]:
        if self._vector_index is None or not self._vector_index._vectors:
            return []
        query_vec = self._encode(query)
        results = self._vector_index.search(query_vec, k=k * 2)
        hits: list[MemoryHit] = []
        for doc_id, score in results:
            record = self.get_record(doc_id, include_inactive=include_inactive)
            if record is None:
                continue
            if score >= min_relevancy:
                hits.append(
                    MemoryHit(
                        record=record,
                        score=min(score, 1.0),
                        source_backend="memory",
                        frame_id=record.id,
                        snippet="",
                    )
                )
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:k]

    def _find_hybrid(self, query: str, k: int, min_relevancy: float, include_inactive: bool) -> list[MemoryHit]:
        lex_hits = self._find_lexical(query, k=k * 2, min_relevancy=0.0, include_inactive=include_inactive)
        vec_hits = self._find_vector(query, k=k * 2, min_relevancy=0.0, include_inactive=include_inactive)

        # Build rank maps
        lex_ranks: dict[str, int] = {}
        for rank, hit in enumerate(lex_hits, start=1):
            lex_ranks[hit.record.id] = rank

        vec_ranks: dict[str, int] = {}
        for rank, hit in enumerate(vec_hits, start=1):
            vec_ranks[hit.record.id] = rank

        # RRF fusion
        fused_scores: dict[str, float] = {}
        all_ids = set(lex_ranks) | set(vec_ranks)
        for doc_id in all_ids:
            score = 0.0
            if doc_id in lex_ranks:
                score += 1.0 / (_RRF_K + lex_ranks[doc_id])
            if doc_id in vec_ranks:
                score += 1.0 / (_RRF_K + vec_ranks[doc_id])
            fused_scores[doc_id] = score

        # Sort by fused score desc
        sorted_ids = sorted(fused_scores, key=lambda did: fused_scores[did], reverse=True)

        hits: list[MemoryHit] = []
        query_token_set = set(_tokens(query))
        for doc_id in sorted_ids[:k]:
            record = self.get_record(doc_id, include_inactive=include_inactive)
            if record is None:
                continue
            rrf_score = fused_scores[doc_id]
            # Scale to 0-1 for compatibility
            normalized = min(rrf_score * 60, 1.0)  # heuristic scaling
            if normalized >= min_relevancy:
                hits.append(
                    MemoryHit(
                        record=record,
                        score=normalized,
                        source_backend="memory",
                        frame_id=record.id,
                        snippet=_snippet(record.content, query_token_set),
                    )
                )
        return hits

    def find(
        self,
        query: str,
        k: int = 8,
        mode: str = "auto",
        min_relevancy: float = 0.0,
        *,
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        with self._state_lock:
            self._sync_indices()
            if k <= 0:
                return []
            resolved = mode if mode != "auto" else ("hybrid" if self._vector_index is not None else "lex")
            if resolved == "hybrid" and self._vector_index is not None:
                return self._find_hybrid(query, k, min_relevancy, include_inactive)
            if resolved in {"vec", "vector"} and self._vector_index is not None:
                return self._find_vector(query, k, min_relevancy, include_inactive)
            return self._find_lexical(query, k, min_relevancy, include_inactive)

    def find_frames(
        self,
        query: str,
        k: int = 8,
        layers: tuple[MemoryLayer, ...] | None = None,
        frame_types: tuple[str, ...] | None = None,
        mode: str = "auto",
        include_inactive: bool = False,
    ) -> list[MemoryHit]:
        if layers is not None and self.layer not in layers:
            return []
        hits = self.find(query=query, k=k, mode=mode, include_inactive=include_inactive)
        if frame_types is None:
            return hits
        allowed = set(frame_types)
        return [hit for hit in hits if str(hit.record.metadata.get("frame_type", "raw_chunk")) in allowed]

    def seal(self) -> None:
        with self._state_lock, self._snapshot_file_lock(exclusive=True):
            harden_memory_artifact_files(self.path)
            disk_records = self._load_snapshot_records() or []
            self.records[:] = _merge_records(disk_records, self.records)
            self._mark_mutation(indices_current=False)
            self._sync_indices()
            write_private_text(
                self._snapshot_path,
                json.dumps(_snapshot_payload(self.records), indent=2),
                encoding="utf-8",
            )

    def verify(self) -> bool:
        with self._state_lock:
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


def _is_active(record: MemoryRecord) -> bool:
    return record.metadata.get("active", True) is not False


def _snapshot_payload(records: Iterable[MemoryRecord]) -> list[dict[str, object]]:
    return [
        {
            "id": record.id,
            "title": record.title,
            "layer": record.layer.value,
            "kind": record.kind.value,
            "content": record.content,
            "confidence": record.confidence,
            "importance": record.importance,
            "tags": record.tags,
            "metadata": record.metadata,
            "evidence": [ref.__dict__ for ref in record.evidence],
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "expires_at": record.expires_at.isoformat() if record.expires_at else None,
        }
        for record in records
    ]


def _merge_records(
    persisted: Iterable[MemoryRecord],
    active: Iterable[MemoryRecord],
) -> list[MemoryRecord]:
    """Union snapshots by stable ID, preferring the most recently updated copy."""

    merged: list[MemoryRecord] = []
    positions: dict[str, int] = {}
    for record in (*tuple(persisted), *tuple(active)):
        position = positions.get(record.id)
        if position is None:
            positions[record.id] = len(merged)
            merged.append(record)
            continue
        if _aware_datetime(record.updated_at) >= _aware_datetime(merged[position].updated_at):
            merged[position] = record
    return merged


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
    evidence: list[EvidenceRef] = []
    raw_evidence = item.get("evidence")
    if isinstance(raw_evidence, list):
        for raw_ref in raw_evidence:
            if not isinstance(raw_ref, dict):
                continue
            source = str(raw_ref.get("source", "")).strip()
            locator = str(raw_ref.get("locator", "")).strip()
            if source and locator:
                quote_raw = raw_ref.get("quote")
                evidence.append(
                    EvidenceRef(
                        source=source,
                        locator=locator,
                        quote=str(quote_raw) if quote_raw is not None else None,
                    )
                )
    now = datetime.now(UTC)
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
        evidence=evidence,
        created_at=_snapshot_datetime(item.get("created_at")) or now,
        updated_at=_snapshot_datetime(item.get("updated_at")) or now,
        expires_at=_snapshot_datetime(item.get("expires_at")),
    )


def _snapshot_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _aware_datetime(datetime.fromisoformat(value))
    except ValueError:
        return None


def _aware_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


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
