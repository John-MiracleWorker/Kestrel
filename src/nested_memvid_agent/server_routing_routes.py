from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from dataclasses import replace
from typing import Annotated, Any, Literal

from fastapi import Depends, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .lan_discovery_service import (
    LAN_STALE_REASON_ORDER,
    LanDiscoveryConflict,
    LanDiscoveryService,
    LanExpectedRevision,
    LanImportRequest,
    LanReplacementConfirmation,
    LanReviewRequest,
)
from .provider_probe import (
    MAX_DISCOVERY_MODELS,
    MAX_PROBE_TIMEOUT_SECONDS,
    CapabilityEvidence,
    DiscoveredModelProbe,
    ProviderDiscoveryResult,
    ProviderProbeService,
    routing_constraint_presets,
)
from .routing.ledger import RoutingLedger
from .routing.ledger_records import ModelTargetEntry, RoutingRevisionConflict
from .routing.ledger_registry import _lan_is_managed_metadata
from .routing.models import ModelTarget, ProviderProfile, RoutePolicy, RoutingMode
from .routing.router import RoutingUnavailableError
from .routing.runtime import AdaptiveFlockRuntimeConfig
from .routing.service import AdaptiveFlockRoutingService
from .server_support import RequestBodyTooLarge

RoutingLocality = Literal["local", "cloud", "hybrid"]
RoutingHealth = Literal["unknown", "healthy", "degraded", "open", "unavailable"]
RoutingPrivacy = Literal["local_required", "local_preferred", "approved_cloud", "any"]
LanStaleReasonRequest = Literal[
    "interface_changed",
    "network_changed",
    "address_changed",
    "port_changed",
    "transport_security_changed",
    "certificate_changed",
    "api_shape_changed",
    "catalog_changed",
    "model_identity_changed",
    "model_missing",
    "capability_changed",
    "freshness_expired",
]

_LAN_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_LAN_PROFILE_ID_PATTERN = r"^lan-provider-[0-9a-f]{64}$"
_LAN_TARGET_ID_PATTERN = r"^lan-target-[0-9a-f]{64}$"
MAX_LAN_MUTATION_BODY_BYTES = 32 * 1024
LAN_MUTATION_OWNER_PRINCIPAL = "owner:local-runtime:v1"
_LAN_FORBIDDEN_IDENTITY_HEADERS = frozenset(
    {
        "x-kestrel-owner-principal",
        "x-owner-principal",
        "x-authenticated-principal",
    }
)
LanAffinityRequest = Annotated[str, Field(min_length=1, max_length=64, strict=True)]


