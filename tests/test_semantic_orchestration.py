from __future__ import annotations

import argparse
import json
from dataclasses import replace
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.cli import _add_agent_args, _agent_config_from_args
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.event_log import JsonlEventLog
from nested_memvid_agent.graph_runtime import (
    _deterministic_criterion_assessment,
    _parse_provider_review,
)
from nested_memvid_agent.llm.base import ProviderCapabilities
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.run_manager import RunManager, _validate_task_completion
from nested_memvid_agent.runtime_models import (
    AgentTurnResult,
    ChatMessage,
    LLMOptions,
    LLMResponse,
    ToolCall,
    ToolExecution,
    ToolSpec,
)
from nested_memvid_agent.runtime_settings import (
    RuntimeSettings,
    apply_runtime_settings,
    merge_runtime_settings,
    runtime_settings_snapshot,
)
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord


class _StructuredMockProvider(MockLLMProvider):
    def __init__(self, canned: list[LLMResponse] | None = None) -> None:
        super().__init__(canned=canned)
        self.requests: list[list[ChatMessage]] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        return replace(super().capabilities, name="structured-test")

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        self.requests.append(list(messages))
        return super().generate(messages, tools, options)


class _ReviewUnavailableProvider(_StructuredMockProvider):
    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        if len(self.requests) == 2:
            self.requests.append(list(messages))
            raise RuntimeError("semantic reviewer unavailable")
        return super().generate(messages, tools, options)


@pytest.mark.parametrize("criterion", ["All tests pass.", "No tests fail."])
def test_unrelated_successful_tool_cannot_prove_validation_criterion(
    criterion: str,
) -> None:
    result = AgentTurnResult(
        session_id="semantic-review",
        user_message="Check validation.",
        assistant_message="The lookup succeeded.",
        tool_executions=(),
        context_chars=0,
        memory_writes=(),
        stop_reason="complete",
    )

    assessment = _deterministic_criterion_assessment(
        criterion,
        result=result,
        evidence=[
            {
                "id": "tool:memory-search",
                "kind": "tool_success",
                "tool": "memory.search",
            }
        ],
    )

    assert assessment["status"] == "not_proven"
    assert assessment["satisfied"] is False
    assert assessment["evidence_refs"] == []


@pytest.mark.parametrize("criterion", ["All tests pass.", "No tests fail."])
def test_scheduler_validation_rejects_unrelated_successful_tool(
    criterion: str,
) -> None:
    task = TaskNodeRecord(
        task_id="task-validation-authority",
        run_id="run-validation-authority",
        title="Validate",
        goal="Run the test suite",
        profile="reviewer",
        status="running",
        approved=True,
        acceptance_criteria=(criterion,),
    )
    search_call = ToolCall(name="memory.search", arguments={"query": "tests"})
    result = AgentTurnResult(
        session_id="scheduler-review",
        user_message="Run the tests.",
        assistant_message="The memory lookup succeeded.",
        tool_executions=(
            ToolExecution(
                call=search_call,
                success=True,
                content="Found a memory.",
            ),
        ),
        context_chars=0,
        memory_writes=(),
        stop_reason="complete",
    )

    validation = _validate_task_completion(task, result)

    assert validation["passed"] is False
    assert validation["criteria"][0]["status"] == "not_proven"
    assert validation["criteria"][0]["evidence_refs"] == []


