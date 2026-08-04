"""Canonical Flock qualification value models (Adaptive Flock plan, Phase 1).

These immutable, validated dataclasses define exact qualification scope,
money, price, target, and corpus values. Money is stored as integer
micro-USD; binary floating-point is never used for prices or spend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from .qualification_digest import canonical_digest

__all__ = [
    "CorpusItem",
    "CorpusManifest",
    "EvidenceKind",
    "MoneyMicros",
    "PriceSnapshot",
    "PriceSource",
    "QualificationScope",
    "QualificationThresholds",
    "RiskLevel",
    "TargetSnapshot",
]

PriceSource = Literal[
    "provider_published",
    "operator_verified",
    "operator_confirmed_non_billed_local",
    "unknown",
]

RiskLevel = Literal["low", "medium", "high", "critical"]

EvidenceKind = Literal["synthetic", "real_project"]

_RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "critical")
_BILLED_SOURCES: tuple[str, ...] = ("provider_published", "operator_verified")
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")
_MICROS_PER_USD = 1_000_000


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _require_digest(value: str, name: str) -> None:
    if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase sha256 hex digest")


def _sorted_unique(values: tuple[str, ...], name: str) -> tuple[str, ...]:
    for value in values:
        _require_text(value, f"{name} entry")
    return tuple(sorted(set(values)))


@dataclass(frozen=True, order=True)
class MoneyMicros:
    """Exact non-negative amount of money in integer micro-USD."""

    micros: int

    def __post_init__(self) -> None:
        if isinstance(self.micros, bool) or not isinstance(self.micros, int) or self.micros < 0:
            raise ValueError("money must be a non-negative integer micro-USD value")

    @classmethod
    def from_usd_text(cls, text: str) -> MoneyMicros:
        """Parse a USD decimal string (e.g. ``"50.00"``) without floats."""
        if not isinstance(text, str) or not text.strip():
            raise ValueError("invalid USD amount")
        stripped = text.strip()
        whole, dot, fraction = stripped.partition(".")
        if not whole or not whole.isdigit():
            raise ValueError("invalid USD amount")
        if dot:
            if not fraction.isdigit():
                raise ValueError("invalid USD amount")
            if len(fraction) > 6:
                raise ValueError("USD amounts support at most six decimal places")
        micros = int(whole) * _MICROS_PER_USD
        if fraction:
            micros += int(fraction.ljust(6, "0"))
        return cls(micros)

    def to_usd_text(self) -> str:
        whole, fraction = divmod(self.micros, _MICROS_PER_USD)
        return f"{whole}.{fraction:06d}".rstrip("0").rstrip(".")


@dataclass(frozen=True)
class QualificationThresholds:
    """Owner-tunable qualification gates with the required defaults."""

    min_examples_per_scope: int = 5
    min_examples_per_target: int = 3
    confidence_threshold: float = 0.70
    utility_margin: float = 0.08
    cost_coverage_threshold: float = 0.80
    decay_half_life_days: int = 30
    max_guardrail_violations: int = 0
    replay_runs: int = 20
    replay_successes_required: int = 20

    def __post_init__(self) -> None:
        for name in ("min_examples_per_scope", "min_examples_per_target", "decay_half_life_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_guardrail_violations < 0:
            raise ValueError("max_guardrail_violations must be non-negative")
        if self.replay_runs < 1 or self.replay_successes_required > self.replay_runs:
            raise ValueError("replay_successes_required must be between 0 and replay_runs")
        for name in ("confidence_threshold", "cost_coverage_threshold"):
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.utility_margin < 0.0:
            raise ValueError("utility_margin must be non-negative")

    @property
    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True)
class PriceSnapshot:
    """Immutable price record for one target.

    An explicit non-billed local price is known zero and carries owner/time
    provenance. ``unknown`` is never converted to zero.
    """

    target_id: str
    source: PriceSource
    captured_at: str
    input_per_million: MoneyMicros | None = None
    output_per_million: MoneyMicros | None = None
    currency: str = "USD"
    confirmed_by: str | None = None
    confirmed_at: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.target_id, "target_id")
        _require_text(self.captured_at, "captured_at")
        _require_text(self.currency, "currency")
        if self.source == "unknown":
            if self.input_per_million is not None or self.output_per_million is not None:
                raise ValueError("unknown price source cannot carry a price; unknown is not zero")
        elif self.source in _BILLED_SOURCES:
            if self.input_per_million is None or self.output_per_million is None:
                raise ValueError("billed price source requires input and output prices")
        elif self.source == "operator_confirmed_non_billed_local":
            zero = MoneyMicros(0)
            if self.input_per_million != zero or self.output_per_million != zero:
                raise ValueError("non-billed local price must be zero")
            if not (self.confirmed_by and self.confirmed_by.strip()) or not (
                self.confirmed_at and self.confirmed_at.strip()
            ):
                raise ValueError("non-billed local price requires owner/time provenance")
        else:
            raise ValueError(f"unsupported price source: {self.source}")

    @property
    def is_known_zero(self) -> bool:
        return self.source == "operator_confirmed_non_billed_local"

    @property
    def is_trustworthy(self) -> bool:
        return self.source != "unknown"

    @property
    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True)
class TargetSnapshot:
    """Immutable snapshot of one eligible target at qualification time."""

    target_id: str
    provider_profile_id: str
    adapter: str
    model: str
    endpoint: str | None
    trust_class: str
    locality: str
    enabled: bool
    health: str
    capabilities: tuple[str, ...]
    privacy_class: str
    price: PriceSnapshot
    config_digest: str
    network_constraints: tuple[str, ...] = ()
    quality_tier: int = 1
    max_context_tokens: int | None = None

    def __post_init__(self) -> None:
        _require_text(self.target_id, "target_id")
        _require_text(self.provider_profile_id, "provider_profile_id")
        _require_text(self.adapter, "adapter")
        _require_text(self.model, "model")
        _require_text(self.trust_class, "trust_class")
        _require_text(self.locality, "locality")
        _require_text(self.health, "health")
        _require_text(self.privacy_class, "privacy_class")
        _require_digest(self.config_digest, "config_digest")
        if not isinstance(self.price, PriceSnapshot):
            raise ValueError("price must be a PriceSnapshot")
        if self.price.target_id != self.target_id:
            raise ValueError("price snapshot must belong to the snapshotted target")
        if not 1 <= self.quality_tier <= 5:
            raise ValueError("quality_tier must be between 1 and 5")
        if self.max_context_tokens is not None and self.max_context_tokens < 1:
            raise ValueError("max_context_tokens must be positive")
        object.__setattr__(self, "capabilities", _sorted_unique(self.capabilities, "capability"))
        object.__setattr__(
            self,
            "network_constraints",
            _sorted_unique(self.network_constraints, "network constraint"),
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True)
class CorpusItem:
    """Immutable hybrid-corpus entry binding contract and acceptance evidence."""

    item_id: str
    task_family: str
    risk: RiskLevel
    capabilities: tuple[str, ...]
    task_contract_digest: str
    acceptance_plan_digest: str
    evidence_kind: EvidenceKind
    actionable: bool = True
    exclusion_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.item_id, "item_id")
        _require_text(self.task_family, "task_family")
        if self.risk not in _RISK_LEVELS:
            raise ValueError(f"risk must be one of {', '.join(_RISK_LEVELS)}")
        _require_digest(self.task_contract_digest, "task_contract_digest")
        _require_digest(self.acceptance_plan_digest, "acceptance_plan_digest")
        if self.evidence_kind not in ("synthetic", "real_project"):
            raise ValueError("evidence_kind must be 'synthetic' or 'real_project'")
        object.__setattr__(self, "capabilities", _sorted_unique(self.capabilities, "capability"))
        object.__setattr__(
            self,
            "exclusion_reasons",
            _sorted_unique(self.exclusion_reasons, "exclusion reason"),
        )
        if self.actionable and self.exclusion_reasons:
            raise ValueError("actionable corpus items cannot carry exclusion reasons")

    @property
    def digest(self) -> str:
        return canonical_digest(self)


@dataclass(frozen=True)
class CorpusManifest:
    """Immutable, digest-bound collection of corpus items."""

    schema_version: int
    items: tuple[CorpusItem, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if isinstance(self.schema_version, bool) or self.schema_version < 1:
            raise ValueError("schema_version must be a positive integer")
        ordered = tuple(sorted(self.items, key=lambda item: item.item_id))
        item_ids = [item.item_id for item in ordered]
        if len(set(item_ids)) != len(item_ids):
            raise ValueError("corpus item IDs must be unique")
        object.__setattr__(self, "items", ordered)

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "items": [item.digest for item in self.items],
            }
        )


@dataclass(frozen=True)
class QualificationScope:
    """Exact, replayable qualification scope for one project/task family.

    Capabilities and eligible target IDs are semantic sets: equivalent
    unordered inputs canonicalize to identical values and digests, while any
    cross-project, risk, capability, policy, inventory, price, learned-config,
    or project-authority change produces a different digest.
    """

    project_id: str
    task_family: str
    risk: RiskLevel
    capabilities: tuple[str, ...]
    policy_id: str
    policy_revision: int
    target_ids: tuple[str, ...]
    target_inventory_digest: str
    price_digest: str
    learned_config_digest: str
    project_authority_digest: str

    def __post_init__(self) -> None:
        _require_text(self.project_id, "project_id")
        _require_text(self.task_family, "task_family")
        if self.risk not in _RISK_LEVELS:
            raise ValueError(f"risk must be one of {', '.join(_RISK_LEVELS)}")
        _require_text(self.policy_id, "policy_id")
        if isinstance(self.policy_revision, bool) or self.policy_revision < 1:
            raise ValueError("policy_revision must be a positive integer")
        _require_digest(self.target_inventory_digest, "target_inventory_digest")
        _require_digest(self.price_digest, "price_digest")
        _require_digest(self.learned_config_digest, "learned_config_digest")
        _require_digest(self.project_authority_digest, "project_authority_digest")
        capabilities = _sorted_unique(self.capabilities, "capability")
        if not capabilities:
            raise ValueError("scope requires at least one capability")
        target_ids = _sorted_unique(self.target_ids, "target ID")
        if len(target_ids) < 2:
            raise ValueError("scope requires at least two eligible targets")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "target_ids", target_ids)

    @property
    def capability_key(self) -> str:
        return "+".join(self.capabilities)

    def to_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "task_family": self.task_family,
            "risk": self.risk,
            "capability_key": self.capability_key,
            "policy_id": self.policy_id,
            "policy_revision": self.policy_revision,
            "target_ids": list(self.target_ids),
            "target_inventory_digest": self.target_inventory_digest,
            "price_digest": self.price_digest,
            "learned_config_digest": self.learned_config_digest,
            "project_authority_digest": self.project_authority_digest,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_payload())
