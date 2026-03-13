"""
Vector memory store with pgvector compatibility and a native SQLite exact-search backend.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

try:
    import asyncpg
except ModuleNotFoundError:  # pragma: no cover - optional in native mode
    asyncpg = None

from native_backends import native_state_dir, use_local_vector_backend

logger = logging.getLogger("brain.memory.vector_store")

EMBEDDINGS_DIM = int(os.getenv("EMBEDDINGS_DIMENSION", "384"))
VECTOR_SEARCH_LIMIT = int(os.getenv("VECTOR_SEARCH_LIMIT", "10"))

DB_URL = os.getenv(
    "DATABASE_URL",
    f"postgresql://{os.getenv('POSTGRES_USER', 'kestrel')}:"
    f"{os.getenv('POSTGRES_PASSWORD', 'changeme')}@"
    f"{os.getenv('POSTGRES_HOST', 'localhost')}:"
    f"{os.getenv('POSTGRES_PORT', '5432')}/"
    f"{os.getenv('POSTGRES_DB', 'kestrel')}"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class VectorStore:
    """Semantic search for memories and knowledge with local native fallback."""

    def __init__(self):
        self._pool: Optional["asyncpg.Pool"] = None
        self._sqlite: Optional[sqlite3.Connection] = None
        self._embedder = None
        self.backend_name = "pgvector"

    async def initialize(self):
        """Create the configured backend and schema."""
        if use_local_vector_backend():
            db_path = native_state_dir() / "brain_vector_store.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._sqlite = sqlite3.connect(str(db_path), check_same_thread=False)
            self._sqlite.row_factory = sqlite3.Row
            self._sqlite.execute("PRAGMA journal_mode=WAL")
            self._sqlite.execute("PRAGMA synchronous=NORMAL")
            self._sqlite.executescript(
                """
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    source_id TEXT,
                    embedding_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_workspace_source
                    ON memory_embeddings(workspace_id, source_type, created_at DESC);
                """
            )
            self._sqlite.commit()
            self.backend_name = "sqlite_exact"
            logger.info("Vector store initialized with SQLite exact backend at %s", db_path)
            return

        if asyncpg is None:
            raise RuntimeError(
                "pgvector backend requires asyncpg. "
                "Install packages/brain/requirements.txt or enable native vector backend."
            )

        self._pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)

        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS memory_embeddings (
                    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                    workspace_id UUID NOT NULL,
                    content TEXT NOT NULL,
                    source_type TEXT DEFAULT 'conversation',
                    source_id TEXT,
                    embedding vector({EMBEDDINGS_DIM}),
                    metadata JSONB DEFAULT '{{}}',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_source
                ON memory_embeddings(workspace_id, source_type, created_at DESC)
                """
            )
            await conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_vector
                ON memory_embeddings
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """
            )

        self.backend_name = "pgvector"
        logger.info("Vector store initialized with pgvector backend")

    def _get_embedder(self):
        """Lazy-load the embedding model."""
        if self._embedder is None:
            try:
                from sentence_transformers import SentenceTransformer

                model_name = os.getenv(
                    "EMBEDDINGS_MODEL",
                    "sentence-transformers/all-MiniLM-L6-v2",
                )
                self._embedder = SentenceTransformer(model_name)
                logger.info("Embedding model loaded: %s", model_name)
            except ImportError:
                logger.warning("sentence-transformers not installed; using deterministic hash embeddings")
        return self._embedder

    @staticmethod
    def _fallback_embed(text: str) -> list[float]:
        vector = [0.0] * EMBEDDINGS_DIM
        for token in (text or "").lower().split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "big") % EMBEDDINGS_DIM
            sign = -1.0 if digest[2] % 2 else 1.0
            vector[index] += sign
        magnitude = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / magnitude for value in vector]

    async def embed(self, text: str) -> list[float]:
        """Generate an embedding for text."""
        embedder = self._get_embedder()
        if not embedder:
            return self._fallback_embed(text)

        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(
            None,
            lambda: embedder.encode(text).tolist(),
        )
        return embedding

    @staticmethod
    def _normalize_metadata(metadata: Any) -> dict[str, Any]:
        if isinstance(metadata, dict):
            return dict(metadata)
        if isinstance(metadata, str):
            try:
                parsed = json.loads(metadata)
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @staticmethod
    def _cosine_similarity(lhs: list[float], rhs: list[float]) -> float:
        return sum(a * b for a, b in zip(lhs, rhs))

    async def store(
        self,
        workspace_id: str,
        content: str,
        source_type: str = "conversation",
        source_id: str = None,
        metadata: dict = None,
    ) -> str:
        """Store a memory with its embedding."""
        embedding = await self.embed(content)
        metadata_value = dict(metadata or {})
        metadata_value.setdefault("created_at", _now_iso())

        if self._sqlite is not None:
            memory_id = str(uuid.uuid4())
            self._sqlite.execute(
                """
                INSERT INTO memory_embeddings (
                    id, workspace_id, content, source_type, source_id, embedding_json, metadata_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_id,
                    workspace_id,
                    content,
                    source_type or "conversation",
                    source_id,
                    json.dumps(embedding),
                    json.dumps(metadata_value, default=str),
                    _now_iso(),
                ),
            )
            self._sqlite.commit()
            return memory_id

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO memory_embeddings
                    (workspace_id, content, source_type, source_id, embedding, metadata)
                VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
                RETURNING id
                """,
                workspace_id,
                content,
                source_type,
                source_id,
                str(embedding),
                json.dumps(metadata_value, default=str),
            )
            return str(row["id"])

    async def upsert(
        self,
        *,
        workspace_id: str,
        documents: list[dict[str, Any]],
        source_filter: str | None = None,
    ) -> list[str]:
        """Replace a logical document group and store the new documents."""
        if source_filter:
            await self.delete_by_source(workspace_id=workspace_id, source_type=source_filter)

        memory_ids: list[str] = []
        for document in documents:
            metadata = self._normalize_metadata(document.get("metadata"))
            source_type = str(
                document.get("source_type")
                or source_filter
                or metadata.get("source")
                or "conversation"
            )
            memory_ids.append(
                await self.store(
                    workspace_id=workspace_id,
                    content=str(document.get("content") or ""),
                    source_type=source_type,
                    source_id=document.get("source_id"),
                    metadata=metadata,
                )
            )
        return memory_ids

    async def search(
        self,
        workspace_id: str,
        query: str,
        limit: int = None,
        *,
        top_k: int | None = None,
        source_filter: str | None = None,
    ) -> list[dict]:
        """Semantic search across workspace memories."""
        limit = top_k or limit or VECTOR_SEARCH_LIMIT
        query_embedding = await self.embed(query)

        if self._sqlite is not None:
            rows = self._sqlite.execute(
                """
                SELECT id, content, source_type, source_id, embedding_json, metadata_json, created_at
                FROM memory_embeddings
                WHERE workspace_id = ?
                """
                + (" AND source_type = ?" if source_filter else "")
                + " ORDER BY created_at DESC",
                (workspace_id, source_filter) if source_filter else (workspace_id,),
            ).fetchall()

            ranked: list[dict[str, Any]] = []
            for row in rows:
                candidate = json.loads(row["embedding_json"])
                similarity = float(self._cosine_similarity(query_embedding, candidate))
                metadata = self._normalize_metadata(row["metadata_json"])
                metadata.setdefault("created_at", row["created_at"])
                ranked.append(
                    {
                        "id": row["id"],
                        "content": row["content"],
                        "source_type": row["source_type"],
                        "source_id": row["source_id"],
                        "similarity": similarity,
                        "score": similarity,
                        "metadata": metadata,
                    }
                )
            ranked.sort(key=lambda item: item["similarity"], reverse=True)
            return ranked[:limit]

        if source_filter:
            rows = await self._pool.fetch(
                """
                SELECT id, content, source_type, source_id, metadata, created_at,
                       1 - (embedding <=> $1::vector) AS similarity
                FROM memory_embeddings
                WHERE workspace_id = $2
                  AND source_type = $3
                ORDER BY embedding <=> $1::vector
                LIMIT $4
                """,
                str(query_embedding),
                workspace_id,
                source_filter,
                limit,
            )
        else:
            rows = await self._pool.fetch(
                """
                SELECT id, content, source_type, source_id, metadata, created_at,
                       1 - (embedding <=> $1::vector) AS similarity
                FROM memory_embeddings
                WHERE workspace_id = $2
                ORDER BY embedding <=> $1::vector
                LIMIT $3
                """,
                str(query_embedding),
                workspace_id,
                limit,
            )

        results = []
        for row in rows:
            metadata = self._normalize_metadata(row["metadata"])
            metadata.setdefault("created_at", str(row["created_at"]))
            similarity = float(row["similarity"])
            results.append(
                {
                    "id": str(row["id"]),
                    "content": row["content"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "similarity": similarity,
                    "score": similarity,
                    "metadata": metadata,
                }
            )
        return results

    async def delete(self, memory_id: str):
        """Delete a specific memory."""
        if self._sqlite is not None:
            self._sqlite.execute("DELETE FROM memory_embeddings WHERE id = ?", (memory_id,))
            self._sqlite.commit()
            return

        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM memory_embeddings WHERE id = $1", memory_id)

    async def clear_workspace(self, workspace_id: str):
        """Delete all memories for a workspace."""
        if self._sqlite is not None:
            self._sqlite.execute("DELETE FROM memory_embeddings WHERE workspace_id = ?", (workspace_id,))
            self._sqlite.commit()
            return

        async with self._pool.acquire() as conn:
            await conn.execute("DELETE FROM memory_embeddings WHERE workspace_id = $1", workspace_id)

    async def delete_by_source(
        self,
        *,
        workspace_id: str,
        source_type: str,
        source_id: str | None = None,
    ) -> None:
        if self._sqlite is not None:
            if source_id is None:
                self._sqlite.execute(
                    "DELETE FROM memory_embeddings WHERE workspace_id = ? AND source_type = ?",
                    (workspace_id, source_type),
                )
            else:
                self._sqlite.execute(
                    """
                    DELETE FROM memory_embeddings
                    WHERE workspace_id = ? AND source_type = ? AND source_id = ?
                    """,
                    (workspace_id, source_type, source_id),
                )
            self._sqlite.commit()
            return

        async with self._pool.acquire() as conn:
            if source_id is None:
                await conn.execute(
                    "DELETE FROM memory_embeddings WHERE workspace_id = $1 AND source_type = $2",
                    workspace_id,
                    source_type,
                )
            else:
                await conn.execute(
                    """
                    DELETE FROM memory_embeddings
                    WHERE workspace_id = $1 AND source_type = $2 AND source_id = $3
                    """,
                    workspace_id,
                    source_type,
                    source_id,
                )

    async def get_latest_by_source(
        self,
        *,
        workspace_id: str,
        source_type: str,
        source_id: str,
    ) -> dict[str, Any] | None:
        if self._sqlite is not None:
            row = self._sqlite.execute(
                """
                SELECT id, content, source_type, source_id, metadata_json, created_at
                FROM memory_embeddings
                WHERE workspace_id = ? AND source_type = ? AND source_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (workspace_id, source_type, source_id),
            ).fetchone()
            if not row:
                return None
            metadata = self._normalize_metadata(row["metadata_json"])
            metadata.setdefault("created_at", row["created_at"])
            return {
                "id": row["id"],
                "content": row["content"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "metadata": metadata,
                "created_at": row["created_at"],
            }

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, content, source_type, source_id, metadata, created_at
                FROM memory_embeddings
                WHERE workspace_id = $1 AND source_type = $2 AND source_id = $3
                ORDER BY created_at DESC
                LIMIT 1
                """,
                workspace_id,
                source_type,
                source_id,
            )
            if not row:
                return None
            metadata = self._normalize_metadata(row["metadata"])
            metadata.setdefault("created_at", str(row["created_at"]))
            return {
                "id": str(row["id"]),
                "content": row["content"],
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "metadata": metadata,
                "created_at": str(row["created_at"]),
            }

    async def list_by_source(
        self,
        *,
        workspace_id: str,
        source_type: str,
    ) -> list[dict[str, Any]]:
        if self._sqlite is not None:
            rows = self._sqlite.execute(
                """
                SELECT id, content, source_type, source_id, metadata_json, created_at
                FROM memory_embeddings
                WHERE workspace_id = ? AND source_type = ?
                ORDER BY created_at DESC
                """,
                (workspace_id, source_type),
            ).fetchall()
            return [
                {
                    "id": row["id"],
                    "content": row["content"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "metadata": self._normalize_metadata(row["metadata_json"]),
                    "created_at": row["created_at"],
                }
                for row in rows
            ]

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, content, source_type, source_id, metadata, created_at
                FROM memory_embeddings
                WHERE workspace_id = $1 AND source_type = $2
                ORDER BY created_at DESC
                """,
                workspace_id,
                source_type,
            )
            return [
                {
                    "id": str(row["id"]),
                    "content": row["content"],
                    "source_type": row["source_type"],
                    "source_id": row["source_id"],
                    "metadata": self._normalize_metadata(row["metadata"]),
                    "created_at": str(row["created_at"]),
                }
                for row in rows
            ]

    async def close(self) -> None:
        if self._sqlite is not None:
            self._sqlite.close()
            self._sqlite = None
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
