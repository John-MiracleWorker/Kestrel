from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from time import monotonic, sleep

import pytest

import nested_memvid_agent.run_manager as run_manager_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.routine_limits import (
    MAX_ROUTINE_CLAIM_TTL_SECONDS,
    MAX_ROUTINE_HISTORY_LIMIT,
    MAX_ROUTINE_INTERVAL_SECONDS,
    MAX_ROUTINE_MISFIRE_GRACE_SECONDS,
    MAX_ROUTINE_POLL_INTERVAL_SECONDS,
    MAX_ROUTINE_RECONCILIATION_LIMIT,
    MAX_ROUTINES_PER_TICK,
    MIN_ROUTINE_CLAIM_TTL_SECONDS,
    MIN_ROUTINE_INTERVAL_SECONDS,
    MIN_ROUTINE_MISFIRE_GRACE_SECONDS,
    MIN_ROUTINE_POLL_INTERVAL_SECONDS,
    MIN_ROUTINES_PER_TICK,
)
from nested_memvid_agent.routines import RoutineService
from nested_memvid_agent.run_manager import RunCapacityError, RunManager
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import (
    AgentStateStore,
    RoutineConflictError,
    RoutineRunNowConflictError,
    routine_manual_occurrence_id,
    routine_occurrence_id,
    routine_run_id,
    routine_session_id,
)


@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_routine_config_and_leases_reject_non_finite_values(
    tmp_path: Path,
    value: float,
) -> None:
    with pytest.raises(ValueError, match="finite"):
        AgentConfig(routine_poll_interval_seconds=value)
    with pytest.raises(ValueError, match="finite"):
        AgentConfig(routine_claim_ttl_seconds=value)

    state = AgentStateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="finite"):
        state.claim_due_routine_occurrences(
            now=datetime.now(UTC),
            claim_owner="finite-test",
            lease_ttl_seconds=value,
        )
    with pytest.raises(ValueError, match="finite"):
        RoutineService(state, object(), claim_ttl_seconds=value)  # type: ignore[arg-type]


def test_routine_runtime_limits_accept_inclusive_boundaries(tmp_path: Path) -> None:
    lower = AgentConfig(
        routine_poll_interval_seconds=MIN_ROUTINE_POLL_INTERVAL_SECONDS,
        routine_claim_ttl_seconds=MIN_ROUTINE_CLAIM_TTL_SECONDS,
        max_routines_per_tick=MIN_ROUTINES_PER_TICK,
    )
    upper = AgentConfig(
        routine_poll_interval_seconds=MAX_ROUTINE_POLL_INTERVAL_SECONDS,
        routine_claim_ttl_seconds=MAX_ROUTINE_CLAIM_TTL_SECONDS,
        max_routines_per_tick=MAX_ROUTINES_PER_TICK,
    )
    assert lower.routine_poll_interval_seconds == MIN_ROUTINE_POLL_INTERVAL_SECONDS
    assert upper.routine_poll_interval_seconds == MAX_ROUTINE_POLL_INTERVAL_SECONDS

    state = AgentStateStore(tmp_path / "state.db")
    service = RoutineService(
        state,
        object(),  # type: ignore[arg-type]
        claim_ttl_seconds=MAX_ROUTINE_CLAIM_TTL_SECONDS,
        max_occurrences_per_tick=MAX_ROUTINES_PER_TICK,
    )
    assert service.claim_ttl_seconds == MAX_ROUTINE_CLAIM_TTL_SECONDS
    assert service.max_occurrences_per_tick == MAX_ROUTINES_PER_TICK
    assert state.claim_due_routine_occurrences(
        now=datetime.now(UTC),
        claim_owner="boundary",
        lease_ttl_seconds=MIN_ROUTINE_CLAIM_TTL_SECONDS,
        limit=MIN_ROUTINES_PER_TICK,
    ).claimed == ()
    assert state.list_routine_occurrences(limit=MAX_ROUTINE_HISTORY_LIMIT) == []
    assert (
        state.list_reconcilable_routine_occurrences(
            limit=MAX_ROUTINE_RECONCILIATION_LIMIT
        )
        == []
    )


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("routine_poll_interval_seconds", MIN_ROUTINE_POLL_INTERVAL_SECONDS / 2),
        ("routine_poll_interval_seconds", MAX_ROUTINE_POLL_INTERVAL_SECONDS + 1),
        ("routine_claim_ttl_seconds", MIN_ROUTINE_CLAIM_TTL_SECONDS / 2),
        ("routine_claim_ttl_seconds", MAX_ROUTINE_CLAIM_TTL_SECONDS + 1),
        ("max_routines_per_tick", MIN_ROUTINES_PER_TICK - 1),
        ("max_routines_per_tick", MAX_ROUTINES_PER_TICK + 1),
    ],
)
def test_routine_config_rejects_out_of_range_magnitudes(
    field_name: str,
    value: float | int,
) -> None:
    with pytest.raises(ValueError, match="between"):
        AgentConfig(**{field_name: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("environment_name", "value"),
    [
        (
            "NEST_AGENT_ROUTINE_POLL_INTERVAL_SECONDS",
            str(MAX_ROUTINE_POLL_INTERVAL_SECONDS + 1),
        ),
        (
            "NEST_AGENT_ROUTINE_CLAIM_TTL_SECONDS",
            str(MAX_ROUTINE_CLAIM_TTL_SECONDS + 1),
        ),
        ("NEST_AGENT_MAX_ROUTINES_PER_TICK", str(MAX_ROUTINES_PER_TICK + 1)),
    ],
)
def test_routine_config_rejects_out_of_range_environment_values(
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
    value: str,
) -> None:
    monkeypatch.setenv(environment_name, value)
    with pytest.raises(ValueError, match="between"):
        AgentConfig.from_env()


def test_routine_config_accepts_environment_boundary_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "NEST_AGENT_ROUTINE_POLL_INTERVAL_SECONDS",
        str(MAX_ROUTINE_POLL_INTERVAL_SECONDS),
    )
    monkeypatch.setenv(
        "NEST_AGENT_ROUTINE_CLAIM_TTL_SECONDS",
        str(MAX_ROUTINE_CLAIM_TTL_SECONDS),
    )
    monkeypatch.setenv(
        "NEST_AGENT_MAX_ROUTINES_PER_TICK",
        str(MAX_ROUTINES_PER_TICK),
    )

    config = AgentConfig.from_env()

    assert config.routine_poll_interval_seconds == MAX_ROUTINE_POLL_INTERVAL_SECONDS
    assert config.routine_claim_ttl_seconds == MAX_ROUTINE_CLAIM_TTL_SECONDS
    assert config.max_routines_per_tick == MAX_ROUTINES_PER_TICK


