from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .routing.ledger import RoutingLedger
from .routing.ledger_records import RoutingRevisionConflict
from .routing.models import ModelTarget, ProviderProfile, RoutePolicy, RoutingMode
from .routing.router import RoutingUnavailableError
from .routing.runtime import AdaptiveFlockRuntimeConfig
from .routing.service import AdaptiveFlockRoutingService

RoutingLocality = Literal["local", "cloud", "hybrid"]
RoutingHealth = Literal["unknown", "healthy", "degraded", "open", "unavailable"]
RoutingPrivacy = Literal["local_required", "local_preferred", "approved_cloud", "any"]


class ProviderProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=240)
    adapter: str = Field(min_length=1, max_length=120)
    base_url: str | None = None
    secret_ref: str | None = None
    enabled: bool = True
    locality: RoutingLocality = "cloud"
    trust_class: str = "standard"
    max_concurrency: int = Field(default=1, ge=1, le=1024)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_revision: int | None = Field(default=None, ge=0)


class ModelTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str = Field(min_length=1, max_length=240)
    provider_profile_id: str = Field(min_length=1, max_length=240)
    provider: str = Field(min_length=1, max_length=120)
    model: str = Field(min_length=1, max_length=512)
    enabled: bool = True
    locality: RoutingLocality = "cloud"
    trust_class: str = "standard"
    capability_tags: list[str] = Field(default_factory=list)
    role_affinities: list[str] = Field(default_factory=list)
    task_family_affinities: list[str] = Field(default_factory=list)
    max_context_tokens: int | None = Field(default=None, ge=1)
    supports_tools: bool = False
    supports_json: bool = False
    supports_vision: bool = False
    supports_reasoning: bool = False
    supports_streaming: bool = False
    quality_tier: int = Field(default=1, ge=1, le=5)
    latency_tier: int = Field(default=3, ge=1, le=5)
    operator_priority: int = Field(default=0, ge=-10, le=10)
    estimated_cost_usd: float | None = Field(default=None, ge=0)
    health: RoutingHealth = "unknown"
    recent_failure_rate: float = Field(default=0.0, ge=0, le=1)
    predicted_success: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_revision: int | None = Field(default=None, ge=0)


class RoutePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_id: str = Field(default="balanced", min_length=1, max_length=240)
    enabled: bool = True
    quality_weight: float = Field(default=0.40, ge=0)
    affinity_weight: float = Field(default=0.16, ge=0)
    health_weight: float = Field(default=0.10, ge=0)
    context_weight: float = Field(default=0.08, ge=0)
    locality_weight: float = Field(default=0.08, ge=0)
    operator_weight: float = Field(default=0.05, ge=0)
    cost_weight: float = Field(default=0.08, ge=0)
    latency_weight: float = Field(default=0.03, ge=0)
    failure_weight: float = Field(default=0.12, ge=0)
    require_different_target_for_review: bool = False
    require_different_model_family_for_review: bool = False
    prefer_different_provider_for_review: bool = False
    minimum_quality_by_risk: dict[str, int] = Field(
        default_factory=lambda: {"low": 1, "medium": 2, "high": 3, "critical": 4}
    )
    expected_revision: int | None = Field(default=None, ge=0)


class RoutingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(min_length=1, max_length=240)
    task_id: str = Field(min_length=1, max_length=240)
    policy_id: str | None = Field(default=None, min_length=1, max_length=240)
    direct_target_id: str | None = Field(default=None, min_length=1, max_length=240)
    default_privacy_class: RoutingPrivacy = "approved_cloud"
    local_required: bool = False
    maximum_cost_usd: float | None = Field(default=None, ge=0)
    planner_guidance: dict[str, Any] = Field(default_factory=dict)


