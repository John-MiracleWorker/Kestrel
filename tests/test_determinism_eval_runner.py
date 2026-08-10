from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import scripts.bounded_process as bounded_process
from nested_memvid_agent.security_boundary import REDACTED
from scripts.bounded_process import run_bounded_process
from scripts.golden_eval_contract import validate_golden_report
from scripts.run_determinism_evals import (
    GoldenRunnerError,
    _deterministic_projection,
    _excerpt,
    _subprocess_invoker,
    build_determinism_report,
    run_determinism,
)
from scripts.run_determinism_evals import (
    _redact_json as redact_determinism_json,
)
from scripts.run_golden_evals import (
    _eval_identifier,
)
from scripts.run_golden_evals import (
    _redact_json as redact_golden_json,
)

TEST_CASES = {"alpha": "repo_regression", "beta": "repo_regression"}
SOURCE_COMMIT = "a" * 40
RUNNER_OS = "Linux"
RUNNER_ARCH = "X64"
PYTHON_VERSION = "3.11.13"


def _golden_report(
    *,
    first_passed: bool = True,
    result_order: tuple[str, ...] = ("beta", "alpha"),
    latency_offset: float = 0.0,
    max_case_latency_ms: float | None = None,
    backend: str = "memory",
) -> dict[str, object]:
    results = []
    for index, name in enumerate(result_order):
        passed = first_passed if name == "alpha" else True
        results.append(
            {
                "name": name,
                "category": "repo_regression",
                "passed": passed,
                "score": 1.0 if passed else 0.0,
                "latency_ms": latency_offset + index + 0.25,
                "memory_hits": 0,
                "context_chars": 3,
                "tool_count": 1,
                "cost_estimate_usd": None,
                "executed_tools": ["repo.map"],
            }
        )
    latency_ms_max = max(float(item["latency_ms"]) for item in results)
    latency_passed = (
        latency_ms_max <= max_case_latency_ms
        if max_case_latency_ms is not None
        else None
    )
    latency_acceptance = {
        "measurement_status": "measured",
        "gate_configured": max_case_latency_ms is not None,
        "required": max_case_latency_ms is not None,
        "threshold_max_case_latency_ms": max_case_latency_ms,
        "latency_ms_max": latency_ms_max,
        "passed": latency_passed,
    }
    functional_passed = all(bool(item["passed"]) for item in results)
    report_passed = functional_passed and (
        latency_passed is True if max_case_latency_ms is not None else True
    )
    return {
        "schema": "kestrel.golden_eval_report.v2",
        "configuration": {
            "backend": backend,
            "provider": "mock",
            "model": "mock",
            "seed": 1729,
            "max_case_latency_ms": max_case_latency_ms,
        },
        "results": results,
        "summary": {
            "pass_count": sum(bool(item["passed"]) for item in results),
            "fail_count": sum(not bool(item["passed"]) for item in results),
            "latency_ms_max": latency_ms_max,
            "context_chars_max": 3,
            "tool_count_total": len(results),
            "cost_estimate_usd_total": None,
            "categories": {},
            "acceptance": {
                "latency": dict(latency_acceptance),
                "cost": {
                    "measurement_status": "unmeasured",
                    "gate_configured": False,
                    "required": False,
                    "measured_case_count": 0,
                    "unmeasured_case_count": len(results),
                    "cost_estimate_usd_total": None,
                    "passed": None,
                    "residual": "fixture",
                },
            },
            "promotion_precision": None,
            "false_promotion_count": 0,
        },
        "acceptance": {
            "functional": {
                "required": True,
                "passed": functional_passed,
            },
            "latency": dict(latency_acceptance),
            "cost": {
                "measurement_status": "unmeasured",
                "gate_configured": False,
                "required": False,
                "measured_case_count": 0,
                "unmeasured_case_count": len(results),
                "cost_estimate_usd_total": None,
                "passed": None,
                "residual": "fixture",
            },
        },
        "passed": report_passed,
    }


