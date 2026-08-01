from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ..config import AgentConfig
from ..lan_runtime_authority import (
    LAN_OPENAI_RUNTIME_HARDENING_VERSION,
    LanRuntimeAuthority,
    LanRuntimeAuthorityResolver,
    authenticate_lan_runtime_authority,
    derive_lan_runtime_authority_interface_binding_digest,
)
from .contracts import TaskLike, compile_task_contract
from .ledger_registry import (
    _has_reserved_lan_prefix,
    _lan_is_managed_metadata,
    _validate_lan_protected_metadata,
    _validate_managed_lan_target_model,
)
from .models import (
    AgentTaskContract,
    ModelTarget,
    PrivacyClass,
    ProviderProfile,
    RouteDecision,
    RoutePolicy,
    RoutingMode,
)
from .router import (
    ReviewDiversityContext,
    RoutingUnavailableError,
    managed_lan_target_guard_reasons,
    route_task,
)


@dataclass(frozen=True)
class RoutingAssignment:
    contract: AgentTaskContract
    decision: RouteDecision
    config: AgentConfig
    executes_selected_target: bool


class AdaptiveFlockRoutingService:
    """Compile, explain, and apply one governed route assignment.

    This service intentionally owns no lifecycle state. RunManager remains the
    authority for task claims, attempts, approvals, cancellation, and terminal
    transitions. The service is a deterministic decision boundary that can be
    inserted immediately before worker agent construction.
    """

    def __init__(
        self,
        *,
        profiles: tuple[ProviderProfile, ...] | list[ProviderProfile],
        targets: tuple[ModelTarget, ...] | list[ModelTarget],
        policy: RoutePolicy | None = None,
        mode: RoutingMode = "shadow",
        lan_runtime_authority_resolver: LanRuntimeAuthorityResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if lan_runtime_authority_resolver is not None and not callable(
            lan_runtime_authority_resolver
        ):
            raise TypeError("LAN runtime authority resolver must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("routing clock must be callable")
        profile_tuple = tuple(profiles)
        self.profiles = {profile.profile_id: profile for profile in profile_tuple}
        if len(self.profiles) != len(profile_tuple):
            raise ValueError("provider profile IDs must be unique")
        self.targets = tuple(targets)
        if len({target.target_id for target in self.targets}) != len(self.targets):
            raise ValueError("model target IDs must be unique")
        self.policy = policy or RoutePolicy()
        self.mode = mode
        self.lan_runtime_authority_resolver = lan_runtime_authority_resolver
        self.clock = clock or _utc_now
        self._validate_inventory()

    def preview(
        self,
        task: TaskLike,
        *,
        planner_guidance: dict[str, object] | None = None,
        default_privacy_class: PrivacyClass = "approved_cloud",
        local_required: bool = False,
        maximum_cost_usd: float | None = None,
        allowed_target_ids: Sequence[str] = (),
        forbidden_target_ids: Sequence[str] = (),
        allowed_provider_profiles: Sequence[str] = (),
        forbidden_provider_profiles: Sequence[str] = (),
        direct_target_id: str | None = None,
        review_context: ReviewDiversityContext | None = None,
    ) -> tuple[AgentTaskContract, RouteDecision]:
        contract = compile_task_contract(
            task,
            planner_guidance=planner_guidance,
            default_privacy_class=default_privacy_class,
            local_required=local_required,
            maximum_cost_usd=maximum_cost_usd,
            allowed_target_ids=allowed_target_ids,
            forbidden_target_ids=forbidden_target_ids,
            allowed_provider_profiles=allowed_provider_profiles,
            forbidden_provider_profiles=forbidden_provider_profiles,
        )
        decision = route_task(
            contract,
            list(self._eligible_inventory()),
            policy=self.policy,
            mode=self.mode,
            direct_target_id=direct_target_id,
            review_context=review_context,
            clock=self.clock,
        )
        return contract, decision

    def assign(
        self,
        base_config: AgentConfig,
        task: TaskLike,
        *,
        planner_guidance: dict[str, object] | None = None,
        default_privacy_class: PrivacyClass = "approved_cloud",
        local_required: bool = False,
        maximum_cost_usd: float | None = None,
        allowed_target_ids: Sequence[str] = (),
        forbidden_target_ids: Sequence[str] = (),
        allowed_provider_profiles: Sequence[str] = (),
        forbidden_provider_profiles: Sequence[str] = (),
        direct_target_id: str | None = None,
        review_context: ReviewDiversityContext | None = None,
    ) -> RoutingAssignment:
        contract, decision = self.preview(
            task,
            planner_guidance=planner_guidance,
            default_privacy_class=default_privacy_class,
            local_required=local_required,
            maximum_cost_usd=maximum_cost_usd,
            allowed_target_ids=allowed_target_ids,
            forbidden_target_ids=forbidden_target_ids,
            allowed_provider_profiles=allowed_provider_profiles,
            forbidden_provider_profiles=forbidden_provider_profiles,
            direct_target_id=direct_target_id,
            review_context=review_context,
        )
        if not decision.actionable:
            return RoutingAssignment(
                contract=contract,
                decision=decision,
                config=(
                    base_config
                    if base_config.lan_runtime_authority is None
                    else replace(base_config, lan_runtime_authority=None)
                ),
                executes_selected_target=False,
            )
        return RoutingAssignment(
            contract=contract,
            decision=decision,
            config=self.apply_decision(base_config, decision),
            executes_selected_target=True,
        )

    def apply_decision(self, base_config: AgentConfig, decision: RouteDecision) -> AgentConfig:
        target = decision.selected_target
        now = _read_utc_clock(self.clock)
        lan_reasons = managed_lan_target_guard_reasons(
            target,
            now=now,
        )
        if lan_reasons:
            raise RoutingUnavailableError(
                f"selected LAN target is not executable: {target.target_id}",
                reason_codes=lan_reasons,
            )
        profile = self.profiles.get(target.provider_profile_id)
        if profile is None:
            raise RoutingUnavailableError(
                f"selected target references an unknown provider profile: {target.target_id}",
                reason_codes=("provider_profile_unknown",),
            )
        managed = (
            _has_reserved_lan_prefix(target.target_id)
            or _has_reserved_lan_prefix(target.provider_profile_id)
            or _lan_is_managed_metadata(target.metadata)
            or _has_reserved_lan_prefix(profile.profile_id)
            or _lan_is_managed_metadata(profile.metadata)
        )
        if managed:
            return self._apply_managed_lan_decision(
                base_config,
                profile=profile,
                target=target,
                now=now,
            )
        if not profile.enabled:
            raise RoutingUnavailableError(
                f"selected target provider profile is disabled: {profile.profile_id}",
                reason_codes=("provider_profile_disabled",),
            )
        if target.provider != profile.adapter:
            raise RoutingUnavailableError(
                f"selected target provider does not match its profile: {target.target_id}",
                reason_codes=("provider_profile_adapter_mismatch",),
            )
        return replace(
            base_config,
            provider=profile.adapter,
            model=target.model,
            base_url=profile.base_url,
            api_key_env=profile.secret_ref,
            fallback_provider=None,
            fallback_model=None,
            fallback_base_url=None,
            fallback_api_key_env=None,
            lan_runtime_authority=None,
        )

    def _apply_managed_lan_decision(
        self,
        base_config: AgentConfig,
        *,
        profile: ProviderProfile,
        target: ModelTarget,
        now: datetime,
    ) -> AgentConfig:
        try:
            target_protected = _validate_managed_lan_target_model(target)
            profile_protected = _validate_managed_lan_profile(
                profile,
                target=target,
                target_protected=target_protected,
            )
        except (KeyError, TypeError, ValueError):
            raise RoutingUnavailableError(
                "selected managed LAN inventory is invalid",
                reason_codes=("lan_binding_invalid",),
            ) from None
        resolver = self.lan_runtime_authority_resolver
        if resolver is None:
            raise RoutingUnavailableError(
                "selected managed LAN target has no runtime authority resolver",
                reason_codes=("lan_authority_resolver_missing",),
            )
        try:
            authority = authenticate_lan_runtime_authority(resolver(target.target_id))
        except Exception:
            raise RoutingUnavailableError(
                "selected managed LAN authority is unavailable",
                reason_codes=("lan_authority_invalid",),
            ) from None
        try:
            authority_matches = _authority_matches_inventory(
                authority,
                profile=profile,
                target=target,
                target_protected=target_protected,
                profile_protected=profile_protected,
                now=now,
            )
        except (AttributeError, TypeError, ValueError):
            authority_matches = False
        if not authority_matches:
            raise RoutingUnavailableError(
                "selected managed LAN authority does not match routing inventory",
                reason_codes=("lan_authority_mismatch",),
            )
        return replace(
            base_config,
            provider="lan-openai-compatible",
            model=authority.model_id,
            base_url=_lan_base_url(authority),
            api_key_env=None,
            fallback_provider=None,
            fallback_model=None,
            fallback_base_url=None,
            fallback_api_key_env=None,
            stream=False,
            lan_runtime_authority=authority,
        )

    def _eligible_inventory(self) -> tuple[ModelTarget, ...]:
        return tuple(
            target
            for target in self.targets
            if (profile := self.profiles.get(target.provider_profile_id)) is not None
            and profile.enabled
        )

    def _validate_inventory(self) -> None:
        for target in self.targets:
            profile = self.profiles.get(target.provider_profile_id)
            if profile is None:
                raise ValueError(
                    f"model target {target.target_id} references unknown profile "
                    f"{target.provider_profile_id}"
                )
            if target.provider != profile.adapter:
                raise ValueError(
                    f"model target {target.target_id} provider {target.provider} does not "
                    f"match profile adapter {profile.adapter}"
                )
            if profile.locality != "hybrid" and target.locality != profile.locality:
                raise ValueError(
                    f"model target {target.target_id} locality {target.locality} does not "
                    f"match profile locality {profile.locality}"
                )


def _validate_managed_lan_profile(
    profile: ProviderProfile,
    *,
    target: ModelTarget,
    target_protected: dict[str, object],
) -> dict[str, object]:
    if type(profile.metadata) is not dict or set(profile.metadata) != {"lan_discovery"}:
        raise ValueError("LAN managed provider metadata is invalid")
    protected = _validate_lan_protected_metadata(
        profile.metadata["lan_discovery"],
        target=False,
    )
    address = target_protected["address"]
    port = target_protected["port"]
    if type(address) is not str or type(port) is not int:
        raise ValueError("LAN managed endpoint is invalid")
    host = f"[{address}]" if ":" in address else address
    expected_base_url = f"http://{host}:{port}/v1"
    if (
        profile.profile_id != target.provider_profile_id
        or profile.display_name != f"LAN {address}:{port}"
        or profile.adapter != "lan-openai-compatible"
        or profile.base_url != expected_base_url
        or profile.secret_ref is not None
        or profile.enabled is not True
        or profile.locality != "local"
        or profile.trust_class != "operator_confirmed"
        or profile.max_concurrency != 1
        or target.enabled is not True
        or target.provider != profile.adapter
        or target_protected["runtime_hardening"]
        != LAN_OPENAI_RUNTIME_HARDENING_VERSION
        or protected["runtime_hardening"]
        != LAN_OPENAI_RUNTIME_HARDENING_VERSION
    ):
        raise ValueError("LAN managed provider projection is invalid")
    shared_binding_fields = (
        "owner_principal",
        "endpoint_binding_digest",
        "endpoint_fingerprint",
        "interface_id",
        "confirmed_network",
        "address",
        "port",
        "transport_security",
        "certificate_sha256",
        "api_shape",
        "runtime_adapter",
        "runtime_hardening",
    )
    for field in shared_binding_fields:
        if target_protected.get(field) != protected[field]:
            raise ValueError("LAN managed provider and target evidence disagree")
    return protected


def _authority_matches_inventory(
    authority: LanRuntimeAuthority,
    *,
    profile: ProviderProfile,
    target: ModelTarget,
    target_protected: dict[str, object],
    profile_protected: dict[str, object],
    now: datetime,
) -> bool:
    endpoint = authority.endpoint
    return (
        authority.provider_profile_id == profile.profile_id
        and authority.reviewed_target_id == target.target_id
        and authority.model_id == target.model
        and authority.api_shape == target_protected["api_shape"]
        and authority.runtime_adapter == profile.adapter
        and authority.runtime_hardening_version
        == target_protected["runtime_hardening"]
        and authority.endpoint_binding_digest
        == target_protected["endpoint_binding_digest"]
        and authority.endpoint_fingerprint == target_protected["endpoint_fingerprint"]
        and authority.reviewed_material_binding_digest
        == target_protected["reviewed_material_binding_digest"]
        and authority.review_digest == target_protected["review_digest"]
        and derive_lan_runtime_authority_interface_binding_digest(authority)
        == target_protected["reviewed_runtime_interface_binding_digest"]
        and authority.fresh_until == target_protected["fresh_until"]
        and authority.fresh_until_datetime > now
        and endpoint.address == target_protected["address"]
        and endpoint.port == target_protected["port"]
        and endpoint.interface_id == target_protected["interface_id"]
        and authority.scope.interface.interface_id == target_protected["interface_id"]
        and authority.scope.network == target_protected["confirmed_network"]
        and profile_protected["endpoint_binding_digest"]
        == authority.endpoint_binding_digest
        and profile.base_url == _lan_base_url(authority)
    )


def _lan_base_url(authority: LanRuntimeAuthority) -> str:
    address = authority.endpoint.address
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}:{authority.endpoint.port}/v1"


def _read_utc_clock(clock: Callable[[], datetime]) -> datetime:
    try:
        now = clock()
    except Exception:
        raise RoutingUnavailableError(
            "routing clock is unavailable",
            reason_codes=("lan_clock_invalid",),
        ) from None
    if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
        raise RoutingUnavailableError(
            "routing clock is invalid",
            reason_codes=("lan_clock_invalid",),
        )
    return now.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
