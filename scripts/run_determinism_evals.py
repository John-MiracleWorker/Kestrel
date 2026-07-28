#!/usr/bin/env python3
"""Run Kestrel's everyday golden evaluation repeatedly and report observed flakes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from nested_memvid_agent.security_boundary import redact_secrets  # noqa: E402
from scripts.bounded_process import run_bounded_process  # noqa: E402
from scripts.golden_eval_contract import (  # noqa: E402
    GOLDEN_CASE_CATEGORIES,
    GOLDEN_REPORT_SCHEMA,
    validate_golden_report,
)

GOLDEN_RUNNER = ROOT / "scripts" / "run_golden_evals.py"
DEFAULT_SEED = 1729
DEFAULT_REPEATS = 20
DEFAULT_CASE_TIMEOUT_SECONDS = 120.0
DEFAULT_ITERATION_TIMEOUT_SECONDS = 1_500.0
MAX_PYTHON_HASH_SEED = 4_294_967_295
_VOLATILE_CASE_KEYS = frozenset({"latency_ms"})
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")

IterationRunner = Callable[[int, Path, int], dict[str, object]]


class GoldenRunnerError(RuntimeError):
    """A golden subprocess failed before yielding an acceptable report."""

    def __init__(self, message: str, *, receipt: Mapping[str, object]) -> None:
        super().__init__(message)
        self.receipt = dict(receipt)


def _redact_json(value: Any) -> Any:
    return redact_secrets(value)


def _excerpt(value: str, *, limit: int = 2_000) -> str:
    value = redact_secrets(value)
    if len(value) <= limit:
        return value
    return "[truncated]..." + value[-limit:]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _deterministic_projection(
    report: dict[str, object],
    *,
    expected_case_categories: Mapping[str, str] = GOLDEN_CASE_CATEGORIES,
) -> dict[str, object]:
    validate_golden_report(
        report,
        expected_case_categories=expected_case_categories,
    )
    raw_results = report["results"]
    assert isinstance(raw_results, list)
    cases: list[dict[str, object]] = []
    for raw in raw_results:
        assert isinstance(raw, dict)
        name = str(raw["name"])
        category = str(raw["category"])
        outcome = {
            str(key): value
            for key, value in raw.items()
            if key not in {"name", "category"} | _VOLATILE_CASE_KEYS
        }
        cases.append({"name": name, "category": category, "outcome": outcome})
    cases.sort(key=lambda item: (str(item["name"]), str(item["category"])))
    configuration = report["configuration"]
    assert isinstance(configuration, dict)
    stable_configuration = {
        key: configuration.get(key) for key in ("backend", "provider", "model", "seed")
    }
    return {
        "schema": report["schema"],
        "configuration": stable_configuration,
        "cases": cases,
    }


def _signature(projection: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(projection).encode()).hexdigest()


def _failed_case_names(report: dict[str, object]) -> list[str]:
    raw_results = report["results"]
    assert isinstance(raw_results, list)
    return sorted(
        str(item["name"])
        for item in raw_results
        if isinstance(item, dict) and item.get("passed") is not True
    )


def _differing_cases(projections: list[dict[str, object]]) -> list[str]:
    if not projections:
        return []
    raw_reference_cases = projections[0]["cases"]
    assert isinstance(raw_reference_cases, list)
    reference_cases = {
        str(item["name"]): item for item in raw_reference_cases if isinstance(item, dict)
    }
    differing: set[str] = set()
    for projection in projections[1:]:
        raw_cases = projection["cases"]
        assert isinstance(raw_cases, list)
        cases = {str(item["name"]): item for item in raw_cases if isinstance(item, dict)}
        for name in reference_cases.keys() | cases.keys():
            if reference_cases.get(name) != cases.get(name):
                differing.add(name)
    return sorted(differing)


def build_determinism_report(
    reports: list[dict[str, object]],
    *,
    required_repeats: int,
    seed: int,
    expected_case_categories: Mapping[str, str] = GOLDEN_CASE_CATEGORIES,
    source_commit: str | None = None,
    case_timeout_seconds: float | None = None,
    iteration_timeout_seconds: float | None = None,
) -> dict[str, object]:
    if required_repeats < 2:
        raise ValueError("determinism evaluation requires at least two repeats")
    derived_passes: list[bool] = []
    projections: list[dict[str, object]] = []
    for report in reports:
        derived_passes.append(
            validate_golden_report(
                report,
                expected_case_categories=expected_case_categories,
                expected_seed=seed,
            )
        )
        projections.append(
            _deterministic_projection(
                report,
                expected_case_categories=expected_case_categories,
            )
        )
    signatures = [_signature(projection) for projection in projections]
    signature_counts = Counter(signatures)
    modal_count = max(signature_counts.values(), default=0)
    observed_flake_count = len(signatures) - modal_count
    streak = 0
    if signatures:
        first = signatures[0]
        for signature in signatures:
            if signature != first:
                break
            streak += 1
    all_runs_passed = len(reports) == required_repeats and all(derived_passes)
    summary = {
        "completed_repeats": len(reports),
        "required_repeats": required_repeats,
        "all_runs_passed": all_runs_passed,
        "unique_outcome_signatures": len(signature_counts),
        "determinism_streak": streak,
        "observed_flake_count": observed_flake_count,
        "observed_flake_rate": (
            round(observed_flake_count / len(signatures), 6) if signatures else None
        ),
    }
    return cast(
        dict[str, object],
        _redact_json(
            {
                "schema": "kestrel.determinism_eval_report.v2",
                "configuration": {
                    "seed": seed,
                    "required_repeats": required_repeats,
                    "comparison": "functional_outcomes_excluding_wall_clock_latency",
                    "source_commit": source_commit,
                    "case_timeout_seconds": case_timeout_seconds,
                    "iteration_timeout_seconds": iteration_timeout_seconds,
                },
                "summary": summary,
                "differing_cases": _differing_cases(projections),
                "reference_projection": projections[0] if projections else None,
                "runs": [
                    {
                        "repeat": index,
                        "passed": derived_passed,
                        "outcome_signature": signature,
                        "failed_cases": _failed_case_names(report),
                    }
                    for index, (report, signature, derived_passed) in enumerate(
                        zip(reports, signatures, derived_passes, strict=True),
                        start=1,
                    )
                ],
                "passed": (
                    all_runs_passed
                    and len(signature_counts) == 1
                    and len(reports) == required_repeats
                ),
            }
        ),
    )


def _write_json(path: Path, payload: object) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(_redact_json(payload), indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def _failure_report(
    *,
    seed: int,
    error: str,
    expected_case_categories: Mapping[str, str],
) -> dict[str, object]:
    results: list[dict[str, object]] = [
        {
            "name": name,
            "category": category,
            "passed": False,
            "score": 0.0,
            "latency_ms": 0.0,
            "memory_hits": 0,
            "context_chars": 0,
            "tool_count": 0,
            "cost_estimate_usd": None,
            "error": redact_secrets(error),
        }
        for name, category in sorted(expected_case_categories.items())
    ]
    cost = {
        "measurement_status": "unmeasured",
        "gate_configured": False,
        "required": False,
        "measured_case_count": 0,
        "unmeasured_case_count": len(results),
        "cost_estimate_usd_total": None,
        "passed": None,
        "residual": "Runner failure prevented provider cost measurement.",
    }
    latency = {
        "measurement_status": "measured",
        "gate_configured": False,
        "required": False,
        "threshold_max_case_latency_ms": None,
        "latency_ms_max": 0.0,
        "passed": None,
    }
    return {
        "schema": GOLDEN_REPORT_SCHEMA,
        "configuration": {
            "backend": "memory",
            "provider": "mock",
            "model": "mock",
            "seed": seed,
            "max_case_latency_ms": None,
        },
        "results": results,
        "summary": {
            "pass_count": 0,
            "fail_count": len(results),
            "latency_ms_max": 0.0,
            "context_chars_max": 0,
            "tool_count_total": 0,
            "cost_estimate_usd_total": None,
            "categories": {},
            "acceptance": {"latency": latency, "cost": cost},
            "promotion_precision": None,
            "false_promotion_count": 0,
        },
        "acceptance": {
            "functional": {"required": True, "passed": False},
            "latency": latency,
            "cost": cost,
        },
        "passed": False,
    }


def _base_iteration_receipt(*, iteration: int, seed: int) -> dict[str, object]:
    return {
        "schema": "kestrel.determinism_iteration_receipt.v1",
        "repeat": iteration,
        "seed": seed,
        "status": "runner_error",
        "runner_exit_code": None,
        "elapsed_seconds": None,
        "deadline": {
            "clock": "monotonic",
            "seconds": None,
            "exceeded": False,
        },
        "cleanup": {
            "attempted": False,
            "succeeded": True,
            "method": None,
        },
        "report_schema": None,
        "derived_passed": False,
        "error": None,
        "stdout": "",
        "stderr": "",
    }


def run_determinism(
    *,
    repeats: int,
    seed: int,
    run_root: Path,
    output: Path,
    invoke: IterationRunner,
    expected_case_categories: Mapping[str, str] = GOLDEN_CASE_CATEGORIES,
    source_commit: str | None = None,
    case_timeout_seconds: float | None = None,
    iteration_timeout_seconds: float | None = None,
) -> dict[str, object]:
    if repeats < 2:
        raise ValueError("determinism evaluation requires at least two repeats")
    if not 0 <= seed <= MAX_PYTHON_HASH_SEED:
        raise ValueError(f"seed must be between 0 and {MAX_PYTHON_HASH_SEED}")
    if source_commit is not None and not _COMMIT_RE.fullmatch(source_commit):
        raise ValueError("source commit must be a 40-character lowercase hexadecimal SHA")
    run_root = run_root.expanduser().resolve(strict=False)
    if run_root.exists():
        raise ValueError(f"run root must not already exist: {run_root}")
    run_root.mkdir(parents=True)
    reports: list[dict[str, object]] = []
    result: dict[str, object] = {}
    for iteration in range(1, repeats + 1):
        repeat_root = run_root / f"repeat-{iteration:02d}"
        repeat_root.mkdir()
        memory_root = repeat_root / "memory"
        receipt = _base_iteration_receipt(iteration=iteration, seed=seed)
        try:
            imported = invoke(iteration, memory_root, seed)
            report = _redact_json(imported)
            if not isinstance(report, dict):
                raise ValueError("golden runner report is not a JSON object")
            derived_passed = validate_golden_report(
                report,
                expected_case_categories=expected_case_categories,
                expected_seed=seed,
            )
            receipt.update(
                {
                    "status": "completed",
                    "report_schema": report["schema"],
                    "derived_passed": derived_passed,
                }
            )
        except GoldenRunnerError as exc:
            receipt.update(_redact_json(exc.receipt))
            receipt["error"] = redact_secrets(f"{type(exc).__name__}: {exc}")
            report = _failure_report(
                seed=seed,
                error=str(receipt["error"]),
                expected_case_categories=expected_case_categories,
            )
        except Exception as exc:  # noqa: BLE001 - preserve a fail-closed receipt
            receipt.update(
                {
                    "status": ("invalid_report" if isinstance(exc, ValueError) else "runner_error"),
                    "error": redact_secrets(f"{type(exc).__name__}: {exc}"),
                }
            )
            report = _failure_report(
                seed=seed,
                error=str(receipt["error"]),
                expected_case_categories=expected_case_categories,
            )
        _write_json(repeat_root / "iteration-receipt.json", receipt)
        reports.append(report)
        result = build_determinism_report(
            reports,
            required_repeats=repeats,
            seed=seed,
            expected_case_categories=expected_case_categories,
            source_commit=source_commit,
            case_timeout_seconds=case_timeout_seconds,
            iteration_timeout_seconds=iteration_timeout_seconds,
        )
        _write_json(output, result)
    return result


def _subprocess_invoker(
    *,
    workspace: Path,
    validation_container_image: str | None,
    max_case_latency_ms: float | None,
    case_timeout_seconds: float,
    iteration_timeout_seconds: float,
    golden_runner: Path = GOLDEN_RUNNER,
) -> IterationRunner:
    def invoke(iteration: int, memory_root: Path, seed: int) -> dict[str, object]:
        del iteration
        report_path = memory_root.parent / "golden-report.json"
        command = [
            sys.executable,
            str(golden_runner),
            "--backend",
            "memory",
            "--provider",
            "mock",
            "--model",
            "mock",
            "--workspace",
            str(workspace),
            "--memory-dir",
            str(memory_root),
            "--seed",
            str(seed),
            "--output",
            str(report_path),
            "--case-timeout-seconds",
            str(case_timeout_seconds),
        ]
        if validation_container_image:
            command.extend(["--validation-container-image", validation_container_image])
        if max_case_latency_ms is not None:
            command.extend(["--max-case-latency-ms", str(max_case_latency_ms)])
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = str(seed)
        completed = run_bounded_process(
            command,
            cwd=ROOT,
            environment=environment,
            timeout_seconds=iteration_timeout_seconds,
        )
        receipt: dict[str, object] = {
            "status": (
                "timed_out"
                if completed.timed_out
                else "cleanup_unverified"
                if not completed.cleanup_succeeded
                else "runner_nonzero"
                if completed.returncode != 0
                else "completed"
            ),
            "runner_exit_code": completed.returncode,
            "elapsed_seconds": completed.elapsed_seconds,
            "deadline": {
                "clock": completed.deadline_clock,
                "seconds": iteration_timeout_seconds,
                "exceeded": completed.timed_out,
            },
            "cleanup": {
                "attempted": completed.cleanup_attempted,
                "succeeded": completed.cleanup_succeeded,
                "method": completed.termination_method,
            },
            "capture": {
                "limit_bytes_per_stream": completed.capture_limit_bytes,
                "stdout_total_bytes": completed.stdout_total_bytes,
                "stdout_truncated": completed.stdout_truncated,
                "stderr_total_bytes": completed.stderr_total_bytes,
                "stderr_truncated": completed.stderr_truncated,
            },
            "stdout": _excerpt(completed.stdout),
            "stderr": _excerpt(completed.stderr),
        }
        if completed.timed_out:
            raise GoldenRunnerError(
                f"golden runner exceeded {iteration_timeout_seconds} second deadline",
                receipt=receipt,
            )
        if not completed.cleanup_succeeded:
            raise GoldenRunnerError(
                "golden runner process-tree cleanup could not be verified",
                receipt=receipt,
            )
        if completed.returncode != 0:
            raise GoldenRunnerError(
                f"golden runner exited nonzero ({completed.returncode})",
                receipt=receipt,
            )
        if not report_path.is_file():
            receipt["status"] = "missing_report"
            raise GoldenRunnerError(
                "golden runner exited zero without a report",
                receipt=receipt,
            )
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            receipt["status"] = "invalid_json"
            raise GoldenRunnerError(
                f"golden runner wrote invalid JSON: {exc}",
                receipt=receipt,
            ) from exc
        if not isinstance(payload, dict):
            receipt["status"] = "invalid_json"
            raise GoldenRunnerError(
                "golden runner report is not a JSON object",
                receipt=receipt,
            )
        return cast(dict[str, object], _redact_json(payload))

    return invoke


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument(
        "--run-root",
        type=Path,
        default=Path("./tmp-golden/determinism-runs"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./tmp-golden/determinism-report.json"),
    )
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--validation-container-image",
        default=os.getenv("NEST_AGENT_VALIDATION_CONTAINER_IMAGE"),
    )
    parser.add_argument("--max-case-latency-ms", type=float)
    parser.add_argument(
        "--case-timeout-seconds",
        type=float,
        default=DEFAULT_CASE_TIMEOUT_SECONDS,
    )
    parser.add_argument(
        "--iteration-timeout-seconds",
        type=float,
        default=DEFAULT_ITERATION_TIMEOUT_SECONDS,
    )
    parser.add_argument("--source-commit", default=os.getenv("GITHUB_SHA"))
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("--repeats must be at least 2")
    if not 0 <= args.seed <= MAX_PYTHON_HASH_SEED:
        parser.error(f"--seed must be between 0 and {MAX_PYTHON_HASH_SEED}")
    if args.max_case_latency_ms is not None and args.max_case_latency_ms <= 0:
        parser.error("--max-case-latency-ms must be greater than 0")
    if args.case_timeout_seconds <= 0:
        parser.error("--case-timeout-seconds must be greater than 0")
    if args.iteration_timeout_seconds <= args.case_timeout_seconds:
        parser.error("--iteration-timeout-seconds must exceed --case-timeout-seconds")
    try:
        result = run_determinism(
            repeats=args.repeats,
            seed=args.seed,
            run_root=args.run_root,
            output=args.output,
            invoke=_subprocess_invoker(
                workspace=args.workspace.resolve(strict=True),
                validation_container_image=args.validation_container_image,
                max_case_latency_ms=args.max_case_latency_ms,
                case_timeout_seconds=args.case_timeout_seconds,
                iteration_timeout_seconds=args.iteration_timeout_seconds,
            ),
            source_commit=args.source_commit,
            case_timeout_seconds=args.case_timeout_seconds,
            iteration_timeout_seconds=args.iteration_timeout_seconds,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(_redact_json(result), indent=2, sort_keys=True))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
