from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from scripts.runtime_reliability_contract import (
    RUNTIME_RELIABILITY_ITERATION_TIMEOUT_SECONDS,
)

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


def _qualification_run(**overrides: object) -> dict[str, object]:
    run: dict[str, object] = {
        "id": 4242,
        "head_sha": "a" * 40,
        "head_branch": "main",
        "event": "push",
        "status": "completed",
        "conclusion": "success",
        "run_attempt": 1,
        "updated_at": "2026-07-28T11:59:59Z",
    }
    run.update(overrides)
    return run


def _run_qualification_selector(
    runs: list[dict[str, object]],
    *,
    release_created_at: str = "2026-07-28T12:00:00Z",
    total_count: int | None = None,
) -> subprocess.CompletedProcess[str]:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    gate = workflow.index(
        "Require successful exact-SHA runtime reliability qualification before build"
    )
    assignment = workflow.index("QUALIFICATION_RUN_ID=\"$(", gate)
    heredoc = workflow.index("python - <<'PY'", assignment)
    script_start = workflow.index("\n", heredoc) + 1
    script_end = workflow.index("\n          PY", script_start)
    script = textwrap.dedent(workflow[script_start:script_end])
    environment = os.environ.copy()
    environment.update(
        {
            "RELEASE_COMMIT_SHA": "a" * 40,
            "RELEASE_RUN_JSON": json.dumps({"created_at": release_created_at}),
            "QUALIFICATION_RUNS_JSON": json.dumps(
                {
                    "total_count": len(runs) if total_count is None else total_count,
                    "workflow_runs": runs,
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


def _qualification_job(**overrides: object) -> dict[str, object]:
    job: dict[str, object] = {
        "id": 5252,
        "name": "Exact-SHA five-cell runtime reliability qualification",
        "run_id": 4242,
        "run_attempt": 1,
        "head_sha": "a" * 40,
        "status": "completed",
        "conclusion": "success",
        "completed_at": "2026-07-28T11:59:59Z",
    }
    job.update(overrides)
    return job


def _run_qualification_job_validator(
    jobs: list[dict[str, object]],
    *,
    qualification_run_id: int = 4242,
    total_count: object | None = None,
    release_created_at: str = "2026-07-28T12:00:00Z",
) -> subprocess.CompletedProcess[str]:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assignment = workflow.index(
        'QUALIFICATION_JOBS_JSON="$qualification_jobs" python - <<\'PY\''
    )
    heredoc = workflow.index("python - <<'PY'", assignment)
    script_start = workflow.index("\n", heredoc) + 1
    script_end = workflow.index("\n          PY", script_start)
    script = textwrap.dedent(workflow[script_start:script_end])
    environment = os.environ.copy()
    environment.update(
        {
            "QUALIFICATION_RUN_ID": str(qualification_run_id),
            "RELEASE_COMMIT_SHA": "a" * 40,
            "RELEASE_RUN_JSON": json.dumps({"created_at": release_created_at}),
            "QUALIFICATION_JOBS_JSON": json.dumps(
                {
                    "total_count": len(jobs) if total_count is None else total_count,
                    "jobs": jobs,
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


def _qualification_artifact(**overrides: object) -> dict[str, object]:
    artifact: dict[str, object] = {
        "id": 6262,
        "name": "kestrel-runtime-reliability-qualification-" + "a" * 40,
        "expired": False,
        "workflow_run": {"id": 4242, "head_sha": "a" * 40},
    }
    artifact.update(overrides)
    return artifact


def _run_qualification_artifact_validator(
    artifacts: list[dict[str, object]],
    *,
    qualification_run_id: int = 4242,
    total_count: object | None = None,
) -> subprocess.CompletedProcess[str]:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    assignment = workflow.index(
        'QUALIFICATION_ARTIFACTS_JSON="$qualification_artifacts" python - <<\'PY\''
    )
    heredoc = workflow.index("python - <<'PY'", assignment)
    script_start = workflow.index("\n", heredoc) + 1
    script_end = workflow.index("\n          PY", script_start)
    script = textwrap.dedent(workflow[script_start:script_end])
    environment = os.environ.copy()
    environment.update(
        {
            "QUALIFICATION_RUN_ID": str(qualification_run_id),
            "RELEASE_COMMIT_SHA": "a" * 40,
            "QUALIFICATION_ARTIFACTS_JSON": json.dumps(
                {
                    "total_count": (
                        len(artifacts) if total_count is None else total_count
                    ),
                    "artifacts": artifacts,
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
    assert '--source-commit "${SOURCE_COMMIT}"' in workflow
    assert "--case-timeout-seconds 60" in workflow
    assert "--iteration-timeout-seconds 1500" in workflow
    assert 'PYTHONHASHSEED: "1729"' in workflow
    assert "if: always()" in workflow
    assert (
        "kestrel-determinism-${{ matrix.backend }}-${{ env.SOURCE_COMMIT }}" in workflow
    )
    assert "packages: write" not in workflow
    assert "id-token: write" not in workflow


def test_determinism_lane_binds_pr_evidence_to_the_exact_head_commit() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )

    assert workflow["env"]["SOURCE_COMMIT"] == (
        "${{ github.event.pull_request.head.sha || github.sha }}"
    )
    for job_name in (
        "everyday-golden-determinism",
        "runtime-reliability-qualification",
        "flock-qualification-determinism",
    ):
        checkout = next(
            step
            for step in workflow["jobs"][job_name]["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["ref"] == "${{ env.SOURCE_COMMIT }}"
        assert checkout["with"]["persist-credentials"] is False

    golden = workflow["jobs"]["everyday-golden-determinism"]
    golden_invocation = next(
        step["run"]
        for step in golden["steps"]
        if step.get("name") == "Run twenty identical everyday golden evaluations"
    )
    assert '--source-commit "${SOURCE_COMMIT}"' in golden_invocation
    golden_upload = next(
        step
        for step in golden["steps"]
        if step.get("name") == "Upload the machine-readable flake report"
    )
    assert golden_upload["with"]["name"] == (
        "kestrel-determinism-${{ matrix.backend }}-${{ env.SOURCE_COMMIT }}"
    )

    flock = workflow["jobs"]["flock-qualification-determinism"]
    flock_invocation = next(
        step["run"]
        for step in flock["steps"]
        if step.get("name") == "Run twenty identical flock qualification journeys"
    )
    assert '--source-commit "${SOURCE_COMMIT}"' in flock_invocation
    flock_upload = next(
        step
        for step in flock["steps"]
        if step.get("name") == "Upload the flock qualification determinism report"
    )
    assert flock_upload["with"]["name"] == (
        "kestrel-flock-qualification-determinism-${{ env.SOURCE_COMMIT }}"
    )


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
    assert '--source-commit "${SOURCE_COMMIT}"' in invocation
    upload = next(
        step
        for step in golden["steps"]
        if step.get("name") == "Upload the machine-readable flake report"
    )
    assert upload["if"] == "always()"
    assert upload["with"]["name"] == (
        "kestrel-determinism-${{ matrix.backend }}-${{ env.SOURCE_COMMIT }}"
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


def test_determinism_jobs_install_hash_locked_dependency_closures() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )
    expected_commands = {
        "everyday-golden-determinism": [
            "python -m pip install --require-hashes --only-binary=:all: "
            "-r config/python-build-bootstrap.txt",
            "uv export --frozen --no-dev --no-emit-local --extra dev --extra memvid "
            '--format requirements.txt --output-file "${RUNNER_TEMP}/requirements-determinism.txt"',
            "python -m pip install --require-hashes --only-binary=:all: "
            '-r "${RUNNER_TEMP}/requirements-determinism.txt"',
            "python -m pip install --no-build-isolation --no-deps -e '.[dev,memvid]'",
            "python -m pip check",
        ],
        "flock-qualification-determinism": [
            "python -m pip install --require-hashes --only-binary=:all: "
            "-r config/python-build-bootstrap.txt",
            "uv export --frozen --no-dev --no-emit-local --extra dev "
            '--format requirements.txt --output-file "${RUNNER_TEMP}/requirements-flock-determinism.txt"',
            "python -m pip install --require-hashes --only-binary=:all: "
            '-r "${RUNNER_TEMP}/requirements-flock-determinism.txt"',
            "python -m pip install --no-build-isolation --no-deps -e '.[dev]'",
            "python -m pip check",
        ],
    }

    for job_name, commands in expected_commands.items():
        steps = workflow["jobs"][job_name]["steps"]
        setup_uv = next(
            step for step in steps if step.get("name") == "Install pinned uv"
        )
        assert setup_uv == {
            "name": "Install pinned uv",
            "uses": "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b",
            "with": {"version": "0.11.16"},
        }
        install = next(
            step["run"]
            for step in steps
            if step.get("name") == "Install deterministic evaluation dependencies"
        )
        logical_commands: list[str] = []
        continued = ""
        for line in install.splitlines():
            stripped = line.strip()
            continued = f"{continued} {stripped}".strip()
            if continued.endswith("\\"):
                continued = continued[:-1].rstrip()
                continue
            logical_commands.append(continued)
            continued = ""

        assert not continued
        assert logical_commands == commands


def test_runtime_reliability_matrix_runs_twenty_fresh_process_repeats_on_all_hosts() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )
    runtime = workflow["jobs"]["runtime-reliability"]

    assert runtime["runs-on"] == "${{ matrix.os }}"
    assert runtime["timeout-minutes"] == 120
    assert runtime["defaults"] == {"run": {"shell": "bash"}}
    assert runtime["strategy"] == {
        "fail-fast": False,
        "matrix": {
            "os": ["ubuntu-latest", "macos-latest", "windows-latest"],
            "python-version": ["3.11"],
        },
    }
    checkout = next(
        step
        for step in runtime["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    assert checkout["uses"] == (
        "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5"
    )
    assert checkout["with"] == {
        "persist-credentials": False,
        "ref": "${{ env.SOURCE_COMMIT }}",
    }
    setup = next(
        step
        for step in runtime["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    )
    assert setup["uses"] == (
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
    )
    assert setup["with"]["python-version"] == "${{ matrix.python-version }}"
    setup_uv = next(
        step for step in runtime["steps"] if step.get("name") == "Install pinned uv"
    )
    assert setup_uv == {
        "name": "Install pinned uv",
        "uses": "astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b",
        "with": {"version": "0.11.16"},
    }
    install = next(
        step["run"]
        for step in runtime["steps"]
        if step.get("name") == "Install runtime reliability dependencies"
    )
    logical_commands: list[str] = []
    continued = ""
    for line in install.splitlines():
        stripped = line.strip()
        continued = f"{continued} {stripped}".strip()
        if continued.endswith("\\"):
            continued = continued[:-1].rstrip()
            continue
        logical_commands.append(continued)
        continued = ""

    assert not continued
    assert logical_commands == [
        "python -m pip install --require-hashes --only-binary=:all: "
        "-r config/python-build-bootstrap.txt",
        "uv export --frozen --no-dev --no-emit-local --extra dev "
        '--format requirements.txt --output-file "${RUNNER_TEMP}/requirements-runtime-reliability.txt"',
        "python -m pip install --require-hashes --only-binary=:all: "
        '-r "${RUNNER_TEMP}/requirements-runtime-reliability.txt"',
        "python -m pip install --no-build-isolation --no-deps -e '.[dev]'",
        "python -m pip check",
    ]
    invocation = next(
        step["run"]
        for step in runtime["steps"]
        if step.get("name") == "Run twenty fresh-process runtime reliability repetitions"
    )
    assert "scripts/run_runtime_reliability.py" in invocation
    assert "--repeats 20" in invocation
    assert '--source-commit "${SOURCE_COMMIT}"' in invocation
    assert '--run-root "${RUNNER_TEMP}/kestrel-runtime-reliability-runs"' in invocation
    assert '--output "${RUNNER_TEMP}/kestrel-runtime-reliability-report.json"' in invocation
    assert '--workspace "."' in invocation
    tokens = invocation.split()
    repeats = int(tokens[tokens.index("--repeats") + 1])
    iteration_timeout = int(tokens[tokens.index("--iteration-timeout-seconds") + 1])
    assert iteration_timeout == RUNTIME_RELIABILITY_ITERATION_TIMEOUT_SECONDS == 300.0
    assert repeats * iteration_timeout <= runtime["timeout-minutes"] * 60 - 600
    run_scripts = "\n".join(
        str(step["run"]) for step in runtime["steps"] if "run" in step
    )
    assert "${{ env.SOURCE_COMMIT }}" not in run_scripts
    assert "${{ runner.temp }}" not in run_scripts
    upload = next(
        step
        for step in runtime["steps"]
        if step.get("name") == "Upload runtime reliability receipts"
    )
    assert upload["if"] == "always()"
    assert upload["uses"] == (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert upload["with"]["name"] == (
        "kestrel-runtime-reliability-${{ runner.os }}-${{ env.SOURCE_COMMIT }}"
    )
    assert upload["with"]["path"].splitlines() == [
        "${{ runner.temp }}/kestrel-runtime-reliability-report.json",
        "${{ runner.temp }}/kestrel-runtime-reliability-runs/repeat-*/iteration-receipt.json",
    ]
    assert "pytest-results.xml" not in upload["with"]["path"]
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["retention-days"] == 14
    assert "github.sha" not in json.dumps(runtime, sort_keys=True)


def test_determinism_lane_builds_one_attempt_one_self_contained_five_cell_qualification() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "determinism.yml").read_text(
            encoding="utf-8"
        )
    )
    qualification = workflow["jobs"]["runtime-reliability-qualification"]

    assert qualification["needs"] == [
        "runtime-reliability",
        "everyday-golden-determinism",
    ]
    assert qualification["if"] == "github.run_attempt == 1"
    assert "flock" not in json.dumps(qualification, sort_keys=True).lower()

    pinned_download = (
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093"
    )
    downloads = [
        step
        for step in qualification["steps"]
        if step.get("uses") == pinned_download
    ]
    assert [step["with"] for step in downloads] == [
        {
            "name": "kestrel-runtime-reliability-Linux-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-runtime-reliability-Linux-${{ env.SOURCE_COMMIT }}"
            ),
        },
        {
            "name": "kestrel-runtime-reliability-macOS-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-runtime-reliability-macOS-${{ env.SOURCE_COMMIT }}"
            ),
        },
        {
            "name": "kestrel-runtime-reliability-Windows-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-runtime-reliability-Windows-${{ env.SOURCE_COMMIT }}"
            ),
        },
        {
            "name": "kestrel-determinism-memory-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-determinism-memory-${{ env.SOURCE_COMMIT }}"
            ),
        },
        {
            "name": "kestrel-determinism-memvid-${{ env.SOURCE_COMMIT }}",
            "path": (
                "${{ runner.temp }}/kestrel-runtime-reliability-qualification/"
                "kestrel-determinism-memvid-${{ env.SOURCE_COMMIT }}"
            ),
        },
    ]
    assert len(
        [
            step
            for step in qualification["steps"]
            if str(step.get("uses", "")).startswith("actions/download-artifact@")
        ]
    ) == 5

    build = next(
        step["run"]
        for step in qualification["steps"]
        if step.get("name") == "Build the five-cell runtime reliability qualification"
    )
    assert "scripts/aggregate_runtime_reliability_receipts.py build" in build
    assert '--source-commit "${SOURCE_COMMIT}"' in build
    assert '--workflow-run-id "${GITHUB_RUN_ID}"' in build
    assert '--workflow-run-attempt "${GITHUB_RUN_ATTEMPT}"' in build
    assert (
        '--artifact-root "${RUNNER_TEMP}/kestrel-runtime-reliability-qualification"'
        in build
    )

    upload = next(
        step
        for step in qualification["steps"]
        if step.get("name") == "Upload the five-cell runtime reliability qualification"
    )
    assert upload["uses"] == (
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02"
    )
    assert upload.get("if") != "always()"
    assert upload["with"] == {
        "name": (
            "kestrel-runtime-reliability-qualification-"
            "${{ env.SOURCE_COMMIT }}"
        ),
        "path": "${{ runner.temp }}/kestrel-runtime-reliability-qualification",
        "if-no-files-found": "error",
        "retention-days": 14,
    }


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
    workflow_path = ROOT / ".github" / "workflows" / "release.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)

    rehearsal_gate = workflow_text.index(
        "Require successful exact-SHA release rehearsal before build"
    )
    qualification_gate = workflow_text.index(
        "Require successful exact-SHA runtime reliability qualification before build"
    )
    build = workflow_text.index("Build Python release artifacts")
    assert rehearsal_gate < build
    assert qualification_gate < build
    assert workflow["permissions"] == {"actions": "read", "contents": "read"}

    gate_text = workflow_text[qualification_gate:build]
    assert 'actions/workflows/determinism.yml/runs"' in gate_text
    assert '-f head_sha="$RELEASE_COMMIT_SHA"' in gate_text
    assert "-f branch=main" in gate_text
    assert "-f event=push" in gate_text
    assert "-f status=completed" not in gate_text
    assert 'actions/runs/${GITHUB_RUN_ID}' in gate_text
    assert 'export RELEASE_RUN_JSON="$release_run"' in gate_text
    assert 'run.get("head_sha") == expected_sha' in gate_text
    assert 'run.get("head_branch") == "main"' in gate_text
    assert 'run.get("event") == "push"' in gate_text
    assert 'selected.get("status")' not in gate_text
    assert 'run.get("conclusion") == "success"' not in gate_text
    assert 'type(selected.get("run_attempt")) is not int' in gate_text
    assert 'selected["run_attempt"] != 1' in gate_text
    assert 'selected.get("updated_at")' not in gate_text
    assert 'actions/runs/${QUALIFICATION_RUN_ID}/jobs' in gate_text
    assert 'job.get("name")' in gate_text
    assert '"Exact-SHA five-cell runtime reliability qualification"' in gate_text
    assert 'job.get("conclusion") != "success"' in gate_text
    assert 'job.get("completed_at")' in gate_text
    assert 'gh run download "$QUALIFICATION_RUN_ID"' in gate_text
    assert (
        '"kestrel-runtime-reliability-qualification-${RELEASE_COMMIT_SHA}"'
        in gate_text
    )
    assert gate_text.count("gh run download") == 1
    assert "kestrel-determinism-memory-${RELEASE_COMMIT_SHA}" not in gate_text
    assert "kestrel-determinism-memvid-${RELEASE_COMMIT_SHA}" not in gate_text
    assert "kestrel-flock-qualification" not in gate_text
    assert "scripts/aggregate_runtime_reliability_receipts.py verify" in gate_text
    assert '--source-commit "$RELEASE_COMMIT_SHA"' in gate_text
    assert '--workflow-run-id "$QUALIFICATION_RUN_ID"' in gate_text
    assert "--workflow-run-attempt 1" in gate_text
    assert "kestrel-runtime-reliability-qualification.json" in gate_text


def test_release_qualification_selector_accepts_one_attempt_one_pre_release_run() -> None:
    completed = _run_qualification_selector([_qualification_run()])
    enclosing_workflow_failed = _run_qualification_selector(
        [_qualification_run(conclusion="failure")]
    )
    enclosing_workflow_still_running = _run_qualification_selector(
        [
            _qualification_run(
                status="in_progress",
                updated_at="not-qualification-authority",
            )
        ]
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "4242"
    assert enclosing_workflow_failed.returncode == 0, enclosing_workflow_failed.stderr
    assert enclosing_workflow_failed.stdout.strip() == "4242"
    assert enclosing_workflow_still_running.returncode == 0, (
        enclosing_workflow_still_running.stderr
    )
    assert enclosing_workflow_still_running.stdout.strip() == "4242"


def test_release_qualification_selector_rejects_rerun_and_ambiguous_evidence() -> None:
    rerun = _run_qualification_selector([_qualification_run(run_attempt=2)])
    ambiguous = _run_qualification_selector(
        [_qualification_run(), _qualification_run(id=4343)]
    )
    replacement = _run_qualification_selector(
        [
            _qualification_run(id=4141, conclusion="failure"),
            _qualification_run(id=4242),
        ]
    )

    assert rerun.returncode != 0
    assert "exactly one" in rerun.stderr
    assert ambiguous.returncode != 0
    assert "exactly one" in ambiguous.stderr
    assert replacement.returncode != 0
    assert "exactly one" in replacement.stderr


@pytest.mark.parametrize(
    "malformed_id",
    ["4343", True, 4242.0, None, 0, -1],
    ids=["string", "boolean", "float", "null", "zero", "negative"],
)
def test_release_qualification_selector_rejects_malformed_id_replacement(
    malformed_id: object,
) -> None:
    completed = _run_qualification_selector(
        [_qualification_run(), _qualification_run(id=malformed_id)]
    )

    assert completed.returncode != 0
    assert "replacement runs are rejected" in completed.stderr


def test_release_qualification_selector_rejects_paginated_ambiguity() -> None:
    completed = _run_qualification_selector(
        [_qualification_run()],
        total_count=101,
    )

    assert completed.returncode != 0
    assert "pagination" in completed.stderr


@pytest.mark.parametrize(
    "updated_at",
    ["2026-07-28T12:00:00Z", "2026-07-28T12:00:01Z", "malformed", None],
    ids=["equal", "after", "malformed", "null"],
)
def test_release_qualification_selector_does_not_use_run_updated_at_as_authority(
    updated_at: object,
) -> None:
    completed = _run_qualification_selector(
        [_qualification_run(updated_at=updated_at)]
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "4242"


def test_release_qualification_selector_rejects_wrong_run_metadata() -> None:
    invalid_fields = (
        ("id", "4242", "attempt-1"),
        ("head_sha", "b" * 40, "replacement runs are rejected"),
        ("head_branch", "feature", "replacement runs are rejected"),
        ("event", "workflow_dispatch", "replacement runs are rejected"),
        ("run_attempt", True, "attempt-1"),
    )

    for field, value, expected in invalid_fields:
        completed = _run_qualification_selector(
            [_qualification_run(**{field: value})]
        )

        assert completed.returncode != 0, field
        assert expected in completed.stderr, field


def test_release_qualification_job_validator_accepts_exact_metadata() -> None:
    completed = _run_qualification_job_validator([_qualification_job()])

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "completed_at",
    [
        "2026-07-28T12:00:00Z",
        "2026-07-28T12:00:01Z",
        "malformed",
        None,
        True,
    ],
    ids=["equal", "after", "malformed", "null", "boolean"],
)
def test_release_qualification_job_validator_rejects_non_pre_release_completion(
    completed_at: object,
) -> None:
    completed = _run_qualification_job_validator(
        [_qualification_job(completed_at=completed_at)]
    )

    assert completed.returncode != 0


@pytest.mark.parametrize(
    ("job", "qualification_run_id"),
    [
        (_qualification_job(run_attempt=True), 4242),
        (_qualification_job(run_attempt=1.0), 4242),
        (_qualification_job(run_id=True), 1),
        (_qualification_job(run_id=4242.0), 4242),
        (_qualification_job(head_sha="b" * 40), 4242),
        (_qualification_job(status="in_progress"), 4242),
        (_qualification_job(conclusion="failure"), 4242),
    ],
    ids=[
        "boolean-attempt",
        "float-attempt",
        "boolean-run-id",
        "float-run-id",
        "wrong-sha",
        "incomplete-status",
        "failed-conclusion",
    ],
)
def test_release_qualification_job_validator_rejects_mismatched_metadata(
    job: dict[str, object], qualification_run_id: int
) -> None:
    completed = _run_qualification_job_validator(
        [job], qualification_run_id=qualification_run_id
    )

    assert completed.returncode != 0
    assert "stale, failed, or mismatched" in completed.stderr


@pytest.mark.parametrize(
    "total_count",
    [True, 1.0, 2],
    ids=["boolean", "float", "mismatch"],
)
def test_release_qualification_job_validator_rejects_invalid_total_count(
    total_count: object,
) -> None:
    completed = _run_qualification_job_validator(
        [_qualification_job()], total_count=total_count
    )

    assert completed.returncode != 0
    assert "malformed or requires pagination" in completed.stderr


def test_release_qualification_job_validator_rejects_paginated_metadata() -> None:
    jobs = [_qualification_job()]
    jobs.extend({"name": f"unrelated-job-{index}"} for index in range(100))

    completed = _run_qualification_job_validator(jobs, total_count=len(jobs))

    assert completed.returncode != 0
    assert "malformed or requires pagination" in completed.stderr


def test_release_qualification_job_validator_requires_one_exact_name() -> None:
    missing = _run_qualification_job_validator(
        [_qualification_job(name="unrelated job")]
    )
    duplicate = _run_qualification_job_validator(
        [_qualification_job(), _qualification_job(id=5353)]
    )

    assert missing.returncode != 0
    assert "exactly one aggregate job" in missing.stderr
    assert duplicate.returncode != 0
    assert "exactly one aggregate job" in duplicate.stderr


def test_release_qualification_artifact_validator_accepts_exact_metadata() -> None:
    completed = _run_qualification_artifact_validator([_qualification_artifact()])

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "total_count",
    [True, 1.0, 2],
    ids=["boolean", "float", "mismatch"],
)
def test_release_qualification_artifact_validator_rejects_invalid_total_count(
    total_count: object,
) -> None:
    completed = _run_qualification_artifact_validator(
        [_qualification_artifact()], total_count=total_count
    )

    assert completed.returncode != 0
    assert "malformed or requires pagination" in completed.stderr


def test_release_qualification_artifact_validator_rejects_paginated_metadata() -> None:
    artifacts = [_qualification_artifact()]
    artifacts.extend(
        {"name": f"unrelated-artifact-{index}"} for index in range(100)
    )

    completed = _run_qualification_artifact_validator(
        artifacts, total_count=len(artifacts)
    )

    assert completed.returncode != 0
    assert "malformed or requires pagination" in completed.stderr


@pytest.mark.parametrize(
    ("workflow_run", "qualification_run_id"),
    [
        ({"id": True, "head_sha": "a" * 40}, 1),
        ({"id": 4242.0, "head_sha": "a" * 40}, 4242),
        ({"id": 4343, "head_sha": "a" * 40}, 4242),
        ({"id": 4242, "head_sha": "b" * 40}, 4242),
    ],
    ids=["boolean-run-id", "float-run-id", "wrong-run-id", "wrong-sha"],
)
def test_release_qualification_artifact_validator_rejects_mismatched_workflow_run(
    workflow_run: dict[str, object], qualification_run_id: int
) -> None:
    completed = _run_qualification_artifact_validator(
        [_qualification_artifact(workflow_run=workflow_run)],
        qualification_run_id=qualification_run_id,
    )

    assert completed.returncode != 0
    assert "stale or mismatched" in completed.stderr


def test_release_qualification_artifact_validator_rejects_expired_artifact() -> None:
    completed = _run_qualification_artifact_validator(
        [_qualification_artifact(expired=True)]
    )

    assert completed.returncode != 0
    assert "stale or mismatched" in completed.stderr


def test_release_qualification_artifact_validator_requires_one_exact_name() -> None:
    missing = _run_qualification_artifact_validator(
        [_qualification_artifact(name="unrelated-artifact")]
    )
    duplicate = _run_qualification_artifact_validator(
        [_qualification_artifact(), _qualification_artifact(id=6363)]
    )

    assert missing.returncode != 0
    assert "exactly one exact-name aggregate artifact" in missing.stderr
    assert duplicate.returncode != 0
    assert "exactly one exact-name aggregate artifact" in duplicate.stderr


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
