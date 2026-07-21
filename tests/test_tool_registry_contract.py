from __future__ import annotations

from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace

import pytest

import nested_memvid_agent.tools.registry as registry_module
from nested_memvid_agent.agent import _tool_context_with_preflight
from nested_memvid_agent.behavior_compiler import CompiledBehavior
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.parser import parse_agent_response
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.registry import RetryingRegistry, RuntimeToolFence, ToolRegistry


class ContractSlowTool(AgentTool):
    spec = ToolSpec(
        name="contract.slow",
        description="Sleeps longer than the configured timeout.",
        parameters={"type": "object", "properties": {}},
    )

    def __init__(self) -> None:
        self.cancelled_call_ids: list[str] = []

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        sleep(0.2)
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=arguments), success=True, content="late"
        )

    def cancel(self, call_id: str) -> None:
        self.cancelled_call_ids.append(call_id)


def test_agent_tool_has_noop_cancel_contract() -> None:
    class MinimalTool(AgentTool):
        spec = ToolSpec(name="minimal", description="Minimal tool.", parameters={"type": "object"})

        def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=arguments), success=True, content="ok"
            )

    MinimalTool().cancel("call-id")


def test_behavior_preflight_context_copy_preserves_execution_origin(tmp_path: Path) -> None:
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / "preflight-copy-memory"),
        config=AgentConfig(),
        workspace=tmp_path,
        execution_origin="subagent:durable-worker",
    )

    copied = _tool_context_with_preflight(
        context,
        CompiledBehavior(text="bounded preflight", deltas=()),
    )

    assert copied.execution_origin == "subagent:durable-worker"


def test_tool_registry_rejects_cyclic_direct_arguments_without_dispatch(tmp_path: Path) -> None:
    calls = 0

    class CountingTool(AgentTool):
        spec = ToolSpec(
            name="contract.json-only",
            description="Accept only finite JSON arguments.",
            parameters={"type": "object"},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments, context
            nonlocal calls
            calls += 1
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments={}),
                success=True,
                content="unexpected",
            )

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    registry = ToolRegistry()
    registry.register(CountingTool())

    result = registry.execute(
        ToolCall(name="contract.json-only", arguments=cyclic, id="cyclic-call"),
        ToolContext(
            memory=build_memory_system("memory", tmp_path / "cyclic-memory"),
            config=AgentConfig(),
            workspace=tmp_path,
        ),
    )

    assert result.error == "invalid_tool_arguments"
    assert result.data == {}
    assert calls == 0


def test_interrupted_wait_quarantines_live_worker_and_retains_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = Event()
    release_worker = Event()

    class InterruptingQueue:
        def __init__(self, maxsize: int = 0) -> None:
            del maxsize
            self.items: list[ToolExecution] = []

        def put(self, item: ToolExecution) -> None:
            self.items.append(item)

        def get(self, *, timeout: float) -> ToolExecution:
            del timeout
            assert worker_started.wait(timeout=1.0)
            raise KeyboardInterrupt

    class InterruptedTool(AgentTool):
        spec = ToolSpec(
            name="contract.interrupted-wait",
            description="Remain live after the caller wait is interrupted.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments, context
            worker_started.set()
            assert release_worker.wait(timeout=2.0)
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments={}),
                success=True,
                content="settled late",
            )

    monkeypatch.setattr(registry_module, "Queue", InterruptingQueue)
    runtime_fence = RuntimeToolFence()
    memory = build_memory_system("memory", tmp_path / "interrupted-memory")
    registry = ToolRegistry(runtime_fence=runtime_fence)
    registry.register(InterruptedTool())
    context = ToolContext(
        memory=memory,
        config=AgentConfig(tool_timeout_seconds=1.0),
        workspace=tmp_path,
        run_id="run-interrupted",
    )
    call = ToolCall(name="contract.interrupted-wait", arguments={}, id="call-interrupted")

    with pytest.raises(KeyboardInterrupt):
        registry.execute(call, context)

    assert memory.has_unsettled_tool_executions()
    fresh = ToolRegistry(runtime_fence=runtime_fence)
    fresh.register(InterruptedTool())
    quarantined = fresh.execute(call, context)
    assert quarantined.error == "tool_quarantined_after_unresolved_outcome"

    release_worker.set()
    deadline = monotonic() + 1.0
    while memory.has_unsettled_tool_executions() and monotonic() < deadline:
        sleep(0.01)
    assert not memory.has_unsettled_tool_executions()


