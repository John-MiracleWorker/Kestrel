"""Error recovery & robustness benchmark for Kestrel.

Injects controlled failures into tool execution and measures the agent's
ability to retry, adapt, or fail gracefully.

Failure modes tested:
- transient_error: tool fails on first call, succeeds on retry
- not_found: file/resource missing, agent must report or work around
- empty_results: search returns nothing, agent must handle
- malformed_args: LLM sends bad arguments (registry catches this)
- timeout: tool hangs (simulated via short timeout + sleep)
- wrong_tool: LLM picks suboptimal tool, must recover

Usage:
    python benchmarks/error_recovery_benchmark.py --provider mock --output results/error_recovery.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.cognition.retry_policy import RetryPolicy
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.runtime_models import (
    LLMResponse,
    StrategyProposal,
    ToolCall,
    ToolExecution,
)
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry

# ---------------------------------------------------------------------------
# Fault-injecting tool wrapper
# ---------------------------------------------------------------------------


class FaultInjector:
    """Wraps a ToolRegistry to inject failures on specific call patterns."""

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry
        self._rules: list[dict[str, Any]] = []
        self._call_counts: dict[str, int] = {}
        self._requested_calls: list[ToolCall] = []
        self._executions: list[ToolExecution] = []
        self._injected_executions: list[ToolExecution] = []

    def add_rule(self, rule: dict[str, Any]) -> None:
        """Add a fault injection rule.

        Fields:
            tool: str | None — match tool name, or None for any
            call_index: int | None — match Nth call to this tool (1-based), or any
            error: str — error code to inject
            message: str — error message content
            data: dict | None — extra data
            recover_after: int — how many failures before letting through (default 1)
        """
        self._rules.append(rule)

    def wrap(self) -> ToolRegistry:
        """Return a ToolRegistry that routes through the injector."""
        wrapped = ToolRegistry()
        # Copy all tools from original registry
        for spec in self._registry.specs():
            tool = self._registry._tools.get(spec.name)
            if tool is not None:
                wrapped.register(_FaultInjectedTool(tool, self))
        return wrapped

    def should_inject(self, call: ToolCall) -> dict[str, Any] | None:
        self._requested_calls.append(call)
        key = call.name
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        count = self._call_counts[key]

        # Also check canonical name if this is an alias
        canonical = (
            self._registry._aliases.get(call.name) if hasattr(self._registry, "_aliases") else None
        )
        names_to_check = {key}
        if canonical:
            names_to_check.add(canonical)
            self._call_counts[canonical] = self._call_counts.get(canonical, 0) + 1
            count = self._call_counts[canonical]

        for rule in self._rules:
            if rule.get("tool") is not None and rule["tool"] not in names_to_check:
                continue
            if rule.get("call_index") is not None and rule["call_index"] != count:
                continue
            recover_after = rule.get("recover_after", 1)
            # Only inject if we haven't exceeded the failure count
            if count <= recover_after:
                return rule
        return None

    def record(self, execution: ToolExecution, *, injected: bool = False) -> None:
        self._executions.append(execution)
        if injected:
            self._injected_executions.append(execution)

    @property
    def executions(self) -> tuple[ToolExecution, ...]:
        return tuple(self._executions)

    @property
    def requested_calls(self) -> tuple[ToolCall, ...]:
        return tuple(self._requested_calls)

    @property
    def injected_executions(self) -> tuple[ToolExecution, ...]:
        return tuple(self._injected_executions)


class _FaultInjectedTool(AgentTool):
    def __init__(self, inner: AgentTool, injector: FaultInjector) -> None:
        self._inner = inner
        self.spec = inner.spec
        self._injector = injector

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        # We need the ToolCall but only have arguments here.
        # The registry passes arguments after stripping/re-adding call id.
        # To work around, we intercept at registry level instead.
        return self._inner.run(arguments, context)

    def cancel(self, call_id: str) -> None:
        self._inner.cancel(call_id)


class FaultInjectingRegistry(ToolRegistry):
    """A ToolRegistry that checks the injector before delegating."""

    def __init__(self, injector: FaultInjector) -> None:
        super().__init__()
        self._injector = injector

    def execute(self, call: ToolCall, context: ToolContext) -> ToolExecution:
        rule = self._injector.should_inject(call)
        if rule is not None:
            execution = ToolExecution(
                call=call,
                success=False,
                content=rule.get("message", "Injected failure"),
                error=rule["error"],
                data=rule.get("data") or {},
            )
            self._injector.record(execution, injected=True)
            return execution
        execution = super().execute(call, context)
        self._injector.record(execution)
        return execution


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


def _tool_attempts(injector: FaultInjector, name: str) -> list[ToolExecution]:
    return [
        execution
        for execution in injector.executions
        if execution.call.name == name
        or injector._registry.canonical_name(execution.call.name) == name
    ]


def _requested_tool_calls(injector: FaultInjector, name: str) -> list[ToolCall]:
    return [
        call
        for call in injector.requested_calls
        if call.name == name or injector._registry.canonical_name(call.name) == name
    ]


def _injected_failures(
    injector: FaultInjector,
    *,
    name: str,
    error: str,
) -> list[ToolExecution]:
    return [
        execution
        for execution in injector.injected_executions
        if execution.call.name == name and not execution.success and execution.error == error
    ]


def _read_succeeded(
    execution: ToolExecution,
    *,
    expected_path: Path,
) -> bool:
    return bool(
        execution.success
        and execution.call.arguments.get("path") == str(expected_path)
        and execution.data.get("path") == str(expected_path.resolve())
    )


def _task_transient_file_read(
    agent: Any, workspace: Path, injector: FaultInjector
) -> dict[str, Any]:
    """Tool fails first time, agent should retry or find another way."""
    injector.add_rule(
        {
            "tool": "file.read",
            "call_index": 1,
            "error": "transient_error",
            "message": "Connection reset",
        }
    )
    answer_file = workspace / "answer.txt"
    answer_file.write_text("The secret code is 42.")

    turn = agent.chat(
        f"Read {answer_file} and tell me the secret code.",
        session_id="bench_transient",
        approved_tool_call_ids=frozenset({"tc1", "tc2"}),
        approved_tool_call_arguments={
            "tc1": {"path": str(answer_file)},
            "tc2": {"path": str(answer_file)},
        },
    )
    attempts = _tool_attempts(injector, "file.read")
    injected = _injected_failures(
        injector,
        name="file.read",
        error="transient_error",
    )
    injected = [
        execution
        for execution in injected
        if execution.call.arguments == {"path": str(answer_file)}
    ]
    successful_reads = [
        execution
        for execution in attempts
        if _read_succeeded(execution, expected_path=answer_file) and "42" in execution.content
    ]
    recovered_after_injection = bool(
        injected
        and successful_reads
        and attempts.index(injected[0]) < attempts.index(successful_reads[-1])
    )
    semantic_answer = re.search(r"\b42\b", turn.assistant_message) is not None
    success = recovered_after_injection and semantic_answer
    return {
        "task": "transient_file_read",
        "success": success,
        "turns": 1,
        "tool_calls": [execution.call.name for execution in attempts],
        "errors_seen": [execution.error for execution in attempts if not execution.success],
        "injected_errors": [execution.error for execution in injected],
        "recovered_after_injection": recovered_after_injection,
        "semantic_answer": semantic_answer,
        "final_answer": turn.assistant_message[:200],
    }


def _task_file_not_found(agent: Any, workspace: Path, injector: FaultInjector) -> dict[str, Any]:
    """File doesn't exist. Agent should report it rather than loop forever."""
    missing = workspace / "missing.txt"
    injector.add_rule(
        {
            "tool": "file.read",
            "call_index": 1,
            "error": "not_found",
            "message": f"File not found: {missing}",
        }
    )

    turn = agent.chat(
        f"Read {missing} and tell me its contents.",
        session_id="bench_not_found",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"path": str(missing)}},
    )
    attempts = _tool_attempts(injector, "file.read")
    injected = _injected_failures(injector, name="file.read", error="not_found")
    exact_injected_call = any(
        execution.call.arguments == {"path": str(missing)} for execution in injected
    )
    # Success requires the injected failure itself plus an accurate explanation.
    content = turn.assistant_message.lower()
    semantic_answer = any(
        phrase in content for phrase in ("not found", "missing", "doesn't exist", "does not exist")
    )
    success = exact_injected_call and semantic_answer
    return {
        "task": "file_not_found",
        "success": success,
        "turns": 1,
        "tool_calls": [execution.call.name for execution in attempts],
        "errors_seen": [execution.error for execution in attempts if not execution.success],
        "injected_errors": [execution.error for execution in injected],
        "exact_injected_call": exact_injected_call,
        "semantic_answer": semantic_answer,
        "final_answer": turn.assistant_message[:200],
    }


