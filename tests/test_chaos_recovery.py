from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


def test_sigkill_owner_is_reconciled_without_replaying_side_effects(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    code = """
import os, sys, time
from pathlib import Path
from nested_memvid_agent.state_store import AgentStateStore
state = AgentStateStore(Path(sys.argv[1]))
state.create_run(run_id='crashed_run', message='crash me', session_id='chaos', workspace='.', model='mock')
owner = f'manager_{os.getpid()}_chaos'
lease = state.acquire_run_lease('crashed_run', owner=owner, ttl_seconds=300)
assert lease is not None
state.transition_run('crashed_run', 'running', lease_owner=owner, lease_generation=lease.lease_generation)
print('READY', flush=True)
time.sleep(300)
"""
    child = subprocess.Popen(  # noqa: S603 - deterministic local child process
        [sys.executable, "-c", code, str(state_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "READY"
        child.kill()
        assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)

    config = AgentConfig(
        state_path=state_path,
        memory_dir=tmp_path / "memory",
        workspace=tmp_path,
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
    )
    state = AgentStateStore(state_path)
    manager = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
    )

    recovered = state.get_run("crashed_run")
    assert recovered.status == "failed"
    assert recovered.stop_reason == "interrupted_by_restart"
    assert recovered.lease_owner is None
    assert manager.startup_recovery["failed"] == ["crashed_run"]
