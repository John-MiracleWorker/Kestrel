from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.bounded_process import BoundedProcessResult
from scripts.run_runtime_reliability import (
    RUNTIME_RELIABILITY_TESTS,
    _load_test_evidence,
    _write_json,
    build_iteration_invoker,
    resolve_git_head,
    run_runtime_reliability,
)

SOURCE_COMMIT = "a" * 40
ROOT = Path(__file__).resolve().parents[1]


def _write_pytest_junit(
    repeat_root: Path,
    *,
    outcomes: dict[str, str] | None = None,
    nodeids: tuple[str, ...] = RUNTIME_RELIABILITY_TESTS,
) -> tuple[str, int]:
    outcomes = {} if outcomes is None else outcomes
    cases: list[str] = []
    counts = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for nodeid in nodeids:
        path, name = nodeid.split("::", maxsplit=1)
        classname = path.removesuffix(".py").replace("/", ".")
        outcome = outcomes.get(nodeid, "passed")
        counts[outcome] += 1
        child = {
            "passed": "",
            "failed": '<failure message="failed" />',
            "error": '<error message="error" />',
            "skipped": '<skipped message="skipped" />',
        }[outcome]
        cases.append(
            f'<testcase classname="{classname}" name="{name}" time="0.01">'
            f"{child}</testcase>"
        )
    raw = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests"><testsuite name="pytest" '
        f'errors="{counts["error"]}" failures="{counts["failed"]}" '
        f'skipped="{counts["skipped"]}" tests="{len(nodeids)}">'
        f'{"".join(cases)}</testsuite></testsuites>'
    ).encode()
    path = repeat_root / "pytest-results.xml"
    path.write_bytes(raw)
    return hashlib.sha256(raw).hexdigest(), len(raw)


def _completed_process(
    *,
    returncode: int = 0,
    stdout: str = f"{len(RUNTIME_RELIABILITY_TESTS)} passed\n",
    cleanup_attempted: bool = False,
    cleanup_succeeded: bool = True,
) -> BoundedProcessResult:
    return BoundedProcessResult(
        returncode=returncode,
        stdout=stdout,
        stderr="",
        elapsed_seconds=1.25,
        timed_out=False,
        cleanup_attempted=cleanup_attempted,
        cleanup_succeeded=cleanup_succeeded,
        termination_method=None,
        stdout_total_bytes=len(stdout.encode("utf-8")),
        stderr_total_bytes=0,
    )


def test_runtime_reliability_requires_twenty_fresh_passing_subprocess_receipts(
    tmp_path: Path,
) -> None:
    observed_roots: list[Path] = []
    identity_checks: list[Path] = []
    evidence_sources: dict[int, tuple[str, int]] = {}

    def invoke(repeat: int, repeat_root: Path) -> BoundedProcessResult:
        assert repeat == len(observed_roots) + 1
        assert repeat_root.is_dir()
        observed_roots.append(repeat_root)
        evidence_sources[repeat] = _write_pytest_junit(repeat_root)
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
        digest, size_bytes = evidence_sources[repeat]
        assert run["test_evidence"] == {
            "schema": "kestrel.pytest_evidence.v1",
            "format": "junit_xml",
            "source": {
                "path": "pytest-results.xml",
                "sha256": digest,
                "size_bytes": size_bytes,
            },
            "expected_tests": list(RUNTIME_RELIABILITY_TESTS),
            "observed": [
                {"nodeid": nodeid, "outcome": "passed"}
                for nodeid in RUNTIME_RELIABILITY_TESTS
            ],
            "summary": {
                "expected": len(RUNTIME_RELIABILITY_TESTS),
                "observed": len(RUNTIME_RELIABILITY_TESTS),
                "passed": len(RUNTIME_RELIABILITY_TESTS),
                "failed": 0,
                "errors": 0,
                "skipped": 0,
                "missing": [],
                "unexpected": [],
                "duplicates": [],
                "declared": {
                    "tests": len(RUNTIME_RELIABILITY_TESTS),
                    "errors": 0,
                    "failures": 0,
                    "skipped": 0,
                },
                "declared_matches": True,
            },
            "status": "verified",
            "passed": True,
            "raw_source_retained": False,
        }
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
        assert not (tmp_path / "runs" / f"repeat-{repeat:02d}" / "pytest-results.xml").exists()
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_runtime_reliability_fails_when_cleanup_is_unverified_without_attempt(
    tmp_path: Path,
) -> None:
    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=lambda _repeat, _repeat_root: _completed_process(
            cleanup_attempted=False,
            cleanup_succeeded=False,
        ),
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
    assert report["summary"]["consecutive_passes"] == 0
    assert report["summary"]["failure_count"] == 1
    receipt = report["runs"][0]
    assert receipt["status"] == "cleanup_unverified"
    assert receipt["derived_passed"] is False
    assert receipt["cleanup"] == {
        "attempted": False,
        "succeeded": False,
        "method": None,
    }


