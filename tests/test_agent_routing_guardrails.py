from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from nested_memvid_agent.routing import (
    AdaptiveFlockRoutingService,
    ModelTarget,
    ProviderProfile,
    compile_task_contract,
)


@dataclass(frozen=True)
class _Task:
    task_id: str = "task-security"
    run_id: str = "run-security"
    title: str = "Review authentication security"
    goal: str = "Identify vulnerabilities and trust-boundary flaws."
    profile: str = "worker"
    risk: str = "high"
    required_tools: tuple[str, ...] = ("repo.search",)
    acceptance_criteria: tuple[str, ...] = ("Security risks are evidence-linked.",)
    dependencies: tuple[str, ...] = ()
    plan: dict[str, Any] = field(default_factory=dict)


def test_planner_guidance_cannot_reclassify_protected_security_task() -> None:
    contract = compile_task_contract(
        _Task(),
        planner_guidance={
            "task_family": "documentation",
            "minimum_context_tokens": 8_000,
        },
    )

    assert contract.task_family == "security_review"
    assert contract.minimum_context_tokens >= 64_000
    assert "reasoning" in contract.required_capabilities


def test_service_rejects_target_profile_locality_mismatch() -> None:
    profile = ProviderProfile(
        profile_id="local",
        display_name="Local model server",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        locality="local",
    )
    target = ModelTarget(
        target_id="misdeclared-cloud-target",
        provider_profile_id="local",
        provider="openai-compatible",
        model="remote-model",
        locality="cloud",
        max_context_tokens=64_000,
        supports_tools=True,
        supports_json=True,
        supports_reasoning=True,
        quality_tier=3,
    )

    with pytest.raises(ValueError, match="does not match profile locality"):
        AdaptiveFlockRoutingService(
            profiles=[profile],
            targets=[target],
            mode="shadow",
        )