def _is_lan_managed_conflict(exc: BaseException) -> bool:
    message = str(exc).lower()
    return message.startswith("lan ") or "lan_discovery" in message


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
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


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
    input_cost_per_million_usd: float | None = Field(default=None, ge=0)
    output_cost_per_million_usd: float | None = Field(default=None, ge=0)
    health: RoutingHealth = "unknown"
    recent_failure_rate: float = Field(default=0.0, ge=0, le=1)
    predicted_success: float | None = Field(default=None, ge=0, le=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


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
    expected_revision: int | None = Field(default=None, ge=0, strict=True)


class RoutingPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str | None = Field(default=None, min_length=1, max_length=240)
    task_id: str = Field(min_length=1, max_length=240)
    policy_id: str | None = Field(default=None, min_length=1, max_length=240)
    direct_target_id: str | None = Field(default=None, min_length=1, max_length=240)
    default_privacy_class: RoutingPrivacy = "approved_cloud"
    local_required: bool = False
    maximum_cost_usd: float | None = Field(default=None, ge=0)
    planner_guidance: dict[str, Any] = Field(default_factory=dict)


class ProviderDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_profile_id: str = Field(min_length=1, max_length=240)
    expected_profile_revision: int = Field(ge=1, strict=True)
    max_models: int = Field(default=4, ge=1, le=MAX_DISCOVERY_MODELS)
    timeout_seconds: float = Field(
        default=2.0,
        ge=0.25,
        le=MAX_PROBE_TIMEOUT_SECONDS,
        allow_inf_nan=False,
    )
    probe_capabilities: bool = True


class LanExpectedRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str = Field(pattern=_LAN_TARGET_ID_PATTERN, strict=True)
    revision: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_exact_resource_id(self) -> LanExpectedRevisionRequest:
        if re.fullmatch(_LAN_TARGET_ID_PATTERN, self.resource_id) is None:
            raise ValueError("LAN target identifier is invalid")
        return self


class LanReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_profile_id: str = Field(pattern=_LAN_PROFILE_ID_PATTERN, strict=True)
    expected_profile_revision: int = Field(ge=0, strict=True)
    expected_endpoint_fingerprint: str = Field(
        pattern=_LAN_DIGEST_PATTERN,
        strict=True,
    )
    expected_material_binding_digests: list[str] = Field(
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_material_digests(self) -> LanReplacementRequest:
        if (
            re.fullmatch(_LAN_PROFILE_ID_PATTERN, self.provider_profile_id) is None
            or re.fullmatch(
                _LAN_DIGEST_PATTERN,
                self.expected_endpoint_fingerprint,
            )
            is None
        ):
            raise ValueError("replacement binding is invalid")
        values = self.expected_material_binding_digests
        if any(re.fullmatch(_LAN_DIGEST_PATTERN, value) is None for value in values):
            raise ValueError("replacement material binding digest is invalid")
        if len(values) != len(set(values)):
            raise ValueError("replacement material binding digests must be unique")
        return self


class LanImportRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scan_id: str = Field(min_length=1, max_length=128, strict=True)
    endpoint_binding_digest: str = Field(pattern=_LAN_DIGEST_PATTERN, strict=True)
    expected_terminal_receipt_digest: str = Field(
        pattern=_LAN_DIGEST_PATTERN,
        strict=True,
    )
    expected_observation_digest: str = Field(
        pattern=_LAN_DIGEST_PATTERN,
        strict=True,
    )
    expected_profile_revision: int = Field(ge=0, strict=True)
    expected_target_revisions: list[LanExpectedRevisionRequest]
    replacement: LanReplacementRequest | None = None

    @model_validator(mode="after")
    def validate_target_revisions(self) -> LanImportRouteRequest:
        if any(
            re.fullmatch(_LAN_DIGEST_PATTERN, value) is None
            for value in (
                self.endpoint_binding_digest,
                self.expected_terminal_receipt_digest,
                self.expected_observation_digest,
            )
        ):
            raise ValueError("LAN import digest is invalid")
        target_ids = [item.resource_id for item in self.expected_target_revisions]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("expected target revisions must be unique")
        return self


class LanReviewRouteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_profile_revision: int = Field(ge=0, strict=True)
    expected_target_revision: int = Field(ge=0, strict=True)
    expected_terminal_receipt_digest: str = Field(
        pattern=_LAN_DIGEST_PATTERN,
        strict=True,
    )
    expected_observation_digest: str = Field(
        pattern=_LAN_DIGEST_PATTERN,
        strict=True,
    )
    expected_endpoint_fingerprint: str = Field(
        pattern=_LAN_DIGEST_PATTERN,
        strict=True,
    )
    expected_material_binding_digest: str = Field(
        pattern=_LAN_DIGEST_PATTERN,
        strict=True,
    )
    expected_review_digest: str = Field(pattern=_LAN_DIGEST_PATTERN, strict=True)
    expected_stale_reasons: list[LanStaleReasonRequest] = Field(max_length=12)
    trust_class: Literal["operator_confirmed"]
    intended_roles: list[LanAffinityRequest] = Field(max_length=16)
    task_family_affinities: list[LanAffinityRequest] = Field(max_length=16)
    privacy_acknowledged: bool = Field(strict=True)
    enabled: bool = Field(strict=True)

    @model_validator(mode="after")
    def validate_closed_sequences(self) -> LanReviewRouteRequest:
        if any(
            re.fullmatch(_LAN_DIGEST_PATTERN, value) is None
            for value in (
                self.expected_terminal_receipt_digest,
                self.expected_observation_digest,
                self.expected_endpoint_fingerprint,
                self.expected_material_binding_digest,
                self.expected_review_digest,
            )
        ):
            raise ValueError("LAN review digest is invalid")
        reasons = tuple(self.expected_stale_reasons)
        ordered = tuple(reason for reason in LAN_STALE_REASON_ORDER if reason in reasons)
        if reasons != ordered:
            raise ValueError("stale reasons must be unique and deterministically ordered")
        for values in (self.intended_roles, self.task_family_affinities):
            if values != sorted(set(values)):
                raise ValueError("LAN review affinities must be unique and ordered")
            if any(
                not value
                or unicodedata.normalize("NFC", value) != value
                or any(unicodedata.category(character).startswith("C") for character in value)
                or len(value.encode("utf-8")) > 64
                for value in values
            ):
                raise ValueError("LAN review affinity exceeds 64 UTF-8 bytes")
        return self


def register_routing_routes(
    app: Any,
    *,
    ledger: RoutingLedger,
    runtime: AdaptiveFlockRuntimeConfig,
    http_exception: Callable[..., Exception],
    provider_probe_service: ProviderProbeService | None = None,
    lan_discovery_service: LanDiscoveryService | None = None,
    lan_owner_principal: str | None = None,
) -> None:
    if (lan_discovery_service is None) != (lan_owner_principal is None):
        raise ValueError(
            "LAN discovery service and fixed owner principal must be provided as a pair"
        )
    if lan_owner_principal is not None and lan_owner_principal != LAN_MUTATION_OWNER_PRINCIPAL:
        raise ValueError("LAN mutation routes require the fixed local-runtime owner")
    discovery_service = provider_probe_service or ProviderProbeService()

    async def cache_bounded_lan_request_body(http_request: Request) -> None:
        body = bytearray()
        sentinel_limit = MAX_LAN_MUTATION_BODY_BYTES + 1
        async for chunk in http_request.stream():
            remaining = sentinel_limit - len(body)
            if remaining > 0:
                body.extend(chunk[:remaining])
            if len(body) > MAX_LAN_MUTATION_BODY_BYTES or len(chunk) > remaining:
                http_request._body = bytes(body)
                raise RequestBodyTooLarge("LAN request body exceeds 32 KiB")
        http_request._body = bytes(body)

    async def require_bounded_lan_mutation(http_request: Request) -> bytes:
        if http_request.query_params:
            raise http_exception(
                status_code=400,
                detail={"code": "lan_request_rejected"},
            )
        if any(header in http_request.headers for header in _LAN_FORBIDDEN_IDENTITY_HEADERS):
            raise http_exception(
                status_code=400,
                detail={"code": "lan_request_rejected"},
            )
        content_length = http_request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise http_exception(
                    status_code=400,
                    detail={"code": "lan_request_rejected"},
                ) from exc
            if declared_length < 0:
                raise http_exception(
                    status_code=400,
                    detail={"code": "lan_request_rejected"},
                )
            if declared_length > MAX_LAN_MUTATION_BODY_BYTES:
                raise http_exception(
                    status_code=413,
                    detail={"code": "lan_request_too_large"},
                )
        try:
            await cache_bounded_lan_request_body(http_request)
        except RequestBodyTooLarge as exc:
            raise http_exception(
                status_code=413,
                detail={"code": "lan_request_too_large"},
            ) from exc
        return bytes(http_request._body)

    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate JSON key")
            value[key] = item
        return value

    def reject_nonfinite_constant(value: str) -> object:
        del value
        raise ValueError("non-finite JSON constant")

    async def parse_lan_request(
        http_request: Request,
        model_type: type[LanImportRouteRequest] | type[LanReviewRouteRequest],
    ) -> LanImportRouteRequest | LanReviewRouteRequest:
        raw_body = await require_bounded_lan_mutation(http_request)
        try:
            payload = json.loads(
                raw_body.decode("utf-8"),
                object_pairs_hook=reject_duplicate_keys,
                parse_constant=reject_nonfinite_constant,
            )
        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as exc:
            raise http_exception(
                status_code=400,
                detail={"code": "lan_request_invalid_json"},
            ) from exc
        try:
            return model_type.model_validate(payload, strict=True)
        except ValidationError as exc:
            raise http_exception(
                status_code=422,
                detail={"code": "lan_request_invalid"},
            ) from exc

    async def parse_lan_import_request(
        http_request: Request,
    ) -> LanImportRouteRequest:
        parsed = await parse_lan_request(http_request, LanImportRouteRequest)
        if not isinstance(parsed, LanImportRouteRequest):
            raise RuntimeError("LAN import parser returned the wrong request type")
        return parsed

    async def parse_lan_review_request(
        http_request: Request,
    ) -> LanReviewRouteRequest:
        parsed = await parse_lan_request(http_request, LanReviewRouteRequest)
        if not isinstance(parsed, LanReviewRouteRequest):
            raise RuntimeError("LAN review parser returned the wrong request type")
        return parsed

    if lan_discovery_service is not None and lan_owner_principal is not None:

        @app.post(  # type: ignore[untyped-decorator]
            "/api/routing/lan/import",
        )
        def import_lan_observation(
            request: LanImportRouteRequest = Depends(  # noqa: B008
                parse_lan_import_request
            ),
        ) -> dict[str, Any]:
            try:
                replacement = (
                    None
                    if request.replacement is None
                    else LanReplacementConfirmation(
                        provider_profile_id=request.replacement.provider_profile_id,
                        expected_profile_revision=(request.replacement.expected_profile_revision),
                        expected_endpoint_fingerprint=(
                            request.replacement.expected_endpoint_fingerprint
                        ),
                        expected_material_binding_digests=tuple(
                            request.replacement.expected_material_binding_digests
                        ),
                    )
                )
                typed_request = LanImportRequest(
                    scan_id=request.scan_id,
                    endpoint_binding_digest=request.endpoint_binding_digest,
                    expected_terminal_receipt_digest=(request.expected_terminal_receipt_digest),
                    expected_observation_digest=request.expected_observation_digest,
                    expected_profile_revision=request.expected_profile_revision,
                    expected_target_revisions=tuple(
                        LanExpectedRevision(item.resource_id, item.revision)
                        for item in request.expected_target_revisions
                    ),
                    replacement=replacement,
                )
            except ValueError as exc:
                raise http_exception(
                    status_code=422,
                    detail={"code": "lan_request_invalid"},
                ) from exc
            try:
                result = lan_discovery_service.import_observation(
                    typed_request,
                    authenticated_owner_principal=lan_owner_principal,
                )
            except KeyError as exc:
                raise http_exception(
                    status_code=404,
                    detail={"code": "lan_resource_not_found"},
                ) from exc
            except (LanDiscoveryConflict, ValueError) as exc:
                raise http_exception(
                    status_code=409,
                    detail={"code": "lan_evidence_conflict"},
                ) from exc
            return {
                "profile": (None if result.profile is None else result.profile.to_public_payload()),
                "targets": [item.to_public_payload() for item in result.targets],
                "observation_digest": result.observation_digest,
                "endpoint_fingerprint": result.endpoint_fingerprint,
                "outage_observed": result.outage_observed,
                "affected_target_ids": list(result.affected_target_ids),
                "invalidated_binding_digests": list(result.invalidated_binding_digests),
                "stale_reasons_by_target": [
                    {"target_id": target_id, "reasons": list(reasons)}
                    for target_id, reasons in result.stale_reasons_by_target
                ],
            }

        @app.post(  # type: ignore[untyped-decorator]
            "/api/routing/lan/targets/{target_id}/review",
        )
        def review_lan_target(
            target_id: str,
            request: LanReviewRouteRequest = Depends(  # noqa: B008
                parse_lan_review_request
            ),
        ) -> dict[str, Any]:
            if re.fullmatch(_LAN_TARGET_ID_PATTERN, target_id) is None:
                raise http_exception(
                    status_code=422,
                    detail={"code": "lan_request_rejected"},
                )
            try:
                typed_request = LanReviewRequest(
                    target_id=target_id,
                    expected_profile_revision=request.expected_profile_revision,
                    expected_target_revision=request.expected_target_revision,
                    expected_terminal_receipt_digest=(request.expected_terminal_receipt_digest),
                    expected_observation_digest=request.expected_observation_digest,
                    expected_endpoint_fingerprint=request.expected_endpoint_fingerprint,
                    expected_material_binding_digest=(request.expected_material_binding_digest),
                    expected_review_digest=request.expected_review_digest,
                    expected_stale_reasons=tuple(request.expected_stale_reasons),
                    trust_class=request.trust_class,
                    intended_roles=tuple(request.intended_roles),
                    task_family_affinities=tuple(request.task_family_affinities),
                    privacy_acknowledged=request.privacy_acknowledged,
                    enabled=request.enabled,
                )
            except ValueError as exc:
                raise http_exception(
                    status_code=422,
                    detail={"code": "lan_request_invalid"},
                ) from exc
            try:
                result = lan_discovery_service.review_lan_target(
                    typed_request,
                    authenticated_owner_principal=lan_owner_principal,
                )
            except KeyError as exc:
                raise http_exception(
                    status_code=404,
                    detail={"code": "lan_resource_not_found"},
                ) from exc
            except (LanDiscoveryConflict, ValueError) as exc:
                raise http_exception(
                    status_code=409,
                    detail={"code": "lan_review_conflict"},
                ) from exc
            return {
                "profile": result.profile.to_public_payload(),
                "target": result.target.to_public_payload(),
                "privacy_acknowledgement_digest": (result.privacy_acknowledgement_digest),
                "material_binding_digest": result.material_binding_digest,
            }

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
                "calibrations": len(ledger.list_calibrations()),
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
            status_code = 409 if _is_lan_managed_conflict(exc) else 400
            raise http_exception(status_code=status_code, detail=str(exc)) from exc

    @app.get("/api/routing/targets")  # type: ignore[untyped-decorator]
    def list_model_targets(enabled_only: bool = False) -> list[dict[str, Any]]:
        return [
            item.to_public_payload()
            for item in ledger.list_model_targets(enabled_only=enabled_only)
        ]

    @app.post("/api/routing/targets")  # type: ignore[untyped-decorator]
    def put_model_target(request: ModelTargetRequest) -> dict[str, Any]:
        try:
            current = ledger.get_model_target(request.target_id)
            base_metadata = request.metadata
            if (
                current is not None
                and request.expected_revision is not None
                and "metadata" not in request.model_fields_set
            ):
                base_metadata = current.target.metadata
            if current is not None and _is_discovery_managed(current):
                base_metadata = {
                    **base_metadata,
                    "discovery": current.target.metadata["discovery"],
                }
            metadata = _operator_capability_metadata(
                request,
                base_metadata=base_metadata,
            )
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
                    input_cost_per_million_usd=request.input_cost_per_million_usd,
                    output_cost_per_million_usd=request.output_cost_per_million_usd,
                    health=request.health,
                    recent_failure_rate=request.recent_failure_rate,
                    predicted_success=request.predicted_success,
                    metadata=metadata,
                ),
                expected_revision=request.expected_revision,
            )
            return entry.to_public_payload()
        except RoutingRevisionConflict as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        except (KeyError, ValueError) as exc:
            status_code = 409 if _is_lan_managed_conflict(exc) else 400
            raise http_exception(status_code=status_code, detail=str(exc)) from exc

    @app.get("/api/routing/discovery/presets")  # type: ignore[untyped-decorator]
    def discovery_presets() -> dict[str, object]:
        return {
            "schema": "kestrel.routing.constraint_presets.v1",
            "effect": "filter_or_rank_only",
            "presets": [preset.to_public_payload() for preset in routing_constraint_presets()],
        }

    @app.post("/api/routing/discovery")  # type: ignore[untyped-decorator]
    def discover_provider_targets(
        request: ProviderDiscoveryRequest,
    ) -> dict[str, object]:
        profile_entry = ledger.get_provider_profile(request.provider_profile_id)
        if profile_entry is None:
            raise http_exception(
                status_code=404,
                detail=f"unknown provider profile: {request.provider_profile_id}",
            )
        if request.provider_profile_id.startswith(
            ("lan-provider-", "lan-target-")
        ) or _lan_is_managed_metadata(profile_entry.profile.metadata):
            raise http_exception(
                status_code=409,
                detail="lan managed profile requires the specialized LAN service",
            )
        if profile_entry.revision != request.expected_profile_revision:
            raise http_exception(
                status_code=409,
                detail=(
                    "provider_profile_revision_conflict:"
                    f"{request.provider_profile_id}:{profile_entry.revision}"
                ),
            )
        try:
            discovery = discovery_service.discover(
                profile_entry.profile,
                max_models=request.max_models,
                timeout_seconds=request.timeout_seconds,
                probe_capabilities=request.probe_capabilities,
            )
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        if not discovery.catalog_ok:
            raise http_exception(
                status_code=409,
                detail={
                    "code": "provider_catalog_unavailable",
                    "message": discovery.catalog_error,
                    "catalog": discovery.to_public_payload(),
                },
            )

        # Revalidate after the bounded network phase before making safe,
        # disabled-draft changes. This avoids applying results to an edited
        # profile in the common concurrent-update case.
        current_profile = ledger.get_provider_profile(request.provider_profile_id)
        if current_profile is None or current_profile.revision != request.expected_profile_revision:
            current_revision = 0 if current_profile is None else current_profile.revision
            raise http_exception(
                status_code=409,
                detail=(
                    "provider_profile_revision_conflict:"
                    f"{request.provider_profile_id}:{current_revision}"
                ),
            )
        try:
            (
                target_updates,
                visible_target_count,
                stale_target_ids,
                created_count,
            ) = _plan_discovered_targets(
                ledger,
                profile=current_profile.profile,
                discovery=discovery,
            )
            updated_profile, applied_targets = ledger.apply_provider_inventory(
                replace(
                    current_profile.profile,
                    metadata=_provider_discovery_metadata(
                        current_profile.profile,
                        discovery,
                    ),
                ),
                expected_profile_revision=current_profile.revision,
                target_updates=target_updates,
            )
            targets = applied_targets[:visible_target_count]
        except RoutingRevisionConflict as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        return {
            **discovery.to_public_payload(),
            "provider_profile_revision": updated_profile.revision,
            "created_draft_count": created_count,
            "stale_target_ids": stale_target_ids,
            "targets": [item.to_public_payload() for item in targets],
        }

    @app.get("/api/routing/policies")  # type: ignore[untyped-decorator]
    def list_route_policies(enabled_only: bool = False) -> list[dict[str, Any]]:
        return [
            item.to_public_payload() for item in ledger.list_policies(enabled_only=enabled_only)
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
            if request.run_id is not None and task.run_id != request.run_id:
                raise ValueError("preview task does not belong to run")
            policy_id = request.policy_id or runtime.policy_id
            policy_entry = ledger.get_policy(policy_id)
            if policy_entry is None or not policy_entry.enabled:
                raise RoutingUnavailableError(
                    f"route policy is unavailable: {policy_id}",
                    reason_codes=("route_policy_unavailable",),
                )
            preview_mode: RoutingMode = "shadow" if runtime.mode == "off" else runtime.mode
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
                "run_id": task.run_id,
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
                item.to_payload() for item in ledger.list_decisions(run_id=run_id, task_id=task_id)
            ],
            "outcomes": [
                item.to_payload() for item in ledger.list_outcomes(run_id=run_id, task_id=task_id)
            ],
            "shadows": [
                item.to_payload() for item in ledger.list_shadows(run_id=run_id, task_id=task_id)
            ],
            "calibrations": [
                item.to_payload()
                for item in ledger.list_calibrations(
                    project_id=ledger.state.get_run(run_id).project_id
                )
            ],
        }


