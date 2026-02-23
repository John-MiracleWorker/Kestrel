"""
Post-Task Learning — self-improvement through structured lesson extraction.

After every task completes, the agent:
  1. Asks the LLM to extract lessons learned from the execution trace
  2. Deduplicates against existing lessons (same category + similar summary)
  3. Stores unique lessons in the workspace knowledge base
  4. Reinforces confidence of existing lessons when duplicates are found
  5. Before future tasks, retrieves relevant past lessons to improve planning

Enhancements:
  - Lesson deduplication via summary fingerprinting and semantic matching
    to prevent the knowledge base from accumulating redundant entries.
  - Confidence reinforcement: when a near-duplicate is found, the existing
    lesson's confidence is boosted (capped at 1.0) instead of storing a copy.

This is what makes Kestrel genuinely improve over time — it learns from
both successes and failures, building a growing library of reusable insights.
"""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("brain.agent.learner")

# Minimum Jaccard similarity for considering two lesson summaries as duplicates
DEDUP_SIMILARITY_THRESHOLD = 0.6
# Confidence boost when a duplicate lesson reinforces an existing one
CONFIDENCE_REINFORCEMENT = 0.05


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


def _lesson_fingerprint(category: str, summary: str) -> str:
    """
    Generate a deterministic fingerprint for a lesson based on its
    category and normalized summary. Used for fast exact-match dedup.
    """
    normalized = " ".join(summary.lower().split())
    key = f"{category}:{normalized}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _summary_similarity(a: str, b: str) -> float:
    """
    Compute Jaccard similarity between two lesson summaries based on
    word-level token sets. Fast and effective for short text comparison.
    """
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union)


class TaskLearner:
    """
    Extracts and stores lessons from completed tasks.

    Integrates with the WorkingMemory knowledge tier for persistence
    and retrieval of past lessons during planning.

    Includes deduplication: before storing a new lesson, checks existing
    lessons for duplicates (same category + similar summary). Duplicates
    reinforce the existing lesson's confidence instead of creating noise.
    """

    def __init__(self, provider, model: str, working_memory):
        self._provider = provider
        self._model = model
        self._memory = working_memory
        # In-memory fingerprint cache to speed up dedup within a session
        self._seen_fingerprints: dict[str, str] = {}  # fingerprint → lesson summary

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

            # generate() returns a plain string; generate_with_tools() returns a dict
            content = response if isinstance(response, str) else response.get("content", "")
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

    async def _is_duplicate(
        self,
        workspace_id: str,
        lesson: Lesson,
    ) -> bool:
        """
        Check if a lesson is a duplicate of an existing one.

        Uses two-tier dedup:
          1. Fast fingerprint check (exact match on normalized summary)
          2. Semantic similarity check against recent lessons from the knowledge base

        If a duplicate is found, the existing lesson's confidence is reinforced.
        """
        # Tier 1: Fingerprint check (in-memory, O(1))
        fp = _lesson_fingerprint(lesson.category, lesson.summary)
        if fp in self._seen_fingerprints:
            logger.debug(
                f"Lesson dedup: exact fingerprint match for '{lesson.summary[:60]}'"
            )
            return True
        self._seen_fingerprints[fp] = lesson.summary

        # Tier 2: Semantic similarity check against stored lessons
        if not self._memory:
            return False

        try:
            existing = await self._memory.knowledge_search(
                workspace_id=workspace_id,
                query=lesson.summary,
                top_k=5,
            )

            for r in existing:
                content = r.get("content", "")
                try:
                    stored = json.loads(content)
                except json.JSONDecodeError:
                    continue

                # Same category required for duplicate consideration
                if stored.get("category") != lesson.category:
                    continue

                similarity = _summary_similarity(
                    lesson.summary,
                    stored.get("summary", ""),
                )

                if similarity >= DEDUP_SIMILARITY_THRESHOLD:
                    # Reinforce existing lesson's confidence
                    old_conf = stored.get("confidence", 0.8)
                    new_conf = min(old_conf + CONFIDENCE_REINFORCEMENT, 1.0)
                    if new_conf != old_conf:
                        stored["confidence"] = new_conf
                        stored["reinforcement_count"] = stored.get("reinforcement_count", 0) + 1
                        # Re-store with updated confidence
                        await self._memory.knowledge_store(
                            workspace_id=workspace_id,
                            content=json.dumps(stored),
                            source="agent_lesson",
                            metadata={
                                "category": stored.get("category", "pattern"),
                                "tags": stored.get("tags", []),
                                "success": stored.get("success", True),
                                "source_task_id": stored.get("source_task_id", ""),
                            },
                        )
                        logger.info(
                            f"Lesson dedup: reinforced existing lesson "
                            f"(similarity={similarity:.2f}, new confidence={new_conf:.2f}): "
                            f"'{stored.get('summary', '')[:60]}'"
                        )
                    return True

        except Exception as e:
            logger.debug(f"Lesson dedup check failed: {e}")

        return False

    async def _store_lessons(
        self,
        workspace_id: str,
        lessons: list[Lesson],
    ) -> None:
        """
        Persist lessons to the knowledge base with deduplication.

        Each lesson is checked against existing knowledge before storage.
        Duplicate lessons reinforce existing entries instead of creating new ones.
        """
        if not self._memory:
            logger.debug(f"Lesson persistence skipped for workspace {workspace_id}: no working memory configured")
            return

        stored_count = 0
        dedup_count = 0

        for lesson in lessons:
            if await self._is_duplicate(workspace_id, lesson):
                dedup_count += 1
                continue

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
            stored_count += 1

        if dedup_count > 0:
            logger.info(
                f"Lesson storage: {stored_count} new, {dedup_count} deduplicated "
                f"(workspace {workspace_id})"
            )