def test_routine_service_and_state_reject_unbounded_direct_limits(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="between"):
        RoutineService(
            state,
            object(),  # type: ignore[arg-type]
            claim_ttl_seconds=MAX_ROUTINE_CLAIM_TTL_SECONDS + 1,
        )
    with pytest.raises(ValueError, match="between"):
        RoutineService(
            state,
            object(),  # type: ignore[arg-type]
            max_occurrences_per_tick=MAX_ROUTINES_PER_TICK + 1,
        )
    with pytest.raises(ValueError, match="between"):
        state.claim_due_routine_occurrences(
            now=datetime.now(UTC),
            claim_owner="unbounded",
            lease_ttl_seconds=MAX_ROUTINE_CLAIM_TTL_SECONDS + 1,
        )

    for invalid in (True, 1.0, "1", 0, MAX_ROUTINES_PER_TICK + 1):
        with pytest.raises(ValueError, match="routine claim limit"):
            state.claim_due_routine_occurrences(
                now=datetime.now(UTC),
                claim_owner="invalid-limit",
                limit=invalid,  # type: ignore[arg-type]
            )
    for invalid in (True, 1.0, "1", 0, MAX_ROUTINE_HISTORY_LIMIT + 1):
        with pytest.raises(ValueError, match="routine occurrence limit"):
            state.list_routine_occurrences(limit=invalid)  # type: ignore[arg-type]
    for invalid in (
        True,
        1.0,
        "1",
        0,
        MAX_ROUTINE_RECONCILIATION_LIMIT + 1,
    ):
        with pytest.raises(ValueError, match="routine reconciliation limit"):
            state.list_reconcilable_routine_occurrences(
                limit=invalid  # type: ignore[arg-type]
            )


def test_schema_17_creates_routine_tables_and_revision_uniqueness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    AgentStateStore(path)

    with sqlite3.connect(path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        occurrence_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'routine_occurrences'"
            ).fetchone()[0]
        )
        routine_sql = str(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'routines'"
            ).fetchone()[0]
        )

    assert {"routines", "routine_occurrences"} <= tables
    assert "UNIQUE (routine_id, routine_revision, scheduled_for)" in occurrence_sql
    normalized_routine_sql = " ".join(routine_sql.split())
    assert (
        f"interval_seconds BETWEEN {MIN_ROUTINE_INTERVAL_SECONDS} "
        f"AND {MAX_ROUTINE_INTERVAL_SECONDS}"
        in normalized_routine_sql
    )
    assert (
        f"misfire_grace_seconds BETWEEN {MIN_ROUTINE_MISFIRE_GRACE_SECONDS} "
        f"AND {MAX_ROUTINE_MISFIRE_GRACE_SECONDS}"
        in normalized_routine_sql
    )
    insert_sql = """
        INSERT INTO routines (
            routine_id, name, prompt, schedule_kind, start_at, interval_seconds,
            enabled, revision, autonomy_mode, misfire_grace_seconds,
            created_at, updated_at
        ) VALUES (?, 'name', 'prompt', 'interval', ?, ?, 0, 1, 'background', ?, ?, ?)
    """
    now = datetime.now(UTC).isoformat()
    invalid_magnitudes = (
        ("oversized-interval", MAX_ROUTINE_INTERVAL_SECONDS + 1, 60),
        ("oversized-grace", MIN_ROUTINE_INTERVAL_SECONDS, MAX_ROUTINE_MISFIRE_GRACE_SECONDS + 1),
    )
    for routine_id, interval, grace in invalid_magnitudes:
        with sqlite3.connect(path) as conn, pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                insert_sql,
                (routine_id, now, interval, grace, now, now),
            )


def test_schema_19_adds_manual_trigger_provenance_and_uniqueness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    AgentStateStore(path)

    with sqlite3.connect(path) as conn:
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(routine_occurrences)").fetchall()
        }
        indexes = {
            str(row[1])
            for row in conn.execute("PRAGMA index_list(routine_occurrences)").fetchall()
        }

    assert {"trigger_kind", "trigger_key_digest", "requested_at"} <= columns
    assert "idx_routine_occurrences_manual_trigger" in indexes


def test_schema_18_to_19_preserves_scheduled_occurrences(tmp_path: Path) -> None:
    path = tmp_path / "legacy-v18.db"
    state = AgentStateStore(path)
    due = datetime(2030, 1, 1, tzinfo=UTC)
    routine = _create_routine(state, routine_id="legacy-scheduled", start=due)
    routine = state.update_routine(
        routine.routine_id,
        expected_revision=routine.revision,
        enabled=True,
    )
    occurrence = state.claim_due_routine_occurrences(
        now=due,
        claim_owner="legacy-owner",
    ).claimed[0]
    with sqlite3.connect(path) as conn:
        conn.execute("DROP INDEX idx_routine_occurrences_manual_trigger")
        conn.execute("ALTER TABLE routine_occurrences DROP COLUMN requested_at")
        conn.execute("ALTER TABLE routine_occurrences DROP COLUMN trigger_key_digest")
        conn.execute("ALTER TABLE routine_occurrences DROP COLUMN trigger_kind")
        conn.execute("UPDATE schema_version SET version = 18 WHERE id = 1")

    migrated = AgentStateStore(path)
    preserved = migrated.get_routine_occurrence(occurrence.occurrence_id)

    assert migrated.schema_version() == 19
    assert preserved.trigger_kind == "scheduled"
    assert preserved.trigger_key_digest is None
    assert preserved.requested_at is None
    assert preserved.routine_revision == routine.revision


