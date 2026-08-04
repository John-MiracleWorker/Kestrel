"""Deterministic fakes for the Flock qualification executor harness (Task 8).

The fake provider emits exact provider attempts — token usage, latency, and
typed failure categories — from fixture script inputs, and records every
request it receives so tests can prove containment and eligibility gates run
before any provider contact. Nothing here is random or time-dependent.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.contracts import compile_task_contract
from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import (
    ModelTarget,
    ProviderProfile,
    RoutePolicy,
)
from nested_memvid_agent.routing.qualification_executor import (
    AttemptLease,
    ProviderAttempt,
    ProviderRequest,
    QualificationExecutor,
)
from nested_memvid_agent.routing.qualification_models import MoneyMicros
from nested_memvid_agent.routing.qualification_workspace import QualificationWorkspace
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord

RUN_ID = "run-qual"
CASE_ID = "case-qual-1"

DEFAULT_INPUT_TOKENS = 1_000
DEFAULT_OUTPUT_TOKENS = 500
DEFAULT_LATENCY_SECONDS = 0.25


@dataclass(frozen=True)
class FakeAttemptSpec:
    """Scripted deterministic provider outcome for one target."""

    output: str = "fixture output"
    input_tokens: int = DEFAULT_INPUT_TOKENS
    output_tokens: int = DEFAULT_OUTPUT_TOKENS
    latency_seconds: float = DEFAULT_LATENCY_SECONDS
    failure_category: str | None = None


class DeterministicFakeProvider:
    """Provider fake that replays exact scripted attempts per target.

    Every request is appended to ``calls`` before the scripted response is
    returned; an unscripted target raises ``KeyError`` so tests can never pass
    through an accidental provider contact.
    """

    def __init__(self, script: Mapping[str, FakeAttemptSpec] | None = None) -> None:
        self._script = dict(script or {})
        self.calls: list[ProviderRequest] = []

    def execute(self, request: ProviderRequest) -> ProviderAttempt:
        self.calls.append(request)
        spec = self._script[request.target_id]
        return ProviderAttempt(
            target_id=request.target_id,
            provider=request.provider,
            model=request.model,
            output=spec.output,
            input_tokens=spec.input_tokens,
            output_tokens=spec.output_tokens,
            latency_seconds=spec.latency_seconds,
            failure_category=spec.failure_category,
        )


def fake_profile() -> ProviderProfile:
    return ProviderProfile(
        profile_id="fake-local",
        display_name="Deterministic fake provider",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:9/v1",
        locality="local",
    )


def fake_target(target_id: str, *, enabled: bool = True) -> ModelTarget:
    return ModelTarget(
        target_id=target_id,
        provider_profile_id="fake-local",
        provider="openai-compatible",
        model=f"fake-model-{target_id}",
        enabled=enabled,
        locality="local",
        capability_tags=("repository_inspection", "scout", "worker"),
        role_affinities=("worker",),
        task_family_affinities=("repository_inspection",),
        max_context_tokens=64_000,
        supports_tools=True,
        supports_json=True,
        supports_reasoning=True,
        quality_tier=3,
        latency_tier=1,
        estimated_cost_usd=0.0,
        input_cost_per_million_usd=1.0,
        output_cost_per_million_usd=2.0,
        health="healthy",
    )


@dataclass
class FakeExecutorHarness:
    """Wired executor under test plus its deterministic doubles."""

    state: AgentStateStore
    ledger: RoutingLedger
    coordinator: DurableRoutingCoordinator
    workspace: QualificationWorkspace
    provider: DeterministicFakeProvider
    executor: QualificationExecutor
    workspace_root: Path


def build_fake_harness(
    root: Path,
    *,
    script: Mapping[str, FakeAttemptSpec] | None = None,
    containment_available: bool = True,
    target_ids: tuple[str, ...] = ("target_a", "target_b"),
    disabled_target_ids: tuple[str, ...] = (),
    validator: object = None,
) -> FakeExecutorHarness:
    """Wire a qualification executor against fakes and an in-tree state store."""

    state = AgentStateStore(root / "state" / "agent.db")
    state.create_run(
        run_id=RUN_ID,
        message="Qualify flock targets",
        session_id="session-qual",
        workspace=str(root),
        provider="mock",
        model="mock",
    )
    ledger = RoutingLedger(state)
    ledger.put_provider_profile(fake_profile())
    for target_id in target_ids:
        ledger.put_model_target(fake_target(target_id))
    for target_id in disabled_target_ids:
        ledger.put_model_target(fake_target(target_id, enabled=False))
    ledger.put_policy(RoutePolicy())
    coordinator = DurableRoutingCoordinator(ledger, mode="constrained")
    workspace = QualificationWorkspace(
        root / "qualification-workspaces",
        containment_available=containment_available,
    )
    full_script = {
        target_id: FakeAttemptSpec() for target_id in (*target_ids, *disabled_target_ids)
    }
    full_script.update(script or {})
    provider = DeterministicFakeProvider(full_script)
    executor = QualificationExecutor(
        coordinator,
        base_config=AgentConfig(),
        workspace=workspace,
        provider=provider,
        validator=validator,  # type: ignore[arg-type]
    )
    return FakeExecutorHarness(
        state=state,
        ledger=ledger,
        coordinator=coordinator,
        workspace=workspace,
        provider=provider,
        executor=executor,
        workspace_root=root / "qualification-workspaces",
    )


def create_case_task(harness: FakeExecutorHarness, task_id: str) -> TaskNodeRecord:
    """Create one routing-visible task for a qualification case."""

    return harness.state.create_task_node(
        task_id=task_id,
        run_id=RUN_ID,
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=("repo.search", "repo.map"),
        risk="low",
        acceptance_criteria=(),
    )


def make_lease(
    task: TaskNodeRecord,
    *,
    target_id: str = "target_b",
    containment: str = "isolated_worktree",
    lease_id: str | None = None,
    attempt_id: str | None = None,
    attempt_number: int = 1,
    task_contract_digest: str | None = None,
    tree_digest: str | None = None,
    reservation_micros: int = 1_000_000,
) -> AttemptLease:
    """Build an exact attempt lease bound to the compiled task contract."""

    contract = compile_task_contract(task)
    return AttemptLease(
        lease_id=lease_id or f"lease-{task.task_id}-{target_id}",
        run_id=task.run_id,
        case_id=CASE_ID,
        attempt_id=attempt_id or f"attempt-{task.task_id}-{target_id}",
        attempt_number=attempt_number,
        target_id=target_id,
        task=task,
        task_contract_digest=task_contract_digest or contract.digest,
        project_digest="a" * 64,
        tree_digest=tree_digest or "b" * 64,
        target_digest="c" * 64,
        price_digest="d" * 64,
        policy_digest="e" * 64,
        config_digest="f" * 64,
        reservation=MoneyMicros(reservation_micros),
        containment=containment,  # type: ignore[arg-type]
    )
