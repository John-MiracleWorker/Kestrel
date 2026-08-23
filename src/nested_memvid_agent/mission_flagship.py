"""S8 golden flagship demonstrations (JOURNEY-003/005/006).

Deterministic, receipt-bound demonstrations that integrate the existing
Mission Control controls into one command-center flow:

- ``run_safe_shipping_demo`` (JOURNEY-005): proves the owner-rejected path
  creates no commit, the approved flagship creates the exact reviewed local
  commit, and the live pull-request path stays separately credentialed,
  approved, and disposable.
- ``run_two_task_lesson_demo`` (JOURNEY-006): proves that Task A produces a
  receipt-bound non-policy lesson, Task B (fresh session, same memory)
  retrieves the exact record and succeeds within the attempt bound, while a
  no-memory control fails or uses more failed attempts.
- ``publish_shipping_events``: the server-authored bridge that publishes the
  ``commit.created`` / ``ship.completed`` run events the mission proof reducer
  recognizes, so an approved local commit is visible in Mission Control's
  proof projection (JOURNEY-003).

Everything here is deterministic: mock provider, in-memory memory, and a
scratch Git repository. No remote mutation is ever attempted.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any

from .cognition import LessonManager
from .mission_control import _payload_digest
from .runtime_models import StrategyProposal, ToolCall, ToolExecution, ToolSpec
from .tools.base import AgentTool, ToolContext

SHIPPING_SCHEMA = "kestrel.flagship.shipping.v1"
LEARNING_SCHEMA = "kestrel.flagship.learning.v1"

# The run-step event types the mission proof reducer treats as durable
# shipping evidence. Publishing these is the server-authored way Mission
# Control learns a flagship shipped; the projection itself never infers
# authority from presentation state.
COMMIT_CREATED_EVENT = "commit.created"
SHIP_COMPLETED_EVENT = "ship.completed"


def publish_shipping_events(
    run_id: str,
    execution: ToolExecution | None,
    events: Any,
) -> dict[str, Any] | None:
    """Publish bounded shipping evidence when a ``git.commit`` tool succeeds.

    Returns the published ``commit.created`` payload (or ``None`` when the
    execution is not a successful local commit). The payload only carries
    bounded receipt/handle metadata — commit SHA, branch, message digest,
    repair review id — never raw content or secrets. ``ship.completed`` is
    published immediately after because an approved local commit completes the
    shipping step for that run.
    """
    if execution is None:
        return None
    call = getattr(execution, "call", None)
    if call is None or getattr(call, "name", None) != "git.commit":
        return None
    if getattr(execution, "success", False) is not True:
        return None
    data = dict(getattr(execution, "data", None) or {})
    commit_sha = str(data.get("commit_sha", "")).strip()
    if not commit_sha:
        return None
    message = str(getattr(call, "arguments", {}).get("message", "")).strip()
    payload: dict[str, Any] = {
        "schema": "kestrel.shipping_evidence.v1",
        "commit_sha": commit_sha,
        "branch": str(data.get("branch", "")).strip(),
        "message_digest": hashlib.sha256(message.encode("utf-8")).hexdigest(),
        "repair_review_id": str(data.get("repair_review_id", "")).strip(),
        "publish_event": COMMIT_CREATED_EVENT,
    }
    events.publish(run_id, COMMIT_CREATED_EVENT, payload)
    events.publish(run_id, SHIP_COMPLETED_EVENT, {"commit_sha": commit_sha})
    return payload


# ---------------------------------------------------------------------------
# JOURNEY-005 — safe shipping flagship
# ---------------------------------------------------------------------------


def run_safe_shipping_demo(
    *,
    workspace: Path,
    memory_dir: Path,
    registry: Any,
    memory: Any,
    events: Any,
    run_id: str,
    seed_files: dict[str, bytes] | None = None,
    protected_branches: tuple[str, ...] = ("main", "master", "release/*"),
) -> dict[str, Any]:
    """Run the deterministic safe-shipping flagship and return its receipt.

    The demonstration walks: seed failing repository -> repair prepare ->
    apply patch -> validate -> review -> owner decision (reject then approve)
    -> exact local commit -> PR path gating. It never touches a remote.
    """
    receipt: dict[str, Any] = {
        "schema": SHIPPING_SCHEMA,
        "run_id": run_id,
        "steps": [],
        "owner_rejected": {},
        "owner_approved": {},
        "pr_path": {},
    }

    def _record(step: str, **fields: Any) -> None:
        receipt["steps"].append({"step": step, **fields})

    # 1. Seed the repository (deterministic scratch repo).
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "core.autocrlf", "false")
    _git(workspace, "config", "user.name", "Kestrel Flagship")
    _git(workspace, "config", "user.email", "flagship@example.invalid")
    files = seed_files or {
        "calculator.py": b"def add(a, b):\n    return a - b\n",
        "test_calculator.py": (
            b"from calculator import add\n"
            b"def test_adds_numbers():\n"
            b"    assert add(2, 3) == 5\n"
        ),
    }
    for name, content in files.items():
        (workspace / name).write_bytes(content)
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "seed failing calculator")
    base_sha = _git_text(workspace, "rev-parse", "HEAD").strip()
    _record("seed", base_sha=base_sha, changed_files=sorted(files))

    # 2. Repair flow: prepare a repair branch.
    prepare = ToolCall(
        name="repair.prepare", arguments={"branch": "codex/flagship-repair"}, id="flagship_prepare"
    )
    prepared = registry.execute(prepare, _approved_context(memory, workspace, prepare))
    if not prepared.success:
        raise AssertionError(f"repair.prepare failed: {prepared.error} {prepared.content}")
    _record("repair_prepare", branch="codex/flagship-repair")

    # 3. Apply the reviewed patch.
    patch = (
        "diff --git a/calculator.py b/calculator.py\n"
        "--- a/calculator.py\n"
        "+++ b/calculator.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )
    patch_call = ToolCall(
        name="repair.apply_patch", arguments={"patch": patch}, id="flagship_patch"
    )
    patched = registry.execute(patch_call, _approved_context(memory, workspace, patch_call))
    if not patched.success:
        raise AssertionError(f"repair.apply_patch failed: {patched.error} {patched.content}")
    _record("repair_apply_patch")

    # 4. Validate.
    validate_call = ToolCall(
        name="repair.orchestrate_validate",
        arguments={"command": ["python", "-m", "pytest", "-q", "test_calculator.py"]},
        id="flagship_validate",
    )
    validation = registry.execute(
        validate_call,
        _approved_context(memory, workspace, validate_call, allow_shell=True),
    )
    if not validation.success or validation.data["validation"]["success"] is not True:
        raise AssertionError("flagship validation did not pass")
    _record("repair_validate", validation_id=validation.data["validation"]["validation_id"])

    # 5. Review (creates the exact-candidate commit gate).
    review_call = ToolCall(
        name="repair.review",
        arguments={
            "validation_id": validation.data["validation"]["validation_id"],
            "summary": "Flagship calculator repair validated by targeted pytest.",
        },
        id="flagship_review",
    )
    review = registry.execute(review_call, _approved_context(memory, workspace, review_call))
    if not review.success or review.data["commit_gate"]["commit_allowed"] is not True:
        raise AssertionError("flagship review did not open the commit gate")
    review_id = str(review.data["review_id"])
    diff_digest = str(review.data["diff_digest"])
    _record("repair_review", review_id=review_id, diff_digest=diff_digest)

    # 6. Owner-rejected path: without an approved exact-call grant the commit
    #    tool must refuse to create a commit, and the repository stays at base.
    commit_call = ToolCall(
        name="git.commit",
        arguments={"message": "fix calculator add", "repair_review_id": review_id},
        id="flagship_commit",
    )
    config = _flagship_config(workspace, memory_dir, protected_branches=protected_branches)
    rejected = registry.execute(
        commit_call,
        ToolContext(memory=memory, config=config, workspace=workspace),
    )
    head_after_reject = _git_text(workspace, "rev-parse", "HEAD").strip()
    rejected_no_commit = rejected.error == "approval_required" and head_after_reject == base_sha
    shipping_events_after_reject = [
        row
        for row in _event_log(events, run_id)
        if row.get("type") in {COMMIT_CREATED_EVENT, SHIP_COMPLETED_EVENT}
    ]
    receipt["owner_rejected"] = {
        "error": rejected.error,
        "head_unchanged": head_after_reject == base_sha,
        "no_commit": rejected_no_commit,
        "no_shipping_events": not shipping_events_after_reject,
    }
    _record(
        "owner_rejected",
        error=rejected.error,
        head_unchanged=head_after_reject == base_sha,
    )

    # 7. Owner-approved path: approve the exact-call grant and execute the
    #    commit; the reviewed local commit is created and shipping events are
    #    published through the server-authored bridge.
    approved = registry.execute(
        commit_call,
        _approved_context(memory, workspace, commit_call, config=config),
    )
    if not approved.success or not approved.data.get("commit_sha"):
        raise AssertionError(f"flagship commit failed: {approved.error} {approved.content}")
    commit_sha = str(approved.data["commit_sha"])
    head_after_approve = _git_text(workspace, "rev-parse", "HEAD").strip()
    status_after = _git_text(
        workspace, "status", "--porcelain", "--untracked-files=no"
    ).strip()
    published = publish_shipping_events(run_id, approved, events)
    receipt["owner_approved"] = {
        "commit_created": head_after_approve == commit_sha and head_after_approve != base_sha,
        "commit_sha": commit_sha,
        "working_tree_clean": status_after == "",
        "shipping_event_published": published is not None,
        "shipping_event_type": published["publish_event"] if published else None,
    }
    _record(
        "owner_approved",
        commit_sha=commit_sha,
        head_after_approve=head_after_approve,
        working_tree_clean=status_after == "",
    )

    # 8. Live PR path: the reviewed local commit is a precondition, but remote
    #    publication must stay separately credentialed and approved.
    pr_gated = _pr_path_gate_probe(
        workspace=workspace,
        memory=memory,
        memory_dir=memory_dir,
        registry=registry,
    )
    receipt["pr_path"] = pr_gated
    _record("pr_path", **pr_gated)

    receipt["passed"] = bool(
        receipt["owner_rejected"]["no_commit"]
        and receipt["owner_rejected"]["head_unchanged"]
        and receipt["owner_approved"]["commit_created"]
        and receipt["owner_approved"]["working_tree_clean"]
        and receipt["owner_approved"]["shipping_event_published"]
        and pr_gated["requires_separate_credential"]
        and pr_gated["approval_required"]
        and pr_gated["never_pushed"]
    )
    return receipt


def _pr_path_gate_probe(
    *,
    workspace: Path,
    memory: Any,
    memory_dir: Path,
    registry: Any,
) -> dict[str, Any]:
    """Prove the live PR path is separately credentialed, approved, and local-only.

    ``github.pr.create`` must refuse when Git push / remote mutation are not
    enabled (separate credential) and refuse an unapproved call even when both
    are enabled (approval gate). Nothing is pushed in this deterministic
    scratch: the repository has no remote, so any push is impossible.
    """
    pr_call = ToolCall(
        name="github.pr.create",
        arguments={"request_id": "pr-flagship", "expected_request_digest": "0" * 64},
        id="flagship_pr",
    )
    # Probe 1: no push/remote-mutation credential -> refused at the credential
    # gate before any remote work.
    uncredentialed = registry.execute(
        pr_call,
        ToolContext(
            memory=memory,
            config=_flagship_config(
                workspace, memory_dir, allow_git_push=False, allow_remote_mutation=False
            ),
            workspace=workspace,
        ),
    )
    # Probe 2: credentials enabled but no approval -> refused at the approval
    # gate before any remote work.
    credentialed = registry.execute(
        pr_call,
        ToolContext(
            memory=memory,
            config=_flagship_config(
                workspace, memory_dir, allow_git_push=True, allow_remote_mutation=True
            ),
            workspace=workspace,
        ),
    )
    remotes = _git_text(workspace, "remote", "-v").strip()
    # Without the separate Git push / remote-mutation credential the PR tool
    # is not executable at all (capability gate blocks it before its run()); a
    # credentialed call reaches the approval gate instead. Either way the
    # uncredentialed call must never reach approval, and nothing is pushed.
    uncredentialed_blocked = uncredentialed.error in {
        "tool_disabled",
        "github_remote_mutation_disabled",
    }
    return {
        "requires_separate_credential": uncredentialed_blocked,
        "approval_required": credentialed.error == "approval_required",
        "never_pushed": remotes == "",
        "uncredentialed_error": uncredentialed.error,
        "credentialed_error": credentialed.error,
    }


# ---------------------------------------------------------------------------
# JOURNEY-006 — two-task receipt-bound lesson-reuse/control demonstration
# ---------------------------------------------------------------------------


def run_two_task_lesson_demo(
    *,
    memory_dir: Path,
    lesson_record_id: str | None = None,
    task_a_objective: str = "Fix broad target failure scenario flagship.",
    task_b_objective: str = "Handle a similar broad target failure scenario flagship.",
    k: int = 5,
) -> dict[str, Any]:
    """Run the deterministic two-task receipt-bound lesson demonstration.

    Task A writes a receipt-bound non-policy lesson (LessonCard in the
    procedural layer, never policy). Task B (fresh agent session, same memory)
    recalls the exact record id and succeeds within the attempt bound, while a
    no-memory control (fresh memory, no lesson) fails or needs strictly more
    failed attempts.
    """
    from .models import MemoryLayer
    from .orchestrator import build_memory_system

    memory = build_memory_system("memory", memory_dir)
    lesson_manager = LessonManager(memory)

    receipt: dict[str, Any] = {
        "schema": LEARNING_SCHEMA,
        "task_a": {},
        "task_b": {},
        "control": {},
    }

    # ---- Task A: produce receipt-bound non-policy learning -----------------
    if lesson_record_id is None:
        lesson_record_id = _write_deterministic_lesson(
            lesson_manager,
            run_id="flagship_task_a",
            validation_call_id="validation-flagship-a",
        )
    record = memory.get_record(MemoryLayer.PROCEDURAL, lesson_record_id)
    if record is None:
        raise AssertionError("Task A did not persist the lesson record")
    policy_rows = list(memory.iter_records(MemoryLayer.POLICY))
    receipt["task_a"] = {
        "lesson_record_id": lesson_record_id,
        "procedural_layer": record.layer.value == MemoryLayer.PROCEDURAL.value,
        "cognition_schema": record.metadata.get("cognition_schema"),
        "non_policy": len(policy_rows) == 0,
    }

    # ---- Task B: fresh session, same memory, exact record + bound ----------
    task_b = _run_agent_with_recall(
        memory_dir=memory_dir,
        objective=task_b_objective,
        expected_lesson_id=lesson_record_id,
        session_id="flagship-task-b",
        run_id="flagship_run_b",
        k=k,
        apply_recalled_lesson=True,
    )
    receipt["task_b"] = task_b

    # ---- Control: no-memory, no lesson -> fails or uses more attempts ------
    control_memory_dir = memory_dir.parent / "no-memory-control"
    control = _run_agent_with_recall(
        memory_dir=control_memory_dir,
        objective=task_b_objective,
        expected_lesson_id=None,
        session_id="flagship-control",
        run_id="flagship_control",
        k=k,
        apply_recalled_lesson=False,
    )
    receipt["control"] = control

    receipt["passed"] = bool(
        receipt["task_a"]["non_policy"]
        and receipt["task_a"]["procedural_layer"]
        and receipt["task_b"]["exact_record_recalled"]
        and receipt["task_b"]["succeeded_within_bound"]
        and not receipt["control"]["exact_record_recalled"]
        and (
            not receipt["control"]["succeeded_within_bound"]
            or receipt["control"]["failed_attempts"] > receipt["task_b"]["failed_attempts"]
        )
    )
    return receipt


def _write_deterministic_lesson(
    lesson_manager: LessonManager,
    *,
    run_id: str,
    validation_call_id: str,
) -> str:
    """Write one deterministic receipt-bound non-policy lesson and return its id."""
    from .cognition import FailureEpisode
    from .diagnosis import classify_failure

    failure_execution = ToolExecution(
        call=ToolCall(
            name="fail.tool",
            arguments={"target": "same"},
            id="failure-flagship-a",
        ),
        success=False,
        content="AssertionError: expected fixed",
        error="tool_failed",
    )
    classification = classify_failure("AssertionError: expected fixed", source="tool:fail.tool")
    episode = FailureEpisode.from_tool_failure(
        run_id=run_id,
        execution=failure_execution,
        category=classification.category,
        diagnosis=str(classification.playbook.get("name", classification.category)),
        attempted_strategy="repeat the broad action unchanged",
    )
    validation_execution = ToolExecution(
        call=ToolCall(
            name="validation.check",
            arguments={"target": "focused"},
            id=validation_call_id,
        ),
        success=True,
        content="validation passed",
    )
    strategy = StrategyProposal(
        changed_strategy=(
            "STRATEGY_LESSON_FLAGSHIP validate the focused target instead of "
            "repeating the broad action"
        ),
        why_different="This checks a narrower signal.",
        expected_signal="The focused validation passes.",
        fallback_if_fails="Inspect the failure output before another retry.",
    )
    _lesson, record_id = lesson_manager.write_lesson_from_resolution(
        failure=episode,
        validation=validation_execution,
        strategy=strategy,
    )
    return str(record_id)


def _run_agent_with_recall(
    *,
    memory_dir: Path,
    objective: str,
    expected_lesson_id: str | None,
    session_id: str,
    run_id: str,
    k: int,
    apply_recalled_lesson: bool,
) -> dict[str, Any]:
    """Run a scripted agent turn and report recall + attempt outcomes.

    When ``apply_recalled_lesson`` is true the mock provider applies the
    recalled lesson immediately (validation succeeds on the first attempt, zero
    failed attempts). Otherwise it repeats the broad failure first (one failed
    attempt), proving the no-memory control uses strictly more failed attempts.
    """
    from .llm.mock import MockLLMProvider
    from .orchestrator import build_memory_system
    from .runtime_models import LLMResponse

    memory = build_memory_system("memory", memory_dir)
    if expected_lesson_id is not None:
        recalled = [
            row
            for row in _recall_rows(memory, objective=objective, k=k)
            if row.get("id") == expected_lesson_id
        ]
        exact_recalled = len(recalled) == 1
    else:
        exact_recalled = False

    registry = _lesson_demo_registry()
    if apply_recalled_lesson:
        provider = MockLLMProvider(
            [
                LLMResponse(
                    content="Apply the recalled lesson: validate the focused target.",
                    tool_calls=(
                        ToolCall(
                            name="validation.check",
                            arguments={"target": "focused"},
                            strategy=StrategyProposal(
                                changed_strategy=(
                                    "STRATEGY_LESSON_FLAGSHIP validate the focused target "
                                    "instead of repeating the broad action"
                                ),
                                why_different="The recalled lesson narrows the signal.",
                                expected_signal="The focused validation passes.",
                                fallback_if_fails=(
                                    "Inspect the failure output before another retry."
                                ),
                            ),
                        ),
                    ),
                ),
                LLMResponse(content="Validation passed."),
            ]
        )
    else:
        provider = MockLLMProvider(
            [
                LLMResponse(
                    content="First attempt.",
                    tool_calls=(ToolCall(name="fail.tool", arguments={"target": "same"}),),
                ),
                LLMResponse(
                    content="Retry the broad action unchanged.",
                    tool_calls=(ToolCall(name="fail.tool", arguments={"target": "same"}),),
                ),
                LLMResponse(content="Gave up without a validated strategy."),
            ]
        )
    result = _chat_with(registry, provider, memory_dir, objective, session_id, run_id)
    proof = result.proof_of_work
    failed_attempts = len(proof["failures"]) if proof is not None else 0
    applied_ids = {
        str(item.get("id", ""))
        for item in (proof["lessons_applied"] if proof is not None else [])
    }
    if apply_recalled_lesson:
        succeeded_within_bound = failed_attempts == 0 and (
            expected_lesson_id is None or expected_lesson_id in applied_ids
        )
    else:
        succeeded_within_bound = False
    return {
        "exact_record_recalled": exact_recalled,
        "failed_attempts": failed_attempts,
        "succeeded_within_bound": succeeded_within_bound,
        "lessons_applied": sorted(applied_ids),
    }


def _chat_with(
    registry: Any,
    provider: Any,
    memory_dir: Path,
    objective: str,
    session_id: str,
    run_id: str,
) -> Any:
    from .agent import AgentDependencies, NestedMV2Agent
    from .config import AgentConfig
    from .orchestrator import build_memory_system

    memory = build_memory_system("memory", memory_dir)
    return NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=provider,
            tools=registry,
            config=AgentConfig(memory_dir=memory_dir, log_dir=memory_dir.parent / "logs"),
        )
    ).chat(objective, session_id=session_id, run_id=run_id)


def _recall_rows(memory: Any, *, objective: str, k: int) -> list[dict[str, Any]]:
    manager = LessonManager(memory)
    return manager.preflight(objective=objective, k=k)


def _lesson_demo_registry() -> Any:
    from .tools.registry import ToolRegistry

    class _FailingTool(AgentTool):
        spec = ToolSpec(
            name="fail.tool",
            description="Always fails for flagship demonstration.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
        )

        def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
            del context
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
                success=False,
                content="AssertionError: expected fixed",
                error="tool_failed",
            )

    class _ValidationCheckTool(AgentTool):
        spec = ToolSpec(
            name="validation.check",
            description="Succeeds as a validation step.",
            parameters={"type": "object", "properties": {"target": {"type": "string"}}},
            produces_validation=True,
        )

        def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
            del context
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
                success=True,
                content="validation passed",
            )

    registry = ToolRegistry()
    registry.register(_FailingTool())
    registry.register(_ValidationCheckTool())
    return registry


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _flagship_config(
    workspace: Path,
    memory_dir: Path,
    *,
    protected_branches: tuple[str, ...] = ("main", "master", "release/*"),
    allow_git_push: bool = False,
    allow_remote_mutation: bool = False,
) -> Any:
    from .config import AgentConfig

    return AgentConfig(
        workspace=workspace,
        state_path=memory_dir.parent / "agent.db",
        memory_dir=memory_dir,
        log_dir=memory_dir.parent / "logs",
        secret_store_path=memory_dir.parent / "secrets.json",
        skills_dir=memory_dir.parent / "skills",
        plugins_dir=memory_dir.parent / "plugins",
        mcp_config_path=memory_dir.parent / "mcp.json",
        channel_config_path=memory_dir.parent / "channels.json",
        allow_file_write=True,
        allow_shell=True,
        allow_git_commit=True,
        allow_git_push=allow_git_push,
        allow_remote_mutation=allow_remote_mutation,
        git_write_mode="local_branch",
        protected_branches=protected_branches,
    )


def _approved_context(
    memory: Any,
    workspace: Path,
    call: ToolCall,
    *,
    allow_shell: bool = False,
    config: Any | None = None,
) -> ToolContext:
    if config is None:
        from .config import AgentConfig

        config = AgentConfig(
            allow_file_write=True,
            allow_shell=allow_shell,
            allow_git_commit=True,
            allow_memory_import=True,
        )
    return ToolContext(
        memory=memory,
        config=config,
        workspace=workspace,
        approved_tool_call_ids=frozenset({call.id}),
        approved_tool_call_arguments={call.id: call.arguments},
    )


def _git(workspace: Path, *arguments: str) -> None:
    subprocess.run(  # nosec - deterministic scratch repo commands
        ["git", "-C", str(workspace), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )


def _git_text(workspace: Path, *arguments: str) -> str:
    completed = subprocess.run(  # nosec - read-only git introspection
        ["git", "-C", str(workspace), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout or ""


def _event_log(events: Any, run_id: str) -> list[dict[str, Any]]:
    """Return the run-step events recorded for a run through a live bus."""
    try:
        rows = events.state.list_run_steps(run_id)
    except Exception:  # noqa: BLE001 - fake events expose a compatible API
        rows = list(getattr(events, "recorded_events", []) or [])
    return [
        row if isinstance(row, dict) else {"type": row.type, "payload": row.payload}
        for row in rows
    ]


def binding_digest_of(binding: dict[str, Any]) -> str:
    """Deterministic binding digest helper reused by flagship tests."""
    return str(_payload_digest(binding))