def test_routine_create_is_disabled_then_updates_and_deletes_with_cas(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    start = datetime(2030, 1, 1, tzinfo=UTC)

    with pytest.raises(ValueError, match="created disabled"):
        _create_routine(state, start=start, enabled=True)

    created = _create_routine(state, start=start)
    assert created.enabled is False
    assert created.revision == 1

    enabled = state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    assert enabled.enabled is True
    assert enabled.revision == 2
    with pytest.raises(RoutineConflictError) as stale:
        state.update_routine(
            created.routine_id,
            expected_revision=created.revision,
            name="stale",
        )
    assert stale.value.current.revision == 2

    deleted = state.delete_routine(
        created.routine_id,
        expected_revision=enabled.revision,
    )
    assert deleted.deleted_at is not None
    assert deleted.enabled is False
    assert deleted.revision == 3
    assert state.list_routines() == []
    assert state.get_routine(created.routine_id).deleted_at == deleted.deleted_at
    with pytest.raises(RoutineConflictError):
        state.delete_routine(
            created.routine_id,
            expected_revision=enabled.revision,
        )


def test_routine_rejects_short_intervals_and_raw_secrets(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    start = datetime(2030, 1, 1, tzinfo=UTC)
    secret = "routine-secret-value-7cf994"
    register_secret_value(secret)

    with pytest.raises(ValueError, match="between 60"):
        _create_routine(
            state,
            start=start,
            schedule_kind="interval",
            interval_seconds=59,
        )
    for field in ("name", "prompt", "workspace", "provider", "model"):
        values: dict[str, object] = {field: f"unsafe {secret}"}
        with pytest.raises(ValueError, match=f"{field} may not contain raw secrets"):
            _create_routine(state, routine_id=f"unsafe_{field}", start=start, **values)

    safe = _create_routine(
        state,
        routine_id="safe-ref",
        start=start,
        name="secret://routine_name",
        prompt="secret://routine_prompt",
        workspace="secret://routine_workspace",
        provider="secret://routine_provider",
        model="secret://routine_model",
    )
    assert safe.prompt == "secret://routine_prompt"

    with pytest.raises(ValueError, match="prompt may not contain raw secrets"):
        state.update_routine(
            safe.routine_id,
            expected_revision=safe.revision,
            prompt=f"later {secret}",
        )
    with pytest.raises(ValueError, match="name must be a string"):
        state.update_routine(
            safe.routine_id,
            expected_revision=safe.revision,
            name=None,
        )


def test_routine_schedule_limits_accept_inclusive_boundaries(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    start = datetime(2030, 1, 1, tzinfo=UTC)
    minimum = _create_routine(
        state,
        routine_id="minimum-schedule",
        start=start,
        schedule_kind="interval",
        interval_seconds=MIN_ROUTINE_INTERVAL_SECONDS,
        misfire_grace_seconds=MIN_ROUTINE_MISFIRE_GRACE_SECONDS,
    )
    maximum = _create_routine(
        state,
        routine_id="maximum-schedule",
        start=start,
        schedule_kind="interval",
        interval_seconds=MAX_ROUTINE_INTERVAL_SECONDS,
        misfire_grace_seconds=MAX_ROUTINE_MISFIRE_GRACE_SECONDS,
    )

    assert minimum.interval_seconds == MIN_ROUTINE_INTERVAL_SECONDS
    assert minimum.misfire_grace_seconds == MIN_ROUTINE_MISFIRE_GRACE_SECONDS
    assert maximum.interval_seconds == MAX_ROUTINE_INTERVAL_SECONDS
    assert maximum.misfire_grace_seconds == MAX_ROUTINE_MISFIRE_GRACE_SECONDS

    enabled = state.update_routine(
        maximum.routine_id,
        expected_revision=maximum.revision,
        enabled=True,
    )
    claim = state.claim_due_routine_occurrences(
        now=start,
        claim_owner="maximum-schedule",
    )
    assert len(claim.claimed) == 1
    assert state.get_routine(enabled.routine_id).next_run_at == (
        start + timedelta(seconds=MAX_ROUTINE_INTERVAL_SECONDS)
    ).isoformat()


@pytest.mark.parametrize(
    "interval_seconds",
    [
        MIN_ROUTINE_INTERVAL_SECONDS - 1,
        MAX_ROUTINE_INTERVAL_SECONDS + 1,
        True,
        60.0,
        "60",
    ],
)
def test_routine_rejects_invalid_interval_magnitudes_and_types(
    tmp_path: Path,
    interval_seconds: object,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="interval_seconds"):
        _create_routine(
            state,
            start=datetime(2030, 1, 1, tzinfo=UTC),
            schedule_kind="interval",
            interval_seconds=interval_seconds,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "misfire_grace_seconds",
    [
        MIN_ROUTINE_MISFIRE_GRACE_SECONDS - 1,
        MAX_ROUTINE_MISFIRE_GRACE_SECONDS + 1,
        True,
        60.0,
        "60",
    ],
)
def test_routine_rejects_invalid_misfire_grace_magnitudes_and_types(
    tmp_path: Path,
    misfire_grace_seconds: object,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    with pytest.raises(ValueError, match="misfire_grace_seconds"):
        _create_routine(
            state,
            start=datetime(2030, 1, 1, tzinfo=UTC),
            misfire_grace_seconds=misfire_grace_seconds,  # type: ignore[arg-type]
        )


def test_routine_state_rejects_coercible_expected_revisions(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    created = _create_routine(state, start=datetime(2030, 1, 1, tzinfo=UTC))

    for invalid in (True, 1.0, "1"):
        with pytest.raises(ValueError, match="expected_revision"):
            state.update_routine(
                created.routine_id,
                expected_revision=invalid,  # type: ignore[arg-type]
                name="must not update",
            )
        with pytest.raises(ValueError, match="expected_revision"):
            state.delete_routine(
                created.routine_id,
                expected_revision=invalid,  # type: ignore[arg-type]
            )

    assert state.get_routine(created.routine_id) == created


def test_concurrent_ticks_claim_one_deterministic_once_occurrence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    state = AgentStateStore(path)
    due = datetime(2030, 1, 1, tzinfo=UTC)
    created = _create_routine(state, start=due)
    enabled = state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )

    def claim(index: int) -> tuple[str, ...]:
        batch = AgentStateStore(path).claim_due_routine_occurrences(
            now=due,
            claim_owner=f"worker-{index}",
        )
        return tuple(item.occurrence_id for item in batch.claimed)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(16)))

    claimed = [occurrence_id for result in results for occurrence_id in result]
    expected_id = routine_occurrence_id(
        enabled.routine_id,
        enabled.revision,
        due.isoformat(),
    )
    assert claimed == [expected_id]
    occurrence = state.get_routine_occurrence(expected_id)
    assert occurrence.run_id == routine_run_id(enabled.routine_id, expected_id)
    assert occurrence.routine_revision == enabled.revision
    assert occurrence.request["routine_revision"] == enabled.revision
    assert occurrence.request["prompt"] == enabled.prompt
    assert state.get_routine(enabled.routine_id).next_run_at is None


def test_concurrent_manual_trigger_replays_claim_one_occurrence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    state = AgentStateStore(path)
    future = datetime(2030, 1, 2, tzinfo=UTC)
    created = _create_routine(state, routine_id="manual-concurrent", start=future)
    enabled = state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    requested_at = datetime(2030, 1, 1, tzinfo=UTC)
    key = "manual-concurrent-key-0001"

    def claim(index: int):
        return AgentStateStore(path).claim_manual_routine_occurrence(
            enabled.routine_id,
            expected_revision=enabled.revision,
            idempotency_key=key,
            now=requested_at,
            claim_owner=f"manual-owner-{index}",
            lease_ttl_seconds=120,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(claim, range(16)))

    occurrence_ids = {item.occurrence.occurrence_id for item in results}
    assert len(occurrence_ids) == 1
    assert sum(item.dispatch for item in results) == 1
    occurrence = state.get_routine_occurrence(occurrence_ids.pop())
    assert occurrence.occurrence_id == routine_manual_occurrence_id(
        enabled.routine_id,
        str(occurrence.trigger_key_digest),
    )
    assert occurrence.trigger_kind == "manual"
    assert occurrence.trigger_key_digest is not None
    assert len(occurrence.trigger_key_digest) == 64
    assert key not in str(occurrence.request)
    assert occurrence.requested_at == requested_at.isoformat()
    assert state.get_routine(enabled.routine_id).next_run_at == future.isoformat()


def test_manual_trigger_overlap_is_a_durable_idempotent_skip(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    future = datetime(2030, 1, 2, tzinfo=UTC)
    created = _create_routine(state, routine_id="manual-overlap", start=future)
    enabled = state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    first = state.claim_manual_routine_occurrence(
        enabled.routine_id,
        expected_revision=enabled.revision,
        idempotency_key="manual-overlap-first-0001",
        now=datetime(2030, 1, 1, tzinfo=UTC),
        claim_owner="manual-first",
        lease_ttl_seconds=120,
    )
    second = state.claim_manual_routine_occurrence(
        enabled.routine_id,
        expected_revision=enabled.revision,
        idempotency_key="manual-overlap-second-0002",
        now=datetime(2030, 1, 1, tzinfo=UTC),
        claim_owner="manual-second",
    )
    replay = state.claim_manual_routine_occurrence(
        enabled.routine_id,
        expected_revision=enabled.revision,
        idempotency_key="manual-overlap-second-0002",
        now=datetime(2030, 1, 1, 0, 0, 1, tzinfo=UTC),
        claim_owner="manual-replay",
    )

    assert first.dispatch is True
    assert second.created is True
    assert second.dispatch is False
    assert second.occurrence.status == "skipped"
    assert second.occurrence.skip_reason == "overlap_active"
    assert replay.created is False
    assert replay.dispatch is False
    assert replay.occurrence.occurrence_id == second.occurrence.occurrence_id


def test_manual_trigger_reclaim_increments_generation_and_fences_old_owner(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    future = datetime(2030, 1, 2, tzinfo=UTC)
    created = _create_routine(state, routine_id="manual-reclaim", start=future)
    enabled = state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    instant = datetime(2030, 1, 1, tzinfo=UTC)
    first = state.claim_manual_routine_occurrence(
        enabled.routine_id,
        expected_revision=enabled.revision,
        idempotency_key="manual-reclaim-key-0001",
        now=instant,
        claim_owner="manual-old",
        lease_ttl_seconds=1,
    )
    reclaimed = state.claim_manual_routine_occurrence(
        enabled.routine_id,
        expected_revision=enabled.revision,
        idempotency_key="manual-reclaim-key-0001",
        now=instant + timedelta(seconds=2),
        claim_owner="manual-new",
        lease_ttl_seconds=30,
    )

    assert reclaimed.created is False
    assert reclaimed.dispatch is True
    assert reclaimed.occurrence.claim_generation == first.occurrence.claim_generation + 1
    _current, applied = state.mark_routine_occurrence_running(
        first.occurrence.occurrence_id,
        claim_owner="manual-old",
        claim_generation=first.occurrence.claim_generation,
        run_id=first.occurrence.run_id,
        now=instant + timedelta(seconds=3),
    )
    assert applied is False


def test_manual_trigger_requires_enabled_current_definition(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    future = datetime(2030, 1, 2, tzinfo=UTC)
    created = _create_routine(state, routine_id="manual-state-gates", start=future)
    instant = datetime(2030, 1, 1, tzinfo=UTC)

    with pytest.raises(RoutineRunNowConflictError, match="routine_disabled"):
        state.claim_manual_routine_occurrence(
            created.routine_id,
            expected_revision=created.revision,
            idempotency_key="manual-disabled-key-0001",
            now=instant,
            claim_owner="manual-owner",
        )

    enabled = state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    with pytest.raises(RoutineConflictError):
        state.claim_manual_routine_occurrence(
            enabled.routine_id,
            expected_revision=created.revision,
            idempotency_key="manual-stale-key-000002",
            now=instant,
            claim_owner="manual-owner",
        )


def test_interval_misfire_skips_backlog_and_advances_to_first_future_slot(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    start = datetime(2030, 1, 1, tzinfo=UTC)
    created = _create_routine(
        state,
        start=start,
        schedule_kind="interval",
        interval_seconds=60,
        misfire_grace_seconds=10,
    )
    enabled = state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )

    late = start + timedelta(seconds=310)
    batch = state.claim_due_routine_occurrences(now=late, claim_owner="late")
    assert batch.claimed == ()
    assert len(batch.skipped) == 1
    skipped = batch.skipped[0]
    assert skipped.skip_reason == "misfire_grace_exceeded"
    assert skipped.result["missed_intervals"] == 5
    assert state.get_routine(enabled.routine_id).next_run_at == (
        start + timedelta(seconds=360)
    ).isoformat()

    on_time = start + timedelta(seconds=360)
    next_batch = state.claim_due_routine_occurrences(
        now=on_time,
        claim_owner="on-time",
    )
    assert len(next_batch.claimed) == 1


def test_one_active_run_skips_overlapping_interval_occurrence(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    start = datetime(2030, 1, 1, tzinfo=UTC)
    created = _create_routine(
        state,
        start=start,
        schedule_kind="interval",
        interval_seconds=60,
    )
    state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    first = state.claim_due_routine_occurrences(
        now=start,
        claim_owner="first",
        lease_ttl_seconds=120,
    ).claimed[0]
    _current, applied = state.mark_routine_occurrence_running(
        first.occurrence_id,
        claim_owner="first",
        claim_generation=first.claim_generation,
        run_id=first.run_id,
        now=start,
    )
    assert applied is True

    second = state.claim_due_routine_occurrences(
        now=start + timedelta(seconds=60),
        claim_owner="second",
    )
    assert second.claimed == ()
    assert len(second.skipped) == 1
    assert second.skipped[0].skip_reason == "overlap_active"


def test_expired_claim_reclaim_increments_generation_and_fences_old_owner(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    due = datetime(2030, 1, 1, tzinfo=UTC)
    created = _create_routine(state, start=due)
    state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    first = state.claim_due_routine_occurrences(
        now=due,
        claim_owner="owner-a",
        lease_ttl_seconds=30,
    ).claimed[0]
    second = state.claim_due_routine_occurrences(
        now=due + timedelta(seconds=31),
        claim_owner="owner-b",
        lease_ttl_seconds=30,
    ).claimed[0]

    assert second.occurrence_id == first.occurrence_id
    assert second.claim_generation == first.claim_generation + 1
    assert second.claim_owner == "owner-b"
    assert state.release_routine_occurrence_claim(
        first.occurrence_id,
        claim_owner="owner-a",
        claim_generation=first.claim_generation,
        error="stale",
        now=due + timedelta(seconds=32),
    ) is False
    _current, applied = state.mark_routine_occurrence_running(
        first.occurrence_id,
        claim_owner="owner-a",
        claim_generation=first.claim_generation,
        run_id=first.run_id,
        now=due + timedelta(seconds=32),
    )
    assert applied is False


def test_expired_claim_cannot_admit_or_be_finished_without_running(
    tmp_path: Path,
) -> None:
    due = datetime(2030, 1, 1, tzinfo=UTC)
    state = AgentStateStore(
        tmp_path / "state.db",
        routine_admission_clock=lambda: due + timedelta(seconds=31),
    )
    created = _create_routine(state, start=due)
    state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    claim = state.claim_due_routine_occurrences(
        now=due,
        claim_owner="expired-owner",
        lease_ttl_seconds=30,
    ).claimed[0]

    with pytest.raises(ValueError, match="claim expired"):
        state.create_run_for_routine_occurrence(
            occurrence_id=claim.occurrence_id,
            claim_owner="expired-owner",
            claim_generation=claim.claim_generation,
            dispatch_at=due + timedelta(seconds=31),
            run_id=claim.run_id,
            message=str(claim.request["prompt"]),
            session_id=routine_session_id(claim.routine_id),
            workspace=str(tmp_path),
            provider="mock",
            model="mock",
            config_revision="test",
            config_snapshot={"revision": "test"},
        )
    _current, finished = state.finish_routine_occurrence(
        claim.occurrence_id,
        run_id=claim.run_id,
        status="failed",
        error="stale owner",
        now=due + timedelta(seconds=31),
    )
    assert finished is False
    assert state.get_routine_occurrence(claim.occurrence_id).status == "claimed"
    with pytest.raises(KeyError):
        state.get_run(claim.run_id)


def test_stale_dispatch_timestamp_cannot_bypass_current_admission_clock(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    due = datetime.now(UTC)
    created = _create_routine(state, start=due)
    state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    claim = state.claim_due_routine_occurrences(
        now=due,
        claim_owner="delayed-owner",
        lease_ttl_seconds=MIN_ROUTINE_CLAIM_TTL_SECONDS,
    ).claimed[0]
    sleep(MIN_ROUTINE_CLAIM_TTL_SECONDS + 0.05)

    with pytest.raises(ValueError, match="claim expired"):
        state.create_run_for_routine_occurrence(
            occurrence_id=claim.occurrence_id,
            claim_owner="delayed-owner",
            claim_generation=claim.claim_generation,
            dispatch_at=due,
            run_id=claim.run_id,
            message=str(claim.request["prompt"]),
            session_id=routine_session_id(claim.routine_id),
            workspace=str(tmp_path),
            provider="mock",
            model="mock",
            config_revision="test",
            config_snapshot={"revision": "test"},
        )


@pytest.mark.parametrize("mutation", ["disable", "revise", "delete"])
def test_routine_mutation_fences_claim_before_atomic_run_admission(
    tmp_path: Path,
    mutation: str,
) -> None:
    state = AgentStateStore(tmp_path / f"{mutation}.db")
    due = datetime(2030, 1, 1, tzinfo=UTC)
    created = _create_routine(state, routine_id=mutation, start=due)
    enabled = state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    claim = state.claim_due_routine_occurrences(
        now=due,
        claim_owner="owner",
    ).claimed[0]
    revised = None
    if mutation == "disable":
        state.update_routine(
            enabled.routine_id,
            expected_revision=enabled.revision,
            enabled=False,
        )
    elif mutation == "revise":
        revised = state.update_routine(
            enabled.routine_id,
            expected_revision=enabled.revision,
            prompt="revised prompt",
        )
    else:
        state.delete_routine(
            enabled.routine_id,
            expected_revision=enabled.revision,
        )

    with pytest.raises(ValueError, match="routine (disabled|changed|deleted)"):
        state.create_run_for_routine_occurrence(
            occurrence_id=claim.occurrence_id,
            claim_owner="owner",
            claim_generation=claim.claim_generation,
            dispatch_at=due,
            run_id=claim.run_id,
            message=str(claim.request["prompt"]),
            session_id=routine_session_id(claim.routine_id),
            workspace=str(tmp_path),
            provider="mock",
            model="mock",
            config_revision="test",
            config_snapshot={"revision": "test"},
        )
    with pytest.raises(KeyError):
        state.get_run(claim.run_id)
    occurrence = state.get_routine_occurrence(claim.occurrence_id)
    assert occurrence.status == "skipped"
    replacement = state.claim_due_routine_occurrences(
        now=due,
        claim_owner="replacement-owner",
    )
    if mutation == "revise":
        assert revised is not None
        assert len(replacement.claimed) == 1
        assert replacement.claimed[0].routine_revision == revised.revision
        assert replacement.claimed[0].request["prompt"] == "revised prompt"
    else:
        assert replacement.claimed == ()


def test_atomic_occurrence_admission_is_internal_and_idempotent(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    due = datetime(2030, 1, 1, tzinfo=UTC)
    created = _create_routine(state, start=due)
    state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    claim = state.claim_due_routine_occurrences(
        now=due,
        claim_owner="owner",
    ).claimed[0]
    arguments = {
        "occurrence_id": claim.occurrence_id,
        "claim_owner": "owner",
        "claim_generation": claim.claim_generation,
        "dispatch_at": due,
        "run_id": claim.run_id,
        "message": str(claim.request["prompt"]),
        "session_id": routine_session_id(claim.routine_id),
        "workspace": str(tmp_path),
        "provider": "mock",
        "model": "mock",
        "config_revision": "test",
        "config_snapshot": {"revision": "test"},
    }

    run, created_run = state.create_run_for_routine_occurrence(**arguments)
    replay, replay_created = state.create_run_for_routine_occurrence(**arguments)

    assert created_run is True
    assert replay_created is False
    assert replay == run
    assert run.turn_origin == "scheduled_routine"
    assert run.transcript_scope == "internal"
    assert run.turn_source is None
    assert run.config_snapshot["routine_provenance"] == {
        "routine_id": claim.routine_id,
        "occurrence_id": claim.occurrence_id,
        "routine_revision": claim.routine_revision,
        "scheduled_for": claim.scheduled_for,
        "claim_generation": claim.claim_generation,
    }
    assert state.get_routine_occurrence(claim.occurrence_id).status == "running"


def test_startup_recovers_crash_window_with_complete_atomic_task_graph(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    due = datetime.now(UTC)
    created = _create_routine(state, start=due)
    state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    claim = state.claim_due_routine_occurrences(
        now=due,
        claim_owner="crashed-owner",
    ).claimed[0]
    state.create_run_for_routine_occurrence(
        occurrence_id=claim.occurrence_id,
        claim_owner="crashed-owner",
        claim_generation=claim.claim_generation,
        dispatch_at=due,
        run_id=claim.run_id,
        message=str(claim.request["prompt"]),
        session_id=routine_session_id(claim.routine_id),
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
        config_revision="crash-window",
        config_snapshot={
            "revision": "crash-window",
            "autonomy_mode": "background",
        },
    )
    assert state.list_task_nodes(claim.run_id) == []

    manager = _manager(tmp_path)
    final = _wait_for_run(manager, claim.run_id)
    tasks = manager.state.list_task_nodes(claim.run_id)

    assert final.status == "completed"
    assert len(tasks) == 4
    assert tasks[0].title == "Root objective"
    assert tasks[0].plan is not None
    assert tasks[0].plan["decomposition"] == "initial"
    assert all(task.parent_id == tasks[0].task_id for task in tasks[1:])
    RoutineService(manager.state, manager).tick(now=datetime.now(UTC))
    assert manager.state.get_routine_occurrence(claim.occurrence_id).status == "completed"
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_routine_tick_expires_headless_approval_and_releases_overlap(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    due = datetime.now(UTC)
    created = _create_routine(manager.state, start=due)
    manager.state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    claim = manager.state.claim_due_routine_occurrences(
        now=due,
        claim_owner="approval-owner",
    ).claimed[0]
    manager.state.create_run_for_routine_occurrence(
        occurrence_id=claim.occurrence_id,
        claim_owner="approval-owner",
        claim_generation=claim.claim_generation,
        dispatch_at=due,
        run_id=claim.run_id,
        message=str(claim.request["prompt"]),
        session_id=routine_session_id(claim.routine_id),
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
        config_revision="approval-expiry",
        config_snapshot={"revision": "approval-expiry"},
    )
    manager.state.transition_run(
        claim.run_id,
        "blocked",
        stop_reason="approval_required",
    )
    manager.state.create_approval(
        approval_id="expired-routine-approval",
        run_id=claim.run_id,
        tool_call_id="tool-expired",
        tool_name="shell.run",
        arguments={"command": ["true"]},
        risk="high",
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )

    result = RoutineService(manager.state, manager).tick(now=datetime.now(UTC))

    assert "expired-routine-approval" in {
        item["approval_id"] for item in manager.state.list_approvals(status="expired")
    }
    assert manager.state.get_run(claim.run_id).status == "failed"
    assert manager.state.get_run(claim.run_id).stop_reason == "approval_expired"
    assert manager.state.get_routine_occurrence(claim.occurrence_id).status == "failed"
    assert claim.occurrence_id in result.reconciled
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_run_now_is_scoped_idempotent_and_preserves_schedule(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    requested_at = datetime(2030, 1, 1, tzinfo=UTC)
    future = requested_at + timedelta(days=1)
    target = _create_routine(
        manager.state,
        routine_id="manual-target",
        start=future,
    )
    target = manager.state.update_routine(
        target.routine_id,
        expected_revision=target.revision,
        enabled=True,
    )
    unrelated = _create_routine(
        manager.state,
        routine_id="manual-unrelated-due",
        start=requested_at,
    )
    manager.state.update_routine(
        unrelated.routine_id,
        expected_revision=unrelated.revision,
        enabled=True,
    )
    service = RoutineService(
        manager.state,
        manager,
        clock=lambda: requested_at,
        claim_owner="manual-service",
    )
    key = "manual-service-key-000001"

    result = service.run_now(
        target.routine_id,
        expected_revision=target.revision,
        idempotency_key=key,
    )
    assert result.idempotent_replay is False
    assert result.dispatch is not None
    run = _wait_for_run(manager, result.occurrence.run_id)
    service.reconcile(now=requested_at + timedelta(seconds=1))
    replay = service.run_now(
        target.routine_id,
        expected_revision=target.revision,
        idempotency_key=key,
        now=requested_at + timedelta(seconds=2),
    )

    assert replay.idempotent_replay is True
    assert replay.dispatch is None
    assert replay.occurrence.occurrence_id == result.occurrence.occurrence_id
    assert replay.occurrence.status == "completed"
    assert manager.state.get_routine(target.routine_id).next_run_at == future.isoformat()
    assert manager.state.list_routine_occurrences(unrelated.routine_id) == []
    provenance = run.config_snapshot["routine_provenance"]
    assert provenance["trigger_kind"] == "manual"
    assert provenance["requested_at"] == requested_at.isoformat()
    assert provenance["trigger_key_digest"] == result.occurrence.trigger_key_digest
    assert key not in str(provenance)
    assert run.turn_origin == "scheduled_routine"
    assert run.transcript_scope == "internal"
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_concurrent_run_now_replays_admit_one_run(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    requested_at = datetime(2030, 1, 1, tzinfo=UTC)
    routine = _create_routine(
        manager.state,
        routine_id="manual-service-concurrent",
        start=requested_at + timedelta(days=1),
    )
    routine = manager.state.update_routine(
        routine.routine_id,
        expected_revision=routine.revision,
        enabled=True,
    )
    service = RoutineService(
        manager.state,
        manager,
        clock=lambda: requested_at,
        claim_owner="manual-concurrent-service",
    )

    def trigger(_index: int):
        return service.run_now(
            routine.routine_id,
            expected_revision=routine.revision,
            idempotency_key="manual-concurrent-service-key",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(trigger, range(16)))

    run_ids = {item.occurrence.run_id for item in results}
    assert len(run_ids) == 1
    run_id = run_ids.pop()
    run = _wait_for_run(manager, run_id)
    assert len(manager.state.list_runs_for_session(run.session_id)) == 1
    assert len(manager.state.list_routine_occurrences(routine.routine_id)) == 1
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_routine_service_dispatches_trusted_internal_run_and_reconciles(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    due = datetime(2030, 1, 1, tzinfo=UTC)
    created = _create_routine(manager.state, start=due)
    enabled = manager.state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    service = RoutineService(
        manager.state,
        manager,
        clock=lambda: due,
        claim_owner="service-owner",
    )

    tick = service.tick()
    assert tick.claimed == 1
    assert len(tick.dispatches) == 1
    occurrence_id = routine_occurrence_id(
        enabled.routine_id,
        enabled.revision,
        due.isoformat(),
    )
    expected_run_id = routine_run_id(enabled.routine_id, occurrence_id)
    assert tick.dispatches[0].run_id == expected_run_id
    run = _wait_for_run(manager, expected_run_id)
    assert run.turn_origin == "scheduled_routine"
    assert run.transcript_scope == "internal"
    assert run.turn_source is None
    assert run.session_id == routine_session_id(enabled.routine_id)
    assert run.config_snapshot["routine_provenance"] == {
        "routine_id": enabled.routine_id,
        "occurrence_id": occurrence_id,
        "routine_revision": enabled.revision,
        "scheduled_for": due.isoformat(),
        "claim_generation": 1,
    }

    reconciled = service.tick(now=due + timedelta(seconds=1))
    assert occurrence_id in reconciled.reconciled
    occurrence = manager.state.get_routine_occurrence(occurrence_id)
    assert occurrence.status == "completed"
    assert len(manager.state.list_runs_for_session(run.session_id)) == 1


def test_reconcile_only_does_not_claim_next_due_interval(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    first_due = datetime(2030, 1, 1, tzinfo=UTC)
    created = _create_routine(
        manager.state,
        routine_id="long-running-cli-interval",
        start=first_due,
        schedule_kind="interval",
        interval_seconds=60,
    )
    manager.state.update_routine(
        created.routine_id,
        expected_revision=created.revision,
        enabled=True,
    )
    service = RoutineService(
        manager.state,
        manager,
        claim_owner="reconcile-only-owner",
    )

    tick = service.tick(now=first_due)
    assert tick.claimed == 1
    first = tick.dispatches[0]
    _wait_for_run(manager, first.run_id)

    reconciled = service.reconcile(now=first_due + timedelta(seconds=60))

    assert reconciled == (first.occurrence_id,)
    occurrences = manager.state.list_routine_occurrences(created.routine_id)
    assert [item.occurrence_id for item in occurrences] == [first.occurrence_id]
    assert occurrences[0].status == "completed"
    assert manager.state.get_routine(created.routine_id).next_run_at == (
        first_due + timedelta(seconds=60)
    ).isoformat()
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_terminal_occurrence_reconciliation_is_not_starved_by_newer_blocked_runs(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    start = datetime(2030, 1, 1, tzinfo=UTC)
    older = _create_routine(
        state,
        routine_id="older-terminal",
        start=start,
    )
    state.update_routine(
        older.routine_id,
        expected_revision=older.revision,
        enabled=True,
    )
    older_occurrence = state.claim_due_routine_occurrences(
        now=start,
        claim_owner="older-owner",
    ).claimed[0]
    _older_running, older_applied = state.mark_routine_occurrence_running(
        older_occurrence.occurrence_id,
        claim_owner="older-owner",
        claim_generation=older_occurrence.claim_generation,
        run_id=older_occurrence.run_id,
        now=start,
    )
    assert older_applied is True

    for index in range(100):
        scheduled = start + timedelta(seconds=index + 1)
        routine = _create_routine(
            state,
            routine_id=f"blocked-{index:03d}",
            start=scheduled,
        )
        state.update_routine(
            routine.routine_id,
            expected_revision=routine.revision,
            enabled=True,
        )
        occurrence = state.claim_due_routine_occurrences(
            now=scheduled,
            claim_owner=f"blocked-owner-{index}",
        ).claimed[0]
        state.create_run(
            run_id=occurrence.run_id,
            message="blocked routine",
            session_id=routine_session_id(routine.routine_id),
            workspace=str(tmp_path),
            provider="mock",
            model="mock",
            turn_origin="scheduled_routine",
            transcript_scope="internal",
        )
        state.transition_run(
            occurrence.run_id,
            "blocked",
            stop_reason="approval_required",
        )
        _running, applied = state.mark_routine_occurrence_running(
            occurrence.occurrence_id,
            claim_owner=f"blocked-owner-{index}",
            claim_generation=occurrence.claim_generation,
            run_id=occurrence.run_id,
            now=scheduled,
        )
        assert applied is True

    service = RoutineService(
        state,
        object(),  # type: ignore[arg-type]
        max_occurrences_per_tick=1,
    )
    result = service.tick(now=start + timedelta(seconds=200))

    assert older_occurrence.occurrence_id in result.reconciled
    assert state.get_routine_occurrence(older_occurrence.occurrence_id).status == "failed"
    assert len(state.list_routine_occurrences(statuses=("running",), limit=200)) == 100


def test_run_manager_shutdown_closes_new_admission(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    assert manager.shutdown(timeout_seconds=1.0) is True
    with pytest.raises(RunCapacityError, match="run_manager_shutting_down"):
        manager.create_run(
            message="must not start after lifecycle shutdown",
            session_id="shutdown-test",
        )


def test_run_manager_shutdown_reports_worker_that_outlives_bound(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    started = Event()
    release = Event()

    def blocking_worker(_thread_key: str) -> None:
        started.set()
        release.wait(timeout=2.0)

    manager._start_thread("shutdown-worker", blocking_worker)
    assert started.wait(timeout=1.0)
    assert manager.shutdown(timeout_seconds=0.01) is False
    release.set()
    assert manager.shutdown(timeout_seconds=1.0) is True


def test_run_manager_shutdown_cancels_parent_owned_subagent_thread(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    parent = manager.state.create_run(
        run_id="shutdown-parent",
        message="blocked parent",
        session_id="shutdown-session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    manager.state.transition_run(parent.run_id, "blocked", stop_reason="subagent_wait")
    started = Event()

    def blocking_subagent(_thread_key: str) -> None:
        started.set()
        while not manager._is_cancelled(parent.run_id):
            sleep(0.01)

    manager._start_thread(
        "shutdown-subagent",
        blocking_subagent,
        owner_run_id=parent.run_id,
    )
    assert started.wait(timeout=1.0)

    assert manager.shutdown(timeout_seconds=1.0) is True
    assert manager.state.get_run(parent.run_id).status == "cancelled"
    assert "shutdown-subagent" not in manager._threads


def test_run_manager_shutdown_joins_after_cancel_path_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    parent = manager.state.create_run(
        run_id="shutdown-cancel-failure",
        message="shutdown fallback",
        session_id="shutdown",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    manager.state.transition_run(parent.run_id, "blocked", stop_reason="worker_wait")
    started = Event()

    def owned_worker(_thread_key: str) -> None:
        started.set()
        while not manager._is_cancelled(parent.run_id):
            sleep(0.01)

    manager._start_thread(
        "shutdown-cancel-failure-worker",
        owned_worker,
        owner_run_id=parent.run_id,
    )
    assert started.wait(timeout=1.0)

    def fail_cancel(_run_id: str) -> dict[str, object]:
        raise RuntimeError("cancel probe")

    monkeypatch.setattr(manager, "cancel_run", fail_cancel)

    assert manager.shutdown(timeout_seconds=1.0) is False
    assert "shutdown-cancel-failure-worker" not in manager._threads
    assert manager.state.get_run(parent.run_id).status == "cancelled"
    assert manager.operational_counters()["shutdown_cancellation_failures"] == 1


def test_thread_start_and_shutdown_are_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = _manager(tmp_path)
    start_entered = Event()
    release_start = Event()
    native_thread = Thread

    class GatedThread(native_thread):
        def start(self) -> None:
            start_entered.set()
            assert release_start.wait(timeout=1.0)
            super().start()

    monkeypatch.setattr(run_manager_module, "Thread", GatedThread)
    launch_errors: list[Exception] = []

    def launch() -> None:
        try:
            manager._start_thread("atomic-start", lambda _key: None)
        except Exception as exc:  # pragma: no cover - diagnostic capture
            launch_errors.append(exc)

    launcher = native_thread(target=launch)
    launcher.start()
    assert start_entered.wait(timeout=1.0)
    shutdown_results: list[bool] = []
    shutdown_thread = native_thread(
        target=lambda: shutdown_results.append(
            manager.shutdown(timeout_seconds=1.0)
        )
    )
    shutdown_thread.start()
    sleep(0.05)
    assert shutdown_thread.is_alive()

    release_start.set()
    launcher.join(timeout=1.0)
    shutdown_thread.join(timeout=1.0)

    assert not launcher.is_alive()
    assert not shutdown_thread.is_alive()
    assert launch_errors == []
    assert shutdown_results == [True]


def _create_routine(
    state: AgentStateStore,
    *,
    routine_id: str = "routine-test",
    start: datetime,
    name: str = "Routine test",
    prompt: str = "Give a deterministic mock response",
    schedule_kind: str = "once",
    interval_seconds: int | None = None,
    enabled: bool = False,
    workspace: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    misfire_grace_seconds: int = 60,
):
    return state.create_routine(
        routine_id=routine_id,
        name=name,
        prompt=prompt,
        schedule_kind=schedule_kind,
        start_at=start,
        interval_seconds=interval_seconds,
        enabled=enabled,
        workspace=workspace,
        provider=provider,
        model=model,
        misfire_grace_seconds=misfire_grace_seconds,
    )


def _manager(tmp_path: Path) -> RunManager:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
        stream=False,
    )
    state = AgentStateStore(config.state_path)
    return RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
    )


def _wait_for_run(manager: RunManager, run_id: str):
    deadline = monotonic() + 10.0
    while monotonic() < deadline:
        run = manager.state.get_run(run_id)
        if run.status in {"completed", "failed", "cancelled"}:
            assert run.status == "completed", run.error
            return run
        sleep(0.02)
    raise AssertionError(f"run {run_id} did not finish")
