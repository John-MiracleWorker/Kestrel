from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


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


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|github_pat)_[A-Za-z0-9_]{12,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{12,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"(?i)(api[_-]?key|token|password|authorization)(\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)


def redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: redact_secrets(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(item) for item in value)
    if isinstance(value, str):
        redacted = value
        for pattern in _SECRET_PATTERNS:
            redacted = pattern.sub(_redacted_match, redacted)
        return redacted
    return value


def _redacted_match(match: re.Match[str]) -> str:
    if match.lastindex and match.lastindex >= 3:
        return f"{match.group(1)}{match.group(2)}<redacted>"
    if match.group(0).lower().startswith("bearer "):
        return "Bearer <redacted>"
    return "<redacted>"
