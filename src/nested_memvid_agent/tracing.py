from __future__ import annotations

from dataclasses import asdict
from types import TracebackType
from typing import Any, Literal
from uuid import uuid4

from .event_bus import RunEventBus
from .state_store import AgentStateStore, TraceSpanRecord


class SpanRecorder:
    """Durable span recorder backed by SQLite state and the run event stream."""

    def __init__(self, *, state: AgentStateStore, events: RunEventBus) -> None:
        self.state = state
        self.events = events

    def start(
        self,
        *,
        run_id: str,
        span_type: str,
        name: str,
        parent_span_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TraceSpan:
        return TraceSpan(
            recorder=self,
            run_id=run_id,
            span_type=span_type,
            name=name,
            parent_span_id=parent_span_id,
            metadata=metadata or {},
        )

    def list_run_spans(self, run_id: str) -> list[dict[str, Any]]:
        return [_span_payload(span) for span in self.state.list_trace_spans(run_id)]


class TraceSpan:
    def __init__(
        self,
        *,
        recorder: SpanRecorder,
        run_id: str,
        span_type: str,
        name: str,
        parent_span_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        self.recorder = recorder
        self.run_id = run_id
        self.span_type = span_type
        self.name = name
        self.parent_span_id = parent_span_id
        self.metadata = metadata
        self.span_id = f"span_{uuid4().hex}"
        self.record: TraceSpanRecord | None = None
        self._status: str | None = None
        self._output: dict[str, Any] | None = None
        self._error: str | None = None

    def __enter__(self) -> TraceSpan:
        self.record = self.recorder.state.create_trace_span(
            span_id=self.span_id,
            run_id=self.run_id,
            parent_span_id=self.parent_span_id,
            span_type=self.span_type,
            name=self.name,
            metadata=self.metadata,
        )
        self.recorder.events.publish(self.run_id, "span.started", _span_payload(self.record))
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        del tb
        status = "failed" if exc is not None else self._status or "completed"
        error = f"{exc_type.__name__}: {exc}" if exc_type is not None and exc is not None else self._error
        self.finish(status=status, output=self._output, error=error)
        return False

    def set_result(
        self,
        *,
        status: str = "completed",
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self._status = status
        self._output = output
        self._error = error

    def finish(
        self,
        *,
        status: str = "completed",
        output: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> TraceSpanRecord:
        record = self.recorder.state.finish_trace_span(
            self.span_id,
            status=status,
            output=output,
            error=error,
        )
        self.record = record
        self.recorder.events.publish(self.run_id, "span.finished", _span_payload(record))
        return record


def _span_payload(span: TraceSpanRecord) -> dict[str, Any]:
    payload = asdict(span)
    payload["metadata"] = dict(span.metadata or {})
    payload["output"] = dict(span.output or {})
    return payload