@pytest.mark.parametrize(
    "plan",
    [{"acceptance_evidence": ["declared_tools"]}, {}, None],
    ids=["typed-plan", "legacy-empty-plan", "legacy-null-plan"],
)
def test_builtin_inspection_criterion_accepts_declared_tool_evidence(
    plan: dict[str, Any] | None,
) -> None:
    tool_names = ("repo.search", "repo.map", "memory.search", "context.pack")
    task = TaskNodeRecord(
        task_id="task-inspect-repair",
        run_id="run-inspect-repair",
        title="Inspect repair context",
        goal="Gather repository context and failure evidence.",
        profile="worker",
        status="running",
        approved=True,
        plan=plan,
        required_tools=tool_names,
        acceptance_criteria=(
            "Relevant code, tests, and prior repair lessons are identified before mutation.",
        ),
    )
    result = AgentTurnResult(
        session_id="scheduler-inspection",
        user_message="Inspect the repair context.",
        assistant_message="Relevant code, test locations, and prior lessons were identified.",
        tool_executions=tuple(
            ToolExecution(
                call=ToolCall(name=name, arguments={}, id=f"call-{index}"),
                success=True,
                content="Inspection evidence found.",
            )
            for index, name in enumerate(tool_names)
        ),
        context_chars=0,
        memory_writes=(),
        stop_reason="complete",
    )

    validation = _validate_task_completion(task, result)

    assert validation["passed"] is True
    assert validation["criteria"][0]["status"] == "satisfied"
    assert validation["criteria"][0]["evidence_refs"] == [
        f"tool:call-{index}" for index in range(len(tool_names))
    ]


@pytest.mark.parametrize("criterion", ["All tests pass.", "No tests fail."])
def test_provider_review_cannot_use_unrelated_tool_to_prove_tests_pass(
    criterion: str,
) -> None:
    artifact = _parse_provider_review(
        {
            "verdict": "pass",
            "summary": "The provider claims validation passed.",
            "criteria": [
                {
                    "criterion": criterion,
                    "status": "satisfied",
                    "evidence_refs": ["tool:memory-search"],
                    "reason": "A tool succeeded.",
                }
            ],
            "remaining_risks": [],
            "confidence": 0.9,
        },
        criteria=[criterion],
        evidence=[
            {
                "id": "tool:memory-search",
                "kind": "tool_success",
                "tool": "memory.search",
            }
        ],
        provider="structured-test",
        model="structured-test",
    )

    assert artifact is None


def test_semantic_orchestration_provider_calls_are_explicit_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEST_AGENT_ENABLE_SEMANTIC_ORCHESTRATION", raising=False)
    assert AgentConfig.from_env().enable_semantic_orchestration is False

    monkeypatch.setenv("NEST_AGENT_ENABLE_SEMANTIC_ORCHESTRATION", "1")
    assert AgentConfig.from_env().enable_semantic_orchestration is True


def test_semantic_orchestration_round_trips_cli_config_and_runtime_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("NEST_AGENT_ENABLE_SEMANTIC_ORCHESTRATION", raising=False)
    parser = argparse.ArgumentParser()
    _add_agent_args(parser)
    args = parser.parse_args(["--enable-semantic-orchestration"])

    cli_config = _agent_config_from_args(args)
    assert cli_config.enable_semantic_orchestration is True
    assert AgentConfig.from_mapping(cli_config.to_mapping()).enable_semantic_orchestration is True

    snapshot = runtime_settings_snapshot(cli_config)
    assert snapshot.enable_semantic_orchestration is True
    assert snapshot.to_public_dict(path=Path("runtime.json"), persisted=False)[
        "enable_semantic_orchestration"
    ] is True
    disabled = merge_runtime_settings(
        cli_config,
        snapshot,
        {"enable_semantic_orchestration": False},
    )
    assert disabled.enable_semantic_orchestration is False
    assert apply_runtime_settings(cli_config, disabled).enable_semantic_orchestration is False

    restored = RuntimeSettings.from_mapping(
        {"enable_semantic_orchestration": True},
        AgentConfig(),
    )
    assert restored.enable_semantic_orchestration is True


def _manager(tmp_path: Path, *, provider: str = "mock") -> RunManager:
    config = AgentConfig(
        name="Kestrel",
        provider=provider,
        model="structured-test" if provider != "mock" else "mock",
        backend="memory",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state.db",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
        workspace=tmp_path,
        enable_semantic_orchestration=provider != "mock",
    )
    state = AgentStateStore(config.state_path)
    return RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=MCPManager(state),
        skills=SkillManager(config.skills_dir, state),
    )


