"""Deterministic end-to-end learning release gate for Kestrel.

This benchmark exercises the production ``NestedMV2Agent`` learning cycle. Task 1
causes a real tool failure, supplies a materially changed strategy, validates the
repair, and lets the runtime (not the benchmark) persist a ``FailureEpisode`` and
``LessonCard``. Task 2 then proves that the persisted lesson is recalled as
untrusted evidence and changes the mock model's tool plan.

The benchmark-scoped LLM and tools are deterministic. No benchmark code seeds or
directly writes an oracle lesson. Real-provider learning is evaluated separately by
``scripts/run_live_learning_eval.py``.

Usage:
    python benchmarks/real_agent_learning_benchmark.py \
      --output benchmark_results/agent_learning_gate.json
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_log import JsonlEventLog
from nested_memvid_agent.layers import LayeredMemorySystem, load_layer_specs
from nested_memvid_agent.llm.base import LLMProvider, ProviderCapabilities
from nested_memvid_agent.models import MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.promotion_ledger import PromotionLedger
from nested_memvid_agent.runtime_models import (
    AgentTurnResult,
    ChatMessage,
    LLMOptions,
    LLMResponse,
    StrategyProposal,
    ToolCall,
    ToolExecution,
    ToolSpec,
)
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.registry import ToolRegistry

_CANDIDATE_PATH = "candidate.txt"
_INITIAL_VALUE = "incomplete"
_FAILED_VALUE = "still-incomplete"
_VALIDATED_VALUE = "validated"
_LESSON_HINT = "replace the incomplete value with validated"
_VALIDATION_COMMAND = ["benchmark.validate", _CANDIDATE_PATH, _VALIDATED_VALUE]


@dataclass(frozen=True)
class ScriptedToolStep:
    """One deterministic model-selected tool call."""

    name: str
    arguments: dict[str, Any]
    strategy: StrategyProposal | None = None


@dataclass(frozen=True)
class ScriptedSequence:
    """A deterministic tool-call sequence for one task condition."""

    name: str
    tools: tuple[ScriptedToolStep, ...]
    expects_memory_hint: str | None = None


@dataclass(frozen=True)
class TaskScenario:
    """One half of the end-to-end transfer pair."""

    id: str
    name: str
    category: str
    objective: str
    setup: Callable[[Path], None]
    success_check: Callable[[Path, AgentTurnResult], bool]
    naive_sequence: ScriptedSequence
    optimal_sequence: ScriptedSequence
    max_tool_rounds: int = 8


class BenchmarkMockLLM(LLMProvider):
    """Model fixture that changes plan only when a recalled lesson is present."""

    def __init__(self) -> None:
        self._scenarios: dict[str, TaskScenario] = {}
        self._session_steps: dict[str, int] = {}

    def register_scenario(self, scenario: TaskScenario) -> None:
        self._scenarios[scenario.id] = scenario

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="benchmark_mock",
            supports_native_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_system_messages=True,
        )

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[Any],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del tools, options
        objective = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            "",
        )
        scenario = self._match_scenario(objective)
        if scenario is None:
            return LLMResponse(content=f"Unrecognized benchmark objective: {objective[:80]}")

        recalled_context = "\n".join(
            message.content
            for message in messages
            if message.role == "user"
            and (
                "COMPILED NESTED MEMORY CONTEXT" in message.content
                or "MV2 PSEUDO-CONTEXT PACK" in message.content
                or "Prior Failure Lessons" in message.content
            )
        )
        hint = scenario.optimal_sequence.expects_memory_hint
        has_hint = bool(hint and hint.lower() in recalled_context.lower())
        sequence = scenario.optimal_sequence if has_hint else scenario.naive_sequence

        session_key = self._session_key(messages, scenario.id)
        step = self._session_steps.get(session_key, 0)
        if step < len(sequence.tools):
            scripted = sequence.tools[step]
            self._session_steps[session_key] = step + 1
            return LLMResponse(
                content=f"Execute deterministic step {step + 1}: {scripted.name}.",
                tool_calls=(
                    ToolCall(
                        name=scripted.name,
                        arguments=dict(scripted.arguments),
                        strategy=scripted.strategy,
                    ),
                ),
            )
        return LLMResponse(content="Task complete.")

    def _match_scenario(self, objective: str) -> TaskScenario | None:
        marker = re.search(r"\[bench:([^\]]+)\]", objective)
        if marker is not None:
            return self._scenarios.get(marker.group(1))
        clean = re.sub(r"\s*\[bench:[^\]]+\]", "", objective).strip().lower()
        return next(
            (
                scenario
                for scenario in self._scenarios.values()
                if scenario.objective.lower() in clean or clean in scenario.objective.lower()
            ),
            None,
        )

    @staticmethod
    def _session_key(messages: list[ChatMessage], scenario_id: str) -> str:
        objective = next(
            (message.content for message in reversed(messages) if message.role == "user"),
            scenario_id,
        )
        return f"{scenario_id}:{objective}"


def _public_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in arguments.items() if not key.startswith("_")}


def _tool_call(name: str, arguments: dict[str, Any]) -> ToolCall:
    public = _public_arguments(arguments)
    call_id = str(arguments.get("_tool_call_id", ""))
    return ToolCall(name=name, arguments=public, id=call_id or ToolCall(name, public).id)


class BenchmarkReadTool(AgentTool):
    """Deterministic, workspace-bounded stand-in for ``file.read``."""

    spec = ToolSpec(
        name="file.read",
        description="Read the deterministic benchmark candidate.",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call(self.spec.name, arguments)
        if arguments.get("path") != _CANDIDATE_PATH:
            return self._result(
                call,
                success=False,
                content="Benchmark reads are limited to candidate.txt.",
                error="benchmark_path_rejected",
            )
        content = (context.workspace / _CANDIDATE_PATH).read_text()
        return self._result(call, success=True, content=content, data={"content": content})


class BenchmarkWriteTool(AgentTool):
    """Deterministic high-risk stand-in for ``file.write``."""

    spec = ToolSpec(
        name="file.write",
        description="Write the deterministic benchmark candidate.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        risk="high",
        requires_approval=True,
    )
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call(self.spec.name, arguments)
        if arguments.get("path") != _CANDIDATE_PATH:
            return self._result(
                call,
                success=False,
                content="Benchmark writes are limited to candidate.txt.",
                error="benchmark_path_rejected",
            )
        content = str(arguments.get("content", ""))
        (context.workspace / _CANDIDATE_PATH).write_text(content)
        return self._result(
            call,
            success=True,
            content=f"Wrote {_CANDIDATE_PATH}.",
            data={"path": _CANDIDATE_PATH, "content": content},
        )


class BenchmarkValidationTool(AgentTool):
    """Deterministic validation-producing stand-in for ``test.run``."""

    spec = ToolSpec(
        name="test.run",
        description="Validate the deterministic benchmark candidate.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["command"],
        },
        risk="high",
        requires_approval=True,
        produces_validation=True,
    )
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call(self.spec.name, arguments)
        command = arguments.get("command")
        if command != _VALIDATION_COMMAND:
            return self._result(
                call,
                success=False,
                content="Unexpected deterministic validation command.",
                error="benchmark_command_rejected",
            )
        actual = (context.workspace / _CANDIDATE_PATH).read_text()
        success = actual == _VALIDATED_VALUE
        content = (
            "validation passed: candidate.txt is validated"
            if success
            else f"AssertionError: expected '{_VALIDATED_VALUE}', got '{actual}'"
        )
        return self._result(
            call,
            success=success,
            content=content,
            data={
                "validation": {
                    "success": success,
                    "expected": _VALIDATED_VALUE,
                    "actual": actual,
                    "fixture": "deterministic_mock_tool",
                }
            },
            error=None if success else "benchmark_validation_failed",
        )


def _build_benchmark_tools() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(BenchmarkReadTool())
    registry.register(BenchmarkWriteTool())
    registry.register(BenchmarkValidationTool())
    return registry


def _setup_candidate_task(workspace: Path) -> None:
    (workspace / _CANDIDATE_PATH).write_text(_INITIAL_VALUE)


def _check_candidate_task(workspace: Path, result: AgentTurnResult) -> bool:
    del result
    candidate = workspace / _CANDIDATE_PATH
    return candidate.is_file() and candidate.read_text() == _VALIDATED_VALUE


def _step(
    name: str,
    arguments: dict[str, Any],
    *,
    strategy: StrategyProposal | None = None,
) -> ScriptedToolStep:
    return ScriptedToolStep(name=name, arguments=arguments, strategy=strategy)


def build_task_scenarios() -> list[TaskScenario]:
    """Build the deterministic failure-resolution-transfer pair."""

    failure_then_resolution = ScriptedSequence(
        name="failure_then_validated_resolution",
        tools=(
            _step("file.read", {"path": _CANDIDATE_PATH}),
            _step(
                "file.write",
                {"path": _CANDIDATE_PATH, "content": _FAILED_VALUE},
            ),
            _step("test.run", {"command": list(_VALIDATION_COMMAND)}),
            _step(
                "file.write",
                {"path": _CANDIDATE_PATH, "content": _VALIDATED_VALUE},
            ),
            _step(
                "test.run",
                {"command": list(_VALIDATION_COMMAND)},
                strategy=StrategyProposal(
                    changed_strategy=(
                        "Replace the incomplete value with validated after inspecting "
                        "the failed validation evidence, then verify the corrected candidate."
                    ),
                    why_different=(
                        "The failed candidate retained an incomplete value; this changes "
                        "the artifact before repeating validation."
                    ),
                    expected_signal="The deterministic validation reports success.",
                    fallback_if_fails="Re-read candidate.txt before another mutation.",
                ),
            ),
        ),
    )
    task_two_naive = ScriptedSequence(
        name="unassisted_incomplete_repair",
        tools=(
            _step("file.read", {"path": _CANDIDATE_PATH}),
            _step(
                "file.write",
                {"path": _CANDIDATE_PATH, "content": _FAILED_VALUE},
            ),
        ),
    )
    task_two_learned = ScriptedSequence(
        name="recalled_validated_repair",
        expects_memory_hint=_LESSON_HINT,
        tools=(
            _step("file.read", {"path": _CANDIDATE_PATH}),
            _step(
                "file.write",
                {"path": _CANDIDATE_PATH, "content": _VALIDATED_VALUE},
            ),
            _step("test.run", {"command": list(_VALIDATION_COMMAND)}),
        ),
    )
    objective = "Repair candidate.txt and prove the corrected value with validation"
    return [
        TaskScenario(
            id="validated_repair_1",
            name="Task 1: fail, change strategy, and validate",
            category="validated_repair",
            objective=objective,
            setup=_setup_candidate_task,
            success_check=_check_candidate_task,
            naive_sequence=failure_then_resolution,
            optimal_sequence=failure_then_resolution,
        ),
        TaskScenario(
            id="validated_repair_2",
            name="Task 2: transfer the learned repair",
            category="validated_repair",
            objective=objective,
            setup=_setup_candidate_task,
            success_check=_check_candidate_task,
            naive_sequence=task_two_naive,
            optimal_sequence=task_two_learned,
        ),
    ]


def _build_config(workspace: Path, memory_dir: Path, provider: str, model: str) -> AgentConfig:
    return AgentConfig(
        provider=provider,
        model=model,
        memory_dir=memory_dir,
        workspace=workspace,
        state_path=memory_dir.parent / f"{memory_dir.name}.state.db",
        log_dir=memory_dir.parent / f"{memory_dir.name}.logs",
        allow_shell=True,
        allow_file_write=True,
        allow_git_commit=False,
        max_tool_rounds=8,
        enable_agentic_cycle=True,
        context_budget_chars=12_000,
        tool_retry_max_attempts=0,
    )


def _build_agent_with_llm(config: AgentConfig, llm: LLMProvider) -> NestedMV2Agent:
    """Build the production agent around deterministic benchmark adapters."""

    config.memory_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    specs = load_layer_specs(config.layer_config_path) if config.layer_config_path else None
    state = AgentStateStore(config.state_path)
    memory = build_memory_system(
        config.backend,
        config.memory_dir,
        specs=specs,
        ledger=PromotionLedger(state),
    )
    event_log = JsonlEventLog(config.log_dir / "events.jsonl")
    return NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=_build_benchmark_tools(),
            config=config,
            event_log=event_log,
        )
    )


def _scenario_approval_handler(
    agent: NestedMV2Agent,
    scenario: TaskScenario,
) -> tuple[
    Callable[[ToolCall, ToolSpec, ToolContext], ToolExecution],
    list[dict[str, Any]],
]:
    """Approve only complete, exact calls declared in the scenario fixture."""

    declared_calls = {
        (step.name, json.dumps(step.arguments, sort_keys=True, separators=(",", ":")))
        for sequence in (scenario.naive_sequence, scenario.optimal_sequence)
        for step in sequence.tools
    }
    approvals: list[dict[str, Any]] = []

    def approve_declared_call(
        call: ToolCall,
        spec: ToolSpec,
        context: ToolContext,
    ) -> ToolExecution:
        signature = (
            call.name,
            json.dumps(call.arguments, sort_keys=True, separators=(",", ":")),
        )
        if signature not in declared_calls:
            return ToolExecution(
                call=call,
                success=False,
                content="Benchmark operator refused an undeclared exact call.",
                error="approval_required",
            )
        exact_context = replace(
            context,
            approval_handler=None,
            approved_tool_call_ids=frozenset({call.id}),
            approved_tool_call_arguments={call.id: dict(call.arguments)},
        )
        approvals.append(
            {
                "tool_call_id": call.id,
                "tool_name": spec.name,
                "arguments": dict(call.arguments),
                "exact_argument_match": True,
            }
        )
        return agent.tools.execute(call, exact_context)

    return approve_declared_call, approvals


def _record_snapshot(record: MemoryRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        decoded = json.loads(record.content)
        if isinstance(decoded, dict):
            payload = decoded
    except json.JSONDecodeError:
        payload = {}
    return {
        "id": record.id,
        "layer": record.layer.value,
        "kind": record.kind.value,
        "confidence": record.confidence,
        "metadata": dict(record.metadata),
        "evidence": [
            {"source": ref.source, "locator": ref.locator, "quote": ref.quote}
            for ref in record.evidence
        ],
        "payload": payload,
    }


def _learning_evidence(
    memory: LayeredMemorySystem,
    result: AgentTurnResult,
) -> dict[str, Any]:
    failures = [
        record
        for record in memory.iter_records(MemoryLayer.EPISODIC)
        if record.metadata.get("cognition_schema") == "failure_episode.v1"
    ]
    lessons = [
        record
        for record in memory.iter_records(MemoryLayer.PROCEDURAL)
        if record.metadata.get("cognition_schema") == "lesson_card.v1"
    ]
    proof = result.proof_of_work or {}
    return {
        "failure_episodes": [_record_snapshot(record) for record in failures],
        "lesson_cards": [_record_snapshot(record) for record in lessons],
        "proof": {
            "failures": list(proof.get("failures", [])),
            "validation_evidence": list(proof.get("validation_evidence", [])),
            "lessons_created": list(proof.get("lessons_created", [])),
            "lessons_applied": list(proof.get("lessons_applied", [])),
        },
    }


def _run_task(
    agent: NestedMV2Agent,
    scenario: TaskScenario,
    workspace: Path,
    provider: str = "mock",
) -> dict[str, Any]:
    """Run one task through the production agent loop and return gate evidence."""

    for item in workspace.iterdir():
        if item.is_dir() and item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
    scenario.setup(workspace)

    objective = f"{scenario.objective} [bench:{scenario.id}]"
    approval_handler, operator_approvals = _scenario_approval_handler(agent, scenario)
    started = time.perf_counter()
    result = agent.chat(
        objective,
        session_id=f"bench_{scenario.id}",
        run_id=f"run_bench_{scenario.id}",
        approval_handler=approval_handler,
    )
    elapsed = time.perf_counter() - started
    executions = [
        {
            "tool": execution.call.name,
            "tool_call_id": execution.call.id,
            "arguments": dict(execution.call.arguments),
            "success": execution.success,
            "error": execution.error,
        }
        for execution in result.tool_executions
    ]
    return {
        "scenario_id": scenario.id,
        "scenario_name": scenario.name,
        "category": scenario.category,
        "success": scenario.success_check(workspace, result),
        "tool_calls": [item["tool"] for item in executions],
        "tool_executions": executions,
        "tool_rounds": len(executions),
        "stop_reason": result.stop_reason,
        "elapsed_seconds": round(elapsed, 3),
        "assistant_message": result.assistant_message[:300],
        "operator_approvals": operator_approvals,
        "learning_evidence": _learning_evidence(agent.memory, result),
    }


def _expected_success(*, condition: str, scenario: TaskScenario) -> bool:
    return scenario.id.endswith("_1") or condition == "treatment"


def _new_agent(
    scenarios: list[TaskScenario],
    workspace: Path,
    memory_dir: Path,
    provider: str,
    model: str,
) -> NestedMV2Agent:
    config = _build_config(workspace, memory_dir, provider, model)
    llm = BenchmarkMockLLM()
    for scenario in scenarios:
        llm.register_scenario(scenario)
    return _build_agent_with_llm(config, llm)


def run_control(
    scenarios: list[TaskScenario],
    base_workspace: Path,
    provider: str,
    model: str,
) -> list[dict[str, Any]]:
    """Run each task with an isolated memory, preventing cross-task transfer."""

    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        memory_dir = base_workspace.parent / f"memory_control_{scenario.id}"
        shutil.rmtree(memory_dir, ignore_errors=True)
        agent = _new_agent([scenario], base_workspace, memory_dir, provider, model)
        result = _run_task(agent, scenario, base_workspace, provider=provider)
        result["condition"] = "control"
        result["expected_success"] = _expected_success(
            condition="control", scenario=scenario
        )
        results.append(result)
    return results


def run_treatment(
    scenarios: list[TaskScenario],
    base_workspace: Path,
    provider: str,
    model: str,
) -> list[dict[str, Any]]:
    """Run Task 1 and Task 2 through one agent with shared production memory."""

    memory_dir = base_workspace.parent / "memory_treatment_validated_repair"
    shutil.rmtree(memory_dir, ignore_errors=True)
    agent = _new_agent(scenarios, base_workspace, memory_dir, provider, model)
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        result = _run_task(agent, scenario, base_workspace, provider=provider)
        result["condition"] = "treatment"
        result["expected_success"] = _expected_success(
            condition="treatment", scenario=scenario
        )
        results.append(result)
    return results


def analyze_results(
    control_results: list[dict[str, Any]],
    treatment_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute outcome and successful-run efficiency metrics."""

    task1_control = [item for item in control_results if item["scenario_id"].endswith("_1")]
    task1_treatment = [
        item for item in treatment_results if item["scenario_id"].endswith("_1")
    ]
    task2_control = [item for item in control_results if item["scenario_id"].endswith("_2")]
    task2_treatment = [
        item for item in treatment_results if item["scenario_id"].endswith("_2")
    ]

    def success_rate(results: list[dict[str, Any]]) -> float:
        return round(sum(bool(item["success"]) for item in results) / max(len(results), 1), 2)

    def successful_rounds(results: list[dict[str, Any]]) -> float | None:
        values = [float(item["tool_rounds"]) for item in results if item["success"]]
        return None if not values else round(sum(values) / len(values), 2)

    return {
        "task1_learning_creation": {
            "control_success_rate": success_rate(task1_control),
            "treatment_success_rate": success_rate(task1_treatment),
        },
        "task2_transfer": {
            "control_success_rate": success_rate(task2_control),
            "treatment_success_rate": success_rate(task2_treatment),
            "control_avg_successful_rounds": successful_rounds(task2_control),
            "treatment_avg_successful_rounds": successful_rounds(task2_treatment),
            "outcome_improvement": round(
                success_rate(task2_treatment) - success_rate(task2_control), 2
            ),
        },
    }


