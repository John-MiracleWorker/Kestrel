from __future__ import annotations

from datetime import UTC, datetime

import pytest

import nested_memvid_agent.routine_schedule as routine_schedule
from nested_memvid_agent.routine_schedule import (
    next_cron_instant,
    normalize_timezone,
    parse_cron_expression,
)


def test_cron_parser_supports_ranges_lists_and_steps() -> None:
    schedule = parse_cron_expression("*/15 9-17 * * 1-5")

    assert schedule.matches(datetime(2026, 7, 27, 9, 30, tzinfo=UTC))
    assert not schedule.matches(datetime(2026, 7, 26, 9, 30, tzinfo=UTC))
    assert not schedule.matches(datetime(2026, 7, 27, 9, 31, tzinfo=UTC))


def test_cron_search_skips_nonexistent_local_time() -> None:
    result = next_cron_instant(
        "30 2 * * *",
        "America/Detroit",
        after=datetime(2026, 3, 8, 5, 0, tzinfo=UTC),
    )

    assert result == datetime(2026, 3, 9, 6, 30, tzinfo=UTC)


def test_cron_search_represents_both_fall_back_instants() -> None:
    first = next_cron_instant(
        "30 1 * * *",
        "America/Detroit",
        after=datetime(2026, 11, 1, 4, 0, tzinfo=UTC),
    )
    second = next_cron_instant(
        "30 1 * * *",
        "America/Detroit",
        after=first,
    )

    assert first == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert second == datetime(2026, 11, 1, 6, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    "value",
    ["", "not/a-zone", "UTC\nignored"],
)
def test_timezone_validation_rejects_unknown_or_unsafe_names(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_timezone(value)


def test_timezone_validation_rejects_control_characters_before_zone_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routine_schedule,
        "ZoneInfo",
        lambda _value: pytest.fail("unsafe timezone must not reach zone lookup"),
    )

    with pytest.raises(ValueError):
        normalize_timezone("UTC\nignored")


@pytest.mark.parametrize(
    "value",
    ["* * * *", "61 * * * *", "*/0 * * * *", "* * * * MON"],
)
def test_cron_validation_rejects_invalid_expressions(value: str) -> None:
    with pytest.raises(ValueError):
        parse_cron_expression(value)
