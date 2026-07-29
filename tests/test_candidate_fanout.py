from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.engineering.candidates import (
    CandidateFanoutService,
    CandidateIsolation,
    VerifiedCandidateEvidence,
)
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.state_store import AgentStateStore


def _state(tmp_path: Path) -> AgentStateStore:
    state = AgentStateStore(tmp_path / "state.sqlite3")
    state.create_run(
        run_id="run_candidates",
        message="Repair parser",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    state.create_task_node(
        task_id="root",
        run_id="run_candidates",
        title="Repair",
        goal="Repair parser.",
        profile="planner",
        status="running",
        approved=True,
        plan={"decomposition": "initial"},
        acceptance_criteria=("Repair passes.",),
    )
    for candidate in ("candidate_a", "candidate_b", "candidate_c"):
        state.create_task_node(
            task_id=f"task_{candidate}",
            run_id="run_candidates",
            parent_id="root",
            title=candidate,
            goal="Try the same bounded repair contract.",
            profile="worker",
            status="approved",
            approved=True,
            plan={"candidate_id": candidate},
            required_tools=("repair.apply_patch", "repair.validate", "repair.review"),
            risk="medium",
            acceptance_criteria=("Parser regression test passes.",),
        )
    return state


def _workspace(tmp_path: Path, name: str) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path


def _verifier(
    workspace: Path,
    validation_id: str,
    reviews: tuple[tuple[str, str, str], ...],
) -> VerifiedCandidateEvidence:
    suffix = workspace.name[-1]
    return VerifiedCandidateEvidence(
        candidate_digest=(suffix * 64)[:64],
        validation_id=validation_id,
        validation_passed=True,
        validation_evidence_refs=(f"validation:{validation_id}",),
        review_artifact_refs=tuple(item[0] for item in reviews),
        reviewer_identities=tuple(item[1] for item in reviews),
        reviewer_evidence_refs=tuple(item[2] for item in reviews),
        changed_file_count={"a": 2, "b": 1, "c": 1}[suffix],
        changed_line_count={"a": 20, "b": 10, "c": 5}[suffix],
        risk_notes=(),
    )


def test_fanout_rejects_shared_mutable_workspace(tmp_path: Path) -> None:
    state = _state(tmp_path)
    service = CandidateFanoutService(state, evidence_verifier=_verifier)
    shared = _workspace(tmp_path, "worktree_a")
    contract_digest = service.task_contract_digest("task_candidate_a")
    plan = service.preview(
        fanout_id="fanout_shared",
        run_id="run_candidates",
        source_task_id="task_candidate_a",
        task_contract_digest=contract_digest,
        candidates=(
            CandidateIsolation(
                candidate_id="candidate_a",
                task_id="task_candidate_a",
                workspace=shared,
                branch="kestrel/candidate/a",
            ),
            CandidateIsolation(
                candidate_id="candidate_b",
                task_id="task_candidate_b",
                workspace=shared,
                branch="kestrel/candidate/b",
            ),
        ),
        estimated_budget_delta_usd=0.2,
    )

    with pytest.raises(ValueError, match="share mutable workspace"):
        service.create_fanout(
            fanout_id="fanout_shared",
            plan=plan,
            approved_plan_digest=plan["plan_digest"],
            actor="owner",
        )


def test_selection_requires_trusted_validation_and_reviewer_artifacts(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    service = CandidateFanoutService(state, evidence_verifier=_verifier)
    contract_digest = service.task_contract_digest("task_candidate_a")
    candidates = tuple(
        CandidateIsolation(
            candidate_id=f"candidate_{suffix}",
            task_id=f"task_candidate_{suffix}",
            workspace=_workspace(tmp_path, f"worktree_{suffix}"),
            branch=f"kestrel/candidate/{suffix}",
        )
        for suffix in ("a", "b", "c")
    )
    plan = service.preview(
        fanout_id="fanout_rank",
        run_id="run_candidates",
        source_task_id="task_candidate_a",
        task_contract_digest=contract_digest,
        candidates=candidates,
        estimated_budget_delta_usd=0.6,
    )
    fanout = service.create_fanout(
        fanout_id="fanout_rank",
        plan=plan,
        approved_plan_digest=plan["plan_digest"],
        actor="owner",
    )
    assert fanout.status == "running"
    assert len({item.workspace_identity for item in fanout.candidates}) == 3

    service.record_result(
        candidate_id="candidate_a",
        task_contract_digest=contract_digest,
        validation_id="validation_a",
        reviews=(("review_a", "reviewer_alpha", "route:alpha"),),
        actual_cost_usd=0.10,
        latency_seconds=4.0,
    )
    service.record_result(
        candidate_id="candidate_b",
        task_contract_digest=contract_digest,
        validation_id="validation_b",
        reviews=(
            ("review_b1", "reviewer_alpha", "route:alpha"),
            ("review_b2", "reviewer_beta", "route:beta"),
        ),
        actual_cost_usd=0.20,
        latency_seconds=5.0,
    )
    service.record_result(
        candidate_id="candidate_c",
        task_contract_digest=contract_digest,
        validation_id="validation_c",
        reviews=(),
        actual_cost_usd=0.01,
        latency_seconds=1.0,
    )

    selection = service.select(
        fanout_id="fanout_rank",
        actor="reviewer_gate",
    )

    assert selection.selected_candidate_id == "candidate_b"
    assert selection.ranking[0]["candidate_id"] == "candidate_b"
    assert selection.ranking[0]["reviewer_count"] == 2
    assert "candidate_c" in selection.ineligible_candidates
    assert service.get_fanout("fanout_rank").status == "selected"
    attempts = service.list_candidates(fanout_id="fanout_rank")
    assert {item.status for item in attempts} == {"selected", "rejected", "ineligible"}
    assert all(item.evidence_retained for item in attempts)


def test_fanout_plan_is_exactly_bound_and_contract_cannot_drift(tmp_path: Path) -> None:
    state = _state(tmp_path)
    service = CandidateFanoutService(state, evidence_verifier=_verifier)
    contract_digest = service.task_contract_digest("task_candidate_a")
    candidates = (
        CandidateIsolation(
            candidate_id="candidate_a",
            task_id="task_candidate_a",
            workspace=_workspace(tmp_path, "worktree_a"),
            branch="kestrel/candidate/a",
        ),
        CandidateIsolation(
            candidate_id="candidate_b",
            task_id="task_candidate_b",
            workspace=_workspace(tmp_path, "worktree_b"),
            branch="kestrel/candidate/b",
        ),
    )
    plan = service.preview(
        fanout_id="fanout_contract",
        run_id="run_candidates",
        source_task_id="task_candidate_a",
        task_contract_digest=contract_digest,
        candidates=candidates,
        estimated_budget_delta_usd=0.2,
    )
    stale_contract_plan = service.preview(
        fanout_id="fanout_stale_contract",
        run_id="run_candidates",
        source_task_id="task_candidate_a",
        task_contract_digest="3" * 64,
        candidates=candidates,
        estimated_budget_delta_usd=0.2,
    )
    with pytest.raises(ValueError, match="contract digest is stale"):
        service.create_fanout(
            fanout_id="fanout_stale_contract",
            plan=stale_contract_plan,
            approved_plan_digest=stale_contract_plan["plan_digest"],
            actor="owner",
        )
    with pytest.raises(ValueError, match="approved fanout plan digest"):
        service.create_fanout(
            fanout_id="fanout_contract",
            plan=plan,
            approved_plan_digest="0" * 64,
            actor="owner",
        )

    service.create_fanout(
        fanout_id="fanout_contract",
        plan=plan,
        approved_plan_digest=plan["plan_digest"],
        actor="owner",
    )
    with pytest.raises(ValueError, match="contract digest"):
        service.record_result(
            candidate_id="candidate_a",
            task_contract_digest="4" * 64,
            validation_id="validation_a",
            reviews=(("review_a", "reviewer", "route:reviewer"),),
            actual_cost_usd=0.1,
            latency_seconds=1.0,
        )


def test_candidate_result_rejects_sensitive_material_before_persistence(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    service = CandidateFanoutService(state, evidence_verifier=_verifier)
    contract_digest = service.task_contract_digest("task_candidate_a")
    candidates = (
        CandidateIsolation(
            candidate_id="candidate_a",
            task_id="task_candidate_a",
            workspace=_workspace(tmp_path, "worktree_a"),
            branch="kestrel/candidate/a",
        ),
        CandidateIsolation(
            candidate_id="candidate_b",
            task_id="task_candidate_b",
            workspace=_workspace(tmp_path, "worktree_b"),
            branch="kestrel/candidate/b",
        ),
    )
    plan = service.preview(
        fanout_id="fanout_sensitive",
        run_id="run_candidates",
        source_task_id="task_candidate_a",
        task_contract_digest=contract_digest,
        candidates=candidates,
        estimated_budget_delta_usd=0.2,
    )
    service.create_fanout(
        fanout_id="fanout_sensitive",
        plan=plan,
        approved_plan_digest=plan["plan_digest"],
        actor="owner",
    )
    secret = "candidate-result-sensitive-value-93147"  # gitleaks:allow
    register_secret_value(secret)

    with pytest.raises(ValueError, match="sensitive material"):
        service.record_result(
            candidate_id="candidate_a",
            task_contract_digest=contract_digest,
            validation_id="validation_a",
            reviews=(("review_a", "reviewer", "route:reviewer"),),
            actual_cost_usd=0.1,
            latency_seconds=1.0,
            result={"summary": f"provider echoed {secret}"},
        )

    current = next(
        item
        for item in service.list_candidates(fanout_id="fanout_sensitive")
        if item.candidate_id == "candidate_a"
    )
    assert current.status == "running"
    assert current.result == {}
