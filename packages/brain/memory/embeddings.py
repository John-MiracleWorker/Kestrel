"""
Auto-embedding pipeline — stores conversation content into
vector memory for RAG retrieval.
"""

import logging
import asyncio
from typing import Optional
from datetime import datetime, timezone

from memory.vector_store import VectorStore

logger = logging.getLogger("brain.memory.embeddings")


class EmbeddingPipeline:
    """Automatically embeds conversations and content into vector memory."""

    def __init__(self, vector_store: VectorStore):
        self._store = vector_store
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    async def start(self):
        """Start the background embedding worker."""
        self._running = True
        asyncio.create_task(self._worker())
        logger.info("Embedding pipeline started")

    async def stop(self):
        """Stop the background worker."""
        self._running = False

    async def _worker(self):
        """Background worker that processes embedding queue."""
        while self._running:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=5.0)
                await self._process_item(item)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"Embedding worker error: {e}")

    async def _process_item(self, item: dict):
        """Embed a single item into vector memory."""
        try:
            await self._store.store(
                workspace_id=item["workspace_id"],
                content=item["content"],
                source_type=item.get("source_type", "conversation"),
                source_id=item.get("source_id"),
                metadata={
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    **(item.get("metadata") or {}),
                },
            )
            logger.debug(f"Embedded {item.get('source_type', 'conversation')} content")
        except Exception as e:
            logger.error(f"Failed to embed content: {e}")

    def enqueue(
        self,
        workspace_id: str,
        content: str,
        source_type: str = "conversation",
        source_id: Optional[str] = None,
        metadata: Optional[dict] = None,
    ):
        """Add content to the embedding queue (non-blocking)."""
        if not content or len(content.strip()) < 20:
            return  # Skip very short content

        self._queue.put_nowait({
            "workspace_id": workspace_id,
            "content": content,
            "source_type": source_type,
            "source_id": source_id,
            "metadata": metadata,
        })

    async def embed_conversation_turn(
        self,
        workspace_id: str,
        conversation_id: str,
        user_message: str,
        assistant_response: str,
    ):
        """
        Embed a complete conversation turn (user Q + assistant A).
        Creates a single memory entry combining both for better retrieval.
        """
        combined = (
            f"User asked: {user_message}\n"
            f"Assistant answered: {assistant_response}"
        )

        # Only embed substantial exchanges
        if len(combined) < 50:
            return

        # Truncate very long exchanges
        if len(combined) > 2000:
            combined = combined[:1997] + "..."

        self.enqueue(
            workspace_id=workspace_id,
            content=combined,
            source_type="conversation",
            source_id=conversation_id,
            metadata={
                "conversation_id": conversation_id,
                "turn_type": "qa_pair",
            },
        )

    async def embed_batch(self, items: list[dict]):
        """Embed multiple items at once."""
        for item in items:
            self.enqueue(**item)
