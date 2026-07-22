from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable

import numpy as np

from .models import MemoryLayer, MemoryRecord
from .private_artifacts import (
    harden_private_sqlite_files,
    prepare_private_sqlite_file,
    reset_disposable_private_sqlite_files,
)

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
        self._requires_rebuild: str | None = None
        self._lock = RLock()

    def open(self) -> None:
        with self._lock:
            prepare_private_sqlite_file(self.path)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                self._conn = conn
                self._ensure_schema()
                if self._requires_rebuild is None:
                    self._last_error = None
                harden_private_sqlite_files(self.path)
            except Exception:
                conn.close()
                self._conn = None
                raise

    def upsert(self, record: MemoryRecord) -> bool:
        with self._lock:
            if record.layer != self.layer:
                raise ValueError(
                    f"Cannot index {record.layer} record in {self.layer} vector sidecar"
                )
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
            self._last_error = None
            harden_private_sqlite_files(self.path)
            return True

    def tombstone(self, record_id: str) -> None:
        with self._lock:
            conn = self._require_conn()
            conn.execute(
                "UPDATE vector_records SET active = 0, updated_at = ? WHERE record_id = ?",
                (datetime.now(UTC).isoformat(), record_id),
            )
            conn.commit()
            self._last_error = None
            harden_private_sqlite_files(self.path)

    def rebuild(self, records: Iterable[MemoryRecord]) -> VectorSidecarStatus:
        with self._lock:
            if self._conn is None:
                if self._requires_rebuild is None:
                    raise RuntimeError("VectorSidecar.open() must be called before rebuild")
                try:
                    reset_disposable_private_sqlite_files(self.path)
                    self.open()
                except Exception as exc:
                    self.record_open_error(exc)
                    raise
            rows = tuple(records)
            seen: set[str] = set()
            complete = True
            for record in rows:
                seen.add(record.id)
                complete = self.upsert(record) and complete
            conn = self._require_conn()
            if seen:
                placeholders = ",".join("?" for _ in seen)
                conn.execute(
                    f"DELETE FROM vector_records WHERE record_id NOT IN ({placeholders})",
                    tuple(seen),
                )
            else:
                conn.execute("DELETE FROM vector_records")
            conn.commit()
            harden_private_sqlite_files(self.path)
            if complete:
                self._requires_rebuild = None
                self._last_error = None
            return self.status(records=rows)

    def search(
        self,
        query: str,
        *,
        k: int = 8,
        min_score: float = 0.0,
        include_inactive: bool = False,
    ) -> list[VectorSidecarHit]:
        with self._lock:
            if self._requires_rebuild is not None:
                return []
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
            incompatible_dimension = False
            for record_id, dimension, blob in conn.execute(sql, params):
                vector = np.frombuffer(blob, dtype=np.float32)
                if (
                    int(dimension) != int(normalized_query.shape[0])
                    or vector.shape != normalized_query.shape
                ):
                    incompatible_dimension = True
                    continue
                score = float(vector @ normalized_query)
                if score >= min_score and score > 0:
                    hits.append(
                        VectorSidecarHit(record_id=str(record_id), score=min(score, 1.0))
                    )
            if incompatible_dimension:
                self._requires_rebuild = "vector dimension changed; rebuild required"
                return []
            self._last_error = None
            return sorted(hits, key=lambda hit: hit.score, reverse=True)[:k]

    def status(self, *, records: Iterable[MemoryRecord] | None = None) -> VectorSidecarStatus:
        with self._lock:
            if self._conn is None:
                return self._status_disabled(
                    self._requires_rebuild
                    or self._last_error
                    or "vector sidecar is not open"
                )
            try:
                indexed_count = int(
                    self._conn.execute(
                        "SELECT COUNT(*) FROM vector_records WHERE active = 1"
                    ).fetchone()[0]
                )
                dimension_row = self._conn.execute(
                    "SELECT dimension FROM vector_records "
                    "WHERE active = 1 ORDER BY updated_at DESC LIMIT 1"
                ).fetchone()
                dimension = None if dimension_row is None else int(dimension_row[0])
                stale_count = 0
                missing_count = 0
                if records is not None:
                    indexed = {
                        str(row[0]): (str(row[1]), bool(row[2]))
                        for row in self._conn.execute(
                            "SELECT record_id, content_hash, active FROM vector_records"
                        )
                    }
                    for record in records:
                        row = indexed.get(record.id)
                        if row is None:
                            missing_count += 1
                            continue
                        content_hash, active = row
                        if content_hash != record.content_hash or active != _record_active(record):
                            stale_count += 1
            except Exception as exc:  # noqa: BLE001 - index is disposable
                self.record_error(exc)
                return self._status_disabled(self._last_error or str(exc))
            return VectorSidecarStatus(
                layer=self.layer,
                enabled=self._last_error is None and self._requires_rebuild is None,
                path=str(self.path),
                mv2_path=str(self.mv2_path),
                provider=self.provider,
                embedding_model=self.embedder.model_name,
                indexed_count=indexed_count,
                stale_count=stale_count,
                missing_count=missing_count,
                dimension=dimension,
                disabled_reason=self._requires_rebuild or self._last_error,
            )

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
            self._conn = None
            harden_private_sqlite_files(self.path)

    def record_error(self, error: BaseException) -> None:
        """Expose disposable-index degradation without failing canonical memory."""

        with self._lock:
            self._last_error = f"{type(error).__name__}: {error}"

    def record_open_error(self, error: BaseException) -> None:
        """Retain the original structural failure until the index is replaced."""

        with self._lock:
            reason = f"{type(error).__name__}: {error}"
            self._last_error = reason
            self._requires_rebuild = reason

    def _status_disabled(self, reason: str) -> VectorSidecarStatus:
        return VectorSidecarStatus(
            layer=self.layer,
            enabled=False,
            path=str(self.path),
            mv2_path=str(self.mv2_path),
            provider=self.provider,
            embedding_model=self.embedder.model_name,
            disabled_reason=reason,
        )

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
        existing_metadata = {
            str(key): str(value)
            for key, value in conn.execute("SELECT key, value FROM vector_metadata")
        }
        indexed_count = int(conn.execute("SELECT COUNT(*) FROM vector_records").fetchone()[0])
        identity_keys = ("schema_version", "layer", "provider", "embedding_model")
        incompatible_keys = [
            key
            for key in identity_keys
            if key in existing_metadata and existing_metadata[key] != metadata[key]
        ]
        if indexed_count and (
            incompatible_keys
            or any(key not in existing_metadata for key in identity_keys)
        ):
            conn.execute("DELETE FROM vector_records")
            changed = ", ".join(incompatible_keys) or "missing identity metadata"
            self._requires_rebuild = (
                f"vector index identity changed ({changed}); rebuild required"
            )
        conn.executemany(
            """
            INSERT INTO vector_metadata (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            tuple(metadata.items()),
        )
        conn.commit()
        harden_private_sqlite_files(self.path)

    def _embed_record(self, record: MemoryRecord) -> np.ndarray | None:
        try:
            return np.asarray(self.embedder.embed(_record_text(record)), dtype=np.float32)
        except VectorSidecarUnavailable as exc:
            self._last_error = str(exc)
            return None

    def _embed_query(self, query: str) -> np.ndarray | None:
        try:
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
