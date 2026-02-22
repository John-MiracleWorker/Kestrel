"""
Evidence Chain — auditable decision trail for every agent action.

Every decision the agent makes is recorded with:
  - What was decided
  - Why (reasoning, sources consulted)
  - What alternatives were considered and rejected
  - What evidence supported the choice
  - Confidence level

This creates full transparency. Users can audit any agent action
and understand the reasoning chain that led to it.

No other agent provides this level of decision transparency.
"""

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.evidence")


class EvidenceType(str, Enum):
    """Types of evidence that support a decision."""
    TOOL_OUTPUT = "tool_output"         # Result from a tool call
    USER_INPUT = "user_input"           # Something the user said
    MEMORY_GRAPH = "memory_graph"       # Context from the knowledge graph
    LESSON_LEARNED = "lesson_learned"   # From post-task learning
    PERSONA = "persona"                 # From user preference learning
    REFLECTION = "reflection"           # From self-critique
    DOCUMENTATION = "documentation"     # From docs/files read
    WEB_SEARCH = "web_search"           # From web search results
    PRIOR_DECISION = "prior_decision"   # Reference to an earlier decision
    HEURISTIC = "heuristic"             # Built-in rule or best practice


class DecisionType(str, Enum):
    """Types of decisions an agent can make."""
    PLAN_CHOICE = "plan_choice"         # Chose a plan/approach
    TOOL_SELECTION = "tool_selection"   # Chose which tool to use
    PARAMETER_CHOICE = "parameter"     # Chose parameter values
    STRATEGY = "strategy"              # High-level strategy
    SKIP = "skip"                      # Decided NOT to do something
    DELEGATE = "delegate"              # Delegated to another agent
    APPROVE = "approve"                # Auto-approved an action
    ESCALATE = "escalate"              # Escalated to user


@dataclass
class EvidenceNode:
    """A single piece of evidence supporting a decision."""
    id: str
    evidence_type: EvidenceType
    content: str                # The actual evidence
    source: str = ""            # Where it came from (tool name, file, etc.)
    relevance_score: float = 1.0  # How relevant this evidence was (0.0–1.0)
    timestamp: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.evidence_type.value,
            "content": self.content[:500],
            "source": self.source,
            "relevance": round(self.relevance_score, 2),
        }


@dataclass
class DecisionRecord:
    """A recorded decision with full evidence trail."""
    id: str
    task_id: str
    step_number: int
    decision_type: DecisionType
    description: str              # What was decided
    reasoning: str                # Why this choice was made
    evidence: list[EvidenceNode] = field(default_factory=list)
    alternatives_considered: list[dict] = field(default_factory=list)
    confidence: float = 0.0       # 0.0–1.0
    outcome: Optional[str] = None  # Was it successful? (filled post-execution)
    created_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "step": self.step_number,
            "type": self.decision_type.value,
            "description": self.description,
            "reasoning": self.reasoning,
            "evidence": [e.to_dict() for e in self.evidence],
            "alternatives": self.alternatives_considered,
            "confidence": round(self.confidence, 2),
            "outcome": self.outcome,
            "created_at": self.created_at,
        }


