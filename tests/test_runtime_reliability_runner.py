from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.bounded_process import BoundedProcessResult
from scripts.run_runtime_reliability import (
    RUNTIME_RELIABILITY_TESTS,
    build_iteration_invoker,
    resolve_git_head,
    run_runtime_reliability,
)

SOURCE_COMMIT = "a" * 40


def _completed_process(*, returncode: int = 0, stdout: str = "4 passed\n") -> BoundedProcessResult:
    return BoundedProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        elapsed_seconds=1.25,
        timed_out=False,
        cleanup_attempted=False,
        cleanup_succeeded=True,
        termination_method=None,
        stdout_total_bytes=len(stdout.encode("utf-8")),
        stderr_total_bytes=0,
    )


def test_runtime_reliability_requires_twenty_fresh_passing_subprocess_receipts(
    tmp_path: Path,
) -> None:
    observed_roots: list[Path] = []
    identity_checks: list[Path] = []

    def invoke(repeat: int, repeat_root: Path) -> BoundedProcessResult:
        assert repeat == len(observed_roots) + 1
        assert repeat_root.is_dir()
        observed_roots.append(repeat_root)
        return _completed_process()

    def resolve_identity(workspace: Path) -> str:
        identity_checks.append(workspace)
        return SOURCE_COMMIT

    output = tmp_path / "report.json"
    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=output,
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        workspace=tmp_path,
        resolve_workspace_head=resolve_identity,
        runner_os="Windows",
        runner_arch="AMD64",
        python_version="3.11.9",
        iteration_timeout_seconds=900.0,
    )

    assert report["schema"] == "kestrel.runtime_reliability_report.v1"
    assert report["subject"] == {"source_commit": SOURCE_COMMIT}
    assert report["configuration"] == {
        "required_repeats": 20,
        "tests": list(RUNTIME_RELIABILITY_TESTS),
        "subprocess_isolation": "fresh_interpreter_and_basetemp_per_repeat",
        "iteration_timeout_seconds": 900.0,
    }
    assert report["environment"] == {
        "runner_os": "Windows",
        "runner_arch": "X64",
        "python_version": "3.11.9",
    }
    assert report["summary"] == {
        "passed": True,
        "completed_repeats": 20,
        "required_repeats": 20,
        "consecutive_passes": 20,
        "failure_count": 0,
        "first_failure": None,
    }
    assert len(report["runs"]) == 20
    assert len(set(observed_roots)) == 20
    assert all(root.parent == tmp_path / "runs" for root in observed_roots)
    assert identity_checks == [tmp_path] * 42
    expected_identity = {
        "expected_source_commit": SOURCE_COMMIT,
        "observed_head": SOURCE_COMMIT,
        "clean": True,
        "passed": True,
    }
    assert report["workspace_identity"] == {
        "preflight": expected_identity,
        "final": expected_identity,
    }
    for repeat, run in enumerate(report["runs"], start=1):
        assert run["schema"] == "kestrel.runtime_reliability_iteration.v1"
        assert run["subject"] == {"source_commit": SOURCE_COMMIT}
        assert run["repeat"] == repeat
        assert run["status"] == "completed"
        assert run["derived_passed"] is True
        assert run["workspace_identity"] == {
            "before": expected_identity,
            "after": expected_identity,
        }
        receipt = json.loads(
            (tmp_path / "runs" / f"repeat-{repeat:02d}" / "iteration-receipt.json").read_text(
                encoding="utf-8"
            )
        )
        assert receipt == run
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_runtime_reliability_stops_at_first_failure_with_redacted_diagnostics(
    tmp_path: Path,
) -> None:
    invoked: list[int] = []

    def invoke(repeat: int, _repeat_root: Path) -> BoundedProcessResult:
        invoked.append(repeat)
        if repeat == 1:
            return _completed_process()
        return _completed_process(
            returncode=1,
            stdout=(
                "FAILED tests/test_channels.py::test_server_exposes_channel_ingest_route\n"
                "OPENAI_API_KEY=sk-proj-runtime-reliability-secret123\n"
            ),
        )

    output = tmp_path / "report.json"
    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=output,
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        workspace=tmp_path,
        resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
        runner_os="Darwin",
        runner_arch="arm64",
        python_version="3.13.12",
        iteration_timeout_seconds=900.0,
    )

    assert invoked == [1, 2]
    assert report["summary"]["passed"] is False
    assert report["summary"]["completed_repeats"] == 2
    assert report["summary"]["consecutive_passes"] == 1
    assert report["summary"]["failure_count"] == 1
    first_failure = report["summary"]["first_failure"]
    assert first_failure["repeat"] == 2
    assert first_failure["status"] == "runner_nonzero"
    assert first_failure["runner_exit_code"] == 1
    assert "test_server_exposes_channel_ingest_route" in first_failure["stdout"]
    serialized = json.dumps(report)
    assert "runtime-reliability-secret123" not in serialized
    assert "<redacted>" in serialized
    assert not (tmp_path / "runs" / "repeat-03").exists()
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_runtime_reliability_rejects_subject_mismatch_before_creating_receipts(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="workspace HEAD does not match source commit"):
        run_runtime_reliability(
            repeats=20,
            run_root=tmp_path / "runs",
            output=tmp_path / "report.json",
            invoke=lambda _repeat, _repeat_root: _completed_process(),
            source_commit=SOURCE_COMMIT,
            workspace=tmp_path,
            resolve_workspace_head=lambda _workspace: "b" * 40,
            runner_os="Linux",
            runner_arch="x86_64",
            python_version="3.11.9",
            iteration_timeout_seconds=900.0,
        )

    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "report.json").exists()


