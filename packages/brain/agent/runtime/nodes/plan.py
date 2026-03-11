"""
Plan node — Phase 1 of the agent loop.

Decomposes the user's goal into a structured TaskPlan (DAG of steps).
For chat-originated tasks, creates a fast-path single-step plan.

Wraps existing components:
  - TaskPlanner.create_plan()
  - ReflectionEngine.reflect() (plan red-teaming)
  - OutcomeSimulator.simulate() (pre-flight simulation)

Council gating
──────────────
Mirrors the legacy _should_skip_council() semantics exactly:
  - Skip council if complexity < 8.5 AND no HIGH-risk tools AND
    no security-sensitive keywords in step descriptions.
  - 8.5–9.0 → deliberate_lite (3 members, no debate)
  - > 9.0   → full deliberate (all members + optional debate)

Simulation gate
───────────────
When a simulation recommends aborting, we preserve the legacy behavior:
  1. Emit a SIMULATION_COMPLETE event with the simulation summary.
  2. Emit an APPROVAL_NEEDED event asking the user to explicitly override.
  3. Store the simulation warning in state so the approve node can surface it.
  4. Route to approve (not silently to council) so the user sees the warning.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from agent.types import (
    RiskLevel,
    StepStatus,
    TaskEventType,
    TaskPlan,
    TaskStatus,
    TaskStep,
)
from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.plan")


def _should_skip_council(
    task,
    plan_complexity: float,
    tool_registry,
) -> bool:
    """Decide whether council deliberation can be skipped for a plan.

    Mirrors AgentLoop._should_skip_council() exactly so LangGraph and
    legacy paths apply the same policy.

    A plan is routine when ALL of the following hold:
    - Complexity is below 8.5
    - No plan steps involve HIGH-risk tools
    - No steps mention security-sensitive operations
    """
    if plan_complexity >= 8.5:
        return False

    if not task.plan or not task.plan.steps:
        return False

    for step in task.plan.steps:
        # Only HIGH-risk tools trigger council; MEDIUM-risk tools are routine
        for tc in step.tool_calls:
            tool_name = tc.get("tool", tc.get("function", {}).get("name", ""))
            if tool_name and tool_registry:
                risk = tool_registry.get_risk_level(tool_name)
                if risk == RiskLevel.HIGH:
                    return False

        # Security-sensitive keywords in step descriptions also trigger council
        desc_lower = step.description.lower()
        if any(kw in desc_lower for kw in (
            "delete", "deploy", "credential", "secret", "admin",
            "sudo", "production", "database migration",
        )):
            return False

    logger.debug(
        f"Council skip: no HIGH-risk tools in {len(task.plan.steps)} steps "
        f"(complexity={plan_complexity:.1f})"
    )
    return True


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
    kernel_policy = state.get("kernel_policy", {})

    logger.info(
        f"plan_node entry: task.messages={len(task.messages) if task.messages else 'None/empty'}, "
        f"task.plan={'SET (steps=' + str(len(task.plan.steps)) + ')' if task.plan else 'None'}, "
        f"goal='{task.goal[:80]}'"
    )

    # ── Fast-path for simple chat tasks ─────────────────────────
    # chat_service.py pre-classifies messages:
    #   - Simple (greetings, short Q&A) → task.plan is already set
    #   - Complex (research, actions)   → task.plan is None
    # Only fast-path when the plan was pre-set. Complex chat messages
    # fall through to the full planner so they get tool access.
    if task.messages and task.plan is not None:
        logger.info(f"Chat fast-path: skipping planner for '{task.goal[:60]}'")
        plan = task.plan
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
    # Keep task.plan in sync so _should_skip_council can inspect steps
    task.plan = plan

    # ── Compute complexity score ─────────────────────────────────
    plan_complexity = float(len(plan.steps))
    try:
        from agent.model_router import estimate_complexity, classify_step
        _st = classify_step(task.goal)
        plan_complexity = estimate_complexity(task.goal, _st)
    except Exception:
        pass
    updates["plan_complexity"] = plan_complexity

    # ── Council gating — mirrors legacy _should_skip_council() ───
    # Threshold: complexity >= 7.0 AND not skippable (no HIGH-risk tools,
    # no security-sensitive keywords).  Complexity < 7.0 always skips.
    council_threshold = float(kernel_policy.get("council_threshold", 7.0))
    if (
        kernel_policy.get("use_council", True)
        and plan_complexity >= council_threshold
        and not _should_skip_council(task, plan_complexity, tool_registry)
    ):
        updates["needs_council"] = True
    else:
        updates["needs_council"] = False

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
    reflection_min_steps = int(kernel_policy.get("reflection_min_steps", 3))
    if reflection_engine and kernel_policy.get("use_reflection", True) and len(plan.steps) >= reflection_min_steps:
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
            if not reflection.should_proceed and event_callback:
                await event_callback("thinking", {
                    "content": (
                        f"⚠ Reflection flagged critical issues "
                        f"(confidence={reflection.confidence_score:.2f}). "
                        f"Proceeding with caution.\n" +
                        "\n".join(
                            f"- [{c.severity}] {c.description}"
                            for c in reflection.critique_points[:3]
                        )
                    ),
                })
        except Exception as e:
            logger.warning(f"Reflection engine failed: {e}")

    # ── Pre-flight simulation ────────────────────────────────────
    # When simulation recommends aborting, we preserve legacy semantics:
    # surface SIMULATION_COMPLETE + APPROVAL_NEEDED events and require the
    # user to explicitly override, rather than silently routing to council.
    simulation_threshold = float(kernel_policy.get("simulation_threshold", 7.0))
    if (
        simulator
        and kernel_policy.get("use_simulation", False)
        and plan_complexity >= simulation_threshold
        and len(plan.steps) > 1
    ):
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

            if event_callback:
                await event_callback("simulation_complete", {
                    "content": sim_result.summary(),
                    "should_proceed": sim_result.should_proceed,
                    "recommendation": sim_result.recommendation,
                })

            if not sim_result.should_proceed:
                # Store warning in state so approve_node can surface it
                updates["simulation_warning"] = sim_result.summary()
                # Require explicit user approval (overrides complexity-based routing)
                updates["needs_council"] = True
                if event_callback:
                    await event_callback("approval_needed", {
                        "content": (
                            "Simulation recommends aborting this plan. "
                            "Proceed anyway?"
                        ),
                        "simulation_warning": sim_result.summary(),
                    })
        except Exception as e:
            logger.warning(f"Simulation gate failed: {e}")

    return updates