class EvidenceChain:
    """
    Maintains an auditable decision trail for an agent task.

    Integrates with:
      - Planner: records plan choices and strategy decisions
      - Tool executor: records tool selection and parameter choices
      - Reflection: records why issues were flagged or dismissed
      - Coordinator: records delegation decisions
      - Checkpoint manager: records rollback decisions

    The chain can be exported as a readable audit report.
    """

    def __init__(self, task_id: str, pool=None):
        self._task_id = task_id
        self._pool = pool
        self._decisions: list[DecisionRecord] = []
        self._step_counter = 0

    def record_decision(
        self,
        decision_type: DecisionType,
        description: str,
        reasoning: str,
        evidence: list[dict] = None,
        alternatives: list[dict] = None,
        confidence: float = 0.5,
    ) -> DecisionRecord:
        """Record a decision with its evidence."""
        self._step_counter += 1

        evidence_nodes = []
        for e in (evidence or []):
            evidence_nodes.append(EvidenceNode(
                id=str(uuid.uuid4())[:8],
                evidence_type=EvidenceType(e.get("type", "heuristic")),
                content=e.get("content", ""),
                source=e.get("source", ""),
                relevance_score=e.get("relevance", 1.0),
                timestamp=datetime.now(timezone.utc).isoformat(),
            ))

        record = DecisionRecord(
            id=str(uuid.uuid4()),
            task_id=self._task_id,
            step_number=self._step_counter,
            decision_type=decision_type,
            description=description,
            reasoning=reasoning,
            evidence=evidence_nodes,
            alternatives_considered=alternatives or [],
            confidence=confidence,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        self._decisions.append(record)
        logger.debug(f"Decision recorded: [{decision_type.value}] {description[:80]}")
        return record

    def record_tool_decision(
        self,
        tool_name: str,
        args: dict,
        reasoning: str,
        alternatives: list[str] = None,
    ) -> DecisionRecord:
        """Convenience method for tool selection decisions."""
        return self.record_decision(
            decision_type=DecisionType.TOOL_SELECTION,
            description=f"Selected tool: {tool_name}",
            reasoning=reasoning,
            evidence=[{
                "type": "heuristic",
                "content": f"Tool args: {json.dumps(args)[:300]}",
                "source": "tool_registry",
            }],
            alternatives=[
                {"tool": t, "reason_rejected": "Less suitable for current context"}
                for t in (alternatives or [])
            ],
        )

    def record_plan_decision(
        self,
        plan_summary: str,
        reasoning: str,
        reflection_result: dict = None,
        confidence: float = 0.7,
    ) -> DecisionRecord:
        """Convenience method for plan/strategy decisions."""
        evidence = []
        if reflection_result:
            evidence.append({
                "type": "reflection",
                "content": json.dumps(reflection_result)[:500],
                "source": "reflection_engine",
                "relevance": 0.9,
            })

        return self.record_decision(
            decision_type=DecisionType.PLAN_CHOICE,
            description=plan_summary,
            reasoning=reasoning,
            evidence=evidence,
            confidence=confidence,
        )

    def record_outcome(self, decision_id: str, outcome: str) -> None:
        """Record the outcome of a previously made decision."""
        for d in self._decisions:
            if d.id == decision_id:
                d.outcome = outcome
                break

    def get_chain(self) -> list[dict]:
        """Get the full evidence chain for this task."""
        return [d.to_dict() for d in self._decisions]

    def get_summary(self) -> dict:
        """Get a summary of the decision chain."""
        total = len(self._decisions)
        if not total:
            return {"total_decisions": 0}

        avg_confidence = sum(d.confidence for d in self._decisions) / total
        by_type = {}
        for d in self._decisions:
            by_type[d.decision_type.value] = by_type.get(d.decision_type.value, 0) + 1

        return {
            "task_id": self._task_id,
            "total_decisions": total,
            "avg_confidence": round(avg_confidence, 2),
            "decisions_by_type": by_type,
            "decisions_with_alternatives": sum(
                1 for d in self._decisions if d.alternatives_considered
            ),
            "high_confidence": sum(1 for d in self._decisions if d.confidence >= 0.8),
            "low_confidence": sum(1 for d in self._decisions if d.confidence < 0.4),
        }

    def generate_audit_report(self) -> str:
        """
        Generate a human-readable audit report of all decisions.
        Designed for export or display in the UI.
        """
        lines = [
            f"# 🔍 Evidence Chain — Task {self._task_id[:8]}",
            f"",
            f"**Total decisions:** {len(self._decisions)}",
            f"",
        ]

        for d in self._decisions:
            outcome_icon = ""
            if d.outcome == "success":
                outcome_icon = " ✅"
            elif d.outcome == "failed":
                outcome_icon = " ❌"

            lines.append(f"## Step {d.step_number}: {d.description}{outcome_icon}")
            lines.append(f"**Type:** {d.decision_type.value} · **Confidence:** {d.confidence:.0%}")
            lines.append(f"")
            lines.append(f"**Reasoning:** {d.reasoning}")

            if d.evidence:
                lines.append(f"")
                lines.append(f"**Evidence:**")
                for e in d.evidence:
                    lines.append(f"  - [{e.evidence_type.value}] {e.content[:200]}")

            if d.alternatives_considered:
                lines.append(f"")
                lines.append(f"**Alternatives considered:**")
                for alt in d.alternatives_considered:
                    alt_name = alt.get("tool", alt.get("approach", "unknown"))
                    reason = alt.get("reason_rejected", "")
                    lines.append(f"  - ~~{alt_name}~~ — {reason}")

            lines.append(f"")
            lines.append(f"---")
            lines.append(f"")

        return "\n".join(lines)

    async def persist(self) -> None:
        """Persist the full chain to the database."""
        if not self._pool:
            return

        try:
            async with self._pool.acquire() as conn:
                for d in self._decisions:
                    # asyncpg needs datetime objects, not ISO strings
                    created_dt = datetime.fromisoformat(d.created_at) if d.created_at else datetime.now(timezone.utc)
                    await conn.execute(
                        """
                        INSERT INTO evidence_chain
                            (id, task_id, step_number, decision_type, description,
                             reasoning, evidence, alternatives, confidence, outcome, created_at)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb, $9, $10, $11)
                        ON CONFLICT (id) DO UPDATE SET outcome = $10
                        """,
                        d.id, d.task_id, d.step_number, d.decision_type.value,
                        d.description, d.reasoning,
                        json.dumps([e.to_dict() for e in d.evidence]),
                        json.dumps(d.alternatives_considered),
                        d.confidence, d.outcome, created_dt,
                    )
        except Exception as e:
            logger.error(f"Failed to persist evidence chain: {e}")
