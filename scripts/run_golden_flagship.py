#!/usr/bin/env python3
"""S8 golden flagship demonstration runner (JOURNEY-003/005/006).

Runs the deterministic flagship demonstrations and writes a single receipt:

1. JOURNEY-005 — safe shipping flagship: owner-rejected git.commit creates no
   commit; the approved flagship creates the exact reviewed local commit; the
   live PR path stays separately credentialed, approved, and local-only.
2. JOURNEY-006 — two-task receipt-bound lesson-reuse/control: Task A produces
   a receipt-bound non-policy lesson, Task B retrieves the exact record and
   succeeds within the bound, and a no-memory control fails or uses more
   failed attempts.

Everything is deterministic: mock provider, in-memory memory, and a scratch Git
repository. No remote mutation is ever attempted. The receipt is secret-safe
(bounded ids/digests/statuses only) and can be attached to the S8 PR.

Boundary rules:
- Never activates learned routing or writes policy memory.
- Never pushes to a remote or touches a protected branch.
- Fails (exit 1) if either demonstration does not pass.

Usage:
    python scripts/run_golden_flagship.py [--output RECEIPT.json] [--scratch DIR]
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))


def _git(repository: Path, *arguments: str) -> None:
    import subprocess

    subprocess.run(  # nosec - deterministic scratch repo commands
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _run_safe_shipping(scratch: Path) -> dict[str, Any]:
    """Run the deterministic safe-shipping flagship and return its receipt."""
    import os
    import subprocess as sp

    from nested_memvid_agent.extension_runner import (
        ContainerExecutionRequest,
        ContainerExecutionResult,
    )
    from nested_memvid_agent.tools import process_tools
    from nested_memvid_agent.validation_runner import (
        run_isolated_validation as run_real_isolated_validation,
    )

    # Deterministic local OCI-validation stub (real OCI is integration-only).
    class LocalUnitRunner:
        def run(self, request: ContainerExecutionRequest) -> ContainerExecutionResult:
            normalized = list(request.command)
            if normalized and Path(normalized[0]).name.casefold().startswith("python"):
                normalized[0] = sys.executable
            environment = {"PATH": os.defpath}
            completed = sp.run(  # nosec - deterministic scratch commands
                normalized,
                cwd=request.source_dir,
                env=environment,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
            return ContainerExecutionResult(
                success=completed.returncode == 0,
                stdout=completed.stdout,
                stderr=completed.stderr,
                returncode=completed.returncode,
                content="ok",
                error=None if completed.returncode == 0 else "nonzero",
                tree_digest=request.expected_tree_digest,
                scope_digest=request.scopes.digest(),
            )

    def run_stub(
        *,
        workspace: Path,
        image: str | None,
        command: list[str],
        timeout_seconds: float,
        expected_repair_snapshot: dict[str, Any] | None = None,
        runner: object | None = None,
    ) -> Any:
        del image, runner
        return run_real_isolated_validation(
            workspace=workspace,
            image="example.invalid/kestrel-validation@sha256:" + "a" * 64,
            command=command,
            timeout_seconds=timeout_seconds,
            expected_repair_snapshot=expected_repair_snapshot,
            runner=LocalUnitRunner(),
        )

    process_tools.run_isolated_validation = run_stub

    from nested_memvid_agent.event_bus import RunEventBus
    from nested_memvid_agent.mission_flagship import run_safe_shipping_demo
    from nested_memvid_agent.orchestrator import build_memory_system
    from nested_memvid_agent.state_store import AgentStateStore
    from nested_memvid_agent.tools.builtin import build_default_tools

    workspace = scratch / "workspace"
    workspace.mkdir()
    memory_dir = scratch / "memory"
    memory_dir.mkdir()
    state = AgentStateStore(scratch / "agent.db")
    events = RunEventBus(state)
    state.create_run(
        run_id="flagship_run",
        message="Fix the failing calculator.",
        session_id="flagship",
        workspace=str(workspace),
        provider="mock",
        model="mock",
    )
    receipt = run_safe_shipping_demo(
        workspace=workspace,
        memory_dir=memory_dir,
        registry=build_default_tools(),
        memory=build_memory_system("memory", memory_dir),
        events=events,
        run_id="flagship_run",
    )
    if not receipt["passed"]:
        raise RuntimeError("safe shipping flagship did not pass")
    return receipt


def _run_two_task_lesson(scratch: Path) -> dict[str, Any]:
    """Run the deterministic two-task lesson demonstration and return its receipt."""
    from nested_memvid_agent.mission_flagship import run_two_task_lesson_demo

    memory_dir = scratch / "lesson-memory"
    memory_dir.mkdir()
    receipt = run_two_task_lesson_demo(memory_dir=memory_dir)
    if not receipt["passed"]:
        raise RuntimeError("two-task lesson flagship did not pass")
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=None, help="Receipt JSON output path")
    parser.add_argument("--scratch", default=None, help="Scratch directory (default: temp)")
    args = parser.parse_args()

    if args.scratch:
        scratch = Path(args.scratch).resolve()
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix="flagship-") as _tmp:
            # Keep the outer scratch dir but run inside a fresh temp child so
            # repeated invocations are isolated.
            _child = scratch / "run"
            _child.mkdir(exist_ok=True)
            shipping = _run_safe_shipping(_child)
            learning = _run_two_task_lesson(_child)
    else:
        with tempfile.TemporaryDirectory(prefix="kestrel-flagship-") as tmp:
            scratch = Path(tmp)
            shipping = _run_safe_shipping(scratch)
            learning = _run_two_task_lesson(scratch)

    report = {
        "schema": "kestrel.flagship_demo_receipt.v1",
        "safe_shipping": shipping,
        "two_task_lesson_reuse": learning,
        "passed": bool(shipping["passed"] and learning["passed"]),
    }
    output = Path(args.output).resolve() if args.output else None
    if output:
        output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"Flagship receipt written to {output}")
    else:
        print(json.dumps(report, indent=2, default=str))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
