"""
Working Memory — two-tier memory system for agent task execution.

Tier 1: Scratchpad (Redis) — task-scoped ephemeral storage for intermediate
        results, context, and state. Automatically expires with the task.

Tier 2: Knowledge (pgvector) — permanent semantic memory via the existing
        VectorStore. Used for long-term facts and learnings.
"""

import json
import logging
from typing import Optional

logger = logging.getLogger("brain.agent.memory")

# Default TTL for scratchpad entries: 2 hours
SCRATCHPAD_TTL = 7200


class WorkingMemory:
    """
    Two-tier memory for agent tasks.

    Scratchpad: Redis-backed key-value store scoped to a task.
    Knowledge: pgvector-backed semantic search scoped to a workspace.
    """

    def __init__(self, redis_client=None, vector_store=None):
        self._redis = redis_client
        self._vector_store = vector_store

    # ── Scratchpad (Redis) ───────────────────────────────────────────

    def _sp_key(self, task_id: str, key: str) -> str:
        """Build a Redis key for scratchpad entries."""
        return f"kestrel:agent:scratchpad:{task_id}:{key}"

    def _sp_index_key(self, task_id: str) -> str:
        """Key for the set of all scratchpad keys for a task."""
        return f"kestrel:agent:scratchpad:{task_id}:__index__"

    async def scratchpad_write(
        self,
        task_id: str,
        key: str,
        value: str,
        ttl: int = SCRATCHPAD_TTL,
    ) -> None:
        """Write a value to the task scratchpad."""
        if not self._redis:
            logger.warning("Redis not available for scratchpad")
            return

        redis_key = self._sp_key(task_id, key)
        await self._redis.setex(redis_key, ttl, value)
        # Track the key in the index
        await self._redis.sadd(self._sp_index_key(task_id), key)
        await self._redis.expire(self._sp_index_key(task_id), ttl)

    async def scratchpad_read(
        self,
        task_id: str,
        key: str,
    ) -> Optional[str]:
        """Read a value from the task scratchpad."""
        if not self._redis:
            return None

        return await self._redis.get(self._sp_key(task_id, key))

    async def scratchpad_list(self, task_id: str) -> dict[str, str]:
        """List all scratchpad entries for a task."""
        if not self._redis:
            return {}

        index_key = self._sp_index_key(task_id)
        keys = await self._redis.smembers(index_key)

        result = {}
        for key in keys:
            value = await self._redis.get(self._sp_key(task_id, key))
            if value:
                result[key] = value

        return result

    async def scratchpad_clear(self, task_id: str) -> None:
        """Clear all scratchpad entries for a task."""
        if not self._redis:
            return

        index_key = self._sp_index_key(task_id)
        keys = await self._redis.smembers(index_key)

        pipe = self._redis.pipeline()
        for key in keys:
            pipe.delete(self._sp_key(task_id, key))
        pipe.delete(index_key)
        await pipe.execute()

    # ── Knowledge (pgvector) ─────────────────────────────────────────

    async def knowledge_store(
        self,
        workspace_id: str,
        content: str,
        source: str = "agent",
        metadata: dict = None,
    ) -> Optional[str]:
        """Store information in the workspace knowledge base."""
        if not self._vector_store:
            logger.warning("Vector store not available for knowledge storage")
            return None

        memory_id = await self._vector_store.store(
            workspace_id=workspace_id,
            content=content,
            source_type=source,
            metadata=metadata or {},
        )
        return memory_id

    async def knowledge_search(
        self,
        workspace_id: str,
        query: str,
        top_k: int = 5,
    ) -> list[dict]:
        """Search the workspace knowledge base."""
        if not self._vector_store:
            return []

        results = await self._vector_store.search(
            workspace_id=workspace_id,
            query=query,
            limit=top_k,
        )
        return results

    async def knowledge_forget(self, memory_id: str) -> None:
        """Delete a specific memory from the knowledge base."""
        if not self._vector_store:
            return

        await self._vector_store.delete(memory_id)