@pytest.mark.parametrize(
    ("outcomes", "expected_passed", "expected_skipped"),
    [
        (
            {nodeid: "skipped" for nodeid in RUNTIME_RELIABILITY_TESTS},
            0,
            len(RUNTIME_RELIABILITY_TESTS),
        ),
        (
            {RUNTIME_RELIABILITY_TESTS[-1]: "skipped"},
            len(RUNTIME_RELIABILITY_TESTS) - 1,
            1,
        ),
    ],
    ids=["all-skipped", "partially-skipped"],
)
def test_runtime_reliability_rejects_zero_exit_with_skipped_tests(
    tmp_path: Path,
    outcomes: dict[str, str],
    expected_passed: int,
    expected_skipped: int,
) -> None:
    def invoke(_repeat: int, repeat_root: Path) -> BoundedProcessResult:
        _write_pytest_junit(repeat_root, outcomes=outcomes)
        return _completed_process(stdout=f"{expected_passed} passed, {expected_skipped} skipped\n")

    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
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
    assert report["summary"]["consecutive_passes"] == 0
    receipt = report["runs"][0]
    assert receipt["status"] == "test_evidence_invalid"
    assert receipt["derived_passed"] is False
    evidence = receipt["test_evidence"]
    assert evidence["status"] == "mismatch"
    assert evidence["passed"] is False
    assert evidence["summary"]["passed"] == expected_passed
    assert evidence["summary"]["skipped"] == expected_skipped


@pytest.mark.parametrize(
    ("raw", "expected_status"),
    [
        (None, "missing"),
        (b"<testsuites><broken>", "malformed"),
        (
            b'<!DOCTYPE testsuites [<!ENTITY injected "boom">]>'
            b"<testsuites>&injected;</testsuites>",
            "malformed",
        ),
        (
            b'<testsuites name="pytest tests"><testsuite name="pytest" errors="0" '
            b'failures="0" skipped="0" tests="'
            + (b"9" * 5_000)
            + b'"></testsuite></testsuites>',
            "malformed",
        ),
    ],
)
def test_runtime_reliability_rejects_missing_or_malformed_test_evidence(
    tmp_path: Path,
    raw: bytes | None,
    expected_status: str,
) -> None:
    def invoke(_repeat: int, repeat_root: Path) -> BoundedProcessResult:
        if raw is not None:
            (repeat_root / "pytest-results.xml").write_bytes(raw)
        return _completed_process()

    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        workspace=tmp_path,
        resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
        runner_os="Linux",
        runner_arch="x86_64",
        python_version="3.11.9",
        iteration_timeout_seconds=900.0,
    )

    receipt = report["runs"][0]
    assert receipt["status"] == "test_evidence_invalid"
    assert receipt["derived_passed"] is False
    assert receipt["test_evidence"]["status"] == expected_status
    assert receipt["test_evidence"]["passed"] is False
    assert not (tmp_path / "runs" / "repeat-01" / "pytest-results.xml").exists()


def test_runtime_reliability_discards_unredacted_junit_diagnostics(
    tmp_path: Path,
) -> None:
    secret = "sk-proj-runtime-junit-leak-123456789"

    def invoke(_repeat: int, repeat_root: Path) -> BoundedProcessResult:
        _write_pytest_junit(
            repeat_root,
            outcomes={RUNTIME_RELIABILITY_TESTS[0]: "failed"},
        )
        evidence_path = repeat_root / "pytest-results.xml"
        raw = evidence_path.read_text(encoding="utf-8")
        evidence_path.write_text(
            raw.replace('message="failed"', f'message="{secret}"'),
            encoding="utf-8",
        )
        return _completed_process(returncode=1, stdout=f"failure: {secret}\n")

    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        workspace=tmp_path,
        resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
        runner_os="Linux",
        runner_arch="x86_64",
        python_version="3.11.9",
        iteration_timeout_seconds=900.0,
    )

    receipt = report["runs"][0]
    assert receipt["status"] == "runner_nonzero"
    assert receipt["test_evidence"]["summary"]["failed"] == 1
    assert secret not in json.dumps(report)
    assert not (tmp_path / "runs" / "repeat-01" / "pytest-results.xml").exists()


def test_runtime_reliability_fails_closed_if_raw_evidence_cannot_be_discarded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repeat_root = tmp_path / "repeat-01"
    repeat_root.mkdir()
    _write_pytest_junit(repeat_root)
    evidence_path = repeat_root / "pytest-results.xml"
    unlink = Path.unlink

    def fail_evidence_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == evidence_path:
            raise OSError("simulated evidence cleanup failure")
        unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_evidence_unlink)

    evidence = _load_test_evidence(evidence_path)

    assert evidence["status"] == "raw_cleanup_failed"
    assert evidence["passed"] is False
    assert "simulated evidence cleanup failure" in evidence["error"]


