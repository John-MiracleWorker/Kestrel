from __future__ import annotations
"""
Adaptive Model Router — per-step model selection with provider-aware routing.

Routes different step types to different model+provider combinations:
  - planning    → fast/cheap (Ollama local or cloud flash)
  - coding      → powerful   (cloud pro or large local model)
  - reflection  → fast
  - security    → powerful/cautious (cloud only)
  - general     → workspace default

Supports routing strategies:
  - LOCAL_FIRST  — prefer Ollama, fall back to cloud
  - CLOUD_FIRST  — prefer cloud, fall back to Ollama
  - COST_OPTIMIZED — use local for simple tasks, cloud for complex
  - QUALITY_FIRST — always use most powerful available model

Automatic fallback: if a provider is offline, routes to the next available.
"""

import logging
import os
import time
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


class RoutingStrategy(str, Enum):
    """How to prioritize providers."""
    LOCAL_FIRST = "local_first"        # Prefer Ollama → cloud fallback
    CLOUD_FIRST = "cloud_first"        # Prefer cloud → Ollama fallback
    COST_OPTIMIZED = "cost_optimized"  # Simple → local, complex → cloud
    QUALITY_FIRST = "quality_first"    # Always strongest available model


# ── Route Configuration ─────────────────────────────────────────────


@dataclass
class ModelRoute:
    """A routing rule mapping step type to provider + model."""
    step_type: StepType
    provider: str          # "ollama", "google", "openai", "anthropic", "local"
    model: str             # model ID
    temperature: float = 0.7
    max_tokens: int = 4096
    reason: str = ""

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

# ── Dynamic Model Resolution ────────────────────────────────────────
# We no longer hardcode model names. Instead, at routing time we query
# the model_registry which discovers models from live APIs.

_DEFAULT_STRATEGY = RoutingStrategy(
    os.getenv("ROUTER_STRATEGY", RoutingStrategy.LOCAL_FIRST.value)
)

# Cloud model names — populated dynamically by init_models() from the
# centralized model_registry.  Env-var overrides still work as a safety net.
_CLOUD_FAST_MODEL = os.getenv("ROUTER_CLOUD_FAST_MODEL", "")   # filled at startup
_CLOUD_POWER_MODEL = os.getenv("ROUTER_CLOUD_POWER_MODEL", "") # filled at startup

# Ollama model names — populated dynamically from /api/tags at startup.
# Never read from env vars so we only use models that are actually installed.
_OLLAMA_FAST_MODEL = ""   # filled at startup
_OLLAMA_POWER_MODEL = ""  # filled at startup


async def init_models() -> None:
    """Discover cloud AND Ollama models, populate module globals."""
    global _CLOUD_FAST_MODEL, _CLOUD_POWER_MODEL, _OLLAMA_FAST_MODEL, _OLLAMA_POWER_MODEL
    try:
        from core.model_registry import model_registry

        # Cloud models — use env override if set, otherwise discover
        if not _CLOUD_FAST_MODEL:
            _CLOUD_FAST_MODEL = await model_registry.get_fast_model("google") or "gemini-2.5-flash"
        if not _CLOUD_POWER_MODEL:
            _CLOUD_POWER_MODEL = await model_registry.get_power_model("google") or "gemini-2.5-pro"

        # Ollama — always discover from the live /api/tags endpoint, never env vars
        _OLLAMA_FAST_MODEL = await model_registry.get_ollama_fast_model()
        _OLLAMA_POWER_MODEL = await model_registry.get_ollama_power_model()

        logger.info(
            f"Model router init: "
            f"cloud_fast={_CLOUD_FAST_MODEL}, cloud_power={_CLOUD_POWER_MODEL}, "
            f"ollama_fast={_OLLAMA_FAST_MODEL or '(none)'}, ollama_power={_OLLAMA_POWER_MODEL or '(none)'}"
        )
    except Exception as e:
        logger.warning(f"Dynamic model discovery failed: {e}")
        _CLOUD_FAST_MODEL = _CLOUD_FAST_MODEL or "gemini-2.5-flash"
        _CLOUD_POWER_MODEL = _CLOUD_POWER_MODEL or "gemini-2.5-pro"

# Step types considered "simple" (can run on local models)
_SIMPLE_STEPS = {StepType.RESEARCH, StepType.REFLECTION, StepType.WRITING, StepType.GENERAL}
# Step types considered "complex" (benefit from powerful models)
_COMPLEX_STEPS = {StepType.CODING, StepType.SECURITY, StepType.DATA_ANALYSIS, StepType.PLANNING}