def _plan_discovered_targets(
    ledger: RoutingLedger,
    *,
    profile: ProviderProfile,
    discovery: ProviderDiscoveryResult,
) -> tuple[tuple[tuple[ModelTarget, int], ...], int, list[str], int]:
    entries = [
        entry
        for entry in ledger.list_model_targets()
        if entry.target.provider_profile_id == profile.profile_id
    ]
    managed_by_model = {
        entry.target.model: entry for entry in entries if _is_discovery_managed(entry)
    }
    updates: list[tuple[ModelTarget, int]] = []
    created_count = 0
    for model_probe in discovery.models:
        existing = managed_by_model.get(model_probe.model)
        metadata = _target_discovery_metadata(discovery, model_probe, stale=False)
        if existing is None:
            target = _new_discovered_target(profile, model_probe, metadata=metadata)
            updates.append((target, 0))
            created_count += 1
            continue
        target = existing.target
        operator_confirmed = target.enabled or target.trust_class != "unconfirmed"
        updated = replace(
            target,
            metadata={**target.metadata, "discovery": metadata},
        )
        if not operator_confirmed:
            supported = _supported_capabilities(model_probe)
            updated = replace(
                updated,
                enabled=False,
                capability_tags=tuple(sorted(supported)),
                supports_tools="tools" in supported,
                supports_json="structured_output" in supported,
                supports_vision="vision" in supported,
                supports_streaming="streaming" in supported,
                health="healthy" if _generation_observed(model_probe) else "unknown",
            )
        else:
            updated = _apply_observed_probe_authority(updated, model_probe)
        updates.append((updated, existing.revision))

    available_models = set(discovery.catalog_models)
    stale_target_ids: list[str] = []
    if discovery.catalog_complete:
        for entry in entries:
            if not _is_discovery_managed(entry) or entry.target.model in available_models:
                continue
            previous = entry.target.metadata.get("discovery")
            previous_metadata = previous if isinstance(previous, dict) else {}
            stale_metadata = {
                **previous_metadata,
                "schema": "kestrel.routing.target_discovery_evidence.v1",
                "managed": True,
                "stale": True,
                "stale_at": discovery.probed_at,
                "catalog_digest": discovery.catalog_digest,
                "catalog_fetched_at": discovery.catalog_fetched_at,
            }
            updated = replace(
                entry.target,
                enabled=False,
                health="unavailable",
                metadata={**entry.target.metadata, "discovery": stale_metadata},
            )
            updates.append((updated, entry.revision))
            stale_target_ids.append(entry.target.target_id)

    return (
        tuple(updates),
        len(discovery.models),
        sorted(stale_target_ids),
        created_count,
    )


