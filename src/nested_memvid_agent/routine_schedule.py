from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_CRON_FIELDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 7),
)
_MAX_CRON_SEARCH_MINUTES = 60 * 24 * 366 * 8


@dataclass(frozen=True)
class CronSchedule:
    expression: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    day_of_month_wildcard: bool
    day_of_week_wildcard: bool

    def matches(self, local: datetime) -> bool:
        cron_weekday = (local.weekday() + 1) % 7
        day_of_month_matches = local.day in self.days_of_month
        day_of_week_matches = cron_weekday in self.days_of_week
        if self.day_of_month_wildcard:
            day_matches = day_of_week_matches
        elif self.day_of_week_wildcard:
            day_matches = day_of_month_matches
        else:
            # Traditional cron treats restricted day-of-month and day-of-week
            # as alternatives rather than requiring both.
            day_matches = day_of_month_matches or day_of_week_matches
        return bool(
            local.minute in self.minutes
            and local.hour in self.hours
            and local.month in self.months
            and day_matches
        )


def normalize_timezone(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("timezone must be an IANA timezone name")
    normalized = value.strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("timezone must be an IANA timezone name")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {normalized}") from exc
    return normalized


def parse_cron_expression(value: object) -> CronSchedule:
    if not isinstance(value, str):
        raise ValueError("cron_expression must be a five-field cron expression")
    fields = value.strip().split()
    if len(fields) != 5:
        raise ValueError("cron_expression must contain five fields")
    parsed: list[frozenset[int]] = []
    for raw, (name, minimum, maximum) in zip(fields, _CRON_FIELDS, strict=True):
        values = _parse_field(raw, name=name, minimum=minimum, maximum=maximum)
        if name == "day_of_week":
            values = frozenset(0 if item == 7 else item for item in values)
        parsed.append(values)
    return CronSchedule(
        expression=" ".join(fields),
        minutes=parsed[0],
        hours=parsed[1],
        days_of_month=parsed[2],
        months=parsed[3],
        days_of_week=parsed[4],
        day_of_month_wildcard=fields[2] == "*",
        day_of_week_wildcard=fields[4] == "*",
    )


def next_cron_instant(
    expression: str,
    timezone: str,
    *,
    after: datetime,
    inclusive: bool = False,
) -> datetime:
    if after.tzinfo is None:
        raise ValueError("cron search instant must be timezone-aware")
    schedule = parse_cron_expression(expression)
    zone = ZoneInfo(normalize_timezone(timezone))
    candidate = after.astimezone(UTC).replace(second=0, microsecond=0)
    if not inclusive or candidate < after.astimezone(UTC):
        candidate += timedelta(minutes=1)
    for _ in range(_MAX_CRON_SEARCH_MINUTES):
        if schedule.matches(candidate.astimezone(zone)):
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron_expression has no match within the supported horizon")


def _parse_field(
    raw: str,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> frozenset[int]:
    values: set[int] = set()
    for raw_part in raw.split(","):
        part = raw_part.strip()
        if not part:
            raise ValueError(f"cron {name} contains an empty list item")
        base, separator, raw_step = part.partition("/")
        step = 1
        if separator:
            step = _field_integer(
                raw_step,
                name=f"{name} step",
                minimum=1,
                maximum=maximum - minimum + 1,
            )
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            raw_start, range_separator, raw_end = base.partition("-")
            if not range_separator:
                raise ValueError(f"cron {name} range is invalid")
            start = _field_integer(
                raw_start,
                name=name,
                minimum=minimum,
                maximum=maximum,
            )
            end = _field_integer(
                raw_end,
                name=name,
                minimum=minimum,
                maximum=maximum,
            )
            if end < start:
                raise ValueError(f"cron {name} range must be ascending")
        else:
            start = _field_integer(
                base,
                name=name,
                minimum=minimum,
                maximum=maximum,
            )
            end = start
        values.update(range(start, end + 1, step))
    if not values:
        raise ValueError(f"cron {name} has no values")
    return frozenset(values)


def _field_integer(
    value: str,
    *,
    name: str,
    minimum: int,
    maximum: int,
) -> int:
    if not value.isascii() or not value.isdigit():
        raise ValueError(f"cron {name} must be numeric")
    parsed = int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"cron {name} must be between {minimum} and {maximum}")
    return parsed
