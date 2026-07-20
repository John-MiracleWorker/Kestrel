from __future__ import annotations

from dataclasses import dataclass
from threading import Event, Lock, Thread

import pytest

import nested_memvid_agent.routine_loop as routine_loop_module
from nested_memvid_agent.routine_limits import (
    MAX_ROUTINE_POLL_INTERVAL_SECONDS,
    MIN_ROUTINE_POLL_INTERVAL_SECONDS,
)
from nested_memvid_agent.routine_loop import RoutineLoop
from nested_memvid_agent.security_boundary import register_secret_value


@dataclass(frozen=True)
class _Result:
    claimed: int


@pytest.mark.parametrize("interval", [float("inf"), float("nan")])
def test_routine_loop_rejects_non_finite_interval(interval: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        RoutineLoop(object(), interval_seconds=interval)


@pytest.mark.parametrize(
    "interval",
    [MIN_ROUTINE_POLL_INTERVAL_SECONDS, MAX_ROUTINE_POLL_INTERVAL_SECONDS],
)
def test_routine_loop_accepts_inclusive_interval_boundaries(interval: float) -> None:
    assert RoutineLoop(object(), interval_seconds=interval).interval_seconds == interval


@pytest.mark.parametrize(
    "interval",
    [
        MIN_ROUTINE_POLL_INTERVAL_SECONDS / 2,
        MAX_ROUTINE_POLL_INTERVAL_SECONDS + 1,
        True,
        "30",
    ],
)
def test_routine_loop_rejects_out_of_range_or_coercible_intervals(
    interval: object,
) -> None:
    with pytest.raises(ValueError, match="routine loop interval_seconds"):
        RoutineLoop(object(), interval_seconds=interval)  # type: ignore[arg-type]


def test_routine_loop_ticks_immediately_and_joins_cleanly() -> None:
    ticked = Event()

    class _Service:
        def tick(self) -> _Result:
            ticked.set()
            return _Result(claimed=1)

    loop = RoutineLoop(_Service(), interval_seconds=60)
    loop.start()

    assert ticked.wait(1)
    assert loop.close(timeout_seconds=1) is True
    status = loop.status()
    assert status.running is False
    assert status.tick_count == 1
    assert status.last_result == {"claimed": 1}


def test_routine_loop_prevents_overlapping_local_ticks() -> None:
    entered = Event()
    release = Event()

    class _Service:
        def tick(self) -> dict[str, int]:
            entered.set()
            assert release.wait(1)
            return {"claimed": 1}

    loop = RoutineLoop(_Service(), interval_seconds=60)
    worker = Thread(target=loop.tick_once)
    worker.start()
    assert entered.wait(1)

    assert loop.tick_once() is None

    release.set()
    worker.join(1)
    assert not worker.is_alive()
    assert loop.status().tick_count == 1


def test_routine_loop_reports_tick_that_outlives_shutdown_bound() -> None:
    entered = Event()
    release = Event()

    class _Service:
        def tick(self) -> dict[str, int]:
            entered.set()
            release.wait(timeout=2.0)
            return {"claimed": 0}

    loop = RoutineLoop(_Service(), interval_seconds=60)
    loop.start()
    assert entered.wait(timeout=1.0)

    assert loop.close(timeout_seconds=0.01) is False
    release.set()
    assert loop.close(timeout_seconds=1.0) is True


def test_routine_loop_serializes_concurrent_starts_before_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_factory_entered = Event()
    second_factory_entered = Event()
    tick_entered = Event()
    release_tick = Event()
    created_lock = Lock()
    created_threads: list[Thread] = []
    native_thread = Thread

    class _Service:
        def tick(self) -> dict[str, int]:
            tick_entered.set()
            release_tick.wait(timeout=2.0)
            return {"claimed": 0}

    def gated_thread_factory(*args: object, **kwargs: object) -> Thread:
        thread = native_thread(*args, **kwargs)  # type: ignore[arg-type]
        with created_lock:
            created_threads.append(thread)
            index = len(created_threads)
        if index == 1:
            first_factory_entered.set()
            second_factory_entered.wait(timeout=0.25)
        else:
            second_factory_entered.set()
        return thread

    monkeypatch.setattr(routine_loop_module, "Thread", gated_thread_factory)
    loop = RoutineLoop(_Service(), interval_seconds=60)
    first_starter = native_thread(target=loop.start)
    second_starter = native_thread(target=loop.start)
    first_starter.start()
    assert first_factory_entered.wait(timeout=1.0)
    second_starter.start()
    first_starter.join(timeout=1.0)
    second_starter.join(timeout=1.0)

    try:
        assert not first_starter.is_alive()
        assert not second_starter.is_alive()
        assert len(created_threads) == 1
        assert tick_entered.wait(timeout=1.0)
        assert loop.close(timeout_seconds=0.01) is False
    finally:
        release_tick.set()
        loop.close(timeout_seconds=1.0)
        for thread in created_threads:
            thread.join(timeout=1.0)

    assert loop.status().tick_in_progress is False


def test_routine_loop_redacts_tick_errors_and_keeps_running() -> None:
    secret = "routine-loop-opaque-secret-97143"
    register_secret_value(secret)

    class _Service:
        def tick(self) -> None:
            raise RuntimeError(f"provider returned {secret}")

    loop = RoutineLoop(_Service(), interval_seconds=60)

    assert loop.tick_once() is None
    status = loop.status()
    assert status.tick_count == 1
    assert status.last_error is not None
    assert secret not in status.last_error
    assert "<redacted>" in status.last_error