def _agent(
    manager: RunManager,
    config: AgentConfig,
    provider: MockLLMProvider,
) -> NestedMV2Agent:
    return NestedMV2Agent(
        AgentDependencies(
            memory=build_memory_system(config.backend, config.memory_dir),
            llm=provider,
            tools=manager.build_registry(config),
            config=config,
            event_log=JsonlEventLog(config.log_dir / "events.jsonl"),
        )
    )


def _wait_for_status(
    manager: RunManager,
    run_id: str,
    statuses: set[str],
) -> dict[str, Any]:
    deadline = monotonic() + 10
    while monotonic() < deadline:
        run = manager.get_run(run_id)
        if str(run["status"]) in statuses:
            return run
        sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def test_mock_run_persists_honest_deterministic_plan_and_review_evidence(tmp_path: Path) -> None:
    manager = _manager(tmp_path)

    run = manager.create_run(message="Explain the current runtime", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})

    assert final["status"] == "completed"
    root = manager.task_graph(run.run_id)["tasks"][0]
    children = manager.task_graph(run.run_id)["tasks"][1:]
    assert root["status"] == "completed"
    assert {child["status"] for child in children} == {"skipped"}
    graph_contract = root["plan"]["graph_runtime"]
    assert graph_contract["can_revise_plan"] is False
    assert graph_contract["can_rewrite_task_dag"] is False
    assert graph_contract["execution_model"] == "single_chat_turn_then_optional_task_scheduler"
    assert root["plan"]["semantic_plan"]["source"] == "deterministic_task_graph"
    review = root["result"]["orchestration_review"]
    assert review["status"] == "completed"
    assert review["artifact"]["evaluator"] == "deterministic_runtime_evidence"
    assert review["artifact"]["decision"] == "pass"
    assert review["artifact"]["criteria"][0]["evidence_refs"] == ["assistant_response"]


def test_json_capable_provider_refines_plan_and_semantically_accepts_evidence(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, provider="openai")

    def build_scripted(config: AgentConfig) -> NestedMV2Agent:
        tasks = manager.state.list_task_nodes(manager.state.list_runs()[0].run_id)
        root = next(task for task in tasks if task.parent_id is None)
        children = [task for task in tasks if task.parent_id == root.task_id]
        semantic_criterion = "The response states that the inspection completed."
        plan = {
            "summary": "Inspect the runtime and report the concrete outcome.",
            "acceptance_criteria": [semantic_criterion],
            "task_guidance": [
                {
                    "task_id": task.task_id,
                    "objective": f"Produce evidence for {task.title}.",
                    "acceptance_criteria": [f"Evidence for {task.title} is explicit."],
                }
                for task in children
            ],
            "risks": ["A response without evidence should not pass review."],
        }
        review = {
            "verdict": "pass",
            "summary": "The response directly states the completed inspection outcome.",
            "criteria": [
                {
                    "criterion": criterion,
                    "status": "satisfied",
                    "evidence_refs": ["assistant_response"],
                    "reason": "The final response provides the claimed outcome.",
                }
                for criterion in [*root.acceptance_criteria, semantic_criterion]
            ],
            "remaining_risks": [],
            "confidence": 0.91,
        }
        return _agent(
            manager,
            config,
            _StructuredMockProvider(
                canned=[
                    LLMResponse(content=json.dumps(plan)),
                    LLMResponse(content="Runtime inspection completed with a concrete outcome."),
                    LLMResponse(content=json.dumps(review)),
                ]
            ),
        )

    manager._build_agent = build_scripted  # type: ignore[method-assign]

    run = manager.create_run(message="Inspect the runtime", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})

    assert final["status"] == "completed"
    tasks = manager.task_graph(run.run_id)["tasks"]
    root = tasks[0]
    assert root["plan"]["semantic_plan"]["source"] == "provider_structured"
    assert root["plan"]["graph_runtime"]["provider_plan_status"] == "accepted"
    assert root["plan"]["graph_runtime"]["can_rewrite_task_dag"] is False
    assert tasks[1]["plan"]["semantic_guidance"]["advisory_only"] is True
    artifact = root["result"]["orchestration_review"]["artifact"]
    assert artifact["evaluator"] == "provider_semantic_review"
    assert artifact["decision"] == "pass"
    assert artifact["validation_status"] == "validated_against_runtime_evidence_refs"


