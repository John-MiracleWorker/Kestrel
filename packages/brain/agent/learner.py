"""
Post-Task Learning — self-improvement through structured lesson extraction.

After every task completes, the agent:
  1. Asks the LLM to extract lessons learned from the execution trace
  2. Stores structured lessons in the workspace knowledge base
  3. Before future tasks, retrieves relevant past lessons to improve planning

This is what makes Kestrel genuinely improve over time — it learns from
both successes and failures, building a growing library of reusable insights.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("brain.agent.learner")


@dataclass
class Lesson:
    """A structured lesson extracted from a completed task."""

    category: str  # "pattern", "pitfall", "shortcut", "tool_usage"
    summary: str
    details: str
    tools_used: list[str] = field(default_factory=list)
    success: bool = True
    confidence: float = 0.8  # 0-1 how confident the lesson is
    tags: list[str] = field(default_factory=list)
    source_task_id: str = ""
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "summary": self.summary,
            "details": self.details,
            "tools_used": self.tools_used,
            "success": self.success,
            "confidence": self.confidence,
            "tags": self.tags,
            "source_task_id": self.source_task_id,
            "created_at": self.created_at or datetime.now(timezone.utc).isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Lesson":
        return cls(
            category=data.get("category", "pattern"),
            summary=data.get("summary", ""),
            details=data.get("details", ""),
            tools_used=data.get("tools_used", []),
            success=data.get("success", True),
            confidence=data.get("confidence", 0.8),
            tags=data.get("tags", []),
            source_task_id=data.get("source_task_id", ""),
            created_at=data.get("created_at", ""),
        )


# ── Extraction Prompt ────────────────────────────────────────────────

EXTRACT_LESSONS_PROMPT = """\
You are analyzing a completed agent task to extract reusable lessons.

Task Goal: {goal}
Task Status: {status}
Total Steps: {total_steps}
Tool Calls Made: {tool_calls}
{error_section}

Step Execution Summary:
{step_summary}

Extract 1-5 structured lessons from this execution. Focus on:
- Patterns that worked well and should be reused
- Pitfalls or mistakes to avoid in the future
- Shortcuts discovered (faster ways to accomplish things)
- Tool usage insights (which tools work best for what)

Respond with a JSON array of lesson objects:
```json
[
  {{
    "category": "pattern|pitfall|shortcut|tool_usage",
    "summary": "One-line summary of the lesson",
    "details": "2-3 sentence explanation with specifics",
    "tools_used": ["tool1", "tool2"],
    "success": true,
    "confidence": 0.9,
    "tags": ["tag1", "tag2"]
  }}
]
```

Only output the JSON array, no other text.
"""


class TaskLearner:
    """
    Extracts and stores lessons from completed tasks.

    Integrates with the WorkingMemory knowledge tier for persistence
    and retrieval of past lessons during planning.
    """

    def __init__(self, provider, model: str, working_memory):
        self._provider = provider
        self._model = model
        self._memory = working_memory

    async def extract_lessons(self, task) -> list[Lesson]:
        """
        Analyze a completed task and extract structured lessons.
        Returns the lessons and stores them in the knowledge base.
        """
        from agent.types import TaskStatus

        if task.status not in (TaskStatus.COMPLETE, TaskStatus.FAILED):
            return []

        # Build step summary
        step_lines = []
        all_tools_used = set()
        if task.plan:
            for step in task.plan.steps:
                status_icon = "✅" if step.status.value == "complete" else "❌"
                step_lines.append(
                    f"  {status_icon} {step.description}"
                )
                if step.result:
                    step_lines.append(f"     Result: {step.result[:200]}")
                if step.error:
                    step_lines.append(f"     Error: {step.error[:200]}")
                for tc in step.tool_calls:
                    all_tools_used.add(tc.get("tool", "unknown"))

        error_section = ""
        if task.error:
            error_section = f"\nTask Error: {task.error}"

        prompt = EXTRACT_LESSONS_PROMPT.format(
            goal=task.goal,
            status=task.status.value,
            total_steps=len(task.plan.steps) if task.plan else 0,
            tool_calls=task.tool_calls_count,
            error_section=error_section,
            step_summary="\n".join(step_lines) or "(no steps recorded)",
        )

        try:
            response = await self._provider.generate(
                messages=[
                    {"role": "system", "content": "You extract structured lessons from agent task executions."},
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                temperature=0.3,
                max_tokens=2048,
            )

            content = response.get("content", "")
            # Strip markdown code fences
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            lessons_data = json.loads(content.strip())
            lessons = []
            for ld in lessons_data:
                lesson = Lesson.from_dict(ld)
                lesson.source_task_id = task.id
                lesson.created_at = datetime.now(timezone.utc).isoformat()
                lessons.append(lesson)

            # Store in knowledge base
            await self._store_lessons(task.workspace_id, lessons)

            logger.info(
                f"Extracted {len(lessons)} lessons from task {task.id}"
            )
            return lessons

        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(f"Failed to parse lessons: {e}")
            return []
        except Exception as e:
            logger.error(f"Lesson extraction failed: {e}")
            return []

    async def enrich_context(
        self,
        workspace_id: str,
        goal: str,
        max_lessons: int = 5,
    ) -> str:
        """
        Retrieve relevant past lessons to inject into the planning prompt.
        Returns a formatted string of lessons, or empty string if none found.
        """
        if not self._memory:
            return ""

        try:
            results = await self._memory.knowledge_search(
                workspace_id=workspace_id,
                query=f"agent lessons for: {goal}",
                top_k=max_lessons,
            )

            if not results:
                return ""

            lesson_lines = ["## Lessons from Past Tasks\n"]
            for r in results:
                content = r.get("content", "")
                try:
                    lesson = json.loads(content)
                    icon = "✅" if lesson.get("success", True) else "⚠️"
                    lesson_lines.append(
                        f"{icon} **{lesson.get('summary', '')}**\n"
                        f"   {lesson.get('details', '')}\n"
                    )
                except json.JSONDecodeError:
                    lesson_lines.append(f"- {content[:200]}\n")

            return "\n".join(lesson_lines)

        except Exception as e:
            logger.warning(f"Failed to retrieve lessons: {e}")
            return ""

    async def _store_lessons(
        self,
        workspace_id: str,
        lessons: list[Lesson],
    ) -> None:
        """Persist lessons to the knowledge base."""
        if not self._memory:
            return

        for lesson in lessons:
            await self._memory.knowledge_store(
                workspace_id=workspace_id,
                content=json.dumps(lesson.to_dict()),
                source="agent_lesson",
                metadata={
                    "category": lesson.category,
                    "tags": lesson.tags,
                    "success": lesson.success,
                    "source_task_id": lesson.source_task_id,
                },
            )
