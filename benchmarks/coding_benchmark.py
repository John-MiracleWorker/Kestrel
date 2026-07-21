"""Coding benchmark for Kestrel against Dominion tasks.

Tests Kestrel's ability to solve coding problems using file.write + shell.run tools.
Uses the same 5 tasks as Dominion's coding benchmark.

Usage:
    python benchmarks/coding_benchmark.py --provider ollama-cloud --model kimi-k2.6 --output benchmark_results/coding_kimi.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.app_factory import build_agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.runtime_models import ToolSpec
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry

# Dominion task definitions
TASKS = [
    {
        "id": "easy-js-duration",
        "language": "javascript",
        "difficulty": 1,
        "file": "solution.js",
        "prompt": "Create a CommonJS module exporting function parseDuration(input). It receives a human duration string and returns total seconds as an integer. Support units: weeks/w, days/d, hours/hr/h, minutes/min/m, seconds/sec/s. Examples: '1h 30m' => 5400, '2 days, 3 hours' => 183600, '45s' => 45, '1.5h' => 5400. Whitespace and commas are optional. Unit names may be singular/plural. Throw TypeError for non-string input and Error for invalid/empty/negative duration strings. Do not use external packages. Only output code for solution.js.",
        "test": "test.js",
    },
    {
        "id": "medium-js-lru",
        "language": "javascript",
        "difficulty": 2,
        "file": "solution.js",
        "prompt": "Create a CommonJS module exporting class LRUCache. Constructor takes positive integer capacity. Methods: get(key) returns value or -1 and marks key recently used; put(key,value) inserts/updates and evicts least recently used when over capacity; delete(key) removes and returns true/false; size getter returns number of entries; keys() returns keys from most-recent to least-recent. Keys can be any JS value, including objects. Must be O(1) average for get/put/delete. Throw Error for invalid capacity. Do not use external packages. Only output code for solution.js.",
        "test": "test.js",
    },
    {
        "id": "hard-js-async-pool",
        "language": "javascript",
        "difficulty": 3,
        "file": "solution.js",
        "prompt": "Create a CommonJS module exporting async function runPool(tasks, limit, options). tasks is an array of functions returning values/promises. limit is max concurrency. options optional: { stopOnError=false, signal }. Return a promise resolving to an array of results in original task order. If a task rejects and stopOnError is false, store the Error object in that result slot and continue. If stopOnError is true, reject immediately with that error and do not start more tasks. Respect AbortSignal: if signal is aborted before or during run, reject with an Error whose name is 'AbortError' and do not start more tasks. Never run more than limit tasks concurrently. Validate inputs. Do not use external packages. Only output code for solution.js.",
        "test": "test.js",
    },
    {
        "id": "expert-js-json-patch",
        "language": "javascript",
        "difficulty": 4,
        "file": "solution.js",
        "prompt": "Create a CommonJS module exporting function applyJsonPatch(document, patch). Implement RFC6902 operations add, remove, replace, move, copy, test for JSON-compatible data. Do not mutate the input document. Support JSON Pointer escaping ~0 and ~1. Array add supports '-' append. Throw descriptive Error for invalid paths, failed test, unknown op, invalid array index, missing fields. Deep equality for test. Do not use external packages. Only output code for solution.js.",
        "test": "test.js",
    },
    {
        "id": "expert-py-template-engine",
        "language": "python",
        "difficulty": 5,
        "file": "solution.py",
        "prompt": "Write a Python 3 module solution.py implementing render(template: str, context: dict) -> str. Mini template language: variables {{ user.name }} with dotted lookup through dicts/objects; HTML escape variables by default; triple braces {{{ raw }}} are unescaped; filters pipe syntax supports upper, lower, default('x'), join(','); conditionals {% if expr %}...{% else %}...{% endif %} where expr supports dotted truthiness and 'not'; loops {% for item in items %}...{% endfor %}; comments {# ... #}; literal delimiters can be escaped with backslash. Missing variables render empty string unless default filter. Raise TemplateSyntaxError for malformed tags/unclosed blocks. No external packages. Prioritize correctness and security. Only output code for solution.py.",
        "test": "test_solution.py",
    },
]

# Path to Dominion tests
DOMINION_TESTS = Path.home() / ".openclaw" / "workspace" / "benchmarks" / "dominion-coding" / "tests"


def _build_aliased_registry() -> ToolRegistry:
    """Build a minimal tool registry with extra aliases for coding tasks."""
    base = build_default_tools()
    registry = ToolRegistry()
    # Only register essential coding tools to reduce model cognitive load
    essential_tools = {
        "file.list", "file.read", "file.write", "shell.run", "test.run", "lint.run"
    }
    extra_aliases: dict[str, tuple[str, ...]] = {
        "file.list": ("list",),
        "file.write": ("write",),
        "shell.run": ("shell", "exec", "run"),
        "test.run": ("test",),
        "lint.run": ("lint",),
    }
    for name, tool in base._tools.items():
        if name not in essential_tools:
            continue
        aliases = tool.spec.aliases + extra_aliases.get(name, ())
        new_spec = ToolSpec(
            name=tool.spec.name,
            description=tool.spec.description,
            parameters=tool.spec.parameters,
            risk=tool.spec.risk,
            requires_approval=tool.spec.requires_approval,
            source=tool.spec.source,
            server_id=tool.spec.server_id,
            skill_id=tool.spec.skill_id,
            capabilities=tool.spec.capabilities,
            produces_validation=tool.spec.produces_validation,
            aliases=aliases,
        )
        object.__setattr__(tool, "spec", new_spec)
        registry.register(tool)
    return registry


def _build_config(workspace: Path, provider: str, model: str) -> AgentConfig:
    return AgentConfig(
        provider=provider,
        model=model,
        memory_dir=workspace / ".memory",
        workspace=workspace,
        state_path=workspace / ".state.db",
        log_dir=workspace / ".logs",
        allow_shell=True,
        allow_file_write=True,
        allow_git_commit=False,
        require_approval_for_high_risk_tools=False,
        max_tool_rounds=12,
        enable_agentic_cycle=False,
        context_budget_chars=4_000,
        timeout_seconds=300,
        tool_retry_max_attempts=3,
        tool_retry_backoff_base_seconds=1.0,
    )


def _run_task(
    task: dict[str, Any],
    workspace: Path,
    provider: str,
    model: str,
) -> dict[str, Any]:
    """Run a single coding task and return results."""
    # Clean workspace
    for item in workspace.iterdir():
        if item.name.startswith("."):
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()

    # Copy hidden test into workspace
    test_src = DOMINION_TESTS / task["id"] / task["test"]
    if test_src.exists():
        shutil.copy2(test_src, workspace / task["test"])
    else:
        print(f"WARNING: test file not found: {test_src}", file=sys.stderr)

    # Build agent with aliased registry
    config = _build_config(workspace, provider, model)
    registry = _build_aliased_registry()
    agent = build_agent(config, tools=registry)

    # Construct objective
    objective = (
        f"Coding task: {task['id']}\n\n"
        f"Write the solution to the following problem in the file `{task['file']}` in the workspace {workspace}.\n\n"
        f"{task['prompt']}\n\n"
        f"After writing the solution, run the tests in `{task['test']}` to verify correctness. "
        f"Use the shell.run tool to execute: `node {task['test']}` for JS or `python -m pytest {task['test']} -q` for Python."
    )

    start = time.perf_counter()
    try:
        result = agent.chat(
            objective,
            session_id=f"coding_{task['id']}",
        )
        elapsed = time.perf_counter() - start
    except Exception as e:
        elapsed = time.perf_counter() - start
        return {
            "task_id": task["id"],
            "success": False,
            "error": str(e),
            "elapsed_seconds": round(elapsed, 3),
            "tool_calls": [],
            "solution_exists": False,
        }

    # Check if solution file was written
    solution_path = workspace / task["file"]
    solution_exists = solution_path.exists()
    solution_size = solution_path.stat().st_size if solution_exists else 0

    # Run hidden tests
    test_passed = False
    test_output = ""
    if solution_exists:
        if task["language"] == "javascript":
            proc = subprocess.run(
                ["node", task["test"]],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", task["test"], "-q"],
                cwd=workspace,
                capture_output=True,
                text=True,
                timeout=30,
            )
        test_passed = proc.returncode == 0
        test_output = proc.stdout + proc.stderr

    tool_calls = [te.call.name for te in result.tool_executions] if result.tool_executions else []

    return {
        "task_id": task["id"],
        "difficulty": task["difficulty"],
        "language": task["language"],
        "success": test_passed,
        "solution_exists": solution_exists,
        "solution_size_bytes": solution_size,
        "tool_calls": tool_calls,
        "tool_rounds": len(result.tool_executions) if result.tool_executions else 0,
        "stop_reason": result.stop_reason,
        "elapsed_seconds": round(elapsed, 3),
        "test_output": test_output[:1000],
        "assistant_message": result.assistant_message[:500],
    }


def run_coding_benchmark(
    *,
    provider: str = "mock",
    model: str = "mock",
    tasks: list[str] | None = None,
) -> dict[str, Any]:
    active_tasks = [t for t in TASKS if tasks is None or t["id"] in tasks]

    results: list[dict[str, Any]] = []
    base_tmp = Path(tempfile.mkdtemp(prefix="kestrel_coding_"))

    print(f"Coding benchmark workspace: {base_tmp}")
    print(f"Tasks: {len(active_tasks)}")
    print(f"Provider: {provider}, Model: {model}")

    for task in active_tasks:
        workspace = base_tmp / task["id"]
        workspace.mkdir()
        print(f"\n[{task['id']}] Running...", file=sys.stderr)
        r = _run_task(task, workspace, provider, model)
        results.append(r)
        status = "PASS" if r["success"] else "FAIL"
        print(f"  [{status}] {task['id']}: {r['tool_rounds']} rounds, {r['elapsed_seconds']}s", file=sys.stderr)
        if not r["success"] and r.get("test_output"):
            print(f"  Test output: {r['test_output'][:300]}", file=sys.stderr)
        if not r["success"] and r.get("error"):
            print(f"  Error: {r['error'][:300]}", file=sys.stderr)

    # Cleanup
    shutil.rmtree(base_tmp, ignore_errors=True)

    success_count = sum(1 for r in results if r["success"])
    total_tool_calls = sum(len(r["tool_calls"]) for r in results)

    report = {
        "schema": "kestrel.coding_benchmark.v1",
        "config": {
            "provider": provider,
            "model": model,
            "tasks": [t["id"] for t in active_tasks],
        },
        "summary": {
            "total_tasks": len(active_tasks),
            "success_count": success_count,
            "success_rate": round(success_count / max(len(active_tasks), 1), 2),
            "total_tool_calls": total_tool_calls,
            "avg_tools_per_task": round(total_tool_calls / max(len(active_tasks), 1), 2),
        },
        "results": results,
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Kestrel coding benchmark")
    parser.add_argument("--provider", default="mock")
    parser.add_argument("--model", default="mock")
    parser.add_argument("--output", default="benchmark_results/coding_benchmark.json")
    parser.add_argument("--tasks", nargs="+", default=None, help="Task IDs to run")
    args = parser.parse_args()

    report = run_coding_benchmark(
        provider=args.provider,
        model=args.model,
        tasks=args.tasks,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nWrote report to {output_path}", file=sys.stderr)

    # Print summary
    print("\n" + "=" * 60)
    print("KESTREL CODING BENCHMARK RESULTS")
    print("=" * 60)
    for r in report["results"]:
        status = "PASS" if r["success"] else "FAIL"
        print(f"  [{status}] {r['task_id']} (difficulty {r['difficulty']})")
    print(f"\nScore: {report['summary']['success_count']}/{report['summary']['total_tasks']} ({report['summary']['success_rate'] * 100:.0f}%)")
    print("=" * 60)

    return 0 if report["summary"]["success_rate"] == 1.0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