def _build_routes(strategy: RoutingStrategy) -> dict[StepType, ModelRoute]:
    """Build routing table based on strategy.
    
    Ollama model names are read from the already-populated module globals
    (_OLLAMA_FAST_MODEL, _OLLAMA_POWER_MODEL).  If Ollama is unavailable,
    the globals will be empty strings; the router's _is_provider_available
    check + _find_fallback will handle escalation gracefully.
    """
    ollama_fast  = _OLLAMA_FAST_MODEL
    ollama_power = _OLLAMA_POWER_MODEL or ollama_fast  # fall back to fast if no large model

    if strategy == RoutingStrategy.LOCAL_FIRST:
        return {
            StepType.PLANNING: ModelRoute(
                StepType.PLANNING, "ollama", ollama_fast,
                temperature=0.3, max_tokens=4096,
                reason="Planning is fast; local model is sufficient",
            ),
            StepType.CODING: ModelRoute(
                StepType.CODING, "ollama", ollama_power,
                temperature=0.2, max_tokens=8192,
                reason="Code gen with largest local model for quality",
            ),
            StepType.RESEARCH: ModelRoute(
                StepType.RESEARCH, "ollama", ollama_fast,
                temperature=0.5, max_tokens=4096,
                reason="Research is exploratory; local model handles it",
            ),
            StepType.REFLECTION: ModelRoute(
                StepType.REFLECTION, "ollama", ollama_fast,
                temperature=0.4, max_tokens=4096,
                reason="Meta-reasoning; fast local model is fine",
            ),
            StepType.SECURITY: ModelRoute(
                StepType.SECURITY, "google", _CLOUD_POWER_MODEL,
                temperature=0.1, max_tokens=4096,
                reason="Security review needs maximum accuracy → cloud",
            ),
            StepType.DATA_ANALYSIS: ModelRoute(
                StepType.DATA_ANALYSIS, "ollama", ollama_power,
                temperature=0.3, max_tokens=8192,
                reason="Data analysis with large local model",
            ),
            StepType.WRITING: ModelRoute(
                StepType.WRITING, "ollama", ollama_fast,
                temperature=0.7, max_tokens=4096,
                reason="Writing benefits from creativity; local is fine",
            ),
            StepType.GENERAL: ModelRoute(
                StepType.GENERAL, "ollama", ollama_fast,
                temperature=0.7, max_tokens=4096,
                reason="Default: local model for uncategorized steps",
            ),
        }

    elif strategy == RoutingStrategy.CLOUD_FIRST:
        return {
            StepType.PLANNING: ModelRoute(
                StepType.PLANNING, "google", _CLOUD_FAST_MODEL,
                temperature=0.3, max_tokens=4096,
                reason="Cloud flash for fast planning",
            ),
            StepType.CODING: ModelRoute(
                StepType.CODING, "google", _CLOUD_POWER_MODEL,
                temperature=0.2, max_tokens=8192,
                reason="Strongest cloud model for code generation",
            ),
            StepType.RESEARCH: ModelRoute(
                StepType.RESEARCH, "google", _CLOUD_FAST_MODEL,
                temperature=0.5, max_tokens=4096,
                reason="Cloud flash for fast research",
            ),
            StepType.REFLECTION: ModelRoute(
                StepType.REFLECTION, "google", _CLOUD_FAST_MODEL,
                temperature=0.4, max_tokens=4096,
                reason="Fast cloud model for reflection",
            ),
            StepType.SECURITY: ModelRoute(
                StepType.SECURITY, "google", _CLOUD_POWER_MODEL,
                temperature=0.1, max_tokens=4096,
                reason="Maximum accuracy for security",
            ),
            StepType.DATA_ANALYSIS: ModelRoute(
                StepType.DATA_ANALYSIS, "google", _CLOUD_POWER_MODEL,
                temperature=0.3, max_tokens=8192,
                reason="Strong reasoning for data analysis",
            ),
            StepType.WRITING: ModelRoute(
                StepType.WRITING, "google", _CLOUD_FAST_MODEL,
                temperature=0.7, max_tokens=4096,
                reason="Cloud flash for writing tasks",
            ),
            StepType.GENERAL: ModelRoute(
                StepType.GENERAL, "google", _CLOUD_FAST_MODEL,
                temperature=0.7, max_tokens=4096,
                reason="Default: cloud flash for general tasks",
            ),
        }

    elif strategy == RoutingStrategy.COST_OPTIMIZED:
        # Simple steps → fast local, complex steps → cloud
        routes = {}
        for st in StepType:
            if st in _SIMPLE_STEPS:
                routes[st] = ModelRoute(
                    st, "ollama", ollama_fast,
                    temperature=0.5, max_tokens=4096,
                    reason=f"Cost-optimized: {st.value} runs locally",
                )
            else:
                routes[st] = ModelRoute(
                    st, "google", _CLOUD_POWER_MODEL,
                    temperature=0.3, max_tokens=8192,
                    reason=f"Cost-optimized: {st.value} needs cloud quality",
                )
        return routes

    else:  # QUALITY_FIRST
        return {st: ModelRoute(
            st, "google", _CLOUD_POWER_MODEL,
            temperature=0.3, max_tokens=8192,
            reason="Quality-first: always use strongest model",
        ) for st in StepType}