def test_interrupted_worker_start_quarantines_and_retains_started_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_started = Event()
    release_worker = Event()

    class StartInterruptedTool(AgentTool):
        spec = ToolSpec(
            name="contract.interrupted-start",
            description="Start successfully before the caller sees an interrupt.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments, context
            worker_started.set()
            assert release_worker.wait(timeout=2.0)
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments={}),
                success=True,
                content="settled late",
            )

    real_start = registry_module.Thread.start
    interrupt_injected = False

    def start_then_interrupt(thread: Thread) -> None:
        nonlocal interrupt_injected
        real_start(thread)
        if not interrupt_injected:
            interrupt_injected = True
            assert worker_started.wait(timeout=1.0)
            raise KeyboardInterrupt

    monkeypatch.setattr(registry_module.Thread, "start", start_then_interrupt)
    runtime_fence = RuntimeToolFence()
    memory = build_memory_system("memory", tmp_path / "interrupted-start-memory")
    registry = ToolRegistry(runtime_fence=runtime_fence)
    registry.register(StartInterruptedTool())
    context = ToolContext(
        memory=memory,
        config=AgentConfig(tool_timeout_seconds=1.0),
        workspace=tmp_path,
        run_id="run-interrupted-start",
    )
    call = ToolCall(name="contract.interrupted-start", arguments={}, id="call-start")

    with pytest.raises(KeyboardInterrupt):
        registry.execute(call, context)

    assert memory.has_unsettled_tool_executions()
    fresh = ToolRegistry(runtime_fence=runtime_fence)
    fresh.register(StartInterruptedTool())
    quarantined = fresh.execute(call, context)
    assert quarantined.error == "tool_quarantined_after_unresolved_outcome"

    release_worker.set()
    deadline = monotonic() + 1.0
    while memory.has_unsettled_tool_executions() and monotonic() < deadline:
        sleep(0.01)
    assert not memory.has_unsettled_tool_executions()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 0.0, 3_600.1])
def test_agent_config_rejects_unbounded_tool_timeouts(value: float) -> None:
    with pytest.raises(ValueError, match="tool_timeout_seconds"):
        AgentConfig(tool_timeout_seconds=value)


