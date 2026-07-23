from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

RoutingMode = Literal["off", "shadow", "constrained", "adaptive"]
PrivacyClass = Literal["local_required", "local_preferred", "approved_cloud", "any"]
TargetLocality = Literal["local", "cloud", "hybrid"]
TargetHealth = Literal["unknown", "healthy", "degraded", "open", "unavailable"]


@dataclass(frozen=True)
class AgentTaskContract:
    task_id: str
    run_id: str
    role: str
    task_family: str
    objective: str
    complexity: float
    ambiguity: float
    risk: str
    required_tools: tuple[str, ...] = ()
    required_capabilities: tuple[str, ...] = ()
    required_modalities: tuple[str, ...] = ()
    minimum_context_tokens: int | None = None
    structured_output_required: bool = False
    privacy_class: PrivacyClass = "approved_cloud"
    local_preferred: bool = False
    local_required: bool = False
    maximum_cost_usd: float | None = None
    preferred_target_tags: tuple[str, ...] = ()
    forbidden_target_tags: tuple[str, ...] = ()
    preferred_provider_profiles: tuple[str, ...] = ()
    forbidden_provider_profiles: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("complexity", "ambiguity"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{name} must be a finite number")
            if not 0.0 <= float(value) <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.minimum_context_tokens is not None and self.minimum_context_tokens < 1:
            raise ValueError("minimum_context_tokens must be positive")
        if self.maximum_cost_usd is not None:
            if not math.isfinite(self.maximum_cost_usd) or self.maximum_cost_usd < 0:
                raise ValueError("maximum_cost_usd must be a finite non-negative number")
        if self.local_required and self.privacy_class != "local_required":
            object.__setattr__(self, "privacy_class", "local_required")

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in (
            "required_tools",
            "required_capabilities",
            "required_modalities",
            "preferred_target_tags",
            "forbidden_target_tags",
            "preferred_provider_profiles",
            "forbidden_provider_profiles",
        ):
            payload[key] = list(payload[key])
        return payload

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ModelTarget:
    target_id: str
    provider_profile_id: str
    provider: str
    model: str
    enabled: bool = True
    locality: TargetLocality = "cloud"
    trust_class: str = "standard"
    capability_tags: tuple[str, ...] = ()
    role_affinities: tuple[str, ...] = ()
    task_family_affinities: tuple[str, ...] = ()
    max_context_tokens: int | None = None
    supports_tools: bool = False
    supports_json: bool = False
    supports_vision: bool = False
    supports_reasoning: bool = False
    supports_streaming: bool = False
    quality_tier: int = 1
    latency_tier: int = 3
    operator_priority: int = 0
    estimated_cost_usd: float | None = None
    health: TargetHealth = "unknown"
    recent_failure_rate: float = 0.0
    predicted_success: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.target_id.strip():
            raise ValueError("target_id is required")
        if not self.provider_profile_id.strip():
            raise ValueError("provider_profile_id is required")
        if not 1 <= self.quality_tier <= 5:
            raise ValueError("quality_tier must be between 1 and 5")
        if not 1 <= self.latency_tier <= 5:
            raise ValueError("latency_tier must be between 1 and 5")
        if not 0.0 <= self.recent_failure_rate <= 1.0:
            raise ValueError("recent_failure_rate must be between 0 and 1")
        if self.predicted_success is not None and not 0.0 <= self.predicted_success <= 1.0:
            raise ValueError("predicted_success must be between 0 and 1")
        if self.estimated_cost_usd is not None:
            if not math.isfinite(self.estimated_cost_usd) or self.estimated_cost_usd < 0:
                raise ValueError("estimated_cost_usd must be a finite non-negative number")

    def to_public_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("capability_tags", "role_affinities", "task_family_affinities"):
            payload[key] = list(payload[key])
        return payload


@dataclass(frozen=True)
class RoutePolicy:
    policy_id: str = "balanced"
    quality_weight: float = 0.40
    affinity_weight: float = 0.16
    health_weight: float = 0.10
    context_weight: float = 0.08
    locality_weight: float = 0.08
    operator_weight: float = 0.05
    cost_weight: float = 0.08
    latency_weight: float = 0.03
    failure_weight: float = 0.12
    require_different_target_for_review: bool = False
    require_different_model_family_for_review: bool = False
    prefer_different_provider_for_review: bool = False
    minimum_quality_by_risk: dict[str, int] = field(
        default_factory=lambda: {"low": 1, "medium": 2, "high": 3, "critical": 4}
    )

    def __post_init__(self) -> None:
        numeric = (
            self.quality_weight,
            self.affinity_weight,
            self.health_weight,
            self.context_weight,
            self.locality_weight,
            self.operator_weight,
            self.cost_weight,
            self.latency_weight,
            self.failure_weight,
        )
        if any(not math.isfinite(value) or value < 0 for value in numeric):
            raise ValueError("route policy weights must be finite and non-negative")
        for risk, tier in self.minimum_quality_by_risk.items():
            if tier < 1 or tier > 5:
                raise ValueError(f"minimum quality tier for {risk} must be between 1 and 5")


@dataclass(frozen=True)
class RouteCandidate:
    target: ModelTarget
    eligible: bool
    score: float | None
    reason_codes: tuple[str, ...]
    components: dict[str, float] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "target_id": self.target.target_id,
            "provider_profile_id": self.target.provider_profile_id,
            "provider": self.target.provider,
            "model": self.target.model,
            "eligible": self.eligible,
            "score": self.score,
            "reason_codes": list(self.reason_codes),
            "components": dict(self.components),
        }


@dataclass(frozen=True)
class RouteDecision:
    mode: RoutingMode
    policy_id: str
    contract_digest: str
    selected_target: ModelTarget
    selection_kind: str
    score: float
    reason_codes: tuple[str, ...]
    candidates: tuple[RouteCandidate, ...]
    actionable: bool

    def to_payload(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "policy_id": self.policy_id,
            "contract_digest": self.contract_digest,
            "selected_target_id": self.selected_target.target_id,
            "selected_provider_profile_id": self.selected_target.provider_profile_id,
            "selected_provider": self.selected_target.provider,
            "selected_model": self.selected_target.model,
            "selection_kind": self.selection_kind,
            "score": self.score,
            "reason_codes": list(self.reason_codes),
            "actionable": self.actionable,
            "candidates": [candidate.to_payload() for candidate in self.candidates],
        }
