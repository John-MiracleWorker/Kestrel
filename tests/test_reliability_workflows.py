from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
    assert 'gh run download "$DETERMINISM_RUN_ID"' in workflow
    assert "kestrel-determinism-${RELEASE_COMMIT_SHA}" in workflow
    assert 'report.get("schema") != "kestrel.determinism_eval_report.v2"' in workflow
    assert 'configuration.get("source_commit") != expected_sha' in workflow
    assert 'summary.get("completed_repeats") != 20' in workflow
