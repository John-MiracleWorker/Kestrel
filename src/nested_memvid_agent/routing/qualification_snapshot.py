"""Immutable eligibility snapshots for Flock qualification (plan Task 6).

A snapshot captures every eligible target exactly: target/provider IDs,
adapter, model ID, endpoint, trust/locality, enabled/health, capabilities
with provenance, privacy/network constraints, quality/context limits, exact
price with currency/source/time, and the configuration digest. Prices are
integer micro-USD; unknown is never converted to zero.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from .models import ModelTarget, ProviderProfile
from .qualification_digest import canonical_digest
from .qualification_models import MoneyMicros, PriceSnapshot, TargetSnapshot

__all__ = [
    "PRICE_STALE_WARNING",
    "PRICE_UNKNOWN_BLOCKER",
    "TargetInventorySnapshot",
    "build_target_snapshot",
    "derive_price_snapshot",
    "price_freshness_warning",
]

#: Start-blocker reason code when a target has no trustworthy price.
PRICE_UNKNOWN_BLOCKER = "price_unknown"

#: Freshness warning code when a price is older than the decay half-life.
PRICE_STALE_WARNING = "price_stale"

_MICROS_PER_USD = Decimal(1_000_000)

_CAPABILITY_FLAGS: tuple[tuple[str, str], ...] = (
    ("supports_tools", "tools"),
    ("supports_json", "json"),
    ("supports_vision", "vision"),
    ("supports_reasoning", "reasoning"),
    ("supports_streaming", "streaming"),
)


def _usd_to_micros(value: float) -> MoneyMicros:
    """Convert a USD float to exact micro-USD via its decimal repr."""

    micros = (Decimal(repr(value)) * _MICROS_PER_USD).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return MoneyMicros(int(micros))


def derive_price_snapshot(target: ModelTarget, *, now: datetime) -> PriceSnapshot:
    """Derive the price record implied by the target configuration.

    Both per-million prices configured means an operator verified the billed
    price (zeros included). Anything else is ``unknown`` — never zero.
    """

    captured_at = now.astimezone(UTC).isoformat()
    input_cost = target.input_cost_per_million_usd
    output_cost = target.output_cost_per_million_usd
    if input_cost is not None and output_cost is not None:
        return PriceSnapshot(
            target_id=target.target_id,
            source="operator_verified",
            captured_at=captured_at,
            input_per_million=_usd_to_micros(input_cost),
            output_per_million=_usd_to_micros(output_cost),
        )
    return PriceSnapshot(
        target_id=target.target_id,
        source="unknown",
        captured_at=captured_at,
    )


def price_freshness_warning(
    price: PriceSnapshot,
    *,
    now: datetime,
    max_age_days: int,
) -> str | None:
    """Return ``price_stale`` when the price is older than *max_age_days*."""

    try:
        captured = datetime.fromisoformat(price.captured_at)
    except ValueError:
        return PRICE_STALE_WARNING
    if captured.tzinfo is None or captured.utcoffset() is None:
        captured = captured.replace(tzinfo=UTC)
    if now.astimezone(UTC) - captured.astimezone(UTC) > timedelta(days=max_age_days):
        return PRICE_STALE_WARNING
    return None


def build_target_snapshot(
    target: ModelTarget,
    profile: ProviderProfile,
    price: PriceSnapshot,
    *,
    privacy_class: str,
) -> TargetSnapshot:
    """Snapshot one eligible target with its profile, price, and digests."""

    capabilities = {f"tag:{tag}" for tag in target.capability_tags}
    capabilities.update(
        f"flag:{name}" for field_name, name in _CAPABILITY_FLAGS if getattr(target, field_name)
    )
    network_constraints = ("local_network_only",) if target.locality == "local" else ()
    return TargetSnapshot(
        target_id=target.target_id,
        provider_profile_id=profile.profile_id,
        adapter=profile.adapter,
        model=target.model,
        endpoint=profile.base_url,
        trust_class=target.trust_class,
        locality=target.locality,
        enabled=target.enabled,
        health=target.health,
        capabilities=tuple(sorted(capabilities)),
        privacy_class=privacy_class,
        price=price,
        config_digest=canonical_digest(target.to_public_payload()),
        network_constraints=network_constraints,
        quality_tier=target.quality_tier,
        max_context_tokens=target.max_context_tokens,
    )


@dataclass(frozen=True)
class TargetInventorySnapshot:
    """Digest-bound snapshot of every target eligible for any selected scope."""

    targets: tuple[TargetSnapshot, ...]
    target_inventory_digest: str

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.targets, key=lambda snapshot: snapshot.target_id))
        target_ids = [snapshot.target_id for snapshot in ordered]
        if len(set(target_ids)) != len(target_ids):
            raise ValueError("snapshot target IDs must be unique")
        object.__setattr__(self, "targets", ordered)

    @property
    def target_ids(self) -> tuple[str, ...]:
        return tuple(snapshot.target_id for snapshot in self.targets)

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "target_inventory_digest": self.target_inventory_digest,
                "targets": [snapshot.digest for snapshot in self.targets],
            }
        )