def _task_empty_search_results(
    agent: Any, workspace: Path, injector: FaultInjector
) -> dict[str, Any]:
    """Search returns nothing. Agent should handle gracefully."""
    injector.add_rule(
        {
            "tool": "repo.search",
            "call_index": 1,
            "error": "empty_results",
            "message": "No matches found.",
        }
    )
    (workspace / "fruits.txt").write_text("apple\nbanana\n")

    turn = agent.chat(
        f"Search for 'zebra' in {workspace}.",
        session_id="bench_empty_search",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"query": "zebra", "path": str(workspace)}},
    )
    attempts = _tool_attempts(injector, "repo.search")
    injected = _injected_failures(
        injector,
        name="repo.search",
        error="empty_results",
    )
    exact_injected_call = any(
        execution.call.arguments == {"query": "zebra", "path": str(workspace)}
        for execution in injected
    )
    content = turn.assistant_message.lower()
    semantic_answer = any(
        phrase in content for phrase in ("no matches", "not found", "nothing", "empty")
    )
    success = exact_injected_call and semantic_answer
    return {
        "task": "empty_search_results",
        "success": success,
        "turns": 1,
        "tool_calls": [execution.call.name for execution in attempts],
        "errors_seen": [execution.error for execution in attempts if not execution.success],
        "injected_errors": [execution.error for execution in injected],
        "exact_injected_call": exact_injected_call,
        "semantic_answer": semantic_answer,
        "final_answer": turn.assistant_message[:200],
    }