def _one_case_report(
    *,
    name: str,
    memory_hits: int,
    context_chars: int,
    backend: str = "memory",
) -> dict[str, object]:
    report = _golden_report(
        result_order=("alpha",),
        backend=backend,
    )
    result = report["results"][0]  # type: ignore[index]
    assert isinstance(result, dict)
    result["name"] = name
    result["category"] = "memory_precision_recall"
    result["memory_hits"] = memory_hits
    result["context_chars"] = context_chars
    summary = report["summary"]
    assert isinstance(summary, dict)
    summary["context_chars_max"] = context_chars
    return report


def test_golden_report_validation_requires_exact_expected_backend() -> None:
    categories = {"alpha": "repo_regression"}
    report = _golden_report(result_order=("alpha",), backend="memory")

    assert (
        validate_golden_report(
            report,
            expected_backend="memory",
            expected_case_categories=categories,
            expected_seed=1729,
        )
        is True
    )
    with pytest.raises(ValueError, match="golden report backend mismatch"):
        validate_golden_report(
            report,
            expected_backend="memvid",
            expected_case_categories=categories,
            expected_seed=1729,
        )


def test_passed_retrieval_case_cannot_report_zero_retrieval_evidence() -> None:
    case = "summary_first_expand_raw_on_demand"
    report = _one_case_report(name=case, memory_hits=0, context_chars=0)

    with pytest.raises(ValueError, match="retrieval evidence"):
        validate_golden_report(
            report,
            expected_backend="memory",
            expected_case_categories={case: "memory_precision_recall"},
            expected_seed=1729,
        )


def test_correction_retrieval_requires_a_hit_but_not_earlier_turn_context() -> None:
    case = "remember_correction_across_sessions"
    categories = {case: "memory_precision_recall"}

    assert validate_golden_report(
        _one_case_report(name=case, memory_hits=1, context_chars=0),
        expected_backend="memory",
        expected_case_categories=categories,
        expected_seed=1729,
    )
    with pytest.raises(ValueError, match="retrieval evidence"):
        validate_golden_report(
            _one_case_report(name=case, memory_hits=0, context_chars=0),
            expected_backend="memory",
            expected_case_categories=categories,
            expected_seed=1729,
        )


