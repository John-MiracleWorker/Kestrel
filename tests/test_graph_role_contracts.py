"""Regression tests for the graph role-contract synthesis in Adaptive Flock.

Pins two P1 review defects fixed on the S4 live-reviewer-separation branch:

  * project routing constraints (local_required, allowed/forbidden target ids
    and provider profiles, budget) must flow into the synthesized
    executor/planner/reviewer contracts — a local-required project's reviewer
    must never resolve to a cloud target.
  * the reviewer diversity anchor must be derived from the *executed* config
    (``ctx.config``), not the ledger's synthetic executor decision, so a
    reviewer routed to the same provider/model as the real executor is never
    mis-labeled ``independent_target``.
"""

from __future__ import annotations

import os
from typing import Any

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.graph_runtime import GraphRunState
from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.routing.role_resolver import ReviewAuthority, RoleAssignmentResolver
from nested_memvid_agent.routing.run_manager import (
    AdaptiveFlockRunManager,
    _executor_diversity_anchor,
    _graph_role_contracts,
)
from nested_memvid_agent.state_store import AgentStateStore


def _project_bound_state(
    tmp_path: Any,
    *,
    privacy_class: str,
    provider_policy: dict[str, Any] | None = None,
) -> AgentStateStore:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    repo = os.path.realpath(str(tmp_path))
    state.create_project(
        project_id="proj",
        display_name="Proj",
        repository_path=repo,
        privacy_class=privacy_class,
        provider_policy=provider_policy,
    )
    state.create_run(
        run_id="run-local",
        message="review local work",
        session_id="s",
        workspace=repo,
        provider="mock",
        model="mock",
        project_id="proj",
    )
    state.create_task_node(
        task_id="root",
        run_id="run-local",
        title="Plan",
        goal="Review local work.",
        profile="planner",
        approved=True,
        required_tools=(),
        risk="low",
        acceptance_criteria=(),
    )
    return state


def _ctx(run_id: str = "run-local") -> GraphRunState:
    return GraphRunState(
        run_id=run_id,
        config=AgentConfig(),
        message="review local work",
        session_id="s",
    )


# ---------------------------------------------------------------------------
# P1: project routing constraints must flow into the graph role contracts.
# ---------------------------------------------------------------------------


class TestGraphRoleProjectConstraints:
    def test_local_required_project_contracts_carry_local_required(self, tmp_path: Any) -> None:
        state = _project_bound_state(tmp_path, privacy_class="local_required")
        executor, planner, reviewer = _graph_role_contracts(_ctx(), state=state)
        for contract in (executor, planner, reviewer):
            assert contract.local_required is True
            assert contract.privacy_class == "local_required"

    def test_project_provider_restrictions_flow_into_graph_contracts(
        self, tmp_path: Any
    ) -> None:
        state = _project_bound_state(
            tmp_path,
            privacy_class="approved_cloud",
            provider_policy={
                "forbidden_profiles": ["prov-banned"],
                "allowed_targets": ["tgt-allowed"],
            },
        )
        _, _, reviewer = _graph_role_contracts(_ctx(), state=state)
        assert reviewer.forbidden_provider_profiles == ("prov-banned",)
        assert reviewer.allowed_target_ids == ("tgt-allowed",)

    def test_local_required_project_reviewer_not_routed_to_cloud(self, tmp_path: Any) -> None:
        state = _project_bound_state(tmp_path, privacy_class="local_required")
        executor, planner, reviewer = _graph_role_contracts(_ctx(), state=state)

        profiles = (
            ProviderProfile(
                profile_id="prov-local",
                display_name="L",
                adapter="ollama",
                base_url="http://127.0.0.1:11434/v1",
                secret_ref=None,
                locality="local",
            ),
            ProviderProfile(
                profile_id="prov-cloud",
                display_name="C",
                adapter="openai_compatible",
                base_url="https://example.com",
                secret_ref="secret://cloud",
                locality="cloud",
            ),
        )
        targets = (
            ModelTarget(
                target_id="tgt-exec",
                provider_profile_id="prov-local",
                provider="ollama",
                model="exec-model",
                locality="local",
                role_affinities=("executor",),
                supports_json=True,
                supports_reasoning=True,
                quality_tier=4,
            ),
            ModelTarget(
                target_id="tgt-plan",
                provider_profile_id="prov-local",
                provider="ollama",
                model="plan-model",
                locality="local",
                role_affinities=("planner",),
                supports_json=True,
                supports_reasoning=True,
                quality_tier=4,
            ),
            ModelTarget(
                target_id="tgt-review-cloud",
                provider_profile_id="prov-cloud",
                provider="openai_compatible",
                model="review-model",
                locality="cloud",
                role_affinities=("reviewer",),
                supports_json=True,
                supports_reasoning=True,
                quality_tier=4,
            ),
        )
        resolver = RoleAssignmentResolver(
            profiles=profiles,
            targets=targets,
            policy=RoutePolicy(),
            mode="constrained",
        )
        assignment = resolver.resolve(executor, planner, reviewer)

        # The cloud reviewer target is rejected by the local_required
        # constraint; the reviewer must never land on a non-local target.
        if assignment.reviewer_decision is None:
            assert assignment.review_fallback is True
            assert "local_required" in assignment.review_rejection_reasons
        else:
            assert assignment.reviewer_decision.selected_target.locality == "local"
            assert assignment.reviewer_decision.selected_target.target_id != "tgt-review-cloud"


