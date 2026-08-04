"""Read-only Flock qualification eligibility preview (plan Task 6).

The preview consumes the project authority, the selected task families, the
routing policy, the hybrid corpus, and the current provider profiles/targets.
It produces an exact, digest-bound snapshot of every scope, every eligible
target per scope, exclusions with reason codes, price/freshness warnings,
the attempt-matrix size, and the estimated reserved-cost range.

Eligibility uses the exact same hard filters as ordinary routing
(:func:`nested_memvid_agent.routing.router.evaluate_target_eligibility`);
there is no parallel approximation. ``revalidate_for_start`` fails closed on
any inventory, eligibility, or price drift observed after the preview.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .models import AgentTaskContract, ModelTarget, ProviderProfile, RoutePolicy
from .qualification_digest import canonical_digest
from .qualification_models import (
    CorpusManifest,
    MoneyMicros,
    PriceSnapshot,
    QualificationScope,
    QualificationThresholds,
)
from .qualification_snapshot import (
    PRICE_UNKNOWN_BLOCKER,
    TargetInventorySnapshot,
    build_target_snapshot,
    derive_price_snapshot,
    price_freshness_warning,
)
from .router import EligibilityEvaluation, evaluate_target_eligibility

__all__ = [
    "INSUFFICIENT_ELIGIBLE_TARGETS",
    "QualificationPreview",
    "QualificationPreviewDraft",
    "QualificationPreviewService",
    "TargetInventory",
]

#: Exclusion reason when a comparative scope has fewer than two eligible targets.
INSUFFICIENT_ELIGIBLE_TARGETS = "insufficient_eligible_targets"

_PRIVACY_CLASSES: tuple[str, ...] = (
    "local_required",
    "local_preferred",
    "approved_cloud",
    "any",
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class TargetInventory:
    """The current provider profiles and model targets under evaluation."""

    profiles: tuple[ProviderProfile, ...]
    targets: tuple[ModelTarget, ...]

    def __post_init__(self) -> None:
        if len({profile.profile_id for profile in self.profiles}) != len(self.profiles):
            raise ValueError("provider profile IDs must be unique")
        if len({target.target_id for target in self.targets}) != len(self.targets):
            raise ValueError("model target IDs must be unique")

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "profiles": [
                    profile.to_public_payload()
                    for profile in sorted(self.profiles, key=lambda item: item.profile_id)
                ],
                "targets": [
                    target.to_public_payload()
                    for target in sorted(self.targets, key=lambda item: item.target_id)
                ],
            }
        )


@dataclass(frozen=True)
class QualificationPreviewDraft:
    """Owner intent for one qualification preview: scopes, policy, corpus, prices."""

    project_authority: Mapping[str, Any]
    task_families: tuple[str, ...]
    policy: RoutePolicy
    policy_revision: int
    corpus: CorpusManifest
    prices: Mapping[str, PriceSnapshot] = field(default_factory=dict)
    thresholds: QualificationThresholds = field(default_factory=QualificationThresholds)
    learned_config: Mapping[str, Any] = field(default_factory=dict)
    default_privacy_class: str = "approved_cloud"

    def __post_init__(self) -> None:
        authority = dict(self.project_authority)
        project_id = authority.get("project_id")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project authority requires a project_id")
        object.__setattr__(self, "project_authority", authority)
        families = tuple(dict.fromkeys(self.task_families))
        if not families or any(not family.strip() for family in families):
            raise ValueError("at least one non-empty task family is required")
        object.__setattr__(self, "task_families", families)
        if not isinstance(self.policy, RoutePolicy):
            raise ValueError("policy must be a RoutePolicy")
        if isinstance(self.policy_revision, bool) or self.policy_revision < 1:
            raise ValueError("policy_revision must be a positive integer")
        if not isinstance(self.corpus, CorpusManifest):
            raise ValueError("corpus must be a CorpusManifest")
        prices = dict(self.prices)
        for target_id, price in prices.items():
            if not isinstance(price, PriceSnapshot):
                raise ValueError("prices must be PriceSnapshot values")
            if price.target_id != target_id:
                raise ValueError("price snapshots must be keyed by their target_id")
        object.__setattr__(self, "prices", prices)
        if not isinstance(self.thresholds, QualificationThresholds):
            raise ValueError("thresholds must be QualificationThresholds")
        object.__setattr__(self, "learned_config", dict(self.learned_config))
        if self.default_privacy_class not in _PRIVACY_CLASSES:
            raise ValueError(f"default privacy class must be one of {', '.join(_PRIVACY_CLASSES)}")


@dataclass(frozen=True)
class QualificationPreview:
    """Immutable, digest-bound eligibility snapshot for the selected scopes."""

    draft: QualificationPreviewDraft
    created_at: str
    scopes: tuple[QualificationScope, ...]
    excluded_scopes: Mapping[str, tuple[str, ...]]
    target_snapshot: TargetInventorySnapshot
    excluded_targets: Mapping[str, tuple[str, ...]]
    start_blockers: Mapping[str, tuple[str, ...]]
    warnings: Mapping[str, tuple[str, ...]]
    matrix_size: int
    estimated_reserved_cost_range: tuple[MoneyMicros, MoneyMicros]
    policy_digest: str
    corpus_digest: str
    project_authority_digest: str
    target_inventory_digest: str
    learned_config_digest: str

    def __post_init__(self) -> None:
        if isinstance(self.matrix_size, bool) or self.matrix_size < 0:
            raise ValueError("matrix_size must be a non-negative integer")
        low, high = self.estimated_reserved_cost_range
        if low > high:
            raise ValueError("reserved-cost range must be ordered")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": "kestrel.flock.qualification_preview.v1",
            "created_at": self.created_at,
            "scopes": [scope.to_payload() for scope in self.scopes],
            "excluded_scopes": {
                key: list(reasons) for key, reasons in sorted(self.excluded_scopes.items())
            },
            "target_snapshot_digest": self.target_snapshot.digest,
            "target_ids": list(self.target_snapshot.target_ids),
            "excluded_targets": {
                key: list(reasons) for key, reasons in sorted(self.excluded_targets.items())
            },
            "start_blockers": {
                key: list(reasons) for key, reasons in sorted(self.start_blockers.items())
            },
            "warnings": {key: list(ids) for key, ids in sorted(self.warnings.items())},
            "matrix_size": self.matrix_size,
            "estimated_reserved_cost_range": [
                self.estimated_reserved_cost_range[0].micros,
                self.estimated_reserved_cost_range[1].micros,
            ],
            "policy_digest": self.policy_digest,
            "corpus_digest": self.corpus_digest,
            "project_authority_digest": self.project_authority_digest,
            "target_inventory_digest": self.target_inventory_digest,
            "learned_config_digest": self.learned_config_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())


class QualificationPreviewService:
    """Build and revalidate read-only qualification previews.

    The service owns no mutable state: the current inventory is pulled from
    the supplied provider on every call, so ``revalidate_for_start`` always
    compares the preview against live inventory.
    """

    def __init__(
        self,
        inventory: Callable[[], TargetInventory],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(inventory):
            raise TypeError("inventory provider must be callable")
        if clock is not None and not callable(clock):
            raise TypeError("preview clock must be callable")
        self._inventory = inventory
        self._clock = clock or _utc_now

    def preview(self, draft: QualificationPreviewDraft) -> QualificationPreview:
        """Snapshot every eligible target, price, policy, and authority."""

        if not isinstance(draft, QualificationPreviewDraft):
            raise ValueError("draft must be a QualificationPreviewDraft")
        now = self._now()
        inventory = self._inventory()
        authority = dict(draft.project_authority)
        project_id = str(authority["project_id"])
        privacy_class = str(authority.get("privacy_class") or draft.default_privacy_class)
        if privacy_class not in _PRIVACY_CLASSES:
            raise ValueError(f"project privacy class must be one of {', '.join(_PRIVACY_CLASSES)}")
        profiles = {profile.profile_id: profile for profile in inventory.profiles}

        scope_groups: dict[tuple[str, str, tuple[str, ...]], int] = {}
        for item in draft.corpus.items:
            if not item.actionable or item.task_family not in draft.task_families:
                continue
            key = (item.task_family, item.risk, item.capabilities)
            scope_groups[key] = scope_groups.get(key, 0) + 1

        scopes: list[QualificationScope] = []
        excluded_scopes: dict[str, tuple[str, ...]] = {}
        evaluations_by_target: dict[str, list[EligibilityEvaluation]] = {}
        eligible_any: dict[str, tuple[ModelTarget, ProviderProfile]] = {}
        learned_config_digest = canonical_digest(dict(draft.learned_config))
        project_authority_digest = canonical_digest(authority)

        for task_family, risk, capabilities in sorted(scope_groups):
            scope_key = f"{task_family}:{risk}:{'+'.join(capabilities)}"
            contract = self._scope_contract(
                project_id=project_id,
                scope_key=scope_key,
                task_family=task_family,
                risk=risk,
                capabilities=capabilities,
                privacy_class=privacy_class,
            )
            evaluations = tuple(
                self._evaluate(contract, target, profiles, policy=draft.policy, now=now)
                for target in sorted(inventory.targets, key=lambda item: item.target_id)
            )
            for evaluation in evaluations:
                evaluations_by_target.setdefault(evaluation.target.target_id, []).append(evaluation)
                if evaluation.eligible:
                    eligible_any[evaluation.target.target_id] = (
                        evaluation.target,
                        profiles[evaluation.target.provider_profile_id],
                    )
            eligible_ids = tuple(
                sorted(
                    evaluation.target.target_id for evaluation in evaluations if evaluation.eligible
                )
            )
            if len(eligible_ids) < 2:
                excluded_scopes[scope_key] = (INSUFFICIENT_ELIGIBLE_TARGETS,)
                continue
            scopes.append(
                QualificationScope(
                    project_id=project_id,
                    task_family=task_family,
                    risk=risk,  # type: ignore[arg-type]
                    capabilities=capabilities,
                    policy_id=draft.policy.policy_id,
                    policy_revision=draft.policy_revision,
                    target_ids=eligible_ids,
                    target_inventory_digest=inventory.digest,
                    price_digest=canonical_digest(
                        {
                            "prices": [
                                self._price_for(target, draft, now=now).digest
                                for target, _profile in (
                                    eligible_any[target_id] for target_id in eligible_ids
                                )
                            ]
                        }
                    ),
                    learned_config_digest=learned_config_digest,
                    project_authority_digest=project_authority_digest,
                )
            )

        excluded_targets: dict[str, tuple[str, ...]] = {}
        for target_id in sorted(evaluations_by_target):
            if target_id in eligible_any:
                continue
            reasons = tuple(
                sorted(
                    {
                        code
                        for evaluation in evaluations_by_target[target_id]
                        for code in evaluation.reason_codes
                    }
                )
            )
            excluded_targets[target_id] = reasons

        snapshots = []
        start_blockers: dict[str, tuple[str, ...]] = {}
        warnings: dict[str, list[str]] = {}
        reserve_per_million: list[int] = []
        for target_id in sorted(eligible_any):
            target, profile = eligible_any[target_id]
            price = self._price_for(target, draft, now=now)
            if price.source == "unknown":
                start_blockers[target_id] = (PRICE_UNKNOWN_BLOCKER,)
            else:
                stale = price_freshness_warning(
                    price,
                    now=now,
                    max_age_days=draft.thresholds.decay_half_life_days,
                )
                if stale is not None:
                    warnings.setdefault(stale, []).append(target_id)
                reserve_per_million.append(
                    (price.input_per_million or MoneyMicros(0)).micros
                    + (price.output_per_million or MoneyMicros(0)).micros
                )
            snapshots.append(
                build_target_snapshot(target, profile, price, privacy_class=privacy_class)
            )

        target_snapshot = TargetInventorySnapshot(
            targets=tuple(snapshots),
            target_inventory_digest=inventory.digest,
        )
        if reserve_per_million:
            reserved_range = (
                MoneyMicros(min(reserve_per_million)),
                MoneyMicros(max(reserve_per_million)),
            )
        else:
            reserved_range = (MoneyMicros(0), MoneyMicros(0))

        return QualificationPreview(
            draft=draft,
            created_at=now.isoformat(),
            scopes=tuple(scopes),
            excluded_scopes=excluded_scopes,
            target_snapshot=target_snapshot,
            excluded_targets=excluded_targets,
            start_blockers=start_blockers,
            warnings={key: tuple(sorted(ids)) for key, ids in sorted(warnings.items())},
            matrix_size=sum(len(scope.target_ids) for scope in scopes),
            estimated_reserved_cost_range=reserved_range,
            policy_digest=canonical_digest(draft.policy),
            corpus_digest=draft.corpus.digest,
            project_authority_digest=project_authority_digest,
            target_inventory_digest=inventory.digest,
            learned_config_digest=learned_config_digest,
        )

    def revalidate_for_start(self, preview: QualificationPreview) -> QualificationPreview:
        """Fail closed when inventory, eligibility, or prices drifted.

        Returns the fresh preview when nothing changed. Unknown billed prices
        are start blockers for their targets and reject the start here.
        """

        if not isinstance(preview, QualificationPreview):
            raise ValueError("preview must be a QualificationPreview")
        fresh = self.preview(preview.draft)
        if fresh.target_inventory_digest != preview.target_inventory_digest:
            raise ValueError("target_inventory_changed: inventory drifted after preview")
        if [scope.target_ids for scope in fresh.scopes] != [
            scope.target_ids for scope in preview.scopes
        ] or dict(fresh.excluded_scopes) != dict(preview.excluded_scopes):
            raise ValueError("target_eligibility_changed: scope eligibility drifted")
        if dict(fresh.excluded_targets) != dict(preview.excluded_targets):
            raise ValueError("target_eligibility_changed: target exclusions drifted")
        if self._price_values(fresh) != self._price_values(preview):
            raise ValueError("price_snapshot_changed: target prices drifted after preview")
        if fresh.start_blockers:
            details = ";".join(
                f"{target_id}:{','.join(codes)}"
                for target_id, codes in sorted(fresh.start_blockers.items())
            )
            raise ValueError(f"start_blocked: {details}")
        return fresh

    @staticmethod
    def _price_values(preview: QualificationPreview) -> dict[str, tuple[str, int, int]]:
        values: dict[str, tuple[str, int, int]] = {}
        for snapshot in preview.target_snapshot.targets:
            price = snapshot.price
            values[snapshot.target_id] = (
                price.source,
                (price.input_per_million or MoneyMicros(0)).micros,
                (price.output_per_million or MoneyMicros(0)).micros,
            )
        return values

    def _now(self) -> datetime:
        now = self._clock()
        if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("preview clock must return an aware datetime")
        return now.astimezone(UTC)

    @staticmethod
    def _price_for(
        target: ModelTarget,
        draft: QualificationPreviewDraft,
        *,
        now: datetime,
    ) -> PriceSnapshot:
        explicit = draft.prices.get(target.target_id)
        if explicit is not None:
            return explicit
        return derive_price_snapshot(target, now=now)

    @staticmethod
    def _evaluate(
        contract: AgentTaskContract,
        target: ModelTarget,
        profiles: Mapping[str, ProviderProfile],
        *,
        policy: RoutePolicy,
        now: datetime,
    ) -> EligibilityEvaluation:
        profile = profiles.get(target.provider_profile_id)
        if profile is None:
            return EligibilityEvaluation(
                target=target,
                eligible=False,
                reason_codes=("provider_profile_unknown",),
            )
        if not profile.enabled:
            return EligibilityEvaluation(
                target=target,
                eligible=False,
                reason_codes=("provider_profile_disabled",),
            )
        return evaluate_target_eligibility(contract, target, policy, now=now)

    @staticmethod
    def _scope_contract(
        *,
        project_id: str,
        scope_key: str,
        task_family: str,
        risk: str,
        capabilities: tuple[str, ...],
        privacy_class: str,
    ) -> AgentTaskContract:
        return AgentTaskContract(
            task_id=f"qualification-preview:{scope_key}",
            run_id=f"qualification-preview:{project_id}",
            role="worker",
            task_family=task_family,
            objective="Flock qualification eligibility preview",
            complexity=0.5,
            ambiguity=0.0,
            risk=risk,
            required_capabilities=capabilities,
            privacy_class=privacy_class,  # type: ignore[arg-type]
            local_required=privacy_class == "local_required",
        )
