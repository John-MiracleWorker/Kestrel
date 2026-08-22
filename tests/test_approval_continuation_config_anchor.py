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

import json
from pathlib import Path
from time import monotonic, sleep
from types import SimpleNamespace
from typing import Any

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.event_log import JsonlEventLog
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import AgentTurnResult, LLMResponse, ToolCall
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


class _ReviewerDouble:
    """Minimal separate-reviewer-agent double with a close() observable."""

    def __init__(self, llm: Any, config: AgentConfig) -> None:
        self.llm = llm
        self.config = config
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ExecutorDouble:
    """Minimal executor-agent double; its LLM is never invoked for review."""

    def close(self) -> None:
        pass


def _passing_review_response() -> LLMResponse:
    return LLMResponse(
        content=json.dumps(
            {
                "verdict": "pass",
                "summary": "objective addressed",
                "criteria": [
                    {
                        "criterion": (
                            "User objective is addressed or explicitly blocked with "
                            "next steps."
                        ),
                        "status": "satisfied",
                        "evidence_refs": ["assistant_response"],
                        "reason": "the executor produced a concrete answer",
                    }
                ],
                "remaining_risks": [],
                "confidence": 0.9,
            }
        )
    )


def test_approval_continuation_routes_reviewer_through_resolver(tmp_path: Path) -> None:
    """Approval continuations must route the reviewer through the resolver.

    ``_finish_agent_turn`` (the approval-continuation turn finisher) must go
    through the same reviewer-authority resolver and separate reviewer-agent
    builder as primary turns: the continuation review carries a durable
    ``review_authority`` label, and the separate reviewer agent is closed
    (never leaked).
    """
    manager = _manager(tmp_path)
    state = manager.state

    state.create_run(
        run_id="run-1",
        message="Solve it.",
        session_id="s",
        workspace=str(tmp_path),
        provider="ollama",
        model="exec-model",
    )
    state.create_task_node(
        task_id="root",
        run_id="run-1",
        title="Plan",
        goal="Solve it.",
        profile="planner",
        approved=True,
        required_tools=(),
        risk="low",
        acceptance_criteria=(
            "User objective is addressed or explicitly blocked with next steps.",
        ),
    )

    config = AgentConfig(
        name="Kestrel",
        provider="ollama",
        model="exec-model",
        backend="memory",
        enable_semantic_orchestration=True,
    )

    built_reviewer: list[_ReviewerDouble] = []

    def resolver(ctx: Any) -> Any:
        del ctx
        return SimpleNamespace(review_authority="independent_target")

    def build_reviewer(ctx: Any, assignment: Any) -> _ReviewerDouble:
        del ctx, assignment
        agent = _ReviewerDouble(
            llm=MockLLMProvider(canned=[_passing_review_response()]),
            config=config,
        )
        built_reviewer.append(agent)
        return agent

    manager._review_authority_resolver = lambda: resolver  # type: ignore[method-assign]
    manager._build_reviewer_agent = lambda: build_reviewer  # type: ignore[method-assign]

    executor = _ExecutorDouble()
    result = AgentTurnResult(
        session_id="s",
        user_message="Solve it.",
        assistant_message="The executor produced a concrete answer.",
        tool_executions=(),
        context_chars=0,
        memory_writes=(),
        stop_reason="complete",
    )

    manager._finish_agent_turn("run-1", config, executor, result, lease_generation=0)  # type: ignore[arg-type]

    # The continuation review must carry the durable independent authority label.
    root = next(
        task
        for task in state.list_task_nodes("run-1")
        if task.parent_id is None and task.profile == "planner"
    )
    assert root.result is not None
    review = root.result["orchestration_review"]
    assert review["review_authority"] == "independent_target"
    assert review["review_fallback"] is False
    assert review["off_mode_abstained"] is False

    # The separate reviewer agent must have been built and closed.
    assert built_reviewer, "a separate reviewer agent should have been built"
    assert all(agent.closed for agent in built_reviewer), (
        "the separate reviewer agent must be closed"
    )
