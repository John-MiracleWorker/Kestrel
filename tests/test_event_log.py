from __future__ import annotations

import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import nested_memvid_agent.event_log as event_log_module
from nested_memvid_agent.event_log import AgentEvent, JsonlEventLog


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_event_log_creates_owner_only_directory_and_file(tmp_path: Path) -> None:
    path = tmp_path / "created-logs" / "events.jsonl"
    log = JsonlEventLog(path)

    log.append(AgentEvent(id="evt_mode", type="mode.test", payload={"ok": True}))

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [event.id for event in log.tail()] == ["evt_mode"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_event_log_repairs_file_without_changing_existing_custom_directory(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "custom-logs"
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    path = directory / "events.jsonl"
    path.write_text(
        json.dumps(
            {
                "id": "evt_existing",
                "type": "existing",
                "payload": {},
                "created_at": "2030-01-01T00:00:00+00:00",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o644)

    log = JsonlEventLog(path)
    log.append(AgentEvent(id="evt_appended", type="appended", payload={}))

    assert stat.S_IMODE(directory.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [event.id for event in log.tail()] == ["evt_existing", "evt_appended"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink modes differ on Windows")
def test_event_log_rejects_symlinked_directory_without_mutating_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside-logs"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    linked_directory = tmp_path / "linked-logs"
    linked_directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="event log directory must not be a symbolic link"):
        JsonlEventLog(linked_directory / "events.jsonl")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not (target / "events.jsonl").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink modes differ on Windows")
def test_event_log_rejects_symlinked_file_without_mutating_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside-events.jsonl"
    target.write_text("outside\n", encoding="utf-8")
    os.chmod(target, 0o644)
    directory = tmp_path / "logs"
    directory.mkdir()
    path = directory / "events.jsonl"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="event log must not be a symbolic link"):
        JsonlEventLog(path)

    assert target.read_text(encoding="utf-8") == "outside\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link modes differ on Windows")
def test_event_log_rejects_hard_link_without_mutating_target(tmp_path: Path) -> None:
    target = tmp_path / "outside-hard-link.jsonl"
    target.write_text("outside\n", encoding="utf-8")
    os.chmod(target, 0o644)
    directory = tmp_path / "hard-linked-logs"
    directory.mkdir()
    path = directory / "events.jsonl"
    os.link(target, path)

    with pytest.raises(ValueError, match="event log must not be hard-linked"):
        JsonlEventLog(path)

    assert target.read_text(encoding="utf-8") == "outside\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX file types differ on Windows")
def test_event_log_rejects_nonregular_path_without_chmod(tmp_path: Path) -> None:
    directory = tmp_path / "nonregular-logs"
    directory.mkdir()
    path = directory / "events.jsonl"
    path.mkdir(mode=0o755)
    os.chmod(path, 0o755)

    with pytest.raises(ValueError, match="event log must be a regular file"):
        JsonlEventLog(path)

    assert stat.S_IMODE(path.stat().st_mode) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX append locking differs on Windows")
def test_event_log_concurrent_appends_keep_complete_private_json_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / "concurrent-logs" / "events.jsonl"

    def append(index: int) -> None:
        JsonlEventLog(path).append(
            AgentEvent(
                id=f"evt_{index:03d}",
                type="concurrent",
                payload={"index": index, "text": "x" * 1024},
                created_at="2030-01-01T00:00:00+00:00",
            )
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(100)))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    assert {json.loads(line)["id"] for line in lines} == {
        f"evt_{index:03d}" for index in range(100)
    }
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_event_log_tail_reads_only_a_bounded_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bounded-tail" / "events.jsonl"
    path.parent.mkdir()
    trailing = [
        json.dumps(
            {
                "id": f"evt_tail_{index}",
                "type": "tail.test",
                "payload": {"index": index},
                "created_at": "2030-01-01T00:00:00+00:00",
            }
        )
        for index in range(3)
    ]
    path.write_bytes((b"x" * (2 * 1024 * 1024)) + b"\n" + ("\n".join(trailing) + "\n").encode())
    requested_reads: list[int] = []
    original_read = event_log_module._read_tail_chunk

    def track_read(handle: object, offset: int, size: int) -> bytes:
        requested_reads.append(size)
        return original_read(handle, offset, size)

    monkeypatch.setattr(event_log_module, "_EVENT_TAIL_MAX_BYTES", 4096)
    monkeypatch.setattr(event_log_module, "_read_tail_chunk", track_read)

    events = JsonlEventLog(path).tail(limit=2)

    assert [event.id for event in events] == ["evt_tail_1", "evt_tail_2"]
    assert requested_reads
    assert sum(requested_reads) <= 4096
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


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
