from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_rehearsal_order_gate(*, rehearsal_updated_at: str) -> subprocess.CompletedProcess[str]:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    gate = workflow.index("Require successful exact-SHA release rehearsal before build")
    script_start = workflow.index("          from datetime import datetime", gate)
    script_end = workflow.index("\n          PY", script_start)
    script = textwrap.dedent(workflow[script_start:script_end])
    commit = "a" * 40
    environment = os.environ.copy()
    environment.update(
        {
            "RELEASE_COMMIT_SHA": commit,
            "RELEASE_RUN_JSON": json.dumps({"created_at": "2026-07-28T12:00:00Z"}),
            "REHEARSAL_RUNS_JSON": json.dumps(
                {
                    "workflow_runs": [
                        {
                            "head_sha": commit,
                            "head_branch": "main",
                            "event": "push",
                            "status": "completed",
                            "conclusion": "success",
                            "updated_at": rehearsal_updated_at,
                        }
                    ]
                }
            ),
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
    assert "kestrel-determinism-${{ github.sha }}" in workflow
    assert "packages: write" not in workflow
    assert "id-token: write" not in workflow


def test_release_rehearsal_lane_has_no_production_publication_authority() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-rehearsal.yml").read_text(
        encoding="utf-8"
    )

    assert "push:\n    branches: [main]" in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "scripts/run_release_rehearsal.py" in workflow
    assert "Require rehearsal before production tag creation" in workflow
    assert 'refs/tags/v${VERSION}' in workflow
    assert "production tag already exists; rehearsal must precede tag creation" in workflow
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
    assert "kestrel-determinism-${RELEASE_COMMIT_SHA}" in workflow
    assert 'report.get("schema") != "kestrel.determinism_eval_report.v2"' in workflow
    assert 'configuration.get("source_commit") != expected_sha' in workflow
    assert 'summary.get("completed_repeats") != 20' in workflow


def test_release_order_gate_accepts_only_a_rehearsal_completed_before_tag_workflow() -> None:
    before = _run_rehearsal_order_gate(rehearsal_updated_at="2026-07-28T11:59:59Z")
    after = _run_rehearsal_order_gate(rehearsal_updated_at="2026-07-28T12:00:01Z")

    assert before.returncode == 0, before.stderr
    assert after.returncode != 0
    assert "completed before the release workflow was created" in after.stderr
