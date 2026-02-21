from __future__ import annotations
"""
Agent Reflection & Self-Critique — systematic red-teaming of plans
before execution.

Every plan goes through a structured critique loop:
  1. Generate the initial plan
  2. Run a "red team" pass to identify weaknesses
  3. Assess risks, blind spots, and alternative approaches
  4. Strengthen the plan based on critique
  5. Produce a confidence score with justification

This is what separates Kestrel from "run and pray" agents.
No other agent systematically stress-tests its own reasoning.
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.reflection")


class CritiqueCategory(str, Enum):
    """Categories of critique an agent can identify."""
    RISK = "risk"                    # Could fail or cause harm
    BLIND_SPOT = "blind_spot"        # Something the plan doesn't consider
    ASSUMPTION = "assumption"        # Untested assumption baked in
    ALTERNATIVE = "alternative"      # A better approach exists
    EFFICIENCY = "efficiency"        # Wasted effort or suboptimal path
    DEPENDENCY = "dependency"        # External dependency that could break
    EDGE_CASE = "edge_case"          # Unhandled scenario
    SECURITY = "security"            # Security vulnerability
    REVERSIBILITY = "reversibility"  # Irreversible action without safeguard


@dataclass
class CritiquePoint:
    """A single critique identified during reflection."""
    category: CritiqueCategory
    severity: str  # "low", "medium", "high", "critical"
    description: str
    affected_step: Optional[str] = None
    suggestion: str = ""
    mitigated: bool = False

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "severity": self.severity,
            "description": self.description,
            "affected_step": self.affected_step,
            "suggestion": self.suggestion,
            "mitigated": self.mitigated,
        }


@dataclass
class ReflectionResult:
    """The full output of a reflection pass."""
    plan_summary: str
    critique_points: list[CritiquePoint] = field(default_factory=list)
    confidence_score: float = 0.0    # 0.0–1.0
    confidence_justification: str = ""
    strengthened_plan: str = ""
    alternatives_considered: list[str] = field(default_factory=list)
    estimated_risk_level: str = "medium"  # low, medium, high, critical
    should_proceed: bool = True
    reflection_time_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "plan_summary": self.plan_summary,
            "critique_count": len(self.critique_points),
            "critiques": [c.to_dict() for c in self.critique_points],
            "confidence_score": round(self.confidence_score, 2),
            "confidence_justification": self.confidence_justification,
            "risk_level": self.estimated_risk_level,
            "should_proceed": self.should_proceed,
            "alternatives_considered": self.alternatives_considered,
            "reflection_time_ms": self.reflection_time_ms,
        }


# ── Reflection Prompts ──────────────────────────────────────────────

RED_TEAM_PROMPT = """You are a critical reviewer. Your job is to find weaknesses in this plan.

## Plan to Critique
{plan}

## Task Context
{context}

## Instructions
Analyze this plan and identify:
1. **Risks**: What could go wrong? What are the failure modes?
2. **Blind spots**: What hasn't been considered?
3. **Assumptions**: What untested assumptions does this plan rely on?
4. **Alternatives**: Are there better approaches?
5. **Edge cases**: What scenarios could break this?
6. **Security**: Any security concerns?
7. **Reversibility**: Any irreversible actions without safeguards?

For each issue, rate severity as: low, medium, high, or critical.

Respond in JSON:
{{
  "critiques": [
    {{
      "category": "risk|blind_spot|assumption|alternative|efficiency|dependency|edge_case|security|reversibility",
      "severity": "low|medium|high|critical",
      "description": "...",
      "affected_step": "which step this affects",
      "suggestion": "how to fix or mitigate"
    }}
  ],
  "overall_risk_level": "low|medium|high|critical",
  "confidence_score": 0.0 to 1.0,
  "confidence_justification": "why this confidence level",
  "should_proceed": true/false,
  "alternatives": ["alternative approach 1", "..."]
}}
"""

STRENGTHEN_PROMPT = """You are a plan optimizer. Given the original plan and the critique, produce a strengthened version.

