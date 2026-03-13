"""
Initialize node — Phase 0 of the agent loop.

Retrieves semantic memory, past lessons, user persona, and builds
the context envelope that downstream nodes (plan, execute) will use.

Wraps existing components:
  - TaskLearner.enrich_context()
  - MemoryGraph.format_for_prompt()
  - PersonaLearner.load_persona()
"""

from __future__ import annotations

import logging
from typing import Any

from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.initialize")

# Stop words for goal entity extraction (reused from loop.py)
_STOP_WORDS = frozenset({
    "the", "this", "that", "then", "than", "with", "from", "have",
    "will", "your", "what", "when", "where", "how", "should", "would",
    "could", "into", "need", "make", "also", "some", "more", "just",
    "about", "been", "they", "them", "their", "does", "done", "task",
    "using", "which", "these", "those", "here", "there", "after",
    "before", "please", "like", "want", "help", "create", "build",
})


async def initialize_node(
    state: KestrelState,
    *,
    learner=None,
    memory_graph=None,
    persona_learner=None,
    event_callback=None,
) -> dict[str, Any]:
    """Enrich state with memory context, lessons, and persona.

    Returns a partial state update (LangGraph merges it into the full state).
    """
    task = state["task"]
    updates: dict[str, Any] = {}

    # ── Past lessons ─────────────────────────────────────────────
    if learner:
        try:
            lesson_ctx = await learner.enrich_context(
                workspace_id=task.workspace_id,
                goal=task.goal,
            )
            if lesson_ctx:
                updates["lesson_context"] = lesson_ctx
                if event_callback:
                    lesson_count = lesson_ctx.count("\n") + 1
                    await event_callback("lessons_loaded", {
                        "count": lesson_count,
                        "preview": lesson_ctx[:150],
                    })
        except Exception as e:
            logger.warning(f"Lesson enrichment failed: {e}")

    # ── Memory graph ─────────────────────────────────────────────
    if memory_graph:
        try:
            goal_terms = [
                w.lower().strip(".,!?:;")
                for w in task.goal.split()
                if len(w) > 3 and w.lower() not in _STOP_WORDS
            ][:8]
            memory_ctx = await memory_graph.format_for_prompt(
                workspace_id=task.workspace_id,
                query_entities=goal_terms,
            )
            if memory_ctx:
                updates["memory_context"] = [memory_ctx]
                if event_callback:
                    mem_lines = [line for line in memory_ctx.split("\n") if line.strip()]
                    await event_callback("memory_recalled", {
                        "count": len(mem_lines),
                        "entities": goal_terms,
                        "preview": memory_ctx[:200],
                    })
        except Exception as e:
            logger.warning(f"Memory graph query failed: {e}")

    # ── User persona ─────────────────────────────────────────────
    if persona_learner and task.user_id:
        try:
            persona_prefs = await persona_learner.load_persona(task.user_id)
            persona_ctx = persona_learner.format_for_prompt(persona_prefs)
            if persona_ctx:
                updates["persona_context"] = persona_ctx
                if event_callback:
                    await event_callback("persona_loaded", {
                        "user_id": task.user_id,
                        "preview": persona_ctx[:200],
                    })
        except Exception as e:
            logger.warning(f"Persona loading failed: {e}")

    return updates
