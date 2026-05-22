from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nested_memvid_agent.behavior_delta_ledger import BehaviorDeltaLedger
from nested_memvid_agent.learned_routing import (
    OutcomeCalibratedRouter,
    behavior_delta_shadow_examples_from_ledger,
    evaluate_behavior_delta_shadow_examples,
    evaluate_routing_examples,
    routing_examples_from_ledger,
)
from nested_memvid_agent.promotion_ledger import PromotionLedger
from nested_memvid_agent.state_store import AgentStateStore


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay-evaluate Kestrel memory-routing policies.")
    parser.add_argument("--state-db", type=Path, default=Path(".nest/state/agent.db"))
    parser.add_argument("--mode", choices=["replay"], default="replay")
    parser.add_argument("--baseline", choices=["rule"], default="rule")
    parser.add_argument("--candidate", choices=["oracle"], default="oracle")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-utility-delta", type=float, default=0.15)
    parser.add_argument("--confidence-threshold", type=float, default=0.65)
    parser.add_argument("--activation-margin", type=float, default=0.05)
    parser.add_argument("--min-examples-per-target", type=int, default=3)
    parser.add_argument("--include-behavior-deltas", action="store_true")
    parser.add_argument("--fail-on-regression", action="store_true")
    args = parser.parse_args()

    ledger = PromotionLedger(AgentStateStore(args.state_db))
    examples = routing_examples_from_ledger(ledger)
    router = OutcomeCalibratedRouter.fit(
        examples,
        mode="constrained",
        confidence_threshold=args.confidence_threshold,
        activation_margin=args.activation_margin,
        min_examples_per_target=args.min_examples_per_target,
    )
    report = evaluate_routing_examples(examples, router, min_utility_delta=args.min_utility_delta)
    payload = report.to_payload()
    if args.include_behavior_deltas:
        delta_ledger = BehaviorDeltaLedger(AgentStateStore(args.state_db))
        delta_examples = behavior_delta_shadow_examples_from_ledger(delta_ledger)
        payload["behavior_delta_shadow"] = evaluate_behavior_delta_shadow_examples(delta_examples).to_payload()
    if args.json:
        print(json.dumps(payload, indent=2))
        return 1 if args.fail_on_regression and not payload["improvement"]["passes"] else 0

    print("Memory router replay")
    print(f"State DB: {args.state_db}")
    print(f"Examples: {payload['baseline']['examples']}")
    print()
    print(f"Rule expected utility: {payload['baseline']['expected_utility']}")
    print(f"ORACLE expected utility: {payload['oracle']['expected_utility']}")
    print(f"Expected utility delta: {payload['improvement']['expected_utility_delta']}")
    print(f"Rule false-positive rate: {payload['baseline']['false_positive_rate']}")
    print(f"ORACLE false-positive rate: {payload['oracle']['false_positive_rate']}")
    print(f"ORACLE never-retrieved rate: {payload['oracle']['never_retrieved_rate']}")
    print(f"ORACLE abstention rate: {payload['oracle']['abstention_rate']}")
    print(f"ORACLE gate violations: {payload['oracle']['gate_violations']}")
    print(f"Passes: {payload['improvement']['passes']}")
    if "behavior_delta_shadow" in payload:
        delta_shadow = payload["behavior_delta_shadow"]
        print()
        print("Behavior-delta ORACLE shadow")
        print(f"Authority: {delta_shadow['authority']}")
        print(f"Gate authority: {delta_shadow['gate_authority']}")
        print(f"Policy write authority: {delta_shadow['policy_write_authority']}")
        print(f"Examples: {delta_shadow['summary']['examples']}")
        print(f"Review/rollback recommendations: {delta_shadow['summary']['review_or_rollback']}")
    return 1 if args.fail_on_regression and not payload["improvement"]["passes"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
