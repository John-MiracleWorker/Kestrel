from __future__ import annotations

import queue
from dataclasses import asdict
from typing import Any, cast

from .event_log import JsonlEventLog
from .server_support import bounded_limit


def register_observability_routes(
    app: Any,
    *,
    active_config: Any,
    http_exception: Any,
    streaming_response: Any,
    state: Any,
    events: Any,
    runs: Any,
) -> None:
    @app.get("/api/runs/{run_id}/events")  # type: ignore[untyped-decorator]
    def run_events(run_id: str, after_id: int = 0) -> Any:
        try:
            state.get_run(run_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

        def stream() -> Any:
            subscriber = events.subscribe(run_id, after_id=after_id)
            try:
                while True:
                    try:
                        event = subscriber.get(timeout=15)
                        yield event.to_sse()
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                events.unsubscribe(run_id, subscriber)

        return streaming_response(stream(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/trace")  # type: ignore[untyped-decorator]
    def run_trace(run_id: str, limit: int = 1000) -> dict[str, object]:
        try:
            return cast(
                dict[str, object],
                runs.run_trace(run_id, limit=bounded_limit(limit, default=1000, maximum=5000)),
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    @app.get("/api/logs")  # type: ignore[untyped-decorator]
    def logs(limit: int = 100) -> list[dict[str, object]]:
        event_log = JsonlEventLog(active_config.log_dir / "events.jsonl")
        return [
            asdict(event)
            for event in event_log.tail(limit=bounded_limit(limit, default=100, maximum=500))
        ]