@pytest.mark.parametrize(
    ("nodeids", "expected_missing", "expected_unexpected", "expected_duplicates"),
    [
        (
            (
                *RUNTIME_RELIABILITY_TESTS[:-1],
                "tests/test_channels.py::test_unexpected_runtime_case",
            ),
            [RUNTIME_RELIABILITY_TESTS[-1]],
            ["tests.test_channels::test_unexpected_runtime_case"],
            [],
        ),
        (
            (*RUNTIME_RELIABILITY_TESTS[:-1], RUNTIME_RELIABILITY_TESTS[-2]),
            [RUNTIME_RELIABILITY_TESTS[-1]],
            [],
            [RUNTIME_RELIABILITY_TESTS[-2]],
        ),
    ],
    ids=["unexpected", "duplicate"],
)
def test_runtime_reliability_rejects_evidence_for_the_wrong_test_set(
    tmp_path: Path,
    nodeids: tuple[str, ...],
    expected_missing: list[str],
    expected_unexpected: list[str],
    expected_duplicates: list[str],
) -> None:
    def invoke(_repeat: int, repeat_root: Path) -> BoundedProcessResult:
        _write_pytest_junit(repeat_root, nodeids=nodeids)
        return _completed_process()

    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        workspace=tmp_path,
        resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
        runner_os="Linux",
        runner_arch="x86_64",
        python_version="3.11.9",
        iteration_timeout_seconds=900.0,
    )

    evidence = report["runs"][0]["test_evidence"]
    assert report["runs"][0]["status"] == "test_evidence_invalid"
    assert evidence["summary"]["missing"] == expected_missing
    assert evidence["summary"]["unexpected"] == expected_unexpected
    assert evidence["summary"]["duplicates"] == expected_duplicates
    assert evidence["passed"] is False


