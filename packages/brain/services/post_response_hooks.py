"""Post-response hooks: persistence, RAG embedding, persona learning, memory graph."""

from __future__ import annotations

from typing import Any

from core.config import logger
from core import runtime
from crud import save_message


async def run_post_response_hooks(
    conversation_id: str,
    workspace_id: str,
    user_content: str,
    full_response: str,
    ws_config: dict,
    provider: Any,
    model: str,
    api_key: str,
    user_id: str,
) -> None:
    """Save message, embed for RAG, observe persona patterns, update memory graph.

    All errors are caught and logged — none propagate to the caller.
    """
    if not conversation_id or not full_response:
        return

    await save_message(conversation_id, "assistant", full_response)

    # Auto-embed the Q&A pair for future RAG
    if ws_config.get("rag_enabled") and runtime.embedding_pipeline:
        try:
            await runtime.embedding_pipeline.embed_conversation_turn(
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                user_message=user_content,
                assistant_response=full_response,
            )
        except Exception as e:
            logger.warning(f"RAG embedding failed: {e}")

    # Observe communication patterns for persona learning
    if runtime.persona_learner and full_response:
        try:
            await runtime.persona_learner.observe_communication(
                user_id=user_id,
                user_message=user_content,
                agent_response=full_response,
            )
            await runtime.persona_learner.observe_session_timing(
                user_id=user_id,
            )
        except Exception as e:
            logger.warning(f"Persona observation failed: {e}")

    # Update memory graph with conversation context (LLM extraction)
    if runtime.memory_graph and full_response:
        try:
            from agent.core.memory_graph import extract_entities_llm
            _entities, _relations = await extract_entities_llm(
                provider=provider,
                model=model,
                api_key=api_key,
                user_message=user_content,
                assistant_response=full_response,
            )
            if _entities:
                await runtime.memory_graph.extract_and_store(
                    conversation_id=conversation_id,
                    workspace_id=workspace_id,
                    entities=_entities,
                    relations=_relations,
                )
                logger.info(f"Memory graph: stored {len(_entities)} entities, {len(_relations)} relations")
        except Exception as e:
            logger.warning(f"Memory graph update failed: {e}")
