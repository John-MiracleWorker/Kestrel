"""Feature-mode policy for Core, Ops, and Labs runtime layering."""

from __future__ import annotations

import os
from enum import Enum


class FeatureMode(str, Enum):
    CORE = "core"
    OPS = "ops"
    LABS = "labs"


_MODE_ORDER = {
    FeatureMode.CORE: 0,
    FeatureMode.OPS: 1,
    FeatureMode.LABS: 2,
}


def parse_feature_mode(raw_mode: str | None) -> FeatureMode:
    normalized = (raw_mode or FeatureMode.CORE.value).strip().lower()
    for mode in FeatureMode:
        if normalized == mode.value:
            return mode
    return FeatureMode.CORE


def get_feature_mode() -> FeatureMode:
    return parse_feature_mode(os.getenv("KESTREL_FEATURE_MODE", FeatureMode.CORE.value))


def mode_at_least(current: FeatureMode, required: FeatureMode) -> bool:
    return _MODE_ORDER[current] >= _MODE_ORDER[required]


def mode_supports_ops(current: FeatureMode) -> bool:
    return mode_at_least(current, FeatureMode.OPS)


def mode_supports_labs(current: FeatureMode) -> bool:
    return mode_at_least(current, FeatureMode.LABS)


def enabled_bundles_for_mode(mode: FeatureMode) -> tuple[str, ...]:
    if mode == FeatureMode.LABS:
        return ("chat", "research", "coding", "ops", "media", "self_repair")
    if mode == FeatureMode.OPS:
        return ("chat", "research", "coding", "ops")
    return ("chat", "research", "coding")