# ── Step Type Classifier ─────────────────────────────────────────────

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


# ── Complexity Estimation ────────────────────────────────────────────

# Signals that indicate a task is too complex for small local models
_COMPLEXITY_SIGNALS_HIGH: list[str] = [
    "architect", "design system", "refactor entire", "migration",
    "security audit", "vulnerability", "CVE", "penetration",
    "multi-step", "multi-file", "cross-module", "distributed",
    "concurrent", "race condition", "deadlock", "transaction",
    "optimize", "performance bottleneck", "memory leak",
    "machine learning", "training", "neural", "fine-tune",
    "cryptograph", "encryption", "certificate",
    "kubernetes", "terraform", "infrastructure",
    "complex", "advanced", "sophisticated", "comprehensive",
    "production", "enterprise", "scale", "high-availability",
    "generate a full", "build a complete", "create an entire",
    "plan", "synthesize", "evaluate", "orchestrate",
]

_COMPLEXITY_SIGNALS_LOW: list[str] = [
    "simple", "quick", "basic", "trivial", "minor",
    "rename", "typo", "fix typo", "update comment",
    "list", "show", "display", "print", "log",
    "add a field", "change the", "set the",
    "read", "fetch", "scrape", "search", "find",
]

# Threshold: complexity >= this → escalate to cloud
_ESCALATION_THRESHOLD = float(os.getenv("ROUTER_ESCALATION_THRESHOLD", "5.0"))


def estimate_complexity(
    description: str,
    step_type: StepType,
    expected_tools: list[str] = None,
    context_messages: int = 0,
) -> float:
    """
    Estimate task complexity on a 0-10 scale.

    Factors:
      - Keyword signals (high/low complexity indicators)
      - Description length (longer = usually more complex)
      - Step type (security, coding inherently score higher)
      - Number of expected tools (more tools = more orchestration)
      - Conversation depth (more messages = evolving complexity)

    Returns a float 0.0 (trivial) to 10.0 (extremely complex).
    """
    score = 0.0
    text = description.lower()

    # 1. Keyword signals (+0.6 per high signal, -0.4 per low signal)
    for signal in _COMPLEXITY_SIGNALS_HIGH:
        if signal in text:
            score += 0.6
    for signal in _COMPLEXITY_SIGNALS_LOW:
        if signal in text:
            score -= 0.4

    # 2. Description length (long descriptions usually = complex tasks)
    char_count = len(description)
    if char_count > 500:
        score += 2.0
    elif char_count > 200:
        score += 1.0
    elif char_count > 100:
        score += 0.5

    # 3. Step type baseline
    type_baselines = {
        StepType.SECURITY: 3.0,      # Security always leans complex
        StepType.CODING: 2.0,        # Code gen benefits from reasoning
        StepType.PLANNING: 1.5,      # Planning requires synthesis and reasoning
        StepType.DATA_ANALYSIS: 1.5, # Data tasks need precision
        StepType.RESEARCH: 0.5,      # Research/scraping is trivial
        StepType.REFLECTION: 0.5,    # Meta-reasoning, light
        StepType.WRITING: 0.5,       # Writing is straightforward
        StepType.GENERAL: 0.5,       # Default
    }
    score += type_baselines.get(step_type, 0.5)

    # 4. Tool count (more tools = more orchestration complexity)
    if expected_tools:
        tool_count = len(expected_tools)
        if tool_count >= 5:
            score += 2.0
        elif tool_count >= 3:
            score += 1.0
        elif tool_count >= 1:
            score += 0.5

    # 5. Conversation depth (longer conversations = evolved complexity)
    if context_messages > 20:
        score += 1.0
    elif context_messages > 10:
        score += 0.5

    # Clamp to 0-10
    return max(0.0, min(10.0, score))


# ── Model Router ─────────────────────────────────────────────────────


