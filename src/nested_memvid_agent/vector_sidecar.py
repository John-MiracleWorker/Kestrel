from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .models import MemoryLayer, MemoryRecord

SCHEMA_VERSION = 1
DEFAULT_LOCAL_EMBEDDING_MODEL = "all-MiniLM-L6-v2"


@runtime_checkable
class TextEmbedder(Protocol):
    model_name: str

    def embed(self, text: str) -> np.ndarray:
        raise NotImplementedError


class VectorSidecarUnavailable(RuntimeError):
    """Raised when an explicitly configured vector sidecar cannot embed text."""


@dataclass(frozen=True)
class VectorSidecarHit:
    record_id: str
    score: float


@dataclass(frozen=True)
class VectorSidecarStatus:
    layer: MemoryLayer
    enabled: bool
    path: str | None
    mv2_path: str | None
    provider: str | None
    embedding_model: str | None
    indexed_count: int = 0
    stale_count: int = 0
    missing_count: int = 0
    dimension: int | None = None
    disabled_reason: str | None = None

    @classmethod
    def disabled(cls, layer: MemoryLayer, reason: str) -> VectorSidecarStatus:
        return cls(
            layer=layer,
            enabled=False,
            path=None,
            mv2_path=None,
            provider=None,
            embedding_model=None,
            disabled_reason=reason,
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "layer": self.layer.value,
            "enabled": self.enabled,
            "path": self.path,
            "mv2_path": self.mv2_path,
            "provider": self.provider,
            "embedding_model": self.embedding_model,
            "indexed_count": self.indexed_count,
            "stale_count": self.stale_count,
            "missing_count": self.missing_count,
            "dimension": self.dimension,
            "disabled_reason": self.disabled_reason,
        }


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str = DEFAULT_LOCAL_EMBEDDING_MODEL) -> None:
        self.model_name = model_name
        self._model: Any | None = None

    def embed(self, text: str) -> np.ndarray:
        model = self._load_model()
        encoded = model.encode(text, convert_to_numpy=True, normalize_embeddings=False)
        return np.asarray(encoded, dtype=np.float32)

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise VectorSidecarUnavailable("sentence-transformers is required for local vector sidecars") from exc
        self._model = SentenceTransformer(self.model_name)
        return self._model


def make_local_embedder(model_name: str | None = None) -> TextEmbedder:
    return SentenceTransformerEmbedder(model_name or DEFAULT_LOCAL_EMBEDDING_MODEL)


