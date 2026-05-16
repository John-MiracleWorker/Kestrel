from __future__ import annotations

import json
import queue
from collections import defaultdict
from dataclasses import dataclass
from threading import Lock
from typing import Any

from .event_log import redact_secrets
from .state_store import AgentStateStore


@dataclass(frozen=True)
class RunEvent:
    id: int
    run_id: str
    type: str
    payload: dict[str, Any]

    def to_sse(self) -> str:
        data = json.dumps(
            {
                "id": self.id,
                "run_id": self.run_id,
                "type": self.type,
                "payload": self.payload,
            }
        )
        return f"id: {self.id}\nevent: {self.type}\ndata: {data}\n\n"


class RunEventBus:
    """Small in-process fan-out bus backed by the persistent run step log."""

    def __init__(self, state: AgentStateStore) -> None:
        self.state = state
        self._lock = Lock()
        self._subscribers: dict[str, list[queue.Queue[RunEvent]]] = defaultdict(list)

    def publish(self, run_id: str, type: str, payload: dict[str, Any]) -> RunEvent:
        safe_payload = redact_secrets(payload)
        event_id = self.state.append_run_step(run_id, type, safe_payload)
        event = RunEvent(id=event_id, run_id=run_id, type=type, payload=safe_payload)
        with self._lock:
            subscribers = list(self._subscribers.get(run_id, []))
        for subscriber in subscribers:
            subscriber.put(event)
        return event

    def subscribe(self, run_id: str, after_id: int = 0) -> queue.Queue[RunEvent]:
        subscriber: queue.Queue[RunEvent] = queue.Queue()
        for row in self.state.list_run_steps(run_id, after_id=after_id):
            subscriber.put(
                RunEvent(
                    id=int(row["id"]),
                    run_id=str(row["run_id"]),
                    type=str(row["type"]),
                    payload=dict(row["payload"]),
                )
            )
        with self._lock:
            self._subscribers[run_id].append(subscriber)
        return subscriber

    def unsubscribe(self, run_id: str, subscriber: queue.Queue[RunEvent]) -> None:
        with self._lock:
            subscribers = self._subscribers.get(run_id)
            if not subscribers:
                return
            if subscriber in subscribers:
                subscribers.remove(subscriber)
            if not subscribers:
                self._subscribers.pop(run_id, None)
