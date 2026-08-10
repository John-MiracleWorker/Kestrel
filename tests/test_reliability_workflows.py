from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def _run_rehearsal_order_gate(
    *,
    rehearsal_updated_at: str,
    rehearsal_overrides: dict[str, object] | None = None,
) -> subprocess.CompletedProcess[str]:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    gate = workflow.index("Require successful exact-SHA release rehearsal before build")
    script_start = workflow.index("          from datetime import datetime", gate)
    script_end = workflow.index("\n          PY", script_start)
    script = textwrap.dedent(workflow[script_start:script_end])
    commit = "a" * 40
    environment = os.environ.copy()
    rehearsal = {
        "head_sha": commit,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "updated_at": rehearsal_updated_at,
    }
    rehearsal.update(rehearsal_overrides or {})
    environment.update(
        {
            "RELEASE_COMMIT_SHA": commit,
            "RELEASE_RUN_JSON": json.dumps({"created_at": "2026-07-28T12:00:00Z"}),
            "REHEARSAL_RUNS_JSON": json.dumps({"workflow_runs": [rehearsal]}),
        }
    )
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def _determinism_v3_receipt(
    *,
    backend: str = "memory",
    golden_report_sha256: str | None = None,
) -> dict[str, object]:
    commit = "a" * 40
    reference_projection: dict[str, object] = {}
    outcome_signature = hashlib.sha256(
        json.dumps(
            reference_projection,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode()
    ).hexdigest()
    return {
        "schema": "kestrel.determinism_eval_report.v3",
        "subject": {"source_commit": commit},
        "configuration": {
            "backend": backend,
            "provider": "mock",
            "model": "mock",
            "seed": 1729,
            "required_repeats": 20,
            "comparison": "functional_outcomes_excluding_wall_clock_latency",
            "case_timeout_seconds": 60.0,
            "iteration_timeout_seconds": 1500.0,
            "max_case_latency_ms": 45000.0,
        },
        "environment": {
            "os": "Linux",
            "architecture": "X64",
            "python_version": "3.11.13",
        },
        "summary": {
            "completed_repeats": 20,
            "required_repeats": 20,
            "all_runs_passed": True,
            "unique_outcome_signatures": 1,
            "determinism_streak": 20,
            "observed_flake_count": 0,
            "observed_flake_rate": 0.0,
        },
        "differing_cases": [],
        "reference_projection": reference_projection,
        "runs": [
            {
                "repeat": repeat,
                "passed": True,
                "outcome_signature": outcome_signature,
                "golden_report_sha256": (
                    golden_report_sha256
                    if golden_report_sha256 is not None
                    else _golden_report_sha256(_golden_reports()[repeat - 1])
                ),
                "failed_cases": [],
            }
            for repeat in range(1, 21)
        ],
        "passed": True,
    }


def _golden_reports() -> list[dict[str, object]]:
    return [
        {
            "schema": "kestrel.golden_eval_report.v2",
            "results": [
                {
                    "name": "deterministic-case",
                    "passed": True,
                    "latency_ms": float(repeat),
                }
            ],
        }
        for repeat in range(1, 21)
    ]


def _golden_report_sha256(report: dict[str, object]) -> str:
    canonical = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _run_determinism_receipt_gate(
    tmp_path: Path,
    receipt: dict[str, object],
    *,
    golden_reports: list[dict[str, object]] | None = None,
    omitted_repeats: frozenset[int] = frozenset(),
    extra_reports: dict[int, dict[str, object]] | None = None,
) -> subprocess.CompletedProcess[str]:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    gate = workflow.index("Require successful exact-SHA determinism receipt before build")
    receipt_validation = workflow.index("DETERMINISM_REPORT=", gate)
    script_start = workflow.index("          import hashlib", receipt_validation)
    script_end = workflow.index("\n          PY", script_start)
    script = textwrap.dedent(workflow[script_start:script_end])
    gate_root = tmp_path / f"gate-{len(tuple(tmp_path.iterdir()))}"
    artifact_root = gate_root / "artifact"
    run_root = artifact_root / "kestrel-determinism-runs"
    artifact_root.mkdir(parents=True)
    report_path = artifact_root / "kestrel-determinism-report.json"
    report_path.write_text(json.dumps(receipt), encoding="utf-8")
    reports = golden_reports if golden_reports is not None else _golden_reports()
    for repeat, golden_report in enumerate(reports, start=1):
        if repeat in omitted_repeats:
            continue
        repeat_root = run_root / f"repeat-{repeat:02d}"
        repeat_root.mkdir(parents=True)
        (repeat_root / "golden-report.json").write_text(
            json.dumps(golden_report),
            encoding="utf-8",
        )
    for repeat, golden_report in (extra_reports or {}).items():
        repeat_root = run_root / f"repeat-{repeat:02d}"
        repeat_root.mkdir(parents=True, exist_ok=True)
        (repeat_root / "golden-report.json").write_text(
            json.dumps(golden_report),
            encoding="utf-8",
        )
    environment = os.environ.copy()
    environment.update(
        {
            "RELEASE_COMMIT_SHA": "a" * 40,
            "DETERMINISM_RUN_ID": "4242",
            "DETERMINISM_ARTIFACT_DIR": str(artifact_root),
            "DETERMINISM_REPORT": str(report_path),
        }
    )
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )


def test_determinism_lane_runs_twenty_seeded_repeats_and_always_uploads_report() -> None:
    workflow = (ROOT / ".github" / "workflows" / "determinism.yml").read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in workflow
    assert "scripts/run_determinism_evals.py" in workflow
    assert "--repeats 20" in workflow
    assert "--seed 1729" in workflow
    assert '--source-commit "${GITHUB_SHA}"' in workflow
    assert "--case-timeout-seconds 60" in workflow
    assert "--iteration-timeout-seconds 1500" in workflow
    assert 'PYTHONHASHSEED: "1729"' in workflow
    assert "if: always()" in workflow
    assert "kestrel-determinism-${{ matrix.backend }}-${{ github.sha }}" in workflow
    assert "packages: write" not in workflow
    assert "id-token: write" not in workflow


def test_golden_determinism_matrix_runs_twenty_memory_and_memvid_repeats() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]
    golden = jobs["everyday-golden-determinism"]

    assert golden["strategy"] == {
        "fail-fast": False,
        "matrix": {"backend": ["memory", "memvid"]},
    }
    install = next(
        step["run"]
        for step in golden["steps"]
        if step.get("name") == "Install deterministic evaluation dependencies"
    )
    assert ".[dev,memvid]" in install
    invocation = next(
        step["run"]
        for step in golden["steps"]
        if step.get("name") == "Run twenty identical everyday golden evaluations"
    )
    assert "--backend ${{ matrix.backend }}" in invocation
    assert "--repeats 20" in invocation
    assert "--seed 1729" in invocation
    assert '--source-commit "${GITHUB_SHA}"' in invocation
    upload = next(
        step
        for step in golden["steps"]
        if step.get("name") == "Upload the machine-readable flake report"
    )
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == (
        "kestrel-determinism-${{ matrix.backend }}-${{ github.sha }}"
    )
    assert "iteration-receipt.json" in upload["with"]["path"]
    assert "golden-report.json" in upload["with"]["path"]

    flock = jobs["flock-qualification-determinism"]
    assert "strategy" not in flock
    flock_runs = "\n".join(
        str(step.get("run", "")) for step in flock["steps"]
    )
    assert "run_flock_qualification_determinism.py" in flock_runs
    assert "run_determinism_evals.py" not in flock_runs


def test_release_rehearsal_lane_is_repeatable_and_has_no_publication_authority() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-rehearsal.yml").read_text(
        encoding="utf-8"
    )

    assert "push:\n    branches: [main]" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "scripts/run_release_rehearsal.py" in workflow
    assert "git ls-remote --tags" not in workflow
    assert "production tag already exists" not in workflow
    assert (
        "kestrel-rehearsal-${GITHUB_REPOSITORY_ID}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"
        in workflow
    )
    assert "if: always()" in workflow
    for forbidden in (
        "packages: write",
        "contents: write",
        "id-token: write",
        "secrets.",
        "gh release",
        "docker push",
        "pypa/gh-action-pypi-publish",
    ):
        assert forbidden not in workflow


def test_testing_guide_determinism_command_binds_backend_and_source_subject() -> None:
    guide = (ROOT / "docs" / "TESTING.md").read_text(encoding="utf-8")
    command_start = guide.index('DETERMINISM_PARENT="$(mktemp -d)"')
    command_end = guide.index("```", command_start)
    command = guide[command_start:command_end]

    assert "--backend memory" in command
    assert 'WORKTREE_STATUS="$(git status --porcelain=v1 --untracked-files=normal)"' in command
    assert 'if test -n "$WORKTREE_STATUS"; then' in command
    assert "exit 1" in command
    assert 'SOURCE_COMMIT="$(git rev-parse --verify HEAD)"' in command
    assert '--source-commit "$SOURCE_COMMIT"' in command


def test_production_release_requires_exact_sha_reliability_receipts_before_build() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    rehearsal_gate = workflow.index("Require successful exact-SHA release rehearsal before build")
    determinism_gate = workflow.index(
        "Require successful exact-SHA determinism receipt before build"
    )
    build = workflow.index("Build Python release artifacts")
    assert rehearsal_gate < build
    assert determinism_gate < build
    assert 'actions/workflows/release-rehearsal.yml/runs"' in workflow
    assert 'actions/workflows/determinism.yml/runs"' in workflow
    assert '-f head_sha="$RELEASE_COMMIT_SHA"' in workflow
    assert "-f branch=main" in workflow
    assert "-f event=push" in workflow
    assert 'run.get("head_sha") == expected_sha' in workflow
    assert 'run.get("conclusion") == "success"' in workflow
    assert 'actions/runs/${GITHUB_RUN_ID}' in workflow
    assert 'release_run.get("created_at")' in workflow
    assert 'run.get("updated_at")' in workflow
    assert '"release rehearsal push run on main that completed before the "' in workflow
    assert '"release workflow was created"' in workflow
    assert 'gh run download "$DETERMINISM_RUN_ID"' in workflow
    assert "kestrel-determinism-memory-${RELEASE_COMMIT_SHA}" in workflow
    assert 'report.get("schema") != "kestrel.determinism_eval_report.v3"' in workflow
    assert 'subject.get("source_commit") != expected_sha' in workflow
    assert 'configuration.get("backend") != "memory"' in workflow
    assert 'configuration.get("max_case_latency_ms") != 45000.0' in workflow
    assert "if summary != derived_summary" in workflow