class ModelRouter:
    """
    Routes agent steps to the optimal model+provider combination.

    Unlike the previous version, this router is provider-aware:
    - It selects which provider (ollama, google, openai, etc.) to use
    - It checks provider availability before returning a route
    - It falls back automatically if a provider is offline

    Usage:
        router = ModelRouter(strategy=RoutingStrategy.LOCAL_FIRST)
        route = router.select(step_description, expected_tools)
        provider = resolve_provider(route.provider)
        response = await provider.generate_with_tools(model=route.model, ...)
    """

    def __init__(
        self,
        strategy: RoutingStrategy = None,
        custom_routes: dict[StepType, ModelRoute] = None,
        provider_checker=None,  # callable: (name) -> bool
        workspace_provider: str = None,
        workspace_model: str = None,
    ):
        self._strategy = strategy or _DEFAULT_STRATEGY
        self._routes = _build_routes(self._strategy)
        if custom_routes:
            self._routes.update(custom_routes)

        # Optional: inject a function that checks if a provider is available
        # Defaults to checking via providers_registry at runtime
        self._provider_checker = provider_checker
        self._workspace_provider = workspace_provider
        self._workspace_model = workspace_model

        # If a workspace model is configured, only override routes whose
        # provider matches the workspace provider.
        # IMPORTANT: Do NOT apply a model name across providers — a Google
        # model name (e.g. gemini-3-flash-preview) applied to an Ollama
        # route will cause a 404 since Ollama doesn't have that model.
        if workspace_model and workspace_provider:
            for st, route in self._routes.items():
                if route.provider == workspace_provider:
                    route.model = workspace_model

        # Stats for cost tracking
        self._route_counts: dict[StepType, int] = {}
        self._fallback_counts: dict[str, int] = {}
        self._escalation_counts: dict[StepType, int] = {}

        logger.info(f"ModelRouter initialized with strategy={self._strategy.value}")

    def _is_provider_available(self, name: str) -> bool:
        """Check if a provider is currently available."""
        if self._provider_checker:
            return self._provider_checker(name)
        try:
            from providers_registry import get_provider
            return get_provider(name).is_ready()
        except Exception:
            return False

    def _find_fallback(self, original_provider: str, route: ModelRoute) -> ModelRoute:
        """Find a working fallback provider for a route, using live-discovered model names."""
        from core.model_registry import model_registry
        import asyncio

        def _get_ollama_fast() -> str:
            """Return the best fast Ollama model from the registry (sync wrapper)."""
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're inside an async context — return the cached value directly
                    return model_registry._pick("ollama", "fast") or _OLLAMA_FAST_MODEL
                return loop.run_until_complete(model_registry.get_ollama_fast_model())
            except Exception:
                return _OLLAMA_FAST_MODEL

        fallbacks = []

        # Start with the configured workspace provider if available
        if self._workspace_provider and self._workspace_provider != original_provider:
            fallbacks.append((self._workspace_provider, self._workspace_model or ""))

        # Define fallback chains with live model names
        if original_provider in ("ollama", "local"):
            # Local failed → escalate to cloud (use dynamic model names)
            fallbacks.extend([
                ("google", _CLOUD_FAST_MODEL),
                ("google", _CLOUD_POWER_MODEL),
                ("openai", model_registry._pick("openai", "fast") or ""),
                ("anthropic", model_registry._pick("anthropic", "fast") or ""),
            ])
        else:
            # Cloud failed → try Ollama first (also with live model names), then other clouds
            fast_local = _get_ollama_fast()
            fallbacks.extend([
                ("ollama", fast_local),
                ("google", _CLOUD_FAST_MODEL),
                ("openai", model_registry._pick("openai", "fast") or ""),
                ("anthropic", model_registry._pick("anthropic", "fast") or ""),
            ])

        for fb_provider, fb_model in fallbacks:
            if not fb_model or fb_provider == original_provider:
                continue
            if self._is_provider_available(fb_provider):
                self._fallback_counts[fb_provider] = self._fallback_counts.get(fb_provider, 0) + 1
                logger.info(
                    f"Fallback: {original_provider} → {fb_provider}:{fb_model} "
                    f"for {route.step_type.value}"
                )
                return ModelRoute(
                    step_type=route.step_type,
                    provider=fb_provider,
                    model=fb_model,
                    temperature=route.temperature,
                    max_tokens=route.max_tokens,
                    reason=f"fallback from {original_provider}",
                )

        # No fallback available — return original and let it fail at call time
        logger.warning(f"No fallback available for {original_provider}")
        return route

    def _maybe_escalate(self, route: ModelRoute, complexity: float) -> ModelRoute:
        """
        If complexity exceeds threshold and we're on a local model,
        escalate to the best available cloud provider.
        """
        if complexity < _ESCALATION_THRESHOLD:
            return route

        # Only escalate from local providers
        if route.provider not in ("ollama", "local"):
            return route

        # Find the best available cloud provider
        cloud_priority = []
        if self._workspace_provider and self._workspace_provider not in ("ollama", "local"):
            cloud_priority.append((self._workspace_provider, self._workspace_model or ""))

        cloud_priority.extend([
            ("google", _CLOUD_POWER_MODEL),
            ("openai", "gpt-5.2"),
            ("anthropic", "claude-sonnet-4-6"),
        ])

        for cloud_provider, cloud_model in cloud_priority:
            cloud_model = cloud_model or model_registry._pick(cloud_provider, "power") or model_registry._pick(cloud_provider, "fast")
            if not cloud_model:
                continue
            if self._is_provider_available(cloud_provider):
                self._escalation_counts[route.step_type] = (
                    self._escalation_counts.get(route.step_type, 0) + 1
                )
                logger.info(
                    f"Escalating [{route.step_type.value}] "
                    f"{route.provider}:{route.model} → {cloud_provider}:{cloud_model} "
                    f"(complexity={complexity:.1f}, threshold={_ESCALATION_THRESHOLD})"
                )
                return ModelRoute(
                    step_type=route.step_type,
                    provider=cloud_provider,
                    model=cloud_model,
                    temperature=route.temperature,
                    max_tokens=max(route.max_tokens, 8192),  # cloud can handle more
                    reason=f"escalated from {route.provider} (complexity={complexity:.1f})",
                )

        # No cloud available — stay local
        return route

    def select(
        self,
        step_description: str = "",
        expected_tools: list[str] = None,
        step_type: StepType = None,
        context_messages: int = 0,
    ) -> ModelRoute:
        """
        Select the best model+provider for a step.

        Performs three checks:
        1. Classify step type and look up the base route
        2. Estimate complexity — escalate local → cloud if too complex
        3. Check provider availability — fall back if offline

        Returns a ModelRoute with provider, model, temperature, max_tokens.
        """
        if step_type is None:
            step_type = classify_step(step_description, expected_tools)

        route = self._routes.get(step_type, self._routes[StepType.GENERAL])

        # Estimate complexity and escalate if needed
        complexity = estimate_complexity(
            step_description, step_type, expected_tools, context_messages
        )

        if self._strategy in (RoutingStrategy.LOCAL_FIRST, RoutingStrategy.COST_OPTIMIZED):
            route = self._maybe_escalate(route, complexity)

        # Escalate heavy tool steps from local to cloud proactively.
        # 1-2 tools: let ollama try (failover catches errors gracefully)
        # 3+ tools: go straight to cloud for speed/reliability.
        # SKIP this escalation if a workspace model is explicitly configured —
        # the user chose that model because it's capable enough.
        if (
            expected_tools
            and len(expected_tools) > 2
            and route.provider in ("ollama", "local")
            and not self._workspace_model  # Don't override user's explicit choice
        ):
            logger.info(
                f"Tool-calling step on {route.provider} → escalating to cloud "
                f"({len(expected_tools)} tools expected)"
            )
            route = self._maybe_escalate(
                route,
                max(complexity, _ESCALATION_THRESHOLD + 1),  # Force escalation
            )

        # Check availability and fall back if needed
        if not self._is_provider_available(route.provider):
            route = self._find_fallback(route.provider, route)

        # Track usage
        self._route_counts[step_type] = self._route_counts.get(step_type, 0) + 1

        logger.debug(
            f"Routed [{step_type.value}] → {route.provider}:{route.model} "
            f"(complexity={complexity:.1f}, temp={route.temperature}) "
            f"| {step_description[:60]}..."
        )
        return route

    def override(self, step_type: StepType, route: ModelRoute) -> None:
        """Override a specific route (e.g. from workspace config)."""
        self._routes[step_type] = route
        logger.info(f"Route override: {step_type.value} → {route.provider}:{route.model}")

    def set_strategy(self, strategy: RoutingStrategy) -> None:
        """Change routing strategy and rebuild routes."""
        self._strategy = strategy
        self._routes = _build_routes(strategy)
        logger.info(f"Routing strategy changed to {strategy.value}")

    def get_stats(self) -> dict:
        """Return routing + fallback + escalation statistics."""
        return {
            "routes": {
                st.value: count
                for st, count in sorted(self._route_counts.items(), key=lambda x: -x[1])
            },
            "fallbacks": dict(self._fallback_counts),
            "escalations": {
                st.value: count
                for st, count in self._escalation_counts.items()
            },
            "strategy": self._strategy.value,
            "escalation_threshold": _ESCALATION_THRESHOLD,
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
            logger.debug(f"No routing overrides loaded: {e}")