def test_determinism_report_binds_backend_subject_and_environment() -> None:
    reports = [
        _golden_report(result_order=("alpha",), backend="memvid"),
        _golden_report(result_order=("alpha",), backend="memvid"),
    ]
    categories = {"alpha": "repo_regression"}

    report = build_determinism_report(
        reports,
        required_repeats=2,
        seed=1729,
        backend="memvid",
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=categories,
    )

    assert report["schema"] == "kestrel.determinism_eval_report.v3"
    assert report["subject"] == {"source_commit": SOURCE_COMMIT}
    assert report["configuration"]["backend"] == "memvid"
    assert report["environment"] == {
        "os": RUNNER_OS,
        "architecture": RUNNER_ARCH,
        "python_version": PYTHON_VERSION,
    }
    canonical = json.dumps(
        reports[0], sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode()
    assert report["runs"][0]["golden_report_sha256"] == hashlib.sha256(
        canonical
    ).hexdigest()

    substituted = _golden_report(result_order=("alpha",), backend="memory")
    with pytest.raises(ValueError, match="golden report backend mismatch"):
        build_determinism_report(
            [reports[0], substituted],
            required_repeats=2,
            seed=1729,
            backend="memvid",
            source_commit=SOURCE_COMMIT,
            runner_os=RUNNER_OS,
            runner_arch=RUNNER_ARCH,
            python_version=PYTHON_VERSION,
            expected_case_categories=categories,
        )


@pytest.mark.parametrize("backend", ["memory", "memvid"])
def test_subprocess_invoker_forwards_backend_to_isolated_golden_runner(
    tmp_path: Path,
    backend: str,
) -> None:
    runner = tmp_path / "fake_golden_runner.py"
    payload = json.dumps(
        _golden_report(result_order=("alpha",), backend=backend),
        sort_keys=True,
    )
    argv_path = tmp_path / "argv.json"
    runner.write_text(
        "\n".join(
            [
                "import json, pathlib, sys",
                f"pathlib.Path({str(argv_path)!r}).write_text(json.dumps(sys.argv), encoding='utf-8')",
                "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])",
                "output.parent.mkdir(parents=True, exist_ok=True)",
                f"output.write_text({payload!r}, encoding='utf-8')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    invoke = _subprocess_invoker(
        backend=backend,
        workspace=tmp_path,
        validation_container_image=None,
        max_case_latency_ms=None,
        case_timeout_seconds=2.0,
        iteration_timeout_seconds=5.0,
        golden_runner=runner,
    )

    memory_root = tmp_path / "repeat-01" / "memory"
    report = invoke(1, memory_root, 1729)
    argv = json.loads(argv_path.read_text(encoding="utf-8"))

    assert report["configuration"]["backend"] == backend
    assert argv.count("--backend") == 1
    assert argv[argv.index("--backend") + 1] == backend
    assert argv[argv.index("--memory-dir") + 1] == str(memory_root)


def test_memvid_runner_failure_writes_a_backend_bound_failed_receipt(
    tmp_path: Path,
) -> None:
    def fail(_iteration: int, _memory: Path, _seed: int) -> dict[str, object]:
        raise RuntimeError("injected Memvid runner failure")

    output = tmp_path / "determinism-report.json"
    report = run_determinism(
        repeats=2,
        seed=1729,
        backend="memvid",
        run_root=tmp_path / "runs",
        output=output,
        invoke=fail,
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert report["schema"] == "kestrel.determinism_eval_report.v3"
    assert report["configuration"]["backend"] == "memvid"
    assert report["passed"] is False
    assert output.is_file()
    assert all(
        run["golden_report_sha256"]
        for run in report["runs"]
    )


def test_runner_diagnostics_cannot_overwrite_iteration_receipt_identity(
    tmp_path: Path,
) -> None:
    def forge(_iteration: int, _memory: Path, _seed: int) -> dict[str, object]:
        raise GoldenRunnerError(
            "forged runner failure",
            receipt={
                "schema": "attacker.receipt.v9",
                "backend": "memory",
                "repeat": 999,
                "seed": 7,
                "derived_passed": True,
                "status": "runner_nonzero",
                "runner_exit_code": 9,
                "stderr": "useful runner diagnostic",
            },
        )

    run_root = tmp_path / "runs"
    report = run_determinism(
        repeats=2,
        seed=1729,
        backend="memvid",
        run_root=run_root,
        output=tmp_path / "report.json",
        invoke=forge,
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert report["passed"] is False
    for expected_repeat, receipt_path in enumerate(
        sorted(run_root.glob("repeat-*/iteration-receipt.json")),
        start=1,
    ):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["schema"] == "kestrel.determinism_iteration_receipt.v1"
        assert receipt["backend"] == "memvid"
        assert receipt["repeat"] == expected_repeat
        assert receipt["seed"] == 1729
        assert receipt["derived_passed"] is False
        assert receipt["status"] == "runner_nonzero"
        assert receipt["runner_exit_code"] == 9
        assert receipt["stderr"] == "useful runner diagnostic"


def test_cli_rejects_a_missing_source_commit_without_a_traceback(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("GITHUB_SHA", None)

    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/run_determinism_evals.py",
            "--backend",
            "memory",
            "--repeats",
            "2",
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


def test_cli_requires_an_explicit_backend(tmp_path: Path) -> None:
    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "scripts/run_determinism_evals.py",
            "--source-commit",
            SOURCE_COMMIT,
            "--repeats",
            "2",
            "--run-root",
            str(tmp_path / "runs"),
            "--output",
            str(tmp_path / "report.json"),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert "--backend" in completed.stderr
    assert not (tmp_path / "runs").exists()


def test_runner_rejects_unsupported_architecture_before_creating_run_root(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"

    with pytest.raises(ValueError, match="runner architecture"):
        run_determinism(
            repeats=2,
            seed=1729,
            backend="memory",
            run_root=run_root,
            output=tmp_path / "report.json",
            invoke=lambda _iteration, _memory, _seed: _golden_report(),
            source_commit=SOURCE_COMMIT,
            runner_os=RUNNER_OS,
            runner_arch="mips64",
            python_version=PYTHON_VERSION,
            expected_case_categories=TEST_CASES,
        )

    assert not run_root.exists()


def test_seeded_eval_identifier_is_stable_and_never_looks_like_a_release_id() -> None:
    assert _eval_identifier(1729) == _eval_identifier(1729)
    assert _eval_identifier(1729) != _eval_identifier(1730)
    assert _eval_identifier(1729).startswith("golden_seed_")


def test_projection_sorts_cases_and_excludes_wall_clock_measurements() -> None:
    first = _golden_report(result_order=("beta", "alpha"), latency_offset=0.0)
    second = _golden_report(result_order=("alpha", "beta"), latency_offset=9000.0)

    assert _deterministic_projection(
        first, expected_backend="memory", expected_case_categories=TEST_CASES
    ) == _deterministic_projection(
        second, expected_backend="memory", expected_case_categories=TEST_CASES
    )
    assert _deterministic_projection(
        first,
        expected_backend="memory",
        expected_case_categories=TEST_CASES,
    )["cases"] == [
        {
            "category": "repo_regression",
            "name": "alpha",
            "outcome": {
                "context_chars": 3,
                "cost_estimate_usd": None,
                "executed_tools": ["repo.map"],
                "memory_hits": 0,
                "passed": True,
                "score": 1.0,
                "tool_count": 1,
            },
        },
        {
            "category": "repo_regression",
            "name": "beta",
            "outcome": {
                "context_chars": 3,
                "cost_estimate_usd": None,
                "executed_tools": ["repo.map"],
                "memory_hits": 0,
                "passed": True,
                "score": 1.0,
                "tool_count": 1,
            },
        },
    ]


def test_determinism_report_fails_when_one_repeat_changes_outcome() -> None:
    reports = [_golden_report(), _golden_report(), _golden_report(first_passed=False)]

    report = build_determinism_report(
        reports,
        required_repeats=3,
        seed=1729,
        backend="memory",
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert report["passed"] is False
    assert report["summary"] == {
        "completed_repeats": 3,
        "required_repeats": 3,
        "all_runs_passed": False,
        "unique_outcome_signatures": 2,
        "determinism_streak": 2,
        "observed_flake_count": 1,
        "observed_flake_rate": 0.333333,
    }
    assert report["differing_cases"] == ["alpha"]
    assert report["runs"][0]["failed_cases"] == []
    assert report["runs"][2]["failed_cases"] == ["alpha"]
    assert report["reference_projection"]["cases"][0]["name"] == "alpha"


def test_determinism_report_binds_the_invoked_latency_gate() -> None:
    reports = [
        _golden_report(max_case_latency_ms=45_000.0),
        _golden_report(max_case_latency_ms=45_000.0),
    ]

    report = build_determinism_report(
        reports,
        required_repeats=2,
        seed=1729,
        backend="memory",
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        max_case_latency_ms=45_000.0,
        expected_case_categories=TEST_CASES,
    )

    assert report["passed"] is True
    assert report["configuration"]["max_case_latency_ms"] == 45_000.0


def test_determinism_report_rejects_a_missing_or_different_latency_gate() -> None:
    with pytest.raises(ValueError, match="latency gate"):
        build_determinism_report(
            [_golden_report(), _golden_report()],
            required_repeats=2,
            seed=1729,
            backend="memory",
            source_commit=SOURCE_COMMIT,
            runner_os=RUNNER_OS,
            runner_arch=RUNNER_ARCH,
            python_version=PYTHON_VERSION,
            max_case_latency_ms=45_000.0,
            expected_case_categories=TEST_CASES,
        )

    with pytest.raises(ValueError, match="latency gate"):
        build_determinism_report(
            [
                _golden_report(max_case_latency_ms=30_000.0),
                _golden_report(max_case_latency_ms=30_000.0),
            ],
            required_repeats=2,
            seed=1729,
            backend="memory",
            source_commit=SOURCE_COMMIT,
            runner_os=RUNNER_OS,
            runner_arch=RUNNER_ARCH,
            python_version=PYTHON_VERSION,
            max_case_latency_ms=45_000.0,
            expected_case_categories=TEST_CASES,
        )


def test_runner_isolates_each_repeat_and_writes_report_on_failure(tmp_path: Path) -> None:
    seen_roots: list[Path] = []

    def invoke(iteration: int, memory_root: Path, seed: int) -> dict[str, object]:
        assert seed == 1729
        assert iteration in {1, 2, 3}
        seen_roots.append(memory_root)
        return _golden_report(first_passed=iteration != 3)

    output = tmp_path / "reports" / "determinism.json"
    report = run_determinism(
        repeats=3,
        seed=1729,
        backend="memory",
        run_root=tmp_path / "runs",
        output=output,
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert report["passed"] is False
    assert seen_roots == [
        tmp_path / "runs" / "repeat-01" / "memory",
        tmp_path / "runs" / "repeat-02" / "memory",
        tmp_path / "runs" / "repeat-03" / "memory",
    ]
    assert output.is_file()
    assert '"observed_flake_count": 1' in output.read_text(encoding="utf-8")


def test_runner_rejects_seed_outside_python_hash_range_before_writing(
    tmp_path: Path,
) -> None:
    run_root = tmp_path / "runs"

    with pytest.raises(ValueError, match="seed must be between"):
        run_determinism(
            repeats=2,
            seed=-1,
            backend="memory",
            run_root=run_root,
            output=tmp_path / "report.json",
            invoke=lambda _iteration, _memory, _seed: _golden_report(),
            source_commit=SOURCE_COMMIT,
            runner_os=RUNNER_OS,
            runner_arch=RUNNER_ARCH,
            python_version=PYTHON_VERSION,
            expected_case_categories=TEST_CASES,
        )

    assert not run_root.exists()


def test_runner_redacts_iteration_errors_from_machine_report(tmp_path: Path) -> None:
    secret = "sk-proj-determinism-report-secret123"

    def fail(_iteration: int, _memory: Path, _seed: int) -> dict[str, object]:
        raise RuntimeError(f"OPENAI_API_KEY={secret}")

    output = tmp_path / "report.json"
    run_determinism(
        repeats=2,
        seed=1729,
        backend="memory",
        run_root=tmp_path / "runs",
        output=output,
        invoke=fail,
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert secret not in output.read_text(encoding="utf-8")


def test_runner_failure_receipt_preserves_required_latency_gate(tmp_path: Path) -> None:
    def fail(_iteration: int, _memory: Path, _seed: int) -> dict[str, object]:
        raise RuntimeError("injected runner failure")

    report = run_determinism(
        repeats=2,
        seed=1729,
        backend="memory",
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=fail,
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
        max_case_latency_ms=45_000.0,
    )

    assert report["passed"] is False
    assert report["configuration"]["max_case_latency_ms"] == 45_000.0


def test_runner_redacts_complete_imported_and_final_reports(tmp_path: Path) -> None:
    secret = "sk-proj-imported-report-secret123"

    def invoke(_iteration: int, _memory: Path, _seed: int) -> dict[str, object]:
        report = _golden_report()
        results = report["results"]
        assert isinstance(results, list)
        first = results[0]
        assert isinstance(first, dict)
        first["nested_provider_receipt"] = {
            "request": ["safe", {"authorization": f"Bearer {secret}"}],
            "diagnostic": f"OPENAI_API_KEY={secret}",
            f"secret-key-{secret}": "redact dictionary keys too",
        }
        return report

    output = tmp_path / "report.json"
    report = run_determinism(
        repeats=2,
        seed=1729,
        backend="memory",
        run_root=tmp_path / "runs",
        output=output,
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert report["passed"] is True
    assert secret not in json.dumps(report)
    assert secret not in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("redactor", [redact_determinism_json, redact_golden_json])
def test_report_redactors_apply_sensitive_key_rules_recursively(
    redactor: object,
) -> None:
    assert callable(redactor)
    payload = {
        "safe": [
            {
                "api_key": "opaque-value-that-has-no-secret-shape",
                "nested": {"client_secret": "another-opaque-value"},
            }
        ]
    }

    redacted = redactor(payload)

    serialized = json.dumps(redacted)
    assert "opaque-value-that-has-no-secret-shape" not in serialized
    assert "another-opaque-value" not in serialized
    assert redacted["safe"][0]["api_key"] == REDACTED  # type: ignore[index]
    assert redacted["safe"][0]["nested"]["client_secret"] == REDACTED  # type: ignore[index]


def test_forged_top_level_pass_cannot_override_failed_case() -> None:
    forged = _golden_report(first_passed=False)
    forged["passed"] = True

    report = build_determinism_report(
        [forged, forged],
        required_repeats=2,
        seed=1729,
        backend="memory",
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert report["passed"] is False
    assert report["summary"]["all_runs_passed"] is False


def test_inconsistent_top_level_failure_cannot_become_a_derived_pass() -> None:
    inconsistent = _golden_report()
    inconsistent["passed"] = False

    report = build_determinism_report(
        [inconsistent, inconsistent],
        required_repeats=2,
        seed=1729,
        backend="memory",
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert report["passed"] is False
    assert report["summary"]["all_runs_passed"] is False


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        (lambda report: report.update(schema="kestrel.golden_eval_report.v1"), "schema"),
        (
            lambda report: report["results"].pop(),  # type: ignore[index,union-attr]
            "case set",
        ),
        (
            lambda report: report.update(unexpected=True),
            "top-level fields",
        ),
    ],
)
def test_imported_report_requires_exact_schema_and_expected_case_set(
    mutation: object,
    match: str,
) -> None:
    report = _golden_report()
    assert callable(mutation)
    mutation(report)

    with pytest.raises(ValueError, match=match):
        _deterministic_projection(
            report,
            expected_backend="memory",
            expected_case_categories=TEST_CASES,
        )


def test_nonzero_runner_exit_is_failure_even_when_report_claims_pass(
    tmp_path: Path,
) -> None:
    runner = tmp_path / "forged_runner.py"
    payload = json.dumps(_golden_report())
    runner.write_text(
        "\n".join(
            [
                "import pathlib, sys",
                f"payload = {payload!r}",
                "output = pathlib.Path(sys.argv[sys.argv.index('--output') + 1])",
                "output.parent.mkdir(parents=True, exist_ok=True)",
                "output.write_text(payload, encoding='utf-8')",
                "raise SystemExit(9)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    invoke = _subprocess_invoker(
        backend="memory",
        workspace=tmp_path,
        validation_container_image=None,
        max_case_latency_ms=None,
        case_timeout_seconds=1.0,
        iteration_timeout_seconds=2.0,
        golden_runner=runner,
    )

    result = run_determinism(
        repeats=2,
        seed=1729,
        backend="memory",
        run_root=tmp_path / "runs",
        output=tmp_path / "report.json",
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert result["passed"] is False
    receipts = sorted((tmp_path / "runs").glob("repeat-*/iteration-receipt.json"))
    assert len(receipts) == 2
    assert all(json.loads(path.read_text())["runner_exit_code"] == 9 for path in receipts)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group descendant assertion")
def test_bounded_process_uses_monotonic_deadline_and_kills_descendants(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    program = tmp_path / "hang.py"
    program.write_text(
        "\n".join(
            [
                "import pathlib, subprocess, sys, time",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid), encoding='utf-8')",
                "time.sleep(60)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_bounded_process(
        [sys.executable, str(program), str(child_pid_path)],
        cwd=tmp_path,
        environment=os.environ.copy(),
        timeout_seconds=0.4,
    )

    assert result.timed_out is True
    assert result.cleanup_attempted is True
    assert result.cleanup_succeeded is True
    assert result.elapsed_seconds < 5
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    for _ in range(40):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"descendant process {child_pid} survived process-group cleanup")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group descendant assertion")
def test_bounded_process_quiesces_descendants_after_successful_leader_exit(
    tmp_path: Path,
) -> None:
    late_marker = tmp_path / "late-marker.txt"
    program = tmp_path / "leader.py"
    descendant = (
        "import pathlib,time;"
        "time.sleep(1);"
        f"pathlib.Path({str(late_marker)!r}).write_text('late', encoding='utf-8')"
    )
    program.write_text(
        f"import subprocess,sys\nsubprocess.Popen([sys.executable, '-c', {descendant!r}])\n",
        encoding="utf-8",
    )

    result = run_bounded_process(
        [sys.executable, str(program)],
        cwd=tmp_path,
        environment=os.environ.copy(),
        timeout_seconds=5,
    )

    assert result.timed_out is False
    assert result.returncode == 0
    assert result.cleanup_attempted is True
    assert result.cleanup_succeeded is True
    time.sleep(1.2)
    assert late_marker.exists() is False


def test_bounded_process_caps_each_stream_and_retains_useful_tail(tmp_path: Path) -> None:
    program = tmp_path / "large-output.py"
    stdout_tail = b"STDOUT-USEFUL-TAIL"
    stderr_tail = b"STDERR-USEFUL-TAIL"
    stdout_size = 3 * 1024 * 1024
    stderr_size = 2 * 1024 * 1024
    program.write_text(
        "\n".join(
            [
                "import os",
                f"os.write(1, b'x' * {stdout_size} + {stdout_tail!r})",
                f"os.write(2, b'y' * {stderr_size} + {stderr_tail!r})",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = run_bounded_process(
        [sys.executable, str(program)],
        cwd=tmp_path,
        environment=os.environ.copy(),
        timeout_seconds=5,
        capture_limit_bytes=64 * 1024,
    )

    assert result.returncode == 0
    assert result.stdout.endswith(stdout_tail.decode())
    assert result.stderr.endswith(stderr_tail.decode())
    assert len(result.stdout.encode()) <= 64 * 1024
    assert len(result.stderr.encode()) <= 64 * 1024
    assert result.stdout_total_bytes == stdout_size + len(stdout_tail)
    assert result.stderr_total_bytes == stderr_size + len(stderr_tail)
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert result.capture_limit_bytes == 64 * 1024


def test_iteration_excerpt_preserves_the_most_recent_bounded_output() -> None:
    value = "old-prefix" + ("x" * 4_000) + "USEFUL-TAIL"

    excerpt = _excerpt(value, limit=128)

    assert excerpt.startswith("[truncated]...")
    assert excerpt.endswith("USEFUL-TAIL")
    assert "old-prefix" not in excerpt


def test_windows_job_is_assigned_before_resume_and_quiesced_after_leader_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeStream:
        def read(self, _size: int = -1) -> bytes:
            return b""

    class FakeProcess:
        pid = 4242
        returncode = 0
        stdout = FakeStream()
        stderr = FakeStream()

        def poll(self) -> int:
            return 0

        def wait(self) -> int:
            events.append("wait")
            return 0

        def kill(self) -> None:
            events.append("parent-kill")

    class FakeJob:
        def assign(self, process_id: int) -> bool:
            events.append(f"assign:{process_id}")
            return True

        def resume(self, process_id: int) -> bool:
            events.append(f"resume:{process_id}")
            return True

        def terminate_and_wait(self, *, timeout_seconds: float) -> bool:
            events.append(f"quiesce:{timeout_seconds}")
            return True

        def close(self) -> bool:
            events.append("close")
            return True

    def fake_popen(*_args: object, **kwargs: object) -> FakeProcess:
        events.append(f"launch:{kwargs['creationflags']}")
        return FakeProcess()

    monkeypatch.setattr(bounded_process, "_PLATFORM_NAME", "nt")
    monkeypatch.setattr(bounded_process, "_create_windows_process_job", FakeJob)
    monkeypatch.setattr(bounded_process.subprocess, "Popen", fake_popen)

    result = run_bounded_process(
        ["example.exe"],
        cwd=tmp_path,
        environment={},
        timeout_seconds=5,
    )

    assert result.returncode == 0
    assert result.cleanup_attempted is True
    assert result.cleanup_succeeded is True
    assert result.termination_method == "windows_job_object_quiesced"
    assert int(events[0].partition(":")[2]) & 0x00000004
    assert events[1:3] == ["assign:4242", "resume:4242"]
    assert "quiesce:3.0" in events
    assert events[-1] == "close"


def test_windows_verified_job_and_parent_cleanup_reports_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class FakeStream:
        def read(self, _size: int = -1) -> bytes:
            return b""

    class FakeProcess:
        pid = 4343
        returncode: int | None = None
        stdout = FakeStream()
        stderr = FakeStream()

        def poll(self) -> int | None:
            return self.returncode

        def wait(self) -> int:
            events.append("wait")
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            events.append("parent-kill")
            self.returncode = 1

    class FakeJob:
        def assign(self, process_id: int) -> bool:
            events.append(f"assign:{process_id}")
            return True

        def resume(self, process_id: int) -> bool:
            events.append(f"resume:{process_id}")
            return True

        def terminate_and_wait(self, *, timeout_seconds: float) -> bool:
            events.append(f"quiesce:{timeout_seconds}")
            return True

        def close(self) -> bool:
            events.append("close")
            return True

    waits = iter((False, True))
    monkeypatch.setattr(bounded_process, "_PLATFORM_NAME", "nt")
    monkeypatch.setattr(bounded_process, "_create_windows_process_job", FakeJob)
    monkeypatch.setattr(
        bounded_process.subprocess,
        "Popen",
        lambda *_args, **_kwargs: FakeProcess(),
    )
    monkeypatch.setattr(
        bounded_process,
        "_wait_until",
        lambda *_args, **_kwargs: next(waits),
    )

    result = run_bounded_process(
        ["example.exe"],
        cwd=tmp_path,
        environment={},
        timeout_seconds=5,
    )

    assert result.returncode == 1
    assert result.timed_out is True
    assert result.cleanup_attempted is True
    assert result.cleanup_succeeded is True
    assert result.termination_method == "windows_job_object_terminated"
    assert events[-3:] == ["parent-kill", "wait", "close"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Job Object integration assertion")
def test_bounded_process_quiesces_windows_descendant_after_successful_leader_exit(
    tmp_path: Path,
) -> None:
    late_marker = tmp_path / "windows-late-marker.txt"
    program = tmp_path / "windows-leader.py"
    descendant = (
        "import pathlib,time;"
        "time.sleep(1);"
        f"pathlib.Path({str(late_marker)!r}).write_text('late', encoding='utf-8')"
    )
    program.write_text(
        f"import subprocess,sys\nsubprocess.Popen([sys.executable, '-c', {descendant!r}])\n",
        encoding="utf-8",
    )

    result = run_bounded_process(
        [sys.executable, str(program)],
        cwd=tmp_path,
        environment=os.environ.copy(),
        timeout_seconds=5,
    )

    assert result.returncode == 0
    assert result.cleanup_attempted is True
    assert result.cleanup_succeeded is True
    time.sleep(1.2)
    assert late_marker.exists() is False


def test_timeout_writes_atomic_redacted_iteration_and_aggregate_receipts(
    tmp_path: Path,
) -> None:
    runner = tmp_path / "hang_secret.py"
    runner.write_text(
        "import sys, time\n"
        "print('OPENAI_API_KEY=sk-proj-timeout-secret123', file=sys.stderr, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    invoke = _subprocess_invoker(
        backend="memory",
        workspace=tmp_path,
        validation_container_image=None,
        max_case_latency_ms=None,
        case_timeout_seconds=1.0,
        iteration_timeout_seconds=0.3,
        golden_runner=runner,
    )
    output = tmp_path / "report.json"

    report = run_determinism(
        repeats=2,
        seed=1729,
        backend="memory",
        run_root=tmp_path / "runs",
        output=output,
        invoke=invoke,
        source_commit=SOURCE_COMMIT,
        runner_os=RUNNER_OS,
        runner_arch=RUNNER_ARCH,
        python_version=PYTHON_VERSION,
        expected_case_categories=TEST_CASES,
    )

    assert report["passed"] is False
    serialized = output.read_text(encoding="utf-8")
    assert "sk-proj-timeout-secret123" not in serialized
    for receipt_path in sorted((tmp_path / "runs").glob("repeat-*/iteration-receipt.json")):
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        assert receipt["status"] == "timed_out"
        assert receipt["cleanup"]["attempted"] is True
        assert receipt["cleanup"]["succeeded"] is True
        assert receipt["capture"] == {
            "limit_bytes_per_stream": 262144,
            "stderr_total_bytes": len(
                f"OPENAI_API_KEY=sk-proj-timeout-secret123{os.linesep}".encode()
            ),
            "stderr_truncated": False,
            "stdout_total_bytes": 0,
            "stdout_truncated": False,
        }
        assert "sk-proj-timeout-secret123" not in receipt_path.read_text(encoding="utf-8")
