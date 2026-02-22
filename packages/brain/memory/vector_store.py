"""
Vector memory store — pgvector-backed semantic search
for long-term knowledge retrieval.
"""

import json

import os
import asyncio
import logging
from typing import Optional

import asyncpg

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


class VectorStore:
    """pgvector-backed semantic search for memories and knowledge."""

    def __init__(self):
        self._pool: Optional[asyncpg.Pool] = None
        self._embedder = None

    async def initialize(self):
        """Create connection pool and ensure pgvector extension + table."""
        self._pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5)

        async with self._pool.acquire() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(f"""
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
            """)
            # Create HNSW index for fast similarity search
            await conn.execute(f"""
                CREATE INDEX IF NOT EXISTS idx_memory_embeddings_vector
                ON memory_embeddings
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
            """)

        logger.info("Vector store initialized")

    def _get_embedder(self):
        """Lazy-load the embedding model."""
        if self._embedder is None:
            try:
                from sentence_transformers import SentenceTransformer
                model_name = os.getenv(
                    "EMBEDDINGS_MODEL",
                    "sentence-transformers/all-MiniLM-L6-v2"
                )
                self._embedder = SentenceTransformer(model_name)
                logger.info(f"Embedding model loaded: {model_name}")
            except ImportError:
                logger.warning("sentence-transformers not installed")
        return self._embedder

    async def embed(self, text: str) -> list[float]:
        """Generate embedding for text."""
        embedder = self._get_embedder()
        if not embedder:
            return [0.0] * EMBEDDINGS_DIM

        loop = asyncio.get_event_loop()
        embedding = await loop.run_in_executor(
            None, lambda: embedder.encode(text).tolist()
        )
        return embedding

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

        metadata_val = metadata or {}
        if isinstance(metadata_val, str):
            metadata_json = metadata_val  # Already serialized
        else:
            try:
                metadata_json = json.dumps(metadata_val, default=str)
            except (TypeError, ValueError):
                metadata_json = "{}"

        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """INSERT INTO memory_embeddings
                   (workspace_id, content, source_type, source_id, embedding, metadata)
                   VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
                   RETURNING id""",
                workspace_id, content, source_type, source_id,
                str(embedding), metadata_json,
            )
            return str(row["id"])

    async def search(
        self,
        workspace_id: str,
        query: str,
        limit: int = None,
    ) -> list[dict]:
        """Semantic search across workspace memories."""
        limit = limit or VECTOR_SEARCH_LIMIT
        query_embedding = await self.embed(query)

        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT id, content, source_type, source_id, metadata,
                          1 - (embedding <=> $1::vector) AS similarity
                   FROM memory_embeddings
                   WHERE workspace_id = $2
                   ORDER BY embedding <=> $1::vector
                   LIMIT $3""",
                str(query_embedding), workspace_id, limit,
            )

        return [
            {
                "id": str(r["id"]),
                "content": r["content"],
                "source_type": r["source_type"],
                "source_id": r["source_id"],
                "similarity": float(r["similarity"]),
                "metadata": (r["metadata"] if isinstance(r["metadata"], dict)
                             else json.loads(r["metadata"]) if isinstance(r["metadata"], str)
                             else {}) if r["metadata"] else {},
            }
            for r in rows
        ]

    async def delete(self, memory_id: str):
        """Delete a specific memory."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memory_embeddings WHERE id = $1",
                memory_id,
            )

    async def clear_workspace(self, workspace_id: str):
        """Delete all memories for a workspace."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM memory_embeddings WHERE workspace_id = $1",
                workspace_id,
            )
