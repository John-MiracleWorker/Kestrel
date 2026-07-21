from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from nested_memvid_agent.state_store import AgentStateStore
from scripts import eval_memory_router


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_router_replay_uses_snapshot_without_mutating_source(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    state_path = tmp_path / "agent.db"
    AgentStateStore(state_path)
    before_digest = _sha256(state_path)
    before_mtime = state_path.stat().st_mtime_ns
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval_memory_router.py", "--state-db", str(state_path), "--json"],
    )

    assert eval_memory_router.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["evaluation_source"]["mode"] == "consistent_readonly_sqlite_backup"
    assert _sha256(state_path) == before_digest
    assert state_path.stat().st_mtime_ns == before_mtime


def test_router_replay_refuses_missing_source_without_creating_it(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    state_path = tmp_path / "missing.db"
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval_memory_router.py", "--state-db", str(state_path), "--json"],
    )

    assert eval_memory_router.main() == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert payload["stage"] == "state_snapshot"
    assert not state_path.exists()


def test_router_replay_refuses_symlink_source(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    target = tmp_path / "target.db"
    AgentStateStore(target)
    state_path = tmp_path / "linked.db"
    state_path.symlink_to(target)
    monkeypatch.setattr(
        sys,
        "argv",
        ["eval_memory_router.py", "--state-db", str(state_path), "--json"],
    )

    assert eval_memory_router.main() == 2

    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is False
    assert "symlink" in payload["error"]