def _task_malformed_tool_name(
    agent: Any, workspace: Path, injector: FaultInjector
) -> dict[str, Any]:
    """LLM hallucinates a tool name. Registry should resolve alias or fail cleanly."""
    answer_file = workspace / "answer.txt"
    answer_file.write_text("The secret code is 42.")

    # No injection needed — we use mock LLM that sends alias 'read' instead of 'file.read'
    turn = agent.chat(
        f"Read {answer_file} and tell me the secret code.",
        session_id="bench_malformed_name",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"path": str(answer_file)}},
    )
    # Prove that the alias resolved to the exact canonical read and that the
    # answer reflects the observed tool result.
    attempts = _tool_attempts(injector, "file.read")
    exact_read = any(
        _read_succeeded(execution, expected_path=answer_file) and "42" in execution.content
        for execution in attempts
    )
    semantic_answer = re.search(r"\b42\b", turn.assistant_message) is not None
    success = exact_read and semantic_answer
    return {
        "task": "malformed_tool_name",
        "success": success,
        "turns": 1,
        "tool_calls": [execution.call.name for execution in attempts],
        "errors_seen": [execution.error for execution in attempts if not execution.success],
        "injected_errors": [],
        "expected_tool_succeeded": exact_read,
        "semantic_answer": semantic_answer,
        "final_answer": turn.assistant_message[:200],
    }


