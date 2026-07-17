from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .security_boundary import redact_secrets as redact_secrets


@dataclass(frozen=True)
class AgentEvent:
    type: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: f"evt_{uuid4().hex}")
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


class JsonlEventLog:
    """Raw audit log. This is intentionally not a retrieval database."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: AgentEvent) -> None:
        event = AgentEvent(
            type=event.type,
            payload=redact_secrets(event.payload),
            id=event.id,
            created_at=event.created_at,
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")

    def tail(self, limit: int = 50) -> list[AgentEvent]:
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        events: list[AgentEvent] = []
        for line in lines:
            raw = json.loads(line)
            events.append(
                AgentEvent(
                    id=raw["id"],
                    type=raw["type"],
                    payload=raw["payload"],
                    created_at=raw["created_at"],
                )
            )
        return events