def _task_result(
    results: list[dict[str, Any]],
    suffix: str,
) -> dict[str, Any] | None:
    return next((item for item in results if item["scenario_id"].endswith(suffix)), None)


def _valid_learning_chain(task1: dict[str, Any] | None) -> tuple[bool, dict[str, Any]]:
    if task1 is None:
        return False, {"reason": "Task 1 result is missing."}
    evidence = task1.get("learning_evidence", {})
    failures = evidence.get("failure_episodes", [])
    lessons = evidence.get("lesson_cards", [])
    proof = evidence.get("proof", {})
    if not failures or not lessons:
        return False, {
            "reason": "Task 1 did not persist both cognition record types.",
            "failure_count": len(failures),
            "lesson_count": len(lessons),
        }
    failure = failures[-1]
    lesson = lessons[-1]
    failure_id = str(failure.get("id", ""))
    lesson_payload = lesson.get("payload", {})
    evidence_refs = lesson_payload.get("evidence_refs", [])
    lesson_evidence = lesson.get("evidence", [])
    validation_refs = [
        item for item in lesson_evidence if item.get("source") == "validation"
    ]
    checks = {
        "failed_validation_observed": any(
            item.get("tool") == "test.run" and item.get("success") is False
            for item in task1.get("tool_executions", [])
        ),
        "successful_validation_observed": any(
            item.get("tool") == "test.run" and item.get("success") is True
            for item in task1.get("tool_executions", [])
        ),
        "failure_schema": failure.get("metadata", {}).get("cognition_schema")
        == "failure_episode.v1",
        "failure_has_runtime_provenance": any(
            str(item.get("source", "")).startswith("agent_runtime://runs/")
            and item.get("locator") == failure_id
            for item in failure.get("evidence", [])
        ),
        "failure_confidence": float(failure.get("confidence", 0.0)) > 0.0,
        "failure_marked_resolved": failure.get("payload", {}).get("resolved") is True
        and failure.get("metadata", {}).get("validation_status") == "resolved",
        "failure_links_validation": bool(
            failure.get("payload", {}).get("validation_evidence")
        )
        and any(
            item.get("source") == "validation" and bool(item.get("locator"))
            for item in failure.get("evidence", [])
        ),
        "lesson_schema": lesson.get("metadata", {}).get("cognition_schema")
        == "lesson_card.v1",
        "lesson_validated_once": lesson.get("metadata", {}).get("validation_status")
        == "validated_once",
        "lesson_confidence": float(lesson.get("confidence", 0.0)) >= 0.8,
        "lesson_links_failure": failure_id in evidence_refs
        and any(
            item.get("source") == "failure_episode" and item.get("locator") == failure_id
            for item in lesson_evidence
        ),
        "lesson_links_validation": bool(validation_refs)
        and all(bool(item.get("locator")) for item in validation_refs),
        "proof_records_failure": any(
            item.get("failure_id") == failure_id for item in proof.get("failures", [])
        ),
        "proof_records_validation": bool(proof.get("validation_evidence")),
        "proof_records_lesson": any(
            item.get("id") == lesson.get("id") for item in proof.get("lessons_created", [])
        ),
    }
    return all(checks.values()), {
        "checks": checks,
        "failure_id": failure_id,
        "lesson_id": lesson.get("id"),
    }