## Original Plan
{plan}

## Critique Points
{critiques}

## Instructions
Revise the plan to:
1. Mitigate all high/critical severity issues
2. Add safeguards for identified risks
3. Address blind spots
4. Include fallback strategies
5. Add validation checkpoints

Produce the strengthened plan. Be specific about what changed and why.
"""


class ReflectionEngine:
    """
    Systematically critiques and strengthens agent plans.

    Integrates with the agent loop: after planning but before execution,
    the plan is passed through reflection. If critical issues are found,
    the plan is revised or execution is blocked pending user approval.
    """

    def __init__(self, llm_provider=None, model: str = ""):
        self._llm = llm_provider
        self._model = model
        self._reflection_history: list[ReflectionResult] = []

    async def reflect(
        self,
        plan: str,
        context: str = "",
        task_goal: str = "",
    ) -> ReflectionResult:
        """
        Run a full reflection cycle on a plan.

        Steps:
          1. Red-team critique
          2. Parse and score critiques
          3. Strengthen the plan if issues found
          4. Return confidence assessment
        """
        import time
        start = time.time()

        result = ReflectionResult(plan_summary=plan[:500])

        # ── Step 1: Red-team critique ────────────────────────────
        critique_raw = await self._run_red_team(plan, context)
        parsed = self._parse_critique_response(critique_raw)

        result.critique_points = parsed["critiques"]
        result.confidence_score = parsed["confidence_score"]
        result.confidence_justification = parsed["confidence_justification"]
        result.estimated_risk_level = parsed["overall_risk_level"]
        result.should_proceed = parsed["should_proceed"]
        result.alternatives_considered = parsed["alternatives"]

        # ── Step 2: Strengthen if needed ─────────────────────────
        high_severity = [
            c for c in result.critique_points
            if c.severity in ("high", "critical")
        ]

        if high_severity and self._llm:
            strengthened = await self._strengthen_plan(plan, result.critique_points)
            result.strengthened_plan = strengthened

            # Mark mitigated critiques
            for critique in result.critique_points:
                if critique.suggestion and critique.suggestion.lower() in strengthened.lower():
                    critique.mitigated = True

        result.reflection_time_ms = int((time.time() - start) * 1000)

        # Store in history
        self._reflection_history.append(result)

        logger.info(
            f"Reflection complete: {len(result.critique_points)} critiques, "
            f"confidence={result.confidence_score:.2f}, "
            f"risk={result.estimated_risk_level}"
        )

        return result

    async def quick_check(self, action: str, context: str = "") -> dict:
        """
        Lightweight reflection for individual actions (not full plans).
        Returns a risk assessment without the full critique cycle.
        """
        risk_keywords = {
            "critical": ["delete", "drop", "remove all", "truncate", "rm -rf", "format", "destroy"],
            "high": ["modify", "update all", "deploy", "migrate", "overwrite", "replace"],
            "medium": ["create", "install", "write", "execute", "run"],
            "low": ["read", "list", "search", "query", "view", "check"],
        }

        action_lower = action.lower()
        detected_level = "low"

        for level, keywords in risk_keywords.items():
            if any(kw in action_lower for kw in keywords):
                detected_level = level
                break

        return {
            "action": action[:200],
            "risk_level": detected_level,
            "needs_approval": detected_level in ("critical", "high"),
            "checkpoint_recommended": detected_level in ("critical", "high", "medium"),
        }

    async def _run_red_team(self, plan: str, context: str) -> str:
        """Run the red-team prompt against the LLM."""
        if not self._llm:
            return self._generate_rule_based_critique(plan)

        prompt = RED_TEAM_PROMPT.format(plan=plan, context=context)
        try:
            response = await self._llm.generate(
                messages=[{"role": "user", "content": prompt}],
                model=self._model,
                temperature=0.3,
                max_tokens=2000,
            )
            return response.get("content", "")
        except Exception as e:
            logger.error(f"Red-team LLM call failed: {e}")
            return self._generate_rule_based_critique(plan)

    async def _strengthen_plan(self, plan: str, critiques: list[CritiquePoint]) -> str:
        """Use LLM to produce a strengthened plan."""
        if not self._llm:
            return plan

        critique_text = "\n".join(
            f"- [{c.severity.upper()}] {c.description} → {c.suggestion}"
            for c in critiques
        )

        prompt = STRENGTHEN_PROMPT.format(plan=plan, critiques=critique_text)
        try:
            response = await self._llm.generate(
                messages=[{"role": "user", "content": prompt}],
                model=self._model,
                temperature=0.3,
                max_tokens=2000,
            )
            return response.get("content", plan)
        except Exception as e:
            logger.error(f"Strengthen LLM call failed: {e}")
            return plan

    def _parse_critique_response(self, raw: str) -> dict:
        """Parse the red-team JSON response."""
        defaults = {
            "critiques": [],
            "overall_risk_level": "medium",
            "confidence_score": 0.5,
            "confidence_justification": "Unable to fully assess",
            "should_proceed": True,
            "alternatives": [],
        }

        try:
            # Try to extract JSON from response
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(raw[start:end])

                parsed_critiques = []
                for c in data.get("critiques", []):
                    try:
                        parsed_critiques.append(CritiquePoint(
                            category=CritiqueCategory(c.get("category", "risk")),
                            severity=c.get("severity", "medium"),
                            description=c.get("description", ""),
                            affected_step=c.get("affected_step"),
                            suggestion=c.get("suggestion", ""),
                        ))
                    except ValueError:
                        continue

                return {
                    "critiques": parsed_critiques,
                    "overall_risk_level": data.get("overall_risk_level", "medium"),
                    "confidence_score": float(data.get("confidence_score", 0.5)),
                    "confidence_justification": data.get("confidence_justification", ""),
                    "should_proceed": data.get("should_proceed", True),
                    "alternatives": data.get("alternatives", []),
                }
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            logger.warning(f"Failed to parse critique JSON: {e}")

        return defaults

    def _generate_rule_based_critique(self, plan: str) -> str:
        """Fallback: rule-based critique when LLM is unavailable."""
        critiques = []
        plan_lower = plan.lower()

        # Check for risky patterns
        if "delete" in plan_lower or "remove" in plan_lower:
            critiques.append({
                "category": "risk",
                "severity": "high",
                "description": "Plan includes destructive operations (delete/remove)",
                "suggestion": "Add backup/checkpoint before destructive operations",
            })

        if "deploy" in plan_lower:
            critiques.append({
                "category": "reversibility",
                "severity": "high",
                "description": "Deployment is difficult to reverse",
                "suggestion": "Ensure rollback procedure is documented",
            })

        if "all" in plan_lower and ("update" in plan_lower or "modify" in plan_lower):
            critiques.append({
                "category": "risk",
                "severity": "high",
                "description": "Bulk modification detected — high blast radius",
                "suggestion": "Process in batches with validation between batches",
            })

        if not any(kw in plan_lower for kw in ["test", "verify", "validate", "check"]):
            critiques.append({
                "category": "blind_spot",
                "severity": "medium",
                "description": "Plan has no explicit validation or testing step",
                "suggestion": "Add a verification step after key actions",
            })

        if not any(kw in plan_lower for kw in ["rollback", "revert", "backup", "undo"]):
            critiques.append({
                "category": "reversibility",
                "severity": "medium",
                "description": "No rollback strategy mentioned",
                "suggestion": "Add checkpoint or backup before making changes",
            })

        return json.dumps({
            "critiques": critiques,
            "overall_risk_level": "high" if any(c["severity"] == "high" for c in critiques) else "medium",
            "confidence_score": max(0.3, 1.0 - len(critiques) * 0.15),
            "confidence_justification": "Rule-based assessment (LLM unavailable)",
            "should_proceed": not any(c["severity"] == "critical" for c in critiques),
            "alternatives": [],
        })

    def get_history(self) -> list[dict]:
        """Get recent reflection history."""
        return [r.to_dict() for r in self._reflection_history[-10:]]
