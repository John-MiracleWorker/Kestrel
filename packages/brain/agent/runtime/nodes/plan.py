"""
Plan node — Phase 1 of the agent loop.

Decomposes the user's goal into a structured TaskPlan (DAG of steps).
For chat-originated tasks, creates a fast-path single-step plan.

Wraps existing components:
  - TaskPlanner.create_plan()
  - ReflectionEngine.reflect() (plan red-teaming)
  - OutcomeSimulator.simulate() (pre-flight simulation)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from agent.types import (
    StepStatus,
    TaskEvent,
    TaskEventType,
    TaskPlan,
    TaskStatus,
    TaskStep,
)
from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.plan")


async def plan_node(
    state: KestrelState,
    *,
    planner=None,
    tool_registry=None,
    reflection_engine=None,
    simulator=None,
    evidence_chain=None,
    event_callback=None,
) -> dict[str, Any]:
    """Create or revise the task plan.

    Returns partial state update with the new plan and complexity score.
    """
    task = state["task"]
    updates: dict[str, Any] = {}

    # ── Fast-path for chat tasks ─────────────────────────────────
    if task.messages:
        logger.info(f"Chat fast-path: skipping planner for '{task.goal[:60]}'")
        plan = TaskPlan(
            goal=task.goal,
            steps=[TaskStep(
                index=0,
                description=task.goal[:200],
                status=StepStatus.PENDING,
            )],
            reasoning="Chat-originated task — direct execution",
        )
        updates["plan"] = plan
        updates["plan_complexity"] = 1.0
        updates["needs_council"] = False
        return updates

    # ── Build context ────────────────────────────────────────────
    context_parts = [f"Workspace: {task.workspace_id}"]
    if task.conversation_id:
        context_parts.append(f"Conversation: {task.conversation_id}")

    workspace_file = os.path.expanduser("~/.kestrel/WORKSPACE.md")
    if os.path.exists(workspace_file):
        try:
            with open(workspace_file, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content:
                context_parts.append("\n=== System Workspace Context ===")
                context_parts.append(content)
                context_parts.append("================================\n")
        except Exception as e:
            logger.warning(f"Failed to read WORKSPACE.md: {e}")

    lesson_ctx = state.get("lesson_context", "")
    memory_ctx = "\n".join(state.get("memory_context", []))

    if lesson_ctx:
        context_parts.append(lesson_ctx)
    if memory_ctx:
        # Deduplicate memory vs lesson lines
        if lesson_ctx:
            lesson_fps = {
                line.strip().lower()[:60]
                for line in lesson_ctx.splitlines()
                if line.strip()
            }
            deduped = "\n".join(
                line for line in memory_ctx.splitlines()
                if line.strip().lower()[:60] not in lesson_fps
            )
            if deduped.strip():
                context_parts.append(deduped)
        else:
            context_parts.append(memory_ctx)

    context = "\n".join(context_parts)

    # ── Generate plan ────────────────────────────────────────────
    try:
        plan = await planner.create_plan(
            goal=task.goal,
            available_tools=tool_registry.list_tools(),
            context=context,
        )
    except Exception as e:
        logger.warning(f"Planning failed, using single-step fallback: {e}")
        plan = TaskPlan(
            goal=task.goal,
            steps=[TaskStep(
                index=0,
                description=f"Execute the goal directly: {task.goal[:200]}",
                status=StepStatus.PENDING,
            )],
            reasoning=f"Planning failed ({e}) — executing as single step",
        )

    updates["plan"] = plan

    # ── Compute complexity score ─────────────────────────────────
    plan_complexity = float(len(plan.steps))
    try:
        from agent.model_router import estimate_complexity, classify_step
        _st = classify_step(task.goal)
        plan_complexity = estimate_complexity(task.goal, _st)
    except Exception:
        pass
    updates["plan_complexity"] = plan_complexity

    # Council threshold: >= 7.0 complexity AND has HIGH-risk tools
    updates["needs_council"] = plan_complexity >= 7.0

    if event_callback:
        await event_callback("plan_created", {
            "step_count": len(plan.steps),
            "steps": [
                {"index": s.index, "description": s.description[:100]}
                for s in plan.steps[:6]
            ],
        })

    # ── Evidence recording ───────────────────────────────────────
    if evidence_chain:
        evidence_chain.record_plan_decision(
            plan_summary=f"Created {len(plan.steps)}-step plan for: {task.goal[:100]}",
            reasoning=f"Decomposed goal into {len(plan.steps)} steps",
            confidence=0.7,
        )

    # ── Red-team reflection ──────────────────────────────────────
    if reflection_engine and len(plan.steps) > 2:
        try:
            plan_text = json.dumps(plan.to_dict())
            reflection = await reflection_engine.reflect(
                plan=plan_text,
                task_goal=task.goal,
            )
            logger.info(
                f"Reflection: confidence={reflection.confidence_score:.2f} "
                f"risk={reflection.estimated_risk_level} "
                f"proceed={reflection.should_proceed}"
            )
            if evidence_chain:
                evidence_chain.record_plan_decision(
                    plan_summary=f"Reflection: {reflection.estimated_risk_level} risk",
                    reasoning=reflection.confidence_justification[:200],
                    confidence=reflection.confidence_score,
                )
        except Exception as e:
            logger.warning(f"Reflection engine failed: {e}")

    # ── Pre-flight simulation ────────────────────────────────────
    if simulator and len(plan.steps) > 1:
        try:
            sim_result = await simulator.simulate(
                plan=plan,
                tool_names=[t.name for t in tool_registry.list_tools()],
            )
            if evidence_chain:
                evidence_chain.record_plan_decision(
                    plan_summary=f"Simulation: {sim_result.recommendation}",
                    reasoning=sim_result.summary(),
                    confidence=0.8 if sim_result.should_proceed else 0.3,
                )
            if not sim_result.should_proceed:
                updates["needs_council"] = True  # Force council/approval
        except Exception as e:
            logger.warning(f"Simulation gate failed: {e}")

    return updates