@pytest.mark.parametrize(
    "variant",
    [
        "declared-counts-mismatch",
        "suite-level-error",
        "testcases-directly-under-root",
        "testcases-nested-in-failure",
        "outcome-nested-in-system-output",
        "namespaced-outcome-nested-in-system-output",
    ],
)
def test_runtime_reliability_rejects_noncanonical_junit_structure(
    tmp_path: Path,
    variant: str,
) -> None:
    def invoke(_repeat: int, repeat_root: Path) -> BoundedProcessResult:
        _write_pytest_junit(repeat_root)
        evidence_path = repeat_root / "pytest-results.xml"
        raw = evidence_path.read_text(encoding="utf-8")
        suite_start = raw.index('<testsuite name="pytest"')
        case_start = raw.index(">", suite_start) + 1
        case_end = raw.index("</testsuite>", case_start)
        cases = raw[case_start:case_end]
        if variant == "declared-counts-mismatch":
            raw = raw.replace(
                f'errors="0" failures="0" skipped="0" tests="{len(RUNTIME_RELIABILITY_TESTS)}"',
                'errors="7" failures="6" skipped="5" tests="99"',
            )
        elif variant == "suite-level-error":
            raw = raw.replace(
                "</testsuite>",
                '<error message="collection failed" /></testsuite>',
                1,
            )
        elif variant == "testcases-directly-under-root":
            raw = f'<testsuites name="pytest tests">{cases}</testsuites>'
        elif variant == "testcases-nested-in-failure":
            raw = (
                '<testsuites name="pytest tests"><testsuite name="pytest" '
                f'errors="0" failures="0" skipped="0" tests="{len(RUNTIME_RELIABILITY_TESTS)}">'
                f'<failure message="nested">{cases}</failure>'
                "</testsuite></testsuites>"
            )
        elif variant == "outcome-nested-in-system-output":
            raw = raw.replace(
                "</testcase>",
                '<system-out><failure message="hidden" /></system-out></testcase>',
                1,
            )
        elif variant == "namespaced-outcome-nested-in-system-output":
            raw = raw.replace(
                "</testcase>",
                '<system-out><failure xmlns="urn:hidden" /></system-out></testcase>',
                1,
            )
        else:  # pragma: no cover - the parametrization is exhaustive
            raise AssertionError(f"unexpected variant: {variant}")
        evidence_path.write_text(raw, encoding="utf-8")
        return _completed_process()

    report = run_runtime_reliability(
        repeats=20,
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        workspace=tmp_path,
        resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
        runner_os="Linux",
        runner_arch="x86_64",
        python_version="3.11.9",
        iteration_timeout_seconds=900.0,
    )

    receipt = report["runs"][0]
    assert report["summary"]["passed"] is False
    assert report["summary"]["completed_repeats"] == 1
    assert receipt["status"] == "test_evidence_invalid"
    assert receipt["derived_passed"] is False
    assert receipt["test_evidence"]["status"] in {"malformed", "mismatch"}
    assert receipt["test_evidence"]["passed"] is False


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_runtime_reliability_rejects_non_finite_iteration_deadlines(
    tmp_path: Path,
    timeout: float,
) -> None:
    with pytest.raises(ValueError, match="finite and greater than zero"):
        run_runtime_reliability(
            repeats=20,
            run_root=tmp_path / "runs",
            output=tmp_path / "report.json",
            invoke=lambda _repeat, _repeat_root: _completed_process(),
            source_commit=SOURCE_COMMIT,
            workspace=tmp_path,
            resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
            runner_os="Linux",
            runner_arch="x86_64",
            python_version="3.11.9",
            iteration_timeout_seconds=timeout,
        )
    with pytest.raises(ValueError, match="finite and greater than zero"):
        build_iteration_invoker(
            workspace=tmp_path,
            python_executable=sys.executable,
            iteration_timeout_seconds=timeout,
        )

    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "report.json").exists()


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf"])
def test_runtime_reliability_cli_rejects_non_finite_iteration_deadlines(
    tmp_path: Path,
    timeout: str,
) -> None:
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/run_runtime_reliability.py",
            "--source-commit",
            SOURCE_COMMIT,
            f"--iteration-timeout-seconds={timeout}",
            "--run-root",
            str(tmp_path / "runs"),
            "--output",
            str(tmp_path / "report.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert "finite and greater than 0" in completed.stderr
    assert "traceback" not in completed.stderr.lower()
    assert not (tmp_path / "runs").exists()
    assert not (tmp_path / "report.json").exists()


def test_runtime_reliability_default_receipt_root_is_git_ignored() -> None:
    for receipt_path in (
        "tmp-runtime-reliability/runs/repeat-01/iteration-receipt.json",
        "tmp-runtime-reliability/report.json",
    ):
        completed = subprocess.run(  # noqa: S603
            ["git", "check-ignore", "--quiet", receipt_path],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )

        assert completed.returncode == 0


def test_runtime_reliability_writes_receipts_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replace_calls: list[tuple[Path, Path]] = []
    replace = Path.replace

    def replace_and_record(source: Path, target: Path) -> Path:
        replace_calls.append((source, target))
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", replace_and_record)
    output = tmp_path / "report.json"

    _write_json(output, {"schema": "kestrel.runtime_reliability_report.v1"})

    temporary = tmp_path / ".report.json.tmp"
    assert replace_calls == [(temporary, output)]
    assert not temporary.exists()
    assert json.loads(output.read_text(encoding="utf-8")) == {
        "schema": "kestrel.runtime_reliability_report.v1"
    }


def test_runtime_reliability_stops_at_first_failure_with_redacted_diagnostics(
    tmp_path: Path,
) -> None:
    invoked: list[int] = []

    def invoke(repeat: int, _repeat_root: Path) -> BoundedProcessResult:
        invoked.append(repeat)
        if repeat == 1:
            _write_pytest_junit(_repeat_root)
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
        base_environment={
            "PATH": "/qualification/bin",
            "PYTHONHASHSEED": "unexpected",
            "PYTEST_ADDOPTS": "-p unexpected --maxfail=1",
            "PYTEST_PLUGINS": "unexpected.plugin",
        },
        bounded_runner=bounded_runner,
    )

    repeat_one = tmp_path / "repeat-01"
    repeat_two = tmp_path / "repeat-02"
    assert invoke(1, repeat_one) is result
    assert invoke(2, repeat_two) is result

    expected_targets = (
        "tests/test_channels.py::test_run_manager_channel_turn_is_durable_and_isolated_from_primary_replay",
        "tests/test_channels.py::test_server_exposes_channel_ingest_route",
        "tests/test_channels.py::test_public_channel_webhook_allows_explicit_unsigned_channel",
        "tests/test_lan_scan_manager.py::test_manual_confirm_requires_exact_consent_and_cached_authority_without_writes[nonzero-cas]",
        "tests/test_full_agent_runtime.py::test_run_manager_heartbeat_renews_and_releases_its_run_lease",
        "tests/test_full_agent_runtime.py::test_cancelling_queued_run_finishes_publication_fence_without_worker",
        "tests/test_full_agent_runtime.py::test_approval_heartbeat_delayed_renewal_cannot_cancel_after_finalization",
        "tests/test_full_agent_runtime.py::test_approved_repair_scheduler_flow_binds_real_validation_and_review_receipts",
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
            "--junitxml",
            str(repeat_root / "pytest-results.xml"),
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
