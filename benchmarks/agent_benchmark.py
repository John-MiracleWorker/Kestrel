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
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.runtime_models import LLMResponse, ToolCall
from nested_memvid_agent.tools.builtin import build_default_tools

# ---------------------------------------------------------------------------
# Task definitions
# ---------------------------------------------------------------------------


def _successful_exact_tool_executions(
    turn: Any,
    *,
    name: str,
    arguments: dict[str, Any],
) -> list[Any]:
    """Return only successful executions of the exact expected call."""

    return [
        execution
        for execution in getattr(turn, "tool_executions", ())
        if execution.success
        and execution.call.name == name
        and execution.call.arguments == arguments
    ]


def _task_memory_persistence(agent: Any, workspace: Path) -> dict[str, Any]:
    """Agent must write an exact record and surface it in a fresh session."""

    del workspace
    write_session_id = "bench_memory_write"
    recall_session_id = "bench_memory_recall"
    expected_arguments = {
        "layer": "working",
        "title": "Favorite color",
        "content": "User's favorite color is teal.",
    }
    turn1 = agent.chat(
        "Call memory.write exactly once with layer 'working', title 'Favorite color', and "
        "content \"User's favorite color is teal.\"",
        session_id=write_session_id,
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": expected_arguments},
    )

    writes = _successful_exact_tool_executions(
        turn1,
        name="memory.write",
        arguments=expected_arguments,
    )
    write_execution = writes[0] if len(writes) == 1 else None
    record_id = (
        str(write_execution.data.get("record_id", "")) if write_execution is not None else ""
    )
    record = (
        agent.memory.get_record(
            MemoryLayer.WORKING,
            record_id,
            include_inactive=False,
        )
        if record_id
        else None
    )
    current_tool_record = bool(
        record is not None
        and record.id == record_id
        and record.layer == MemoryLayer.WORKING
        and record.title == expected_arguments["title"]
        and record.content == expected_arguments["content"]
        and record.metadata.get("source") == "tool.memory.write"
        and record.metadata.get("session_id") == write_session_id
    )

    recall_arguments = {
        "query": "favorite color teal",
        "layers": ["working"],
        "k": 5,
    }
    # A distinct session excludes same-session transcript recall. Force an
    # explicit retrieval so the exact tool-produced content, rather than a turn
    # transcript or canned response, is the evidence used by the recall turn.
    turn2 = agent.chat(
        "In this new session, call memory.search exactly once with query 'favorite color teal', "
        "layers ['working'], and k 5; then tell me my favorite color.",
        session_id=recall_session_id,
        approved_tool_call_ids=frozenset(),
    )
    fresh_session = turn1.session_id != turn2.session_id == recall_session_id
    searches = _successful_exact_tool_executions(
        turn2,
        name="memory.search",
        arguments=recall_arguments,
    )
    record_surfaced = any(
        any(
            hit.get("layer") == "working"
            and hit.get("title") == expected_arguments["title"]
            and expected_arguments["content"].lower() in str(hit.get("snippet", "")).lower()
            for hit in execution.data.get("hits", [])
        )
        for execution in searches
    )
    llm_recall = "teal" in turn2.assistant_message.lower()

    success = bool(
        write_execution is not None
        and current_tool_record
        and fresh_session
        and record_surfaced
        and llm_recall
    )
    executions = (*turn1.tool_executions, *turn2.tool_executions)
    return {
        "task": "memory_persistence",
        "success": success,
        "turns": 2,
        "tool_calls": [execution.call.name for execution in executions],
        "expected_tool_succeeded": write_execution is not None,
        "current_tool_record": current_tool_record,
        "record_id": record_id,
        "fresh_session": fresh_session,
        "record_surfaced_in_recall_tool": record_surfaced,
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
    expected_arguments = {"path": str(answer_file)}
    expected_executions = _successful_exact_tool_executions(
        turn,
        name="file.read",
        arguments=expected_arguments,
    )
    tool_evidence = any(
        execution.data.get("path") == str(answer_file.resolve()) and "42" in execution.content
        for execution in expected_executions
    )
    semantic_answer = re.search(r"\b42\b", turn.assistant_message) is not None
    success = tool_evidence and semantic_answer
    tool_calls = [te.call.name for te in turn.tool_executions] if turn.tool_executions else []
    return {
        "task": "file_read_qa",
        "success": success,
        "turns": 1,
        "tool_calls": tool_calls,
        "expected_tool_succeeded": tool_evidence,
        "semantic_answer": semantic_answer,
        "final_answer": turn.assistant_message[:200],
    }


def _task_repo_search(agent: Any, workspace: Path) -> dict[str, Any]:
    """Agent must search the repo for a pattern and report the file."""
    (workspace / "fruits.txt").write_text("apple\nbanana\ncherry\n")
    (workspace / "vegetables.txt").write_text("carrot\nspinach\n")

    turn = agent.chat(
        f"Call repo.search exactly once with query 'banana' and path '{workspace}', then tell me "
        "which file contains the word banana.",
        session_id="bench_repo_search",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"query": "banana", "path": str(workspace)}},
    )
    expected_arguments = {"query": "banana", "path": str(workspace)}
    expected_executions = _successful_exact_tool_executions(
        turn,
        name="repo.search",
        arguments=expected_arguments,
    )
    tool_evidence = any(
        any(match.get("path") == "fruits.txt" for match in execution.data.get("matches", []))
        for execution in expected_executions
    )
    semantic_answer = "fruits.txt" in turn.assistant_message.lower()
    success = tool_evidence and semantic_answer
    tool_calls = [te.call.name for te in turn.tool_executions] if turn.tool_executions else []
    return {
        "task": "repo_search",
        "success": success,
        "turns": 1,
        "tool_calls": tool_calls,
        "expected_tool_succeeded": tool_evidence,
        "semantic_answer": semantic_answer,
        "final_answer": turn.assistant_message[:200],
    }


