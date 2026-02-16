"""
Memory tools — search and store information in the workspace knowledge base.

Wraps the existing VectorStore (pgvector) for agent use, enabling
long-term knowledge storage and semantic retrieval.
"""

import logging
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.memory")

# Vector store reference (set during registration)
_vector_store = None


def register_memory_tools(registry, vector_store=None) -> None:
    """Register memory/knowledge tools."""
    global _vector_store
    _vector_store = vector_store

    registry.register(
        definition=ToolDefinition(
            name="memory_search",
            description=(
                "Search the workspace knowledge base for relevant information. "
                "Uses semantic similarity (not keyword matching). Good for "
                "finding previous conversations, stored facts, and context."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of results to return (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=15,
            category="memory",
        ),
        handler=memory_search,
    )

    registry.register(
        definition=ToolDefinition(
            name="memory_store",
            description=(
                "Store a piece of information in the workspace knowledge base. "
                "Use for saving important facts, decisions, research findings, "
                "or any information that should be retrieved later."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The information to store",
                    },
                    "source": {
                        "type": "string",
                        "description": "Source label (e.g., 'agent_research', 'user_note')",
                        "default": "agent",
                    },
                },
                "required": ["content"],
            },
            risk_level=RiskLevel.LOW,
            timeout_seconds=15,
            category="memory",
        ),
        handler=memory_store,
    )


async def memory_search(
    query: str,
    top_k: int = 5,
    workspace_id: str = "default",
) -> dict:
    """Search the workspace knowledge base."""
    if not _vector_store:
        return {"error": "Knowledge base not available"}

    top_k = min(top_k, 20)

    try:
        results = await _vector_store.search(
            workspace_id=workspace_id,
            query=query,
            limit=top_k,
        )

        return {
            "query": query,
            "results": [
                {
                    "content": r.get("content", ""),
                    "source": r.get("source_type", ""),
                    "similarity": round(r.get("similarity", 0.0), 3),
                }
                for r in results
            ],
            "count": len(results),
        }

    except Exception as e:
        return {"query": query, "error": str(e)}


async def memory_store(
    content: str,
    source: str = "agent",
    workspace_id: str = "default",
) -> dict:
    """Store information in the workspace knowledge base."""
    if not _vector_store:
        return {"error": "Knowledge base not available"}

    try:
        memory_id = await _vector_store.store(
            workspace_id=workspace_id,
            content=content,
            source_type=source,
        )

        return {
            "id": memory_id,
            "stored": True,
            "content_length": len(content),
        }

    except Exception as e:
        return {"error": str(e)}