def test_runtime_reliability_rejects_fewer_than_twenty_repeats_before_mutation(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="at least 20 repeats"):
        run_runtime_reliability(
            repeats=19,
            run_root=tmp_path / "runs",
            output=tmp_path / "report.json",
            invoke=lambda _repeat, _repeat_root: _completed_process(),
            source_commit=SOURCE_COMMIT,
            workspace=tmp_path,
            resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
            runner_os="Linux",
            runner_arch="x86_64",
            python_version="3.11.9",
            iteration_timeout_seconds=900.0,
        )

    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "report.json").exists()


def test_runtime_reliability_records_invoker_errors_fail_closed(tmp_path: Path) -> None:
    def invoke(_repeat: int, _repeat_root: Path) -> BoundedProcessResult:
        raise RuntimeError(
            "could not start pytest OPENAI_API_KEY=sk-proj-runtime-reliability-error123"
        )

    output = tmp_path / "report.json"
    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=output,
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        workspace=tmp_path,
        resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
        runner_os="Linux",
        runner_arch="x86_64",
        python_version="3.11.9",
        iteration_timeout_seconds=900.0,
    )

    assert report["summary"]["passed"] is False
    assert report["summary"]["completed_repeats"] == 1
    first_failure = report["summary"]["first_failure"]
    assert first_failure["status"] == "runner_error"
    assert "could not start pytest" in first_failure["error"]
    serialized = json.dumps(report)
    assert "runtime-reliability-error123" not in serialized
    assert "<redacted>" in serialized
    receipt = json.loads(
        (tmp_path / "runs" / "repeat-01" / "iteration-receipt.json").read_text(encoding="utf-8")
    )
    assert receipt == report["runs"][0]
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_runtime_reliability_rejects_and_records_a_tracked_mutation_after_a_repeat(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    tracked = workspace / "tracked.txt"
    source_commit = _commit_file(workspace, tracked, "original\n")
    invoked: list[int] = []

    def invoke(repeat: int, _repeat_root: Path) -> BoundedProcessResult:
        invoked.append(repeat)
        tracked.write_text("mutated\n", encoding="utf-8")
        return _completed_process()

    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=invoke,
        source_commit=source_commit,
        workspace=workspace,
        resolve_workspace_head=resolve_git_head,
        runner_os="Windows",
        runner_arch="AMD64",
        python_version="3.11.9",
        iteration_timeout_seconds=900.0,
    )

    assert invoked == [1]
    assert report["summary"]["passed"] is False
    receipt = report["runs"][0]
    assert receipt["status"] == "workspace_integrity_failed"
    assert receipt["derived_passed"] is False
    assert receipt["workspace_identity"]["before"] == {
        "expected_source_commit": source_commit,
        "observed_head": source_commit,
        "clean": True,
        "passed": True,
    }
    assert receipt["workspace_identity"]["after"] == {
        "expected_source_commit": source_commit,
        "observed_head": None,
        "clean": False,
        "passed": False,
        "error": "ValueError: workspace contains uncommitted changes; exact-SHA qualification requires a clean checkout",
    }
    assert report["workspace_identity"]["final"] == receipt["workspace_identity"]["after"]


