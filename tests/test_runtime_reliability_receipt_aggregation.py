from __future__ import annotations

import copy
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.bounded_process import BoundedProcessResult
from scripts.golden_eval_contract import GOLDEN_CASE_CATEGORIES
from scripts.run_determinism_evals import (
    IterationInvocation,
    _deterministic_projection,
    _signature,
    run_determinism,
)
from scripts.run_determinism_evals import (
    _excerpt as _golden_excerpt,
)
from scripts.run_golden_evals import _summary as _golden_summary
from scripts.run_runtime_reliability import (
    RUNTIME_RELIABILITY_TESTS,
    run_runtime_reliability,
)
from scripts.run_runtime_reliability import (
    _excerpt as _runtime_excerpt,
)

SOURCE_COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
WORKFLOW_RUN_ID = 4242
RUN_ATTEMPT = 1

CELL_IDS = (
    "runtime-linux",
    "runtime-macos",
    "runtime-windows",
    "determinism-memory",
    "determinism-memvid",
)


def _artifact_name(cell_id: str, source_commit: str = SOURCE_COMMIT) -> str:
    selectors = {
        "runtime-linux": "kestrel-runtime-reliability-Linux",
        "runtime-macos": "kestrel-runtime-reliability-macOS",
        "runtime-windows": "kestrel-runtime-reliability-Windows",
        "determinism-memory": "kestrel-determinism-memory",
        "determinism-memvid": "kestrel-determinism-memvid",
    }
    return f"{selectors[cell_id]}-{source_commit}"


def _load_api() -> tuple[Callable[..., dict[str, object]], Callable[..., dict[str, object]]]:
    try:
        module = importlib.import_module("scripts.aggregate_runtime_reliability_receipts")
    except ModuleNotFoundError as exc:
        pytest.fail(
            "Unit 2.3 RED: scripts.aggregate_runtime_reliability_receipts is missing",
            pytrace=False,
        )
        raise AssertionError("unreachable") from exc
    return module.build_qualification, module.verify_qualification


def _read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_pytest_junit(repeat_root: Path) -> None:
    cases: list[str] = []
    for nodeid in RUNTIME_RELIABILITY_TESTS:
        path, name = nodeid.split("::", maxsplit=1)
        classname = path.removesuffix(".py").replace("/", ".")
        cases.append(f'<testcase classname="{classname}" name="{name}" time="0.01" />')
    raw = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<testsuites name="pytest tests"><testsuite name="pytest" '
        f'errors="0" failures="0" skipped="0" tests="{len(cases)}">'
        f'{"".join(cases)}</testsuite></testsuites>'
    )
    (repeat_root / "pytest-results.xml").write_text(raw, encoding="utf-8")


def _completed_process(*, native_windows_cleanup: bool = False) -> BoundedProcessResult:
    stdout = f"{len(RUNTIME_RELIABILITY_TESTS)} passed\n"
    return BoundedProcessResult(
        returncode=0,
        stdout=stdout,
        stderr="",
        elapsed_seconds=1.25,
        timed_out=False,
        cleanup_attempted=native_windows_cleanup,
        cleanup_succeeded=True,
        termination_method=(
            "windows_job_object_quiesced" if native_windows_cleanup else None
        ),
        stdout_total_bytes=len(stdout.encode("utf-8")),
        stderr_total_bytes=0,
    )


def _write_runtime_artifact(
    root: Path,
    *,
    cell_id: str,
    runner_os: str,
    runner_arch: str,
    python_version: str,
) -> None:
    artifact_root = root / _artifact_name(cell_id)
    artifact_root.mkdir(parents=True)

    def invoke(_repeat: int, repeat_root: Path) -> BoundedProcessResult:
        _write_pytest_junit(repeat_root)
        return _completed_process(native_windows_cleanup=runner_os == "Windows")

    run_runtime_reliability(
        repeats=20,
        run_root=artifact_root / "kestrel-runtime-reliability-runs",
        output=artifact_root / "kestrel-runtime-reliability-report.json",
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        workspace=artifact_root,
        resolve_workspace_head=lambda _workspace: SOURCE_COMMIT,
        runner_os=runner_os,
        runner_arch=runner_arch,
        python_version=python_version,
        iteration_timeout_seconds=150.0,
    )


