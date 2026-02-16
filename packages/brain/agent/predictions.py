"""
Predictive Task Suggestions — proactive intelligence that suggests
tasks before the user asks.

Based on:
  - Conversation patterns (what topics come up regularly)
  - Temporal patterns (what they do on Mondays vs Fridays)
  - Cron history (what scheduled tasks exist)
  - Memory graph signals (entities that haven't been revisited)
  - Workflow patterns (common sequences of actions)
  - Stale items (unresolved issues, old TODO items)

This is what makes Kestrel feel like a thoughtful colleague, not a tool.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger("brain.agent.predictions")


@dataclass
class TaskSuggestion:
    """A proactive task suggestion."""
    id: str
    title: str
    description: str
    reason: str              # Why we're suggesting this
    confidence: float        # 0.0–1.0
    category: str            # "routine", "followup", "maintenance", "opportunity", "stale"
    priority: str = "medium" # low, medium, high
    goal_template: str = ""  # Pre-filled goal for one-click launch
    context_entities: list[str] = field(default_factory=list)
    expires_at: Optional[str] = None
    dismissed: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "reason": self.reason,
            "confidence": round(self.confidence, 2),
            "category": self.category,
            "priority": self.priority,
            "goal_template": self.goal_template,
            "context_entities": self.context_entities,
        }


# ── Pattern Rules ────────────────────────────────────────────────────

@dataclass
class TemporalRule:
    """A time-based suggestion rule."""
    name: str
    day_of_week: Optional[int] = None  # 0=Monday, 6=Sunday
    hour_range: tuple[int, int] = (0, 23)
    suggestion: TaskSuggestion = None


# Built-in temporal patterns
TEMPORAL_PATTERNS = [
    TemporalRule(
        name="monday_planning",
        day_of_week=0,
        hour_range=(8, 11),
        suggestion=TaskSuggestion(
            id="weekly-planning",
            title="📋 Weekly Planning",
            description="Review last week's outcomes and plan this week's priorities.",
            reason="It's Monday morning — good time to plan the week",
            confidence=0.7,
            category="routine",
            priority="high",
            goal_template="Review my recent task history, summarize outcomes from last week, and help me plan priorities for this week.",
        ),
    ),
    TemporalRule(
        name="friday_review",
        day_of_week=4,
        hour_range=(14, 17),
        suggestion=TaskSuggestion(
            id="weekly-review",
            title="📊 Weekly Review",
            description="Summarize this week's accomplishments and open items.",
            reason="It's Friday afternoon — good time to review the week",
            confidence=0.65,
            category="routine",
            priority="medium",
            goal_template="Summarize everything I accomplished this week, list any open items or unresolved issues, and flag anything that needs attention before next week.",
        ),
    ),
    TemporalRule(
        name="morning_standup",
        day_of_week=None,  # Any weekday
        hour_range=(8, 10),
        suggestion=TaskSuggestion(
            id="daily-standup",
            title="☀️ Morning Standup",
            description="Quick review of yesterday's progress and today's plan.",
            reason="Good morning — here's a quick way to start the day",
            confidence=0.5,
            category="routine",
            priority="medium",
            goal_template="Review what I worked on yesterday, check for any overnight notifications or updates, and help me prioritize today's tasks.",
        ),
    ),
]


class TaskPredictor:
    """
    Predicts and suggests tasks the user might want to do.

    Sources of prediction signals:
      1. Time-of-day / day-of-week patterns
      2. Recent conversation topics (from memory graph)
      3. Stale entities (things not revisited in a while)
      4. Follow-ups from completed tasks
      5. User-defined recurring patterns
    """

    def __init__(self, pool, memory_graph=None, persona_learner=None):
        self._pool = pool
        self._graph = memory_graph
        self._persona = persona_learner
        self._dismissed: set[str] = set()
        self._custom_rules: list[TemporalRule] = []

    async def generate_suggestions(
        self,
        user_id: str,
        workspace_id: str,
        max_suggestions: int = 5,
    ) -> list[dict]:
        """
        Generate ranked task suggestions for the user.
        Called when a user opens the dashboard or starts a new session.
        """
        suggestions: list[TaskSuggestion] = []
        now = datetime.now(timezone.utc)

        # ── 1. Temporal patterns ─────────────────────────────────
        temporal = self._check_temporal_patterns(now)
        suggestions.extend(temporal)

        # ── 2. Stale entity suggestions ──────────────────────────
        if self._graph:
            stale = await self._check_stale_entities(workspace_id)
            suggestions.extend(stale)

        # ── 3. Follow-up from recent tasks ───────────────────────
        followups = await self._check_task_followups(user_id, workspace_id)
        suggestions.extend(followups)

        # ── 4. Maintenance suggestions ───────────────────────────
        maintenance = await self._check_maintenance(workspace_id)
        suggestions.extend(maintenance)

        # Filter dismissed
        suggestions = [s for s in suggestions if s.id not in self._dismissed]

        # Rank by confidence * priority weight
        priority_weights = {"high": 1.5, "medium": 1.0, "low": 0.5}
        suggestions.sort(
            key=lambda s: s.confidence * priority_weights.get(s.priority, 1.0),
            reverse=True,
        )

        return [s.to_dict() for s in suggestions[:max_suggestions]]

    def dismiss_suggestion(self, suggestion_id: str) -> None:
        """Dismiss a suggestion so it doesn't appear again this session."""
        self._dismissed.add(suggestion_id)

    def _check_temporal_patterns(self, now: datetime) -> list[TaskSuggestion]:
        """Check which temporal rules match the current time."""
        results = []
        all_rules = TEMPORAL_PATTERNS + self._custom_rules

        for rule in all_rules:
            if rule.suggestion is None:
                continue

            # Check day of week
            if rule.day_of_week is not None and now.weekday() != rule.day_of_week:
                continue
            # Skip weekends for weekday-only rules
            if rule.day_of_week is None and now.weekday() >= 5:
                continue

            # Check hour range
            low, high = rule.hour_range
            if not (low <= now.hour <= high):
                continue

            results.append(rule.suggestion)

        return results

    async def _check_stale_entities(self, workspace_id: str) -> list[TaskSuggestion]:
        """Find entities in the memory graph that haven't been visited recently."""
        suggestions = []

        try:
            async with self._pool.acquire() as conn:
                # Find important entities not seen in 7+ days
                rows = await conn.fetch(
                    """
                    SELECT name, entity_type, weight, mention_count, last_seen
                    FROM memory_graph_nodes
                    WHERE workspace_id = $1
                      AND weight > 1.0
                      AND last_seen < now() - interval '7 days'
                    ORDER BY weight DESC
                    LIMIT 3
                    """,
                    workspace_id,
                )

                for row in rows:
                    name = row["name"]
                    entity_type = row["entity_type"]
                    days = (datetime.now(timezone.utc) - row["last_seen"].replace(tzinfo=timezone.utc)).days

                    suggestions.append(TaskSuggestion(
                        id=f"stale-{name[:20]}",
                        title=f"🔍 Revisit: {name}",
                        description=f"Haven't looked at {name} ({entity_type}) in {days} days.",
                        reason=f"This {entity_type} was mentioned {row['mention_count']} times but hasn't been revisited in {days} days",
                        confidence=min(0.4 + (days / 30) * 0.3, 0.8),
                        category="stale",
                        priority="low",
                        goal_template=f"Review the current state of {name} and check if anything needs attention or updating.",
                        context_entities=[name],
                    ))

        except Exception as e:
            logger.error(f"Stale entity check failed: {e}")

        return suggestions

    async def _check_task_followups(self, user_id: str, workspace_id: str) -> list[TaskSuggestion]:
        """Check recently completed tasks for potential follow-ups."""
        suggestions = []

        try:
            async with self._pool.acquire() as conn:
                # Find tasks completed in the last 24 hours
                rows = await conn.fetch(
                    """
                    SELECT id, goal, status, completed_at
                    FROM agent_tasks
                    WHERE user_id = $1 AND workspace_id = $2
                      AND status = 'completed'
                      AND completed_at > now() - interval '24 hours'
                    ORDER BY completed_at DESC
                    LIMIT 3
                    """,
                    user_id, workspace_id,
                )

                for row in rows:
                    goal = row["goal"] or ""
                    summary = goal[:80]

                    suggestions.append(TaskSuggestion(
                        id=f"followup-{row['id'][:8]}",
                        title=f"🔄 Follow up: {summary}",
                        description=f"Your recent task finished — want to review the results or continue?",
                        reason="Recently completed task may need follow-up",
                        confidence=0.55,
                        category="followup",
                        priority="medium",
                        goal_template=f"Review the results of my previous task ('{summary}') and check if any follow-up actions are needed.",
                    ))

        except Exception as e:
            logger.error(f"Task followup check failed: {e}")

        return suggestions

    async def _check_maintenance(self, workspace_id: str) -> list[TaskSuggestion]:
        """Suggest periodic maintenance tasks."""
        suggestions = []

        try:
            async with self._pool.acquire() as conn:
                # Check memory graph size — suggest cleanup if large
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM memory_graph_nodes WHERE workspace_id = $1",
                    workspace_id,
                )

                if count and count > 500:
                    suggestions.append(TaskSuggestion(
                        id="graph-cleanup",
                        title="🧹 Memory Graph Cleanup",
                        description=f"Your knowledge graph has {count} entities — some may be stale.",
                        reason=f"Graph has {count} entities; periodic cleanup keeps context relevant",
                        confidence=0.45,
                        category="maintenance",
                        priority="low",
                        goal_template="Review my memory graph, identify low-weight or stale entities, and help me clean up anything that's no longer relevant.",
                    ))

        except Exception as e:
            logger.debug(f"Maintenance check skipped: {e}")

        return suggestions

    def add_custom_rule(self, rule: TemporalRule) -> None:
        """Add a custom temporal suggestion rule."""
        self._custom_rules.append(rule)
