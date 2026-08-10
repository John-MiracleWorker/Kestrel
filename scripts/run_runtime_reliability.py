#!/usr/bin/env python3
"""Qualify timing-sensitive runtime paths in fresh Python interpreters."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess  # nosec B404
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from nested_memvid_agent.security_boundary import redact_secrets  # noqa: E402
from scripts.bounded_process import BoundedProcessResult, run_bounded_process  # noqa: E402

RUNTIME_RELIABILITY_TESTS = (
    "tests/test_channels.py::test_run_manager_channel_turn_is_durable_and_isolated_from_primary_replay",
    "tests/test_channels.py::test_server_exposes_channel_ingest_route",
    "tests/test_full_agent_runtime.py::test_run_manager_heartbeat_renews_and_releases_its_run_lease",
)
DEFAULT_REPEATS = 20
DEFAULT_ITERATION_TIMEOUT_SECONDS = 900.0

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_PYTHON_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[A-Za-z0-9.+-]*)?$")

IterationInvoker = Callable[[int, Path], BoundedProcessResult]
WorkspaceHeadResolver = Callable[[Path], str]
BoundedRunner = Callable[..., BoundedProcessResult]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def _excerpt(value: str, *, limit: int = 4_000) -> str:
    redacted = str(redact_secrets(value))
    if len(redacted) <= limit:
        return redacted
    return "[truncated]..." + redacted[-limit:]


def _normalize_runner_os(value: str) -> str:
    normalized = value.strip().lower()
    if normalized == "linux":
        return "Linux"
    if normalized in {"darwin", "macos"}:
        return "macOS"
    if normalized == "windows":
        return "Windows"
    raise ValueError(f"runner operating system is unsupported: {value!r}")


def _normalize_runner_arch(value: str) -> str:
    normalized = value.strip().lower()
    if normalized in {"amd64", "x86_64", "x64"}:
        return "X64"
    if normalized in {"aarch64", "arm64"}:
        return "ARM64"
    raise ValueError(f"runner architecture is unsupported: {value!r}")


def _validate_preflight(
    *,
    repeats: int,
    source_commit: str,
    workspace: Path,
    run_root: Path,
    output: Path,
    resolve_workspace_head: WorkspaceHeadResolver,
    runner_os: str,
    runner_arch: str,
    python_version: str,
    iteration_timeout_seconds: float,
) -> tuple[str, str, str]:
    if repeats < DEFAULT_REPEATS:
        raise ValueError(
            f"runtime reliability qualification requires at least {DEFAULT_REPEATS} repeats"
        )
    if not _COMMIT_RE.fullmatch(source_commit):
        raise ValueError("source commit must be a lowercase 40-character hexadecimal SHA")
    if iteration_timeout_seconds <= 0:
        raise ValueError("iteration timeout must be greater than zero")
    if not workspace.is_dir():
        raise ValueError(f"workspace is not a directory: {workspace}")

    normalized_os = _normalize_runner_os(runner_os)
    normalized_arch = _normalize_runner_arch(runner_arch)
    normalized_python = python_version.strip()
    if not _PYTHON_VERSION_RE.fullmatch(normalized_python):
        raise ValueError("runner Python version must be a concrete semantic version")

    observed_head = resolve_workspace_head(workspace).strip().lower()
    if observed_head != source_commit:
        raise ValueError(
            "workspace HEAD does not match source commit: "
            f"expected {source_commit}, observed {observed_head or '<empty>'}"
        )
    if run_root.exists():
        raise FileExistsError(f"runtime reliability run root already exists: {run_root}")
    if output.exists():
        raise FileExistsError(f"runtime reliability report already exists: {output}")
    return normalized_os, normalized_arch, normalized_python


def _status(result: BoundedProcessResult) -> str:
    if result.timed_out:
        return "timed_out"
    if result.cleanup_attempted and not result.cleanup_succeeded:
        return "cleanup_unverified"
    if result.returncode != 0:
        return "runner_nonzero"
    return "completed"


def _iteration_receipt(
    *,
    repeat: int,
    source_commit: str,
    result: BoundedProcessResult,
) -> dict[str, object]:
    status = _status(result)
    stdout = _excerpt(result.stdout)
    stderr = _excerpt(result.stderr)
    receipt: dict[str, object] = {
        "schema": "kestrel.runtime_reliability_iteration.v1",
        "subject": {"source_commit": source_commit},
        "repeat": repeat,
        "status": status,
        "derived_passed": status == "completed",
        "runner_exit_code": result.returncode,
        "elapsed_seconds": result.elapsed_seconds,
        "deadline": {
            "clock": result.deadline_clock,
            "exceeded": result.timed_out,
        },
        "cleanup": {
            "attempted": result.cleanup_attempted,
            "succeeded": result.cleanup_succeeded,
            "method": result.termination_method,
        },
        "capture": {
            "limit_bytes": result.capture_limit_bytes,
            "stdout_total_bytes": result.stdout_total_bytes,
            "stderr_total_bytes": result.stderr_total_bytes,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "stdout_sha256": hashlib.sha256(result.stdout.encode("utf-8")).hexdigest(),
            "stderr_sha256": hashlib.sha256(result.stderr.encode("utf-8")).hexdigest(),
        },
        "stdout": stdout,
        "stderr": stderr,
    }
    return cast(dict[str, object], redact_secrets(receipt))


def _iteration_error_receipt(
    *,
    repeat: int,
    source_commit: str,
    error: Exception,
) -> dict[str, object]:
    empty_sha256 = hashlib.sha256(b"").hexdigest()
    receipt: dict[str, object] = {
        "schema": "kestrel.runtime_reliability_iteration.v1",
        "subject": {"source_commit": source_commit},
        "repeat": repeat,
        "status": "runner_error",
        "derived_passed": False,
        "runner_exit_code": None,
        "elapsed_seconds": None,
        "deadline": {"clock": "monotonic", "exceeded": False},
        "cleanup": {"attempted": False, "succeeded": False, "method": None},
        "capture": {
            "limit_bytes": None,
            "stdout_total_bytes": 0,
            "stderr_total_bytes": 0,
            "stdout_truncated": False,
            "stderr_truncated": False,
            "stdout_sha256": empty_sha256,
            "stderr_sha256": empty_sha256,
        },
        "stdout": "",
        "stderr": "",
        "error": _excerpt(f"{type(error).__name__}: {error}"),
    }
    return cast(dict[str, object], redact_secrets(receipt))


def build_iteration_invoker(
    *,
    workspace: Path,
    python_executable: str,
    iteration_timeout_seconds: float,
    base_environment: Mapping[str, str] | None = None,
    bounded_runner: BoundedRunner = run_bounded_process,
) -> IterationInvoker:
    """Build a focused pytest invoker with process and basetemp isolation."""

    if not python_executable:
        raise ValueError("Python executable must not be empty")
    if iteration_timeout_seconds <= 0:
        raise ValueError("iteration timeout must be greater than zero")
    environment = dict(os.environ if base_environment is None else base_environment)
    environment["PYTHONHASHSEED"] = "0"
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    def invoke(_repeat: int, repeat_root: Path) -> BoundedProcessResult:
        command = (
            python_executable,
            "-m",
            "pytest",
            "-q",
            *RUNTIME_RELIABILITY_TESTS,
            "--basetemp",
            str(repeat_root / "pytest-tmp"),
        )
        return bounded_runner(
            command,
            cwd=workspace,
            environment=environment,
            timeout_seconds=iteration_timeout_seconds,
        )

    return invoke


def _first_failure(receipt: dict[str, object]) -> dict[str, object]:
    failure = {
        key: receipt[key]
        for key in (
            "repeat",
            "status",
            "runner_exit_code",
            "elapsed_seconds",
            "deadline",
            "cleanup",
            "capture",
            "stdout",
            "stderr",
        )
    }
    if "error" in receipt:
        failure["error"] = receipt["error"]
    return failure


def run_runtime_reliability(
    *,
    repeats: int,
    run_root: Path,
    output: Path,
    invoke: IterationInvoker,
    source_commit: str,
    workspace: Path,
    resolve_workspace_head: WorkspaceHeadResolver,
    runner_os: str,
    runner_arch: str,
    python_version: str,
    iteration_timeout_seconds: float,
) -> dict[str, object]:
    """Run the focused runtime qualification, stopping on the first failure."""

    normalized_os, normalized_arch, normalized_python = _validate_preflight(
        repeats=repeats,
        source_commit=source_commit,
        workspace=workspace,
        run_root=run_root,
        output=output,
        resolve_workspace_head=resolve_workspace_head,
        runner_os=runner_os,
        runner_arch=runner_arch,
        python_version=python_version,
        iteration_timeout_seconds=iteration_timeout_seconds,
    )

    run_root.mkdir(parents=True)
    runs: list[dict[str, object]] = []
    for repeat in range(1, repeats + 1):
        repeat_root = run_root / f"repeat-{repeat:02d}"
        repeat_root.mkdir()
        try:
            result = invoke(repeat, repeat_root)
            receipt = _iteration_receipt(
                repeat=repeat,
                source_commit=source_commit,
                result=result,
            )
        except Exception as exc:  # noqa: BLE001 - preserve a fail-closed receipt
            receipt = _iteration_error_receipt(
                repeat=repeat,
                source_commit=source_commit,
                error=exc,
            )
        _write_json(repeat_root / "iteration-receipt.json", receipt)
        runs.append(receipt)
        if not receipt["derived_passed"]:
            break

    failed = [run for run in runs if not run["derived_passed"]]
    consecutive_passes = 0
    for run in runs:
        if not run["derived_passed"]:
            break
        consecutive_passes += 1
    first_failure = _first_failure(failed[0]) if failed else None
    passed = len(runs) == repeats and not failed
    report: dict[str, object] = {
        "schema": "kestrel.runtime_reliability_report.v1",
        "subject": {"source_commit": source_commit},
        "configuration": {
            "required_repeats": repeats,
            "tests": list(RUNTIME_RELIABILITY_TESTS),
            "subprocess_isolation": "fresh_interpreter_and_basetemp_per_repeat",
            "iteration_timeout_seconds": iteration_timeout_seconds,
        },
        "environment": {
            "runner_os": normalized_os,
            "runner_arch": normalized_arch,
            "python_version": normalized_python,
        },
        "summary": {
            "passed": passed,
            "completed_repeats": len(runs),
            "required_repeats": repeats,
            "consecutive_passes": consecutive_passes,
            "failure_count": len(failed),
            "first_failure": first_failure,
        },
        "runs": runs,
    }
    report = cast(dict[str, object], redact_secrets(report))
    _write_json(output, report)
    return report


def resolve_git_head(workspace: Path) -> str:
    """Resolve an exact commit only when the checkout contains no other code."""

    try:
        completed = subprocess.run(  # noqa: S603  # nosec B603 B607
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=workspace,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not resolve workspace HEAD: {exc}") from exc
    if completed.returncode != 0:
        diagnostic = _excerpt(completed.stderr.strip() or completed.stdout.strip())
        raise ValueError(f"could not resolve workspace HEAD: {diagnostic or 'git failed'}")
    try:
        status = subprocess.run(  # noqa: S603  # nosec B603 B607
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=workspace,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"could not verify workspace cleanliness: {exc}") from exc
    if status.returncode != 0:
        diagnostic = _excerpt(status.stderr.strip() or status.stdout.strip())
        raise ValueError(
            f"could not verify workspace cleanliness: {diagnostic or 'git failed'}"
        )
    if status.stdout.strip():
        raise ValueError(
            "workspace contains uncommitted changes; exact-SHA qualification "
            "requires a clean checkout"
        )
    return completed.stdout.strip().lower()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument(
        "--run-root",
        type=Path,
        default=Path("./tmp-runtime-reliability/runs"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("./tmp-runtime-reliability/report.json"),
    )
    parser.add_argument("--workspace", type=Path, default=ROOT)
    parser.add_argument(
        "--iteration-timeout-seconds",
        type=float,
        default=DEFAULT_ITERATION_TIMEOUT_SECONDS,
    )
    parser.add_argument("--source-commit", default=os.getenv("GITHUB_SHA"))
    args = parser.parse_args(argv)
    if args.repeats < DEFAULT_REPEATS:
        parser.error(f"--repeats must be at least {DEFAULT_REPEATS}")
    if args.iteration_timeout_seconds <= 0:
        parser.error("--iteration-timeout-seconds must be greater than 0")
    if not isinstance(args.source_commit, str) or not _COMMIT_RE.fullmatch(args.source_commit):
        parser.error("--source-commit must be a 40-character lowercase hexadecimal SHA")

    try:
        workspace = args.workspace.expanduser().resolve(strict=True)
        report = run_runtime_reliability(
            repeats=args.repeats,
            run_root=args.run_root.expanduser().resolve(strict=False),
            output=args.output.expanduser().resolve(strict=False),
            invoke=build_iteration_invoker(
                workspace=workspace,
                python_executable=sys.executable,
                iteration_timeout_seconds=args.iteration_timeout_seconds,
            ),
            source_commit=args.source_commit,
            workspace=workspace,
            resolve_workspace_head=resolve_git_head,
            runner_os=platform.system(),
            runner_arch=platform.machine(),
            python_version=platform.python_version(),
            iteration_timeout_seconds=args.iteration_timeout_seconds,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(redact_secrets(report), indent=2, sort_keys=True))
    summary = report.get("summary")
    return 0 if isinstance(summary, dict) and summary.get("passed") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