def _golden_report(*, backend: str, repeat: int) -> dict[str, object]:
    results: list[dict[str, object]] = []
    for index, (name, category) in enumerate(GOLDEN_CASE_CATEGORIES.items()):
        results.append(
            {
                "name": name,
                "category": category,
                "passed": True,
                "score": 1.0,
                "latency_ms": repeat + index / 100,
                "memory_hits": 1,
                "context_chars": 3,
                "tool_count": 1,
                "cost_estimate_usd": None,
                "executed_tools": ["repo.map"],
            }
        )
    summary = _golden_summary(results, max_case_latency_ms=45000.0)
    acceptance = summary["acceptance"]
    assert isinstance(acceptance, dict)
    return {
        "schema": "kestrel.golden_eval_report.v2",
        "configuration": {
            "backend": backend,
            "provider": "mock",
            "model": "mock",
            "seed": 1729,
            "max_case_latency_ms": 45000.0,
        },
        "results": results,
        "summary": summary,
        "acceptance": {
            "functional": {"required": True, "passed": True},
            "latency": dict(acceptance["latency"]),
            "cost": dict(acceptance["cost"]),
        },
        "passed": True,
    }


def _write_determinism_artifact(root: Path, *, backend: str) -> None:
    cell_id = f"determinism-{backend}"
    artifact_root = root / _artifact_name(cell_id)
    artifact_root.mkdir(parents=True)
    reports = {
        repeat: _golden_report(backend=backend, repeat=repeat)
        for repeat in range(1, 21)
    }

    def invoke(repeat: int, memory_root: Path, _seed: int) -> IterationInvocation:
        report = reports[repeat]
        _write_json(memory_root.parent / "golden-report.json", report)
        return IterationInvocation(
            report=report,
            diagnostics={
                "status": "completed",
                "runner_exit_code": 0,
                "elapsed_seconds": 2.5,
                "deadline": {
                    "clock": "monotonic",
                    "seconds": 1500.0,
                    "exceeded": False,
                },
                "cleanup": {
                    "attempted": False,
                    "succeeded": True,
                    "method": None,
                },
                "capture": {
                    "limit_bytes_per_stream": 262144,
                    "stdout_total_bytes": 0,
                    "stdout_truncated": False,
                    "stderr_total_bytes": 0,
                    "stderr_truncated": False,
                },
                "stdout": "",
                "stderr": "",
            },
        )

    run_determinism(
        repeats=20,
        seed=1729,
        backend=backend,  # type: ignore[arg-type]
        run_root=artifact_root / "kestrel-determinism-runs",
        output=artifact_root / "kestrel-determinism-report.json",
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        runner_os="Linux",
        runner_arch="X64",
        python_version="3.11.15",
        max_case_latency_ms=45000.0,
        case_timeout_seconds=60.0,
        iteration_timeout_seconds=1500.0,
    )


@pytest.fixture(scope="session")
def five_cell_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("five-cell-artifacts")
    _write_runtime_artifact(
        root,
        cell_id="runtime-linux",
        runner_os="Linux",
        runner_arch="X64",
        python_version="3.11.15",
    )
    _write_runtime_artifact(
        root,
        cell_id="runtime-macos",
        runner_os="macOS",
        runner_arch="ARM64",
        python_version="3.11.9",
    )
    _write_runtime_artifact(
        root,
        cell_id="runtime-windows",
        runner_os="Windows",
        runner_arch="X64",
        python_version="3.11.9",
    )
    _write_determinism_artifact(root, backend="memory")
    _write_determinism_artifact(root, backend="memvid")
    return root


@pytest.fixture
def five_cell_artifacts(tmp_path: Path, five_cell_template: Path) -> Path:
    root = tmp_path / "artifacts"
    shutil.copytree(five_cell_template, root)
    return root


def _build(root: Path, *, run_attempt: int = RUN_ATTEMPT) -> dict[str, object]:
    build_qualification, _verify_qualification = _load_api()
    return build_qualification(
        root,
        source_commit=SOURCE_COMMIT,
        workflow_run_id=WORKFLOW_RUN_ID,
        run_attempt=run_attempt,
    )


