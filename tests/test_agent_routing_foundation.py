from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nested_memvid_agent.routing import (
    ModelTarget,
    ReviewDiversityContext,
    RoutePolicy,
    RoutingUnavailableError,
    compile_task_contract,
    route_task,
)


@dataclass(frozen=True)
class _Task:
    task_id: str = "task-1"
    run_id: str = "run-1"
    title: str = "Update React Card component"
    goal: str = "Change only Card.tsx and run the targeted tests."
    profile: str = "worker"
    risk: str = "low"
    required_tools: tuple[str, ...] = ("patch.apply", "test.run")
    acceptance_criteria: tuple[str, ...] = ("Targeted tests pass.",)
    dependencies: tuple[str, ...] = ()
    plan: dict[str, Any] = field(
        default_factory=lambda: {"acceptance_evidence": ["validation_tools"]}
    )


def _target(target_id: str, **changes: Any) -> ModelTarget:
    values: dict[str, Any] = {
        "target_id": target_id,
        "provider_profile_id": f"profile-{target_id}",
        "provider": "openai-compatible",
        "model": target_id,
        "locality": "local",
        "capability_tags": ("coding", "bounded_code_change", "worker"),
        "role_affinities": ("worker",),
        "task_family_affinities": ("bounded_code_change",),
        "max_context_tokens": 64_000,
        "supports_tools": True,
        "supports_json": True,
        "supports_reasoning": True,
        "quality_tier": 3,
        "latency_tier": 2,
        "estimated_cost_usd": 0.02,
        "health": "healthy",
    }
    values.update(changes)
    return ModelTarget(**values)


def test_contract_preserves_authoritative_task_requirements() -> None:
    contract = compile_task_contract(_Task())

    assert contract.task_family == "frontend_implementation"
    assert contract.risk == "low"
    assert contract.required_tools == ("patch.apply", "test.run")
    assert "tools" in contract.required_capabilities
    assert contract.structured_output_required is True
    assert contract.minimum_context_tokens == 32_000


def test_planner_guidance_can_enrich_but_not_lower_deterministic_requirements() -> None:
    task = _Task(
        risk="high",
        title="Review security architecture",
        goal="Determine the safest design.",
    )
    baseline = compile_task_contract(task)
    guided = compile_task_contract(
        task,
        planner_guidance={
            "task_family": "security_review",
            "complexity": 0.1,
            "ambiguity": 0.1,
            "required_capabilities": ["vision"],
            "minimum_context_tokens": 16_000,
        },
    )

    assert guided.complexity >= baseline.complexity
    assert guided.ambiguity >= baseline.ambiguity
    assert {"tools", "structured_output", "reasoning", "vision"} <= set(
        guided.required_capabilities
    )
    assert guided.minimum_context_tokens >= baseline.minimum_context_tokens


def test_local_required_contract_rejects_cloud_target() -> None:
    contract = compile_task_contract(_Task(), local_required=True)
    local = _target("local")
    cloud = _target("cloud", locality="cloud")

    decision = route_task(contract, [cloud, local], mode="constrained")

    assert decision.selected_target.target_id == "local"
    rejected = next(item for item in decision.candidates if item.target.target_id == "cloud")
    assert "local_required" in rejected.reason_codes


def test_tool_required_contract_rejects_target_without_tools() -> None:
    contract = compile_task_contract(_Task())
    no_tools = _target("no-tools", supports_tools=False)

    with pytest.raises(RoutingUnavailableError) as caught:
        route_task(contract, [no_tools], mode="constrained")

    assert "tools_unsupported" in caught.value.reason_codes


def test_shadow_decision_is_recordable_but_not_actionable() -> None:
    contract = compile_task_contract(_Task())

    decision = route_task(contract, [_target("worker")], mode="shadow")

    assert decision.selected_target.target_id == "worker"
    assert decision.actionable is False


def test_direct_override_fails_closed_when_target_is_ineligible() -> None:
    contract = compile_task_contract(_Task(), local_required=True)
    cloud = _target("cloud", locality="cloud")

    with pytest.raises(RoutingUnavailableError) as caught:
        route_task(contract, [cloud], direct_target_id="cloud", mode="adaptive")

    assert "local_required" in caught.value.reason_codes


def test_high_risk_task_enforces_quality_floor() -> None:
    contract = compile_task_contract(_Task(risk="high"))
    weak = _target("weak", quality_tier=2)
    strong = _target("strong", quality_tier=4, estimated_cost_usd=0.3)

    decision = route_task(contract, [weak, strong], mode="adaptive")

    assert decision.selected_target.target_id == "strong"
    rejected = next(item for item in decision.candidates if item.target.target_id == "weak")
    assert "quality_below_risk_floor" in rejected.reason_codes


def test_reviewer_can_require_different_model_family() -> None:
    task = _Task(
        profile="reviewer",
        title="Review implementation",
        goal="Review the final diff.",
        required_tools=(),
    )
    contract = compile_task_contract(task)
    same = _target(
        "same-family",
        role_affinities=("reviewer",),
        task_family_affinities=("review",),
        metadata={"model_family": "qwen"},
    )
    independent = _target(
        "independent",
        provider_profile_id="profile-other",
        role_affinities=("reviewer",),
        task_family_affinities=("review",),
        metadata={"model_family": "gemini"},
    )
    policy = RoutePolicy(require_different_model_family_for_review=True)

    decision = route_task(
        contract,
        [same, independent],
        policy=policy,
        mode="adaptive",
        review_context=ReviewDiversityContext(model_family="qwen"),
    )

    assert decision.selected_target.target_id == "independent"
    rejected = next(
        item for item in decision.candidates if item.target.target_id == "same-family"
    )
    assert "review_model_family_not_independent" in rejected.reason_codes


def test_routing_tie_break_is_stable() -> None:
    contract = compile_task_contract(_Task())
    alpha = _target("alpha")
    beta = _target("beta")

    first = route_task(contract, [beta, alpha], mode="adaptive")
    second = route_task(contract, [alpha, beta], mode="adaptive")

    assert first.selected_target.target_id == "alpha"
    assert second.selected_target.target_id == "alpha"
    assert first.score == second.score
