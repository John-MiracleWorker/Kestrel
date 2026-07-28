from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.engineering.graph_amendments import GraphAmendmentService
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.state_store import AgentStateStore


def _state(tmp_path: Path) -> AgentStateStore:
    state = AgentStateStore(tmp_path / "state.sqlite3")
    state.create_run(
        run_id="run_1",
        message="Repair the parser",
        session_id="session_1",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    state.create_task_node(
        task_id="root",
        run_id="run_1",
        title="Repair parser",
        goal="Repair the parser without regressions.",
        profile="planner",
        status="running",
        approved=True,
        plan={"decomposition": "initial"},
        required_tools=(),
        risk="low",
        acceptance_criteria=("All parser tests pass.",),
    )
    state.create_task_node(
        task_id="repair",
        run_id="run_1",
        parent_id="root",
        title="Implement repair",
        goal="Implement the bounded parser repair.",
        profile="worker",
        status="failed",
        approved=True,
        dependencies=(),
        required_tools=("repo.search", "repair.apply_patch"),
        risk="medium",
        acceptance_criteria=("The parser regression test passes.",),
        attempt_count=1,
        failure_reason="Regression test still fails.",
    )
    return state


def test_cancel_amendment_is_applied_and_idempotent(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.update_task_node("repair", status="queued", failure_reason="")
    service = GraphAmendmentService(state)

    first = service.propose(
        amendment_id="amend_cancel",
        run_id="run_1",
        operation="cancel_task",
        payload={"task_id": "repair", "reason": "Superseded by owner."},
        actor="owner",
        evidence_refs=("run_step:4",),
    )
    replay = service.propose(
        amendment_id="amend_cancel",
        run_id="run_1",
        operation="cancel_task",
        payload={"task_id": "repair", "reason": "Superseded by owner."},
        actor="owner",
        evidence_refs=("run_step:4",),
    )

    assert first.status == "applied"
    assert replay == first
    assert first.requires_approval is False
    assert first.base_graph_digest != first.result_graph_digest
    assert state.get_task_node("repair").status == "cancelled"


def test_scope_expansion_requires_digest_bound_owner_approval(tmp_path: Path) -> None:
    state = _state(tmp_path)
    service = GraphAmendmentService(state)

    proposed = service.propose(
        amendment_id="amend_add",
        run_id="run_1",
        operation="add_task",
        payload={
            "task": {
                "task_id": "security_review",
                "parent_id": "root",
                "title": "Review security impact",
                "goal": "Review security impact of the parser repair.",
                "profile": "reviewer",
                "dependencies": ["repair"],
                "required_tools": ["repo.search"],
                "risk": "high",
                "acceptance_criteria": ["Security impact is evidenced."],
            },
            "estimated_budget_delta_usd": 0.25,
        },
        actor="planner",
        permitted_tools={"repo.search", "repair.apply_patch"},
    )

    assert proposed.status == "pending_approval"
    assert proposed.requires_approval is True
    assert "risk_expansion" in proposed.approval_reasons
    assert "cost_expansion" in proposed.approval_reasons
    with pytest.raises(ValueError, match="graph digest"):
        service.decide(
            "amend_add",
            approved=True,
            actor="owner",
            expected_base_graph_digest="0" * 64,
        )

    applied = service.decide(
        "amend_add",
        approved=True,
        actor="owner",
        expected_base_graph_digest=proposed.base_graph_digest,
    )

    assert applied.status == "applied"
    assert applied.approved_by == "owner"
    assert state.get_task_node("security_review").risk == "high"
    with pytest.raises(ValueError, match="already decided"):
        service.decide(
            "amend_add",
            approved=True,
            actor="owner",
            expected_base_graph_digest=proposed.base_graph_digest,
        )


def test_graph_amendment_cannot_grant_an_unpermitted_tool(tmp_path: Path) -> None:
    state = _state(tmp_path)
    service = GraphAmendmentService(state)

    with pytest.raises(PermissionError, match="not permitted"):
        service.propose(
            amendment_id="amend_tool",
            run_id="run_1",
            operation="add_task",
            payload={
                "task": {
                    "task_id": "publish",
                    "parent_id": "root",
                    "title": "Publish",
                    "goal": "Publish the candidate.",
                    "required_tools": ["github.create_pull_request"],
                    "risk": "high",
                    "acceptance_criteria": ["Pull request exists."],
                }
            },
            actor="planner",
            permitted_tools={"repo.search", "repair.apply_patch"},
        )


def test_replace_dependency_rejects_cycles_before_persistence(tmp_path: Path) -> None:
    state = _state(tmp_path)
    state.create_task_node(
        task_id="review",
        run_id="run_1",
        parent_id="root",
        title="Review",
        goal="Review repair.",
        profile="reviewer",
        approved=True,
        dependencies=("repair",),
        acceptance_criteria=("Review is complete.",),
    )
    service = GraphAmendmentService(state)

    with pytest.raises(ValueError, match="cycle"):
        service.propose(
            amendment_id="amend_cycle",
            run_id="run_1",
            operation="replace_dependency",
            payload={
                "task_id": "repair",
                "remove_dependency": None,
                "add_dependency": "review",
            },
            actor="planner",
        )

    assert service.list(run_id="run_1") == []
    assert state.get_task_node("repair").dependencies == ()


def test_bounded_recovery_adds_evidence_then_changed_strategy_retry(tmp_path: Path) -> None:
    state = _state(tmp_path)
    service = GraphAmendmentService(state)

    proposed = service.propose_recovery(
        run_id="run_1",
        failed_task_id="repair",
        error="Regression test still fails.",
        diagnosis={"category": "validation_failure"},
        actor="RecoveryNode",
        estimated_budget_delta_usd=0.10,
        preauthorized_budget_usd=0.0,
    )

    assert proposed.status == "pending_approval"
    assert proposed.operation == "request_evidence"
    applied = service.decide(
        proposed.amendment_id,
        approved=True,
        actor="owner",
        expected_base_graph_digest=proposed.base_graph_digest,
    )

    diagnostic_id = str(applied.result["diagnostic_task_id"])
    retry_id = str(applied.result["retry_task_id"])
    diagnostic = state.get_task_node(diagnostic_id)
    retry = state.get_task_node(retry_id)
    assert diagnostic.required_tools == ()
    assert diagnostic.dependencies == ()
    assert retry.dependencies == (diagnostic_id,)
    assert retry.required_tools == ("repo.search", "repair.apply_patch")
    assert retry.retry_strategy is not None
    assert retry.retry_strategy["requires_changed_strategy"] is True
    assert retry.retry_strategy["retry_allowed"] is True
    assert retry.plan is not None
    assert retry.plan["recovery_depth"] == 1

    with pytest.raises(ValueError, match="recovery depth"):
        service.propose_recovery(
            run_id="run_1",
            failed_task_id=retry_id,
            error="Retry failed too.",
            diagnosis={"category": "validation_failure"},
            actor="RecoveryNode",
        )


def test_graph_amendment_rejects_sensitive_or_nonfinite_payloads(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    state.update_task_node("repair", status="queued", failure_reason="")
    service = GraphAmendmentService(state)
    secret = "graph-amendment-sensitive-value-41872"  # gitleaks:allow
    register_secret_value(secret)

    with pytest.raises(ValueError, match="sensitive material"):
        service.propose(
            amendment_id="amend_sensitive",
            run_id="run_1",
            operation="cancel_task",
            payload={"task_id": "repair", "reason": f"Contains {secret}"},
            actor="owner",
        )
    with pytest.raises(ValueError, match="finite JSON"):
        service.propose(
            amendment_id="amend_nonfinite",
            run_id="run_1",
            operation="cancel_task",
            payload={"task_id": "repair", "score": float("nan")},
            actor="owner",
        )

    assert service.list(run_id="run_1") == []