def test_registry_defense_in_depth_rejects_nonfinite_timeout(tmp_path: Path) -> None:
    class MustNotRunTool(AgentTool):
        spec = ToolSpec(
            name="contract.invalid-timeout",
            description="Must not start under invalid timeout configuration.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            raise AssertionError("tool worker started with invalid timeout")

    registry = ToolRegistry()
    registry.register(MustNotRunTool())
    result = registry.execute(
        ToolCall(name="contract.invalid-timeout", arguments={}),
        ToolContext(
            memory=build_memory_system("memory", tmp_path / "invalid-timeout-memory"),
            config=SimpleNamespace(tool_timeout_seconds=float("nan")),  # type: ignore[arg-type]
            workspace=tmp_path,
        ),
    )
    assert result.error == "invalid_tool_timeout"
    assert result.data["retryable"] is False


def test_tool_registry_calls_cancel_on_timeout(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = ToolRegistry()
    tool = ContractSlowTool()
    registry.register(tool)

    result = registry.execute(
        ToolCall(name="contract.slow", arguments={}, id="slow-call"),
        ToolContext(
            memory=memory, config=AgentConfig(tool_timeout_seconds=0.01), workspace=tmp_path
        ),
    )

    assert result.success is True
    assert result.error is None
    assert result.data["tool_deadline_exceeded"] is True
    assert result.data["tool_timeout_seconds"] == 0.01
    assert tool.cancelled_call_ids == ["slow-call"]


def test_tool_registry_noop_cancel_cannot_wait_forever_or_invite_retry(
    tmp_path: Path,
) -> None:
    never_return = Event()
    call_count = 0

    class PermanentlyBlockedTool(AgentTool):
        spec = ToolSpec(
            name="contract.blocked",
            description="Never returns and inherits the no-op cancellation hook.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments, context
            nonlocal call_count
            call_count += 1
            never_return.wait()
            raise AssertionError("unreachable")

    runtime_fence = RuntimeToolFence()
    inner = ToolRegistry(runtime_fence=runtime_fence)
    inner.register(PermanentlyBlockedTool())
    registry = RetryingRegistry(inner, max_attempts=3, backoff_base_seconds=0)
    memory = build_memory_system("memory", tmp_path / "memory")
    started = monotonic()

    result = registry.execute(
        ToolCall(name="contract.blocked", arguments={}, id="blocked-call"),
        ToolContext(
            memory=memory,
            config=AgentConfig(tool_timeout_seconds=0.01),
            workspace=tmp_path,
            execution_origin="subagent:a",
        ),
    )

    assert monotonic() - started < 1.0
    assert result.success is False
    assert result.error == "tool_outcome_unresolved"
    assert result.data["outcome_indeterminate"] is True
    assert result.data["retryable"] is False
    assert result.data["reconciliation_required"] is True
    assert result.data["execution_may_still_be_running"] is True
    assert call_count == 1

    fresh_registry = ToolRegistry(runtime_fence=runtime_fence)
    fresh_registry.register(PermanentlyBlockedTool())
    quarantined = fresh_registry.execute(
        ToolCall(name="contract.blocked", arguments={}, id="blocked-call-2"),
        ToolContext(
            memory=build_memory_system("memory", tmp_path / "memory-2"),
            config=AgentConfig(tool_timeout_seconds=0.01),
            workspace=tmp_path,
            execution_origin="subagent:b",
        ),
    )
    assert quarantined.error == "tool_quarantined_after_unresolved_outcome"
    assert quarantined.data["tool_quarantined"] is True
    assert quarantined.data["retryable"] is False
    assert quarantined.data["reconciliation_required"] is True
    assert call_count == 1

    class IndependentRuntimeTool(AgentTool):
        spec = PermanentlyBlockedTool.spec

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del context
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=arguments),
                success=True,
                content="independent runtime",
            )

    independent = ToolRegistry()
    independent.register(IndependentRuntimeTool())
    isolated = independent.execute(
        ToolCall(name="contract.blocked", arguments={}, id="independent-call"),
        ToolContext(
            memory=build_memory_system("memory", tmp_path / "independent-memory"),
            config=AgentConfig(tool_timeout_seconds=0.1),
            workspace=tmp_path,
        ),
    )
    assert isolated.success is True


def test_tool_registry_atomically_fences_exact_concurrent_call_runtime_wide(
    tmp_path: Path,
) -> None:
    release = Event()
    started = Event()
    results: list[ToolExecution] = []
    call_count = 0

    class ConcurrentBlockedTool(AgentTool):
        spec = ToolSpec(
            name="contract.concurrent-blocked",
            description="Blocks so a concurrent duplicate can be tested.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments, context
            nonlocal call_count
            call_count += 1
            started.set()
            release.wait()
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments={}),
                success=True,
                content="settled",
            )

    runtime_fence = RuntimeToolFence()
    first = ToolRegistry(runtime_fence=runtime_fence)
    first.register(ConcurrentBlockedTool())
    second = ToolRegistry(runtime_fence=runtime_fence)
    second.register(ConcurrentBlockedTool())
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / "concurrent-memory"),
        config=AgentConfig(tool_timeout_seconds=1.0),
        workspace=tmp_path,
    )
    thread = Thread(
        target=lambda: results.append(
            first.execute(
                ToolCall(name="contract.concurrent-blocked", arguments={}, id="same-call"),
                context,
            )
        )
    )
    thread.start()
    assert started.wait(timeout=1.0)

    duplicate = second.execute(
        ToolCall(name="contract.concurrent-blocked", arguments={}, id="same-call"),
        context,
    )
    assert duplicate.error == "tool_execution_in_progress"
    assert duplicate.data["tool_execution_in_progress"] is True
    assert duplicate.data["retryable"] is False
    assert call_count == 1

    release.set()
    thread.join(timeout=1.0)
    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0].success is True


