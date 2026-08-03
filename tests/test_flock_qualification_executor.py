"""Isolated Flock qualification executor tests (Adaptive Flock plan, Task 8).

The executor runs one leased qualification attempt through the normal
eligibility and direct-target routing path — never a parallel approximation —
stages candidate code only inside the containment mode allowed by the corpus
item, invokes the provider under task capability ceilings, validates with
trusted validators (never the candidate model's self-report), and records
bounded evidence with receipt references. The deterministic fake provider
proves the harness by emitting exact attempts from fixture inputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _qualification_fakes import (
    FakeAttemptSpec,
    build_fake_harness,
    create_case_task,
    make_lease,
)

from nested_memvid_agent.routing.qualification_executor import (
    AttemptLease,
    ProviderAttempt,
    ProviderRequest,
    QualificationAttemptBlocked,
)
from nested_memvid_agent.routing.qualification_workspace import (
    CONTAINMENT_MODES,
    QualificationWorkspace,
)

# --- Plan Step 1: direct-target and containment tests -------------------------


def test_executor_forces_matrix_target_through_normal_eligibility(
    tmp_path: Path,
) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-direct")

    evidence = harness.executor.execute(make_lease(task, target_id="target_b"))

    assert evidence.actual_target_id == "target_b"
    assert evidence.route_decision.hard_filter_reasons == ()
    assert evidence.route_decision.selection_kind == "operator_override"
    # The exact pin survives the learned-routing layer: the recorded routing
    # decision names the pinned matrix target, not a learned substitute.
    record = harness.ledger.get_attempt_decision(
        run_id=task.run_id,
        task_id=task.task_id,
        subagent_id=None,
        attempt=1,
    )
    assert record is not None
    assert record.selected_target_id == "target_b"


def test_candidate_code_never_falls_back_to_host_when_containment_missing(
    tmp_path: Path,
) -> None:
    harness = build_fake_harness(tmp_path, containment_available=False)
    task = create_case_task(harness, "task-code")

    with pytest.raises(QualificationAttemptBlocked, match="containment_required"):
        harness.executor.execute(make_lease(task, containment="isolated_worktree"))
    assert harness.executor.provider.calls == []


# --- Eligibility is never bypassed ---------------------------------------------


def test_ineligible_direct_target_blocks_before_provider_contact(
    tmp_path: Path,
) -> None:
    harness = build_fake_harness(tmp_path, disabled_target_ids=("target_c",))
    task = create_case_task(harness, "task-ineligible")

    with pytest.raises(QualificationAttemptBlocked, match="route_target_ineligible"):
        harness.executor.execute(make_lease(task, target_id="target_c"))
    assert harness.executor.provider.calls == []
    # The blocked attempt left no routing decision lease behind.
    assert (
        harness.ledger.get_attempt_decision(
            run_id=task.run_id,
            task_id=task.task_id,
            subagent_id=None,
            attempt=1,
        )
        is None
    )


def test_unknown_direct_target_blocks_before_provider_contact(
    tmp_path: Path,
) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-unknown")

    with pytest.raises(QualificationAttemptBlocked, match="route_target_unknown"):
        harness.executor.execute(make_lease(task, target_id="target_zzz"))
    assert harness.executor.provider.calls == []


# --- Lease verification and idempotency ----------------------------------------


def test_lease_contract_digest_mismatch_blocks_before_any_work(
    tmp_path: Path,
) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-digest")

    with pytest.raises(QualificationAttemptBlocked, match="lease_contract_mismatch"):
        harness.executor.execute(make_lease(task, task_contract_digest="0" * 64))
    assert harness.executor.provider.calls == []
    assert not harness.workspace_root.exists()


def test_execute_is_idempotent_by_lease_key(tmp_path: Path) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-idem")
    lease = make_lease(task, target_id="target_a")

    first = harness.executor.execute(lease)
    second = harness.executor.execute(lease)

    assert second is first
    assert len(harness.executor.provider.calls) == 1
    outcomes = [decision for decision in harness.ledger.list_decisions(run_id=task.run_id)]
    assert len(outcomes) == 1


# --- Deterministic fake proves the harness --------------------------------------


def test_fake_provider_emits_exact_fixture_attempt(tmp_path: Path) -> None:
    script = {
        "target_b": FakeAttemptSpec(
            output="fixture output: mapped",
            input_tokens=2_048,
            output_tokens=512,
            latency_seconds=1.5,
        )
    }
    harness = build_fake_harness(tmp_path, script=script)
    task = create_case_task(harness, "task-fake")

    evidence = harness.executor.execute(make_lease(task, target_id="target_b"))

    attempt = evidence.provider_attempt
    assert attempt.output == "fixture output: mapped"
    assert attempt.input_tokens == 2_048
    assert attempt.output_tokens == 512
    assert attempt.latency_seconds == 1.5
    assert attempt.failure_category is None
    # Usage is attributed to the durable routing outcome exactly.
    outcome = harness.ledger.get_outcome(evidence.route_decision.decision_id)
    assert outcome is not None
    assert outcome.input_tokens == 2_048
    assert outcome.output_tokens == 512
    assert outcome.latency_seconds == 1.5


def test_provider_runs_under_task_capability_ceilings(tmp_path: Path) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-ceilings")
    lease = make_lease(task, target_id="target_a")

    harness.executor.execute(lease)

    assert len(harness.provider.calls) == 1
    request = harness.provider.calls[0]
    assert request.idempotency_key == lease.lease_id
    assert request.target_id == "target_a"
    assert request.max_input_tokens >= 1
    assert request.max_output_tokens >= 1
    assert request.workspace_ref


def test_route_decision_is_persisted_before_provider_execution(
    tmp_path: Path,
) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-order")
    lease = make_lease(task, target_id="target_a")
    observed: list[bool] = []

    real_execute = harness.provider.execute

    def spying_execute(request: ProviderRequest) -> ProviderAttempt:
        observed.append(
            harness.ledger.get_attempt_decision(
                run_id=task.run_id,
                task_id=task.task_id,
                subagent_id=None,
                attempt=1,
            )
            is not None
        )
        return real_execute(request)

    harness.provider.execute = spying_execute  # type: ignore[method-assign]
    harness.executor.execute(lease)

    assert observed == [True]


# --- Trusted validation, never candidate self-report ----------------------------


def _acceptance_validator(
    lease: AttemptLease, attempt: ProviderAttempt
) -> tuple[bool, tuple[str, ...]]:
    if attempt.failure_category is not None:
        return False, ("provider_failure",)
    if "acceptance:repository_mapped" in attempt.output:
        return True, ("accepted",)
    return False, ("acceptance_missing",)


def test_candidate_self_report_is_never_used_as_validation(tmp_path: Path) -> None:
    script = {
        "target_a": FakeAttemptSpec(
            output="I reviewed my own work and it PASSED all acceptance checks."
        )
    }
    harness = build_fake_harness(tmp_path, script=script, validator=_acceptance_validator)
    task = create_case_task(harness, "task-self-report")

    evidence = harness.executor.execute(make_lease(task, target_id="target_a"))

    assert evidence.validation_passed is False
    assert evidence.validation_codes == ("acceptance_missing",)
    outcome = harness.ledger.get_outcome(evidence.route_decision.decision_id)
    assert outcome is not None
    assert outcome.validation_passed is False


def test_trusted_validator_accepts_exact_acceptance_evidence(tmp_path: Path) -> None:
    script = {"target_a": FakeAttemptSpec(output="done\nacceptance:repository_mapped")}
    harness = build_fake_harness(tmp_path, script=script, validator=_acceptance_validator)
    task = create_case_task(harness, "task-acceptance")

    evidence = harness.executor.execute(make_lease(task, target_id="target_a"))

    assert evidence.validation_passed is True
    assert evidence.validation_codes == ("accepted",)
    assert evidence.failure_category is None


def test_typed_provider_failure_is_recorded_in_evidence(tmp_path: Path) -> None:
    script = {
        "target_a": FakeAttemptSpec(
            output="",
            input_tokens=0,
            output_tokens=0,
            latency_seconds=30.0,
            failure_category="provider_timeout",
        )
    }
    harness = build_fake_harness(tmp_path, script=script)
    task = create_case_task(harness, "task-failure")

    evidence = harness.executor.execute(make_lease(task, target_id="target_a"))

    assert evidence.failure_category == "provider_timeout"
    assert evidence.validation_passed is False
    outcome = harness.ledger.get_outcome(evidence.route_decision.decision_id)
    assert outcome is not None
    assert outcome.failure_category == "provider_timeout"


# --- Workspace containment and receipt-bound review -----------------------------


def test_isolated_worktree_is_left_for_receipt_bound_review(tmp_path: Path) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-worktree")
    lease = make_lease(task, target_id="target_a", containment="isolated_worktree")

    evidence = harness.executor.execute(lease)

    workspace_dir = harness.workspace_root / lease.lease_id
    # The attempt workspace is not deleted: receipt-bound cleanup/review owns it.
    assert workspace_dir.is_dir()
    assert evidence.containment == "isolated_worktree"
    assert any(ref.startswith("workspace:") for ref in evidence.evidence_refs)
    assert any(ref.startswith("routing_decision:") for ref in evidence.evidence_refs)


def test_read_only_containment_needs_no_worktree(tmp_path: Path) -> None:
    # A read-only corpus item may execute even when no isolated worktree can
    # be staged; candidate code still never touches the host tree.
    harness = build_fake_harness(tmp_path, containment_available=False)
    task = create_case_task(harness, "task-readonly")

    evidence = harness.executor.execute(
        make_lease(task, target_id="target_a", containment="read_only")
    )

    assert evidence.containment == "read_only"
    assert evidence.actual_target_id == "target_a"
    assert not harness.workspace_root.exists()


def test_workspace_rejects_unknown_containment_mode(tmp_path: Path) -> None:
    workspace = QualificationWorkspace(tmp_path / "ws")
    with pytest.raises(ValueError, match="containment"):
        workspace.stage(
            lease_id="lease-x",
            containment="host",  # type: ignore[arg-type]
            tree_digest="b" * 64,
        )


def test_containment_modes_are_exactly_the_plan_vocabulary() -> None:
    assert CONTAINMENT_MODES == (
        "read_only",
        "isolated_worktree",
        "qualified_containment",
    )


# --- Lease validation ------------------------------------------------------------


def test_lease_requires_exact_digests(tmp_path: Path) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-lease-validation")
    with pytest.raises(ValueError, match="tree_digest"):
        make_lease(task, tree_digest="not-a-digest")


def test_lease_run_must_match_task_run(tmp_path: Path) -> None:
    harness = build_fake_harness(tmp_path)
    task = create_case_task(harness, "task-run-binding")
    lease = make_lease(task)
    with pytest.raises(ValueError, match="run_id"):
        AttemptLease(
            **{**lease.__dict__, "run_id": "run-other"}  # type: ignore[arg-type]
        )
