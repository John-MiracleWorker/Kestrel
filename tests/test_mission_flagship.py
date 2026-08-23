"""S8 golden flagship tests (JOURNEY-005 / JOURNEY-006).

Deterministic demonstrations:

- JOURNEY-005 ``test_safe_shipping_flagship`` — owner-rejected path creates no
  commit, approved flagship creates the exact reviewed local commit, and the
  live PR path stays separately credentialed, approved, and local-only.
- JOURNEY-006 ``test_two_task_lesson_reuse_and_control`` — Task A produces a
  receipt-bound non-policy lesson, Task B retrieves the exact record and
  succeeds within the bound, and a no-memory control fails or uses more failed
  attempts.

Both are deterministic: mock provider, in-memory memory, scratch Git repo, and
the documented local OCI-validation stub used by the repair suite.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.extension_runner import (
    ContainerExecutionRequest,
    ContainerExecutionResult,
)
from nested_memvid_agent.tools import process_tools
from nested_memvid_agent.validation_runner import (
    IsolatedValidationResult,
)
from nested_memvid_agent.validation_runner import (
    run_isolated_validation as run_real_isolated_validation,
)


@pytest.fixture(autouse=True)
def _isolated_repair_validation_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep flagship repair unit tests deterministic (real OCI is integration-only)."""

    class LocalUnitRunner:
        def run(self, request: ContainerExecutionRequest) -> ContainerExecutionResult:
            normalized = list(request.command)
            if normalized and Path(normalized[0]).name.casefold().startswith("python"):
                normalized[0] = sys.executable
            environment = {"PATH": os.defpath}
            for name in (
                "COMSPEC",
                "PATHEXT",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "TMPDIR",
                "WINDIR",
            ):
                if value := os.environ.get(name):
                    environment[name] = value
            completed = subprocess.run(  # noqa: S603  # nosec B603
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
                content="Container execution completed.",
                error=None if completed.returncode == 0 else "container_nonzero_exit",
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
    ) -> IsolatedValidationResult:
        del image, runner
        return run_real_isolated_validation(
            workspace=workspace,
            image="example.invalid/kestrel-validation@sha256:" + "a" * 64,
            command=command,
            timeout_seconds=timeout_seconds,
            expected_repair_snapshot=expected_repair_snapshot,
            runner=LocalUnitRunner(),
        )

    monkeypatch.setattr(process_tools, "run_isolated_validation", run_stub)


def _live_events(tmp_path: Path) -> tuple[Any, Any]:
    from nested_memvid_agent.event_bus import RunEventBus
    from nested_memvid_agent.state_store import AgentStateStore

    state = AgentStateStore(tmp_path / "agent.db")
    return state, RunEventBus(state)


def test_safe_shipping_flagship(tmp_path: Path) -> None:
    from nested_memvid_agent.mission_flagship import run_safe_shipping_demo
    from nested_memvid_agent.orchestrator import build_memory_system
    from nested_memvid_agent.tools.builtin import build_default_tools

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    state, events = _live_events(tmp_path)
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

    # Owner-rejected path creates no commit and stays at the seed HEAD.
    assert receipt["passed"] is True
    assert receipt["owner_rejected"]["no_commit"] is True
    assert receipt["owner_rejected"]["head_unchanged"] is True
    # Approved flagship creates the exact reviewed local commit and a clean tree.
    assert receipt["owner_approved"]["commit_created"] is True
    assert receipt["owner_approved"]["working_tree_clean"] is True
    # The approved commit's shipping evidence was published server-side.
    assert receipt["owner_approved"]["shipping_event_published"] is True
    assert receipt["owner_approved"]["shipping_event_type"] == "commit.created"
    # The live PR path stays separately credentialed, approved, and local-only.
    assert receipt["pr_path"]["requires_separate_credential"] is True
    assert receipt["pr_path"]["approval_required"] is True
    assert receipt["pr_path"]["never_pushed"] is True

    # The shipping events actually landed in the durable run-step log.
    types = {row["type"] for row in state.list_run_steps("flagship_run")}
    assert "commit.created" in types
    assert "ship.completed" in types


