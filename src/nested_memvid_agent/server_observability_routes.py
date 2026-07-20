from __future__ import annotations

import queue
from dataclasses import asdict
from importlib import import_module
from typing import Any, cast

from .event_log import JsonlEventLog
from .operational_metrics import operational_snapshot, prometheus_snapshot
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
    routine_loop: Any | None = None,
) -> None:
    plain_text_response = import_module("fastapi.responses").PlainTextResponse

    def config() -> Any:
        return active_config() if callable(active_config) else active_config

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
        event_log = JsonlEventLog(config().log_dir / "events.jsonl")
        return [
            asdict(event)
            for event in event_log.tail(limit=bounded_limit(limit, default=100, maximum=500))
        ]

    @app.get("/api/metrics")  # type: ignore[untyped-decorator]
    def metrics() -> dict[str, object]:
        return cast(
            dict[str, object],
            operational_snapshot(
                config=config(),
                state=state,
                runs=runs,
                routine_loop=routine_loop,
            ),
        )

    @app.get("/metrics")  # type: ignore[untyped-decorator]
    def prometheus_metrics() -> Any:
        snapshot = operational_snapshot(
            config=config(),
            state=state,
            runs=runs,
            routine_loop=routine_loop,
        )
        return plain_text_response(
            prometheus_snapshot(snapshot),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/api/diagnostics")  # type: ignore[untyped-decorator]
    def diagnostics(log_limit: int = 50) -> dict[str, object]:
        event_log = JsonlEventLog(config().log_dir / "events.jsonl")
        return {
            "schema": "kestrel.diagnostics.v1",
            "metrics": operational_snapshot(
                config=config(),
                state=state,
                runs=runs,
                routine_loop=routine_loop,
            ),
            "startup_recovery": getattr(runs, "startup_recovery", {}),
            "logs": [
                asdict(event)
                for event in event_log.tail(
                    limit=bounded_limit(log_limit, default=50, maximum=100)
                )
            ],
        }
