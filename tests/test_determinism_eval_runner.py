from __future__ import annotations

from pathlib import Path

import pytest

from scripts.run_determinism_evals import (
    _deterministic_projection,
    build_determinism_report,
    run_determinism,
)
from scripts.run_golden_evals import _eval_identifier


def _golden_report(
    *,
    first_passed: bool = True,
    result_order: tuple[str, ...] = ("beta", "alpha"),
    latency_offset: float = 0.0,
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
    return {
        "schema": "kestrel.golden_eval_report.v2",
        "configuration": {
            "backend": "memory",
            "provider": "mock",
            "model": "mock",
            "seed": 1729,
            "max_case_latency_ms": None,
        },
        "results": results,
        "summary": {
            "pass_count": sum(bool(item["passed"]) for item in results),
            "fail_count": sum(not bool(item["passed"]) for item in results),
        },
        "passed": all(bool(item["passed"]) for item in results),
    }


def test_seeded_eval_identifier_is_stable_and_never_looks_like_a_release_id() -> None:
    assert _eval_identifier(1729) == _eval_identifier(1729)
    assert _eval_identifier(1729) != _eval_identifier(1730)
    assert _eval_identifier(1729).startswith("golden_seed_")


def test_projection_sorts_cases_and_excludes_wall_clock_measurements() -> None:
    first = _golden_report(result_order=("beta", "alpha"), latency_offset=0.0)
    second = _golden_report(result_order=("alpha", "beta"), latency_offset=9000.0)

    assert _deterministic_projection(first) == _deterministic_projection(second)
    assert _deterministic_projection(first)["cases"] == [
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

    report = build_determinism_report(reports, required_repeats=3, seed=1729)

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
        run_root=tmp_path / "runs",
        output=output,
        invoke=invoke,
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
            run_root=run_root,
            output=tmp_path / "report.json",
            invoke=lambda _iteration, _memory, _seed: _golden_report(),
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
        run_root=tmp_path / "runs",
        output=output,
        invoke=fail,
    )

    assert secret not in output.read_text(encoding="utf-8")
