from __future__ import annotations

from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.projects import ProjectRecord
from nested_memvid_agent.routing.router import RoutingUnavailableError
from nested_memvid_agent.routing.run_manager import AdaptiveFlockRunManager
from nested_memvid_agent.state_store import RunRecord, TaskNodeRecord


class _Ledger:
    def __init__(self) -> None:
        self.decisions = [
            SimpleNamespace(
                decision_id="decision-prior",
                actionable=True,
                estimated_cost_usd=0.75,
            )
        ]

    def get_attempt_decision(self, **_kwargs: Any) -> None:
        return None

    def list_decisions(self, **_kwargs: Any) -> list[Any]:
        return list(self.decisions)


class _Coordinator:
    mode = "constrained"
    policy_id = "balanced"

    def __init__(self) -> None:
        self.ledger = _Ledger()
        self.calls: list[dict[str, Any]] = []

    def assign(self, _config: AgentConfig, _task: TaskNodeRecord, **kwargs: Any) -> Any:
        self.calls.append(dict(kwargs))
        return SimpleNamespace(record=SimpleNamespace(decision_id="decision-current"))


def test_project_policy_and_remaining_budget_reach_durable_assignment(
    tmp_path: Path,
) -> None:
    project = _project(
        tmp_path,
        provider_policy={
            "policy_id": "balanced",
            "preset": "local_only",
            "allowed_targets": ["local-strong"],
            "forbidden_targets": ["retired"],
            "allowed_profiles": ["local"],
            "forbidden_profiles": ["cloud"],
        },
        cost_budget=2.0,
    )
    coordinator = _Coordinator()
    manager = object.__new__(AdaptiveFlockRunManager)
    manager.routing_coordinator = coordinator  # type: ignore[assignment]
    manager._routing_assignment_lock = Lock()
    manager.state = SimpleNamespace(get_project=lambda _project_id: project)
    run = RunRecord(
        run_id="run-project-policy",
        status="running",
        message="Repair safely",
        session_id="session-project-policy",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
        project_id=project.project_id,
    )
    task = TaskNodeRecord(
        task_id="task-project-policy",
        run_id=run.run_id,
        title="Repair",
        goal="Repair safely",
        profile="worker",
        status="queued",
        required_tools=("repair.apply_patch",),
        risk="high",
    )

    manager._assign_with_project_policy(
        AgentConfig(provider="mock", model="mock"),
        task,
        run=run,
        subagent_id="subagent-project-policy",
        attempt=1,
    )

    call = coordinator.calls[0]
    assert call["local_required"] is True
    assert call["maximum_cost_usd"] == 1.25
    assert call["allowed_target_ids"] == ("local-strong",)
    assert call["forbidden_target_ids"] == ("retired",)
    assert call["allowed_provider_profiles"] == ("local",)
    assert call["forbidden_provider_profiles"] == ("cloud",)


def test_project_route_policy_mismatch_fails_closed(tmp_path: Path) -> None:
    project = _project(
        tmp_path,
        provider_policy={"policy_id": "frontier-review"},
        cost_budget=None,
    )
    manager = object.__new__(AdaptiveFlockRunManager)
    manager.routing_coordinator = _Coordinator()  # type: ignore[assignment]
    manager._routing_assignment_lock = Lock()
    manager.state = SimpleNamespace(get_project=lambda _project_id: project)
    run = RunRecord(
        run_id="run-policy-mismatch",
        status="running",
        message="Review",
        session_id="session-policy-mismatch",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
        project_id=project.project_id,
    )
    task = TaskNodeRecord(
        task_id="task-policy-mismatch",
        run_id=run.run_id,
        title="Review",
        goal="Review",
        profile="reviewer",
        status="queued",
    )

    with pytest.raises(RoutingUnavailableError, match="does not match"):
        manager._assign_with_project_policy(
            AgentConfig(provider="mock", model="mock"),
            task,
            run=run,
            subagent_id=None,
            attempt=1,
        )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("shadow", False),
        ("constrained", True),
        ("adaptive", True),
    ],
)
def test_only_actionable_adaptive_modes_replace_the_direct_provider_gate(
    mode: str,
    expected: bool,
) -> None:
    manager = object.__new__(AdaptiveFlockRunManager)
    manager.routing_coordinator = SimpleNamespace(mode=mode)

    assert manager._uses_actionable_project_routing() is expected


def _project(
    root: Path,
    *,
    provider_policy: dict[str, Any],
    cost_budget: float | None,
) -> ProjectRecord:
    return ProjectRecord(
        project_id="project_policy",
        display_name="Project policy",
        repository_path=str(root.resolve()),
        remote=None,
        default_branch="main",
        allowed_paths=(".",),
        provider_policy=provider_policy,
        cost_budget=cost_budget,
        privacy_class="local_required",
        test_recipes=(),
        build_recipes=(),
        capability_ceiling=(),
        baseline_index_digest=None,
        revision=1,
        archived_at=None,
        created_at="2026-07-27T00:00:00+00:00",
        updated_at="2026-07-27T00:00:00+00:00",
    )
