from __future__ import annotations

from dataclasses import dataclass, replace

from ..config import AgentConfig
from .contracts import TaskLike, compile_task_contract
from .models import (
    AgentTaskContract,
    ModelTarget,
    PrivacyClass,
    ProviderProfile,
    RouteDecision,
    RoutePolicy,
    RoutingMode,
)
from .router import ReviewDiversityContext, RoutingUnavailableError, route_task


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
    ) -> None:
        profile_tuple = tuple(profiles)
        self.profiles = {profile.profile_id: profile for profile in profile_tuple}
        if len(self.profiles) != len(profile_tuple):
            raise ValueError("provider profile IDs must be unique")
        self.targets = tuple(targets)
        if len({target.target_id for target in self.targets}) != len(self.targets):
            raise ValueError("model target IDs must be unique")
        self.policy = policy or RoutePolicy()
        self.mode = mode
        self._validate_inventory()

    def preview(
        self,
        task: TaskLike,
        *,
        planner_guidance: dict[str, object] | None = None,
        default_privacy_class: PrivacyClass = "approved_cloud",
        local_required: bool = False,
        maximum_cost_usd: float | None = None,
        direct_target_id: str | None = None,
        review_context: ReviewDiversityContext | None = None,
    ) -> tuple[AgentTaskContract, RouteDecision]:
        contract = compile_task_contract(
            task,
            planner_guidance=planner_guidance,
            default_privacy_class=default_privacy_class,
            local_required=local_required,
            maximum_cost_usd=maximum_cost_usd,
        )
        decision = route_task(
            contract,
            list(self._eligible_inventory()),
            policy=self.policy,
            mode=self.mode,
            direct_target_id=direct_target_id,
            review_context=review_context,
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
        direct_target_id: str | None = None,
        review_context: ReviewDiversityContext | None = None,
    ) -> RoutingAssignment:
        contract, decision = self.preview(
            task,
            planner_guidance=planner_guidance,
            default_privacy_class=default_privacy_class,
            local_required=local_required,
            maximum_cost_usd=maximum_cost_usd,
            direct_target_id=direct_target_id,
            review_context=review_context,
        )
        if not decision.actionable:
            return RoutingAssignment(
                contract=contract,
                decision=decision,
                config=base_config,
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
        profile = self.profiles.get(target.provider_profile_id)
        if profile is None:
            raise RoutingUnavailableError(
                f"selected target references an unknown provider profile: {target.target_id}",
                reason_codes=("provider_profile_unknown",),
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
