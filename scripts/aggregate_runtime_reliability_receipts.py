#!/usr/bin/env python3
"""Build and verify Kestrel's exact-SHA five-cell reliability qualification."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.golden_eval_contract import (  # noqa: E402
    GOLDEN_CASE_CATEGORIES,
    GOLDEN_REPORT_SCHEMA,
    GoldenBackend,
    validate_golden_report,
)
from scripts.runtime_reliability_contract import (  # noqa: E402
    RUNTIME_RELIABILITY_ISOLATION,
    RUNTIME_RELIABILITY_ITERATION_TIMEOUT_SECONDS,
    RUNTIME_RELIABILITY_REQUIRED_REPEATS,
    RUNTIME_RELIABILITY_TESTS,
)

QUALIFICATION_SCHEMA = "kestrel.runtime_reliability_qualification.v1"
QUALIFICATION_REPORT_FILENAME = "kestrel-runtime-reliability-qualification.json"
DETERMINISM_REPORT_SCHEMA = "kestrel.determinism_eval_report.v3"
RUNTIME_REPORT_SCHEMA = "kestrel.runtime_reliability_report.v1"
REQUIRED_REPEATS = 20
GOLDEN_SEED = 1729
GOLDEN_CASE_TIMEOUT_SECONDS = 60.0
GOLDEN_ITERATION_TIMEOUT_SECONDS = 1500.0
GOLDEN_MAX_CASE_LATENCY_MS = 45000.0
MAX_JSON_BYTES = 4 * 1024 * 1024

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_PYTHON_311_RE = re.compile(r"^3\.11\.\d+$")

CellKind = Literal["runtime", "determinism"]


@dataclass(frozen=True)
class CellSpec:
    cell_id: str
    kind: CellKind
    qualifier: str
    expected_os: str
    expected_arch: str

    def artifact_name(self, source_commit: str) -> str:
        if self.kind == "runtime":
            return f"kestrel-runtime-reliability-{self.qualifier}-{source_commit}"
        return f"kestrel-determinism-{self.qualifier}-{source_commit}"

    @property
    def report_filename(self) -> str:
        if self.kind == "runtime":
            return "kestrel-runtime-reliability-report.json"
        return "kestrel-determinism-report.json"


CELL_SPECS = (
    CellSpec("runtime-linux", "runtime", "Linux", "Linux", "X64"),
    CellSpec("runtime-macos", "runtime", "macOS", "macOS", "ARM64"),
    CellSpec("runtime-windows", "runtime", "Windows", "Windows", "X64"),
    CellSpec("determinism-memory", "determinism", "memory", "Linux", "X64"),
    CellSpec("determinism-memvid", "determinism", "memvid", "Linux", "X64"),
)
REQUIRED_CELL_IDS = tuple(spec.cell_id for spec in CELL_SPECS)


@dataclass(frozen=True)
class JsonEvidence:
    relative_path: str
    value: dict[str, object]
    sha256: str
    canonical_json_sha256: str
    size_bytes: int

    def manifest_record(self) -> dict[str, object]:
        return {
            "path": self.relative_path,
            "sha256": self.sha256,
            "canonical_json_sha256": self.canonical_json_sha256,
            "size_bytes": self.size_bytes,
        }


def _canonical_json(value: object) -> bytes:
    try:
        rendered = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"value is not canonical JSON: {exc}") from exc
    return rendered.encode("ascii")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON object contains duplicate key {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"JSON contains non-finite number {value!r}")


def _load_json_file(path: Path, *, relative_path: str) -> JsonEvidence:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"qualification evidence is missing or unreadable: {relative_path}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"qualification evidence contains an unsafe symbolic link: {relative_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"qualification evidence is not a regular file: {relative_path}")
    if metadata.st_size > MAX_JSON_BYTES:
        raise ValueError(f"qualification JSON exceeds {MAX_JSON_BYTES} bytes: {relative_path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"qualification evidence is unreadable: {relative_path}") from exc
    if raw.startswith(b"\xef\xbb\xbf"):
        raise ValueError(f"qualification JSON must not contain a UTF-8 BOM: {relative_path}")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError(f"qualification JSON is not valid UTF-8: {relative_path}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"qualification JSON is malformed at {relative_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"qualification JSON must be an object: {relative_path}")
    canonical = _canonical_json(value)
    return JsonEvidence(
        relative_path=relative_path,
        value=cast(dict[str, object], value),
        sha256=hashlib.sha256(raw).hexdigest(),
        canonical_json_sha256=hashlib.sha256(canonical).hexdigest(),
        size_bytes=len(raw),
    )


def _exact_object(value: object, expected: set[str], *, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{label} fields are not accepted: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return cast(dict[str, object], value)


def _require_int(value: object, *, label: str, minimum: int = 0) -> int:
    if type(value) is not int or int(value) < minimum:
        raise ValueError(f"{label} must be an integer greater than or equal to {minimum}")
    return int(value)


def _require_number(value: object, *, label: str, minimum: float = 0.0) -> float:
    if type(value) not in {int, float} or not math.isfinite(float(value)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if result < minimum:
        raise ValueError(f"{label} must be greater than or equal to {minimum}")
    return result


def _require_digest(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _require_source_commit(value: object, *, label: str = "source commit") -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be a lowercase 40-character SHA")
    return value


def _strict_equal(left: object, right: object) -> bool:
    return hmac.compare_digest(_canonical_json(left), _canonical_json(right))


def _redaction_expansion_slack(visible: str, *, boundary_may_split_marker: bool) -> int:
    """Return the maximum bytes by which visible redaction markers may expand input."""

    marker = "<redacted>"
    marker_count = visible.count(marker)
    # Every current redaction replaces at least one source byte with this
    # ten-byte marker, so one marker can expand retained diagnostics by at most 9.
    slack = marker_count * (len(marker) - 1)
    if boundary_may_split_marker:
        for offset in range(1, len(marker)):
            suffix = marker[offset:]
            if visible.startswith(suffix):
                slack += len(suffix) - 1
                break
    return slack


def _validate_retained_diagnostic(
    retained: str,
    *,
    total_size: int,
    capture_truncated: bool,
    excerpt_limit: int,
    label: str,
) -> bool:
    """Validate producer-possible capture/excerpt sizes; return whether digest is visible."""

    prefix = "[truncated]..."
    starts_with_prefix = retained.startswith(prefix)
    excerpted = starts_with_prefix and len(retained) == len(prefix) + excerpt_limit
    if excerpted:
        visible = retained.removeprefix(prefix)
    else:
        if len(retained) > excerpt_limit:
            raise ValueError(f"{label} diagnostics do not have a producer-valid bound")
        visible = retained

    if capture_truncated:
        if total_size <= 262144:
            raise ValueError(f"{label} truncation state does not match its byte count")
        captured_source_size = 262144
    else:
        if total_size > 262144:
            raise ValueError(f"{label} byte count exceeds its capture limit")
        captured_source_size = total_size

    redaction_slack = _redaction_expansion_slack(
        visible,
        boundary_may_split_marker=excerpted,
    )
    replacement_slack = visible.count("\ufffd") * 2
    expansion_slack = redaction_slack + replacement_slack
    visible_size = len(visible.encode("utf-8"))

    if capture_truncated and not excerpted and redaction_slack == 0:
        raise ValueError(f"{label} truncated capture has no bounded excerpt or redaction")
    if excerpted:
        impossible = (
            captured_source_size + expansion_slack < visible_size
            if expansion_slack
            else captured_source_size <= visible_size
        )
    elif redaction_slack:
        impossible = captured_source_size + expansion_slack < visible_size
    elif replacement_slack:
        impossible = (
            captured_source_size + replacement_slack < visible_size
            or captured_source_size > visible_size
        )
    else:
        impossible = captured_source_size != visible_size
    if impossible:
        raise ValueError(f"{label} diagnostics are larger than their claimed source")

    return not excerpted and redaction_slack == 0


def _expected_cell_paths(spec: CellSpec) -> tuple[str, ...]:
    if spec.kind == "runtime":
        return (
            spec.report_filename,
            *(
                f"kestrel-runtime-reliability-runs/repeat-{repeat:02d}/iteration-receipt.json"
                for repeat in range(1, REQUIRED_REPEATS + 1)
            ),
        )
    return (
        spec.report_filename,
        *(
            path
            for repeat in range(1, REQUIRED_REPEATS + 1)
            for path in (
                f"kestrel-determinism-runs/repeat-{repeat:02d}/golden-report.json",
                f"kestrel-determinism-runs/repeat-{repeat:02d}/iteration-receipt.json",
            )
        ),
    )


def _walk_regular_files(root: Path, *, label: str) -> tuple[str, ...]:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ValueError(f"missing required cell artifact: {label}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"qualification evidence contains an unsafe symbolic link: {label}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"required cell artifact is not a directory: {label}")

    files: list[str] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in tuple(dirnames):
            child = directory_path / name
            child_metadata = child.lstat()
            relative = child.relative_to(root).as_posix()
            if stat.S_ISLNK(child_metadata.st_mode):
                raise ValueError(
                    f"qualification evidence contains an unsafe symbolic link: {label}/{relative}"
                )
            if not stat.S_ISDIR(child_metadata.st_mode):
                raise ValueError(
                    f"qualification evidence contains an unsafe directory entry: {label}/{relative}"
                )
        for name in filenames:
            child = directory_path / name
            child_metadata = child.lstat()
            relative = child.relative_to(root).as_posix()
            if stat.S_ISLNK(child_metadata.st_mode):
                raise ValueError(
                    f"qualification evidence contains an unsafe symbolic link: {label}/{relative}"
                )
            if not stat.S_ISREG(child_metadata.st_mode):
                raise ValueError(
                    f"qualification evidence contains a non-regular file: {label}/{relative}"
                )
            files.append(relative)
    return tuple(sorted(files))


def _load_cell(root: Path, spec: CellSpec, source_commit: str) -> dict[str, JsonEvidence]:
    artifact_name = spec.artifact_name(source_commit)
    cell_root = root / artifact_name
    expected = set(_expected_cell_paths(spec))
    actual = set(_walk_regular_files(cell_root, label=artifact_name))
    if actual != expected:
        raise ValueError(
            f"{spec.cell_id} artifact file layout has missing or unexpected files: "
            f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
        )
    return {
        relative: _load_json_file(
            cell_root / relative,
            relative_path=f"{artifact_name}/{relative}",
        )
        for relative in sorted(expected)
    }


def _validate_identity(value: object, *, source_commit: str, label: str) -> None:
    identity = _exact_object(
        value,
        {"expected_source_commit", "observed_head", "clean", "passed"},
        label=label,
    )
    expected = {
        "expected_source_commit": source_commit,
        "observed_head": source_commit,
        "clean": True,
        "passed": True,
    }
    if not _strict_equal(identity, expected):
        raise ValueError(f"{label} does not prove a clean exact-SHA workspace")


def _validate_runtime_test_evidence(value: object, *, label: str) -> None:
    evidence = _exact_object(
        value,
        {
            "schema",
            "format",
            "source",
            "expected_tests",
            "observed",
            "summary",
            "status",
            "passed",
            "raw_source_retained",
        },
        label=label,
    )
    source = _exact_object(
        evidence["source"],
        {"path", "sha256", "size_bytes"},
        label=f"{label} source",
    )
    _require_digest(source["sha256"], label=f"{label} source digest")
    _require_int(source["size_bytes"], label=f"{label} source size", minimum=1)
    expected_observed = [
        {"nodeid": nodeid, "outcome": "passed"} for nodeid in RUNTIME_RELIABILITY_TESTS
    ]
    expected_summary = {
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
    }
    if (
        evidence["schema"] != "kestrel.pytest_evidence.v1"
        or evidence["format"] != "junit_xml"
        or source["path"] != "pytest-results.xml"
        or evidence["expected_tests"] != list(RUNTIME_RELIABILITY_TESTS)
        or not _strict_equal(evidence["observed"], expected_observed)
        or not _strict_equal(evidence["summary"], expected_summary)
        or evidence["status"] != "verified"
        or evidence["passed"] is not True
        or evidence["raw_source_retained"] is not False
    ):
        raise ValueError(f"{label} does not prove the declared passing runtime tests")


def _validate_runtime_run(
    value: object,
    *,
    source_commit: str,
    repeat: int,
    runner_os: str,
) -> None:
    run = _exact_object(
        value,
        {
            "schema",
            "subject",
            "repeat",
            "status",
            "derived_passed",
            "runner_exit_code",
            "elapsed_seconds",
            "deadline",
            "cleanup",
            "capture",
            "stdout",
            "stderr",
            "test_evidence",
            "workspace_identity",
        },
        label=f"runtime repeat {repeat}",
    )
    subject = _exact_object(run["subject"], {"source_commit"}, label="runtime run subject")
    deadline = _exact_object(run["deadline"], {"clock", "exceeded"}, label="runtime deadline")
    cleanup = _exact_object(
        run["cleanup"], {"attempted", "succeeded", "method"}, label="runtime cleanup"
    )
    capture = _exact_object(
        run["capture"],
        {
            "limit_bytes",
            "stdout_total_bytes",
            "stderr_total_bytes",
            "stdout_truncated",
            "stderr_truncated",
            "stdout_sha256",
            "stderr_sha256",
        },
        label="runtime capture",
    )
    workspace = _exact_object(
        run["workspace_identity"], {"before", "after"}, label="runtime workspace identity"
    )
    _validate_identity(workspace["before"], source_commit=source_commit, label="before identity")
    _validate_identity(workspace["after"], source_commit=source_commit, label="after identity")
    _validate_runtime_test_evidence(run["test_evidence"], label=f"runtime repeat {repeat} JUnit")

    if (
        run["schema"] != "kestrel.runtime_reliability_iteration.v1"
        or subject["source_commit"] != source_commit
        or type(run["repeat"]) is not int
        or run["repeat"] != repeat
        or run["status"] != "completed"
        or run["derived_passed"] is not True
        or type(run["runner_exit_code"]) is not int
        or run["runner_exit_code"] != 0
        or not _strict_equal(deadline, {"clock": "monotonic", "exceeded": False})
    ):
        raise ValueError(f"runtime repeat {repeat} receipt is not an exact passing run")
    _require_number(run["elapsed_seconds"], label=f"runtime repeat {repeat} elapsed")

    expected_cleanup = (
        {"attempted": True, "succeeded": True, "method": "windows_job_object_quiesced"}
        if runner_os == "Windows"
        else {"attempted": False, "succeeded": True, "method": None}
    )
    if not _strict_equal(cleanup, expected_cleanup):
        raise ValueError(f"runtime repeat {repeat} cleanup is not release-qualified")
    if (
        type(capture["limit_bytes"]) is not int
        or capture["limit_bytes"] != 262144
        or type(capture["stdout_truncated"]) is not bool
        or type(capture["stderr_truncated"]) is not bool
        or not isinstance(run["stdout"], str)
        or not isinstance(run["stderr"], str)
    ):
        raise ValueError(f"runtime repeat {repeat} capture is incomplete")
    stdout = cast(str, run["stdout"])
    stderr = cast(str, run["stderr"])
    stdout_size = _require_int(
        capture["stdout_total_bytes"], label="runtime stdout byte count"
    )
    stderr_size = _require_int(
        capture["stderr_total_bytes"], label="runtime stderr byte count"
    )
    stdout_digest = _require_digest(capture["stdout_sha256"], label="runtime stdout digest")
    stderr_digest = _require_digest(capture["stderr_sha256"], label="runtime stderr digest")
    for stream, retained, total_size, capture_truncated, digest in (
        (
            "stdout",
            stdout,
            stdout_size,
            cast(bool, capture["stdout_truncated"]),
            stdout_digest,
        ),
        (
            "stderr",
            stderr,
            stderr_size,
            cast(bool, capture["stderr_truncated"]),
            stderr_digest,
        ),
    ):
        retained_bytes = retained.encode("utf-8")
        digest_is_visible = _validate_retained_diagnostic(
            retained,
            total_size=total_size,
            capture_truncated=capture_truncated,
            excerpt_limit=4_000,
            label=f"runtime repeat {repeat} {stream}",
        )
        if digest_is_visible and digest != hashlib.sha256(retained_bytes).hexdigest():
            raise ValueError(
                f"runtime repeat {repeat} {stream} digest does not match diagnostics"
            )


def _validate_runtime_cell(
    files: Mapping[str, JsonEvidence],
    *,
    spec: CellSpec,
    source_commit: str,
) -> tuple[dict[str, object], dict[str, object]]:
    report_evidence = files[spec.report_filename]
    report = _exact_object(
        report_evidence.value,
        {"schema", "subject", "configuration", "environment", "workspace_identity", "summary", "runs"},
        label=f"{spec.cell_id} report",
    )
    subject = _exact_object(report["subject"], {"source_commit"}, label="runtime report subject")
    configuration = _exact_object(
        report["configuration"],
        {"required_repeats", "tests", "subprocess_isolation", "iteration_timeout_seconds"},
        label="runtime report configuration",
    )
    environment = _exact_object(
        report["environment"],
        {"runner_os", "runner_arch", "python_version"},
        label="runtime report environment",
    )
    workspace = _exact_object(
        report["workspace_identity"], {"preflight", "final"}, label="runtime report workspace"
    )
    _validate_identity(workspace["preflight"], source_commit=source_commit, label="preflight identity")
    _validate_identity(workspace["final"], source_commit=source_commit, label="final identity")
    if report["schema"] != RUNTIME_REPORT_SCHEMA:
        raise ValueError(f"{spec.cell_id} source receipt schema is not accepted")
    if subject["source_commit"] != source_commit:
        raise ValueError(f"{spec.cell_id} source commit does not match qualification SHA")
    expected_configuration = {
        "required_repeats": RUNTIME_RELIABILITY_REQUIRED_REPEATS,
        "tests": list(RUNTIME_RELIABILITY_TESTS),
        "subprocess_isolation": RUNTIME_RELIABILITY_ISOLATION,
        "iteration_timeout_seconds": RUNTIME_RELIABILITY_ITERATION_TIMEOUT_SECONDS,
    }
    if not _strict_equal(configuration, expected_configuration):
        raise ValueError(f"{spec.cell_id} runtime configuration is not release-qualified")
    if (
        environment["runner_os"] != spec.expected_os
        or environment["runner_arch"] != spec.expected_arch
        or not isinstance(environment["python_version"], str)
        or _PYTHON_311_RE.fullmatch(cast(str, environment["python_version"])) is None
    ):
        raise ValueError(f"{spec.cell_id} runner platform or Python environment is not accepted")
    runs = report["runs"]
    if not isinstance(runs, list) or len(runs) != REQUIRED_REPEATS:
        raise ValueError(f"{spec.cell_id} runtime receipt must contain 20 ordered repeats")
    for repeat, embedded in enumerate(runs, start=1):
        relative = (
            f"kestrel-runtime-reliability-runs/repeat-{repeat:02d}/iteration-receipt.json"
        )
        external = files[relative].value
        if not _strict_equal(embedded, external):
            raise ValueError(
                f"{spec.cell_id} external receipt does not match embedded run {repeat}"
            )
        _validate_runtime_run(
            embedded,
            source_commit=source_commit,
            repeat=repeat,
            runner_os=spec.expected_os,
        )
    expected_summary = {
        "passed": True,
        "completed_repeats": REQUIRED_REPEATS,
        "required_repeats": REQUIRED_REPEATS,
        "consecutive_passes": REQUIRED_REPEATS,
        "failure_count": 0,
        "first_failure": None,
    }
    if not _strict_equal(report["summary"], expected_summary):
        raise ValueError(f"{spec.cell_id} runtime summary does not match its runs")
    derived = {
        "required_repeats": REQUIRED_REPEATS,
        "completed_repeats": REQUIRED_REPEATS,
        "passed_repeats": REQUIRED_REPEATS,
        "execution_count": REQUIRED_REPEATS * len(RUNTIME_RELIABILITY_TESTS),
        "failure_count": 0,
        "observed_flake_count": 0,
        "cleanup_failure_count": 0,
        "passed": True,
    }
    return dict(environment), derived


def _deterministic_projection(
    report: dict[str, object], *, backend: GoldenBackend
) -> dict[str, object]:
    validate_golden_report(report, expected_backend=backend, expected_seed=GOLDEN_SEED)
    raw_results = report["results"]
    assert isinstance(raw_results, list)
    cases: list[dict[str, object]] = []
    for raw_result in raw_results:
        assert isinstance(raw_result, dict)
        cases.append(
            {
                "name": str(raw_result["name"]),
                "category": str(raw_result["category"]),
                "outcome": {
                    str(key): value
                    for key, value in raw_result.items()
                    if key not in {"name", "category", "latency_ms"}
                },
            }
        )
    cases.sort(key=lambda item: (str(item["name"]), str(item["category"])))
    configuration = cast(dict[str, object], report["configuration"])
    return {
        "schema": report["schema"],
        "configuration": {
            key: configuration.get(key) for key in ("backend", "provider", "model", "seed")
        },
        "cases": cases,
    }


def _derived_golden_summary(
    results: list[dict[str, object]], *, max_case_latency_ms: float
) -> dict[str, object]:
    pass_count = sum(item["passed"] is True for item in results)
    fail_count = len(results) - pass_count
    latencies = [float(item["latency_ms"]) for item in results]
    context_sizes = [int(item["context_chars"]) for item in results]
    tool_counts = [int(item["tool_count"]) for item in results]
    measured_costs = [
        float(item["cost_estimate_usd"])
        for item in results
        if item["cost_estimate_usd"] is not None
    ]
    measured_count = len(measured_costs)
    cost_status = (
        "unmeasured"
        if measured_count == 0
        else "measured"
        if measured_count == len(results)
        else "partially_measured"
    )
    cost_total = round(sum(measured_costs), 6) if measured_costs else None
    cost = {
        "measurement_status": cost_status,
        "gate_configured": False,
        "required": False,
        "measured_case_count": measured_count,
        "unmeasured_case_count": len(results) - measured_count,
        "cost_estimate_usd_total": cost_total,
        "passed": None,
        "residual": (
            "Provider usage and pricing were not supplied for every golden case; "
            "cost is not an acceptance gate."
            if cost_status != "measured"
            else "Cost is measured but no budget threshold is configured."
        ),
    }
    latency_max = max(latencies)
    latency_pass_count = sum(
        latency <= max_case_latency_ms for latency in latencies
    )
    latency = {
        "measurement_status": "measured",
        "gate_configured": True,
        "required": True,
        "threshold_max_case_latency_ms": max_case_latency_ms,
        "latency_ms_max": latency_max,
        "passed": latency_max <= max_case_latency_ms,
    }
    categories: dict[str, dict[str, object]] = {}
    for category in dict.fromkeys(GOLDEN_CASE_CATEGORIES.values()):
        matching = [item for item in results if item["category"] == category]
        category_passes = sum(item["passed"] is True for item in matching)
        categories[category] = {
            "case_count": len(matching),
            "pass_count": category_passes,
            "fail_count": len(matching) - category_passes,
            "score": (
                None
                if not matching
                else round(category_passes / len(matching), 4)
            ),
        }
    categories["latency"] = {
        "case_count": len(results),
        "pass_count": latency_pass_count,
        "fail_count": len(results) - latency_pass_count,
        "score": round(latency_pass_count / len(results), 4),
        **latency,
    }
    categories["cost"] = {
        "case_count": len(results),
        "pass_count": None,
        "fail_count": None,
        "score": None,
        **cost,
    }
    return {
        "pass_count": pass_count,
        "fail_count": fail_count,
        "latency_ms_max": latency_max,
        "context_chars_max": max(context_sizes),
        "tool_count_total": sum(tool_counts),
        "cost_estimate_usd_total": cost_total,
        "categories": categories,
        "acceptance": {"latency": latency, "cost": cost},
        "promotion_precision": None,
        "false_promotion_count": sum(
            item["name"] == "no_success_claim_without_evidence"
            and item["passed"] is not True
            for item in results
        ),
    }


def _validate_determinism_iteration(
    value: object, *, backend: GoldenBackend, repeat: int
) -> None:
    receipt = _exact_object(
        value,
        {
            "schema",
            "backend",
            "repeat",
            "seed",
            "status",
            "runner_exit_code",
            "elapsed_seconds",
            "deadline",
            "cleanup",
            "capture",
            "report_schema",
            "derived_passed",
            "error",
            "stdout",
            "stderr",
        },
        label=f"{backend} determinism iteration {repeat}",
    )
    deadline = _exact_object(
        receipt["deadline"], {"clock", "seconds", "exceeded"}, label="golden deadline"
    )
    cleanup = _exact_object(
        receipt["cleanup"], {"attempted", "succeeded", "method"}, label="golden cleanup"
    )
    capture = _exact_object(
        receipt["capture"],
        {
            "limit_bytes_per_stream",
            "stdout_total_bytes",
            "stdout_truncated",
            "stderr_total_bytes",
            "stderr_truncated",
        },
        label="golden capture",
    )
    if (
        receipt["schema"] != "kestrel.determinism_iteration_receipt.v1"
        or receipt["backend"] != backend
        or type(receipt["repeat"]) is not int
        or receipt["repeat"] != repeat
        or type(receipt["seed"]) is not int
        or receipt["seed"] != GOLDEN_SEED
        or receipt["status"] != "completed"
        or type(receipt["runner_exit_code"]) is not int
        or receipt["runner_exit_code"] != 0
        or receipt["report_schema"] != GOLDEN_REPORT_SCHEMA
        or receipt["derived_passed"] is not True
        or receipt["error"] is not None
        or not isinstance(receipt["stdout"], str)
        or not isinstance(receipt["stderr"], str)
    ):
        raise ValueError(f"{backend} determinism iteration receipt {repeat} is not passing")
    _require_number(receipt["elapsed_seconds"], label="golden iteration elapsed")
    expected_deadline = {
        "clock": "monotonic",
        "seconds": GOLDEN_ITERATION_TIMEOUT_SECONDS,
        "exceeded": False,
    }
    if not _strict_equal(deadline, expected_deadline):
        raise ValueError(f"{backend} determinism iteration {repeat} deadline is not accepted")
    if not _strict_equal(
        cleanup,
        {"attempted": False, "succeeded": True, "method": None},
    ):
        raise ValueError(f"{backend} determinism iteration {repeat} cleanup failed")
    if (
        type(capture["limit_bytes_per_stream"]) is not int
        or capture["limit_bytes_per_stream"] != 262144
        or type(capture["stdout_truncated"]) is not bool
        or type(capture["stderr_truncated"]) is not bool
    ):
        raise ValueError(f"{backend} determinism iteration {repeat} capture is incomplete")
    stdout_size = _require_int(
        capture["stdout_total_bytes"], label="golden stdout byte count"
    )
    stderr_size = _require_int(
        capture["stderr_total_bytes"], label="golden stderr byte count"
    )
    for stream, retained, total_size, capture_truncated in (
        (
            "stdout",
            cast(str, receipt["stdout"]),
            stdout_size,
            cast(bool, capture["stdout_truncated"]),
        ),
        (
            "stderr",
            cast(str, receipt["stderr"]),
            stderr_size,
            cast(bool, capture["stderr_truncated"]),
        ),
    ):
        _validate_retained_diagnostic(
            retained,
            total_size=total_size,
            capture_truncated=capture_truncated,
            excerpt_limit=2_000,
            label=f"{backend} determinism iteration {repeat} {stream}",
        )


def _validate_determinism_cell(
    files: Mapping[str, JsonEvidence],
    *,
    spec: CellSpec,
    source_commit: str,
) -> tuple[dict[str, object], dict[str, object]]:
    backend = cast(GoldenBackend, spec.qualifier)
    report_evidence = files[spec.report_filename]
    report = _exact_object(
        report_evidence.value,
        {
            "schema",
            "subject",
            "configuration",
            "environment",
            "summary",
            "differing_cases",
            "reference_projection",
            "runs",
            "passed",
        },
        label=f"{spec.cell_id} report",
    )
    subject = _exact_object(report["subject"], {"source_commit"}, label="golden report subject")
    configuration = _exact_object(
        report["configuration"],
        {
            "backend",
            "provider",
            "model",
            "seed",
            "required_repeats",
            "comparison",
            "case_timeout_seconds",
            "iteration_timeout_seconds",
            "max_case_latency_ms",
        },
        label="golden determinism configuration",
    )
    environment = _exact_object(
        report["environment"], {"os", "architecture", "python_version"}, label="golden environment"
    )
    if report["schema"] != DETERMINISM_REPORT_SCHEMA:
        raise ValueError(f"{spec.cell_id} source receipt schema is not accepted")
    if subject["source_commit"] != source_commit:
        raise ValueError(f"{spec.cell_id} source commit does not match qualification SHA")
    expected_configuration = {
        "backend": backend,
        "provider": "mock",
        "model": "mock",
        "seed": GOLDEN_SEED,
        "required_repeats": REQUIRED_REPEATS,
        "comparison": "functional_outcomes_excluding_wall_clock_latency",
        "case_timeout_seconds": GOLDEN_CASE_TIMEOUT_SECONDS,
        "iteration_timeout_seconds": GOLDEN_ITERATION_TIMEOUT_SECONDS,
        "max_case_latency_ms": GOLDEN_MAX_CASE_LATENCY_MS,
    }
    if not _strict_equal(configuration, expected_configuration):
        raise ValueError(f"{spec.cell_id} backend or determinism configuration is not accepted")
    if (
        environment["os"] != spec.expected_os
        or environment["architecture"] != spec.expected_arch
        or not isinstance(environment["python_version"], str)
        or _PYTHON_311_RE.fullmatch(cast(str, environment["python_version"])) is None
    ):
        raise ValueError(f"{spec.cell_id} runner platform or Python environment is not accepted")
    runs = report["runs"]
    if not isinstance(runs, list) or len(runs) != REQUIRED_REPEATS:
        raise ValueError(f"{spec.cell_id} determinism receipt must contain 20 ordered repeats")

    recomputed_passes: list[bool] = []
    projections: list[dict[str, object]] = []
    signatures: list[str] = []
    for repeat, run_value in enumerate(runs, start=1):
        run = _exact_object(
            run_value,
            {"repeat", "passed", "outcome_signature", "golden_report_sha256", "failed_cases"},
            label=f"{backend} determinism run {repeat}",
        )
        if (
            type(run["repeat"]) is not int
            or run["repeat"] != repeat
            or run["passed"] is not True
            or run["failed_cases"] != []
        ):
            raise ValueError(f"{backend} determinism run {repeat} is incomplete or failing")
        _require_digest(run["outcome_signature"], label="golden outcome signature")
        _require_digest(run["golden_report_sha256"], label="golden report digest")
        prefix = f"kestrel-determinism-runs/repeat-{repeat:02d}"
        _validate_determinism_iteration(
            files[f"{prefix}/iteration-receipt.json"].value,
            backend=backend,
            repeat=repeat,
        )
        golden_report = files[f"{prefix}/golden-report.json"].value
        golden_digest = hashlib.sha256(_canonical_json(golden_report)).hexdigest()
        if golden_digest != run["golden_report_sha256"]:
            raise ValueError(f"{backend} golden report digest does not match repeat {repeat}")
        golden_configuration = _exact_object(
            golden_report.get("configuration"),
            {"backend", "provider", "model", "seed", "max_case_latency_ms"},
            label=f"{backend} golden report configuration {repeat}",
        )
        if golden_configuration["max_case_latency_ms"] != GOLDEN_MAX_CASE_LATENCY_MS:
            raise ValueError(f"{backend} golden report latency gate does not match repeat {repeat}")
        try:
            passed = validate_golden_report(
                golden_report,
                expected_backend=backend,
                expected_seed=GOLDEN_SEED,
            )
        except ValueError as exc:
            raise ValueError(f"{backend} golden report contract is invalid at repeat {repeat}: {exc}") from exc
        if passed is not True:
            raise ValueError(f"{backend} golden report acceptance failed at repeat {repeat}")
        raw_results = golden_report["results"]
        assert isinstance(raw_results, list)
        typed_results = [
            cast(dict[str, object], result) for result in raw_results
        ]
        expected_golden_summary = _derived_golden_summary(
            typed_results,
            max_case_latency_ms=GOLDEN_MAX_CASE_LATENCY_MS,
        )
        if not _strict_equal(golden_report["summary"], expected_golden_summary):
            raise ValueError(
                f"{backend} golden report summary is not derived at repeat {repeat}"
            )
        expected_acceptance = {
            "functional": {"required": True, "passed": True},
            **cast(dict[str, object], expected_golden_summary["acceptance"]),
        }
        if not _strict_equal(golden_report["acceptance"], expected_acceptance):
            raise ValueError(
                f"{backend} golden report acceptance is not derived at repeat {repeat}"
            )
        projection = _deterministic_projection(golden_report, backend=backend)
        signature = hashlib.sha256(_canonical_json(projection)).hexdigest()
        if signature != run["outcome_signature"]:
            raise ValueError(f"{backend} golden report projection does not match repeat {repeat}")
        recomputed_passes.append(passed)
        projections.append(projection)
        signatures.append(signature)

    distinct_signatures = set(signatures)
    first_signature = signatures[0]
    streak = 0
    for signature in signatures:
        if signature != first_signature:
            break
        streak += 1
    signature_counts = Counter(signatures)
    modal_count = max(signature_counts.values())
    flake_count = len(signatures) - modal_count
    expected_summary = {
        "completed_repeats": REQUIRED_REPEATS,
        "required_repeats": REQUIRED_REPEATS,
        "all_runs_passed": all(recomputed_passes),
        "unique_outcome_signatures": len(distinct_signatures),
        "determinism_streak": streak,
        "observed_flake_count": flake_count,
        "observed_flake_rate": round(flake_count / REQUIRED_REPEATS, 6),
    }
    if len(distinct_signatures) != 1:
        raise ValueError(f"{backend} determinism receipt contains observed flakes")
    if report["differing_cases"] != []:
        raise ValueError(f"{backend} determinism receipt reports differing cases")
    if not _strict_equal(report["reference_projection"], projections[0]):
        raise ValueError(f"{backend} determinism reference projection does not match evidence")
    if not _strict_equal(report["summary"], expected_summary):
        raise ValueError(f"{backend} determinism summary does not match raw reports")
    if report["passed"] is not True:
        raise ValueError(f"{backend} determinism receipt is not passing")
    derived = {
        "required_repeats": REQUIRED_REPEATS,
        "completed_repeats": REQUIRED_REPEATS,
        "passed_repeats": REQUIRED_REPEATS,
        "execution_count": REQUIRED_REPEATS * len(GOLDEN_CASE_CATEGORIES),
        "failure_count": 0,
        "observed_flake_count": flake_count,
        "cleanup_failure_count": 0,
        "passed": True,
    }
    return dict(environment), derived


def _validate_root_layout(root: Path, *, source_commit: str) -> None:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise ValueError(f"qualification artifact root is missing: {root}") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError("qualification artifact root must not be a symbolic link")
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("qualification artifact root must be a real directory")
    expected_directories = {spec.artifact_name(source_commit) for spec in CELL_SPECS}
    actual_directories: set[str] = set()
    actual_files: set[str] = set()
    for entry in os.scandir(root):
        entry_metadata = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_metadata.st_mode):
            raise ValueError(f"qualification artifact root contains unsafe symlink {entry.name!r}")
        if stat.S_ISDIR(entry_metadata.st_mode):
            actual_directories.add(entry.name)
        elif stat.S_ISREG(entry_metadata.st_mode):
            actual_files.add(entry.name)
        else:
            raise ValueError(f"qualification artifact root contains unsafe entry {entry.name!r}")
    missing = expected_directories - actual_directories
    extra_directories = actual_directories - expected_directories
    extra_files = actual_files - {QUALIFICATION_REPORT_FILENAME}
    if missing:
        raise ValueError(f"missing required five-cell artifacts: {sorted(missing)}")
    if extra_directories or extra_files:
        raise ValueError(
            "qualification artifact root contains unexpected cells or files: "
            f"directories={sorted(extra_directories)}, files={sorted(extra_files)}"
        )


def _validate_inputs(
    *, source_commit: str, workflow_run_id: int, run_attempt: int
) -> None:
    _require_source_commit(source_commit)
    _require_int(workflow_run_id, label="workflow run id", minimum=1)
    _require_int(run_attempt, label="workflow run attempt", minimum=1)
    if run_attempt != 1:
        raise ValueError("qualification evidence must come from workflow attempt 1; reruns are rejected")


def build_qualification(
    artifact_root: Path,
    *,
    source_commit: str,
    workflow_run_id: int,
    run_attempt: int,
) -> dict[str, object]:
    """Independently derive one passing five-cell qualification from raw artifacts."""

    _validate_inputs(
        source_commit=source_commit,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
    )
    root = Path(artifact_root)
    _validate_root_layout(root, source_commit=source_commit)
    cells: list[dict[str, object]] = []
    linux_python_versions: list[str] = []
    for spec in CELL_SPECS:
        files = _load_cell(root, spec, source_commit)
        if spec.kind == "runtime":
            environment, derived = _validate_runtime_cell(
                files,
                spec=spec,
                source_commit=source_commit,
            )
        else:
            environment, derived = _validate_determinism_cell(
                files,
                spec=spec,
                source_commit=source_commit,
            )
        if spec.expected_os == "Linux":
            python_version = environment.get("python_version")
            assert isinstance(python_version, str)
            linux_python_versions.append(python_version)
        manifest = [files[path].manifest_record() for path in sorted(files)]
        manifest_digest = hashlib.sha256(_canonical_json(manifest)).hexdigest()
        report = files[spec.report_filename]
        cells.append(
            {
                "cell_id": spec.cell_id,
                "kind": spec.kind,
                "qualifier": spec.qualifier,
                "artifact_name": spec.artifact_name(source_commit),
                "source_schema": report.value["schema"],
                "environment": environment,
                "report_sha256": report.sha256,
                "report_canonical_json_sha256": report.canonical_json_sha256,
                "artifact_manifest_sha256": manifest_digest,
                "file_count": len(files),
                "total_size_bytes": sum(item.size_bytes for item in files.values()),
                "derived": derived,
            }
        )
    if len(set(linux_python_versions)) != 1:
        raise ValueError(
            "Linux runtime, memory, and Memvid cells must use the same Python patch version"
        )

    runtime_cells = [cell for cell in cells if cell["kind"] == "runtime"]
    golden_cells = [cell for cell in cells if cell["kind"] == "determinism"]
    totals = {
        "required_cells": len(CELL_SPECS),
        "passed_cells": sum(
            cast(dict[str, object], cell["derived"])["passed"] is True for cell in cells
        ),
        "required_repeats": len(CELL_SPECS) * REQUIRED_REPEATS,
        "completed_repeats": sum(
            cast(int, cast(dict[str, object], cell["derived"])["completed_repeats"])
            for cell in cells
        ),
        "runtime_repeats": len(runtime_cells) * REQUIRED_REPEATS,
        "golden_repeats": len(golden_cells) * REQUIRED_REPEATS,
        "runtime_test_executions": sum(
            cast(int, cast(dict[str, object], cell["derived"])["execution_count"])
            for cell in runtime_cells
        ),
        "golden_case_executions": sum(
            cast(int, cast(dict[str, object], cell["derived"])["execution_count"])
            for cell in golden_cells
        ),
        "failure_count": sum(
            cast(int, cast(dict[str, object], cell["derived"])["failure_count"])
            for cell in cells
        ),
        "observed_flake_count": sum(
            cast(int, cast(dict[str, object], cell["derived"])["observed_flake_count"])
            for cell in cells
        ),
        "cleanup_failure_count": sum(
            cast(int, cast(dict[str, object], cell["derived"])["cleanup_failure_count"])
            for cell in cells
        ),
    }
    expected_totals = {
        "required_cells": 5,
        "passed_cells": 5,
        "required_repeats": 100,
        "completed_repeats": 100,
        "runtime_repeats": 60,
        "golden_repeats": 40,
        "runtime_test_executions": 540,
        "golden_case_executions": 840,
        "failure_count": 0,
        "observed_flake_count": 0,
        "cleanup_failure_count": 0,
    }
    if not _strict_equal(totals, expected_totals):
        raise ValueError("five-cell qualification derived totals are not release-qualified")
    return {
        "schema": QUALIFICATION_SCHEMA,
        "subject": {"source_commit": source_commit},
        "workflow": {"run_id": workflow_run_id, "run_attempt": run_attempt},
        "required_cells": list(REQUIRED_CELL_IDS),
        "cells": cells,
        "totals": totals,
        "diagnostics": {
            "missing_cells": [],
            "stale_cells": [],
            "mismatched_cells": [],
            "duplicate_cells": [],
        },
        "passed": True,
    }


def verify_qualification(
    artifact_root: Path,
    qualification: Mapping[str, object],
    *,
    source_commit: str,
    workflow_run_id: int,
    run_attempt: int,
) -> dict[str, object]:
    """Rebuild a qualification from raw evidence and require an exact match."""

    supplied = _exact_object(
        dict(qualification),
        {
            "schema",
            "subject",
            "workflow",
            "required_cells",
            "cells",
            "totals",
            "diagnostics",
            "passed",
        },
        label="qualification report",
    )
    expected = build_qualification(
        artifact_root,
        source_commit=source_commit,
        workflow_run_id=workflow_run_id,
        run_attempt=run_attempt,
    )
    if not _strict_equal(supplied, expected):
        raise ValueError(
            "qualification aggregate does not match independently rebuilt totals, manifests, "
            "or provenance"
        )
    return expected


def _write_json(path: Path, value: object) -> None:
    rendered = json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
    )
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(rendered + "\n", encoding="utf-8")
    temporary.replace(path)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "verify"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--artifact-root", type=Path, required=True)
        subparser.add_argument("--source-commit", required=True)
        subparser.add_argument("--workflow-run-id", type=int, required=True)
        subparser.add_argument(
            "--workflow-run-attempt",
            "--run-attempt",
            dest="run_attempt",
            type=int,
            required=True,
        )
        if command == "build":
            subparser.add_argument("--output", type=Path, required=True)
        else:
            subparser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        root = Path(os.path.abspath(args.artifact_root))
        if args.command == "build":
            output = Path(os.path.abspath(args.output))
            if output.parent != root or output.name != QUALIFICATION_REPORT_FILENAME:
                raise ValueError(
                    f"qualification output must be {root / QUALIFICATION_REPORT_FILENAME}"
                )
            if output.exists() or output.is_symlink():
                raise ValueError(f"qualification output already exists: {output}")
            qualification = build_qualification(
                root,
                source_commit=args.source_commit,
                workflow_run_id=args.workflow_run_id,
                run_attempt=args.run_attempt,
            )
            _write_json(output, qualification)
            print(f"wrote five-cell qualification {output}")
            return 0
        report_path = Path(os.path.abspath(args.report))
        if report_path.parent != root or report_path.name != QUALIFICATION_REPORT_FILENAME:
            raise ValueError(
                f"qualification report must be {root / QUALIFICATION_REPORT_FILENAME}"
            )
        report = _load_json_file(
            report_path,
            relative_path=QUALIFICATION_REPORT_FILENAME,
        ).value
        verify_qualification(
            root,
            report,
            source_commit=args.source_commit,
            workflow_run_id=args.workflow_run_id,
            run_attempt=args.run_attempt,
        )
        print(
            "verified exact-SHA five-cell reliability qualification "
            f"from workflow run {args.workflow_run_id} attempt {args.run_attempt}"
        )
        return 0
    except (OSError, ValueError) as exc:
        print(f"qualification rejected: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
