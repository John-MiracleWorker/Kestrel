from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from threading import Event, Lock, Thread
from time import monotonic
from typing import Any

from .event_log import redact_secrets
from .routine_limits import validate_routine_poll_interval


@dataclass(frozen=True)
class RoutineLoopStatus:
    running: bool
    tick_count: int
    last_result: dict[str, Any] | None
    last_error: str | None
    tick_in_progress: bool
    current_tick_age_seconds: float | None
    last_started_at: str | None
    last_finished_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RoutineLoop:
    """A bounded, stoppable polling loop around ``RoutineService.tick``.

    Durable occurrence claims remain the cross-process concurrency boundary.
    The local lock only prevents overlapping ticks inside this process.
    """

    def __init__(self, service: Any, *, interval_seconds: float) -> None:
        self.service = service
        self.interval_seconds = validate_routine_poll_interval(
            interval_seconds,
            field_name="routine loop interval_seconds",
        )
        self._stop = Event()
        self._lifecycle_lock = Lock()
        self._tick_lock = Lock()
        self._status_lock = Lock()
        self._thread: Thread | None = None
        self._tick_count = 0
        self._last_result: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._tick_in_progress = False
        self._tick_started_monotonic: float | None = None
        self._last_started_at: str | None = None
        self._last_finished_at: str | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = Thread(
                target=self._run,
                name="kestrel-proactive-routines",
                daemon=True,
            )
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._stop.set()
                raise

    def close(self, *, timeout_seconds: float = 5.0) -> bool:
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
            if thread is None:
                return True
            thread.join(timeout=max(0.0, timeout_seconds))
            stopped = not thread.is_alive()
            if stopped and self._thread is thread:
                self._thread = None
            return stopped

    def tick_once(self) -> dict[str, Any] | None:
        if not self._tick_lock.acquire(blocking=False):
            return None
        with self._status_lock:
            self._tick_in_progress = True
            self._tick_started_monotonic = monotonic()
            self._last_started_at = datetime.now(UTC).isoformat()
        try:
            result = self.service.tick()
            payload = _public_tick_result(result)
            with self._status_lock:
                self._tick_count += 1
                self._last_result = payload
                self._last_error = None
            return payload
        except Exception as exc:  # noqa: BLE001 - loop records and survives one failed tick
            safe_error = str(redact_secrets(f"{type(exc).__name__}: {exc}"))
            with self._status_lock:
                self._tick_count += 1
                self._last_error = safe_error
            return None
        finally:
            with self._status_lock:
                self._tick_in_progress = False
                self._tick_started_monotonic = None
                self._last_finished_at = datetime.now(UTC).isoformat()
            self._tick_lock.release()

    def status(self) -> RoutineLoopStatus:
        with self._status_lock:
            thread = self._thread
            age = (
                max(0.0, monotonic() - self._tick_started_monotonic)
                if self._tick_in_progress and self._tick_started_monotonic is not None
                else None
            )
            return RoutineLoopStatus(
                running=bool(thread and thread.is_alive() and not self._stop.is_set()),
                tick_count=self._tick_count,
                last_result=self._last_result,
                last_error=self._last_error,
                tick_in_progress=self._tick_in_progress,
                current_tick_age_seconds=(round(age, 3) if age is not None else None),
                last_started_at=self._last_started_at,
                last_finished_at=self._last_finished_at,
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick_once()
            if self._stop.wait(self.interval_seconds):
                return


def _public_tick_result(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_dict"):
        payload = result.to_dict()
    elif hasattr(result, "to_public_dict"):
        payload = result.to_public_dict()
    elif hasattr(result, "__dataclass_fields__"):
        payload = asdict(result)
    elif isinstance(result, dict):
        payload = dict(result)
    else:
        payload = {"result": str(result)}
    safe = redact_secrets(payload)
    return safe if isinstance(safe, dict) else {"result": str(safe)}