def _task_strategy_retry(agent: Any, workspace: Path, injector: FaultInjector) -> dict[str, Any]:
    """Tool fails first time; LLM provides changed strategy and retry succeeds."""
    injector.add_rule(
        {
            "tool": "file.read",
            "call_index": 1,
            "error": "transient_error",
            "message": "Connection reset",
        }
    )
    answer_file = workspace / "answer.txt"
    answer_file.write_text("The secret code is 42.")

    turn = agent.chat(
        f"Read {answer_file} and tell me the secret code.",
        session_id="bench_strategy_retry",
        approved_tool_call_ids=frozenset({"tc1", "tc2"}),
        approved_tool_call_arguments={
            "tc1": {"path": str(answer_file)},
            "tc2": {"path": str(answer_file)},
        },
    )
    attempts = _tool_attempts(injector, "file.read")
    injected = _injected_failures(
        injector,
        name="file.read",
        error="transient_error",
    )
    injected = [
        execution
        for execution in injected
        if execution.call.arguments == {"path": str(answer_file)}
    ]
    successful_reads = [
        execution
        for execution in attempts
        if _read_succeeded(execution, expected_path=answer_file) and "42" in execution.content
    ]
    requested_reads = _requested_tool_calls(injector, "file.read")
    retry_decision = (
        RetryPolicy().assess_call(requested_reads[1], [injected[0]])
        if injected and successful_reads and len(requested_reads) >= 2
        else None
    )
    retry_allowed = bool(retry_decision is not None and retry_decision.retry_allowed)
    recovered_after_injection = bool(
        injected
        and successful_reads
        and attempts.index(injected[0]) < attempts.index(successful_reads[0])
    )
    semantic_answer = re.search(r"\b42\b", turn.assistant_message) is not None
    success = recovered_after_injection and retry_allowed and semantic_answer
    return {
        "task": "strategy_retry",
        "success": success,
        "retry_allowed": retry_allowed,
        "turns": 1,
        "tool_calls": [execution.call.name for execution in attempts],
        "errors_seen": [execution.error for execution in attempts if not execution.success],
        "injected_errors": [execution.error for execution in injected],
        "retry_decision": retry_decision.to_payload() if retry_decision is not None else None,
        "recovered_after_injection": recovered_after_injection,
        "semantic_answer": semantic_answer,
        "final_answer": turn.assistant_message[:200],
    }


def _task_max_retries_exceeded(
    agent: Any, workspace: Path, injector: FaultInjector
) -> dict[str, Any]:
    """Tool fails every time. Agent should stop trying and report failure."""
    injector.add_rule(
        {
            "tool": "file.read",
            "error": "transient_error",
            "message": "Persistent failure",
            "recover_after": 10,
        }
    )
    answer_file = workspace / "answer.txt"
    answer_file.write_text("The secret code is 42.")

    turn = agent.chat(
        f"Read {answer_file} and tell me the secret code.",
        session_id="bench_max_retries",
        approved_tool_call_ids=frozenset({"tc1", "tc2", "tc3", "tc4", "tc5"}),
        approved_tool_call_arguments={
            "tc1": {"path": str(answer_file)},
            "tc2": {"path": str(answer_file)},
            "tc3": {"path": str(answer_file)},
            "tc4": {"path": str(answer_file)},
            "tc5": {"path": str(answer_file)},
        },
    )
    attempts = _tool_attempts(injector, "file.read")
    injected = _injected_failures(
        injector,
        name="file.read",
        error="transient_error",
    )
    injected = [
        execution
        for execution in injected
        if execution.call.arguments == {"path": str(answer_file)}
    ]
    file_read_count = len(attempts)
    nonzero_bounded_failures = bool(
        0 < file_read_count <= agent.config.max_tool_rounds
        and len(injected) == file_read_count
        and all(not execution.success for execution in attempts)
    )
    content = turn.assistant_message.lower()
    terminal_failure_reported = any(
        phrase in content for phrase in ("unable", "could not", "couldn't", "failed", "failure")
    )
    success = nonzero_bounded_failures and terminal_failure_reported
    return {
        "task": "max_retries_exceeded",
        "success": success,
        "turns": 1,
        "tool_calls": [execution.call.name for execution in attempts],
        "errors_seen": [execution.error for execution in attempts if not execution.success],
        "injected_errors": [execution.error for execution in injected],
        "file_read_attempts": file_read_count,
        "max_tool_rounds": agent.config.max_tool_rounds,
        "nonzero_bounded_failures": nonzero_bounded_failures,
        "terminal_failure_reported": terminal_failure_reported,
        "final_answer": turn.assistant_message[:200],
    }


# ---------------------------------------------------------------------------
# Mock response programming
# ---------------------------------------------------------------------------