def test_release_gate_accepts_only_exact_sha_memory_v3_lane_receipt(
    tmp_path: Path,
) -> None:
    accepted = _run_determinism_receipt_gate(tmp_path, _determinism_v3_receipt())
    substituted = _run_determinism_receipt_gate(
        tmp_path,
        _determinism_v3_receipt(backend="memvid"),
    )
    malformed_digest = _run_determinism_receipt_gate(
        tmp_path,
        _determinism_v3_receipt(golden_report_sha256="not-a-digest"),
    )

    assert accepted.returncode == 0, accepted.stderr
    assert substituted.returncode != 0
    assert "memory backend" in substituted.stderr
    assert malformed_digest.returncode != 0
    assert "golden report digest" in malformed_digest.stderr


def test_release_gate_accepts_distinct_golden_digests_for_one_functional_signature(
    tmp_path: Path,
) -> None:
    receipt = _determinism_v3_receipt()
    runs = receipt["runs"]
    assert isinstance(runs, list)
    digests = {
        run["golden_report_sha256"]
        for run in runs
        if isinstance(run, dict)
    }

    completed = _run_determinism_receipt_gate(tmp_path, receipt)

    assert len(digests) == 20
    assert completed.returncode == 0, completed.stderr


def test_release_gate_recomputes_each_uploaded_golden_report_digest(
    tmp_path: Path,
) -> None:
    receipt = _determinism_v3_receipt()
    substituted_reports = _golden_reports()
    substituted_reports[-1] = {
        **substituted_reports[-1],
        "substituted": True,
    }

    completed = _run_determinism_receipt_gate(
        tmp_path,
        receipt,
        golden_reports=substituted_reports,
    )

    assert completed.returncode != 0
    assert "golden report digest does not match repeat 20" in completed.stderr


def test_release_gate_rejects_missing_or_extra_golden_reports(tmp_path: Path) -> None:
    receipt = _determinism_v3_receipt()
    missing = _run_determinism_receipt_gate(
        tmp_path,
        receipt,
        omitted_repeats=frozenset({20}),
    )
    extra = _run_determinism_receipt_gate(
        tmp_path,
        receipt,
        extra_reports={21: {"extra": True}},
    )

    assert missing.returncode != 0
    assert "golden report artifact count" in missing.stderr
    assert extra.returncode != 0
    assert "golden report artifact count" in extra.stderr


def test_release_gate_derives_one_distinct_outcome_signature(tmp_path: Path) -> None:
    receipt = _determinism_v3_receipt()
    runs = receipt["runs"]
    assert isinstance(runs, list)
    assert isinstance(runs[-1], dict)
    runs[-1]["outcome_signature"] = "d" * 64

    completed = _run_determinism_receipt_gate(tmp_path, receipt)

    assert completed.returncode != 0
    assert "multiple outcome signatures" in completed.stderr


def test_release_gate_derives_reference_projection_signature(tmp_path: Path) -> None:
    receipt = _determinism_v3_receipt()
    receipt["reference_projection"] = {"substituted": True}

    completed = _run_determinism_receipt_gate(tmp_path, receipt)

    assert completed.returncode != 0
    assert "reference projection digest" in completed.stderr


def test_release_gate_rejects_forged_derived_summary(tmp_path: Path) -> None:
    receipt = _determinism_v3_receipt()
    summary = receipt["summary"]
    assert isinstance(summary, dict)
    summary["determinism_streak"] = 19

    completed = _run_determinism_receipt_gate(tmp_path, receipt)

    assert completed.returncode != 0
    assert "derived summary" in completed.stderr


def test_release_order_gate_accepts_only_a_rehearsal_completed_before_tag_workflow() -> None:
    before = _run_rehearsal_order_gate(rehearsal_updated_at="2026-07-28T11:59:59Z")
    after = _run_rehearsal_order_gate(rehearsal_updated_at="2026-07-28T12:00:01Z")

    assert before.returncode == 0, before.stderr
    assert after.returncode != 0
    assert "completed before the release workflow was created" in after.stderr


def test_release_order_gate_rejects_nonqualifying_rehearsal_metadata() -> None:
    invalid_fields = (
        ("head_sha", "b" * 40),
        ("head_branch", "feature"),
        ("event", "workflow_dispatch"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("updated_at", "2026-07-28T12:00:00Z"),
    )

    for field, value in invalid_fields:
        completed = _run_rehearsal_order_gate(
            rehearsal_updated_at="2026-07-28T11:59:59Z",
            rehearsal_overrides={field: value},
        )

        assert completed.returncode != 0, field