def test_provider_semantic_rejection_fails_run_with_persisted_criterion_evidence(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, provider="openai")

    def build_scripted(config: AgentConfig) -> NestedMV2Agent:
        tasks = manager.state.list_task_nodes(manager.state.list_runs()[0].run_id)
        root = next(task for task in tasks if task.parent_id is None)
        children = [task for task in tasks if task.parent_id == root.task_id]
        semantic_criterion = "The response cites validation evidence."
        plan = {
            "summary": "Validate before claiming completion.",
            "acceptance_criteria": [semantic_criterion],
            "task_guidance": [
                {
                    "task_id": task.task_id,
                    "objective": task.goal,
                    "acceptance_criteria": list(task.acceptance_criteria) or ["Report evidence."],
                }
                for task in children
            ],
            "risks": ["Unsupported completion claim."],
        }
        review = {
            "verdict": "fail",
            "summary": "The response claims completion but supplies no validation evidence.",
            "criteria": [
                {
                    "criterion": root.acceptance_criteria[0],
                    "status": "satisfied",
                    "evidence_refs": ["assistant_response"],
                    "reason": "A final response exists.",
                },
                {
                    "criterion": semantic_criterion,
                    "status": "not_proven",
                    "evidence_refs": [],
                    "reason": "No validation evidence reference was supplied.",
                },
            ],
            "remaining_risks": ["The claimed validation is unverified."],
            "confidence": 0.95,
        }
        return _agent(
            manager,
            config,
            _StructuredMockProvider(
                canned=[
                    LLMResponse(content=json.dumps(plan)),
                    LLMResponse(content="Everything is done."),
                    LLMResponse(content=json.dumps(review)),
                ]
            ),
        )

    manager._build_agent = build_scripted  # type: ignore[method-assign]

    run = manager.create_run(message="Validate the runtime", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})

    assert final["status"] == "failed"
    assert final["stop_reason"] == "semantic_review_failed"
    root = manager.task_graph(run.run_id)["tasks"][0]
    assert root["status"] == "failed"
    assert root["result"]["orchestration_review"]["artifact"]["decision"] == "fail"
    review = root["result"]["review"]
    assert review["artifact"]["decision"] == "fail"
    rejected = review["artifact"]["criteria"][1]
    assert rejected["status"] == "not_proven"
    assert rejected["evidence_refs"] == []


def test_semantic_planner_and_reviewer_redact_secrets_before_provider_calls(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, provider="openai")
    secret = "sk-semanticBoundarySecret123456789"
    providers: list[_StructuredMockProvider] = []

    def build_scripted(config: AgentConfig) -> NestedMV2Agent:
        tasks = manager.state.list_task_nodes(manager.state.list_runs()[0].run_id)
        root = next(task for task in tasks if task.parent_id is None)
        children = [task for task in tasks if task.parent_id == root.task_id]
        semantic_criterion = "The response is non-empty."
        plan = {
            "summary": "Return a redacted response.",
            "acceptance_criteria": [semantic_criterion],
            "task_guidance": [
                {
                    "task_id": task.task_id,
                    "objective": "Return bounded evidence.",
                    "acceptance_criteria": ["Evidence is present."],
                }
                for task in children
            ],
            "risks": [],
        }
        review = {
            "verdict": "pass",
            "summary": "The response is present.",
            "criteria": [
                {
                    "criterion": criterion,
                    "status": "satisfied",
                    "evidence_refs": ["assistant_response"],
                    "reason": "A bounded assistant response is present.",
                }
                for criterion in [*root.acceptance_criteria, semantic_criterion]
            ],
            "remaining_risks": [],
            "confidence": 0.8,
        }
        provider = _StructuredMockProvider(
            canned=[
                LLMResponse(content=json.dumps(plan)),
                LLMResponse(content="Completed without echoing credentials."),
                LLMResponse(content=json.dumps(review)),
            ]
        )
        providers.append(provider)
        return _agent(manager, config, provider)

    manager._build_agent = build_scripted  # type: ignore[method-assign]

    run = manager.create_run(
        message=f"Inspect the runtime with credential {secret}",
        session_id="secret-boundary",
    )
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})

    assert final["status"] == "completed"
    request_text = "\n".join(
        message.content
        for provider in providers
        for request in provider.requests
        for message in request
    )
    assert secret not in request_text
    assert "<redacted>" in request_text


