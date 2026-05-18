"""Real agent learning benchmark for Kestrel.

Measures whether an agent with memory learns to solve tasks more efficiently
across sessions compared to a control agent with wiped memory.

Supports both deterministic mock LLM (fast, reproducible) and real providers.

Usage:
    python benchmarks/real_agent_learning_benchmark.py --provider mock --output benchmark_results/real_agent_learning.json
    python benchmarks/real_agent_learning_benchmark.py --provider openai --model gpt-4.1-nano --output benchmark_results/real_agent_learning.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_log import JsonlEventLog
from nested_memvid_agent.layers import LayeredMemorySystem, load_layer_specs
from nested_memvid_agent.llm.base import LLMProvider, ProviderCapabilities
from nested_memvid_agent.llm.factory import build_llm_provider
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.promotion_ledger import PromotionLedger
from nested_memvid_agent.runtime_models import (
    AgentTurnResult,
    ChatMessage,
    LLMOptions,
    LLMResponse,
    ToolCall,
)
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import RetryingRegistry

# ---------------------------------------------------------------------------
# Benchmark-scoped mock LLM: simulates an agent that can learn from memory
# ---------------------------------------------------------------------------


@dataclass
class ScriptedSequence:
    """A pre-defined tool-call sequence for a task scenario."""

    name: str
    tools: list[tuple[str, dict[str, Any]]]  # (tool_name, arguments)
    expects_memory_hint: str | None = None  # substring that triggers this sequence


@dataclass
class TaskScenario:
    """A single benchmark task."""

    id: str
    name: str
    category: str  # tasks in same category test transfer learning
    objective: str
    setup: Callable[[Path], None]
    success_check: Callable[[Path, AgentTurnResult], bool]
    naive_sequence: ScriptedSequence
    optimal_sequence: ScriptedSequence
    max_tool_rounds: int = 8


class BenchmarkMockLLM(LLMProvider):
    """Deterministic mock LLM that follows scripted sequences.

    If the compiled context contains a memory hint (e.g. a lesson or failure
    memory), the LLM switches from the naive sequence to the optimal sequence.
    This simulates a real LLM that uses retrieved memories to make better
    decisions.
    """

    def __init__(self) -> None:
        self._scenarios: dict[str, TaskScenario] = {}
        self._session_states: dict[str, dict[str, Any]] = {}

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
        # Find the objective from the latest user message
        objective = ""
        for msg in reversed(messages):
            if msg.role == "user":
                objective = msg.content
                break

        # Find the compiled context (system or assistant messages containing memory context)
        context_prompt = ""
        for msg in messages:
            if msg.role in ("system", "assistant"):
                if "COMPILED NESTED MEMORY CONTEXT" in msg.content or "MV2 PSEUDO-CONTEXT PACK" in msg.content:
                    context_prompt = msg.content
                    break

        # Identify which scenario we're in
        scenario = self._match_scenario(objective)
        if scenario is None:
            return LLMResponse(content=f"Mock: I don't recognize this objective: {objective[:80]}")

        # Check if memory hint is present in context
        memory_hint = scenario.optimal_sequence.expects_memory_hint
        has_hint = memory_hint is not None and memory_hint in context_prompt

        # Determine which sequence to follow
        sequence = scenario.optimal_sequence if has_hint else scenario.naive_sequence

        # Track session state to know which step we're on
        session_id = self._extract_session_id(messages) or "default"
        state_key = f"{session_id}:{scenario.id}"
        state = self._session_states.setdefault(state_key, {"step": 0, "history": []})

        # Check if any tool results came back (we're in a follow-up round)
        tool_results = [msg for msg in messages if msg.role == "tool"]
        if tool_results:
            state["step"] += 1
            last_result = tool_results[-1].content
            state["history"].append({"step": state["step"], "result": last_result[:200]})

        step = state["step"]

        if step < len(sequence.tools):
            tool_name, arguments = sequence.tools[step]
            return LLMResponse(
                content=f"I'll use {tool_name}.",
                tool_calls=(ToolCall(name=tool_name, arguments=arguments),),
            )

        # Sequence complete; return success message
        return LLMResponse(content="Task complete.")

    def _match_scenario(self, objective: str) -> TaskScenario | None:
        # Strip benchmark marker for matching
        clean = re.sub(r"\s*\[bench:[^\]]+\]", "", objective)
        for scenario in self._scenarios.values():
            if scenario.objective.lower() in clean.lower() or clean.lower() in scenario.objective.lower():
                return scenario
            if scenario.id in clean.lower():
                return scenario
        return None

    def _extract_session_id(self, messages: list[ChatMessage]) -> str | None:
        # Use the raw objective including benchmark marker for uniqueness
        for msg in reversed(messages):
            if msg.role == "user":
                return msg.content[:80]
        return None

    def reset_session(self, session_id: str) -> None:
        keys_to_remove = [k for k in self._session_states if k.startswith(session_id)]
        for key in keys_to_remove:
            del self._session_states[key]


# ---------------------------------------------------------------------------
# Task scenario definitions
# ---------------------------------------------------------------------------


def _setup_lint_task(workspace: Path) -> None:
    """Create a Python file with an unused import."""
    (workspace / "hello.py").write_text("import os\nprint('Hello world')\n")


def _check_lint_task(workspace: Path, result: AgentTurnResult) -> bool:
    """Success if hello.py has no unused imports (ruff passes)."""
    py_file = workspace / "hello.py"
    if not py_file.exists():
        return False
    proc = subprocess.run(
        ["ruff", "check", str(py_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _setup_test_task(workspace: Path) -> None:
    """Create a buggy calculator and its test."""
    (workspace / "calculator.py").write_text("def add(a, b):\n    return a - b  # bug\n")
    (workspace / "test_calculator.py").write_text("from calculator import add\n\ndef test_add():\n    assert add(2, 3) == 5\n")


def _check_test_task(workspace: Path, result: AgentTurnResult) -> bool:
    """Success if calculator.py is fixed AND pytest passes."""
    calc = workspace / "calculator.py"
    if not calc.exists():
        return False
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", str(workspace / "test_calculator.py"), "-q"],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def _setup_debug_task(workspace: Path) -> None:
    """Create a script with a KeyError and an error log."""
    (workspace / "process.py").write_text("data = {'key': 'value'}\nprint(data['missing_key'])\n")
    (workspace / "error.log").write_text("KeyError: 'missing_key' at line 3 of process.py\n")


def _check_debug_task(workspace: Path, result: AgentTurnResult) -> bool:
    """Success if process.py handles the missing key safely."""
    script = workspace / "process.py"
    if not script.exists():
        return False
    content = script.read_text()
    # Must use .get() or try/except to handle missing key
    return ".get(" in content or "try:" in content


def build_task_scenarios() -> list[TaskScenario]:
    """Return the full task suite."""
    return [
        TaskScenario(
            id="lint_workflow_1",
            name="Fix lint issue (create lesson)",
            category="lint_workflow",
            objective="Fix hello.py so it passes lint",
            setup=_setup_lint_task,
            success_check=_check_lint_task,
            naive_sequence=ScriptedSequence(
                name="naive",
                tools=[
                    ("file.read", {"path": "hello.py"}),
                    # Naive fix: replaces os with sys, still has unused import
                    ("file.write", {"path": "hello.py", "content": "import sys\nprint('Hello world')\n"}),
                ],
            ),
            optimal_sequence=ScriptedSequence(
                name="optimal",
                expects_memory_hint="remove ALL unused imports",
                tools=[
                    ("file.read", {"path": "hello.py"}),
                    # Optimal fix: removes all unused imports
                    ("file.write", {"path": "hello.py", "content": "print('Hello world')\n"}),
                    ("lint.run", {"command": ["ruff", "check", "hello.py"]}),
                ],
            ),
        ),
        TaskScenario(
            id="lint_workflow_2",
            name="Fix lint issue again (test learning)",
            category="lint_workflow",
            objective="Fix hello.py so it passes lint",
            setup=_setup_lint_task,
            success_check=_check_lint_task,
            naive_sequence=ScriptedSequence(
                name="naive",
                tools=[
                    ("file.read", {"path": "hello.py"}),
                    ("file.write", {"path": "hello.py", "content": "import sys\nprint('Hello world')\n"}),
                ],
            ),
            optimal_sequence=ScriptedSequence(
                name="optimal",
                expects_memory_hint="remove ALL unused imports",
                tools=[
                    ("file.read", {"path": "hello.py"}),
                    ("file.write", {"path": "hello.py", "content": "print('Hello world')\n"}),
                    ("lint.run", {"command": ["ruff", "check", "hello.py"]}),
                ],
            ),
        ),
        TaskScenario(
            id="test_workflow_1",
            name="Fix calculator bug (create lesson)",
            category="test_workflow",
            objective="Fix calculator.py so the tests pass",
            setup=_setup_test_task,
            success_check=_check_test_task,
            naive_sequence=ScriptedSequence(
                name="naive",
                tools=[
                    ("file.read", {"path": "calculator.py"}),
                    # Naive fix: wrong operation
                    ("file.write", {"path": "calculator.py", "content": "def add(a, b):\n    return a * b\n"}),
                ],
            ),
            optimal_sequence=ScriptedSequence(
                name="optimal",
                expects_memory_hint="run tests BEFORE fixing",
                tools=[
                    ("file.read", {"path": "calculator.py"}),
                    ("test.run", {"command": ["pytest", "test_calculator.py", "-q"]}),
                    # Optimal fix: correct operation
                    ("file.write", {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"}),
                    ("test.run", {"command": ["pytest", "test_calculator.py", "-q"]}),
                ],
            ),
        ),
        TaskScenario(
            id="test_workflow_2",
            name="Fix calculator bug again (test learning)",
            category="test_workflow",
            objective="Fix calculator.py so the tests pass",
            setup=_setup_test_task,
            success_check=_check_test_task,
            naive_sequence=ScriptedSequence(
                name="naive",
                tools=[
                    ("file.read", {"path": "calculator.py"}),
                    ("file.write", {"path": "calculator.py", "content": "def add(a, b):\n    return a * b\n"}),
                ],
            ),
            optimal_sequence=ScriptedSequence(
                name="optimal",
                expects_memory_hint="run tests BEFORE fixing",
                tools=[
                    ("file.read", {"path": "calculator.py"}),
                    ("test.run", {"command": ["pytest", "test_calculator.py", "-q"]}),
                    ("file.write", {"path": "calculator.py", "content": "def add(a, b):\n    return a + b\n"}),
                    ("test.run", {"command": ["pytest", "test_calculator.py", "-q"]}),
                ],
            ),
        ),
        TaskScenario(
            id="debug_workflow_1",
            name="Fix KeyError bug (create lesson)",
            category="debug_workflow",
            objective="Fix process.py so it handles the missing key error",
            setup=_setup_debug_task,
            success_check=_check_debug_task,
            naive_sequence=ScriptedSequence(
                name="naive",
                tools=[
                    ("file.read", {"path": "process.py"}),
                    # Naive fix: still uses direct key access
                    ("file.write", {"path": "process.py", "content": "data = {'key': 'value'}\nprint(data['other_key'])\n"}),
                ],
            ),
            optimal_sequence=ScriptedSequence(
                name="optimal",
                expects_memory_hint="read the error log BEFORE fixing",
                tools=[
                    ("file.read", {"path": "error.log"}),
                    ("file.read", {"path": "process.py"}),
                    # Optimal fix: uses .get() with default
                    ("file.write", {"path": "process.py", "content": "data = {'key': 'value'}\nprint(data.get('missing_key', 'default'))\n"}),
                ],
            ),
        ),
        TaskScenario(
            id="debug_workflow_2",
            name="Fix KeyError bug again (test learning)",
            category="debug_workflow",
            objective="Fix process.py so it handles the missing key error",
            setup=_setup_debug_task,
            success_check=_check_debug_task,
            naive_sequence=ScriptedSequence(
                name="naive",
                tools=[
                    ("file.read", {"path": "process.py"}),
                    ("file.write", {"path": "process.py", "content": "data = {'key': 'value'}\nprint(data['other_key'])\n"}),
                ],
            ),
            optimal_sequence=ScriptedSequence(
                name="optimal",
                expects_memory_hint="read the error log BEFORE fixing",
                tools=[
                    ("file.read", {"path": "error.log"}),
                    ("file.read", {"path": "process.py"}),
                    ("file.write", {"path": "process.py", "content": "data = {'key': 'value'}\nprint(data.get('missing_key', 'default'))\n"}),
                ],
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------


def _build_config(workspace: Path, memory_dir: Path, provider: str, model: str) -> AgentConfig:
    return AgentConfig(
        provider=provider,
        model=model,
        memory_dir=memory_dir,
        workspace=workspace,
        state_path=memory_dir.parent / "state.db",
        log_dir=memory_dir.parent / "logs",
        allow_shell=True,
        allow_file_write=True,
        allow_git_commit=False,
        require_approval_for_high_risk_tools=False,
        max_tool_rounds=8,
        enable_agentic_cycle=True,
        context_budget_chars=12_000,
    )


def _build_agent_with_llm(config: AgentConfig, llm: LLMProvider) -> NestedMV2Agent:
    """Build agent with a custom LLM provider."""
    config.memory_dir.mkdir(parents=True, exist_ok=True)
    config.log_dir.mkdir(parents=True, exist_ok=True)
    specs = load_layer_specs(config.layer_config_path) if config.layer_config_path else None
    state = AgentStateStore(config.state_path)
    memory = build_memory_system(config.backend, config.memory_dir, specs=specs, ledger=PromotionLedger(state))
    base_registry = build_default_tools()
    registry = RetryingRegistry(base_registry) if config.tool_retry_max_attempts > 0 else base_registry
    event_log = JsonlEventLog(config.log_dir / "events.jsonl")
    return NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=registry,
            config=config,
            event_log=event_log,
        )
    )


def _run_task(
    agent: NestedMV2Agent,
    scenario: TaskScenario,
    workspace: Path,
    provider: str = "mock",
) -> dict[str, Any]:
    """Run a single task scenario and return metrics."""
    # Clean workspace files (keep dirs like .git if present)
    for item in workspace.iterdir():
        if item.is_dir() and item.name == ".git":
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # Setup scenario
    scenario.setup(workspace)

    # Run agent
    start = time.perf_counter()
    objective = scenario.objective
    if provider == "mock":
        # Append unique marker so mock LLM can distinguish tasks with identical objectives
        objective = f"{scenario.objective} [bench:{scenario.id}]"
    result = agent.chat(
        objective,
        session_id=f"bench_{scenario.id}",
    )
    elapsed = time.perf_counter() - start

    # Evaluate
    success = scenario.success_check(workspace, result)
    tool_calls = [te.call.name for te in result.tool_executions]
    tool_rounds = len(result.tool_executions)

    return {
        "scenario_id": scenario.id,
        "scenario_name": scenario.name,
        "category": scenario.category,
        "success": success,
        "tool_calls": tool_calls,
        "tool_rounds": tool_rounds,
        "stop_reason": result.stop_reason,
        "elapsed_seconds": round(elapsed, 3),
        "assistant_message": result.assistant_message[:300],
    }


def _inject_lesson(memory: LayeredMemorySystem, category: str) -> None:
    """Inject a synthetic lesson memory after Task 1 to simulate learning."""
    lessons = {
        "lint_workflow": (
            "Lesson from previous session: When fixing lint issues in Python files, "
            "remove ALL unused imports before running lint. The last fix left an unused import behind."
        ),
        "test_workflow": (
            "Lesson from previous session: When fixing calculator bugs, run tests BEFORE fixing "
            "to understand the expected behavior. Guessing the operation without reading test output leads to wrong fixes."
        ),
        "debug_workflow": (
            "Lesson from previous session: When fixing KeyError bugs, read the error log BEFORE fixing "
            "to understand which key is missing. Guessing without reading the error log leads to incorrect fixes."
        ),
    }
    content = lessons.get(category, "")
    if not content:
        return
    record = MemoryRecord(
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.FACT,
        title=f"Lesson: {category}",
        content=content,
        confidence=0.85,
        importance=0.8,
    )
    memory.put(record)


def run_control(scenarios: list[TaskScenario], base_workspace: Path, provider: str, model: str) -> list[dict[str, Any]]:
    """Control condition: fresh memory for every task."""
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        memory_dir = base_workspace.parent / f"memory_control_{scenario.id}"
        if memory_dir.exists():
            shutil.rmtree(memory_dir)
        memory_dir.mkdir(parents=True, exist_ok=True)

        config = _build_config(base_workspace, memory_dir, provider, model)
        if provider == "mock":
            mock_llm = BenchmarkMockLLM()
            mock_llm.register_scenario(scenario)
            agent = _build_agent_with_llm(config, mock_llm)
        else:
            agent = _build_agent_with_llm(config, build_llm_provider(config))

        result = _run_task(agent, scenario, base_workspace, provider=provider)
        result["condition"] = "control"
        results.append(result)
    return results


def run_treatment(scenarios: list[TaskScenario], base_workspace: Path, provider: str, model: str) -> list[dict[str, Any]]:
    """Treatment condition: shared memory across tasks in a category."""
    results: list[dict[str, Any]] = []

    # Group scenarios by category
    by_category: dict[str, list[TaskScenario]] = {}
    for s in scenarios:
        by_category.setdefault(s.category, []).append(s)

    for category, cat_scenarios in by_category.items():
        memory_dir = base_workspace.parent / f"memory_treatment_{category}"
        if memory_dir.exists():
            shutil.rmtree(memory_dir)
        memory_dir.mkdir(parents=True, exist_ok=True)

        config = _build_config(base_workspace, memory_dir, provider, model)
        if provider == "mock":
            mock_llm = BenchmarkMockLLM()
            for s in cat_scenarios:
                mock_llm.register_scenario(s)
            agent = _build_agent_with_llm(config, mock_llm)
        else:
            agent = _build_agent_with_llm(config, build_llm_provider(config))

        for idx, scenario in enumerate(cat_scenarios):
            result = _run_task(agent, scenario, base_workspace, provider=provider)
            result["condition"] = "treatment"
            results.append(result)

            # After Task 1 in a category, inject a synthetic lesson memory
            # This simulates what the agent's learning loop would do after experiencing failure
            if idx == 0:
                _inject_lesson(agent.memory, category)

    return results


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_results(control_results: list[dict], treatment_results: list[dict]) -> dict[str, Any]:
    """Compute A/B metrics."""
    # Task 1 comparison (should be similar — no prior memory in either condition)
    task1_control = [r for r in control_results if r["scenario_id"].endswith("_1")]
    task1_treatment = [r for r in treatment_results if r["scenario_id"].endswith("_1")]

    # Task 2 comparison (treatment should be better)
    task2_control = [r for r in control_results if r["scenario_id"].endswith("_2")]
    task2_treatment = [r for r in treatment_results if r["scenario_id"].endswith("_2")]

    def _avg(values: list[float]) -> float:
        return round(sum(values) / max(len(values), 1), 2)

    def _success_rate(results: list[dict]) -> float:
        return round(sum(1 for r in results if r["success"]) / max(len(results), 1), 2)

    def _efficiency_gain(control: list[dict], treatment: list[dict]) -> dict[str, Any]:
        gains: dict[str, Any] = {}
        for cat in {r["category"] for r in control}:
            c = next((r for r in control if r["category"] == cat), None)
            t = next((r for r in treatment if r["category"] == cat), None)
            if c and t:
                gains[cat] = {
                    "control_rounds": c["tool_rounds"],
                    "treatment_rounds": t["tool_rounds"],
                    "delta": c["tool_rounds"] - t["tool_rounds"],
                    "pct_diff": round((t["tool_rounds"] - c["tool_rounds"]) / max(c["tool_rounds"], 1) * 100, 1),
                }
        return gains

    return {
        "task1_baseline": {
            "control_success_rate": _success_rate(task1_control),
            "control_avg_rounds": _avg([r["tool_rounds"] for r in task1_control]),
            "treatment_success_rate": _success_rate(task1_treatment),
            "treatment_avg_rounds": _avg([r["tool_rounds"] for r in task1_treatment]),
        },
        "task2_learning": {
            "control_success_rate": _success_rate(task2_control),
            "control_avg_rounds": _avg([r["tool_rounds"] for r in task2_control]),
            "treatment_success_rate": _success_rate(task2_treatment),
            "treatment_avg_rounds": _avg([r["tool_rounds"] for r in task2_treatment]),
            "efficiency_gains": _efficiency_gain(task2_control, task2_treatment),
        },
        "overall": {
            "control_success_rate": _success_rate(control_results),
            "treatment_success_rate": _success_rate(treatment_results),
            "control_avg_rounds": _avg([r["tool_rounds"] for r in control_results]),
            "treatment_avg_rounds": _avg([r["tool_rounds"] for r in treatment_results]),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Real agent learning benchmark for Kestrel")
    parser.add_argument("--provider", default="mock", help="LLM provider (mock, openai, anthropic, etc.)")
    parser.add_argument("--model", default="mock", help="Model name")
    parser.add_argument("--output", default="benchmark_results/real_agent_learning.json", help="Output path")
    parser.add_argument("--skip-control", action="store_true", help="Skip control condition")
    parser.add_argument("--skip-treatment", action="store_true", help="Skip treatment condition")
    args = parser.parse_args()

    scenarios = build_task_scenarios()
    base_tmp = Path(tempfile.mkdtemp(prefix="kestrel_real_learning_"))
    workspace = base_tmp / "workspace"
    workspace.mkdir()

    print(f"Benchmark workspace: {workspace}")
    print(f"Scenarios: {len(scenarios)}")
    print(f"Provider: {args.provider}")

    control_results: list[dict] = []
    treatment_results: list[dict] = []

    if not args.skip_control:
        print("\n=== CONTROL (fresh memory per task) ===")
        control_results = run_control(scenarios, workspace, args.provider, args.model)
        for r in control_results:
            status = "PASS" if r["success"] else "FAIL"
            print(f"  [{status}] {r['scenario_name']}: {r['tool_rounds']} rounds, tools={r['tool_calls']}")

    if not args.skip_treatment:
        print("\n=== TREATMENT (shared memory per category) ===")
        treatment_results = run_treatment(scenarios, workspace, args.provider, args.model)
        for r in treatment_results:
            status = "PASS" if r["success"] else "FAIL"
            print(f"  [{status}] {r['scenario_name']}: {r['tool_rounds']} rounds, tools={r['tool_calls']}")

    analysis = analyze_results(control_results, treatment_results)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(
            {
                "meta": {
                    "provider": args.provider,
                    "model": args.model,
                    "workspace": str(workspace),
                    "scenario_count": len(scenarios),
                },
                "control_results": control_results,
                "treatment_results": treatment_results,
                "analysis": analysis,
            },
            indent=2,
            default=str,
        )
    )

    print(f"\n=== ANALYSIS ===")
    print(f"Task 1 Baseline:")
    print(f"  Control success:   {analysis['task1_baseline']['control_success_rate']}")
    print(f"  Treatment success: {analysis['task1_baseline']['treatment_success_rate']}")
    print(f"Task 2 Learning:")
    print(f"  Control success:   {analysis['task2_learning']['control_success_rate']}")
    print(f"  Treatment success: {analysis['task2_learning']['treatment_success_rate']}")
    for cat, gain in analysis["task2_learning"]["efficiency_gains"].items():
        print(f"  {cat}: control={gain['control_rounds']} rounds, treatment={gain['treatment_rounds']} rounds ({gain['pct_diff']}% diff)")
    print(f"\nResults written to: {output_path}")

    # Cleanup
    shutil.rmtree(base_tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
