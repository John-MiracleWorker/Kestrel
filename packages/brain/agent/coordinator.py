from __future__ import annotations
"""
Multi-Agent Coordinator — orchestrates specialist sub-agents.

The coordinator can delegate subtasks to focused sub-agents, each with
a filtered tool registry and tailored persona. This enables complex tasks
to be decomposed across specialists:

  - Researcher: web_search, web_browse, memory tools
  - Coder: code_execute, file tools
  - Analyst: database tools, memory tools, code_execute
  - Reviewer: file_read, memory_search (read-only validation)
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from agent.types import (
    AgentTask,
    GuardrailConfig,
    TaskStatus,
)

logger = logging.getLogger("brain.agent.coordinator")


# ── Specialist Definitions ───────────────────────────────────────────

@dataclass
class SpecialistConfig:
    """Configuration for a specialist sub-agent."""
    name: str
    persona: str
    allowed_tools: list[str]
    max_iterations: int = 15
    max_tool_calls: int = 30


SPECIALISTS = {
    "researcher": SpecialistConfig(
        name="Researcher",
        persona=(
            "You are a research specialist. Your job is to find, validate, "
            "and synthesize information from the web and knowledge base. "
            "Focus on accuracy, cite sources, and organize findings clearly."
        ),
        allowed_tools=[
            "web_search", "web_browse", "memory_search", "memory_store",
            "ask_human", "task_complete",
        ],
    ),
    "coder": SpecialistConfig(
        name="Coder",
        persona=(
            "You are a coding specialist. Your job is to write, modify, "
            "and debug code. Follow best practices, write clean code, "
            "and test your changes before reporting completion."
        ),
        allowed_tools=[
            "code_execute", "file_read", "file_write", "file_list",
            "memory_search", "ask_human", "task_complete",
        ],
    ),
    "analyst": SpecialistConfig(
        name="Data Analyst",
        persona=(
            "You are a data analysis specialist. Your job is to query "
            "databases, analyze data, produce insights, and create "
            "visualizations. Be thorough and highlight key findings."
        ),
        allowed_tools=[
            "database_query", "database_mutate", "code_execute",
            "memory_search", "memory_store", "file_write",
            "ask_human", "task_complete",
        ],
    ),
    "reviewer": SpecialistConfig(
        name="Reviewer",
        persona=(
            "You are a review specialist. Your job is to validate work "
            "done by others — check code for bugs, verify data accuracy, "
            "and ensure quality standards. You have READ-ONLY access."
        ),
        allowed_tools=[
            "file_read", "file_list", "memory_search", "database_query",
            "ask_human", "task_complete",
        ],
    ),
    "explorer": SpecialistConfig(
        name="Code Explorer",
        persona=(
            "You are a code exploration specialist. Your job is to deeply "
            "analyze codebases using host filesystem tools. Strategy:\n"
            "1. Use project_recall(name) to check for cached context\n"
            "2. Use host_tree(path) for full project structure (ONE call)\n"
            "3. Use host_batch_read(paths) to read multiple key files at once\n"
            "4. Use host_find(pattern) to locate specific files\n"
            "5. Use host_search(query) to grep across files\n"
            "Report your findings clearly and thoroughly."
        ),
        allowed_tools=[
            "project_recall", "host_tree", "host_batch_read", "host_read",
            "host_find", "host_search", "host_list",
            "memory_store", "memory_search",
            "task_complete",
        ],
        max_iterations=20,
        max_tool_calls=40,
    ),
}


class Coordinator:
    """
    Orchestrates multi-agent delegation by spawning specialist sub-agents.

    The coordinator wraps an AgentLoop and creates child tasks with filtered
    tool registries and specialized personas.
    """

    def __init__(self, agent_loop, persistence, tool_registry, event_callback=None):
        self._loop = agent_loop
        self._persistence = persistence
        self._full_registry = tool_registry
        self._event_callback = event_callback

    async def _emit(self, activity_type: str, data: dict):
        """Emit an agent activity event to the UI."""
        if self._event_callback:
            await self._event_callback(activity_type, data)

    async def delegate(
        self,
        parent_task: AgentTask,
        goal: str,
        specialist_type: str,
    ) -> str:
        """
        Spawn a specialist sub-agent to handle a subtask.

        Returns the sub-agent's result string when complete.
        """
        spec = SPECIALISTS.get(specialist_type)
        if not spec:
            return f"Unknown specialist type: {specialist_type}. Available: {list(SPECIALISTS.keys())}"

        await self._emit("delegation_started", {
            "specialist": spec.name,
            "type": specialist_type,
            "goal": goal[:200],
            "tools": spec.allowed_tools,
        })

        # Create child task
        import uuid
        child_task = AgentTask(
            id=str(uuid.uuid4()),
            user_id=parent_task.user_id,
            workspace_id=parent_task.workspace_id,
            conversation_id=parent_task.conversation_id,
            goal=f"[{spec.name}] {goal}",
            status=TaskStatus.PLANNING,
            config=GuardrailConfig(
                max_iterations=spec.max_iterations,
                max_tool_calls=spec.max_tool_calls,
                max_tokens=parent_task.config.max_tokens // 3,
                max_wall_time_seconds=min(
                    parent_task.config.max_wall_time_seconds // 2, 300
                ),
                auto_approve_risk=parent_task.config.auto_approve_risk,
            ),
            parent_task_id=parent_task.id,
        )

        # Track child in parent
        if not hasattr(parent_task, 'child_task_ids'):
            parent_task.child_task_ids = []
        parent_task.child_task_ids.append(child_task.id)

        # Save child task
        await self._persistence.save_task(child_task)
        await self._persistence.update_task(parent_task)

        logger.info(
            f"Delegating to {spec.name}: '{goal}' "
            f"(child={child_task.id}, parent={parent_task.id})"
        )

        # Create a filtered tool registry for the specialist
        filtered_registry = self._full_registry.filter(spec.allowed_tools)

        # Build a child loop with filtered tools
        from agent.loop import AgentLoop
        child_loop = AgentLoop(
            provider=self._loop._provider,
            tool_registry=filtered_registry,
            guardrails=self._loop._guardrails,
            persistence=self._persistence,
            model=self._loop._model,
            learner=self._loop._learner,
        )

        # Execute child and collect result
        result_parts = []
        try:
            async for event in child_loop.run(child_task):
                event_name = event.type.name if hasattr(event.type, 'name') else str(event.type)

                # Forward ALL child events so the parent stream can render them
                if event_name == "THINKING":
                    await self._emit("delegation_progress", {
                        "specialist": spec.name,
                        "status": "thinking",
                        "thinking": (event.content or "")[:200],
                    })
                elif event_name == "TOOL_CALLED":
                    await self._emit("delegation_progress", {
                        "specialist": spec.name,
                        "tool": event.tool_name,
                        "tool_args": (event.tool_args or "")[:200],
                        "status": "tool_calling",
                    })
                elif event_name == "TOOL_RESULT":
                    await self._emit("delegation_progress", {
                        "specialist": spec.name,
                        "tool": event.tool_name,
                        "tool_result": (event.tool_result or "")[:300],
                        "status": "tool_result",
                    })
                elif event_name == "STEP_COMPLETE":
                    await self._emit("delegation_progress", {
                        "specialist": spec.name,
                        "status": "step_done",
                        "content": (event.content or "")[:300],
                    })
                elif event_name in ("TASK_COMPLETE", "TASK_FAILED"):
                    result_parts.append(event.content or "")
        except Exception as e:
            logger.error(f"Sub-agent {child_task.id} failed: {e}")
            await self._emit("delegation_complete", {
                "specialist": spec.name,
                "status": "failed",
                "result": str(e)[:200],
            })
            return f"Sub-agent ({spec.name}) failed: {e}"

        result = "\n".join(result_parts) if result_parts else "Sub-agent completed with no output."
        await self._emit("delegation_complete", {
            "specialist": spec.name,
            "status": "complete",
            "result": result[:300],
        })
        return result

    async def delegate_parallel(
        self,
        parent_task: AgentTask,
        subtasks: list[dict],
    ) -> list[str]:
        """
        Run multiple specialist sub-agents in parallel.

        Each subtask dict should have 'goal' and 'specialist' keys.
        Returns a list of result strings, one per subtask.
        """
        import asyncio

        max_parallel = 5
        if len(subtasks) > max_parallel:
            subtasks = subtasks[:max_parallel]
            logger.warning(f"Capped parallel subtasks to {max_parallel}")

        await self._emit("parallel_delegation_started", {
            "count": len(subtasks),
            "subtasks": [
                {"goal": s.get("goal", "")[:100], "specialist": s.get("specialist", "explorer")}
                for s in subtasks
            ],
        })

        async def _run_one(subtask: dict, index: int) -> str:
            goal = subtask.get("goal", "")
            specialist = subtask.get("specialist", "explorer")
            try:
                result = await self.delegate(parent_task, goal, specialist)
                return result
            except Exception as e:
                return f"Subtask {index} failed: {e}"

        coros = [_run_one(s, i) for i, s in enumerate(subtasks)]
        results = await asyncio.gather(*coros, return_exceptions=True)

        # Convert exceptions to strings
        final = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                final.append(f"Subtask {i} error: {r}")
            else:
                final.append(str(r))

        await self._emit("parallel_delegation_complete", {
            "count": len(final),
            "successful": sum(1 for r in final if not r.startswith("Subtask")),
        })

        return final

    def get_specialist_info(self) -> list[dict]:
        """Return descriptions of available specialists for tool schema."""
        return [
            {
                "type": key,
                "name": spec.name,
                "persona": spec.persona,
                "tools": spec.allowed_tools,
            }
            for key, spec in SPECIALISTS.items()
        ]
