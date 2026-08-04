"""Live-provider Flock qualification evidence contract (Adaptive Flock Task 23).

The live qualification runner is explicit and non-default: it never runs
without an operator-supplied run ID, expected receipt ID/digest, installed
artifact digest, output path, and an explicit confirmation flag, and it never
auto-activates learned routing.  These tests pin the release evidence
verifier: mock-only or single-target evidence can never certify production
provider qualification, and every report is bound to the exact installed
artifact bytes.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from scripts.run_flock_live_qualification import (
    REPORT_SCHEMA,
    build_live_report,
    main,
    report_from_receipt,
    verify_live_report,
)


def _target(target_id: str, *, evidence_kind: str = "real_provider") -> dict[str, Any]:
    return {
        "target_id": target_id,
        "provider": f"provider-{target_id}",
        "model": f"model-{target_id}",
        "eligible": True,
        "evidence_kind": evidence_kind,
        "profile_subject_digest": "5" * 64,
        "model_subject_digest": "6" * 64,
        "target_digest": "7" * 64,
    }


def valid_live_report() -> dict[str, Any]:
    """A complete, passing live qualification evidence report."""

    return build_live_report(
        source_commit="a" * 40,
        installed_artifact_digest="b" * 64,
        platform="macOS-15.5",
        architecture="arm64",
        run_id="qual_live_001",
        receipt_id="receipt-qual_live_001-terminal",
        receipt_digest="c" * 64,
        status="completed",
        targets=[_target("target_a"), _target("target_b")],
        project_digest="d" * 64,
        tree_digest="e" * 64,
        grants=[
            {"grant_id": "grant-1", "scope_digest": "8" * 64},
        ],
        costs={
            "total_micros": 1_000_000,
            "unresolved_micros": 0,
            "cap_micros": 50_000_000,
        },
        replay={"repeats": 20, "unique_projection_digests": 1},
        guardrail_violations=0,
    )


def one_target_report() -> dict[str, Any]:
    report = valid_live_report()
    report["targets"] = report["targets"][:1]
    return report


def mock_only_report() -> dict[str, Any]:
    report = valid_live_report()
    report["targets"] = [
        _target("target_a", evidence_kind="deterministic_mock"),
        _target("target_b", evidence_kind="deterministic_mock"),
    ]
    return report


def test_live_report_rejects_mock_only_or_one_target_evidence() -> None:
    with pytest.raises(ValueError, match="two real eligible targets"):
        verify_live_report(one_target_report())
    with pytest.raises(ValueError, match="mock evidence cannot certify"):
        verify_live_report(mock_only_report())


def test_live_report_is_bound_to_installed_artifact() -> None:
    report = valid_live_report()
    report["installed_artifact_digest"] = "b" * 64
    with pytest.raises(ValueError, match="artifact digest"):
        verify_live_report(report, expected_artifact_digest="a" * 64)


def test_valid_live_report_passes_verification() -> None:
    report = valid_live_report()
    verify_live_report(
        report,
        expected_artifact_digest="b" * 64,
        expected_receipt_digest="c" * 64,
    )
    assert report["schema"] == REPORT_SCHEMA


def test_live_report_rejects_receipt_digest_mismatch() -> None:
    report = valid_live_report()
    with pytest.raises(ValueError, match="receipt digest"):
        verify_live_report(report, expected_receipt_digest="f" * 64)


def test_live_report_requires_twenty_of_twenty_replay() -> None:
    report = valid_live_report()
    report["replay"] = {"repeats": 20, "unique_projection_digests": 2}
    with pytest.raises(ValueError, match="replay"):
        verify_live_report(report)


def test_live_report_requires_zero_guardrail_violations() -> None:
    report = valid_live_report()
    report["guardrail_violations"] = 1
    with pytest.raises(ValueError, match="guardrail"):
        verify_live_report(report)


def test_live_report_requires_resolved_costs() -> None:
    report = valid_live_report()
    report["costs"]["unresolved_micros"] = 1
    with pytest.raises(ValueError, match="unresolved cost"):
        verify_live_report(report)


def test_live_report_requires_terminal_completed_status() -> None:
    report = valid_live_report()
    report["status"] = "failed"
    with pytest.raises(ValueError, match="completed"):
        verify_live_report(report)


def test_live_report_is_redacted_and_contains_no_raw_source() -> None:
    report = build_live_report(
        source_commit="a" * 40,
        installed_artifact_digest="b" * 64,
        platform="macOS-15.5",
        architecture="arm64",
        run_id="qual_live_001",
        receipt_id="receipt-qual_live_001-terminal",
        receipt_digest="c" * 64,
        status="completed",
        targets=[_target("target_a"), _target("target_b")],
        project_digest="d" * 64,
        tree_digest="e" * 64,
        grants=[{"grant_id": "grant-1", "scope_digest": "8" * 64}],
        costs={
            "total_micros": 1_000_000,
            "unresolved_micros": 0,
            "cap_micros": 50_000_000,
        },
        replay={"repeats": 20, "unique_projection_digests": 1},
        guardrail_violations=0,
        details={"api_key": "sk-live-secret-value"},
    )
    assert report["details"]["api_key"] != "sk-live-secret-value"


def test_runner_requires_explicit_confirmation_flag() -> None:
    with pytest.raises(SystemExit):
        main(
            [
                "--run-id",
                "qual_live_001",
                "--expected-receipt-id",
                "receipt-qual_live_001-terminal",
                "--expected-receipt-digest",
                "c" * 64,
                "--installed-artifact-digest",
                "b" * 64,
                "--output",
                "report.json",
            ]
        )


def _attempt_summary(
    attempt_id: str,
    target_id: str,
    evidence_kind: str | None = "real_provider",
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "attempt_id": attempt_id,
        "case_id": f"case_{attempt_id}",
        "target_id": target_id,
    }
    if evidence_kind is not None:
        summary["evidence_kind"] = evidence_kind
    return summary


def _receipt_payload(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "run": {"run_id": "qual_live_001"},
        "digests": {"scope": "9" * 64},
        "attempts": attempts,
        "payload_digest": "c" * 64,
        "status": "completed",
        "spend": {
            "actual_spend_micros": 1_000_000,
            "unresolved_reserve_micros": 0,
            "inflight_reserve_micros": 0,
        },
        "caps": {"max_spend_micros": 50_000_000},
        "replay": {"repeats": 20, "unique_projection_digests": 1},
        "guardrail_violations": 0,
        "terminal_reason": "matrix_exhausted",
    }


def _report_from_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    return report_from_receipt(
        _receipt_payload(attempts),
        source_commit="a" * 40,
        installed_artifact_digest="b" * 64,
        receipt_id="receipt-qual_live_001-terminal",
        platform="macOS-15.5",
        architecture="arm64",
        project_digest="d" * 64,
        tree_digest="e" * 64,
    )


def test_mock_executor_receipt_cannot_certify_live_qualification() -> None:
    attempts = [
        _attempt_summary("attempt_1", "target_a", "deterministic_mock"),
        _attempt_summary("attempt_2", "target_b", "deterministic_mock"),
    ]
    report = _report_from_attempts(attempts)
    # The receipt's per-attempt evidence kind is projected, never relabeled.
    assert {target["evidence_kind"] for target in report["targets"]} == {
        "deterministic_mock"
    }
    with pytest.raises(ValueError, match="mock evidence cannot certify"):
        verify_live_report(report)


def test_real_attempt_evidence_kind_propagates_into_report() -> None:
    attempts = [
        _attempt_summary("attempt_1", "target_a"),
        _attempt_summary("attempt_2", "target_b"),
    ]
    report = _report_from_attempts(attempts)
    assert {target["evidence_kind"] for target in report["targets"]} == {
        "real_provider"
    }
    verify_live_report(report)


def test_missing_attempt_evidence_kind_fails_closed() -> None:
    attempts = [
        _attempt_summary("attempt_1", "target_a", None),
        _attempt_summary("attempt_2", "target_b", None),
    ]
    report = _report_from_attempts(attempts)
    with pytest.raises(ValueError, match="two real eligible targets"):
        verify_live_report(report)


def test_report_is_deeply_independent_of_builder_inputs() -> None:
    targets = [_target("target_a"), _target("target_b")]
    grants = [{"grant_id": "grant-1", "scope_digest": "8" * 64}]
    report = build_live_report(
        source_commit="a" * 40,
        installed_artifact_digest="b" * 64,
        platform="macOS-15.5",
        architecture="arm64",
        run_id="qual_live_001",
        receipt_id="receipt-qual_live_001-terminal",
        receipt_digest="c" * 64,
        status="completed",
        targets=targets,
        project_digest="d" * 64,
        tree_digest="e" * 64,
        grants=grants,
        costs={
            "total_micros": 1_000_000,
            "unresolved_micros": 0,
            "cap_micros": 50_000_000,
        },
        replay={"repeats": 20, "unique_projection_digests": 1},
        guardrail_violations=0,
    )
    targets[0]["evidence_kind"] = "deterministic_mock"
    grants.clear()
    verify_live_report(copy.deepcopy(report))
    assert report["targets"][0]["evidence_kind"] == "real_provider"
    assert report["grants"]