def test_runtime_fence_allows_distinct_calls_from_parallel_subagents_in_one_run(
    tmp_path: Path,
) -> None:
    both_started = Event()
    release = Event()
    starts = 0
    starts_lock = Lock()

    class ParallelSubagentReadTool(AgentTool):
        spec = ToolSpec(
            name="contract.parallel-subagent-read",
            description="Independent calls in one parent run execute concurrently.",
            parameters={
                "type": "object",
                "properties": {"branch": {"type": "string"}},
                "required": ["branch"],
            },
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            nonlocal starts
            with starts_lock:
                starts += 1
                if starts == 2:
                    both_started.set()
            assert release.wait(timeout=1.0)
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments={}),
                success=True,
                content=f"{context.run_id}:{arguments['branch']}",
            )

    runtime_fence = RuntimeToolFence()
    registries = [
        ToolRegistry(runtime_fence=runtime_fence),
        ToolRegistry(runtime_fence=runtime_fence),
    ]
    for registry in registries:
        registry.register(ParallelSubagentReadTool())
    generated_calls = [
        parse_agent_response(
            (
                '{"message":"","tool_calls":['
                '{"name":"contract.parallel-subagent-read",'
                f'"arguments":{{"branch":"{branch}"}}}}]}}'
            ),
            tools=[ParallelSubagentReadTool.spec],
            strict=True,
        ).tool_calls[0]
        for branch in ("a", "b")
    ]
    assert generated_calls[0].id != generated_calls[1].id
    # Some native/local providers may explicitly recycle a counter-based ID in
    # sibling subagents, including for the same tool and arguments.
    calls = [
        ToolCall(
            name=call.name,
            arguments={"branch": "same"},
            id="provider-reused-call-0",
        )
        for call in generated_calls
    ]
    memory = build_memory_system("memory", tmp_path / "parallel-subagent-memory")
    results: list[ToolExecution] = []
    threads = [
        Thread(
            target=lambda registry=registry, call=call, origin=origin: results.append(
                registry.execute(
                    call,
                    ToolContext(
                        memory=memory,
                        config=AgentConfig(tool_timeout_seconds=1.0),
                        workspace=tmp_path,
                        run_id="shared-parent-run",
                        execution_origin=f"subagent:{origin}",
                    ),
                )
            )
        )
        for registry, call, origin in zip(registries, calls, ("a", "b"), strict=True)
    ]
    for thread in threads:
        thread.start()
    assert both_started.wait(timeout=1.0)
    release.set()
    for thread in threads:
        thread.join(timeout=1.0)
        assert not thread.is_alive()
    assert len(results) == 2
    assert all(result.success for result in results)


def test_runtime_fence_allows_same_origin_and_call_id_with_distinct_arguments(
    tmp_path: Path,
) -> None:
    both_started = Event()
    release = Event()
    starts = 0
    starts_lock = Lock()

    class ArgumentScopedTool(AgentTool):
        spec = ToolSpec(
            name="contract.argument-scoped",
            description="Distinguish exact calls by their public arguments.",
            parameters={
                "type": "object",
                "properties": {"value": {"type": "string"}},
                "required": ["value"],
            },
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del context
            nonlocal starts
            with starts_lock:
                starts += 1
                if starts == 2:
                    both_started.set()
            assert release.wait(timeout=1.0)
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=arguments),
                success=True,
                content=str(arguments["value"]),
            )

    runtime_fence = RuntimeToolFence()
    registries = [
        ToolRegistry(runtime_fence=runtime_fence),
        ToolRegistry(runtime_fence=runtime_fence),
    ]
    for registry in registries:
        registry.register(ArgumentScopedTool())
    memory = build_memory_system("memory", tmp_path / "argument-scoped-memory")
    results: list[ToolExecution] = []
    threads = [
        Thread(
            target=lambda registry=registry, value=value: results.append(
                registry.execute(
                    ToolCall(
                        name="contract.argument-scoped",
                        arguments={"value": value},
                        id="provider-reused-call-0",
                    ),
                    ToolContext(
                        memory=memory,
                        config=AgentConfig(tool_timeout_seconds=1.0),
                        workspace=tmp_path,
                        run_id="shared-parent-run",
                        execution_origin="subagent:same",
                    ),
                )
            )
        )
        for registry, value in zip(registries, ("a", "b"), strict=True)
    ]
    for thread in threads:
        thread.start()
    assert both_started.wait(timeout=1.0)
    release.set()
    for thread in threads:
        thread.join(timeout=1.0)
        assert not thread.is_alive()
    assert sorted(result.content for result in results) == ["a", "b"]


