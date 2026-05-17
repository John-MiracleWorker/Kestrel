from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nested_memvid_agent.state_store import AgentStateStore


def test_state_store_rejects_unknown_update_columns(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_1",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )

    with pytest.raises(ValueError, match="Unknown runs column"):
        state.update_run("run_1", **{"status = 'completed' --": "completed"})


def test_state_store_rejects_unknown_subagent_update_columns(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_1",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    state.create_subagent_run(
        subagent_id="subagent_1",
        run_id="run_1",
        profile="research",
        goal="inspect the task",
    )

    with pytest.raises(ValueError, match="Unknown subagent_runs column"):
        state.update_subagent_run("subagent_1", **{"status = 'completed' --": "completed"})


def test_state_store_wal_allows_concurrent_reads_and_writes(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_1",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )

    def worker(index: int) -> None:
        for attempt in range(40):
            state.update_run("run_1", assistant_message=f"{index}:{attempt}")
            state.get_run("run_1")
            state.list_runs()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(worker, range(5)))

    assert state.get_run("run_1").assistant_message
