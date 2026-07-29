"""Regression tests for run-config anchoring in the approval-continuation path.

A high-risk tool (``shell.run``) requested under ``allow_shell=True`` must remain
executable when the approval is decided by a *separate* process/manager whose own
volatile config has ``allow_shell=False``. The durable run config snapshot -- not
the approving process's CLI flags -- anchors volatile gates during continuation.

Covers two gates that previously failed closed in this scenario:
  * GATE 1: ``approval_invalid_before_continuation`` / ``capability_not_enabled``
  * GATE 2: ``review_evidence_failed`` (approved tool re-gated as ``tool_disabled``)
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic, sleep

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.event_log import JsonlEventLog
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import LLMResponse, ToolCall
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


def _manager(tmp_path: Path) -> RunManager:
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        workspace=tmp_path,
    )
    state = AgentStateStore(config.state_path)
    events = RunEventBus(state)
    mcp = MCPManager(state)
    skills = SkillManager(config.skills_dir, state)
    return RunManager(config=config, state=state, events=events, mcp=mcp, skills=skills)


def _wait(manager: RunManager, run_id: str, statuses: set[str]) -> dict:
    deadline = monotonic() + 30
    while True:
        run = manager.state.get_run(run_id)
        if run.status in statuses:
            return {
                "status": run.status,
                "stop_reason": run.stop_reason,
                "error": run.error,
            }
        if monotonic() > deadline:
            raise AssertionError(
                {"run_id": run_id, "status": run.status, "stop_reason": run.stop_reason}
            )
        sleep(0.05)


def test_approval_continuation_anchors_to_run_config(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config = AgentConfig(**{**manager.config.__dict__, "allow_shell": True})
    scripted = [
        LLMResponse(
            content="I will run the command.",
            tool_calls=(ToolCall(name="shell.run", arguments={"command": ["echo", "ok"]}),),
        ),
        LLMResponse(content="Command executed."),
    ]

    def build_scripted_agent(config: AgentConfig) -> NestedMV2Agent:
        response = scripted.pop(0)
        return NestedMV2Agent(
            AgentDependencies(
                memory=build_memory_system(config.backend, config.memory_dir),
                llm=MockLLMProvider(canned=[response]),
                tools=manager.build_registry(),
                config=config,
                event_log=JsonlEventLog(config.log_dir / "events.jsonl"),
            )
        )

    manager._build_agent = build_scripted_agent  # type: ignore[method-assign]

    run = manager.create_run(message="Run the echo command please")
    blocked = _wait(manager, run.run_id, {"blocked", "failed"})
    assert blocked["status"] == "blocked", blocked
    assert blocked["stop_reason"] == "approval_required"

    approvals = manager.state.list_approvals(status="pending")
    assert len(approvals) == 1
    approval = approvals[0]

    # Simulate a separate `approve` CLI invocation without --allow-shell.
    manager.config = AgentConfig(**{**manager.config.__dict__, "allow_shell": False})
    manager.decide_approval(approval["approval_id"], approved=True, arguments=approval["arguments"])
    final = _wait(manager, run.run_id, {"completed", "failed"})
    assert final["status"] == "completed", final
    assert final["stop_reason"] == "complete", final
