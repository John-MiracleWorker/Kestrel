from __future__ import annotations
"""
Pre-Flight Outcome Simulation — simulate plan execution before committing.

Uses a separate LLM call to predict per-step outcomes, side effects,
and reversibility. Integrates with ReflectionEngine critique results
to give the user a full picture before execution begins.
"""

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("brain.agent.simulation")


# ── Simulation Prompt ────────────────────────────────────────────────

SIMULATION_PROMPT = """\
You are a pre-flight simulation engine. Given an agent's execution plan,
predict what will happen at each step WITHOUT actually executing anything.

Goal: {goal}

Plan steps:
{steps}

Available tools the agent can use:
{tool_names}

For each step, predict:
1. **outcome**: What will likely happen
2. **side_effects**: Any changes to files, state, or external systems
3. **risk**: low / medium / high
4. **reversible**: Whether this step can be undone
5. **potential_failure**: What could go wrong

Also provide:
- **overall_risk**: low / medium / high for the entire plan
- **recommendation**: proceed / proceed_with_caution / abort
- **concerns**: Any cross-step issues (ordering, dependencies, conflicts)

Output ONLY valid JSON:
{{
  "steps": [
    {{
      "step_id": "...",
      "description": "...",
      "predicted_outcome": "...",
      "side_effects": ["..."],
      "risk": "low|medium|high",
      "reversible": true|false,
      "potential_failure": "..."
    }}
  ],
  "overall_risk": "low|medium|high",
  "recommendation": "proceed|proceed_with_caution|abort",
  "concerns": ["..."]
}}
"""


# ── Data Models ──────────────────────────────────────────────────────


@dataclass
class StepSimulation:
    """Predicted outcome for a single plan step."""
    step_id: str = ""
    description: str = ""
    predicted_outcome: str = ""
    side_effects: list[str] = field(default_factory=list)
    risk: str = "low"
    reversible: bool = True
    potential_failure: str = ""

    def to_dict(self) -> dict:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "predicted_outcome": self.predicted_outcome,
            "side_effects": self.side_effects,
            "risk": self.risk,
            "reversible": self.reversible,
            "potential_failure": self.potential_failure,
        }


@dataclass
class SimulationResult:
    """Full simulation result for a plan."""
    plan_goal: str = ""
    step_simulations: list[StepSimulation] = field(default_factory=list)
    overall_risk: str = "medium"
    recommendation: str = "proceed"  # proceed / proceed_with_caution / abort
    concerns: list[str] = field(default_factory=list)
    simulation_time_ms: int = 0
    should_proceed: bool = True

    def to_dict(self) -> dict:
        return {
            "plan_goal": self.plan_goal,
            "steps": [s.to_dict() for s in self.step_simulations],
            "overall_risk": self.overall_risk,
            "recommendation": self.recommendation,
            "concerns": self.concerns,
            "simulation_time_ms": self.simulation_time_ms,
            "should_proceed": self.should_proceed,
        }

    @property
    def high_risk_steps(self) -> list[StepSimulation]:
        return [s for s in self.step_simulations if s.risk == "high"]

    @property
    def irreversible_steps(self) -> list[StepSimulation]:
        return [s for s in self.step_simulations if not s.reversible]

    def summary(self) -> str:
        """Human-readable summary."""
        lines = [f"🔮 Simulation: {self.recommendation.upper()}"]
        lines.append(f"   Overall risk: {self.overall_risk}")
        if self.high_risk_steps:
            lines.append(f"   ⚠️  {len(self.high_risk_steps)} high-risk steps")
        if self.irreversible_steps:
            lines.append(f"   🔒 {len(self.irreversible_steps)} irreversible steps")
        if self.concerns:
            lines.append(f"   Concerns: {'; '.join(self.concerns[:3])}")
        return "\n".join(lines)


# ── Outcome Simulator ────────────────────────────────────────────────