def test_trusted_timeout_settlement_opt_in_remains_hard_bounded(
    tmp_path: Path,
) -> None:
    never_return = Event()

    class TrustedBlockedTool(AgentTool):
        wait_for_completion_on_timeout = True
        spec = ToolSpec(
            name="contract.trusted-blocked",
            description="Opts into settlement waiting but never settles.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments, context
            never_return.wait()
            raise AssertionError("unreachable")

    registry = ToolRegistry()
    registry.register(TrustedBlockedTool())
    memory = build_memory_system("memory", tmp_path / "trusted-memory")
    started = monotonic()

    result = registry.execute(
        ToolCall(name="contract.trusted-blocked", arguments={}),
        ToolContext(
            memory=memory,
            config=AgentConfig(tool_timeout_seconds=0.01),
            workspace=tmp_path,
        ),
    )

    assert monotonic() - started < 1.0
    assert result.error == "tool_outcome_unresolved"
    assert result.data["settlement_timeout_seconds"] <= 5.0
    assert result.data["retryable"] is False


def test_blocking_cancellation_hook_cannot_bypass_timeout_bound(tmp_path: Path) -> None:
    never_return = Event()

    class BlockingCancelTool(AgentTool):
        spec = ToolSpec(
            name="contract.blocking-cancel",
            description="Both execution and cancellation block forever.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments, context
            never_return.wait()
            raise AssertionError("unreachable")

        def cancel(self, call_id: str) -> None:
            del call_id
            never_return.wait()

    registry = ToolRegistry()
    registry.register(BlockingCancelTool())
    memory = build_memory_system("memory", tmp_path / "blocking-cancel-memory")
    started = monotonic()
    result = registry.execute(
        ToolCall(name="contract.blocking-cancel", arguments={}),
        ToolContext(
            memory=memory,
            config=AgentConfig(tool_timeout_seconds=0.01),
            workspace=tmp_path,
        ),
    )
    assert monotonic() - started < 1.0
    assert result.error == "tool_outcome_unresolved"
    assert result.data["cancellation_hook_settled"] is False
    assert result.data["reconciliation_required"] is True


def test_late_blocking_cancel_keeps_settled_worker_outcome_quarantined(
    tmp_path: Path,
) -> None:
    cancel_started = Event()
    worker_returned = Event()
    release_cancel = Event()

    class LateCancelTool(AgentTool):
        spec = ToolSpec(
            name="contract.late-blocking-cancel",
            description="Worker settles only after its cancellation hook has wedged.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments, context
            assert cancel_started.wait(timeout=1.0)
            worker_returned.set()
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments={}),
                success=True,
                content="worker settled",
            )

        def cancel(self, call_id: str) -> None:
            del call_id
            cancel_started.set()
            release_cancel.wait()

    runtime_fence = RuntimeToolFence()
    first = ToolRegistry(runtime_fence=runtime_fence)
    first.register(LateCancelTool())
    result = first.execute(
        ToolCall(name="contract.late-blocking-cancel", arguments={}),
        ToolContext(
            memory=build_memory_system("memory", tmp_path / "late-cancel-memory"),
            config=AgentConfig(tool_timeout_seconds=0.01),
            workspace=tmp_path,
        ),
    )
    assert result.error == "tool_outcome_unresolved"
    assert worker_returned.is_set()
    assert result.data["cancellation_hook_may_still_be_running"] is True

    fresh = ToolRegistry(runtime_fence=runtime_fence)
    fresh.register(LateCancelTool())
    quarantined = fresh.execute(
        ToolCall(name="contract.late-blocking-cancel", arguments={}, id="retry"),
        ToolContext(
            memory=build_memory_system("memory", tmp_path / "late-cancel-retry-memory"),
            config=AgentConfig(tool_timeout_seconds=0.01),
            workspace=tmp_path,
        ),
    )
    assert quarantined.error == "tool_quarantined_after_unresolved_outcome"
    release_cancel.set()


def test_runtime_fence_allows_same_read_tool_in_distinct_runs(tmp_path: Path) -> None:
    both_started = Event()
    release = Event()
    starts = 0
    starts_lock = Lock()

    class ConcurrentReadTool(AgentTool):
        spec = ToolSpec(
            name="contract.concurrent-read",
            description="Read-only work may run in separately admitted runs.",
            parameters={"type": "object", "properties": {}},
        )

        def run(
            self,
            arguments: dict[str, object],
            context: ToolContext,
        ) -> ToolExecution:
            del arguments
            nonlocal starts
            with starts_lock:
                starts += 1
                if starts == 2:
                    both_started.set()
            assert release.wait(timeout=1.0)
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments={}),
                success=True,
                content=str(context.run_id),
            )

    registry = ToolRegistry()
    registry.register(ConcurrentReadTool())
    memory = build_memory_system("memory", tmp_path / "concurrent-read-memory")
    results: list[ToolExecution] = []
    threads = [
        Thread(
            target=lambda run_id=run_id: results.append(
                registry.execute(
                    ToolCall(name="contract.concurrent-read", arguments={}, id=f"call-{run_id}"),
                    ToolContext(
                        memory=memory,
                        config=AgentConfig(tool_timeout_seconds=1.0),
                        workspace=tmp_path,
                        run_id=run_id,
                    ),
                )
            )
        )
        for run_id in ("run-a", "run-b")
    ]
    for thread in threads:
        thread.start()
    assert both_started.wait(timeout=1.0)
    release.set()
    for thread in threads:
        thread.join(timeout=1.0)
        assert not thread.is_alive()
    assert len(results) == 2
    assert all(result.success for result in results)
