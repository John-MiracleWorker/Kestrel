from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from typing import cast

from ..config import AgentConfig
from ..event_bus import RunEventBus
from ..mcp_manager import MCPManager
from ..plugin_manager import PluginManager
from ..run_manager import RunManager
from ..skill_manager import SkillManager
from ..state_store import AgentStateStore
from .coordinator import DurableRoutingCoordinator
from .learned_router import LearnedRouterConfig
from .ledger import RoutingLedger
from .models import RoutePolicy, RoutingMode
from .run_manager import AdaptiveFlockRunManager


@dataclass(frozen=True)
class AdaptiveFlockRuntimeConfig:
    enabled: bool = False
    mode: RoutingMode = "off"
    policy_id: str = "balanced"
    learned_min_examples: int = 5
    learned_min_target_examples: int = 3
    learned_confidence_threshold: float = 0.70
    learned_activation_margin: float = 0.08
    learned_cost_coverage_threshold: float = 0.80
    learned_decay_half_life_days: float = 30.0
    learned_activation_replay_verified: bool = False

    def __post_init__(self) -> None:
        if self.enabled and self.mode == "off":
            raise ValueError("Adaptive Flock mode must not be off when the runtime is enabled")
        if not self.enabled and self.mode != "off":
            raise ValueError("Adaptive Flock mode must be off when the runtime is disabled")
        if not self.policy_id.strip():
            raise ValueError("Adaptive Flock policy_id is required")
        LearnedRouterConfig(
            min_examples=self.learned_min_examples,
            min_target_examples=self.learned_min_target_examples,
            confidence_threshold=self.learned_confidence_threshold,
            activation_margin=self.learned_activation_margin,
            cost_coverage_threshold=self.learned_cost_coverage_threshold,
            decay_half_life_days=self.learned_decay_half_life_days,
            replay_gate_enabled=self.learned_activation_replay_verified,
        )
        if self.learned_activation_replay_verified and self.mode != "adaptive":
            raise ValueError(
                "learned routing activation requires adaptive mode"
            )

    @classmethod
    def from_env(cls) -> AdaptiveFlockRuntimeConfig:
        enabled = _env_bool("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK")
        configured_mode = os.getenv(
            "NEST_AGENT_ADAPTIVE_FLOCK_MODE",
            "shadow",
        ).strip().lower()
        if configured_mode not in {"off", "shadow", "constrained", "adaptive"}:
            raise ValueError(
                "NEST_AGENT_ADAPTIVE_FLOCK_MODE must be off, shadow, constrained, or adaptive"
            )
        if enabled and configured_mode == "off":
            raise ValueError("Adaptive Flock mode must not be off when the runtime is enabled")
        policy_id = os.getenv("NEST_AGENT_ADAPTIVE_FLOCK_POLICY", "balanced").strip()
        effective_mode = configured_mode if enabled else "off"
        return cls(
            enabled=enabled,
            mode=cast(RoutingMode, effective_mode),
            policy_id=policy_id,
            learned_min_examples=_env_int(
                "NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_MIN_EXAMPLES",
                5,
            ),
            learned_min_target_examples=_env_int(
                "NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_MIN_TARGET_EXAMPLES",
                3,
            ),
            learned_confidence_threshold=_env_float(
                "NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_CONFIDENCE",
                0.70,
            ),
            learned_activation_margin=_env_float(
                "NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_MARGIN",
                0.08,
            ),
            learned_cost_coverage_threshold=_env_float(
                "NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_COST_COVERAGE",
                0.80,
            ),
            learned_decay_half_life_days=_env_float(
                "NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_HALF_LIFE_DAYS",
                30.0,
            ),
            learned_activation_replay_verified=(
                enabled
                and _env_bool(
                    "NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED"
                )
            ),
        )

    def to_public_payload(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "policy_id": self.policy_id,
            "learned": {
                "min_examples": self.learned_min_examples,
                "min_target_examples": self.learned_min_target_examples,
                "confidence_threshold": self.learned_confidence_threshold,
                "activation_margin": self.learned_activation_margin,
                "cost_coverage_threshold": self.learned_cost_coverage_threshold,
                "decay_half_life_days": self.learned_decay_half_life_days,
                "activation_replay_verified": (
                    self.learned_activation_replay_verified
                ),
            },
        }

    def learned_router_config(self) -> LearnedRouterConfig:
        return LearnedRouterConfig(
            min_examples=self.learned_min_examples,
            min_target_examples=self.learned_min_target_examples,
            confidence_threshold=self.learned_confidence_threshold,
            activation_margin=self.learned_activation_margin,
            cost_coverage_threshold=self.learned_cost_coverage_threshold,
            decay_half_life_days=self.learned_decay_half_life_days,
            replay_gate_enabled=self.learned_activation_replay_verified,
        )


@dataclass(frozen=True)
class RunManagerBuild:
    runs: RunManager
    routing_ledger: RoutingLedger
    routing_config: AdaptiveFlockRuntimeConfig


def build_run_manager(
    *,
    config: AgentConfig,
    state: AgentStateStore,
    events: RunEventBus,
    mcp: MCPManager,
    skills: SkillManager,
    plugins: PluginManager | None = None,
    secret_resolver: Callable[[str | None], str | None] | None = None,
    enforce_single_owner: bool = False,
    auto_start: bool = True,
    routing_config: AdaptiveFlockRuntimeConfig | None = None,
) -> RunManagerBuild:
    active_routing = routing_config or AdaptiveFlockRuntimeConfig.from_env()
    ledger = RoutingLedger(state)
    _ensure_policy(ledger, active_routing.policy_id)
    if not active_routing.enabled:
        runs: RunManager = RunManager(
            config=config,
            state=state,
            events=events,
            mcp=mcp,
            skills=skills,
            plugins=plugins,
            secret_resolver=secret_resolver,
            lan_runtime_authority_resolver=None,
            enforce_single_owner=enforce_single_owner,
            auto_start=auto_start,
        )
    else:
        lan_runtime_authority_resolver = ledger.resolve_lan_runtime_authority
        coordinator = DurableRoutingCoordinator(
            ledger,
            policy_id=active_routing.policy_id,
            mode=active_routing.mode,
            learned_config=active_routing.learned_router_config(),
            lan_runtime_authority_resolver=lan_runtime_authority_resolver,
        )
        runs = AdaptiveFlockRunManager(
            routing_coordinator=coordinator,
            config=config,
            state=state,
            events=events,
            mcp=mcp,
            skills=skills,
            plugins=plugins,
            secret_resolver=secret_resolver,
            enforce_single_owner=enforce_single_owner,
            auto_start=auto_start,
        )
    return RunManagerBuild(
        runs=runs,
        routing_ledger=ledger,
        routing_config=active_routing,
    )


def _ensure_policy(ledger: RoutingLedger, policy_id: str) -> None:
    if ledger.get_policy(policy_id) is not None:
        return
    if policy_id != "balanced":
        raise ValueError(f"Adaptive Flock policy is not configured: {policy_id}")
    ledger.put_policy(RoutePolicy(policy_id=policy_id))


def _env_bool(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not isfinite(value):
        raise ValueError(f"{name} must be finite")
    return value