def test_invalid_provider_review_fallback_preserves_semantic_criteria_and_fails_closed(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, provider="openai")

    def build_scripted(config: AgentConfig) -> NestedMV2Agent:
        tasks = manager.state.list_task_nodes(manager.state.list_runs()[0].run_id)
        root = next(task for task in tasks if task.parent_id is None)
        children = [task for task in tasks if task.parent_id == root.task_id]
        plan = {
            "summary": "Return a concrete response.",
            "acceptance_criteria": ["The answer is concrete."],
            "task_guidance": [
                {
                    "task_id": task.task_id,
                    "objective": task.goal,
                    "acceptance_criteria": list(task.acceptance_criteria) or ["Report evidence."],
                }
                for task in children
            ],
            "risks": [],
        }
        return _agent(
            manager,
            config,
            _StructuredMockProvider(
                canned=[
                    LLMResponse(content=json.dumps(plan)),
                    LLMResponse(content="A concrete response."),
                    LLMResponse(content="not-json"),
                ]
            ),
        )

    manager._build_agent = build_scripted  # type: ignore[method-assign]

    run = manager.create_run(message="Give a concrete response", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})

    assert final["status"] == "failed"
    assert final["stop_reason"] == "acceptance_evidence_missing"
    root = manager.task_graph(run.run_id)["tasks"][0]
    review = root["result"]["orchestration_review"]
    assert review["provider_review_status"] == "invalid_json"
    assert review["artifact"]["evaluator"] == "deterministic_runtime_evidence"
    assert review["artifact"]["remaining_risks"]
    semantic_assessment = next(
        item
        for item in review["artifact"]["criteria"]
        if item["criterion"] == "The answer is concrete."
    )
    assert semantic_assessment["status"] == "not_proven"
    assert semantic_assessment["evidence_refs"] == []


def test_unavailable_provider_review_fallback_preserves_semantic_criteria(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path, provider="openai")

    def build_scripted(config: AgentConfig) -> NestedMV2Agent:
        tasks = manager.state.list_task_nodes(manager.state.list_runs()[0].run_id)
        root = next(task for task in tasks if task.parent_id is None)
        children = [task for task in tasks if task.parent_id == root.task_id]
        plan = {
            "summary": "Return bounded evidence.",
            "acceptance_criteria": ["The answer includes bounded evidence."],
            "task_guidance": [
                {
                    "task_id": task.task_id,
                    "objective": task.goal,
                    "acceptance_criteria": list(task.acceptance_criteria) or ["Report evidence."],
                }
                for task in children
            ],
            "risks": [],
        }
        return _agent(
            manager,
            config,
            _ReviewUnavailableProvider(
                canned=[
                    LLMResponse(content=json.dumps(plan)),
                    LLMResponse(content="Bounded evidence was returned."),
                ]
            ),
        )

    manager._build_agent = build_scripted  # type: ignore[method-assign]

    run = manager.create_run(message="Return bounded evidence", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})

    assert final["status"] == "failed"
    root = manager.task_graph(run.run_id)["tasks"][0]
    review = root["result"]["orchestration_review"]
    assert review["provider_review_status"] == "error:RuntimeError"
    assert review["artifact"]["evaluator"] == "deterministic_runtime_evidence"
    assert any(
        item["criterion"] == "The answer includes bounded evidence."
        and item["status"] == "not_proven"
        for item in review["artifact"]["criteria"]
    )