class OutcomeSimulator:
    """
    Simulates plan execution using an LLM to predict outcomes.

    Usage:
        sim = OutcomeSimulator(provider)
        result = await sim.simulate(plan, tool_names)
        if not result.should_proceed:
            # Surface to user for approval
    """

    def __init__(self, llm_provider=None, model: str = "", event_callback=None, provider_resolver=None):
        self._provider = llm_provider
        self._model = model
        self._event_callback = event_callback
        self._resolver = provider_resolver  # callable() -> provider

    def _get_provider(self):
        """Resolve provider dynamically."""
        if self._resolver:
            return self._resolver()
        return self._provider

    async def simulate(
        self,
        plan,  # TaskPlan
        tool_names: list[str] = None,
        context: str = "",
    ) -> SimulationResult:
        """
        Run pre-flight simulation on a TaskPlan.

        Returns SimulationResult with per-step predictions.
        """
        start = time.time()

        if self._event_callback:
            await self._event_callback("simulation_started", {
                "goal": plan.goal,
                "step_count": len(plan.steps),
            })

        # Format steps for the prompt
        steps_text = "\n".join(
            f"  {i+1}. [{s.id}] {s.description} (tools: {', '.join(s.expected_tools) or 'any'})"
            for i, s in enumerate(plan.steps)
        )

        tools_text = ", ".join(tool_names or ["(unknown)"])

        # If no LLM provider, fall back to rule-based simulation
        provider = self._get_provider()
        if not provider:
            result = self._rule_based_simulation(plan)
            result.simulation_time_ms = int((time.time() - start) * 1000)
            return result

        # LLM-based simulation
        prompt = SIMULATION_PROMPT.format(
            goal=plan.goal,
            steps=steps_text,
            tool_names=tools_text,
        )

        try:
            response = await provider.generate(
                messages=[{"role": "user", "content": prompt}],
                model=self._model,
                temperature=0.3,
                max_tokens=2048,
            )

            raw = response.get("content", "")
            result = self._parse_simulation(raw, plan)

        except Exception as e:
            logger.error(f"LLM simulation failed, falling back to rules: {e}")
            result = self._rule_based_simulation(plan)

        result.simulation_time_ms = int((time.time() - start) * 1000)

        # Determine should_proceed
        result.should_proceed = result.recommendation != "abort"

        if self._event_callback:
            await self._event_callback("simulation_complete", result.to_dict())

        return result

    def _parse_simulation(self, raw: str, plan) -> SimulationResult:
        """Parse LLM simulation response."""
        # Strip markdown fences
        raw = re.sub(r"```json\s*", "", raw)
        raw = re.sub(r"```\s*$", "", raw)

        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            logger.warning("Failed to parse simulation JSON, using rule-based fallback")
            return self._rule_based_simulation(plan)

        steps = []
        for s in data.get("steps", []):
            steps.append(StepSimulation(
                step_id=s.get("step_id", ""),
                description=s.get("description", ""),
                predicted_outcome=s.get("predicted_outcome", ""),
                side_effects=s.get("side_effects", []),
                risk=s.get("risk", "low"),
                reversible=s.get("reversible", True),
                potential_failure=s.get("potential_failure", ""),
            ))

        return SimulationResult(
            plan_goal=plan.goal,
            step_simulations=steps,
            overall_risk=data.get("overall_risk", "medium"),
            recommendation=data.get("recommendation", "proceed"),
            concerns=data.get("concerns", []),
        )

    def _rule_based_simulation(self, plan) -> SimulationResult:
        """Fallback: rule-based risk assessment when LLM isn't available."""
        RISKY_TOOLS = {"host_write", "execute_code", "shell_exec", "computer_use"}
        IRREVERSIBLE_TOOLS = {"shell_exec", "computer_use"}

        steps = []
        high_risk_count = 0
        for s in plan.steps:
            tools = set(s.expected_tools)
            risk = "low"
            reversible = True

            if tools & RISKY_TOOLS:
                risk = "high"
                high_risk_count += 1
            elif len(tools) > 3:
                risk = "medium"

            if tools & IRREVERSIBLE_TOOLS:
                reversible = False

            steps.append(StepSimulation(
                step_id=s.id,
                description=s.description,
                predicted_outcome=f"Step will execute using: {', '.join(s.expected_tools) or 'general reasoning'}",
                side_effects=[f"Uses {t}" for t in tools & RISKY_TOOLS] if tools & RISKY_TOOLS else [],
                risk=risk,
                reversible=reversible,
            ))

        overall = "low"
        if high_risk_count > len(plan.steps) // 2:
            overall = "high"
        elif high_risk_count > 0:
            overall = "medium"

        recommendation = "proceed"
        if overall == "high":
            recommendation = "proceed_with_caution"

        return SimulationResult(
            plan_goal=plan.goal,
            step_simulations=steps,
            overall_risk=overall,
            recommendation=recommendation,
            should_proceed=True,
        )