def _mock_for_task(task_name: str, workspace: Path) -> MockLLMProvider:
    answer_file = workspace / "answer.txt"
    missing_file = workspace / "missing.txt"
    if task_name == "transient_file_read":
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll read that file.",
                    tool_calls=(
                        ToolCall(
                            name="file.read",
                            arguments={"path": str(answer_file)},
                            id="tc1",
                        ),
                    ),
                ),
                LLMResponse(
                    content="Let me try again.",
                    tool_calls=(
                        ToolCall(
                            name="file.read",
                            arguments={"path": str(answer_file), "max_chars": 1_000},
                            id="tc2",
                        ),
                    ),
                ),
                LLMResponse(content="The secret code is 42."),
            ]
        )
    if task_name == "file_not_found":
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll read that file.",
                    tool_calls=(
                        ToolCall(
                            name="file.read",
                            arguments={"path": str(missing_file)},
                            id="tc1",
                        ),
                    ),
                ),
                LLMResponse(content="The file missing.txt does not exist."),
            ]
        )
    if task_name == "empty_search_results":
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll search for that.",
                    tool_calls=(
                        ToolCall(
                            name="repo.search",
                            arguments={"query": "zebra", "path": str(workspace)},
                            id="tc1",
                        ),
                    ),
                ),
                LLMResponse(content="No matches were found for 'zebra'."),
            ]
        )
    if task_name == "malformed_tool_name":
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll read that file.",
                    tool_calls=(
                        ToolCall(
                            name="read",
                            arguments={"path": str(answer_file)},
                            id="tc1",
                        ),
                    ),
                ),
                LLMResponse(content="The secret code is 42."),
            ]
        )
    if task_name == "strategy_retry":
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll read that file.",
                    tool_calls=(
                        ToolCall(
                            name="file.read",
                            arguments={"path": str(answer_file)},
                            id="tc1",
                        ),
                    ),
                ),
                LLMResponse(
                    content="The previous read failed due to a connection reset. I will retry with the same path but expect the filesystem to be stable now.",
                    tool_calls=(
                        ToolCall(
                            name="file.read",
                            arguments={"path": str(answer_file)},
                            id="tc2",
                            strategy=StrategyProposal(
                                changed_strategy="Retry the file read assuming the transient connection issue has cleared.",
                                why_different="The first failure was a connection reset, which is typically transient.",
                                expected_signal="File contents returned successfully.",
                                fallback_if_fails="Report the file as unreadable.",
                            ),
                        ),
                    ),
                ),
                LLMResponse(content="The secret code is 42."),
            ]
        )
    if task_name == "max_retries_exceeded":
        responses = []
        for i in range(5):
            strategy = None
            if i > 0:
                strategy = StrategyProposal(
                    changed_strategy=(
                        f"Attempt bounded diagnostic read number {i + 1} after checking "
                        "the persistent failure evidence."
                    ),
                    why_different="This attempt follows a newly observed bounded failure.",
                    expected_signal="The exact file contents are returned.",
                    fallback_if_fails="Stop at the configured tool-round boundary.",
                )
            responses.append(
                LLMResponse(
                    content=f"Attempt {i + 1}...",
                    tool_calls=(
                        ToolCall(
                            name="file.read",
                            arguments={"path": str(answer_file)},
                            id=f"tc{i + 1}",
                            strategy=strategy,
                        ),
                    ),
                )
            )
        responses.append(
            LLMResponse(content="I was unable to read the file after multiple attempts.")
        )
        return MockLLMProvider(responses)
    return MockLLMProvider([LLMResponse(content="I don't know.")])


# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------


