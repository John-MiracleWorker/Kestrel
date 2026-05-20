from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.learning_eval import (  # noqa: E402
    LearningEvalOptions,
    list_learning_eval_scenarios,
    load_learning_eval_scenario,
    run_learning_eval_suite,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Kestrel's guarded learning architecture eval harness.")
    parser.add_argument("--provider", choices=["mock", "openai", "openai-compatible"], default="mock")
    parser.add_argument("--model", default=None)
    parser.add_argument("--backend", choices=["memory", "memvid"], default="memory")
    parser.add_argument("--memory-dir", type=Path, default=None)
    parser.add_argument("--workspace", type=Path, default=None)
    parser.add_argument("--scenario", default=None, help="Scenario id or JSON fixture path.")
    parser.add_argument("--all", action="store_true", help="Run all compatible scenarios.")
    parser.add_argument("--json", action="store_true", help="Emit a JSON result payload.")
    parser.add_argument("--report", type=Path, default=None, help="Write a Markdown report.")
    parser.add_argument("--max-cost-usd", type=float, default=1.0)
    parser.add_argument("--max-llm-calls", type=int, default=8)
    parser.add_argument("--max-tool-calls", type=int, default=8)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--base-url", default=None, help="Base URL for openai-compatible providers.")
    parser.add_argument("--api-key-env", default=None, help="Provider API key env var name.")
    args = parser.parse_args()

    if not args.all and not args.scenario:
        parser.error("provide --scenario or --all")

    scenarios = list_learning_eval_scenarios() if args.all else [load_learning_eval_scenario(args.scenario)]
    options = LearningEvalOptions(
        provider=args.provider,
        model=args.model,
        backend=args.backend,
        memory_dir=args.memory_dir,
        workspace=args.workspace,
        report_path=args.report,
        max_cost_usd=args.max_cost_usd,
        max_llm_calls=args.max_llm_calls,
        max_tool_calls=args.max_tool_calls,
        timeout_seconds=args.timeout_seconds,
        keep_artifacts=args.keep_artifacts,
        dry_run=args.dry_run,
        base_url=args.base_url,
        api_key_env=args.api_key_env,
    )
    report = run_learning_eval_suite(scenarios, options)
    payload = report.to_payload()

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        summary = payload["summary"]
        print("Kestrel learning architecture eval")
        print(f"Status: {payload['status']}")
        print(
            "Scenarios: "
            f"{summary['scenario_count']} total, {summary['passed']} passed, "
            f"{summary['failed']} failed, {summary['skipped']} skipped"
        )
        print(f"LLM calls: {summary['llm_calls']}")
        print(f"Tool calls: {summary['tool_calls']}")
        print(f"Estimated cost USD: {summary['estimated_cost_usd']}")
        if args.report:
            print(f"Report: {args.report}")
        for result in payload["results"]:
            reason = f" ({result['skipped_reason']})" if result.get("skipped_reason") else ""
            print(f"- {result['scenario_id']}: {result['status']}{reason}")

    return 1 if report.status == "fail" else 0


if __name__ == "__main__":
    raise SystemExit(main())
