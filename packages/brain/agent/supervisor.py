"""
Supervisor — DeerFlow-style high-level task classification and routing.

Analyzes incoming tasks and determines the optimal execution strategy:
  - Simple tasks → direct execution via execute node
  - Deep research → research subgraph (parallel fan-out)
  - Content generation → content subgraph (AIGC pipeline)
  - Complex mixed tasks → supervisor-coordinated multi-stage execution

Sits above the coordinator as a strategic planning layer.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agent.types import AgentTask

logger = logging.getLogger("brain.agent.supervisor")

# Task type classification keywords
_RESEARCH_KEYWORDS = frozenset({
    "research", "investigate", "analyze", "study", "compare",
    "survey", "review", "explore", "deep dive", "comprehensive",
    "find out", "discover", "what is", "how does", "why do",
})

_CONTENT_KEYWORDS = frozenset({
    "slides", "presentation", "slide deck", "powerpoint",
    "webpage", "web page", "html", "website",
    "pdf", "report", "document", "paper",
    "video", "generate video", "create video",
    "image", "generate image", "create image",
})

_CODING_KEYWORDS = frozenset({
    "code", "implement", "build", "develop", "fix", "debug",
    "refactor", "test", "deploy", "write code", "program",
    "function", "class", "api", "endpoint", "database",
})


class TaskClassification:
    """Result of task classification."""

    def __init__(
        self,
        task_type: str,
        confidence: float,
        sub_types: list[str] | None = None,
        recommended_specialists: list[str] | None = None,
        estimated_complexity: float = 5.0,
    ):
        self.task_type = task_type  # "research", "content", "coding", "simple", "mixed"
        self.confidence = confidence
        self.sub_types = sub_types or []
        self.recommended_specialists = recommended_specialists or []
        self.estimated_complexity = estimated_complexity


def classify_task(task: AgentTask) -> TaskClassification:
    """Classify a task to determine optimal execution strategy.

    Uses keyword matching as a fast heuristic. In production,
    this can be enhanced with an LLM classifier call.
    """
    goal_lower = task.goal.lower()
    scores = {
        "research": 0.0,
        "content": 0.0,
        "coding": 0.0,
        "simple": 0.0,
    }

    # Score each category
    for kw in _RESEARCH_KEYWORDS:
        if kw in goal_lower:
            scores["research"] += 1.0

    for kw in _CONTENT_KEYWORDS:
        if kw in goal_lower:
            scores["content"] += 1.0

    for kw in _CODING_KEYWORDS:
        if kw in goal_lower:
            scores["coding"] += 1.0

    # Determine primary type
    max_score = max(scores.values())
    if max_score == 0:
        return TaskClassification(
            task_type="simple",
            confidence=0.8,
            estimated_complexity=2.0,
        )

    # Check for mixed tasks
    high_scores = [k for k, v in scores.items() if v > 0 and v >= max_score * 0.5]
    if len(high_scores) > 1:
        return TaskClassification(
            task_type="mixed",
            confidence=0.6,
            sub_types=high_scores,
            recommended_specialists=_specialists_for_types(high_scores),
            estimated_complexity=8.0,
        )

    primary = max(scores, key=scores.get)
    return TaskClassification(
        task_type=primary,
        confidence=min(0.9, 0.5 + max_score * 0.1),
        recommended_specialists=_specialists_for_types([primary]),
        estimated_complexity=_complexity_for_type(primary, goal_lower),
    )


def _specialists_for_types(types: list[str]) -> list[str]:
    """Map task types to recommended specialists."""
    mapping = {
        "research": ["researcher", "synthesizer"],
        "content": ["synthesizer", "coder"],
        "coding": ["coder", "reviewer"],
        "simple": [],
    }
    result = []
    for t in types:
        result.extend(mapping.get(t, []))
    return list(dict.fromkeys(result))  # Deduplicate preserving order


def _complexity_for_type(task_type: str, goal: str) -> float:
    """Estimate complexity based on task type and goal length."""
    base = {"research": 7.0, "content": 6.0, "coding": 5.0, "simple": 2.0}
    complexity = base.get(task_type, 5.0)
    # Longer goals tend to be more complex
    if len(goal) > 500:
        complexity += 1.5
    elif len(goal) > 200:
        complexity += 0.5
    return min(10.0, complexity)


class Supervisor:
    """High-level task orchestrator.

    Classifies tasks and routes them to the appropriate execution
    strategy (direct, research subgraph, content subgraph, or mixed).
    """

    def __init__(
        self,
        coordinator=None,
        search_router=None,
    ):
        self._coordinator = coordinator
        self._search_router = search_router

    def classify(self, task: AgentTask) -> TaskClassification:
        """Classify a task for routing."""
        return classify_task(task)

    async def route(self, task: AgentTask) -> dict[str, Any]:
        """Classify and return routing decision.

        Returns a dict compatible with LangGraph state updates:
        {"route": "research_graph" | "content_graph" | "execute"}
        """
        classification = self.classify(task)
        logger.info(
            f"Task classified as {classification.task_type} "
            f"(confidence={classification.confidence:.2f}, "
            f"complexity={classification.estimated_complexity:.1f})"
        )

        if classification.task_type == "research" and classification.confidence > 0.6:
            return {
                "route": "research_graph",
                "plan_complexity": classification.estimated_complexity,
            }
        elif classification.task_type == "content" and classification.confidence > 0.6:
            return {
                "route": "content_graph",
                "plan_complexity": classification.estimated_complexity,
            }
        else:
            return {
                "route": "execute",
                "plan_complexity": classification.estimated_complexity,
            }
