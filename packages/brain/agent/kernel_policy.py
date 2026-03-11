from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.model_router import RoutingStrategy, classify_step, estimate_complexity


@dataclass(frozen=True)
class KernelPolicy:
    preset: str
    step_type: str
    estimated_complexity: float
    routing_strategy: str
    planning_depth: str
    use_reflection: bool
    use_simulation: bool
    use_council: bool
    council_threshold: float
    simulation_threshold: float
    reflection_min_steps: int
    memory_depth: int
    persona_weight: float
    preferred_categories: tuple[str, ...]
    active_nodes: tuple[str, ...]
    subsystem_health: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset": self.preset,
            "step_type": self.step_type,
            "estimated_complexity": self.estimated_complexity,
            "routing_strategy": self.routing_strategy,
            "planning_depth": self.planning_depth,
            "use_reflection": self.use_reflection,
            "use_simulation": self.use_simulation,
            "use_council": self.use_council,
            "council_threshold": self.council_threshold,
            "simulation_threshold": self.simulation_threshold,
            "reflection_min_steps": self.reflection_min_steps,
            "memory_depth": self.memory_depth,
            "persona_weight": self.persona_weight,
            "preferred_categories": list(self.preferred_categories),
            "active_nodes": list(self.active_nodes),
            "subsystem_health": dict(self.subsystem_health),
        }


class KernelPolicyService:
    """Select adaptive orchestration settings per task."""

    _CATEGORY_HINTS = {
        "media": ("media", "computer_use", "ui"),
        "coding": ("code", "file", "analysis", "development"),
        "research": ("web", "data", "memory"),
        "ops": ("automation", "mcp", "social", "infrastructure"),
        "self_repair": ("development", "host_file", "skill", "analysis"),
        "general": ("memory", "web"),
    }

    def evaluate(
        self,
        *,
        task,
        execution_context=None,
        subsystem_health: dict[str, str] | None = None,
        persona_context: str = "",
    ) -> KernelPolicy:
        subsystem_health = dict(subsystem_health or {})
        goal_lower = task.goal.lower()
        preset = (
            getattr(execution_context, "kernel_preset", "")
            or getattr(execution_context, "default_mode", "")
            or getattr(task, "_feature_mode", "")
            or "ops"
        )
        preset = str(preset or "ops").lower()
        if preset not in {"core", "ops", "labs"}:
            preset = "ops"

        step_type = classify_step(task.goal).value
        complexity = estimate_complexity(task.goal, classify_step(task.goal))

        planning_depth = "standard"
        if complexity >= 8.5:
            planning_depth = "deep"
        elif complexity <= 3.0:
            planning_depth = "shallow"

        routing_strategy = RoutingStrategy.LOCAL_FIRST.value
        if step_type in {"coding", "security"}:
            routing_strategy = RoutingStrategy.QUALITY_FIRST.value
        elif step_type in {"research", "general"}:
            routing_strategy = RoutingStrategy.COST_OPTIMIZED.value

        council_threshold = 7.5
        simulation_threshold = 7.0
        reflection_min_steps = 3
        memory_depth = 6
        persona_weight = 0.55 if persona_context else 0.25

        if preset == "core":
            council_threshold = 8.5
            simulation_threshold = 8.0
            memory_depth = 4
        elif preset == "labs":
            council_threshold = 6.5
            simulation_threshold = 6.0
            memory_depth = 8
            persona_weight = max(persona_weight, 0.65)

        capability_gap = any(
            keyword in goal_lower
            for keyword in ("create tool", "new tool", "missing tool", "doesn't exist", "does not exist")
        )
        simulation_status = subsystem_health.get("simulation", "unknown")
        simulation_ready = simulation_status not in {"unavailable", "degraded"}

        use_reflection = complexity >= 4.0 or "fix" in goal_lower or capability_gap
        use_simulation = simulation_ready and (
            complexity >= simulation_threshold or capability_gap
        )
        use_council = complexity >= council_threshold

        profile = getattr(task, "task_profile", "") or "general"
        preferred_categories = self._CATEGORY_HINTS.get(profile, self._CATEGORY_HINTS["general"])

        active_nodes = ["initialize", "policy", "plan", "execute", "reflect", "complete"]
        if use_simulation:
            active_nodes.append("simulate")
        if use_council:
            active_nodes.append("council")
        if capability_gap:
            active_nodes.append("capability_gap")

        return KernelPolicy(
            preset=preset,
            step_type=step_type,
            estimated_complexity=complexity,
            routing_strategy=routing_strategy,
            planning_depth=planning_depth,
            use_reflection=use_reflection,
            use_simulation=use_simulation,
            use_council=use_council,
            council_threshold=council_threshold,
            simulation_threshold=simulation_threshold,
            reflection_min_steps=reflection_min_steps,
            memory_depth=memory_depth,
            persona_weight=persona_weight,
            preferred_categories=preferred_categories,
            active_nodes=tuple(active_nodes),
            subsystem_health=subsystem_health,
        )
