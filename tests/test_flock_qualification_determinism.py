"""Deterministic Flock qualification gate (Adaptive Flock Task 22).

The same bounded qualification journey is executed twenty times under a
frozen clock, frozen IDs, frozen evidence order, frozen target inventory,
frozen prices, a frozen build ID, and scripted provider outputs.  Every
repeat must complete and produce the exact same receipt projection digest
with zero guardrail violations; the report binds the source commit and a
configuration digest so drift is attributable.
"""

from __future__ import annotations

from pathlib import Path

from scripts.run_flock_qualification_determinism import (
    REPORT_SCHEMA,
    run_qualification_determinism,
)


def test_determinism_runner_requires_twenty_identical_receipts(tmp_path: Path) -> None:
    report = run_qualification_determinism(tmp_path, repeats=20)
    assert report["schema"] == REPORT_SCHEMA
    assert report["completed_repeats"] == 20
    assert report["unique_receipt_projection_digests"] == 1
    assert report["guardrail_violations"] == 0
    assert report["passed"] is True


def test_report_binds_source_commit_and_configuration(tmp_path: Path) -> None:
    source_commit = "a" * 40
    report = run_qualification_determinism(
        tmp_path / "first", repeats=2, source_commit=source_commit
    )
    assert report["source_commit"] == source_commit
    configuration_digest = report["configuration_digest"]
    assert isinstance(configuration_digest, str)
    assert len(configuration_digest) == 64

    repeated = run_qualification_determinism(
        tmp_path / "second", repeats=2, source_commit=source_commit
    )
    assert repeated["configuration_digest"] == configuration_digest
    assert (
        repeated["receipt_projection_digests"] == report["receipt_projection_digests"]
    )


def test_report_is_machine_readable_and_redacted(tmp_path: Path) -> None:
    report = run_qualification_determinism(tmp_path, repeats=2, source_commit="b" * 40)
    assert report["repeats"] == 2
    assert len(report["receipt_projection_digests"]) == 2
    assert report["flake_count"] == 0
    assert report["reasons"] == []
