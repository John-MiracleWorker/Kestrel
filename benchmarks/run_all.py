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
from memory_benchmark import run_memory_benchmark


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
    args = parser.parse_args()

    report: dict[str, object] = {
        "schema": "kestrel.benchmark_report.v1",
        "timestamp": datetime.now(UTC).isoformat(),
    }

    if not args.agent_only and not args.error_recovery_only:
        print("Running memory benchmark...", file=sys.stderr)
        report["memory"] = run_memory_benchmark(k=args.memory_k)

    if not args.memory_only and not args.error_recovery_only:
        print("Running agent benchmark...", file=sys.stderr)
        report["agent"] = run_agent_benchmark(
            provider=args.agent_provider,
            model=args.agent_model,
            backend=args.agent_backend,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )

    if not args.memory_only and not args.agent_only:
        print("Running error recovery benchmark...", file=sys.stderr)
        report["error_recovery"] = run_error_recovery_benchmark(
            provider=args.agent_provider,
            model=args.agent_model,
            backend=args.agent_backend,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )

    print(json.dumps(report, indent=2))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2))
    print(f"\nWrote consolidated report to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
