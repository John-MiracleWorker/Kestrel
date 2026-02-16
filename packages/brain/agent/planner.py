"""
Task Planner — LLM-based goal decomposition into executable step DAGs.

Uses the configured LLM provider to decompose a high-level user goal
into concrete, actionable steps with tool hints and dependencies.
Supports re-planning after step completion to adapt to new information.
"""

import json
import logging
from typing import Optional

from agent.types import (
    TaskPlan,
    TaskStep,
    StepStatus,
    ToolDefinition,
)

logger = logging.getLogger("brain.agent.planner")

# ── Planning Prompts ─────────────────────────────────────────────────

PLAN_SYSTEM_PROMPT = """\
You are a task planner for an autonomous AI agent. Your job is to decompose
a user's goal into concrete, actionable steps.

Rules:
1. Each step should be small enough to accomplish with 1-3 tool calls.
2. Steps should be ordered logically with explicit dependencies.
3. Include which tools each step will likely need (as hints, not constraints).
4. Steps must be independently verifiable — the agent should know when each is done.
5. Be practical: prefer fewer, well-defined steps over many tiny ones.
6. If the goal is simple enough for 1-2 steps, don't over-decompose it.

Available tools:
{tool_descriptions}

Output your plan as JSON with this exact schema:
{{
    "reasoning": "Brief explanation of your decomposition strategy",
    "steps": [
        {{
            "id": "step_1",
            "description": "Clear description of what this step accomplishes",
            "expected_tools": ["tool_name_1", "tool_name_2"],
            "depends_on": []
        }},
        {{
            "id": "step_2",
            "description": "...",
            "expected_tools": ["tool_name_3"],
            "depends_on": ["step_1"]
        }}
    ]
}}

IMPORTANT: Output ONLY valid JSON. No markdown, no explanation outside the JSON."""

REPLAN_SYSTEM_PROMPT = """\
You are a task planner for an autonomous AI agent. The agent is partway through
executing a plan and needs to revise the remaining steps based on new information.

Original goal: {goal}
Completed steps and their results:
{completed_steps}

Current remaining steps:
{remaining_steps}

New observations:
{observations}

Revise the remaining steps. You may add, remove, or modify steps.
Keep completed step IDs stable. Output the FULL revised plan (including completed steps).
Use the same JSON schema as before.

Available tools:
{tool_descriptions}

Output ONLY valid JSON."""


class TaskPlanner:
    """LLM-based task planner that decomposes goals into step DAGs."""

    def __init__(self, provider, model: str = ""):
        """
        Args:
            provider: CloudProvider or LocalProvider with .generate() method
            model: Model override (empty = use provider default)
        """
        self._provider = provider
        self._model = model

    async def create_plan(
        self,
        goal: str,
        available_tools: list[ToolDefinition],
        context: str = "",
    ) -> TaskPlan:
        """
        Decompose a goal into an executable plan.

        Args:
            goal: The user's high-level goal
            available_tools: Tools the agent can use
            context: Optional additional context (workspace info, history)
        """
        tool_descriptions = self._format_tool_descriptions(available_tools)

        system_prompt = PLAN_SYSTEM_PROMPT.format(
            tool_descriptions=tool_descriptions,
        )

        user_message = f"Goal: {goal}"
        if context:
            user_message += f"\n\nAdditional context:\n{context}"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        response = await self._provider.generate(
            messages=messages,
            model=self._model,
            temperature=0.3,    # Low temp for structured output
            max_tokens=2048,
        )

        plan = self._parse_plan_response(response, goal)
        logger.info(
            f"Created plan for '{goal}': {len(plan.steps)} steps",
        )
        return plan

    async def revise_plan(
        self,
        plan: TaskPlan,
        observations: str,
        available_tools: list[ToolDefinition],
    ) -> TaskPlan:
        """
        Revise a plan based on new observations from completed steps.

        Args:
            plan: Current plan with some completed steps
            observations: What was learned during execution
            available_tools: Tools the agent can use
        """
        completed = []
        remaining = []
        for step in plan.steps:
            if step.status == StepStatus.COMPLETE:
                completed.append(
                    f"- [{step.id}] {step.description}\n  Result: {step.result or 'OK'}"
                )
            elif step.status in (StepStatus.PENDING, StepStatus.IN_PROGRESS):
                remaining.append(
                    f"- [{step.id}] {step.description}"
                )

        system_prompt = REPLAN_SYSTEM_PROMPT.format(
            goal=plan.goal,
            completed_steps="\n".join(completed) or "(none)",
            remaining_steps="\n".join(remaining) or "(none)",
            observations=observations,
            tool_descriptions=self._format_tool_descriptions(available_tools),
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Revise the plan based on the observations above."},
        ]

        response = await self._provider.generate(
            messages=messages,
            model=self._model,
            temperature=0.3,
            max_tokens=2048,
        )

        revised = self._parse_plan_response(response, plan.goal)
        revised.revision_count = plan.revision_count + 1

        # Preserve completed step statuses
        completed_ids = {s.id for s in plan.steps if s.status == StepStatus.COMPLETE}
        for step in revised.steps:
            if step.id in completed_ids:
                original = next(s for s in plan.steps if s.id == step.id)
                step.status = original.status
                step.result = original.result
                step.completed_at = original.completed_at
                step.attempts = original.attempts

        logger.info(
            f"Revised plan (rev {revised.revision_count}): "
            f"{len(revised.steps)} steps"
        )
        return revised

    def _parse_plan_response(self, response: str, goal: str) -> TaskPlan:
        """Parse the LLM's JSON response into a TaskPlan."""
        # Strip markdown code fences if present
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first and last lines (fences)
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
            text = text.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse plan JSON: {e}")
            # Fallback: single-step plan
            return TaskPlan(
                goal=goal,
                steps=[
                    TaskStep(
                        id="step_1",
                        index=0,
                        description=f"Execute the goal directly: {goal}",
                        expected_tools=[],
                    )
                ],
                reasoning="Failed to decompose — executing as single step",
            )

        steps = []
        for i, step_data in enumerate(data.get("steps", [])):
            steps.append(TaskStep(
                id=step_data.get("id", f"step_{i + 1}"),
                index=i,
                description=step_data.get("description", ""),
                expected_tools=step_data.get("expected_tools", []),
                depends_on=step_data.get("depends_on", []),
            ))

        return TaskPlan(
            goal=goal,
            steps=steps,
            reasoning=data.get("reasoning", ""),
        )

    def _format_tool_descriptions(self, tools: list[ToolDefinition]) -> str:
        """Format tool list for the planning prompt."""
        lines = []
        for tool in tools:
            params = ", ".join(
                f"{k}: {v.get('type', 'any')}"
                for k, v in tool.parameters.get("properties", {}).items()
            )
            lines.append(f"- {tool.name}({params}): {tool.description}")
        return "\n".join(lines)
