from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast

from ..config import AgentConfig
from ..event_bus import RunEventBus
from ..mcp_manager import MCPManager
from ..plugin_manager import PluginManager
from ..run_manager import RunManager
from ..skill_manager import SkillManager
from ..state_store import AgentStateStore
from .coordinator import DurableRoutingCoordinator
from .ledger import RoutingLedger
from .models import RoutePolicy, RoutingMode
from .run_manager import AdaptiveFlockRunManager


@dataclass(frozen=True)
class AdaptiveFlockRuntimeConfig:
    enabled: bool = False
    mode: RoutingMode = "off"
    policy_id: str = "balanced"

    def __post_init__(self) -> None:
        if self.enabled and self.mode == "off":
            raise ValueError("Adaptive Flock mode must not be off when the runtime is enabled")
        if not self.enabled and self.mode != "off":
            raise ValueError("Adaptive Flock mode must be off when the runtime is disabled")
        if not self.policy_id.strip():
            raise ValueError("Adaptive Flock policy_id is required")

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
        )

    def to_public_payload(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "mode": self.mode,
            "policy_id": self.policy_id,
        }


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
            enforce_single_owner=enforce_single_owner,
            auto_start=auto_start,
        )
    else:
        coordinator = DurableRoutingCoordinator(
            ledger,
            policy_id=active_routing.policy_id,
            mode=active_routing.mode,
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