def register_routing_routes(
    app: Any,
    *,
    ledger: RoutingLedger,
    runtime: AdaptiveFlockRuntimeConfig,
    http_exception: Callable[..., Exception],
) -> None:
    @app.get("/api/routing/status")  # type: ignore[untyped-decorator]
    def routing_status() -> dict[str, object]:
        profiles = ledger.list_provider_profiles()
        targets = ledger.list_model_targets()
        policies = ledger.list_policies()
        return {
            "schema": "kestrel.adaptive_flock.status.v1",
            "runtime": runtime.to_public_payload(),
            "routing_schema_version": ledger.schema_version(),
            "counts": {
                "provider_profiles": len(profiles),
                "enabled_provider_profiles": sum(1 for item in profiles if item.profile.enabled),
                "model_targets": len(targets),
                "enabled_model_targets": sum(1 for item in targets if item.target.enabled),
                "policies": len(policies),
                "enabled_policies": sum(1 for item in policies if item.enabled),
            },
        }

    @app.get("/api/routing/providers")  # type: ignore[untyped-decorator]
    def list_provider_profiles(enabled_only: bool = False) -> list[dict[str, Any]]:
        return [
            item.to_public_payload()
            for item in ledger.list_provider_profiles(enabled_only=enabled_only)
        ]

    @app.post("/api/routing/providers")  # type: ignore[untyped-decorator]
    def put_provider_profile(request: ProviderProfileRequest) -> dict[str, Any]:
        try:
            current = ledger.get_provider_profile(request.profile_id)
            base_url = request.base_url
            secret_ref = request.secret_ref
            if current is not None and request.expected_revision is not None:
                if "base_url" not in request.model_fields_set:
                    base_url = current.profile.base_url
                if "secret_ref" not in request.model_fields_set:
                    secret_ref = current.profile.secret_ref
            entry = ledger.put_provider_profile(
                ProviderProfile(
                    profile_id=request.profile_id,
                    display_name=request.display_name,
                    adapter=request.adapter,
                    base_url=base_url,
                    secret_ref=secret_ref,
                    enabled=request.enabled,
                    locality=request.locality,
                    trust_class=request.trust_class,
                    max_concurrency=request.max_concurrency,
                    metadata=request.metadata,
                ),
                expected_revision=request.expected_revision,
            )
            return entry.to_public_payload()
        except RoutingRevisionConflict as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.get("/api/routing/targets")  # type: ignore[untyped-decorator]
    def list_model_targets(enabled_only: bool = False) -> list[dict[str, Any]]:
        return [
            item.to_public_payload()
            for item in ledger.list_model_targets(enabled_only=enabled_only)
        ]

    @app.post("/api/routing/targets")  # type: ignore[untyped-decorator]
    def put_model_target(request: ModelTargetRequest) -> dict[str, Any]:
        try:
            entry = ledger.put_model_target(
                ModelTarget(
                    target_id=request.target_id,
                    provider_profile_id=request.provider_profile_id,
                    provider=request.provider,
                    model=request.model,
                    enabled=request.enabled,
                    locality=request.locality,
                    trust_class=request.trust_class,
                    capability_tags=tuple(request.capability_tags),
                    role_affinities=tuple(request.role_affinities),
                    task_family_affinities=tuple(request.task_family_affinities),
                    max_context_tokens=request.max_context_tokens,
                    supports_tools=request.supports_tools,
                    supports_json=request.supports_json,
                    supports_vision=request.supports_vision,
                    supports_reasoning=request.supports_reasoning,
                    supports_streaming=request.supports_streaming,
                    quality_tier=request.quality_tier,
                    latency_tier=request.latency_tier,
                    operator_priority=request.operator_priority,
                    estimated_cost_usd=request.estimated_cost_usd,
                    health=request.health,
                    recent_failure_rate=request.recent_failure_rate,
                    predicted_success=request.predicted_success,
                    metadata=request.metadata,
                ),
                expected_revision=request.expected_revision,
            )
            return entry.to_public_payload()
        except RoutingRevisionConflict as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.get("/api/routing/policies")  # type: ignore[untyped-decorator]
    def list_route_policies(enabled_only: bool = False) -> list[dict[str, Any]]:
        return [
            item.to_public_payload()
            for item in ledger.list_policies(enabled_only=enabled_only)
        ]

    @app.post("/api/routing/policies")  # type: ignore[untyped-decorator]
    def put_route_policy(request: RoutePolicyRequest) -> dict[str, Any]:
        try:
            entry = ledger.put_policy(
                RoutePolicy(
                    policy_id=request.policy_id,
                    quality_weight=request.quality_weight,
                    affinity_weight=request.affinity_weight,
                    health_weight=request.health_weight,
                    context_weight=request.context_weight,
                    locality_weight=request.locality_weight,
                    operator_weight=request.operator_weight,
                    cost_weight=request.cost_weight,
                    latency_weight=request.latency_weight,
                    failure_weight=request.failure_weight,
                    require_different_target_for_review=request.require_different_target_for_review,
                    require_different_model_family_for_review=(
                        request.require_different_model_family_for_review
                    ),
                    prefer_different_provider_for_review=(
                        request.prefer_different_provider_for_review
                    ),
                    minimum_quality_by_risk=dict(request.minimum_quality_by_risk),
                ),
                enabled=request.enabled,
                expected_revision=request.expected_revision,
            )
            return entry.to_public_payload()
        except RoutingRevisionConflict as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.post("/api/routing/preview")  # type: ignore[untyped-decorator]
    def preview_route(request: RoutingPreviewRequest) -> dict[str, object]:
        try:
            task = ledger.state.get_task_node(request.task_id)
            if task.run_id != request.run_id:
                raise ValueError("preview task does not belong to run")
            policy_id = request.policy_id or runtime.policy_id
            policy_entry = ledger.get_policy(policy_id)
            if policy_entry is None or not policy_entry.enabled:
                raise RoutingUnavailableError(
                    f"route policy is unavailable: {policy_id}",
                    reason_codes=("route_policy_unavailable",),
                )
            preview_mode: RoutingMode = (
                "shadow" if runtime.mode == "off" else runtime.mode
            )
            service = AdaptiveFlockRoutingService(
                profiles=[item.profile for item in ledger.list_provider_profiles()],
                targets=[item.target for item in ledger.list_model_targets()],
                policy=policy_entry.policy,
                mode=preview_mode,
            )
            contract, decision = service.preview(
                task,
                planner_guidance=request.planner_guidance,
                default_privacy_class=request.default_privacy_class,
                local_required=request.local_required,
                maximum_cost_usd=request.maximum_cost_usd,
                direct_target_id=request.direct_target_id,
            )
            return {
                "schema": "kestrel.adaptive_flock.preview.v1",
                "run_id": request.run_id,
                "task_id": request.task_id,
                "task": {
                    "task_id": task.task_id,
                    "run_id": task.run_id,
                    "title": task.title,
                    "status": task.status,
                },
                "policy_revision": policy_entry.revision,
                "contract": contract.to_payload(),
                "decision": decision.to_payload(),
            }
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except RoutingUnavailableError as exc:
            raise http_exception(
                status_code=409,
                detail={
                    "code": "routing_unavailable",
                    "message": str(exc),
                    "reason_codes": list(exc.reason_codes),
                },
            ) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/routing")  # type: ignore[untyped-decorator]
    def run_routing(run_id: str, task_id: str | None = None) -> dict[str, object]:
        return {
            "run_id": run_id,
            "task_id": task_id,
            "decisions": [
                item.to_payload()
                for item in ledger.list_decisions(run_id=run_id, task_id=task_id)
            ],
            "outcomes": [
                item.to_payload()
                for item in ledger.list_outcomes(run_id=run_id, task_id=task_id)
            ],
        }
