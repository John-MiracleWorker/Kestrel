from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from nested_memvid_agent.event_log import AgentEvent, JsonlEventLog
from nested_memvid_agent.server_observability_routes import register_observability_routes


class _FakeConfig:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.provider = "mock"
        self.model = "mock"


class _FakeState:
    def __init__(self) -> None:
        self.run_ids = {"run_ok"}

    def get_run(self, run_id: str) -> dict[str, object]:
        if run_id not in self.run_ids:
            raise KeyError(run_id)
        return {"run_id": run_id}


class _FakeEvents:
    def __init__(self) -> None:
        self.subscribed: list[tuple[str, int]] = []

    def subscribe(self, run_id: str, after_id: int = 0) -> Any:
        self.subscribed.append((run_id, after_id))
        raise AssertionError("stream should not subscribe until the response body is consumed")

    def unsubscribe(self, run_id: str, subscriber: Any) -> None:
        del run_id, subscriber


class _FakeSSEEvent:
    def to_sse(self) -> str:
        return 'id: 1\nevent: run.step\ndata: {"ok": true}\n\n'


class _FakeSubscriber:
    def __init__(self) -> None:
        self.timeouts: list[int] = []

    def get(self, timeout: int) -> _FakeSSEEvent:
        self.timeouts.append(timeout)
        return _FakeSSEEvent()


class _FakeStreamingEvents:
    def __init__(self) -> None:
        self.subscriber = _FakeSubscriber()
        self.subscribed: list[tuple[str, int]] = []
        self.unsubscribed: list[tuple[str, _FakeSubscriber]] = []

    def subscribe(self, run_id: str, after_id: int = 0) -> _FakeSubscriber:
        self.subscribed.append((run_id, after_id))
        return self.subscriber

    def unsubscribe(self, run_id: str, subscriber: _FakeSubscriber) -> None:
        self.unsubscribed.append((run_id, subscriber))


class _FakeRuns:
    def __init__(self) -> None:
        self.trace_limits: list[int] = []

    def run_trace(self, run_id: str, limit: int = 1000) -> dict[str, object]:
        if run_id != "run_ok":
            raise KeyError(run_id)
        self.trace_limits.append(limit)
        return {"run": {"run_id": run_id}, "summary": {"limit": limit}, "traces": {}}


def test_observability_routes_tail_logs_and_trace_with_bounded_limits(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    log.append(AgentEvent(type="turn.start", payload={"message": "hello"}))
    log.append(
        AgentEvent(
            type="memory.write",
            payload={"text": "Bearer raw-secret-value-12345", "token": "tiny-secret"},
        )
    )

    app = FastAPI()
    state = _FakeState()
    events = _FakeEvents()
    runs = _FakeRuns()
    register_observability_routes(
        app,
        active_config=_FakeConfig(tmp_path / "logs"),
        http_exception=HTTPException,
        streaming_response=lambda *args, **kwargs: None,
        state=state,
        events=events,
        runs=runs,
    )
    client = TestClient(app)

    trace = client.get("/api/runs/run_ok/trace", params={"limit": 99999})
    logs = client.get("/api/logs", params={"limit": 1})
    metrics = client.get("/api/metrics")
    diagnostics = client.get("/api/diagnostics", params={"log_limit": 1})
    missing_trace = client.get("/api/runs/missing/trace")
    missing_events = client.get("/api/runs/missing/events")

    assert trace.status_code == 200
    assert trace.json()["summary"]["limit"] == 5000
    assert runs.trace_limits == [5000]
    assert logs.status_code == 200
    assert [event["type"] for event in logs.json()] == ["memory.write"]
    assert "raw-secret-value" not in logs.text
    assert "tiny-secret" not in logs.text
    assert "<redacted>" in logs.text
    assert metrics.status_code == 200
    assert metrics.json()["schema"] == "kestrel.operational_metrics.v1"
    prometheus = client.get("/metrics")
    assert prometheus.status_code == 200
    assert prometheus.headers["content-type"].startswith("text/plain")
    assert "kestrel_up 1" in prometheus.text
    assert "kestrel_runs" in prometheus.text
    assert diagnostics.status_code == 200
    assert diagnostics.json()["schema"] == "kestrel.diagnostics.v1"
    assert "raw-secret-value" not in diagnostics.text
    assert missing_trace.status_code == 404
    assert missing_events.status_code == 404
    assert events.subscribed == []


def test_observability_routes_expose_streaming_events_response(tmp_path: Path) -> None:
    app = FastAPI()
    captured: dict[str, object] = {}
    events = _FakeStreamingEvents()

    class _StreamingResponse:
        def __init__(self, body: Any, media_type: str) -> None:
            captured["body"] = body
            captured["media_type"] = media_type

    register_observability_routes(
        app,
        active_config=_FakeConfig(tmp_path / "logs"),
        http_exception=HTTPException,
        streaming_response=_StreamingResponse,
        state=_FakeState(),
        events=events,
        runs=_FakeRuns(),
    )

    route = next(route for route in app.routes if getattr(route, "path", "") == "/api/runs/{run_id}/events")
    response = route.endpoint("run_ok", after_id=7)
    stream = captured["body"]
    first_chunk = next(stream)  # type: ignore[arg-type]
    stream.close()  # type: ignore[attr-defined]

    assert response is not None
    assert captured["media_type"] == "text/event-stream"
    assert first_chunk == 'id: 1\nevent: run.step\ndata: {"ok": true}\n\n'
    assert events.subscribed == [("run_ok", 7)]
    assert events.subscriber.timeouts == [15]
    assert events.unsubscribed == [("run_ok", events.subscriber)]