def test_approval_continuation_runs_same_reviewer_gate_and_replaces_blocked_artifact(
    tmp_path: Path,
) -> None:
    manager = _manager(tmp_path)
    manager.config = replace(manager.config, allow_shell=True)
    scripted = [
        LLMResponse(
            content="Approval is required for validation.",
            tool_calls=(
                ToolCall(
                    name="test.run",
                    arguments={"command": ["python3", "-c", "print('semantic-review-ok')"]},
                ),
            ),
        ),
        LLMResponse(content="Validation completed after approval with no remaining blocker."),
    ]

    def build_scripted(config: AgentConfig) -> NestedMV2Agent:
        return _agent(manager, config, MockLLMProvider(canned=[scripted.pop(0)]))

    manager._build_agent = build_scripted  # type: ignore[method-assign]

    run = manager.create_run(message="Run validation", session_id="session")
    blocked = _wait_for_status(manager, run.run_id, {"blocked", "failed"})
    assert blocked["status"] == "blocked"
    assert manager.task_graph(run.run_id)["tasks"][0]["status"] == "blocked"
    approval = manager.state.list_approvals(status="pending")[0]

    manager.decide_approval(
        approval["approval_id"],
        approved=True,
        arguments=approval["arguments"],
    )
    final = _wait_for_status(manager, run.run_id, {"completed", "failed"})

    assert final["status"] == "completed"
    reviews = [
        event["payload"]
        for event in manager.state.list_run_steps(run.run_id)
        if event["type"] == "review.completed"
    ]
    assert [review["status"] for review in reviews] == ["blocked", "completed"]
    assert reviews[1]["continuation"] == "approval"
    root = manager.task_graph(run.run_id)["tasks"][0]
    assert root["status"] == "completed"
    assert root["result"]["orchestration_review"]["status"] == "completed"


def test_scheduler_root_reconciliation_preserves_primary_review_artifact(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    manager.config = replace(
        manager.config,
        enable_autonomous_scheduler=True,
        max_scheduler_tasks=3,
        max_scheduler_cycles=5,
    )

    run = manager.create_run(message="Inspect and summarize the runtime", session_id="session")
    final = _wait_for_status(manager, run.run_id, {"completed", "failed", "blocked"})

    assert final["status"] == "completed"
    root = manager.task_graph(run.run_id)["tasks"][0]
    assert root["status"] == "completed"
    assert root["result"]["orchestration_review"]["artifact"]["decision"] == "pass"
    assert root["result"]["child_statuses"] == ["completed"]
    assert root["result"]["terminal_reconciliation"]["status"] == "completed"


def test_mock_task_validation_reports_unverified_criteria_instead_of_fake_success() -> None:
    task = TaskNodeRecord(
        task_id="task_mock_evidence",
        run_id="run_mock_evidence",
        title="Validate",
        goal="Run validation",
        profile="worker",
        status="running",
        approved=True,
        required_tools=("test.run",),
        acceptance_criteria=("Validation passes.",),
    )
    result = AgentTurnResult(
        session_id="session",
        user_message="validate",
        assistant_message="Mock result",
        tool_executions=(),
        context_chars=0,
        memory_writes=(),
        stop_reason="complete",
    )

    validation = _validate_task_completion(task, result, allow_mock_provider=True)

    assert validation["passed"] is True
    assert validation["gate"] == "mock_execution_bypass"
    assert validation["criteria"][0]["satisfied"] is False
    assert validation["criteria"][0]["status"] == "not_verified_mock"
    assert validation["criteria"][0]["evidence_refs"] == []