def _verify(root: Path, qualification: dict[str, object]) -> dict[str, object]:
    _build_qualification, verify_qualification = _load_api()
    return verify_qualification(
        root,
        qualification,
        source_commit=SOURCE_COMMIT,
        workflow_run_id=WORKFLOW_RUN_ID,
        run_attempt=RUN_ATTEMPT,
    )


def _runtime_report(root: Path, cell_id: str) -> Path:
    return root / _artifact_name(cell_id) / "kestrel-runtime-reliability-report.json"


def _runtime_receipt(root: Path, cell_id: str, repeat: int) -> Path:
    return (
        root
        / _artifact_name(cell_id)
        / "kestrel-runtime-reliability-runs"
        / f"repeat-{repeat:02d}"
        / "iteration-receipt.json"
    )


def _determinism_report(root: Path, backend: str) -> Path:
    return root / _artifact_name(f"determinism-{backend}") / "kestrel-determinism-report.json"


def _determinism_repeat_file(root: Path, backend: str, repeat: int, filename: str) -> Path:
    return (
        root
        / _artifact_name(f"determinism-{backend}")
        / "kestrel-determinism-runs"
        / f"repeat-{repeat:02d}"
        / filename
    )


def _rebind_golden_report_digest(
    root: Path,
    backend: str,
    repeat: int,
    *,
    outcome_signature: bool = False,
) -> None:
    golden_path = _determinism_repeat_file(root, backend, repeat, "golden-report.json")
    determinism_path = _determinism_report(root, backend)
    golden = _read_json(golden_path)
    determinism = _read_json(determinism_path)
    runs = determinism["runs"]
    assert isinstance(runs, list)
    run = runs[repeat - 1]
    assert isinstance(run, dict)
    canonical = json.dumps(
        golden,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode()
    run["golden_report_sha256"] = hashlib.sha256(canonical).hexdigest()
    if outcome_signature:
        assert backend in {"memory", "memvid"}
        run["outcome_signature"] = _signature(
            _deterministic_projection(
                golden,
                expected_backend=backend,  # type: ignore[arg-type]
            )
        )
    _write_json(determinism_path, determinism)


def test_builds_and_independently_verifies_exact_five_cell_qualification(
    five_cell_artifacts: Path,
) -> None:
    qualification = _build(five_cell_artifacts)

    assert set(qualification) == {
        "schema",
        "subject",
        "workflow",
        "required_cells",
        "cells",
        "totals",
        "diagnostics",
        "passed",
    }
    assert qualification["schema"] == "kestrel.runtime_reliability_qualification.v1"
    assert qualification["subject"] == {"source_commit": SOURCE_COMMIT}
    assert qualification["workflow"] == {
        "run_id": WORKFLOW_RUN_ID,
        "run_attempt": RUN_ATTEMPT,
    }
    assert qualification["required_cells"] == list(CELL_IDS)
    assert qualification["totals"] == {
        "required_cells": 5,
        "passed_cells": 5,
        "required_repeats": 100,
        "completed_repeats": 100,
        "runtime_repeats": 60,
        "golden_repeats": 40,
        "runtime_test_executions": 420,
        "golden_case_executions": 840,
        "failure_count": 0,
        "observed_flake_count": 0,
        "cleanup_failure_count": 0,
    }
    assert qualification["diagnostics"] == {
        "missing_cells": [],
        "stale_cells": [],
        "mismatched_cells": [],
        "duplicate_cells": [],
    }
    assert qualification["passed"] is True

    cells = qualification["cells"]
    assert isinstance(cells, list)
    assert [cell["cell_id"] for cell in cells] == list(CELL_IDS)
    expected_file_counts = [21, 21, 21, 41, 41]
    expected_schemas = [
        "kestrel.runtime_reliability_report.v1",
        "kestrel.runtime_reliability_report.v1",
        "kestrel.runtime_reliability_report.v1",
        "kestrel.determinism_eval_report.v3",
        "kestrel.determinism_eval_report.v3",
    ]
    expected_environments = [
        {"runner_os": "Linux", "runner_arch": "X64", "python_version": "3.11.15"},
        {"runner_os": "macOS", "runner_arch": "ARM64", "python_version": "3.11.9"},
        {"runner_os": "Windows", "runner_arch": "X64", "python_version": "3.11.9"},
        {"os": "Linux", "architecture": "X64", "python_version": "3.11.15"},
        {"os": "Linux", "architecture": "X64", "python_version": "3.11.15"},
    ]
    for cell, cell_id, expected_file_count in zip(
        cells,
        CELL_IDS,
        expected_file_counts,
        strict=True,
    ):
        cell_index = CELL_IDS.index(cell_id)
        assert cell["artifact_name"] == _artifact_name(cell_id)
        assert cell["source_schema"] == expected_schemas[cell_index]
        assert cell["environment"] == expected_environments[cell_index]
        assert cell["file_count"] == expected_file_count
        assert cell["derived"]["passed"] is True
        assert cell["derived"]["required_repeats"] == 20
        assert cell["derived"]["completed_repeats"] == 20
        report_path = (
            _runtime_report(five_cell_artifacts, cell_id)
            if cell_id.startswith("runtime-")
            else _determinism_report(five_cell_artifacts, cell_id.removeprefix("determinism-"))
        )
        assert cell["report_sha256"] == _sha256(report_path)
        assert len(cell["artifact_manifest_sha256"]) == 64
        int(cell["artifact_manifest_sha256"], 16)

    assert _verify(five_cell_artifacts, qualification) == qualification


@pytest.mark.parametrize(
    "missing_cell",
    CELL_IDS,
)
def test_rejects_a_missing_required_cell(
    five_cell_artifacts: Path,
    missing_cell: str,
) -> None:
    shutil.rmtree(five_cell_artifacts / _artifact_name(missing_cell))

    with pytest.raises(ValueError, match="missing|required cell"):
        _build(five_cell_artifacts)


@pytest.mark.parametrize("substitution", ["backend", "platform", "source_commit"])
def test_rejects_backend_platform_or_sha_substitution(
    five_cell_artifacts: Path,
    substitution: str,
) -> None:
    if substitution == "backend":
        report_path = _determinism_report(five_cell_artifacts, "memvid")
        report = _read_json(report_path)
        configuration = report["configuration"]
        assert isinstance(configuration, dict)
        configuration["backend"] = "memory"
    elif substitution == "platform":
        report_path = _runtime_report(five_cell_artifacts, "runtime-macos")
        report = _read_json(report_path)
        environment = report["environment"]
        assert isinstance(environment, dict)
        environment["runner_os"] = "Linux"
    else:
        report_path = _runtime_report(five_cell_artifacts, "runtime-windows")
        report = _read_json(report_path)
        subject = report["subject"]
        assert isinstance(subject, dict)
        subject["source_commit"] = OTHER_COMMIT
    _write_json(report_path, report)

    with pytest.raises(ValueError, match="backend|platform|runner|source|commit|SHA"):
        _build(five_cell_artifacts)


def test_rejects_nineteen_external_runtime_receipts(five_cell_artifacts: Path) -> None:
    _runtime_receipt(five_cell_artifacts, "runtime-linux", 20).unlink()

    with pytest.raises(ValueError, match="repeat|receipt|missing"):
        _build(five_cell_artifacts)


def test_rejects_reordered_runtime_receipts(five_cell_artifacts: Path) -> None:
    first_path = _runtime_receipt(five_cell_artifacts, "runtime-macos", 1)
    second_path = _runtime_receipt(five_cell_artifacts, "runtime-macos", 2)
    first = _read_json(first_path)
    second = _read_json(second_path)
    _write_json(first_path, second)
    _write_json(second_path, first)

    with pytest.raises(ValueError, match="repeat|receipt|order"):
        _build(five_cell_artifacts)


def test_rejects_external_runtime_receipt_that_differs_from_embedded_run(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-windows", 7)
    receipt = _read_json(receipt_path)
    receipt["elapsed_seconds"] = 99.0
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="embedded|external|receipt|run"):
        _build(five_cell_artifacts)


def test_accepts_digest_bound_runtime_output_with_a_bounded_excerpt(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    full_stdout = "x" * 5_000
    embedded["stdout"] = "[truncated]..." + full_stdout[-4_000:]
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = len(full_stdout.encode("utf-8"))
    capture["stdout_sha256"] = hashlib.sha256(full_stdout.encode("utf-8")).hexdigest()
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    assert _build(five_cell_artifacts)["passed"] is True


def test_accepts_runtime_capture_decoded_with_utf8_replacement(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    retained_stdout = "\ufffd" * 10
    embedded["stdout"] = retained_stdout
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 10
    capture["stdout_sha256"] = hashlib.sha256(
        retained_stdout.encode("utf-8")
    ).hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    assert _build(five_cell_artifacts)["passed"] is True


def test_rejects_runtime_utf8_replacement_with_impossible_raw_byte_count(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    embedded["stdout"] = "\ufffd"
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 4
    capture["stdout_sha256"] = hashlib.sha256("\ufffd".encode("utf-8")).hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    with pytest.raises(ValueError, match="capture|diagnostic|source|byte"):
        _build(five_cell_artifacts)


def test_accepts_honestly_truncated_runtime_capture(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    captured_tail = "x" * 262_144
    embedded["stdout"] = _runtime_excerpt(captured_tail)
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 262_145
    capture["stdout_sha256"] = hashlib.sha256(
        captured_tail.encode("utf-8")
    ).hexdigest()
    capture["stdout_truncated"] = True
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    assert _build(five_cell_artifacts)["passed"] is True


def test_accepts_digest_bound_runtime_output_redacted_to_the_same_byte_length(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    raw_stdout = "abcdefghij"
    retained_stdout = "<redacted>"
    assert len(raw_stdout.encode("utf-8")) == len(retained_stdout.encode("utf-8"))
    embedded["stdout"] = retained_stdout
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = len(raw_stdout.encode("utf-8"))
    capture["stdout_sha256"] = hashlib.sha256(raw_stdout.encode("utf-8")).hexdigest()
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    assert _build(five_cell_artifacts)["passed"] is True


def test_accepts_runtime_excerpt_with_redaction_cut_at_truncation_boundary(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    raw_stdout = 'TOKEN=""' + "x" * 3_992
    retained_stdout = _runtime_excerpt(raw_stdout)
    assert retained_stdout == "[truncated]...edacted>" + "x" * 3_992
    embedded["stdout"] = retained_stdout
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = len(raw_stdout.encode("utf-8"))
    capture["stdout_sha256"] = hashlib.sha256(raw_stdout.encode("utf-8")).hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    assert _build(five_cell_artifacts)["passed"] is True


def test_rejects_runtime_capture_claiming_untruncated_output_above_its_limit(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    full_stdout = "x" * 262_145
    embedded["stdout"] = "[truncated]..." + full_stdout[-4_000:]
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = len(full_stdout.encode("utf-8"))
    capture["stdout_sha256"] = hashlib.sha256(full_stdout.encode("utf-8")).hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    with pytest.raises(ValueError, match="capture|limit|truncat|byte"):
        _build(five_cell_artifacts)


def test_rejects_runtime_truncated_excerpt_larger_than_claimed_source(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    embedded["stdout"] = "[truncated]..." + "x" * 4_000
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 1
    capture["stdout_sha256"] = hashlib.sha256(b"x").hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    with pytest.raises(ValueError, match="capture|excerpt|source|byte|diagnostic"):
        _build(five_cell_artifacts)


def test_rejects_runtime_short_literal_truncation_prefix(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    embedded["stdout"] = "[truncated]...x"
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 2
    capture["stdout_sha256"] = hashlib.sha256(b"xx").hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    with pytest.raises(ValueError, match="capture|excerpt|bound|diagnostic"):
        _build(five_cell_artifacts)


def test_accepts_runtime_output_with_a_literal_truncation_prefix(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    retained_stdout = "[truncated]...x"
    embedded["stdout"] = retained_stdout
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = len(retained_stdout.encode("utf-8"))
    capture["stdout_sha256"] = hashlib.sha256(
        retained_stdout.encode("utf-8")
    ).hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    assert _build(five_cell_artifacts)["passed"] is True


def test_rejects_runtime_partial_redaction_marker_with_impossible_source_size(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    embedded["stdout"] = "[truncated]...edacted>" + "x" * 3_992
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 3_991
    capture["stdout_sha256"] = hashlib.sha256(b"x" * 3_991).hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    with pytest.raises(ValueError, match="capture|excerpt|source|byte|redact"):
        _build(five_cell_artifacts)


def test_rejects_nonempty_redacted_runtime_output_with_zero_total_bytes(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    embedded["stdout"] = "<redacted>"
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 0
    capture["stdout_sha256"] = hashlib.sha256(b"").hexdigest()
    capture["stdout_truncated"] = False
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    with pytest.raises(ValueError, match="capture|redact|byte|diagnostic"):
        _build(five_cell_artifacts)


def test_rejects_float_disguised_as_runtime_capture_limit(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    capture = embedded["capture"]
    assert isinstance(capture, dict)
    capture["limit_bytes"] = 262144.0
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    with pytest.raises(ValueError, match="capture|receipt|run"):
        _build(five_cell_artifacts)


def test_rejects_failed_golden_iteration_receipt(five_cell_artifacts: Path) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        11,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    receipt["status"] = "runner_nonzero"
    receipt["runner_exit_code"] = 1
    receipt["derived_passed"] = False
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="iteration|receipt|pass|status"):
        _build(five_cell_artifacts)


def test_rejects_float_disguised_as_golden_capture_limit(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["limit_bytes_per_stream"] = 262144.0
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="capture|iteration|receipt"):
        _build(five_cell_artifacts)


def test_rejects_golden_capture_claiming_untruncated_output_above_its_limit(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    full_stdout = "x" * 262_145
    receipt["stdout"] = "[truncated]..." + full_stdout[-2_000:]
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = len(full_stdout.encode("utf-8"))
    capture["stdout_truncated"] = False
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="capture|limit|truncat|byte"):
        _build(five_cell_artifacts)


def test_accepts_golden_capture_decoded_with_utf8_replacement(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    receipt["stdout"] = "\ufffd" * 10
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 10
    capture["stdout_truncated"] = False
    _write_json(receipt_path, receipt)

    assert _build(five_cell_artifacts)["passed"] is True


def test_rejects_golden_utf8_replacement_with_impossible_raw_byte_count(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    receipt["stdout"] = "\ufffd"
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 4
    capture["stdout_truncated"] = False
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="capture|diagnostic|source|byte"):
        _build(five_cell_artifacts)


def test_accepts_honestly_truncated_golden_capture(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    receipt["stdout"] = _golden_excerpt("x" * 262_144)
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 262_145
    capture["stdout_truncated"] = True
    _write_json(receipt_path, receipt)

    assert _build(five_cell_artifacts)["passed"] is True


def test_accepts_golden_excerpt_with_redaction_cut_at_truncation_boundary(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    raw_stdout = 'TOKEN=""' + "x" * 1_992
    retained_stdout = _golden_excerpt(raw_stdout)
    assert retained_stdout == "[truncated]...edacted>" + "x" * 1_992
    receipt["stdout"] = retained_stdout
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = len(raw_stdout.encode("utf-8"))
    capture["stdout_truncated"] = False
    _write_json(receipt_path, receipt)

    assert _build(five_cell_artifacts)["passed"] is True


def test_rejects_golden_truncated_excerpt_larger_than_claimed_source(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    receipt["stdout"] = "[truncated]..." + "x" * 2_000
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 1
    capture["stdout_truncated"] = False
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="capture|excerpt|source|byte|diagnostic"):
        _build(five_cell_artifacts)


def test_rejects_golden_short_literal_truncation_prefix(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    receipt["stdout"] = "[truncated]...x"
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 2
    capture["stdout_truncated"] = False
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="capture|excerpt|bound|diagnostic"):
        _build(five_cell_artifacts)


def test_accepts_golden_output_with_a_literal_truncation_prefix(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    retained_stdout = "[truncated]...x"
    receipt["stdout"] = retained_stdout
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = len(retained_stdout.encode("utf-8"))
    capture["stdout_truncated"] = False
    _write_json(receipt_path, receipt)

    assert _build(five_cell_artifacts)["passed"] is True


def test_rejects_golden_partial_redaction_marker_with_impossible_source_size(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        3,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    receipt["stdout"] = "[truncated]...edacted>" + "x" * 1_992
    capture = receipt["capture"]
    assert isinstance(capture, dict)
    capture["stdout_total_bytes"] = 1_991
    capture["stdout_truncated"] = False
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="capture|excerpt|source|byte|redact"):
        _build(five_cell_artifacts)


def test_rejects_golden_iteration_report_schema_drift(
    five_cell_artifacts: Path,
) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memvid",
        9,
        "iteration-receipt.json",
    )
    receipt = _read_json(receipt_path)
    receipt["report_schema"] = "kestrel.golden_eval_report.v999"
    _write_json(receipt_path, receipt)

    with pytest.raises(ValueError, match="iteration|receipt|report|schema"):
        _build(five_cell_artifacts)


def test_rejects_tampered_golden_report_digest(five_cell_artifacts: Path) -> None:
    golden_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memvid",
        13,
        "golden-report.json",
    )
    golden = _read_json(golden_path)
    results = golden["results"]
    assert isinstance(results, list)
    first = results[0]
    assert isinstance(first, dict)
    first["latency_ms"] = float(first["latency_ms"]) + 0.001
    _write_json(golden_path, golden)

    with pytest.raises(ValueError, match="golden|digest|sha256"):
        _build(five_cell_artifacts)


def test_rejects_validly_rebound_golden_outcome_signature_drift(
    five_cell_artifacts: Path,
) -> None:
    golden_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memvid",
        7,
        "golden-report.json",
    )
    golden = _read_json(golden_path)
    results = golden["results"]
    assert isinstance(results, list)
    first = results[0]
    assert isinstance(first, dict)
    first["context_chars"] = int(first["context_chars"]) + 1

    summary = _golden_summary(results, max_case_latency_ms=45000.0)
    acceptance = summary["acceptance"]
    assert isinstance(acceptance, dict)
    golden["summary"] = summary
    golden["acceptance"] = {
        "functional": {"required": True, "passed": True},
        "latency": copy.deepcopy(acceptance["latency"]),
        "cost": copy.deepcopy(acceptance["cost"]),
    }
    _write_json(golden_path, golden)
    _rebind_golden_report_digest(
        five_cell_artifacts,
        "memvid",
        7,
        outcome_signature=True,
    )

    with pytest.raises(ValueError, match="signature|flake|determin|projection"):
        _build(five_cell_artifacts)


@pytest.mark.parametrize(
    "forgery",
    ["false_promotion_count", "promotion_precision", "categories", "bool_fail_count"],
)
def test_rejects_semantically_forged_golden_summary(
    five_cell_artifacts: Path,
    forgery: str,
) -> None:
    golden_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        5,
        "golden-report.json",
    )
    golden = _read_json(golden_path)
    summary = golden["summary"]
    assert isinstance(summary, dict)
    if forgery == "false_promotion_count":
        summary["false_promotion_count"] = 999
    elif forgery == "promotion_precision":
        summary["promotion_precision"] = "forged"
    elif forgery == "categories":
        summary["categories"] = {"forged": {"score": 1.0}}
    else:
        summary["fail_count"] = False
    _write_json(golden_path, golden)
    _rebind_golden_report_digest(five_cell_artifacts, "memory", 5)

    with pytest.raises(ValueError, match="golden|summary|promotion|categor|count|boolean"):
        _build(five_cell_artifacts)


def test_rejects_unexpected_file_in_artifact_tree(five_cell_artifacts: Path) -> None:
    (five_cell_artifacts / _artifact_name("runtime-linux") / "untrusted.txt").write_text(
        "not qualification evidence\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="extra|unexpected|file"):
        _build(five_cell_artifacts)


def test_rejects_duplicate_json_object_key(five_cell_artifacts: Path) -> None:
    receipt_path = _determinism_repeat_file(
        five_cell_artifacts,
        "memory",
        2,
        "iteration-receipt.json",
    )
    raw = receipt_path.read_text(encoding="utf-8")
    needle = '"status": "completed"'
    assert raw.count(needle) == 1
    receipt_path.write_text(
        raw.replace(needle, f"{needle},\n  {needle}", 1),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate|malformed|JSON"):
        _build(five_cell_artifacts)


def test_rejects_case_mangled_evidence_layout(five_cell_artifacts: Path) -> None:
    original = _determinism_repeat_file(
        five_cell_artifacts,
        "memvid",
        4,
        "golden-report.json",
    )
    mangled = original.with_name("Golden-Report.json")
    original.rename(mangled)
    if original.exists():
        pytest.skip("filesystem does not distinguish evidence filename case")

    with pytest.raises(ValueError, match="layout|missing|unexpected"):
        _build(five_cell_artifacts)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink support")
def test_rejects_symbolic_link_in_artifact_tree(
    five_cell_artifacts: Path,
    tmp_path: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    outside = tmp_path / "outside-report.json"
    shutil.copy2(report_path, outside)
    report_path.unlink()
    try:
        report_path.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(ValueError, match="symbolic|symlink|unsafe"):
        _build(five_cell_artifacts)


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="platform has no symlink support")
def test_cli_rejects_symbolic_link_as_artifact_root(
    five_cell_artifacts: Path,
    tmp_path: Path,
) -> None:
    linked_root = tmp_path / "linked-artifacts"
    try:
        linked_root.symlink_to(five_cell_artifacts, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/aggregate_runtime_reliability_receipts.py",
            "build",
            "--artifact-root",
            str(linked_root),
            "--source-commit",
            SOURCE_COMMIT,
            "--workflow-run-id",
            str(WORKFLOW_RUN_ID),
            "--workflow-run-attempt",
            str(RUN_ATTEMPT),
            "--output",
            str(linked_root / "kestrel-runtime-reliability-qualification.json"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode != 0
    assert "symbolic" in completed.stderr or "symlink" in completed.stderr


def test_rejects_bool_disguised_as_runtime_deadline_state(
    five_cell_artifacts: Path,
) -> None:
    report_path = _runtime_report(five_cell_artifacts, "runtime-linux")
    receipt_path = _runtime_receipt(five_cell_artifacts, "runtime-linux", 1)
    report = _read_json(report_path)
    runs = report["runs"]
    assert isinstance(runs, list)
    embedded = runs[0]
    assert isinstance(embedded, dict)
    deadline = embedded["deadline"]
    assert isinstance(deadline, dict)
    deadline["exceeded"] = 0
    _write_json(report_path, report)
    _write_json(receipt_path, embedded)

    with pytest.raises(ValueError, match="deadline|receipt|run"):
        _build(five_cell_artifacts)


def test_rejects_rerun_attempt_two(five_cell_artifacts: Path) -> None:
    with pytest.raises(ValueError, match="attempt|rerun"):
        _build(five_cell_artifacts, run_attempt=2)


@pytest.mark.parametrize(
    "forgery",
    [
        "subject",
        "workflow",
        "required_cells",
        "cells",
        "totals",
        "diagnostics",
        "passed",
    ],
)
def test_verifier_rejects_forged_aggregate_field(
    five_cell_artifacts: Path,
    forgery: str,
) -> None:
    qualification = _build(five_cell_artifacts)
    forged = copy.deepcopy(qualification)
    if forgery == "subject":
        subject = forged["subject"]
        assert isinstance(subject, dict)
        subject["source_commit"] = OTHER_COMMIT
    elif forgery == "workflow":
        workflow = forged["workflow"]
        assert isinstance(workflow, dict)
        workflow["run_id"] = WORKFLOW_RUN_ID + 1
    elif forgery == "required_cells":
        required_cells = forged["required_cells"]
        assert isinstance(required_cells, list)
        required_cells.reverse()
    elif forgery == "cells":
        cells = forged["cells"]
        assert isinstance(cells, list)
        cell = cells[0]
        assert isinstance(cell, dict)
        cell["artifact_manifest_sha256"] = "0" * 64
    elif forgery == "totals":
        totals = forged["totals"]
        assert isinstance(totals, dict)
        totals["completed_repeats"] = 99
    elif forgery == "diagnostics":
        diagnostics = forged["diagnostics"]
        assert isinstance(diagnostics, dict)
        diagnostics["missing_cells"] = ["runtime-linux"]
    else:
        forged["passed"] = False

    with pytest.raises(
        ValueError,
        match="qualification|aggregate|total|digest|manifest|provenance",
    ):
        _verify(five_cell_artifacts, forged)


def test_memory_only_receipt_is_not_release_qualification(
    five_cell_artifacts: Path,
    tmp_path: Path,
) -> None:
    memory_only = tmp_path / "memory-only"
    memory_only.mkdir()
    memory_artifact = _artifact_name("determinism-memory")
    shutil.copytree(
        five_cell_artifacts / memory_artifact,
        memory_only / memory_artifact,
    )

    with pytest.raises(ValueError, match="five|missing|required cell"):
        _build(memory_only)