# ---------------------------------------------------------------------------
# P1: the reviewer diversity anchor must come from the executed config.
# ---------------------------------------------------------------------------


class TestExecutorDiversityAnchorFromExecutedConfig:
    def test_reviewer_not_independent_when_equal_to_executed_provider(
        self, tmp_path: Any
    ) -> None:
        state = AgentStateStore(tmp_path / "state" / "agent.db")
        repo = os.path.realpath(str(tmp_path))
        state.create_run(
            run_id="run-r",
            message="m",
            session_id="s",
            workspace=repo,
            provider="mock",
            model="mock",
        )
        state.create_task_node(
            task_id="root",
            run_id="run-r",
            title="Plan",
            goal="m",
            profile="planner",
            approved=True,
            required_tools=(),
            risk="low",
            acceptance_criteria=(),
        )

        ledger = RoutingLedger(state)
        ledger.put_provider_profile(
            ProviderProfile(
                profile_id="prov-cloud",
                display_name="C",
                adapter="openai_compatible",
                base_url="https://example.com",
                secret_ref="secret://c",
                locality="cloud",
            )
        )
        ledger.put_provider_profile(
            ProviderProfile(
                profile_id="prov-local",
                display_name="L",
                adapter="ollama",
                base_url="http://127.0.0.1:11434/v1",
                secret_ref=None,
                locality="local",
            )
        )
        ledger.put_model_target(
            ModelTarget(
                target_id="tgt-exec",
                provider_profile_id="prov-cloud",
                provider="openai_compatible",
                model="exec-model",
                locality="cloud",
                role_affinities=("executor",),
                quality_tier=4,
                metadata={"model_family": "family-a"},
            )
        )
        ledger.put_model_target(
            ModelTarget(
                target_id="tgt-review",
                provider_profile_id="prov-local",
                provider="ollama",
                model="review-model",
                locality="local",
                role_affinities=("reviewer",),
                supports_json=True,
                supports_reasoning=True,
                quality_tier=3,
                metadata={"model_family": "family-b"},
            )
        )
        ledger.put_policy(RoutePolicy(require_different_target_for_review=True))
        coordinator = DurableRoutingCoordinator(
            ledger,
            policy_id="balanced",
            mode="constrained",
        )

        manager = object.__new__(AdaptiveFlockRunManager)
        manager.routing_coordinator = coordinator  # type: ignore[attr-defined]
        manager.state = state  # type: ignore[attr-defined]
        resolve = manager._review_authority_resolver()  # type: ignore[attr-defined]

        # The actually-executed config is a direct ollama/review-model provider
        # — the SAME provider/model as the reviewer target, and DIFFERENT from
        # the ledger's top executor target (openai_compatible/exec-model).
        ctx = GraphRunState(
            run_id="run-r",
            config=AgentConfig(provider="ollama", model="review-model"),
            message="m",
            session_id="s",
        )
        assignment = resolve(ctx)

        assert assignment is not None
        # The reviewer target equals the executed provider/model, so it must
        # NOT be labeled independent of the real executor.
        assert assignment.review_authority == ReviewAuthority.DETERMINISTIC_FALLBACK
        assert assignment.reviewer_decision is None
        assert "review_target_not_independent" in assignment.review_rejection_reasons


class TestExecutorDiversityAnchorHelper:
    def test_anchor_derived_from_executed_config(self) -> None:
        profiles = (
            ProviderProfile(
                profile_id="prov-cloud",
                display_name="C",
                adapter="openai_compatible",
                base_url="https://example.com",
                secret_ref="secret://c",
                locality="cloud",
            ),
            ProviderProfile(
                profile_id="prov-local",
                display_name="L",
                adapter="ollama",
                base_url="http://127.0.0.1:11434/v1",
                secret_ref=None,
                locality="local",
            ),
        )
        targets = (
            ModelTarget(
                target_id="tgt-exec",
                provider_profile_id="prov-cloud",
                provider="openai_compatible",
                model="exec-model",
                locality="cloud",
                metadata={"model_family": "family-a"},
            ),
            ModelTarget(
                target_id="tgt-review",
                provider_profile_id="prov-local",
                provider="ollama",
                model="review-model",
                locality="local",
                metadata={"model_family": "family-b"},
            ),
        )
        anchor = _executor_diversity_anchor(
            AgentConfig(provider="ollama", model="review-model"),
            targets=targets,
            profiles=profiles,
        )
        assert anchor is not None
        assert anchor.target_id == "tgt-review"
        assert anchor.provider_profile_id == "prov-local"
        assert anchor.model_family == "family-b"

    def test_anchor_is_none_when_config_matches_no_ledger_target(self) -> None:
        profiles = (
            ProviderProfile(
                profile_id="prov-cloud",
                display_name="C",
                adapter="openai_compatible",
                base_url="https://example.com",
                secret_ref="secret://c",
                locality="cloud",
            ),
        )
        targets = (
            ModelTarget(
                target_id="tgt-exec",
                provider_profile_id="prov-cloud",
                provider="openai_compatible",
                model="exec-model",
                locality="cloud",
            ),
        )
        anchor = _executor_diversity_anchor(
            AgentConfig(provider="ollama", model="review-model"),
            targets=targets,
            profiles=profiles,
        )
        assert anchor is None
