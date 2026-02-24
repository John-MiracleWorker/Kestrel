"""
Adaptive Model Router — per-step model selection for cost+quality optimisation.

Routes different step types to different models:
  - planning    → fast/cheap model (e.g. gemini-2.0-flash)
  - coding      → powerful model  (e.g. gemini-2.5-pro)
  - reflection  → fast model
  - security    → powerful/cautious model
  - general     → workspace default

Supports per-workspace overrides stored in the DB.
"""

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.model_router")


# ── Step Types ───────────────────────────────────────────────────────


class StepType(str, Enum):
    """Classifiable step types for routing."""
    PLANNING = "planning"
    CODING = "coding"
    RESEARCH = "research"
    REFLECTION = "reflection"
    SECURITY = "security"
    DATA_ANALYSIS = "data_analysis"
    WRITING = "writing"
    GENERAL = "general"


# ── Route Configuration ─────────────────────────────────────────────


@dataclass
class ModelRoute:
    """A routing rule mapping step type to model."""
    step_type: StepType
    provider: str          # "cloud", "local"
    model: str             # "gemini-2.0-flash", "gemini-2.5-pro", etc.
    temperature: float = 0.7
    max_tokens: int = 4096
    reason: str = ""       # Why this model was chosen

    def to_dict(self) -> dict:
        return {
            "step_type": self.step_type.value,
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "reason": self.reason,
        }


# ── Default Routing Table ────────────────────────────────────────────


# Environment-driven defaults
_FAST_MODEL = os.getenv("ROUTER_FAST_MODEL", "gemini-2.0-flash")
_POWER_MODEL = os.getenv("ROUTER_POWER_MODEL", "gemini-2.5-pro")
_DEFAULT_PROVIDER = os.getenv("ROUTER_DEFAULT_PROVIDER", "cloud")

DEFAULT_ROUTES: dict[StepType, ModelRoute] = {
    StepType.PLANNING: ModelRoute(
        step_type=StepType.PLANNING,
        provider=_DEFAULT_PROVIDER,
        model=_FAST_MODEL,
        temperature=0.3,
        max_tokens=4096,
        reason="Planning benefits from speed; doesn't need heavy reasoning",
    ),
    StepType.CODING: ModelRoute(
        step_type=StepType.CODING,
        provider=_DEFAULT_PROVIDER,
        model=_POWER_MODEL,
        temperature=0.2,
        max_tokens=8192,
        reason="Code generation requires strong reasoning and accuracy",
    ),
    StepType.RESEARCH: ModelRoute(
        step_type=StepType.RESEARCH,
        provider=_DEFAULT_PROVIDER,
        model=_FAST_MODEL,
        temperature=0.5,
        max_tokens=4096,
        reason="Research is exploratory; speed matters more than depth",
    ),
    StepType.REFLECTION: ModelRoute(
        step_type=StepType.REFLECTION,
        provider=_DEFAULT_PROVIDER,
        model=_FAST_MODEL,
        temperature=0.4,
        max_tokens=2048,
        reason="Reflection is meta-reasoning; fast model is sufficient",
    ),
    StepType.SECURITY: ModelRoute(
        step_type=StepType.SECURITY,
        provider=_DEFAULT_PROVIDER,
        model=_POWER_MODEL,
        temperature=0.1,
        max_tokens=4096,
        reason="Security review must be thorough and cautious",
    ),
    StepType.DATA_ANALYSIS: ModelRoute(
        step_type=StepType.DATA_ANALYSIS,
        provider=_DEFAULT_PROVIDER,
        model=_POWER_MODEL,
        temperature=0.3,
        max_tokens=8192,
        reason="Data analysis needs precise reasoning",
    ),
    StepType.WRITING: ModelRoute(
        step_type=StepType.WRITING,
        provider=_DEFAULT_PROVIDER,
        model=_FAST_MODEL,
        temperature=0.7,
        max_tokens=4096,
        reason="Writing tasks benefit from creativity",
    ),
    StepType.GENERAL: ModelRoute(
        step_type=StepType.GENERAL,
        provider=_DEFAULT_PROVIDER,
        model=_FAST_MODEL,
        temperature=0.7,
        max_tokens=4096,
        reason="Default route for uncategorised steps",
    ),
}


# ── Step Type Classifier ─────────────────────────────────────────────