def run_error_recovery_benchmark(
    *,
    provider: str = "mock",
    model: str = "mock",
    base_url: str | None = None,
    api_key_env: str | None = None,
    backend: str = "memory",
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    task_fns = {
        "transient_file_read": _task_transient_file_read,
        "file_not_found": _task_file_not_found,
        "empty_search_results": _task_empty_search_results,
        "malformed_tool_name": _task_malformed_tool_name,
        "strategy_retry": _task_strategy_retry,
        "max_retries_exceeded": _task_max_retries_exceeded,
    }
    active_tasks = list(task_fns) if tasks is None else list(tasks)

    results = []
    total_time = 0.0
    for task_name in active_tasks:
        with tempfile.TemporaryDirectory(prefix=f"kestrel-error-bench-{task_name}-") as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            memory_dir = Path(tmpdir) / "memory"
            memory_dir.mkdir()

            config = AgentConfig(
                provider=provider,
                model=model,
                base_url=base_url,
                api_key_env=api_key_env,
                backend=backend,
                memory_dir=memory_dir,
                workspace=workspace,
                log_dir=Path(tmpdir) / "logs",
                state_path=Path(tmpdir) / "state" / "agent.db",
                secret_store_path=Path(tmpdir) / "secrets" / "local_vault.json",
                skills_dir=Path(tmpdir) / "skills",
                plugins_dir=Path(tmpdir) / "plugins",
                mcp_config_path=Path(tmpdir) / "config" / "mcp_servers.json",
                channel_config_path=Path(tmpdir) / "config" / "channels.json",
                worker_worktree_dir=Path(tmpdir) / "worktrees",
                allow_web=False,
                allow_shell=False,
                max_tool_rounds=5,
            )

            from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
            from nested_memvid_agent.backends.in_memory import InMemoryBackend
            from nested_memvid_agent.event_log import JsonlEventLog
            from nested_memvid_agent.layers import LayeredMemorySystem
            from nested_memvid_agent.llm.factory import build_llm_provider

            memory = LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)
            base_tools = build_default_tools()
            event_log = JsonlEventLog(config.log_dir / "events.jsonl")

            injector = FaultInjector(base_tools)
            raw_tools = FaultInjectingRegistry(injector)
            for spec in base_tools.specs():
                tool = base_tools._tools.get(spec.name)
                if tool is not None:
                    raw_tools.register(tool)

            # Real-provider transient recovery exercises the transparent retry
            # layer. Strategy and maximum-round tasks must expose each failure
            # to the model so their explicit retry-policy evidence is possible.
            use_transparent_retry = (
                provider != "mock"
                and config.tool_retry_max_attempts > 0
                and task_name not in {"strategy_retry", "max_retries_exceeded"}
            )
            if use_transparent_retry:
                from nested_memvid_agent.tools.registry import RetryingRegistry

                tools = RetryingRegistry(
                    raw_tools,
                    max_attempts=config.tool_retry_max_attempts,
                    backoff_base_seconds=config.tool_retry_backoff_base_seconds,
                )
            else:
                tools = raw_tools

            if provider == "mock":
                llm = _mock_for_task(task_name, workspace)
            else:
                llm = build_llm_provider(config)

            agent = NestedMV2Agent(
                AgentDependencies(
                    memory=memory,
                    llm=llm,
                    tools=tools,
                    config=config,
                    event_log=event_log,
                )
            )

            t0 = time.perf_counter()
            try:
                result = task_fns[task_name](agent, workspace, injector)
            except Exception as exc:
                result = {
                    "task": task_name,
                    "success": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            t1 = time.perf_counter()
            result["elapsed_ms"] = round((t1 - t0) * 1000, 2)
            total_time += t1 - t0
            results.append(result)

    success_count = sum(1 for r in results if r.get("success"))
    total_errors = sum(len(r.get("injected_errors", [])) for r in results)
    total_attempts = sum(len(r.get("tool_calls", [])) for r in results)
    passed = bool(results) and success_count == len(results)

    return {
        "schema": "kestrel.error_recovery_benchmark.v1",
        "config": {
            "provider": provider,
            "model": model,
            "backend": "in_memory",
            "requested_backend": backend,
            "tasks": active_tasks,
        },
        "summary": {
            "total_tasks": len(results),
            "success_count": success_count,
            "success_rate": round(success_count / len(results), 2) if results else 0.0,
            "passed": passed,
            "total_errors_injected": total_errors,
            "total_tool_attempts": total_attempts,
            "avg_tools_per_task": round(total_attempts / len(results), 2) if results else 0.0,
            "total_elapsed_ms": round(total_time * 1000, 2),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Kestrel error recovery benchmark.")
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--model", default="mock")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--backend", default="memory")
    parser.add_argument("--tasks", nargs="+", help="Subset of tasks to run")
    parser.add_argument("--output", type=Path, help="JSON output path")
    args = parser.parse_args()

    result = run_error_recovery_benchmark(
        provider=args.provider,
        model=args.model,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
        backend=args.backend,
        tasks=args.tasks,
    )
    print(json.dumps(result, indent=2))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2))
        print(f"\nWrote results to {args.output}", file=sys.stderr)
    summary = result.get("summary", {})
    total_tasks = int(summary.get("total_tasks", 0))
    success_count = int(summary.get("success_count", 0))
    rows = result.get("results", [])
    passed = bool(
        rows
        and len(rows) == total_tasks
        and success_count == total_tasks
        and all(bool(row.get("success")) for row in rows)
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
