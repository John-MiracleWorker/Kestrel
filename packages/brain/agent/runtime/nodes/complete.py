"""
Complete node — final phase of the agent loop.

Builds the task result summary, persists final state, emits
completion events, and triggers post-task learning.

Also handles memory graph entity extraction and persona observation,
which must happen after task.result is fully assembled.

Wraps existing components:
  - TaskLearner.extract_lessons()
  - MetricsCollector
  - MemoryGraph.extract_and_store()
  - PersonaLearner.observe_communication()
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from agent.types import StepStatus, TaskEventType, TaskStatus
from agent.runtime.state import KestrelState

logger = logging.getLogger("brain.agent.runtime.nodes.complete")

# Matches ```json ... ``` fenced blocks
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _extract_human_text(raw: str) -> str:
    """Extract human-readable text from a step result that may contain raw JSON tool calls.

    When the LLM outputs a tool call (like task_complete) as a text/JSON block
    instead of a proper function call, the raw JSON ends up in step.result.
    This function detects that pattern and extracts the summary field.
    """
    if not raw or not raw.strip():
        return raw or ""

    stripped = raw.strip()

    # Try to parse directly as JSON (no fencing)
    for candidate in [stripped]:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                # Pattern: {"name": "task_complete", "arguments": {"summary": "..."}}
                if parsed.get("name") == "task_complete":
                    args = parsed.get("arguments") or parsed.get("args") or {}
                    summary = args.get("summary", "")
                    if summary:
                        return summary
                # Pattern: {"summary": "..."}
                if "summary" in parsed and len(parsed) <= 3:
                    return parsed["summary"]
        except (json.JSONDecodeError, TypeError):
            pass

    # Try to extract from ```json ... ``` fenced blocks
    for match in _JSON_FENCE_RE.finditer(stripped):
        try:
            parsed = json.loads(match.group(1))
            if isinstance(parsed, dict):
                if parsed.get("name") == "task_complete":
                    args = parsed.get("arguments") or parsed.get("args") or {}
                    summary = args.get("summary", "")
                    if summary:
                        return summary
                if "summary" in parsed and len(parsed) <= 3:
                    return parsed["summary"]
        except (json.JSONDecodeError, TypeError):
            continue

    return raw


async def complete_node(
    state: KestrelState,
    *,
    persistence=None,
    learner=None,
    metrics=None,
    evidence_chain=None,
    memory_graph=None,
    persona_learner=None,
    provider=None,
    model: str = "",
    api_key: str = "",
    event_callback=None,
) -> dict[str, Any]:
    """Finalize the task: build result, persist, learn, emit events."""
    task = state["task"]
    plan = state.get("plan") or task.plan
    updates: dict[str, Any] = {}

    if state.get("messages") and not task.messages:
        task.messages = state.get("messages", [])

    # ── Handle cancelled/denied tasks ────────────────────────────
    if state.get("approval_granted") is False:
        task.status = TaskStatus.CANCELLED
        task.error = task.error or "Task denied by user."
        if persistence:
            await persistence.update_task(task)
        updates["status"] = TaskStatus.CANCELLED.value
        return updates

    # ── Build final result summary ───────────────────────────────
    task.status = TaskStatus.COMPLETE
    task.completed_at = datetime.now(timezone.utc)

    if plan:
        results = []
        for s in plan.steps:
            if s.result:
                cleaned = _extract_human_text(s.result)
                if task.messages:
                    results.append(cleaned)
                else:
                    results.append(f"**{s.description}**: {cleaned}")
        task.result = "\n".join(results) if results else "Task completed successfully."
    else:
        task.result = "Task completed successfully."

    if persistence:
        await persistence.update_task(task)

    # ── Emit completion events ───────────────────────────────────
    if event_callback:
        # Token usage metrics
        metrics_data = metrics.metrics.to_dict() if metrics else {}
        await event_callback("token_usage", {
            "total_tokens": task.token_usage,
            "iterations": task.iterations,
            "tool_calls": task.tool_calls_count,
            "estimated_cost_usd": metrics_data.get("estimated_cost_usd", 0),
            "llm_calls": metrics_data.get("llm_calls", 0),
            "avg_tool_time_ms": metrics_data.get("avg_tool_time_ms", 0),
            "total_elapsed_ms": metrics_data.get("total_elapsed_ms", 0),
        })

        # Evidence summary
        if evidence_chain and evidence_chain._decisions:
            await event_callback("evidence_summary", {
                "decision_count": len(evidence_chain._decisions),
                "decisions": [
                    {"type": d.decision_type.value, "description": d.description[:80]}
                    for d in evidence_chain._decisions[:5]
                ],
            })

    # ── Update memory graph with task entities ───────────────────
    # Runs here (not in reflect_node) so task.result is guaranteed to be set.
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

    # ── Observe persona signals ──────────────────────────────────
    # Runs here so task.result is available for observation.
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

    # ── Post-task learning ───────────────────────────────────────
    if learner:
        _is_trivial = (
            task.iterations <= 2
            and task.tool_calls_count < 5
            and task.status == TaskStatus.COMPLETE
            and (not plan or len(plan.steps) <= 1)
        )
        if not _is_trivial:
            try:
                await learner.extract_lessons(task)
            except Exception as e:
                logger.warning(f"Post-task learning failed: {e}")

    updates["status"] = TaskStatus.COMPLETE.value
    return updates
