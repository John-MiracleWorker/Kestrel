from __future__ import annotations

from math import isfinite

MIN_ROUTINE_POLL_INTERVAL_SECONDS = 1.0
MAX_ROUTINE_POLL_INTERVAL_SECONDS = 3_600.0
MIN_ROUTINE_CLAIM_TTL_SECONDS = 1.0
MAX_ROUTINE_CLAIM_TTL_SECONDS = 3_600.0
MIN_ROUTINES_PER_TICK = 1
MAX_ROUTINES_PER_TICK = 100
MIN_ROUTINE_QUERY_LIMIT = 1
MAX_ROUTINE_HISTORY_LIMIT = 500
MAX_ROUTINE_RECONCILIATION_LIMIT = 1_000

MIN_ROUTINE_INTERVAL_SECONDS = 60
MAX_ROUTINE_INTERVAL_SECONDS = 31_536_000
MIN_ROUTINE_MISFIRE_GRACE_SECONDS = 0
MAX_ROUTINE_MISFIRE_GRACE_SECONDS = 604_800


def validate_routine_poll_interval(
    value: object,
    *,
    field_name: str = "routine_poll_interval_seconds",
) -> float:
    return _bounded_float(
        value,
        field_name=field_name,
        minimum=MIN_ROUTINE_POLL_INTERVAL_SECONDS,
        maximum=MAX_ROUTINE_POLL_INTERVAL_SECONDS,
    )


def validate_routine_claim_ttl(
    value: object,
    *,
    field_name: str = "routine_claim_ttl_seconds",
) -> float:
    return _bounded_float(
        value,
        field_name=field_name,
        minimum=MIN_ROUTINE_CLAIM_TTL_SECONDS,
        maximum=MAX_ROUTINE_CLAIM_TTL_SECONDS,
    )


def validate_routines_per_tick(
    value: object,
    *,
    field_name: str = "max_routines_per_tick",
) -> int:
    return _bounded_int(
        value,
        field_name=field_name,
        minimum=MIN_ROUTINES_PER_TICK,
        maximum=MAX_ROUTINES_PER_TICK,
    )


def validate_routine_history_limit(
    value: object,
    *,
    field_name: str = "routine occurrence limit",
) -> int:
    return _bounded_int(
        value,
        field_name=field_name,
        minimum=MIN_ROUTINE_QUERY_LIMIT,
        maximum=MAX_ROUTINE_HISTORY_LIMIT,
    )


def validate_routine_reconciliation_limit(
    value: object,
    *,
    field_name: str = "routine reconciliation limit",
) -> int:
    return _bounded_int(
        value,
        field_name=field_name,
        minimum=MIN_ROUTINE_QUERY_LIMIT,
        maximum=MAX_ROUTINE_RECONCILIATION_LIMIT,
    )


def validate_routine_interval(
    value: object,
    *,
    field_name: str = "interval_seconds",
) -> int:
    return _bounded_int(
        value,
        field_name=field_name,
        minimum=MIN_ROUTINE_INTERVAL_SECONDS,
        maximum=MAX_ROUTINE_INTERVAL_SECONDS,
    )


def validate_routine_misfire_grace(
    value: object,
    *,
    field_name: str = "misfire_grace_seconds",
) -> int:
    return _bounded_int(
        value,
        field_name=field_name,
        minimum=MIN_ROUTINE_MISFIRE_GRACE_SECONDS,
        maximum=MAX_ROUTINE_MISFIRE_GRACE_SECONDS,
    )


def _bounded_float(
    value: object,
    *,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a number")
    parsed = float(value)
    if not isfinite(parsed):
        raise ValueError(f"{field_name} must be finite")
    if parsed < minimum or parsed > maximum:
        raise ValueError(
            f"{field_name} must be between {minimum:g} and {maximum:g} seconds"
        )
    return parsed


def _bounded_int(
    value: object,
    *,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(f"{field_name} must be between {minimum} and {maximum}")
    return value
