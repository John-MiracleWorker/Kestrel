"""End-to-end agent task benchmark for Kestrel.

Runs multi-step tasks in sandboxed workspaces and measures success rate,
tool selection accuracy, memory persistence, and step efficiency.

Usage:
    python benchmarks/agent_benchmark.py --provider mock --output results/agent_benchmark.json
    python benchmarks/agent_benchmark.py --provider openai --model gpt-4.1-nano --output results/agent_benchmark.json
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
from nested_memvid_agent.runtime_models import LLMResponse, ToolCall
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------

def _task_memory_persistence(agent: Any, workspace: Path) -> dict[str, Any]:
    """Agent must remember a user preference across two turns."""
    # Turn 1: explicit instruction to save to memory
    turn1 = agent.chat(
        "Use the memory.write tool to save that my favorite color is teal.",
        session_id="bench_memory",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"layer": "working", "title": "Favorite color", "content": "User's favorite color is teal."}},
    )

    # Direct verification: check backend for the saved fact
    direct_hits = agent.memory.retrieve(
        RetrievalQuery(
            query="favorite color teal",
            layers=(MemoryLayer.WORKING, MemoryLayer.EPISODIC),
            k_per_layer=5,
        )
    )
    direct_found = any("teal" in hit.record.content.lower() for hit in direct_hits)

    # Turn 2: ask the LLM to recall it
    turn2 = agent.chat("What is my favorite color?", session_id="bench_memory", approved_tool_call_ids=frozenset())
    llm_recall = "teal" in turn2.assistant_message.lower()

    success = direct_found or llm_recall
    tool_calls = [te.call.name for te in turn2.tool_executions] if turn2.tool_executions else []
    return {
        "task": "memory_persistence",
        "success": success,
        "turns": 2,
        "tool_calls": tool_calls,
        "direct_found": direct_found,
        "llm_recall": llm_recall,
        "final_answer": turn2.assistant_message[:200],
    }


def _task_file_read_qa(agent: Any, workspace: Path) -> dict[str, Any]:
    """Agent must read a file and answer a question."""
    answer_file = workspace / "answer.txt"
    answer_file.write_text("The secret code is 42.")

    turn = agent.chat(
        f"Read {answer_file} and tell me the secret code.",
        session_id="bench_file_read",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"path": str(answer_file)}},
    )
    success = "42" in turn.assistant_message
    tool_calls = [te.call.name for te in turn.tool_executions] if turn.tool_executions else []
    return {
        "task": "file_read_qa",
        "success": success,
        "turns": 1,
        "tool_calls": tool_calls,
        "final_answer": turn.assistant_message[:200],
    }


def _task_repo_search(agent: Any, workspace: Path) -> dict[str, Any]:
    """Agent must search the repo for a pattern and report the file."""
    (workspace / "fruits.txt").write_text("apple\nbanana\ncherry\n")
    (workspace / "vegetables.txt").write_text("carrot\nspinach\n")

    turn = agent.chat(
        f"Which file in {workspace} contains the word banana?",
        session_id="bench_repo_search",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"query": "banana", "path": str(workspace)}},
    )
    success = "fruits.txt" in turn.assistant_message
    tool_calls = [te.call.name for te in turn.tool_executions] if turn.tool_executions else []
    return {
        "task": "repo_search",
        "success": success,
        "turns": 1,
        "tool_calls": tool_calls,
        "final_answer": turn.assistant_message[:200],
    }


def _task_git_status(agent: Any, workspace: Path) -> dict[str, Any]:
    """Agent must check git status and report untracked files."""
    os.system(f"cd {workspace} && git init -q && git config user.email 'bench@test' && git config user.name 'Bench'")
    (workspace / "new_feature.py").write_text("# new feature")

    turn = agent.chat(
        f"What is the git status in {workspace}? Are there untracked files?",
        session_id="bench_git_status",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"path": str(workspace)}},
    )
    content = turn.assistant_message.lower()
    success = "untracked" in content or "new_feature.py" in content
    tool_calls = [te.call.name for te in turn.tool_executions] if turn.tool_executions else []
    return {
        "task": "git_status",
        "success": success,
        "turns": 1,
        "tool_calls": tool_calls,
        "final_answer": turn.assistant_message[:200],
    }


# ---------------------------------------------------------------------------
# Mock response programming for deterministic fast path
# ---------------------------------------------------------------------------

def _mock_for_task(task_name: str) -> MockLLMProvider:
    """Return a MockLLMProvider programmed to succeed at the task."""
    if task_name == "memory_persistence":
        return MockLLMProvider([
            LLMResponse(
                content="I'll save that to memory.",
                tool_calls=(ToolCall(name="memory.write", arguments={"layer": "working", "title": "Favorite color", "content": "User's favorite color is teal."}, id="tc1"),),
            ),
            LLMResponse(content="Your favorite color is teal.", tool_calls=()),
        ])
    if task_name == "file_read_qa":
        return MockLLMProvider([
            LLMResponse(
                content="I'll read the file for you.",
                tool_calls=(ToolCall(name="file.read", arguments={"path": "answer.txt"}, id="tc1"),),
            ),
            LLMResponse(content="The secret code is 42."),
        ])
    if task_name == "repo_search":
        return MockLLMProvider([
            LLMResponse(
                content="I'll search for that word.",
                tool_calls=(ToolCall(name="repo.search", arguments={"query": "banana"}, id="tc1"),),
            ),
            LLMResponse(content="The file fruits.txt contains banana."),
        ])
    if task_name == "git_status":
        return MockLLMProvider([
            LLMResponse(
                content="I'll check the git status.",
                tool_calls=(ToolCall(name="git.status", arguments={"path": "."}, id="tc1"),),
            ),
            LLMResponse(content="There is one untracked file: new_feature.py"),
        ])
    return MockLLMProvider([LLMResponse(content="I don't know.")])


# ---------------------------------------------------------------------------
# Benchmark harness
# ---------------------------------------------------------------------------

def run_agent_benchmark(
    *,
    provider: str = "mock",
    model: str = "mock",
    backend: str = "memory",
    base_url: str | None = None,
    api_key_env: str | None = None,
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    task_fns = {
        "memory_persistence": _task_memory_persistence,
        "file_read_qa": _task_file_read_qa,
        "repo_search": _task_repo_search,
        "git_status": _task_git_status,
    }
    active_tasks = tasks or list(task_fns.keys())

    results = []
    total_time = 0.0
    for task_name in active_tasks:
        with tempfile.TemporaryDirectory(prefix=f"kestrel-agent-bench-{task_name}-") as tmpdir:
            workspace = Path(tmpdir) / "workspace"
            workspace.mkdir()
            memory_dir = Path(tmpdir) / "memory"
            memory_dir.mkdir()

            config = AgentConfig(
                provider=provider,
                model=model,
                backend=backend,
                base_url=base_url,
                api_key_env=api_key_env,
                memory_dir=memory_dir,
                workspace=workspace,
                log_dir=Path(tmpdir) / "logs",
                allow_web=False,
                allow_shell=False,
            )

            # For mock provider, inject programmed responses
            if provider == "mock":
                from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
                from nested_memvid_agent.backends.in_memory import InMemoryBackend
                from nested_memvid_agent.layers import LayeredMemorySystem
                from nested_memvid_agent.state_store import AgentStateStore
                from nested_memvid_agent.event_log import JsonlEventLog

                state = AgentStateStore(config.state_path)
                memory = LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)
                tools = build_default_tools()
                event_log = JsonlEventLog(config.log_dir / "events.jsonl")
                agent = NestedMV2Agent(
                    AgentDependencies(
                        memory=memory,
                        llm=_mock_for_task(task_name),
                        tools=tools,
                        config=config,
                        event_log=event_log,
                    )
                )
            else:
                agent = build_agent(config)

            t0 = time.perf_counter()
            try:
                result = task_fns[task_name](agent, workspace)
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
    total_tool_calls = sum(len(r.get("tool_calls", [])) for r in results)
    return {
        "schema": "kestrel.agent_benchmark.v1",
        "config": {"provider": provider, "model": model, "backend": backend, "tasks": active_tasks},
        "summary": {
            "total_tasks": len(results),
            "success_count": success_count,
            "success_rate": round(success_count / len(results), 2) if results else 0.0,
            "total_tool_calls": total_tool_calls,
            "avg_tools_per_task": round(total_tool_calls / len(results), 2) if results else 0.0,
            "total_elapsed_ms": round(total_time * 1000, 2),
        },
        "results": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Kestrel agent task benchmark.")
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--model", default="mock")
    parser.add_argument("--backend", default="memory")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--tasks", nargs="+", help="Subset of tasks to run")
    parser.add_argument("--output", type=Path, help="JSON output path")
    args = parser.parse_args()

    result = run_agent_benchmark(
        provider=args.provider,
        model=args.model,
        backend=args.backend,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
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
