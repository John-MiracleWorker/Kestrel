from __future__ import annotations

import json
from pathlib import Path

from nested_memvid_agent.event_log import AgentEvent, JsonlEventLog


def test_event_log_redacts_common_secret_shapes(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")

    log.append(
        AgentEvent(
            type="provider.trace",
            payload={
                "openai": "api_key=unit_test_value_12345",
                "auth": "Bearer abcdefghijklmnopqrstuvwxyz",
                "env": "PASSWORD=super-secret",
                "token": "tiny",
                "token_configured": False,
            },
        )
    )

    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "unit_test_value_12345" not in raw
    assert "abcdefghijklmnopqrstuvwxyz" not in raw
    assert "super-secret" not in raw
    payload = json.loads(raw)["payload"]
    assert payload["token"] == "<redacted>"
    assert payload["token_configured"] is False
    assert "<redacted>" in raw