class VectorSidecar:
    """Disposable SQLite vector index keyed to canonical `.mv2` record IDs."""

    def __init__(
        self,
        *,
        path: Path,
        layer: MemoryLayer,
        embedder: TextEmbedder,
        mv2_path: Path,
        provider: str = "local",
    ) -> None:
        self.path = Path(path)
        self.layer = layer
        self.embedder = embedder
        self.mv2_path = Path(mv2_path)
        self.provider = provider
        self._conn: sqlite3.Connection | None = None
        self._last_error: str | None = None

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        self._conn = conn
        self._ensure_schema()

    def upsert(self, record: MemoryRecord) -> bool:
        if record.layer != self.layer:
            raise ValueError(f"Cannot index {record.layer} record in {self.layer} vector sidecar")
        vector = self._embed_record(record)
        if vector is None:
            return False
        conn = self._require_conn()
        normalized = _normalized(vector)
        if normalized is None:
            return False
        conn.execute(
            """
            INSERT INTO vector_records
                (record_id, content_hash, active, dimension, vector, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(record_id) DO UPDATE SET
                content_hash=excluded.content_hash,
                active=excluded.active,
                dimension=excluded.dimension,
                vector=excluded.vector,
                updated_at=excluded.updated_at
            """,
            (
                record.id,
                record.content_hash,
                1 if _record_active(record) else 0,
                int(normalized.shape[0]),
                normalized.astype(np.float32).tobytes(),
                datetime.now(UTC).isoformat(),
            ),
        )
        conn.commit()
        return True

    def tombstone(self, record_id: str) -> None:
        conn = self._require_conn()
        conn.execute(
            "UPDATE vector_records SET active = 0, updated_at = ? WHERE record_id = ?",
            (datetime.now(UTC).isoformat(), record_id),
        )
        conn.commit()

    def rebuild(self, records: Iterable[MemoryRecord]) -> VectorSidecarStatus:
        rows = tuple(records)
        seen: set[str] = set()
        for record in rows:
            seen.add(record.id)
            self.upsert(record)
        conn = self._require_conn()
        if seen:
            placeholders = ",".join("?" for _ in seen)
            conn.execute(f"DELETE FROM vector_records WHERE record_id NOT IN ({placeholders})", tuple(seen))
        else:
            conn.execute("DELETE FROM vector_records")
        conn.commit()
        return self.status(records=rows)

    def search(
        self,
        query: str,
        *,
        k: int = 8,
        min_score: float = 0.0,
        include_inactive: bool = False,
    ) -> list[VectorSidecarHit]:
        query_vector = self._embed_query(query)
        if query_vector is None:
            return []
        normalized_query = _normalized(query_vector)
        if normalized_query is None:
            return []
        conn = self._require_conn()
        sql = "SELECT record_id, dimension, vector FROM vector_records"
        params: tuple[object, ...] = ()
        if not include_inactive:
            sql += " WHERE active = 1"
        hits: list[VectorSidecarHit] = []
        for record_id, dimension, blob in conn.execute(sql, params):
            vector = np.frombuffer(blob, dtype=np.float32)
            if int(dimension) != int(normalized_query.shape[0]) or vector.shape != normalized_query.shape:
                continue
            score = float(vector @ normalized_query)
            if score >= min_score and score > 0:
                hits.append(VectorSidecarHit(record_id=str(record_id), score=min(score, 1.0)))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:k]

    def status(self, *, records: Iterable[MemoryRecord] | None = None) -> VectorSidecarStatus:
        conn = self._require_conn()
        indexed_count = int(conn.execute("SELECT COUNT(*) FROM vector_records WHERE active = 1").fetchone()[0])
        dimension_row = conn.execute(
            "SELECT dimension FROM vector_records WHERE active = 1 ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        dimension = None if dimension_row is None else int(dimension_row[0])
        stale_count = 0
        missing_count = 0
        if records is not None:
            indexed = {
                str(row[0]): (str(row[1]), bool(row[2]))
                for row in conn.execute("SELECT record_id, content_hash, active FROM vector_records")
            }
            for record in records:
                row = indexed.get(record.id)
                if row is None:
                    missing_count += 1
                    continue
                content_hash, active = row
                if content_hash != record.content_hash or active != _record_active(record):
                    stale_count += 1
        return VectorSidecarStatus(
            layer=self.layer,
            enabled=self._last_error is None,
            path=str(self.path),
            mv2_path=str(self.mv2_path),
            provider=self.provider,
            embedding_model=self.embedder.model_name,
            indexed_count=indexed_count,
            stale_count=stale_count,
            missing_count=missing_count,
            dimension=dimension,
            disabled_reason=self._last_error,
        )

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
        self._conn = None

    def _ensure_schema(self) -> None:
        conn = self._require_conn()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_records (
                record_id TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                active INTEGER NOT NULL,
                dimension INTEGER NOT NULL,
                vector BLOB NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vector_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        metadata = {
            "schema_version": str(SCHEMA_VERSION),
            "layer": self.layer.value,
            "provider": self.provider,
            "embedding_model": self.embedder.model_name,
            "mv2_path": str(self.mv2_path),
        }
        conn.executemany(
            """
            INSERT INTO vector_metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            tuple(metadata.items()),
        )
        conn.commit()

    def _embed_record(self, record: MemoryRecord) -> np.ndarray | None:
        try:
            self._last_error = None
            return np.asarray(self.embedder.embed(_record_text(record)), dtype=np.float32)
        except VectorSidecarUnavailable as exc:
            self._last_error = str(exc)
            return None

    def _embed_query(self, query: str) -> np.ndarray | None:
        try:
            self._last_error = None
            return np.asarray(self.embedder.embed(query), dtype=np.float32)
        except VectorSidecarUnavailable as exc:
            self._last_error = str(exc)
            return None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("VectorSidecar.open() must be called before use")
        return self._conn


def _normalized(vector: np.ndarray) -> np.ndarray | None:
    flat = np.asarray(vector, dtype=np.float32).reshape(-1)
    if flat.size == 0:
        return None
    norm = float(np.linalg.norm(flat))
    if norm <= 1e-12:
        return None
    return flat / norm


def _record_text(record: MemoryRecord) -> str:
    return " ".join(
        part
        for part in (
            record.title,
            record.kind.value,
            record.content,
            " ".join(record.tags.values()),
        )
        if part
    )


def _record_active(record: MemoryRecord) -> bool:
    return record.metadata.get("active", True) is not False