def test_runtime_reliability_rejects_and_records_a_head_switch_after_a_repeat(
    tmp_path: Path,
) -> None:
    workspace = _git_workspace(tmp_path)
    tracked = workspace / "tracked.txt"
    source_commit = _commit_file(workspace, tracked, "first\n")
    switched_commit = _commit_file(workspace, tracked, "second\n")
    subprocess.run(["git", "checkout", "--detach", source_commit], cwd=workspace, check=True)

    def invoke(_repeat: int, _repeat_root: Path) -> BoundedProcessResult:
        subprocess.run(["git", "checkout", "--detach", switched_commit], cwd=workspace, check=True)
        return _completed_process()

    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=invoke,
        source_commit=source_commit,
        workspace=workspace,
        resolve_workspace_head=resolve_git_head,
        runner_os="Windows",
        runner_arch="AMD64",
        python_version="3.11.9",
        iteration_timeout_seconds=900.0,
    )

    receipt = report["runs"][0]
    assert receipt["status"] == "workspace_integrity_failed"
    assert receipt["workspace_identity"]["after"] == {
        "expected_source_commit": source_commit,
        "observed_head": switched_commit,
        "clean": True,
        "passed": False,
    }
    assert report["summary"]["first_failure"]["workspace_identity"] == receipt[
        "workspace_identity"
    ]


def test_iteration_invoker_uses_a_fresh_interpreter_and_basetemp_per_repeat(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []
    result = _completed_process()

    def bounded_runner(
        command: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        timeout_seconds: float,
    ) -> BoundedProcessResult:
        calls.append(
            {
                "command": command,
                "cwd": cwd,
                "environment": environment,
                "timeout_seconds": timeout_seconds,
            }
        )
        return result

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    invoke = build_iteration_invoker(
        workspace=workspace,
        python_executable="/qualification/python",
        iteration_timeout_seconds=900.0,
        base_environment={"PATH": "/qualification/bin", "PYTHONHASHSEED": "unexpected"},
        bounded_runner=bounded_runner,
    )

    repeat_one = tmp_path / "repeat-01"
    repeat_two = tmp_path / "repeat-02"
    assert invoke(1, repeat_one) is result
    assert invoke(2, repeat_two) is result

    expected_targets = (
        "tests/test_channels.py::test_run_manager_channel_turn_is_durable_and_isolated_from_primary_replay",
        "tests/test_channels.py::test_server_exposes_channel_ingest_route",
        "tests/test_full_agent_runtime.py::test_run_manager_heartbeat_renews_and_releases_its_run_lease",
        "tests/test_full_agent_runtime.py::test_cross_manager_task_approval_waits_for_origin_lease_and_wakes_scheduler",
    )
    assert len(calls) == 2
    for repeat_root, call in zip((repeat_one, repeat_two), calls, strict=True):
        assert call["command"] == (
            "/qualification/python",
            "-m",
            "pytest",
            "-q",
            *expected_targets,
            "--basetemp",
            str(repeat_root / "pytest-tmp"),
        )
        assert call["cwd"] == workspace
        assert call["timeout_seconds"] == 900.0
        assert call["environment"] == {
            "PATH": "/qualification/bin",
            "PYTHONHASHSEED": "0",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        }


def test_cli_requires_an_exact_source_commit_before_creating_receipts(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("GITHUB_SHA", None)
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/run_runtime_reliability.py",
            "--run-root",
            str(tmp_path / "runs"),
            "--output",
            str(tmp_path / "report.json"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert "source-commit" in completed.stderr.lower()
    assert "traceback" not in completed.stderr.lower()
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "report.json").exists()


def test_git_identity_rejects_a_dirty_workspace_claiming_a_clean_commit(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "reliability@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Reliability Test"],
        cwd=workspace,
        check=True,
    )
    tracked = workspace / "tracked.txt"
    tracked.write_text("committed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    expected_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert resolve_git_head(workspace) == expected_head
    (workspace / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")

    with pytest.raises(ValueError, match="workspace contains uncommitted changes"):
        resolve_git_head(workspace)


def _git_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "reliability@example.invalid"],
        cwd=workspace,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Reliability Test"],
        cwd=workspace,
        check=True,
    )
    return workspace


def _commit_file(workspace: Path, tracked: Path, contents: str) -> str:
    tracked.write_text(contents, encoding="utf-8")
    subprocess.run(["git", "add", tracked.name], cwd=workspace, check=True)
    subprocess.run(
        ["git", "commit", "-m", contents.strip()],
        cwd=workspace,
        check=True,
        capture_output=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
