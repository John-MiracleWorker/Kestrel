"""Disabled-only import and owner review for authenticated private-LAN evidence."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

from .lan_http_transport import InterfaceInventoryResolver
from .lan_runtime_authority import LAN_OPENAI_RUNTIME_HARDENING_VERSION
from .routing.lan_serialization import (
    LAN_OBSERVATION_MAX_AGE_SECONDS as LAN_OBSERVATION_MAX_AGE_SECONDS,
)
from .routing.lan_serialization import (
    LAN_OBSERVATION_MAX_FUTURE_SKEW_SECONDS as LAN_OBSERVATION_MAX_FUTURE_SKEW_SECONDS,
)
from .routing.lan_serialization import (
    validate_digest,
    validate_required_text,
)
from .routing.ledger import RoutingLedger
from .routing.ledger_records import (
    ModelTargetEntry,
    ProviderProfileEntry,
    RoutingRevisionConflict,
)

_LAN_PROFILE_ID_RE = re.compile(r"lan-provider-[0-9a-f]{64}\Z")
_LAN_TARGET_ID_RE = re.compile(r"lan-target-[0-9a-f]{64}\Z")
_LAN_SCAN_ID_RE = re.compile(r"lan_[0-9a-f]{32}\Z")
_MAX_AFFINITIES = 16
_MAX_AFFINITY_UTF8_BYTES = 64

LanStaleReason = Literal[
    "interface_changed",
    "address_changed",
    "port_changed",
    "network_changed",
    "transport_security_changed",
    "certificate_changed",
    "api_shape_changed",
    "catalog_changed",
    "model_identity_changed",
    "model_missing",
    "capability_changed",
    "freshness_expired",
]

LAN_STALE_REASON_ORDER: tuple[LanStaleReason, ...] = (
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
)


class LanDiscoveryConflict(RuntimeError):
    """A closed failure caused by stale, mismatched, or unauthorized LAN state."""


@dataclass(frozen=True)
class LanExpectedRevision:
    resource_id: str
    revision: int

    def __post_init__(self) -> None:
        _validate_target_id(self.resource_id)
        _validate_revision(self.revision)


@dataclass(frozen=True)
class LanReplacementConfirmation:
    provider_profile_id: str
    expected_profile_revision: int
    expected_endpoint_fingerprint: str
    expected_material_binding_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_profile_id(self.provider_profile_id)
        _validate_revision(self.expected_profile_revision)
        _validate_exact_digest(
            self.expected_endpoint_fingerprint,
            "expected_endpoint_fingerprint",
        )
        if type(self.expected_material_binding_digests) is not tuple:
            raise ValueError("replacement material digests must be an exact tuple")
        if not self.expected_material_binding_digests:
            raise ValueError("replacement material digests must not be empty")
        for digest in self.expected_material_binding_digests:
            _validate_exact_digest(digest, "expected_material_binding_digest")
        if len(set(self.expected_material_binding_digests)) != len(
            self.expected_material_binding_digests
        ):
            raise ValueError("replacement material digests must be unique")


@dataclass(frozen=True)
class LanImportRequest:
    scan_id: str
    endpoint_binding_digest: str
    expected_terminal_receipt_digest: str
    expected_observation_digest: str
    expected_profile_revision: int
    expected_target_revisions: tuple[LanExpectedRevision, ...]
    replacement: LanReplacementConfirmation | None = None

    def __post_init__(self) -> None:
        _validate_canonical_text(self.scan_id, "scan_id", maximum=128)
        _validate_exact_digest(self.endpoint_binding_digest, "endpoint_binding_digest")
        _validate_exact_digest(
            self.expected_terminal_receipt_digest,
            "expected_terminal_receipt_digest",
        )
        _validate_exact_digest(
            self.expected_observation_digest,
            "expected_observation_digest",
        )
        _validate_revision(self.expected_profile_revision)
        if type(self.expected_target_revisions) is not tuple:
            raise ValueError("expected target revisions must be an exact tuple")
        if not all(type(item) is LanExpectedRevision for item in self.expected_target_revisions):
            raise ValueError("expected target revisions must be exactly typed")
        for item in self.expected_target_revisions:
            _validate_target_id(item.resource_id)
            _validate_revision(item.revision)
        resource_ids = [item.resource_id for item in self.expected_target_revisions]
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("expected target revisions must be unique")
        if (
            self.replacement is not None
            and type(self.replacement) is not LanReplacementConfirmation
        ):
            raise ValueError("replacement confirmation must be exactly typed")


@dataclass(frozen=True)
class LanReviewRequest:
    target_id: str
    expected_profile_revision: int
    expected_target_revision: int
    expected_terminal_receipt_digest: str
    expected_observation_digest: str
    expected_endpoint_fingerprint: str
    expected_material_binding_digest: str
    expected_review_digest: str
    expected_stale_reasons: tuple[LanStaleReason, ...]
    trust_class: Literal["operator_confirmed"]
    intended_roles: tuple[str, ...]
    task_family_affinities: tuple[str, ...]
    privacy_acknowledged: bool
    enabled: bool

    def __post_init__(self) -> None:
        _validate_target_id(self.target_id)
        _validate_revision(self.expected_profile_revision)
        _validate_revision(self.expected_target_revision)
        for field in (
            "expected_terminal_receipt_digest",
            "expected_observation_digest",
            "expected_endpoint_fingerprint",
            "expected_material_binding_digest",
            "expected_review_digest",
        ):
            _validate_exact_digest(getattr(self, field), field)
        _validate_stale_reasons(self.expected_stale_reasons)
        if self.trust_class != "operator_confirmed":
            raise ValueError("LAN review trust class must be operator_confirmed")
        _validate_affinities(self.intended_roles, "intended roles")
        _validate_affinities(
            self.task_family_affinities,
            "task-family affinities",
        )
        if type(self.privacy_acknowledged) is not bool:
            raise ValueError("LAN review privacy acknowledgement must be boolean")
        if type(self.enabled) is not bool:
            raise ValueError("LAN review enabled must be boolean")


@dataclass(frozen=True)
class LanImportResult:
    profile: ProviderProfileEntry | None
    targets: tuple[ModelTargetEntry, ...]
    affected_target_ids: tuple[str, ...]
    invalidated_binding_digests: tuple[str, ...]
    stale_reasons_by_target: tuple[tuple[str, tuple[LanStaleReason, ...]], ...]
    observation_digest: str
    endpoint_fingerprint: str | None
    outage_observed: bool


@dataclass(frozen=True)
class LanReviewResult:
    profile: ProviderProfileEntry
    target: ModelTargetEntry
    material_binding_digest: str
    privacy_acknowledgement_digest: str


@dataclass(frozen=True)
class LanImportSelector:
    scan_id: str
    endpoint_id: str
    replacement_provider_profile_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.scan_id) is not str or _LAN_SCAN_ID_RE.fullmatch(self.scan_id) is None:
            raise ValueError("LAN import selector scan ID is invalid")
        _validate_exact_digest(self.endpoint_id, "endpoint_id")
        if self.replacement_provider_profile_id is not None:
            _validate_profile_id(self.replacement_provider_profile_id)


@dataclass(frozen=True)
class LanImportAuthority:
    expected_terminal_receipt_digest: str
    expected_observation_digest: str
    expected_profile_revision: int
    expected_target_revisions: tuple[LanExpectedRevision, ...]
    endpoint_fingerprint: str | None
    replacement: LanReplacementConfirmation | None

    def __post_init__(self) -> None:
        _validate_exact_digest(
            self.expected_terminal_receipt_digest,
            "expected_terminal_receipt_digest",
        )
        _validate_exact_digest(
            self.expected_observation_digest,
            "expected_observation_digest",
        )
        _validate_revision(self.expected_profile_revision)
        if type(self.expected_target_revisions) is not tuple or any(
            type(item) is not LanExpectedRevision
            for item in self.expected_target_revisions
        ):
            raise ValueError("LAN import authority revisions must be exactly typed")
        resource_ids = tuple(
            item.resource_id for item in self.expected_target_revisions
        )
        if len(resource_ids) != len(set(resource_ids)):
            raise ValueError("LAN import authority revisions must be unique")
        if self.endpoint_fingerprint is not None:
            _validate_exact_digest(
                self.endpoint_fingerprint,
                "endpoint_fingerprint",
            )
        if self.replacement is not None:
            if type(self.replacement) is not LanReplacementConfirmation:
                raise ValueError("LAN import replacement authority must be exactly typed")
            LanReplacementConfirmation.__post_init__(self.replacement)


@dataclass(frozen=True)
class LanImportPreview:
    selector: LanImportSelector
    preview_digest: str
    evidence_expires_at: str
    authority: LanImportAuthority
    result: LanImportResult
    requires_confirmation: bool = True

    def __post_init__(self) -> None:
        if type(self.selector) is not LanImportSelector:
            raise ValueError("LAN import preview selector must be exactly typed")
        if type(self.authority) is not LanImportAuthority:
            raise ValueError("LAN import preview authority must be exactly typed")
        if type(self.result) is not LanImportResult:
            raise ValueError("LAN import preview result must be exactly typed")
        LanImportSelector.__post_init__(self.selector)
        LanImportAuthority.__post_init__(self.authority)
        _validate_exact_digest(self.preview_digest, "preview_digest")
        _validate_canonical_utc_timestamp(
            self.evidence_expires_at,
            "evidence_expires_at",
        )
        if (
            type(self.requires_confirmation) is not bool
            or self.requires_confirmation is not True
        ):
            raise ValueError("LAN import preview must require confirmation")
        if self.result.observation_digest != self.authority.expected_observation_digest:
            raise ValueError("LAN import preview evidence authority is inconsistent")


@dataclass(frozen=True)
class LanImportConfirmation:
    selector: LanImportSelector
    preview_digest: str
    confirmed: bool

    def __post_init__(self) -> None:
        if type(self.selector) is not LanImportSelector:
            raise ValueError("LAN import confirmation selector must be exactly typed")
        LanImportSelector.__post_init__(self.selector)
        _validate_exact_digest(self.preview_digest, "preview_digest")
        if type(self.confirmed) is not bool or self.confirmed is not True:
            raise ValueError("LAN import confirmation must be explicit")


@dataclass(frozen=True)
class LanImportConfirmationResult:
    preview_digest: str
    result: LanImportResult

    def __post_init__(self) -> None:
        _validate_exact_digest(self.preview_digest, "preview_digest")
        if type(self.result) is not LanImportResult:
            raise ValueError("LAN import confirmation result must be exactly typed")


@dataclass(frozen=True)
class LanReviewOptions:
    target_id: str
    intended_roles: tuple[str, ...]
    task_family_affinities: tuple[str, ...]
    enabled: bool

    def __post_init__(self) -> None:
        _validate_target_id(self.target_id)
        _validate_affinities(self.intended_roles, "intended roles")
        _validate_affinities(self.task_family_affinities, "task-family affinities")
        if type(self.enabled) is not bool:
            raise ValueError("LAN review enabled must be boolean")


@dataclass(frozen=True)
class LanReviewAuthority:
    provider_profile_id: str
    expected_profile_revision: int
    expected_target_revision: int
    expected_terminal_receipt_digest: str
    expected_observation_digest: str
    expected_endpoint_fingerprint: str
    expected_material_binding_digest: str
    expected_stale_reasons: tuple[LanStaleReason, ...]
    trust_class: Literal["operator_confirmed"]
    privacy_acknowledgement_digest: str
    review_digest: str
    reviewed_material_binding_digest: str
    reviewed_runtime_interface_binding_digest: str | None

    def __post_init__(self) -> None:
        _validate_profile_id(self.provider_profile_id)
        _validate_revision(self.expected_profile_revision)
        _validate_revision(self.expected_target_revision)
        for field in (
            "expected_terminal_receipt_digest",
            "expected_observation_digest",
            "expected_endpoint_fingerprint",
            "expected_material_binding_digest",
            "privacy_acknowledgement_digest",
            "review_digest",
            "reviewed_material_binding_digest",
        ):
            _validate_exact_digest(getattr(self, field), field)
        _validate_stale_reasons(self.expected_stale_reasons)
        if self.trust_class != "operator_confirmed":
            raise ValueError("LAN review authority trust class is invalid")
        if self.reviewed_runtime_interface_binding_digest is not None:
            _validate_exact_digest(
                self.reviewed_runtime_interface_binding_digest,
                "reviewed_runtime_interface_binding_digest",
            )


@dataclass(frozen=True)
class LanReviewPreview:
    options: LanReviewOptions
    preview_digest: str
    evidence_expires_at: str
    authority: LanReviewAuthority
    profile: ProviderProfileEntry
    target: ModelTargetEntry
    requires_privacy_acknowledgement: bool = True
    requires_confirmation: bool = True

    def __post_init__(self) -> None:
        if type(self.options) is not LanReviewOptions:
            raise ValueError("LAN review preview options must be exactly typed")
        if type(self.authority) is not LanReviewAuthority:
            raise ValueError("LAN review preview authority must be exactly typed")
        if type(self.profile) is not ProviderProfileEntry:
            raise ValueError("LAN review preview profile must be exactly typed")
        if type(self.target) is not ModelTargetEntry:
            raise ValueError("LAN review preview target must be exactly typed")
        LanReviewOptions.__post_init__(self.options)
        LanReviewAuthority.__post_init__(self.authority)
        _validate_exact_digest(self.preview_digest, "preview_digest")
        _validate_canonical_utc_timestamp(
            self.evidence_expires_at,
            "evidence_expires_at",
        )
        if (
            type(self.requires_privacy_acknowledgement) is not bool
            or self.requires_privacy_acknowledgement is not True
            or type(self.requires_confirmation) is not bool
            or self.requires_confirmation is not True
        ):
            raise ValueError("LAN review preview must require explicit acknowledgement")
        if self.target.target.target_id != self.options.target_id:
            raise ValueError("LAN review preview target is inconsistent")
        if self.profile.profile.profile_id != self.authority.provider_profile_id:
            raise ValueError("LAN review preview profile authority is inconsistent")


@dataclass(frozen=True)
class LanReviewConfirmation:
    options: LanReviewOptions
    preview_digest: str
    privacy_acknowledged: bool
    confirmed: bool

    def __post_init__(self) -> None:
        if type(self.options) is not LanReviewOptions:
            raise ValueError("LAN review confirmation options must be exactly typed")
        LanReviewOptions.__post_init__(self.options)
        _validate_exact_digest(self.preview_digest, "preview_digest")
        if (
            type(self.privacy_acknowledged) is not bool
            or self.privacy_acknowledged is not True
            or type(self.confirmed) is not bool
            or self.confirmed is not True
        ):
            raise ValueError("LAN review confirmation requires explicit owner acknowledgement")


@dataclass(frozen=True)
class LanReviewConfirmationResult:
    preview_digest: str
    result: LanReviewResult

    def __post_init__(self) -> None:
        _validate_exact_digest(self.preview_digest, "preview_digest")
        if type(self.result) is not LanReviewResult:
            raise ValueError("LAN review confirmation result must be exactly typed")


class LanDiscoveryService:
    """Translate authenticated Task 4 evidence into disabled routing drafts."""

    def __init__(
        self,
        registry: RoutingLedger,
        *,
        clock: Callable[[], datetime] | None = None,
        runtime_hardening_version: str | None = None,
        interface_inventory_resolver: InterfaceInventoryResolver | None = None,
    ) -> None:
        if type(registry) is not RoutingLedger:
            raise ValueError("LAN discovery requires the durable routing ledger")
        self.registry = registry
        self._clock = clock or (lambda: datetime.now(UTC))
        if (
            runtime_hardening_version is not None
            and runtime_hardening_version != LAN_OPENAI_RUNTIME_HARDENING_VERSION
        ):
            raise ValueError("LAN runtime hardening version is not installed")
        self._runtime_hardening_version = runtime_hardening_version
        if interface_inventory_resolver is not None and not callable(
            interface_inventory_resolver
        ):
            raise ValueError("LAN interface inventory resolver must be callable")
        self._interface_inventory_resolver = interface_inventory_resolver

    def prepare_lan_import(
        self,
        selector: LanImportSelector,
        *,
        authenticated_owner_principal: str,
    ) -> LanImportPreview:
        if type(selector) is not LanImportSelector:
            raise ValueError("LAN import selector must be exactly typed")
        LanImportSelector.__post_init__(selector)
        owner = _validate_canonical_owner(authenticated_owner_principal)
        now = _validate_utc_clock(self._clock())
        try:
            plan = self.registry.prepare_server_owned_lan_import(
                scan_id=selector.scan_id,
                endpoint_id=selector.endpoint_id,
                replacement_provider_profile_id=(
                    selector.replacement_provider_profile_id
                ),
                authenticated_owner_principal=owner,
                now=now,
                runtime_hardening_version=self._runtime_hardening_version,
            )
        except RoutingRevisionConflict as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        except ValueError as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        replacement = (
            None
            if plan.replacement is None
            else LanReplacementConfirmation(
                provider_profile_id=plan.replacement[0],
                expected_profile_revision=plan.replacement[1],
                expected_endpoint_fingerprint=plan.replacement[2],
                expected_material_binding_digests=plan.replacement[3],
            )
        )
        result = _lan_import_result_from_plan(plan)
        return LanImportPreview(
            selector=selector,
            preview_digest=plan.preview_digest,
            evidence_expires_at=plan.evidence_expires_at,
            authority=LanImportAuthority(
                expected_terminal_receipt_digest=(
                    plan.expected_terminal_receipt_digest
                ),
                expected_observation_digest=plan.expected_observation_digest,
                expected_profile_revision=plan.expected_profile_revision,
                expected_target_revisions=tuple(
                    LanExpectedRevision(resource_id=target_id, revision=revision)
                    for target_id, revision in plan.expected_target_revisions
                ),
                endpoint_fingerprint=plan.endpoint_fingerprint,
                replacement=replacement,
            ),
            result=result,
        )

    def confirm_lan_import(
        self,
        confirmation: LanImportConfirmation,
        *,
        authenticated_owner_principal: str,
    ) -> LanImportConfirmationResult:
        if type(confirmation) is not LanImportConfirmation:
            raise ValueError("LAN import confirmation must be exactly typed")
        LanImportConfirmation.__post_init__(confirmation)
        owner = _validate_canonical_owner(authenticated_owner_principal)
        now = _validate_utc_clock(self._clock())
        try:
            plan = self.registry.confirm_server_owned_lan_import(
                scan_id=confirmation.selector.scan_id,
                endpoint_id=confirmation.selector.endpoint_id,
                replacement_provider_profile_id=(
                    confirmation.selector.replacement_provider_profile_id
                ),
                preview_digest=confirmation.preview_digest,
                authenticated_owner_principal=owner,
                now=now,
                runtime_hardening_version=self._runtime_hardening_version,
            )
        except RoutingRevisionConflict as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        except ValueError as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        return LanImportConfirmationResult(
            preview_digest=plan.preview_digest,
            result=_lan_import_result_from_plan(plan),
        )

    def prepare_lan_review(
        self,
        options: LanReviewOptions,
        *,
        authenticated_owner_principal: str,
    ) -> LanReviewPreview:
        if type(options) is not LanReviewOptions:
            raise ValueError("LAN review options must be exactly typed")
        LanReviewOptions.__post_init__(options)
        owner = _validate_canonical_owner(authenticated_owner_principal)
        now = _validate_utc_clock(self._clock())
        try:
            plan = self.registry.prepare_server_owned_lan_review(
                target_id=options.target_id,
                intended_roles=options.intended_roles,
                task_family_affinities=options.task_family_affinities,
                enabled=options.enabled,
                authenticated_owner_principal=owner,
                now=now,
                runtime_hardening_version=self._runtime_hardening_version,
                interface_inventory_resolver=self._interface_inventory_resolver,
            )
        except RoutingRevisionConflict as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        except ValueError as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        return LanReviewPreview(
            options=options,
            preview_digest=plan.preview_digest,
            evidence_expires_at=plan.evidence_expires_at,
            authority=LanReviewAuthority(
                provider_profile_id=plan.provider_profile_id,
                expected_profile_revision=plan.expected_profile_revision,
                expected_target_revision=plan.expected_target_revision,
                expected_terminal_receipt_digest=(
                    plan.expected_terminal_receipt_digest
                ),
                expected_observation_digest=plan.expected_observation_digest,
                expected_endpoint_fingerprint=plan.expected_endpoint_fingerprint,
                expected_material_binding_digest=(
                    plan.expected_material_binding_digest
                ),
                expected_stale_reasons=cast(
                    tuple[LanStaleReason, ...],
                    plan.expected_stale_reasons,
                ),
                trust_class="operator_confirmed",
                privacy_acknowledgement_digest=(
                    plan.privacy_acknowledgement_digest
                ),
                review_digest=plan.review_digest,
                reviewed_material_binding_digest=(
                    plan.reviewed_material_binding_digest
                ),
                reviewed_runtime_interface_binding_digest=(
                    plan.reviewed_runtime_interface_binding_digest
                ),
            ),
            profile=plan.result_profile,
            target=plan.result_target,
        )

    def confirm_lan_review(
        self,
        confirmation: LanReviewConfirmation,
        *,
        authenticated_owner_principal: str,
    ) -> LanReviewConfirmationResult:
        if type(confirmation) is not LanReviewConfirmation:
            raise ValueError("LAN review confirmation must be exactly typed")
        LanReviewConfirmation.__post_init__(confirmation)
        owner = _validate_canonical_owner(authenticated_owner_principal)
        now = _validate_utc_clock(self._clock())
        options = confirmation.options
        try:
            plan = self.registry.confirm_server_owned_lan_review(
                target_id=options.target_id,
                intended_roles=options.intended_roles,
                task_family_affinities=options.task_family_affinities,
                enabled=options.enabled,
                preview_digest=confirmation.preview_digest,
                authenticated_owner_principal=owner,
                now=now,
                runtime_hardening_version=self._runtime_hardening_version,
                interface_inventory_resolver=self._interface_inventory_resolver,
            )
        except RoutingRevisionConflict as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        except ValueError as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        return LanReviewConfirmationResult(
            preview_digest=plan.preview_digest,
            result=LanReviewResult(
                profile=plan.result_profile,
                target=plan.result_target,
                material_binding_digest=plan.reviewed_material_binding_digest,
                privacy_acknowledgement_digest=(
                    plan.privacy_acknowledgement_digest
                ),
            ),
        )

    def import_observation(
        self,
        request: LanImportRequest,
        *,
        authenticated_owner_principal: str,
    ) -> LanImportResult:
        if type(request) is not LanImportRequest:
            raise ValueError("LAN import request must be exactly typed")
        LanImportRequest.__post_init__(request)
        if request.replacement is not None:
            if type(request.replacement) is not LanReplacementConfirmation:
                raise ValueError("LAN replacement confirmation must be exactly typed")
            LanReplacementConfirmation.__post_init__(request.replacement)
        owner = _validate_canonical_owner(authenticated_owner_principal)
        now = _validate_utc_clock(self._clock())
        replacement = (
            None
            if request.replacement is None
            else (
                request.replacement.provider_profile_id,
                request.replacement.expected_profile_revision,
                request.replacement.expected_endpoint_fingerprint,
                request.replacement.expected_material_binding_digests,
            )
        )
        try:
            result = self.registry.apply_lan_import(
                scan_id=request.scan_id,
                endpoint_binding_digest=request.endpoint_binding_digest,
                expected_terminal_receipt_digest=(request.expected_terminal_receipt_digest),
                expected_observation_digest=request.expected_observation_digest,
                expected_profile_revision=request.expected_profile_revision,
                expected_target_revisions=tuple(
                    (item.resource_id, item.revision) for item in request.expected_target_revisions
                ),
                replacement=replacement,
                authenticated_owner_principal=owner,
                now=now,
                runtime_hardening_version=self._runtime_hardening_version,
            )
        except RoutingRevisionConflict as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        except ValueError as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        return LanImportResult(
            profile=result[0],
            targets=result[1],
            affected_target_ids=result[5],
            invalidated_binding_digests=result[6],
            stale_reasons_by_target=result[7],  # type: ignore[arg-type]
            observation_digest=result[2],
            endpoint_fingerprint=result[3],
            outage_observed=result[4],
        )

    def review_lan_target(
        self,
        request: LanReviewRequest,
        *,
        authenticated_owner_principal: str,
    ) -> LanReviewResult:
        if type(request) is not LanReviewRequest:
            raise ValueError("LAN review request must be exactly typed")
        try:
            LanReviewRequest.__post_init__(request)
        except ValueError as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        owner = _validate_canonical_owner(authenticated_owner_principal)
        now = _validate_utc_clock(self._clock())
        try:
            result = self.registry.review_lan_target(
                target_id=request.target_id,
                expected_profile_revision=request.expected_profile_revision,
                expected_target_revision=request.expected_target_revision,
                expected_terminal_receipt_digest=(request.expected_terminal_receipt_digest),
                expected_observation_digest=request.expected_observation_digest,
                expected_endpoint_fingerprint=request.expected_endpoint_fingerprint,
                expected_material_binding_digest=(request.expected_material_binding_digest),
                expected_review_digest=request.expected_review_digest,
                expected_stale_reasons=request.expected_stale_reasons,
                trust_class=request.trust_class,
                intended_roles=request.intended_roles,
                task_family_affinities=request.task_family_affinities,
                privacy_acknowledged=request.privacy_acknowledged,
                enabled=request.enabled,
                authenticated_owner_principal=owner,
                now=now,
                runtime_hardening_version=self._runtime_hardening_version,
                interface_inventory_resolver=self._interface_inventory_resolver,
            )
        except RoutingRevisionConflict as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        except ValueError as exc:
            raise LanDiscoveryConflict(str(exc)) from None
        return LanReviewResult(
            profile=result[0],
            target=result[1],
            material_binding_digest=result[3],
            privacy_acknowledgement_digest=result[2],
        )


def _validate_canonical_owner(value: object) -> str:
    if type(value) is not str:
        raise ValueError("authenticated LAN owner principal is not canonical")
    owner = validate_required_text(
        value,
        "authenticated_owner_principal",
        maximum=256,
    )
    if (
        owner != value
        or unicodedata.normalize("NFC", owner) != owner
        or any(unicodedata.category(character).startswith("C") for character in owner)
    ):
        raise ValueError("authenticated LAN owner principal is not canonical")
    return owner


def _lan_import_result_from_plan(plan: Any) -> LanImportResult:
    return LanImportResult(
        profile=plan.result_profile,
        targets=plan.result_targets,
        affected_target_ids=plan.affected_target_ids,
        invalidated_binding_digests=plan.invalidated_binding_digests,
        stale_reasons_by_target=plan.stale_reasons_by_target,
        observation_digest=plan.expected_observation_digest,
        endpoint_fingerprint=plan.endpoint_fingerprint,
        outage_observed=plan.outage_observed,
    )


def _validate_canonical_text(value: object, field: str, *, maximum: int) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be exact text")
    normalized = validate_required_text(value, field, maximum=maximum)
    if (
        normalized != value
        or unicodedata.normalize("NFC", value) != value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError(f"{field} must be canonical text")
    return value


def _validate_exact_digest(value: object, field: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{field} must be an exact lowercase sha256 digest")
    validated = validate_digest(value, field)
    if validated is None:
        raise ValueError(f"{field} must be a lowercase sha256 digest")
    return validated


def _validate_utc_clock(value: object) -> datetime:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() != UTC.utcoffset(value)
    ):
        raise ValueError("LAN discovery clock must return an aware UTC datetime")
    return value


def _validate_canonical_utc_timestamp(value: object, field: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError(f"LAN {field} must be canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise ValueError(f"LAN {field} is invalid") from None
    if parsed.astimezone(UTC).isoformat().replace("+00:00", "Z") != value:
        raise ValueError(f"LAN {field} must be canonical UTC")
    return value


def _validate_revision(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("LAN expected revision must be an exact non-negative integer")
    return value


def _validate_profile_id(value: object) -> str:
    if type(value) is not str or _LAN_PROFILE_ID_RE.fullmatch(value) is None:
        raise ValueError("LAN provider profile ID is invalid")
    return value


def _validate_target_id(value: object) -> str:
    if type(value) is not str or _LAN_TARGET_ID_RE.fullmatch(value) is None:
        raise ValueError("LAN target ID is invalid")
    return value


def _validate_stale_reasons(value: object) -> tuple[LanStaleReason, ...]:
    if type(value) is not tuple:
        raise ValueError("LAN stale reasons must be an exact tuple")
    if any(type(reason) is not str for reason in value):
        raise ValueError("LAN stale reasons must contain exact text")
    if len(value) != len(set(value)):
        raise ValueError("LAN stale reasons must be unique")
    try:
        ordered = tuple(reason for reason in LAN_STALE_REASON_ORDER if reason in value)
    except TypeError:
        raise ValueError("LAN stale reasons are invalid") from None
    if value != ordered:
        raise ValueError("LAN stale reasons must use the closed deterministic order")
    return ordered


def _validate_affinities(value: object, field: str) -> tuple[str, ...]:
    if type(value) is not tuple or len(value) > _MAX_AFFINITIES:
        raise ValueError(f"LAN {field} must be an exact tuple of at most 16 values")
    normalized: list[str] = []
    for item in value:
        if type(item) is not str or not item:
            raise ValueError(f"LAN {field} values must be non-empty text")
        if unicodedata.normalize("NFC", item) != item:
            raise ValueError(f"LAN {field} values must be NFC-normalized")
        if any(unicodedata.category(character).startswith("C") for character in item):
            raise ValueError(f"LAN {field} values must not contain control characters")
        if len(item.encode("utf-8")) > _MAX_AFFINITY_UTF8_BYTES:
            raise ValueError(f"LAN {field} values must be at most 64 UTF-8 bytes")
        normalized.append(item)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"LAN {field} values must be unique")
    if tuple(normalized) != tuple(sorted(normalized)):
        raise ValueError(f"LAN {field} values must use deterministic order")
    return tuple(normalized)