# Keywords that signal a step type (checked against step description + tool hints)
_STEP_TYPE_SIGNALS: dict[StepType, list[str]] = {
    StepType.CODING: [
        "code", "implement", "write function", "refactor", "debug", "fix bug",
        "compile", "build", "test", "unittest", "script", "module", "class",
        "execute_code", "host_write", "create_skill",
    ],
    StepType.RESEARCH: [
        "research", "search", "find", "look up", "investigate", "explore",
        "web_read", "web_search", "host_read", "host_tree", "host_find",
    ],
    StepType.SECURITY: [
        "security", "audit", "vulnerability", "permission", "access control",
        "encrypt", "credential", "secret", "auth", "CVE",
    ],
    StepType.DATA_ANALYSIS: [
        "data", "analyse", "analyze", "statistics", "aggregate", "query",
        "database", "SQL", "CSV", "JSON", "parse", "transform",
    ],
    StepType.WRITING: [
        "write", "document", "draft", "compose", "email", "readme",
        "report", "summary", "blog", "post",
    ],
    StepType.REFLECTION: [
        "reflect", "review", "evaluate", "assess", "critique",
    ],
    StepType.PLANNING: [
        "plan", "decompose", "break down", "outline", "strategy",
    ],
}


def classify_step(description: str, expected_tools: list[str] = None) -> StepType:
    """
    Classify a step by examining its description and tool hints.

    Uses keyword matching (fast, no LLM call). Falls back to GENERAL.
    """
    text = description.lower()
    if expected_tools:
        text += " " + " ".join(expected_tools).lower()

    scores: dict[StepType, int] = {}
    for step_type, keywords in _STEP_TYPE_SIGNALS.items():
        score = sum(1 for kw in keywords if kw.lower() in text)
        if score > 0:
            scores[step_type] = score

    if not scores:
        return StepType.GENERAL

    return max(scores, key=scores.get)


# ── Model Router ─────────────────────────────────────────────────────


class ModelRouter:
    """
    Routes agent loop steps to the optimal model+provider combination.

    Usage:
        router = ModelRouter()
        route = router.select(step_description, expected_tools)
        # route.provider, route.model, route.temperature, route.max_tokens
    """

    def __init__(
        self,
        custom_routes: dict[StepType, ModelRoute] = None,
        fallback_provider: str = "",
        fallback_model: str = "",
    ):
        self._routes = dict(DEFAULT_ROUTES)
        if custom_routes:
            self._routes.update(custom_routes)

        self._fallback_provider = fallback_provider or _DEFAULT_PROVIDER
        self._fallback_model = fallback_model or _FAST_MODEL

        # Stats for cost tracking
        self._route_counts: dict[StepType, int] = {}

    def select(
        self,
        step_description: str = "",
        expected_tools: list[str] = None,
        step_type: StepType = None,
    ) -> ModelRoute:
        """
        Select the best model for a step.

        Args:
            step_description: Natural language step description.
            expected_tools: Tool names the step might use.
            step_type: Override — skip classification if you already know.

        Returns:
            ModelRoute with provider, model, temperature, max_tokens.
        """
        if step_type is None:
            step_type = classify_step(step_description, expected_tools)

        route = self._routes.get(step_type, self._routes[StepType.GENERAL])

        # Track usage
        self._route_counts[step_type] = self._route_counts.get(step_type, 0) + 1

        logger.debug(
            f"Routed step to {route.model} ({step_type.value}): "
            f"{step_description[:80]}..."
        )
        return route

    def override(self, step_type: StepType, route: ModelRoute) -> None:
        """Override a specific route (e.g. from workspace config)."""
        self._routes[step_type] = route
        logger.info(f"Route override: {step_type.value} → {route.model}")

    def get_stats(self) -> dict:
        """Return routing statistics for observability."""
        return {
            st.value: count
            for st, count in sorted(
                self._route_counts.items(), key=lambda x: -x[1]
            )
        }

    def get_config(self) -> list[dict]:
        """Return current routing configuration."""
        return [r.to_dict() for r in self._routes.values()]

    async def load_workspace_overrides(self, pool, workspace_id: str) -> None:
        """Load per-workspace routing overrides from the database."""
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT step_type, provider, model, temperature, max_tokens
                    FROM model_routing_config
                    WHERE workspace_id = $1
                    """,
                    workspace_id,
                )
            for row in rows:
                try:
                    st = StepType(row["step_type"])
                    self._routes[st] = ModelRoute(
                        step_type=st,
                        provider=row["provider"],
                        model=row["model"],
                        temperature=row.get("temperature", 0.7),
                        max_tokens=row.get("max_tokens", 4096),
                        reason="workspace override",
                    )
                except (ValueError, KeyError):
                    continue
            if rows:
                logger.info(
                    f"Loaded {len(rows)} workspace routing overrides for {workspace_id}"
                )
        except Exception as e:
            # Table might not exist yet — that's fine
            logger.debug(f"No routing overrides loaded: {e}")
"""
Adaptive Model Router — per-step model selection for cost+quality optimisation.
"""
