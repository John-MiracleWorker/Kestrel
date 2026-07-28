#!/usr/bin/env python3
"""Run Kestrel's everyday golden evaluation repeatedly and report observed flakes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess  # nosec B404 - fixed local Python evaluation command
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nested_memvid_agent.security_boundary import redact_secrets  # noqa: E402

GOLDEN_RUNNER = ROOT / "scripts" / "run_golden_evals.py"
DEFAULT_SEED = 1729
DEFAULT_REPEATS = 20
MAX_PYTHON_HASH_SEED = 4_294_967_295
_VOLATILE_CASE_KEYS = frozenset({"latency_ms"})

IterationRunner = Callable[[int, Path, int], dict[str, object]]


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _deterministic_projection(report: dict[str, object]) -> dict[str, object]:
    raw_results = report.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("golden report results must be a list")
    cases: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for raw in raw_results:
        if not isinstance(raw, dict):
            raise ValueError("golden report result must be an object")
        name = raw.get("name")
        category = raw.get("category")
        if not isinstance(name, str) or not name or name in seen_names:
            raise ValueError(f"golden report case name is invalid or duplicated: {name!r}")
        if not isinstance(category, str) or not category:
            raise ValueError(f"golden report category is invalid for {name!r}")
        seen_names.add(name)
        outcome = {
            str(key): value
            for key, value in raw.items()
            if key not in {"name", "category"} | _VOLATILE_CASE_KEYS
        }
        cases.append({"name": name, "category": category, "outcome": outcome})
    cases.sort(key=lambda item: (str(item["name"]), str(item["category"])))
    configuration = report.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("golden report configuration must be an object")
    stable_configuration = {
        key: configuration.get(key) for key in ("backend", "provider", "model", "seed")
    }
    return {
        "schema": report.get("schema"),
        "configuration": stable_configuration,
        "cases": cases,
    }


def _signature(projection: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(projection).encode()).hexdigest()


def _failed_case_names(report: dict[str, object]) -> list[str]:
    raw_results = report.get("results")
    if not isinstance(raw_results, list):
        raise ValueError("golden report results must be a list")
    return sorted(
        str(item["name"])
        for item in raw_results
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and item.get("passed") is not True
    )


def _differing_cases(projections: list[dict[str, object]]) -> list[str]:
    if not projections:
        return []
    raw_reference_cases = projections[0].get("cases")
    if not isinstance(raw_reference_cases, list):
        raise ValueError("determinism projection cases must be a list")
    reference_cases = {
        str(item["name"]): item for item in raw_reference_cases if isinstance(item, dict)
    }
    differing: set[str] = set()
    for projection in projections[1:]:
        raw_cases = projection.get("cases")
        if not isinstance(raw_cases, list):
            raise ValueError("determinism projection cases must be a list")
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
) -> dict[str, object]:
    if required_repeats < 2:
        raise ValueError("determinism evaluation requires at least two repeats")
    projections = [_deterministic_projection(report) for report in reports]
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
    all_runs_passed = len(reports) == required_repeats and all(
        report.get("passed") is True for report in reports
    )
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
    return {
        "schema": "kestrel.determinism_eval_report.v1",
        "configuration": {
            "seed": seed,
            "required_repeats": required_repeats,
            "comparison": "functional_outcomes_excluding_wall_clock_latency",
        },
        "summary": summary,
        "differing_cases": _differing_cases(projections),
        "reference_projection": projections[0] if projections else None,
        "runs": [
            {
                "repeat": index,
                "passed": report.get("passed") is True,
                "outcome_signature": signature,
                "failed_cases": _failed_case_names(report),
            }
            for index, (report, signature) in enumerate(
                zip(reports, signatures, strict=True), start=1
            )
        ],
        "passed": (
            all_runs_passed and len(signature_counts) == 1 and len(reports) == required_repeats
        ),
    }


def _write_json(path: Path, payload: object) -> None:
    path = path.expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def _failure_report(*, seed: int, error: str) -> dict[str, object]:
    return {
        "schema": "kestrel.golden_eval_report.v2",
        "configuration": {
            "backend": "memory",
            "provider": "mock",
            "model": "mock",
            "seed": seed,
        },
        "results": [
            {
                "name": "runner_failure",
                "category": "repo_regression",
                "passed": False,
                "score": 0.0,
                "memory_hits": 0,
                "context_chars": 0,
                "tool_count": 0,
                "cost_estimate_usd": None,
                "error": error,
            }
        ],
        "passed": False,
    }


def run_determinism(
    *,
    repeats: int,
    seed: int,
    run_root: Path,
    output: Path,
    invoke: IterationRunner,
) -> dict[str, object]:
    if repeats < 2:
        raise ValueError("determinism evaluation requires at least two repeats")
    if not 0 <= seed <= MAX_PYTHON_HASH_SEED:
        raise ValueError(f"seed must be between 0 and {MAX_PYTHON_HASH_SEED}")
    run_root = run_root.expanduser().resolve(strict=False)
    if run_root.exists():
        raise ValueError(f"run root must not already exist: {run_root}")
    run_root.mkdir(parents=True)
    reports: list[dict[str, object]] = []
    for iteration in range(1, repeats + 1):
        repeat_root = run_root / f"repeat-{iteration:02d}"
        repeat_root.mkdir()
        memory_root = repeat_root / "memory"
        try:
            report = invoke(iteration, memory_root, seed)
        except Exception as exc:  # noqa: BLE001 - report every runner failure
            report = _failure_report(
                seed=seed,
                error=redact_secrets(f"{type(exc).__name__}: {exc}"),
            )
        reports.append(report)
    result = build_determinism_report(
        reports,
        required_repeats=repeats,
        seed=seed,
    )
    _write_json(output, result)
    return result


def _subprocess_invoker(
    *,
    workspace: Path,
    validation_container_image: str | None,
    max_case_latency_ms: float | None,
) -> IterationRunner:
    def invoke(iteration: int, memory_root: Path, seed: int) -> dict[str, object]:
        del iteration
        report_path = memory_root.parent / "golden-report.json"
        command = [
            sys.executable,
            str(GOLDEN_RUNNER),
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
        ]
        if validation_container_image:
            command.extend(["--validation-container-image", validation_container_image])
        if max_case_latency_ms is not None:
            command.extend(["--max-case-latency-ms", str(max_case_latency_ms)])
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = str(seed)
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=ROOT,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
        )
        if not report_path.is_file():
            stderr = completed.stderr.strip()
            raise RuntimeError(
                f"golden runner exited {completed.returncode} without a report: {stderr}"
            )
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"golden runner wrote invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("golden runner report is not a JSON object")
        return payload

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
    args = parser.parse_args()
    if args.repeats < 2:
        parser.error("--repeats must be at least 2")
    if not 0 <= args.seed <= MAX_PYTHON_HASH_SEED:
        parser.error(f"--seed must be between 0 and {MAX_PYTHON_HASH_SEED}")
    if args.max_case_latency_ms is not None and args.max_case_latency_ms <= 0:
        parser.error("--max-case-latency-ms must be greater than 0")
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
            ),
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
