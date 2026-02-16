"""
RAG retrieval pipeline — searches vector memory and formats
context for injection into LLM system prompts.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from memory.vector_store import VectorStore

logger = logging.getLogger("brain.memory.retrieval")


class RetrievalPipeline:
    """Retrieves and ranks relevant memories for RAG context injection."""

    def __init__(self, vector_store: VectorStore):
        self._store = vector_store

    async def retrieve_context(
        self,
        workspace_id: str,
        query: str,
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> str:
        """
        Search vector memory and return formatted context string.
        Uses time-weighted scoring: recent memories ranked higher.
        """
        results = await self._store.search(
            workspace_id=workspace_id,
            query=query,
            limit=top_k * 2,  # fetch extra for time-weighted re-ranking
        )

        if not results:
            return ""

        # Time-weighted re-ranking
        now = datetime.now(timezone.utc)
        scored = []
        for r in results:
            similarity = r.get("similarity", 0.0)
            if similarity < min_similarity:
                continue

            # Time decay: memories lose 10% relevance per week
            created = r.get("metadata", {}).get("created_at")
            time_weight = 1.0
            if created:
                try:
                    created_dt = datetime.fromisoformat(created)
                    days_old = (now - created_dt).days
                    time_weight = max(0.3, 1.0 - (days_old * 0.1 / 7))
                except (ValueError, TypeError):
                    pass

            final_score = similarity * time_weight
            scored.append({**r, "final_score": final_score})

        # Sort by final score, take top_k
        scored.sort(key=lambda x: x["final_score"], reverse=True)
        top_results = scored[:top_k]

        if not top_results:
            return ""

        # Format into context block
        return self._format_context(top_results)

    def _format_context(self, results: list[dict]) -> str:
        """Format retrieval results into a context block for the LLM."""
        lines = ["## Relevant Context (from memory)\n"]

        for i, r in enumerate(results, 1):
            source = r.get("source_type", "unknown")
            score = r.get("final_score", r.get("similarity", 0))
            content = r.get("content", "").strip()

            # Truncate very long memories
            if len(content) > 500:
                content = content[:497] + "..."

            lines.append(f"**[{i}]** _{source}_ (relevance: {score:.0%})")
            lines.append(f"> {content}\n")

        return "\n".join(lines)

    async def build_augmented_prompt(
        self,
        workspace_id: str,
        user_message: str,
        system_prompt: str = "",
        top_k: int = 5,
        min_similarity: float = 0.3,
    ) -> str:
        """
        Build a complete system prompt with RAG context injected.

        Returns a system prompt that includes:
        1. The original system prompt (if any)
        2. Retrieved context from memory
        3. Instructions for using the context
        """
        context = await self.retrieve_context(
            workspace_id=workspace_id,
            query=user_message,
            top_k=top_k,
            min_similarity=min_similarity,
        )

        parts = []

        if system_prompt:
            parts.append(system_prompt)

        if context:
            parts.append(
                "\n---\n"
                "The following context was retrieved from your memory. "
                "Use it to provide more informed and personalized responses. "
                "Do not mention that you retrieved this context unless asked.\n\n"
                f"{context}"
            )

        return "\n".join(parts) if parts else ""
