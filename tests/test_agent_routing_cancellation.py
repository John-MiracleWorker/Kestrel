from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing import (
    DurableRoutingCoordinator,
    ModelTarget,
    ProviderProfile,
    RoutePolicy,
    RoutingLedger,
)
from nested_memvid_agent.state_store import AgentStateStore


def test_cancelled_outcome_terminalizes_decision_as_cancelled(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    state.create_run(
        run_id="run-cancelled-route",
        message="Inspect and then cancel",
        session_id="session-cancelled-route",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = state.create_task_node(
        task_id="task-cancelled-route",
        run_id="run-cancelled-route",
        title="Inspect repository",
        goal="Gather repository context.",
        profile="worker",
        approved=True,
        required_tools=("repo.search",),
        risk="low",
        acceptance_criteria=(),
    )
    ledger = RoutingLedger(state)
    ledger.put_provider_profile(
        ProviderProfile(
            profile_id="local",
            display_name="Local model server",
            adapter="openai-compatible",
            base_url="http://127.0.0.1:1234/v1",
            locality="local",
        )
    )
    ledger.put_model_target(
        ModelTarget(
            target_id="local-scout",
            provider_profile_id="local",
            provider="openai-compatible",
            model="qwen-coder",
            locality="local",
            capability_tags=("repository_inspection", "scout", "worker"),
            role_affinities=("worker",),
            task_family_affinities=("repository_inspection",),
            max_context_tokens=64_000,
            supports_tools=True,
            supports_reasoning=True,
            quality_tier=3,
            health="healthy",
        )
    )
    ledger.put_policy(RoutePolicy())
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    durable = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
    coordinator.mark_started(durable)

    coordinator.record_outcome(
        durable,
        execution_status="cancelled",
        validation_passed=False,
        validation_codes=("cancelled",),
        outcome_labels=("cancelled",),
    )

    decision = ledger.get_decision(durable.record.decision_id)
    assert decision is not None
    assert decision.status == "cancelled"
