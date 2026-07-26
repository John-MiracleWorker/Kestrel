#!/usr/bin/env python3
"""Run a deterministic Adaptive Flock routing demonstration.

This script exercises Kestrel's real routing contracts and scoring logic. It
does not invoke model providers, execute the selected assignments, modify
files, or perform Git operations.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "src"))

from nested_memvid_agent.routing.models import (  # noqa: E402
    AgentTaskContract,
    ModelTarget,
    RouteDecision,
    RoutePolicy,
    RoutingMode,
)
from nested_memvid_agent.routing.router import (  # noqa: E402
    ReviewDiversityContext,
    route_task,
)


def build_demo_report(mode: RoutingMode = "shadow") -> dict[str, Any]:
    """Build a provider-free report for a planner/executor/reviewer route."""

    targets = _demo_targets()
    policy = RoutePolicy(
        policy_id="adaptive-flock-demo",
        require_different_target_for_review=True,
        require_different_model_family_for_review=True,
        prefer_different_provider_for_review=True,
    )

    planner = route_task(
        _contract(
            task_id="plan-change",
            role="planner",
            task_family="planning",
            objective="Plan a bounded implementation change.",
            complexity=0.70,
            ambiguity=0.50,
            risk="medium",
            required_capabilities=("reasoning",),
            minimum_context_tokens=32_000,
            structured_output_required=True,
            preferred_target_tags=("planning",),
        ),
        targets,
        policy=policy,
        mode=mode,
    )
    executor = route_task(
        _contract(
            task_id="implement-change",
            role="executor",
            task_family="bounded_code_change",
            objective="Implement the plan locally with tool support.",
            complexity=0.75,
            ambiguity=0.30,
            risk="high",
            required_tools=("patch.apply", "test.run"),
            required_capabilities=("tools", "reasoning"),
            minimum_context_tokens=32_000,
            local_required=True,
            maximum_cost_usd=0.01,
            preferred_target_tags=("coding",),
        ),
        targets,
        policy=policy,
        mode=mode,
    )
    reviewer = route_task(
        _contract(
            task_id="review-change",
            role="reviewer",
            task_family="review",
            objective="Review the implementation with an independent model family.",
            complexity=0.65,
            ambiguity=0.35,
            risk="high",
            required_capabilities=("reasoning",),
            minimum_context_tokens=64_000,
            structured_output_required=True,
            maximum_cost_usd=0.10,
            preferred_target_tags=("review",),
        ),
        targets,
        policy=policy,
        mode=mode,
        review_context=ReviewDiversityContext(
            target_id=executor.selected_target.target_id,
            provider_profile_id=executor.selected_target.provider_profile_id,
            model_family=str(executor.selected_target.metadata["model_family"]),
        ),
    )

    routes = [
        _route_payload("planner", planner),
        _route_payload("executor", executor),
        _route_payload("reviewer", reviewer),
    ]
    return {
        "demo": "kestrel-adaptive-flock-routing",
        "mode": mode,
        "routing_only": True,
        "actionable": all(route["actionable"] for route in routes),
        "routes": routes,
        "safety": {
            "provider_calls": 0,
            "files_modified": False,
            "git_changes": False,
            "merges_or_pushes": False,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect deterministic Adaptive Flock routing without calling providers."
    )
    parser.add_argument(
        "--mode",
        choices=("off", "shadow", "constrained", "adaptive"),
        default="shadow",
        help="Routing mode to report. Shadow is the safe, non-actionable default.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    report = build_demo_report(mode=args.mode)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render_text(report))
    return 0


def _contract(
    *,
    task_id: str,
    role: str,
    task_family: str,
    objective: str,
    complexity: float,
    ambiguity: float,
    risk: str,
    required_tools: tuple[str, ...] = (),
    required_capabilities: tuple[str, ...] = (),
    minimum_context_tokens: int | None = None,
    structured_output_required: bool = False,
    local_required: bool = False,
    maximum_cost_usd: float | None = None,
    preferred_target_tags: tuple[str, ...] = (),
) -> AgentTaskContract:
    return AgentTaskContract(
        task_id=task_id,
        run_id="adaptive-flock-demo",
        role=role,
        task_family=task_family,
        objective=objective,
        complexity=complexity,
        ambiguity=ambiguity,
        risk=risk,
        required_tools=required_tools,
        required_capabilities=required_capabilities,
        minimum_context_tokens=minimum_context_tokens,
        structured_output_required=structured_output_required,
        local_required=local_required,
        maximum_cost_usd=maximum_cost_usd,
        preferred_target_tags=preferred_target_tags,
    )


def _demo_targets() -> tuple[ModelTarget, ...]:
    return (
        ModelTarget(
            target_id="planner-cloud",
            provider_profile_id="demo-planner",
            provider="demo",
            model="planner",
            locality="cloud",
            capability_tags=("planning", "reasoning"),
            role_affinities=("planner",),
            task_family_affinities=("planning",),
            max_context_tokens=128_000,
            supports_json=True,
            supports_reasoning=True,
            quality_tier=5,
            latency_tier=3,
            operator_priority=3,
            estimated_cost_usd=0.05,
            health="healthy",
            predicted_success=0.92,
            metadata={"model_family": "planner-family"},
        ),
        ModelTarget(
            target_id="coder-local",
            provider_profile_id="demo-local",
            provider="demo",
            model="coder",
            locality="local",
            capability_tags=("coding", "tools"),
            role_affinities=("executor",),
            task_family_affinities=("bounded_code_change",),
            max_context_tokens=64_000,
            supports_tools=True,
            supports_json=True,
            supports_reasoning=True,
            quality_tier=4,
            latency_tier=1,
            operator_priority=5,
            estimated_cost_usd=0.0,
            health="healthy",
            predicted_success=0.90,
            metadata={"model_family": "coder-family"},
        ),
        ModelTarget(
            target_id="reviewer-independent",
            provider_profile_id="demo-reviewer",
            provider="demo",
            model="reviewer",
            locality="cloud",
            capability_tags=("review", "reasoning"),
            role_affinities=("reviewer",),
            task_family_affinities=("review",),
            max_context_tokens=128_000,
            supports_json=True,
            supports_reasoning=True,
            quality_tier=4,
            latency_tier=3,
            operator_priority=2,
            estimated_cost_usd=0.03,
            health="healthy",
            predicted_success=0.91,
            metadata={"model_family": "reviewer-family"},
        ),
        ModelTarget(
            target_id="tiny-local",
            provider_profile_id="demo-tiny-local",
            provider="demo",
            model="tiny",
            locality="local",
            capability_tags=("lightweight",),
            role_affinities=("executor",),
            task_family_affinities=("bounded_code_change",),
            max_context_tokens=4_000,
            quality_tier=1,
            latency_tier=1,
            estimated_cost_usd=0.0,
            health="healthy",
            predicted_success=0.65,
            metadata={"model_family": "tiny-family"},
        ),
    )


def _route_payload(role: str, decision: RouteDecision) -> dict[str, Any]:
    payload = decision.to_payload()
    return {
        "role": role,
        "selected_target_id": payload["selected_target_id"],
        "selection_kind": payload["selection_kind"],
        "score": payload["score"],
        "reason_codes": payload["reason_codes"],
        "actionable": payload["actionable"],
        "candidates": payload["candidates"],
    }


def _render_text(report: dict[str, Any]) -> str:
    lines = [
        "Kestrel Adaptive Flock routing demo",
        f"Mode: {report['mode']} (actionable: {str(report['actionable']).lower()})",
        "",
    ]
    for route in report["routes"]:
        lines.append(
            f"{route['role']}: {route['selected_target_id']} "
            f"(score={route['score']:.4f}, actionable={str(route['actionable']).lower()})"
        )
    lines.extend(
        [
            "",
            "Routing only: no provider calls, task execution, file changes, commits, pushes, or merges.",
        ]
    )
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
