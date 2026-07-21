"""Run the full Kestrel benchmark suite and emit a consolidated report.

Usage:
    python benchmarks/run_all.py --output results/benchmark_report.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent_benchmark import run_agent_benchmark
from error_recovery_benchmark import run_error_recovery_benchmark
from learning_benchmark import run_learning_benchmark
from memory_benchmark import run_memory_benchmark

_MEMORY_QUALITY_FLOOR_VERSION = "kestrel.aggregate-memory-quality-floor.v1"
_MEMORY_QUALITY_FLOOR_V1 = {
    "recall_at_k": 0.80,
    "precision_at_k": 0.20,
    "mrr": 0.75,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run all Kestrel benchmarks.")
    parser.add_argument("--memory-k", type=int, default=5)
    parser.add_argument("--agent-provider", default="mock")
    parser.add_argument("--agent-model", default="mock")
    parser.add_argument("--agent-backend", default="memory")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--api-key-env", default=None)
    parser.add_argument("--output", type=Path, default=Path("benchmark_results/report.json"))
    parser.add_argument("--memory-only", action="store_true")
    parser.add_argument("--agent-only", action="store_true")
    parser.add_argument("--error-recovery-only", action="store_true")
    parser.add_argument("--learning-only", action="store_true")
    args = parser.parse_args()

    only_flags = {
        "memory": args.memory_only,
        "agent": args.agent_only,
        "error_recovery": args.error_recovery_only,
        "learning": args.learning_only,
    }
    if sum(bool(enabled) for enabled in only_flags.values()) > 1:
        parser.error("select at most one --*-only benchmark")
    run_everything = not any(only_flags.values())

    report: dict[str, object] = {
        "schema": "kestrel.benchmark_report.v1",
        "timestamp": datetime.now(UTC).isoformat(),
    }
    assertions: dict[str, bool] = {}

    if run_everything or args.memory_only:
        print("Running memory benchmark...", file=sys.stderr)
        memory = run_memory_benchmark(k=args.memory_k)
        report["memory"] = memory
        memory_overall = memory["overall"]
        assertions["memory_standalone_quality_gate_passed"] = bool(
            memory.get("acceptance", {}).get("passed")
        )
        assertions["memory_recall_not_below_baseline"] = (
            memory_overall["kestrel"]["recall_at_k"] >= memory_overall["baseline"]["recall_at_k"]
        )
        assertions["memory_mrr_not_below_baseline"] = (
            memory_overall["kestrel"]["mrr"] >= memory_overall["baseline"]["mrr"]
        )
        memory_floor_checks = {
            f"memory_{metric}_at_or_above_absolute_floor": (
                float(memory_overall["kestrel"][metric]) >= minimum
            )
            for metric, minimum in _MEMORY_QUALITY_FLOOR_V1.items()
        }
        assertions.update(memory_floor_checks)
        report["memory_quality_gate"] = {
            "version": _MEMORY_QUALITY_FLOOR_VERSION,
            "minimums": dict(_MEMORY_QUALITY_FLOOR_V1),
            "observed": {
                metric: float(memory_overall["kestrel"][metric])
                for metric in _MEMORY_QUALITY_FLOOR_V1
            },
            "checks": memory_floor_checks,
            "passed": all(memory_floor_checks.values()),
        }

    if run_everything or args.agent_only:
        print("Running agent benchmark...", file=sys.stderr)
        agent = run_agent_benchmark(
            provider=args.agent_provider,
            model=args.agent_model,
            backend=args.agent_backend,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
        report["agent"] = agent
        agent_results = agent.get("results", [])
        assertions["all_agent_tasks_succeeded"] = (
            bool(agent_results)
            and len(agent_results) == agent["summary"]["total_tasks"]
            and agent["summary"]["success_count"] == agent["summary"]["total_tasks"]
            and all(bool(result.get("success")) for result in agent_results)
        )

    if run_everything or args.error_recovery_only:
        print("Running error recovery benchmark...", file=sys.stderr)
        error_recovery = run_error_recovery_benchmark(
            provider=args.agent_provider,
            model=args.agent_model,
            backend=args.agent_backend,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
        report["error_recovery"] = error_recovery
        recovery_results = error_recovery.get("results", [])
        assertions["all_error_recovery_tasks_succeeded"] = (
            bool(recovery_results)
            and len(recovery_results) == error_recovery["summary"]["total_tasks"]
            and error_recovery["summary"]["success_count"]
            == error_recovery["summary"]["total_tasks"]
            and all(bool(result.get("success")) for result in recovery_results)
        )

    if run_everything or args.learning_only:
        print("Running learning benchmark...", file=sys.stderr)
        learning = run_learning_benchmark()
        report["learning"] = learning
        assertions["learning_gate_passed"] = bool(learning["passed"])

    report["acceptance"] = {
        "assertions": assertions,
        "passed": bool(assertions) and all(assertions.values()),
    }

    print(json.dumps(report, indent=2))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(f"\nWrote consolidated report to {args.output}", file=sys.stderr)
    return 0 if report["acceptance"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