def _operator_capability_metadata(
    request: ModelTargetRequest,
    *,
    base_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(request.metadata if base_metadata is None else base_metadata)
    evidence_payload = metadata.get("capability_evidence")
    evidence = dict(evidence_payload) if isinstance(evidence_payload, dict) else {}
    fields = (
        ("supports_streaming", "streaming"),
        ("supports_json", "structured_output"),
        ("supports_tools", "tools"),
        ("supports_vision", "vision"),
    )
    for field_name, capability in fields:
        if field_name not in request.model_fields_set:
            continue
        item = CapabilityEvidence.operator_supplied(
            capability,
            supported=bool(getattr(request, field_name)),
        )
        evidence[capability] = item.to_public_payload()
    if evidence:
        metadata["capability_evidence"] = evidence
    return metadata


def _provider_discovery_metadata(
    profile: ProviderProfile,
    discovery: ProviderDiscoveryResult,
) -> dict[str, Any]:
    return {
        **profile.metadata,
        "discovery": {
            "schema": "kestrel.routing.provider_catalog_evidence.v1",
            "status": "complete",
            "catalog_digest": discovery.catalog_digest,
            "catalog_source": discovery.catalog_source,
            "catalog_fetched_at": discovery.catalog_fetched_at,
            "catalog_complete": discovery.catalog_complete,
            "catalog_truncated": discovery.catalog_truncated,
            "reported_model_count": discovery.reported_model_count,
            "refreshed_at": discovery.probed_at,
            "catalog_model_count": len(discovery.catalog_models),
            "probed_model_count": len(discovery.models),
        },
    }


def _new_discovered_target(
    profile: ProviderProfile,
    model_probe: DiscoveredModelProbe,
    *,
    metadata: dict[str, object],
) -> ModelTarget:
    supported = _supported_capabilities(model_probe)
    return ModelTarget(
        target_id=_draft_target_id(profile.profile_id, model_probe.model),
        provider_profile_id=profile.profile_id,
        provider=profile.adapter,
        model=model_probe.model,
        enabled=False,
        locality=profile.locality,
        trust_class="unconfirmed",
        capability_tags=tuple(sorted(supported)),
        role_affinities=(),
        task_family_affinities=(),
        supports_tools="tools" in supported,
        supports_json="structured_output" in supported,
        supports_vision="vision" in supported,
        supports_streaming="streaming" in supported,
        quality_tier=1,
        latency_tier=3,
        operator_priority=0,
        estimated_cost_usd=None,
        health="healthy" if _generation_observed(model_probe) else "unknown",
        predicted_success=None,
        metadata={"discovery": metadata},
    )


def _target_discovery_metadata(
    discovery: ProviderDiscoveryResult,
    model_probe: DiscoveredModelProbe,
    *,
    stale: bool,
) -> dict[str, object]:
    return {
        "schema": "kestrel.routing.target_discovery_evidence.v1",
        "managed": True,
        "stale": stale,
        "catalog_digest": discovery.catalog_digest,
        "catalog_fetched_at": discovery.catalog_fetched_at,
        "catalog_complete": discovery.catalog_complete,
        "catalog_truncated": discovery.catalog_truncated,
        "observed_at": discovery.probed_at,
        "model_identity": model_probe.model_identity,
        "identity_provenance": model_probe.identity_provenance,
        "observed_latency_ms": model_probe.latency_ms,
        "capabilities": {
            evidence.capability: {
                "supported": evidence.supported,
                "provenance": evidence.provenance,
                "status": evidence.status,
                "detail": evidence.detail,
            }
            for evidence in model_probe.capabilities
        },
    }


def _supported_capabilities(model_probe: DiscoveredModelProbe) -> set[str]:
    return {
        evidence.capability
        for evidence in model_probe.capabilities
        if evidence.supported is True and evidence.status == "pass"
    }


def _generation_observed(model_probe: DiscoveredModelProbe) -> bool:
    return any(
        evidence.capability == "generation"
        and evidence.supported is True
        and evidence.status == "pass"
        and evidence.provenance == "observed"
        for evidence in model_probe.capabilities
    )


def _apply_observed_probe_authority(
    target: ModelTarget,
    model_probe: DiscoveredModelProbe,
) -> ModelTarget:
    observed = {
        evidence.capability: evidence
        for evidence in model_probe.capabilities
        if evidence.provenance == "observed" and evidence.status in {"pass", "fail"}
    }
    if not observed:
        return target

    capability_tags = set(target.capability_tags)
    for capability, evidence in observed.items():
        if evidence.status == "pass" and evidence.supported is True:
            capability_tags.add(capability)
        else:
            capability_tags.discard(capability)

    health = target.health
    generation = observed.get("generation")
    if generation is not None:
        health = (
            "healthy"
            if generation.status == "pass" and generation.supported is True
            else "unavailable"
        )

    def observed_support(capability: str, current: bool) -> bool:
        evidence = observed.get(capability)
        if evidence is None:
            return current
        return evidence.status == "pass" and evidence.supported is True

    return replace(
        target,
        capability_tags=tuple(sorted(capability_tags)),
        supports_tools=observed_support("tools", target.supports_tools),
        supports_json=observed_support("structured_output", target.supports_json),
        supports_vision=observed_support("vision", target.supports_vision),
        supports_streaming=observed_support("streaming", target.supports_streaming),
        health=health,
    )


def _is_discovery_managed(entry: ModelTargetEntry) -> bool:
    discovery = entry.target.metadata.get("discovery")
    return (
        isinstance(discovery, dict)
        and discovery.get("managed") is True
        and entry.target.target_id
        == _draft_target_id(
            entry.target.provider_profile_id,
            entry.target.model,
        )
    )


def _draft_target_id(profile_id: str, model: str) -> str:
    safe_profile = re.sub(r"[^A-Za-z0-9._-]+", "-", profile_id).strip("-")[:80]
    digest = hashlib.sha256(f"{profile_id}\0{model}".encode()).hexdigest()[:16]
    return f"draft-{safe_profile or 'provider'}-{digest}"