def test_safe_shipping_owner_rejection_leaves_no_commit(
    tmp_path: Path,
) -> None:
    """JOURNEY-005: an owner-rejected git.commit creates no commit at all."""
    from nested_memvid_agent.mission_flagship import run_safe_shipping_demo
    from nested_memvid_agent.orchestrator import build_memory_system
    from nested_memvid_agent.tools.builtin import build_default_tools

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    state, events = _live_events(tmp_path)
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
    # The rejection leg is proven inside the same deterministic run; a denied
    # exact-call approval must never reach the git history.
    assert receipt["owner_rejected"]["no_commit"] is True
    assert receipt["owner_rejected"]["head_unchanged"] is True


def test_mission_proof_shipping_goes_present_after_approved_commit(
    tmp_path: Path,
) -> None:
    """JOURNEY-003/005: the server-authored proof projection reports the
    shipping section ``present`` once an approved flagship commit's events land
    on the run — the UI derives that state from durable evidence, never from
    presentation."""
    from nested_memvid_agent.mission_flagship import run_safe_shipping_demo
    from nested_memvid_agent.mission_proof import build_mission_proof
    from nested_memvid_agent.orchestrator import build_memory_system
    from nested_memvid_agent.state_store import RunRecord
    from nested_memvid_agent.tools.builtin import build_default_tools

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    state, events = _live_events(tmp_path)
    state.create_run(
        run_id="flagship_run",
        message="Fix the failing calculator.",
        session_id="flagship",
        workspace=str(workspace),
        provider="mock",
        model="mock",
    )
    run_safe_shipping_demo(
        workspace=workspace,
        memory_dir=memory_dir,
        registry=build_default_tools(),
        memory=build_memory_system("memory", memory_dir),
        events=events,
        run_id="flagship_run",
    )
    run = state.get_run("flagship_run")
    assert isinstance(run, RunRecord)
    proof = build_mission_proof(
        state=state,
        run=run,
        routing_ledger=_empty_ledger(state),
        runs_dir=tmp_path / "runs",
    )
    shipping = proof["evidence"]["shipping"]
    assert shipping["status"] == "present"
    assert "commit.created" in shipping["evidence"]["events"]
    assert "ship.completed" in shipping["evidence"]["events"]
    # The projection exposes only bounded receipt metadata, never secrets.
    assert proof["schema"] == "kestrel.mission_proof.v1"


def _empty_ledger(state: Any) -> Any:
    from nested_memvid_agent.routing.ledger import RoutingLedger

    return RoutingLedger(state)


def test_two_task_lesson_reuse_and_control(tmp_path: Path) -> None:
    """JOURNEY-006: receipt-bound lesson reuse with a no-memory control."""
    from nested_memvid_agent.mission_flagship import run_two_task_lesson_demo

    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    receipt = run_two_task_lesson_demo(memory_dir=memory_dir)

    assert receipt["passed"] is True
    # Task A produced receipt-bound non-policy learning in the procedural layer.
    assert receipt["task_a"]["non_policy"] is True
    assert receipt["task_a"]["procedural_layer"] is True
    assert receipt["task_a"]["cognition_schema"] == "lesson_card.v1"
    assert receipt["task_a"]["lesson_record_id"]
    # Task B retrieved the exact record and succeeded within the bound.
    assert receipt["task_b"]["exact_record_recalled"] is True
    assert receipt["task_b"]["succeeded_within_bound"] is True
    assert receipt["task_b"]["failed_attempts"] == 0
    # The no-memory control could not retrieve the record and used strictly
    # more failed attempts (or failed outright).
    assert receipt["control"]["exact_record_recalled"] is False
    assert (
        not receipt["control"]["succeeded_within_bound"]
        or receipt["control"]["failed_attempts"] > receipt["task_b"]["failed_attempts"]
    )
