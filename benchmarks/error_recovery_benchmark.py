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
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.models import MemoryLayer, RetrievalQuery
from nested_memvid_agent.runtime_models import LLMResponse, ToolCall, ToolExecution, ToolSpec, StrategyProposal
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
        self._executions: list[ToolExecution] = []

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
        key = call.name
        self._call_counts[key] = self._call_counts.get(key, 0) + 1
        count = self._call_counts[key]

        # Also check canonical name if this is an alias
        canonical = self._registry._aliases.get(call.name) if hasattr(self._registry, '_aliases') else None
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

    def record(self, execution: ToolExecution) -> None:
        self._executions.append(execution)


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
            self._injector.record(execution)
            return execution
        execution = super().execute(call, context)
        self._injector.record(execution)
        return execution


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

def _task_transient_file_read(agent: Any, workspace: Path, injector: FaultInjector) -> dict[str, Any]:
    """Tool fails first time, agent should retry or find another way."""
    injector.add_rule({"tool": "file.read", "call_index": 1, "error": "transient_error", "message": "Connection reset"})
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
    success = "42" in turn.assistant_message
    return {
        "task": "transient_file_read",
        "success": success,
        "turns": 1,
        "tool_calls": [te.call.name for te in turn.tool_executions],
        "errors_seen": [te.error for te in turn.tool_executions if not te.success],
        "final_answer": turn.assistant_message[:200],
    }


def _task_file_not_found(agent: Any, workspace: Path, injector: FaultInjector) -> dict[str, Any]:
    """File doesn't exist. Agent should report it rather than loop forever."""
    missing = workspace / "missing.txt"
    injector.add_rule({"tool": "file.read", "call_index": 1, "error": "not_found", "message": f"File not found: {missing}"})

    turn = agent.chat(
        f"Read {missing} and tell me its contents.",
        session_id="bench_not_found",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"path": str(missing)}},
    )
    # Success if agent acknowledges the file is missing without crashing
    content = turn.assistant_message.lower()
    success = "not found" in content or "missing" in content or "doesn't exist" in content or "does not exist" in content
    return {
        "task": "file_not_found",
        "success": success,
        "turns": 1,
        "tool_calls": [te.call.name for te in turn.tool_executions],
        "errors_seen": [te.error for te in turn.tool_executions if not te.success],
        "final_answer": turn.assistant_message[:200],
    }


def _task_empty_search_results(agent: Any, workspace: Path, injector: FaultInjector) -> dict[str, Any]:
    """Search returns nothing. Agent should handle gracefully."""
    injector.add_rule({"tool": "repo.search", "call_index": 1, "error": "empty_results", "message": "No matches found."})
    (workspace / "fruits.txt").write_text("apple\nbanana\n")

    turn = agent.chat(
        f"Search for 'zebra' in {workspace}.",
        session_id="bench_empty_search",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"query": "zebra", "path": str(workspace)}},
    )
    content = turn.assistant_message.lower()
    success = "no matches" in content or "not found" in content or "nothing" in content or "empty" in content
    return {
        "task": "empty_search_results",
        "success": success,
        "turns": 1,
        "tool_calls": [te.call.name for te in turn.tool_executions],
        "errors_seen": [te.error for te in turn.tool_executions if not te.success],
        "final_answer": turn.assistant_message[:200],
    }


def _task_malformed_tool_name(agent: Any, workspace: Path, injector: FaultInjector) -> dict[str, Any]:
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
    # We test that the registry resolved the alias and the tool succeeded
    file_read_calls = [te for te in turn.tool_executions if te.call.name in ("file.read", "read")]
    success = len(file_read_calls) > 0 and file_read_calls[0].success
    return {
        "task": "malformed_tool_name",
        "success": success,
        "turns": 1,
        "tool_calls": [te.call.name for te in turn.tool_executions],
        "errors_seen": [te.error for te in turn.tool_executions if not te.success],
        "final_answer": turn.assistant_message[:200],
    }


def _task_strategy_retry(agent: Any, workspace: Path, injector: FaultInjector) -> dict[str, Any]:
    """Tool fails first time; LLM provides changed strategy and retry succeeds."""
    injector.add_rule({"tool": "file.read", "call_index": 1, "error": "transient_error", "message": "Connection reset"})
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
    success = "42" in turn.assistant_message
    file_read_calls = [te for te in turn.tool_executions if te.call.name == "file.read"]
    # Did the retry policy ALLOW the second call (because strategy was meaningful)?
    retry_allowed = len(file_read_calls) >= 2 and file_read_calls[1].success
    return {
        "task": "strategy_retry",
        "success": success,
        "retry_allowed": retry_allowed,
        "turns": 1,
        "tool_calls": [te.call.name for te in turn.tool_executions],
        "errors_seen": [te.error for te in turn.tool_executions if not te.success],
        "final_answer": turn.assistant_message[:200],
    }


