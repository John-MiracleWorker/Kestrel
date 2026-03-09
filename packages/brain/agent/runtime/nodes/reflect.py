"""
Reflect node — Phase 3 of the agent loop.

Evaluates execution results, checks evidence bindings, and decides
whether to continue execution, replan, or complete.

Wraps existing components:
  - EvidenceChain verification
  - Memory graph entity extraction and storage
  - PersonaLearner observation
"""

from __future__ import annotations

import logging
from typing import Any

from agent.types import TaskStatus
from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.reflect")


async def reflect_node(
    state: KestrelState,
    *,
    evidence_chain=None,
    memory_graph=None,
    persona_learner=None,
    provider=None,
    model: str = "",
    api_key: str = "",
    event_callback=None,
) -> dict[str, Any]:
    """Reflect on execution results and decide next action.

    Routes:
      - "done": task is complete, proceed to complete_node
      - "continue": more steps to execute, go back to execute_node
      - "replan": plan needs revision, go back to plan_node
    """
    task = state["task"]
    plan = state.get("plan") or task.plan
    updates: dict[str, Any] = {}

    # ── Check plan completion ────────────────────────────────────
    if plan and plan.is_complete:
        # All steps finished — proceed to completion
        updates["route"] = "done"
        updates["status"] = TaskStatus.REFLECTING.value

        # ── Persist evidence chain ───────────────────────────────
        if evidence_chain:
            try:
                await evidence_chain.persist()
            except Exception as e:
                logger.warning(f"Evidence chain persistence failed: {e}")

        # ── Update memory graph with task entities ───────────────
        if memory_graph and task.result and provider:
            try:
                from agent.core.memory_graph import extract_entities_llm
                _entities, _relations = await extract_entities_llm(
                    provider=provider,
                    model=model,
                    api_key=api_key,
                    user_message=task.goal,
                    assistant_response=task.result,
                )
                if _entities:
                    await memory_graph.extract_and_store(
                        conversation_id=task.id,
                        workspace_id=task.workspace_id,
                        entities=_entities,
                        relations=_relations,
                    )
                    logger.info(
                        f"Memory graph: stored {len(_entities)} entities, "
                        f"{len(_relations)} relations from task {task.id}"
                    )
            except Exception as e:
                logger.warning(f"Memory graph update failed: {e}")

        # ── Observe persona signals ──────────────────────────────
        if persona_learner and task.result:
            try:
                await persona_learner.observe_communication(
                    user_id=task.user_id,
                    user_message=task.goal,
                    agent_response=task.result[:500],
                )
                await persona_learner.observe_session_timing(task.user_id)
            except Exception as e:
                logger.warning(f"Persona observation failed: {e}")

    elif plan and not plan.is_complete:
        # Check if we should replan based on drift
        from agent.types import StepStatus
        recent = [s for s in plan.steps if s.status in (StepStatus.COMPLETE, StepStatus.FAILED)]
        consecutive_failures = 0
        for s in reversed(recent[-3:]):
            if s.status == StepStatus.FAILED:
                consecutive_failures += 1
            else:
                break

        if consecutive_failures >= 2 and plan.revision_count < 3:
            updates["route"] = "replan"
            plan.revision_count += 1
        else:
            updates["route"] = "continue"
    else:
        updates["route"] = "done"

    return updates
