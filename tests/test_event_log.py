from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.event_log import AgentEvent, JsonlEventLog


def test_event_log_redacts_common_secret_shapes(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")

    log.append(
        AgentEvent(
            type="provider.trace",
            payload={
                "openai": "sk-thisIsAFakeOpenAIKey123456",
                "auth": "Bearer abcdefghijklmnopqrstuvwxyz",
                "env": "PASSWORD=super-secret",
            },
        )
    )

    raw = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "sk-thisIsAFakeOpenAIKey123456" not in raw
    assert "abcdefghijklmnopqrstuvwxyz" not in raw
    assert "super-secret" not in raw
    assert "<redacted>" in raw