def benchmark_assertions(
    control_results: list[dict[str, Any]],
    treatment_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return fail-closed release assertions for creation and transfer."""

    results = [*control_results, *treatment_results]
    expected_pairs = {
        ("control", "validated_repair_1"),
        ("control", "validated_repair_2"),
        ("treatment", "validated_repair_1"),
        ("treatment", "validated_repair_2"),
    }
    actual_pairs = {
        (str(item.get("condition")), str(item.get("scenario_id"))) for item in results
    }
    incomplete = [
        {
            "condition": item.get("condition"),
            "scenario_id": item.get("scenario_id"),
            "stop_reason": item.get("stop_reason"),
        }
        for item in results
        if item.get("stop_reason") != "complete"
    ]
    mismatches = [
        {
            "condition": item.get("condition"),
            "scenario_id": item.get("scenario_id"),
            "expected": item.get("expected_success"),
            "actual": item.get("success"),
        }
        for item in results
        if bool(item.get("expected_success")) != bool(item.get("success"))
    ]
    treatment_task1 = _task_result(treatment_results, "_1")
    treatment_task2 = _task_result(treatment_results, "_2")
    control_task2 = _task_result(control_results, "_2")
    learning_chain_passed, learning_chain_details = _valid_learning_chain(treatment_task1)

    lesson_ids = {
        item.get("id")
        for item in (treatment_task1 or {})
        .get("learning_evidence", {})
        .get("lesson_cards", [])
    }
    applied_ids = {
        item.get("id")
        for item in (treatment_task2 or {})
        .get("learning_evidence", {})
        .get("proof", {})
        .get("lessons_applied", [])
    }
    transfer_details = {
        "created_lesson_ids": sorted(str(item) for item in lesson_ids if item),
        "task2_applied_ids": sorted(str(item) for item in applied_ids if item),
        "task2_tool_calls": (treatment_task2 or {}).get("tool_calls", []),
    }
    transfer_passed = bool(lesson_ids & applied_ids) and bool(
        treatment_task2 and treatment_task2.get("success")
    )
    improvement_passed = bool(
        control_task2
        and treatment_task2
        and not control_task2.get("success")
        and treatment_task2.get("success")
    )
    exact_approval_failures: list[dict[str, Any]] = []
    approved_call_count = 0
    for item in results:
        approvals = {
            str(approval.get("tool_call_id")): approval
            for approval in item.get("operator_approvals", [])
        }
        approved_call_count += len(approvals)
        for execution in item.get("tool_executions", []):
            if execution.get("tool") not in {"file.write", "test.run"}:
                continue
            approval = approvals.get(str(execution.get("tool_call_id")))
            if (
                approval is None
                or approval.get("tool_name") != execution.get("tool")
                or approval.get("arguments") != execution.get("arguments")
                or approval.get("exact_argument_match") is not True
            ):
                exact_approval_failures.append(
                    {
                        "condition": item.get("condition"),
                        "scenario_id": item.get("scenario_id"),
                        "tool_call_id": execution.get("tool_call_id"),
                        "tool": execution.get("tool"),
                    }
                )
    return [
        {
            "name": "required_ab_runs_executed",
            "passed": actual_pairs == expected_pairs,
            "details": {
                "missing": sorted(expected_pairs - actual_pairs),
                "unexpected": sorted(actual_pairs - expected_pairs),
            },
        },
        {
            "name": "all_agent_turns_completed",
            "passed": not incomplete,
            "details": {"incomplete": incomplete},
        },
        {
            "name": "expected_task_outcomes",
            "passed": not mismatches,
            "details": {"mismatches": mismatches},
        },
        {
            "name": "task1_production_learning_chain",
            "passed": learning_chain_passed,
            "details": learning_chain_details,
        },
        {
            "name": "task2_retrieves_and_applies_created_lesson",
            "passed": transfer_passed,
            "details": transfer_details,
        },
        {
            "name": "task2_outcome_improves_over_fresh_memory_control",
            "passed": improvement_passed,
            "details": {
                "control_success": (control_task2 or {}).get("success"),
                "treatment_success": (treatment_task2 or {}).get("success"),
            },
        },
        {
            "name": "high_risk_calls_use_exact_operator_approvals",
            "passed": not exact_approval_failures and approved_call_count > 0,
            "details": {
                "approved_call_count": approved_call_count,
                "failures": exact_approval_failures,
            },
        },
    ]


def run_learning_gate(base_tmp: Path) -> dict[str, Any]:
    """Execute and return the deterministic end-to-end learning report."""

    workspace = base_tmp / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    scenarios = build_task_scenarios()
    control_results = run_control(scenarios, workspace, "mock", "mock")
    treatment_results = run_treatment(scenarios, workspace, "mock", "mock")
    assertions = benchmark_assertions(control_results, treatment_results)
    return {
        "schema": "kestrel.agent_learning_gate.v1",
        "meta": {
            "benchmark_kind": "deterministic_end_to_end_agent_learning",
            "provider": "mock",
            "model": "mock",
            "workspace": str(workspace),
            "scenario_count": len(scenarios),
            "learning_write_path": "NestedMV2Agent agentic failure cycle",
            "seeded_oracle_lessons": 0,
            "recalled_memory_role": "untrusted_user_evidence",
        },
        "control_results": control_results,
        "treatment_results": treatment_results,
        "analysis": analyze_results(control_results, treatment_results),
        "assertions": assertions,
        "passed": all(item["passed"] for item in assertions),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deterministic end-to-end Kestrel agent learning release gate"
    )
    parser.add_argument(
        "--provider",
        choices=("mock",),
        default="mock",
        help="Only deterministic mock mode is supported; use run_live_learning_eval.py for providers.",
    )
    parser.add_argument("--model", choices=("mock",), default="mock")
    parser.add_argument(
        "--output",
        default="benchmark_results/agent_learning_gate.json",
        help="Output path",
    )
    args = parser.parse_args()

    base_tmp = Path(tempfile.mkdtemp(prefix="kestrel_agent_learning_gate_"))
    try:
        report = run_learning_gate(base_tmp)
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, default=str))

        print("Deterministic end-to-end agent learning gate")
        for result in [*report["control_results"], *report["treatment_results"]]:
            status = "PASS" if result["success"] == result["expected_success"] else "FAIL"
            print(
                f"  [{status}] {result['condition']} {result['scenario_name']}: "
                f"success={result['success']} tools={result['tool_calls']}"
            )
        print("Assertions:")
        for assertion in report["assertions"]:
            status = "PASS" if assertion["passed"] else "FAIL"
            print(f"  [{status}] {assertion['name']}")
        print(f"Learning gate: {'PASS' if report['passed'] else 'FAIL'}")
        print(f"Results written to: {output_path}")
        if not report["passed"]:
            raise SystemExit(1)
    finally:
        shutil.rmtree(base_tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