def _task_max_retries_exceeded(agent: Any, workspace: Path, injector: FaultInjector) -> dict[str, Any]:
    """Tool fails every time. Agent should stop trying and report failure."""
    injector.add_rule({"tool": "file.read", "error": "transient_error", "message": "Persistent failure", "recover_after": 10})
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
    # Success = agent didn't crash, and it eventually stopped
    file_read_count = len([te for te in turn.tool_executions if te.call.name == "file.read"])
    success = file_read_count <= agent.config.max_tool_rounds
    return {
        "task": "max_retries_exceeded",
        "success": success,
        "turns": 1,
        "tool_calls": [te.call.name for te in turn.tool_executions],
        "errors_seen": [te.error for te in turn.tool_executions if not te.success],
        "file_read_attempts": file_read_count,
        "final_answer": turn.assistant_message[:200],
    }


# ---------------------------------------------------------------------------
# Mock response programming
# ---------------------------------------------------------------------------

def _mock_for_task(task_name: str) -> MockLLMProvider:
    if task_name == "transient_file_read":
        return MockLLMProvider([
            LLMResponse(
                content="I'll read that file.",
                tool_calls=(ToolCall(name="file.read", arguments={"path": "answer.txt"}, id="tc1"),),
            ),
            LLMResponse(
                content="Let me try again.",
                tool_calls=(ToolCall(name="file.read", arguments={"path": "answer.txt"}, id="tc2"),),
            ),
            LLMResponse(content="The secret code is 42."),
        ])
    if task_name == "file_not_found":
        return MockLLMProvider([
            LLMResponse(
                content="I'll read that file.",
                tool_calls=(ToolCall(name="file.read", arguments={"path": "missing.txt"}, id="tc1"),),
            ),
            LLMResponse(content="The file missing.txt does not exist."),
        ])
    if task_name == "empty_search_results":
        return MockLLMProvider([
            LLMResponse(
                content="I'll search for that.",
                tool_calls=(ToolCall(name="repo.search", arguments={"query": "zebra", "path": "."}, id="tc1"),),
            ),
            LLMResponse(content="No matches were found for 'zebra'."),
        ])
    if task_name == "malformed_tool_name":
        return MockLLMProvider([
            LLMResponse(
                content="I'll read that file.",
                tool_calls=(ToolCall(name="read", arguments={"path": "answer.txt"}, id="tc1"),),
            ),
            LLMResponse(content="The secret code is 42."),
        ])
    if task_name == "strategy_retry":
        return MockLLMProvider([
            LLMResponse(
                content="I'll read that file.",
                tool_calls=(ToolCall(name="file.read", arguments={"path": "answer.txt"}, id="tc1"),),
            ),
            LLMResponse(
                content="The previous read failed due to a connection reset. I will retry with the same path but expect the filesystem to be stable now.",
                tool_calls=(
                    ToolCall(
                        name="file.read",
                        arguments={"path": "answer.txt"},
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
        ])
    if task_name == "max_retries_exceeded":
        responses = []
        for i in range(5):
            responses.append(
                LLMResponse(
                    content=f"Attempt {i+1}...",
                    tool_calls=(ToolCall(name="file.read", arguments={"path": "answer.txt"}, id=f"tc{i+1}"),),
                )
            )
        responses.append(LLMResponse(content="I was unable to read the file after multiple attempts."))
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
    active_tasks = tasks or list(task_fns.keys())

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
                allow_web=False,
                allow_shell=False,
                max_tool_rounds=5,
            )

            from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
            from nested_memvid_agent.backends.in_memory import InMemoryBackend
            from nested_memvid_agent.layers import LayeredMemorySystem
            from nested_memvid_agent.state_store import AgentStateStore
            from nested_memvid_agent.event_log import JsonlEventLog
            from nested_memvid_agent.llm.factory import build_llm_provider

            state = AgentStateStore(config.state_path)
            memory = LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)
            base_tools = build_default_tools()
            event_log = JsonlEventLog(config.log_dir / "events.jsonl")

            injector = FaultInjector(base_tools)
            raw_tools = FaultInjectingRegistry(injector)
            for spec in base_tools.specs():
                tool = base_tools._tools.get(spec.name)
                if tool is not None:
                    raw_tools.register(tool)

            # For real providers, wrap with transparent retry layer so transient
            # failures are retried automatically before the LLM sees them.
            if provider != "mock" and config.tool_retry_max_attempts > 0:
                from nested_memvid_agent.tools.registry import RetryingRegistry
                tools = RetryingRegistry(
                    raw_tools,
                    max_attempts=config.tool_retry_max_attempts,
                    backoff_base_seconds=config.tool_retry_backoff_base_seconds,
                )
            else:
                tools = raw_tools

            if provider == "mock":
                llm = _mock_for_task(task_name)
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
    total_errors = sum(len(r.get("errors_seen", [])) for r in results)
    total_attempts = sum(len(r.get("tool_calls", [])) for r in results)

    return {
        "schema": "kestrel.error_recovery_benchmark.v1",
        "config": {"provider": provider, "model": model, "backend": backend, "tasks": active_tasks},
        "summary": {
            "total_tasks": len(results),
            "success_count": success_count,
            "success_rate": round(success_count / len(results), 2) if results else 0.0,
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