def _task_git_status(agent: Any, workspace: Path) -> dict[str, Any]:
    """Agent must check git status and report untracked files."""
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "config", "user.email", "bench@test"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Bench"],
        cwd=workspace,
        check=True,
    )
    (workspace / "new_feature.py").write_text("# new feature")

    turn = agent.chat(
        f"What is the git status in {workspace}? Are there untracked files?",
        session_id="bench_git_status",
        approved_tool_call_ids=frozenset({"tc1"}),
        approved_tool_call_arguments={"tc1": {"path": str(workspace)}},
    )
    expected_executions = _successful_exact_tool_executions(
        turn,
        name="git.status",
        arguments={},
    )
    tool_evidence = any("new_feature.py" in execution.content for execution in expected_executions)
    content = turn.assistant_message.lower()
    semantic_answer = "untracked" in content and "new_feature.py" in content
    success = tool_evidence and semantic_answer
    tool_calls = [te.call.name for te in turn.tool_executions] if turn.tool_executions else []
    return {
        "task": "git_status",
        "success": success,
        "turns": 1,
        "tool_calls": tool_calls,
        "expected_tool_succeeded": tool_evidence,
        "semantic_answer": semantic_answer,
        "final_answer": turn.assistant_message[:200],
    }


# ---------------------------------------------------------------------------
# Mock response programming for deterministic fast path
# ---------------------------------------------------------------------------


def _mock_for_task(task_name: str, workspace: Path) -> MockLLMProvider:
    """Return a MockLLMProvider programmed to succeed at the task."""
    if task_name == "memory_persistence":
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll save that to memory.",
                    tool_calls=(
                        ToolCall(
                            name="memory.write",
                            arguments={
                                "layer": "working",
                                "title": "Favorite color",
                                "content": "User's favorite color is teal.",
                            },
                            id="tc1",
                        ),
                    ),
                ),
                LLMResponse(content="Saved your preference.", tool_calls=()),
                LLMResponse(
                    content="I'll search the persisted memory.",
                    tool_calls=(
                        ToolCall(
                            name="memory.search",
                            arguments={
                                "query": "favorite color teal",
                                "layers": ["working"],
                                "k": 5,
                            },
                            id="tc2",
                        ),
                    ),
                ),
                LLMResponse(content="Your favorite color is teal.", tool_calls=()),
            ]
        )
    if task_name == "file_read_qa":
        answer_file = workspace / "answer.txt"
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll read the file for you.",
                    tool_calls=(
                        ToolCall(
                            name="file.read",
                            arguments={"path": str(answer_file)},
                            id="tc1",
                        ),
                    ),
                ),
                LLMResponse(content="The secret code is 42."),
            ]
        )
    if task_name == "repo_search":
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll search for that word.",
                    tool_calls=(
                        ToolCall(
                            name="repo.search",
                            arguments={"query": "banana", "path": str(workspace)},
                            id="tc1",
                        ),
                    ),
                ),
                LLMResponse(content="The file fruits.txt contains banana."),
            ]
        )
    if task_name == "git_status":
        return MockLLMProvider(
            [
                LLMResponse(
                    content="I'll check the git status.",
                    tool_calls=(ToolCall(name="git.status", arguments={}, id="tc1"),),
                ),
                LLMResponse(content="There is one untracked file: new_feature.py"),
            ]
        )
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
    active_tasks = list(task_fns) if tasks is None else list(tasks)

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
                state_path=Path(tmpdir) / "state" / "agent.db",
                secret_store_path=Path(tmpdir) / "secrets" / "local_vault.json",
                skills_dir=Path(tmpdir) / "skills",
                plugins_dir=Path(tmpdir) / "plugins",
                mcp_config_path=Path(tmpdir) / "config" / "mcp_servers.json",
                channel_config_path=Path(tmpdir) / "config" / "channels.json",
                worker_worktree_dir=Path(tmpdir) / "worktrees",
                allow_web=False,
                allow_shell=False,
            )

            # For mock provider, inject programmed responses
            if provider == "mock":
                from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
                from nested_memvid_agent.backends.in_memory import InMemoryBackend
                from nested_memvid_agent.event_log import JsonlEventLog
                from nested_memvid_agent.layers import LayeredMemorySystem

                memory = LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)
                tools = build_default_tools()
                event_log = JsonlEventLog(config.log_dir / "events.jsonl")
                agent = NestedMV2Agent(
                    AgentDependencies(
                        memory=memory,
                        llm=_mock_for_task(task_name, workspace),
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
    passed = bool(results) and success_count == len(results)
    return {
        "schema": "kestrel.agent_benchmark.v1",
        "config": {
            "provider": provider,
            "model": model,
            "backend": "in_memory" if provider == "mock" else backend,
            "requested_backend": backend,
            "tasks": active_tasks,
        },
        "summary": {
            "total_tasks": len(results),
            "success_count": success_count,
            "success_rate": round(success_count / len(results), 2) if results else 0.0,
            "passed": passed,
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
