"""Canonical JSON and SHA-256 digest helpers for Flock qualification values.

Canonical JSON sorts map keys and semantic sets (``set``/``frozenset``),
preserves ordered evidence lists (``list``/``tuple``), rejects NaN/Infinity,
and hashes UTF-8 bytes with SHA-256.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, is_dataclass
from typing import Any

__all__ = ["canonical_json", "canonical_digest", "canonicalize"]


def canonicalize(value: Any) -> Any:
    """Return a JSON-safe canonical form of *value*.

    - dataclasses become their ``asdict`` mapping;
    - mappings are keyed by string and recursively canonicalized;
    - sets/frozensets become lists sorted by canonical JSON encoding;
    - lists/tuples preserve order;
    - non-finite floats are rejected.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return canonicalize(asdict(value))
    if isinstance(value, dict):
        return {str(key): canonicalize(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        canonical_items = [canonicalize(item) for item in value]
        return sorted(canonical_items, key=_sort_key)
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON rejects NaN and Infinity")
        return value
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    raise TypeError(f"value of type {type(value).__name__} is not canonically serializable")


def _sort_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_json(value: Any) -> str:
    """Serialize *value* to canonical JSON text."""
    return json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_digest(value: Any) -> str:
    """SHA-256 hex digest of the canonical JSON UTF-8 bytes of *value*."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
