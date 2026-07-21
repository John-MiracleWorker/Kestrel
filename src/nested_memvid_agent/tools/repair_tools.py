from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess  # nosec B404
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from ..cognition import RetryPolicy
from ..diagnosis import classify_failure
from ..platform_primitives import chmod_descriptor
from ..private_directory import (
    create_owner_private_directory,
    validate_owner_private_directory,
)
from ..repair_integrity import (
    load_review_receipt,
    load_validation_receipt,
    repair_action_lock,
    repair_snapshot,
    require_git_root,
    utc_now,
    write_repair_artifact,
    write_validation_receipt,
)
from ..runtime_models import StrategyProposal, ToolCall, ToolExecution, ToolSpec
from ..security_boundary import redact_secrets, redact_text
from ..validation_runner import IsolatedValidationResult, ValidationIsolationError
from .base import AgentTool, ToolContext
from .command_tools import _is_allowlisted_command, _tool_call_from_runtime_arguments
from .diagnosis_tools import _recall_failure_lessons, _recall_hit_titles
from .git_tools import (
    _changed_files_from_status,
    _git_output,
    _hardened_git_command,
    _is_repair_branch,
    _safe_branch_name,
    _sanitized_git_environment,
)
from .patch_helpers import _validate_patch_paths
from .process_tools import (
    WorkspaceSecretIsolationError,
    _assert_repair_snapshot_workspace_safe,
    _cancel_running_subprocess,
    _normalize_python_command,
    _run_subprocess,
    _subprocess_outcome_unresolved,
    _SubprocessToolOutcomeIndeterminate,
    _SubprocessToolTimeout,
)
from .validation_helpers import (
    _authenticated_validation_evidence_payload,
    _merge_validation_evidence_payloads,
    _tool_validation_evidence_payload,
)
from .workspace_tools import _assert_workspace_path_allowed


class RepairPrepareTool(AgentTool):
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="repair.prepare",
        description=(
            "Confirm an existing scheduler-managed repair worktree, or prepare a local repair "
            "branch from a clean workspace. Records the base SHA and requires approval."
        ),
        parameters={
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "allow_dirty": {"type": "boolean"},
            },
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "git-isolation"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        requested_branch = str(arguments.get("branch", "")).strip()
        if requested_branch and not _safe_branch_name(requested_branch):
            return self._result(
                call,
                success=False,
                content=f"Unsafe branch name: {requested_branch}",
                error="unsafe_branch_name",
            )
        try:
            root = require_git_root(context.workspace)
            base = subprocess.run(  # nosec
                _hardened_git_command(["rev-parse", "HEAD"], workspace=root),
                cwd=root,
                env=_sanitized_git_environment(None),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if base.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=redact_text(f"Unable to resolve base SHA. STDERR:\n{base.stderr}"),
                    error="git_base_failed",
                    data={"returncode": base.returncode},
                )
            status = subprocess.run(  # nosec
                _hardened_git_command(["status", "--porcelain"], workspace=root),
                cwd=root,
                env=_sanitized_git_environment(None),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if status.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=redact_text(f"Unable to inspect worktree. STDERR:\n{status.stderr}"),
                    error="git_status_failed",
                    data={"returncode": status.returncode},
                )
            if status.stdout.strip() and not bool(arguments.get("allow_dirty", False)):
                return self._result(
                    call,
                    success=False,
                    content="Refusing to prepare repair branch with uncommitted changes.",
                    error="dirty_worktree",
                    data={"dirty_status": redact_text(status.stdout)},
                )
            current_branch = _git_output(root, ["git", "branch", "--show-current"])
            git_marker = root / ".git"
            if (
                git_marker.is_file()
                and not git_marker.is_symlink()
                and _is_repair_branch(current_branch, context.config.worker_branch_prefix)
            ):
                payload = {
                    "mode": "git-worktree",
                    "branch": current_branch,
                    "requested_branch": requested_branch or None,
                    "base_sha": base.stdout.strip(),
                    "returncode": 0,
                    "managed_worktree": True,
                    "approval_required_before_commit": True,
                }
                return self._result(
                    call,
                    success=True,
                    content=(
                        "Confirmed scheduler-managed repair worktree on "
                        f"{current_branch} at {base.stdout.strip()}."
                    ),
                    data=payload,
                )
            if not requested_branch:
                return self._result(
                    call,
                    success=False,
                    content="Missing branch outside a managed repair worktree.",
                    error="missing_branch",
                )
            created = subprocess.run(  # nosec
                _hardened_git_command(["switch", "-c", requested_branch], workspace=root),
                cwd=root,
                env=_sanitized_git_environment(None),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = redact_text(
                f"exit_code={created.returncode}\nSTDOUT:\n{created.stdout}\n"
                f"STDERR:\n{created.stderr}"
            )
            success = created.returncode == 0
            return self._result(
                call,
                success=success,
                content=content,
                data={
                    "mode": "branch",
                    "branch": requested_branch,
                    "base_sha": base.stdout.strip(),
                    "returncode": created.returncode,
                    "approval_required_before_commit": True,
                },
                error=None if success else "repair_prepare_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repair_prepare_failed",
            )


class RepairStatusTool(AgentTool):
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="repair.status",
        description="Report whether the workspace is on a repair branch, changed files, and optional base SHA trace metadata.",
        parameters={
            "type": "object",
            "properties": {"base_sha": {"type": "string"}},
        },
        capabilities=("safe-repair", "git-isolation"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            root = require_git_root(context.workspace)
            branch = _git_output(root, ["git", "branch", "--show-current"])
            head = _git_output(root, ["git", "rev-parse", "HEAD"])
            status = _git_output(root, ["git", "status", "--porcelain"])
            changed_files = _changed_files_from_status(status)
            _assert_repair_snapshot_workspace_safe(context)
            snapshot = repair_snapshot(root)
            _assert_repair_snapshot_paths_allowed(context, root, snapshot)
            base_sha = str(arguments.get("base_sha", "")).strip() or None
            payload = {
                "branch": branch,
                "head_sha": head,
                "base_sha": base_sha,
                "active_repair_branch": _is_repair_branch(
                    branch, context.config.worker_branch_prefix
                ),
                "dirty": bool(status.strip()),
                "changed_files": changed_files,
                "raw_status": status,
                "diff_digest": snapshot["diff_digest"],
                "repair_snapshot": snapshot,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="workspace_secret_isolation_required",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repair_status_failed",
            )


class RepairApplyPatchTool(AgentTool):
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="repair.apply_patch",
        description="Apply a repair patch only while on an active repair branch. Requires approval and file-write capability.",
        parameters={
            "type": "object",
            "properties": {"patch": {"type": "string"}, "check": {"type": "boolean"}},
            "required": ["patch"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "patching"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        patch_text = str(arguments.get("patch", ""))
        if not patch_text.strip():
            return self._result(call, success=False, content="Missing patch", error="missing_patch")
        try:
            root = require_git_root(context.workspace)
            with repair_action_lock(root):
                branch = _git_output(root, ["git", "branch", "--show-current"])
                if not _is_repair_branch(branch, context.config.worker_branch_prefix):
                    return self._result(
                        call,
                        success=False,
                        content=f"Refusing to apply repair patch on non-repair branch: {branch}",
                        error="not_repair_branch",
                        data={"branch": branch},
                    )
                _validate_patch_paths(root, patch_text, context=context)
                command = (
                    ["git", "apply", "--check"]
                    if bool(arguments.get("check", False))
                    else ["git", "apply", "--whitespace=nowarn"]
                )
                completed = subprocess.run(  # nosec
                    _hardened_git_command(command[1:], workspace=root),
                    cwd=root,
                    env=_sanitized_git_environment(None),
                    input=patch_text,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
            content = redact_text(
                f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "branch": branch,
                    "returncode": completed.returncode,
                    "check": bool(arguments.get("check", False)),
                },
                error=None if completed.returncode == 0 else "repair_patch_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repair_patch_failed",
            )


class RepairValidateTool(AgentTool):
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="repair.validate",
        description="Run a bounded repair validation command on an active repair branch and classify failures.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
                "subject_record_id": {"type": "string"},
            },
            "required": ["command"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "validation", "self-diagnosis"),
        produces_validation=True,
    )
    allowed_first_tokens = {"pytest", "python", "python3", "ruff", "mypy"}
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call_from_runtime_arguments(self.spec.name, arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(
            isinstance(item, str) for item in command_raw
        ):
            return self._result(
                call, success=False, content="command must be list[str]", error="bad_command"
            )
        command = list(command_raw)
        if not _is_allowlisted_command(command, self.allowed_first_tokens):
            return self._result(
                call,
                success=False,
                content="Command is not allowlisted",
                error="command_not_allowlisted",
            )
        command = _normalize_python_command(command)
        started_at = utc_now()
        try:
            root = require_git_root(context.workspace)
            with repair_action_lock(root):
                branch = _git_output(root, ["git", "branch", "--show-current"])
                if not _is_repair_branch(branch, context.config.worker_branch_prefix):
                    return self._result(
                        call,
                        success=False,
                        content=f"Not on a repair branch: {branch}",
                        error="not_repair_branch",
                    )
                before_snapshot = repair_snapshot(root)
                _assert_repair_snapshot_paths_allowed(context, root, before_snapshot)
                with tempfile.TemporaryDirectory(prefix="kestrel-validation-") as cache_dir:
                    completed = _run_subprocess(
                        command,
                        context=context,
                        arguments=arguments,
                        default_timeout=120,
                        sanitize_environment=True,
                        environment_overrides=_repair_validation_environment(Path(cache_dir)),
                        requires_workspace_secret_isolation=True,
                        require_container_isolation=True,
                        expected_repair_snapshot=before_snapshot,
                    )
                if not isinstance(completed, IsolatedValidationResult):
                    raise ValidationIsolationError(
                        "validation_container_attestation_missing",
                        "Repair validation did not return an OCI isolation attestation.",
                    )
                raw_content = (
                    f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
                    f"STDERR:\n{completed.stderr}"
                )
                content = redact_text(raw_content)
                after_snapshot = repair_snapshot(root)
                drift_fields = _snapshot_drift_fields(before_snapshot, after_snapshot)
                validation_success = completed.returncode == 0 and not drift_fields
                if drift_fields:
                    content += (
                        "\nValidation candidate changed while the command was running; "
                        f"receipt is failed/stale ({', '.join(drift_fields)})."
                    )
                diagnosis = (
                    classify_failure(content, source="repair.validate").to_payload()
                    if not validation_success
                    else None
                )
                validation_evidence = _tool_validation_evidence_payload(
                    "repair_refs",
                    "repair.validate",
                    command,
                    content,
                    validation_success,
                )
                receipt = write_validation_receipt(
                    root,
                    tool_name="repair.validate",
                    command=command,
                    success=validation_success,
                    returncode=completed.returncode,
                    content=content,
                    validation_evidence=validation_evidence,
                    snapshot=after_snapshot,
                    started_at=started_at,
                    isolation_attestation=completed.isolation,
                )
            if validation_success:
                runtime_receipt_id = context.memory.put_runtime_validation_receipt(
                    tool_name=self.spec.name,
                    tool_call_id=call.id,
                    evidence_bucket="repair",
                    command=command,
                    output_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    session_id=context.session_id,
                    run_id=context.run_id,
                    signed_artifact_source="repair.validate",
                    signed_artifact_locator=str(receipt["validation_id"]),
                    subject_record_id=str(arguments.get("subject_record_id") or "").strip() or None,
                )
                validation_evidence = _authenticated_validation_evidence_payload(
                    "repair_refs",
                    receipt_id=runtime_receipt_id,
                    quote=content,
                    source_evidence_chars=len(content),
                )
            safe_command = redact_secrets(command)
            return self._result(
                call,
                success=validation_success,
                content=content,
                data={
                    "branch": branch,
                    "returncode": completed.returncode,
                    "diagnosis": diagnosis,
                    "validation_id": receipt["validation_id"],
                    "repair_snapshot": after_snapshot,
                    "validation_started_snapshot": before_snapshot,
                    "validation_drift_fields": drift_fields,
                    "validation_evidence": validation_evidence,
                    "command": safe_command,
                },
                error=None if validation_success else "repair_validation_failed",
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except ValidationIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except _SubprocessToolOutcomeIndeterminate as exc:
            return _subprocess_outcome_unresolved(call, exc)
        except _SubprocessToolTimeout as exc:
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="tool_timeout"
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repair_validation_failed",
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class RepairOrchestrateValidateTool(AgentTool):
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="repair.orchestrate_validate",
        description="Run repair validation on an active repair branch, classify failures, recall prior lessons, and gate unchanged retries.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
                "previous_command": {"type": "array", "items": {"type": "string"}},
                "proposed_strategy": {"type": "string"},
                "k": {"type": "integer", "minimum": 1, "maximum": 10},
                "subject_record_id": {"type": "string"},
            },
            "required": ["command"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "validation", "self-diagnosis", "failure-recall"),
        produces_validation=True,
    )
    allowed_first_tokens = RepairValidateTool.allowed_first_tokens
    needs_call_id = True

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call_from_runtime_arguments(self.spec.name, arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(
            isinstance(item, str) for item in command_raw
        ):
            return self._result(
                call, success=False, content="command must be list[str]", error="bad_command"
            )
        command = list(command_raw)
        if not _is_allowlisted_command(command, self.allowed_first_tokens):
            return self._result(
                call,
                success=False,
                content="Command is not allowlisted",
                error="command_not_allowlisted",
            )
        command = _normalize_python_command(command)
        started_at = utc_now()
        try:
            root = require_git_root(context.workspace)
            with repair_action_lock(root):
                branch = _git_output(root, ["git", "branch", "--show-current"])
                if not _is_repair_branch(branch, context.config.worker_branch_prefix):
                    return self._result(
                        call,
                        success=False,
                        content=f"Not on a repair branch: {branch}",
                        error="not_repair_branch",
                        data={"branch": branch},
                    )
                before_snapshot = repair_snapshot(root)
                _assert_repair_snapshot_paths_allowed(context, root, before_snapshot)
                with tempfile.TemporaryDirectory(prefix="kestrel-validation-") as cache_dir:
                    completed = _run_subprocess(
                        command,
                        context=context,
                        arguments=arguments,
                        default_timeout=120,
                        sanitize_environment=True,
                        environment_overrides=_repair_validation_environment(Path(cache_dir)),
                        requires_workspace_secret_isolation=True,
                        require_container_isolation=True,
                        expected_repair_snapshot=before_snapshot,
                    )
                if not isinstance(completed, IsolatedValidationResult):
                    raise ValidationIsolationError(
                        "validation_container_attestation_missing",
                        "Repair validation did not return an OCI isolation attestation.",
                    )
                validation_content = redact_text(
                    f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
                    f"STDERR:\n{completed.stderr}"
                )
                snapshot = repair_snapshot(root)
                drift_fields = _snapshot_drift_fields(before_snapshot, snapshot)
                validation_success = completed.returncode == 0 and not drift_fields
                if drift_fields:
                    validation_content += (
                        "\nValidation candidate changed while the command was running; "
                        f"receipt is failed/stale ({', '.join(drift_fields)})."
                    )
                validation_evidence = _tool_validation_evidence_payload(
                    "repair_refs",
                    "repair.orchestrate_validate",
                    command,
                    validation_content,
                    validation_success,
                )
                receipt = write_validation_receipt(
                    root,
                    tool_name="repair.orchestrate_validate",
                    command=command,
                    success=validation_success,
                    returncode=completed.returncode,
                    content=validation_content,
                    validation_evidence=validation_evidence,
                    snapshot=snapshot,
                    started_at=started_at,
                    isolation_attestation=completed.isolation,
                )
            if validation_success:
                runtime_receipt_id = context.memory.put_runtime_validation_receipt(
                    tool_name=self.spec.name,
                    tool_call_id=call.id,
                    evidence_bucket="repair",
                    command=command,
                    output_sha256=hashlib.sha256(validation_content.encode("utf-8")).hexdigest(),
                    session_id=context.session_id,
                    run_id=context.run_id,
                    signed_artifact_source="repair.validate",
                    signed_artifact_locator=str(receipt["validation_id"]),
                    subject_record_id=str(arguments.get("subject_record_id") or "").strip() or None,
                )
                validation_evidence = _authenticated_validation_evidence_payload(
                    "repair_refs",
                    receipt_id=runtime_receipt_id,
                    quote=validation_content,
                    source_evidence_chars=len(validation_content),
                )
            safe_command = redact_secrets(command)
            validation = {
                "success": validation_success,
                "returncode": completed.returncode,
                "command": safe_command,
                "content": validation_content,
                "validation_id": receipt["validation_id"],
                "repair_snapshot": snapshot,
                "validation_started_snapshot": before_snapshot,
                "validation_drift_fields": drift_fields,
                "validation_evidence": validation_evidence,
            }
            diagnosis = None
            recall: dict[str, Any] = {
                "hits": [],
                "query": "",
                "retry_guidance": {"must_change_strategy_before_retry": False},
            }
            retry_gate: dict[str, Any] = {
                "retry_allowed": True,
                "must_change_strategy_before_retry": False,
                "reason": "Validation passed; no retry needed."
                if validation_success
                else "No similar lesson was found; follow the diagnostic playbook.",
                "strategy_changed": True,
            }
            next_action = (
                "create_repair_review_before_commit"
                if validation_success
                else "retry_with_diagnostic_playbook"
            )
            if not validation_success:
                classification = classify_failure(
                    validation_content, source="repair.orchestrate_validate"
                )
                diagnosis = classification.to_payload()
                recall = _recall_failure_lessons(
                    context,
                    classification.category,
                    validation_content,
                    max(1, min(int(arguments.get("k", 5)), 10)),
                )
                previous = arguments.get("previous_command")
                previous_command = (
                    previous
                    if isinstance(previous, list)
                    and all(isinstance(item, str) for item in previous)
                    else []
                )
                previous_command = _normalize_python_command(list(previous_command))
                proposed_strategy = str(arguments.get("proposed_strategy", "")).strip()
                has_lessons = bool(recall["hits"])
                command_repeated = previous_command == command
                must_change = has_lessons and command_repeated
                strategy = (
                    StrategyProposal(changed_strategy=proposed_strategy)
                    if proposed_strategy
                    else None
                )
                retry_decision = RetryPolicy().assess_actions(
                    previous_action=" ".join(previous_command),
                    new_action=" ".join(command),
                    strategy=strategy,
                    require_change=must_change,
                    similar_lessons=_recall_hit_titles(recall),
                )
                retry_allowed = retry_decision.retry_allowed
                retry_gate = {
                    "retry_allowed": retry_allowed,
                    "must_change_strategy_before_retry": must_change,
                    "strategy_changed": bool(
                        retry_decision.strategy_diff
                        and retry_decision.strategy_diff.is_meaningfully_different
                    ),
                    "command_repeated": command_repeated,
                    "reason": retry_decision.reason,
                    "required_change": retry_decision.required_change,
                    "strategy_diff": retry_decision.strategy_diff.to_payload()
                    if retry_decision.strategy_diff
                    else None,
                }
                next_action = (
                    "apply_changed_strategy_then_retry"
                    if retry_allowed and must_change
                    else "change_strategy_before_retry"
                    if not retry_allowed
                    else "retry_with_diagnostic_playbook"
                )
            payload = {
                "branch": branch,
                "active_repair_branch": True,
                "changed_files": snapshot["changed_files"],
                "validation": validation,
                "diagnosis": diagnosis,
                "recall": recall,
                "retry_gate": retry_gate,
                "next_action": next_action,
                "commit_allowed": False,
                "approval_required_before_commit": True,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except ValidationIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error=exc.code,
            )
        except _SubprocessToolOutcomeIndeterminate as exc:
            return _subprocess_outcome_unresolved(call, exc)
        except _SubprocessToolTimeout as exc:
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="tool_timeout"
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repair_orchestration_failed",
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class RepairReviewTool(AgentTool):
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="repair.review",
        description="Create a durable reviewer gate artifact for a validated repair diff before commit.",
        parameters={
            "type": "object",
            "properties": {
                "validation_id": {"type": "string"},
                "summary": {"type": "string"},
                "risks": {"type": "array", "items": {"type": "string"}},
                "subject_record_id": {"type": "string"},
            },
            "required": ["validation_id"],
        },
        risk="medium",
        requires_approval=True,
        capabilities=("safe-repair", "review-gate"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = _tool_call_from_runtime_arguments(self.spec.name, arguments)
        validation_id = str(arguments.get("validation_id", "")).strip()
        if not validation_id:
            return self._result(
                call,
                success=False,
                content="repair.review requires a validation_id emitted by a repair validation tool.",
                error="validation_receipt_required",
            )
        try:
            root = require_git_root(context.workspace)
            with repair_action_lock(root):
                try:
                    validation = load_validation_receipt(root, validation_id)
                except FileNotFoundError:
                    return self._result(
                        call,
                        success=False,
                        content=f"Repair validation receipt not found: {validation_id}",
                        error="validation_receipt_not_found",
                    )
                except ValueError as exc:
                    return self._result(
                        call,
                        success=False,
                        content=redact_text(str(exc)),
                        error="validation_receipt_invalid",
                    )
                if validation.get("success") is not True:
                    return self._result(
                        call,
                        success=False,
                        content=f"Repair validation did not pass: {validation_id}",
                        error="validation_not_successful",
                    )
                branch = _git_output(root, ["git", "branch", "--show-current"])
                if not _is_repair_branch(branch, context.config.worker_branch_prefix):
                    return self._result(
                        call,
                        success=False,
                        content=f"Not on a repair branch: {branch}",
                        error="not_repair_branch",
                        data={"branch": branch},
                    )
                _assert_repair_snapshot_workspace_safe(context)
                snapshot = repair_snapshot(root)
                _assert_repair_snapshot_paths_allowed(context, root, snapshot)
                validated_snapshot = validation.get("repair_snapshot")
                if not isinstance(validated_snapshot, dict):
                    return self._result(
                        call,
                        success=False,
                        content="Repair validation receipt has no candidate fingerprint.",
                        error="validation_receipt_invalid",
                    )
                drift_fields = _snapshot_drift_fields(validated_snapshot, snapshot)
                if drift_fields:
                    return self._result(
                        call,
                        success=False,
                        content=(
                            "Repair changed after validation; validate the current candidate again "
                            f"before review. Drift: {', '.join(drift_fields)}"
                        ),
                        error="validation_receipt_stale",
                        data={
                            "validation_id": validation_id,
                            "drift_fields": drift_fields,
                            "validated_diff_digest": validated_snapshot.get("diff_digest"),
                            "current_diff_digest": snapshot.get("diff_digest"),
                        },
                    )
                if snapshot["empty"]:
                    return self._result(
                        call,
                        success=False,
                        content="No repair diff found to review.",
                        error="empty_repair_diff",
                        data={"branch": branch},
                    )
                created_at = utc_now()
                review_seed = json.dumps(
                    {
                        "validation_id": validation_id,
                        "diff_digest": snapshot["diff_digest"],
                        "created_at": created_at,
                        "nonce": os.urandom(16).hex(),
                    },
                    sort_keys=True,
                )
                review_id = (
                    "repair_review_" + hashlib.sha256(review_seed.encode("utf-8")).hexdigest()[:24]
                )
                risks_arg = arguments.get("risks")
                raw_risks = [str(item) for item in risks_arg] if isinstance(risks_arg, list) else []
                safe_risks = redact_secrets(raw_risks)
                risks = safe_risks if isinstance(safe_risks, list) else []
                summary = redact_text(str(arguments.get("summary", "")).strip())
                validation_evidence = _merge_validation_evidence_payloads(
                    validation.get("validation_evidence"),
                    {
                        "review_refs": [
                            {
                                "source": "repair.review",
                                "locator": review_id,
                                "quote": summary[:240],
                            }
                        ],
                        "source_evidence_chars": len(summary),
                    },
                )
                payload = {
                    "schema_version": 2,
                    "review_id": review_id,
                    "validation_id": validation_id,
                    "branch": branch,
                    "head_sha": snapshot["head_sha"],
                    "diff_hash": snapshot["diff_digest"],
                    "diff_digest": snapshot["diff_digest"],
                    "changed_files": snapshot["changed_files"],
                    "repair_snapshot": snapshot,
                    "summary": summary,
                    "risks": risks,
                    "created_at": created_at,
                    "validation": {
                        "validation_id": validation_id,
                        "tool": validation.get("tool"),
                        "command": validation.get("command"),
                        "success": True,
                        "returncode": validation.get("returncode"),
                        "output_sha256": validation.get("output_sha256"),
                    },
                    "validation_evidence": validation_evidence,
                    "commit_gate": {
                        "commit_allowed": True,
                        "approval_required_before_commit": True,
                        "reason": (
                            "A signed successful validation receipt is bound to the exact current "
                            "repair fingerprint; commit still requires exact-call approval."
                        ),
                    },
                }
                write_repair_artifact(root, "repair_reviews", review_id, payload)
            runtime_receipt_id = context.memory.put_runtime_validation_receipt(
                tool_name=self.spec.name,
                tool_call_id=call.id,
                evidence_bucket="review",
                command=(review_id,),
                output_sha256=hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode("utf-8")
                ).hexdigest(),
                session_id=context.session_id,
                run_id=context.run_id,
                signed_artifact_source="repair.review",
                signed_artifact_locator=review_id,
                subject_record_id=str(arguments.get("subject_record_id") or "").strip() or None,
            )
            payload["runtime_validation_evidence"] = _authenticated_validation_evidence_payload(
                "review_refs",
                receipt_id=runtime_receipt_id,
                quote=summary,
                source_evidence_chars=len(summary),
            )
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="workspace_secret_isolation_required",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repair_review_failed",
            )


class RepairRollbackTool(AgentTool):
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="repair.rollback",
        description=(
            "Rollback only the files captured by a trusted repair validation or review receipt. "
            "Requires approval and preserves unrelated workspace files."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "review_id": {"type": "string"},
                "validation_id": {"type": "string"},
                "expected_current_diff_digest": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": ["expected_current_diff_digest"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "rollback"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        review_id = str(arguments.get("review_id", "")).strip()
        validation_id = str(arguments.get("validation_id", "")).strip()
        expected_digest = str(arguments.get("expected_current_diff_digest", "")).strip()
        if bool(review_id) == bool(validation_id):
            return self._result(
                call,
                success=False,
                content="Provide exactly one trusted review_id or validation_id to scope rollback.",
                error="rollback_receipt_required",
            )
        if len(expected_digest) != 64 or any(
            char not in "0123456789abcdef" for char in expected_digest
        ):
            return self._result(
                call,
                success=False,
                content=(
                    "repair.rollback requires the exact current diff digest so approval is bound "
                    "to the state that will be changed."
                ),
                error="rollback_snapshot_required",
            )

        artifact_id: str | None = None
        root: Path | None = None
        quarantine_relpath: str | None = None
        try:
            root = require_git_root(context.workspace)
            reason = redact_text(
                str(arguments.get("reason", "manual_rollback")).strip() or "manual_rollback"
            )
            with repair_action_lock(root):
                try:
                    receipt = (
                        load_review_receipt(root, review_id)
                        if review_id
                        else load_validation_receipt(root, validation_id)
                    )
                except FileNotFoundError:
                    receipt_id = review_id or validation_id
                    return self._result(
                        call,
                        success=False,
                        content=f"Repair rollback receipt not found: {receipt_id}",
                        error="rollback_receipt_not_found",
                    )
                except ValueError as exc:
                    return self._result(
                        call,
                        success=False,
                        content=redact_text(str(exc)),
                        error="rollback_receipt_invalid",
                    )
                branch = _git_output(root, ["git", "branch", "--show-current"])
                if not _is_repair_branch(branch, context.config.worker_branch_prefix):
                    return self._result(
                        call,
                        success=False,
                        content=f"Not on a repair branch: {branch}",
                        error="not_repair_branch",
                    )
                receipt_snapshot = receipt.get("repair_snapshot")
                if not isinstance(receipt_snapshot, dict):
                    return self._result(
                        call,
                        success=False,
                        content="Repair rollback receipt has no candidate fingerprint.",
                        error="rollback_receipt_invalid",
                    )
                _assert_repair_snapshot_workspace_safe(context)
                current = repair_snapshot(root)
                if receipt_snapshot.get("branch") != branch or receipt_snapshot.get(
                    "head_sha"
                ) != current.get("head_sha"):
                    return self._result(
                        call,
                        success=False,
                        content="Repair rollback receipt belongs to a different branch or HEAD.",
                        error="rollback_receipt_stale",
                        data={
                            "receipt_branch": receipt_snapshot.get("branch"),
                            "current_branch": branch,
                            "receipt_head_sha": receipt_snapshot.get("head_sha"),
                            "current_head_sha": current.get("head_sha"),
                        },
                    )
                if current.get("diff_digest") != expected_digest:
                    return self._result(
                        call,
                        success=False,
                        content=(
                            "Repair candidate changed after rollback approval was prepared; "
                            "inspect status and approve the new exact digest."
                        ),
                        error="rollback_snapshot_stale",
                        data={
                            "expected_current_diff_digest": expected_digest,
                            "actual_current_diff_digest": current.get("diff_digest"),
                        },
                    )
                tracked_files = _receipt_paths(receipt_snapshot.get("tracked_files"))
                untracked_files = _receipt_paths(receipt_snapshot.get("untracked_files"))
                target_files = sorted(set(tracked_files) | set(untracked_files))
                if not target_files:
                    return self._result(
                        call,
                        success=False,
                        content="Repair rollback receipt contains no changed files.",
                        error="empty_repair_diff",
                    )
                _assert_repair_paths_allowed(context, root, target_files)
                _preflight_rollback_targets(root, target_files)
                artifact_seed = json.dumps(
                    {
                        "branch": branch,
                        "reason": reason,
                        "review_id": review_id,
                        "validation_id": validation_id,
                        "expected_digest": expected_digest,
                        "nonce": os.urandom(16).hex(),
                    },
                    sort_keys=True,
                )
                artifact_id = (
                    "repair_rollback_"
                    + hashlib.sha256(artifact_seed.encode("utf-8")).hexdigest()[:24]
                )
                quarantine = _prepare_rollback_quarantine(root, artifact_id)
                quarantine_relpath = quarantine.relative_to(root).as_posix()
                before_status = _git_output(
                    root, ["git", "status", "--porcelain", "--untracked-files=all"]
                )
                before_payload = {
                    "status": before_status,
                    "changed_files": [
                        path
                        for path in _changed_files_from_status(before_status)
                        if not _is_repair_artifact_path(path)
                    ],
                    "receipt_diff_digest": receipt_snapshot.get("diff_digest"),
                    "approved_current_diff_digest": expected_digest,
                }
                write_repair_artifact(
                    root,
                    "repair_rollback_journals",
                    artifact_id,
                    {
                        "schema_version": 1,
                        "rollback_id": artifact_id,
                        "status": "planned",
                        "branch": branch,
                        "review_id": review_id or None,
                        "validation_id": validation_id or receipt.get("validation_id"),
                        "target_files": target_files,
                        "quarantine_path": quarantine_relpath,
                        "before": before_payload,
                    },
                )
                quarantine_manifest = _quarantine_rollback_targets(root, quarantine, target_files)
                restored = _restore_tracked_paths_from_head(root, tracked_files)
                after_status = _git_output(
                    root, ["git", "status", "--porcelain", "--untracked-files=all"]
                )
                after_changed = [
                    path
                    for path in _changed_files_from_status(after_status)
                    if not _is_repair_artifact_path(path)
                ]
                remaining_targets = sorted(set(target_files) & set(after_changed))
                success = not remaining_targets
                artifact_payload = {
                    "schema_version": 1,
                    "rollback_id": artifact_id,
                    "branch": branch,
                    "reason": reason,
                    "review_id": review_id or None,
                    "validation_id": validation_id or receipt.get("validation_id"),
                    "receipt_diff_digest": receipt_snapshot.get("diff_digest"),
                    "approved_current_diff_digest": expected_digest,
                    "target_files": target_files,
                    "restored_files": restored,
                    "removed_untracked_files": [
                        path for path in untracked_files if path in quarantine_manifest
                    ],
                    "quarantined_files": sorted(quarantine_manifest),
                    "quarantine_manifest": quarantine_manifest,
                    "quarantine_path": quarantine_relpath,
                    "recoverable": True,
                    "remaining_target_files": remaining_targets,
                    "before": before_payload,
                    "after": {
                        "status": after_status,
                        "changed_files": after_changed,
                        "preserved_changed_files": [
                            path for path in after_changed if path not in target_files
                        ],
                    },
                    "success": success,
                }
                artifact_relpath = write_repair_artifact(
                    root, "repair_rollbacks", artifact_id, artifact_payload
                )
            return self._result(
                call,
                success=success,
                content=(
                    f"rollback_artifact={artifact_relpath.as_posix()}\n"
                    f"quarantine={quarantine_relpath}\nrecoverable=true"
                ),
                data={
                    **artifact_payload,
                    "rollback_artifact": artifact_relpath.as_posix(),
                    "artifact_path": artifact_relpath.as_posix(),
                },
                error=None if success else "repair_rollback_failed",
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="workspace_secret_isolation_required",
            )
        except Exception as exc:  # noqa: BLE001
            failure_artifact: str | None = None
            if root is not None and artifact_id is not None:
                try:
                    relative = write_repair_artifact(
                        root,
                        "repair_rollbacks",
                        artifact_id,
                        {
                            "schema_version": 1,
                            "rollback_id": artifact_id,
                            "success": False,
                            "error": redact_text(f"{type(exc).__name__}: {exc}"),
                            "review_id": review_id or None,
                            "validation_id": validation_id or None,
                            "quarantine_path": quarantine_relpath,
                            "recoverable": bool(quarantine_relpath),
                        },
                    )
                    failure_artifact = relative.as_posix()
                except Exception:  # noqa: BLE001 - preserve the original rollback failure
                    pass
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="repair_rollback_failed",
                data={
                    "rollback_id": artifact_id,
                    "artifact_path": failure_artifact,
                    "quarantine_path": quarantine_relpath,
                    "recoverable": bool(quarantine_relpath),
                },
            )


def _receipt_paths(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("Repair receipt path manifest is invalid.")
    paths: list[str] = []
    for item in value:
        path = str(item)
        pure = PurePosixPath(path)
        if not path or pure.is_absolute() or ".." in pure.parts or _is_repair_artifact_path(path):
            raise ValueError(f"Unsafe path in repair receipt: {path!r}")
        paths.append(path)
    return sorted(set(paths))


def _snapshot_drift_fields(
    before: dict[str, Any],
    after: dict[str, Any],
) -> list[str]:
    return [
        field
        for field in ("branch", "head_sha", "diff_digest")
        if before.get(field) != after.get(field)
    ]


def _assert_repair_snapshot_paths_allowed(
    context: ToolContext,
    root: Path,
    snapshot: dict[str, Any],
) -> None:
    raw_paths = snapshot.get("changed_files", [])
    if not isinstance(raw_paths, list):
        raise ValueError("Repair snapshot path manifest is invalid.")
    _assert_repair_paths_allowed(context, root, [str(path) for path in raw_paths])


def _assert_repair_paths_allowed(
    context: ToolContext,
    root: Path,
    paths: list[str],
) -> None:
    for relative in paths:
        _assert_workspace_path_allowed(
            context,
            root / Path(relative),
            requested_path=relative,
        )


def _repair_validation_environment(cache_root: Path) -> dict[str, str]:
    existing_pytest_options = os.environ.get("PYTEST_ADDOPTS", "").strip()
    pytest_options = " ".join(
        option for option in (existing_pytest_options, "-p no:cacheprovider") if option
    )
    return {
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_ADDOPTS": pytest_options,
        "MYPY_CACHE_DIR": str(cache_root / "mypy"),
        "RUFF_CACHE_DIR": str(cache_root / "ruff"),
    }


def _is_repair_artifact_path(path: str) -> bool:
    return path == ".nest/repair-actions.lock" or path.startswith(
        (
            ".nest/repair_validations/",
            ".nest/repair_reviews/",
            ".nest/repair_rollbacks/",
            ".nest/repair_rollback_journals/",
            ".nest/repair_rollback_quarantine/",
            ".nest/repair_indexes/",
        )
    )


def _preflight_rollback_targets(workspace: Path, paths: list[str]) -> None:
    root = require_git_root(workspace)
    for relative in paths:
        candidate = root / Path(relative)
        current = root
        for component in Path(relative).parts[:-1]:
            current /= component
            try:
                parent_metadata = os.lstat(current)
            except FileNotFoundError:
                break
            if _rollback_metadata_is_reparse_point(parent_metadata) or not stat.S_ISDIR(
                parent_metadata.st_mode
            ):
                raise ValueError(f"Rollback path traverses an unsafe parent: {relative}")
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
            raise ValueError(f"Rollback targets must be files or symlinks: {relative}")


def _prepare_rollback_quarantine(workspace: Path, rollback_id: str) -> Path:
    root = require_git_root(workspace)
    if _uses_windows_rollback_path_fallback():
        return _prepare_rollback_quarantine_path(root, rollback_id)
    root_descriptor = os.open(
        root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptors: list[int] = [root_descriptor]
    try:
        parent = root_descriptor
        for name in (".nest", "repair_rollback_quarantine", rollback_id):
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
            except FileExistsError:
                if name == rollback_id:
                    raise ValueError(f"Rollback quarantine already exists: {rollback_id}") from None
            descriptor = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=parent,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                os.close(descriptor)
                raise ValueError(f"Rollback quarantine component is not a directory: {name}")
            if os.name != "nt":
                chmod_descriptor(descriptor, 0o700)
            descriptors.append(descriptor)
            parent = descriptor
        return root / ".nest" / "repair_rollback_quarantine" / rollback_id
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _quarantine_rollback_targets(
    workspace: Path,
    quarantine: Path,
    paths: list[str],
) -> dict[str, dict[str, object]]:
    root = require_git_root(workspace)
    if _uses_windows_rollback_path_fallback():
        return _quarantine_rollback_targets_path(root, quarantine, paths)
    manifest: dict[str, dict[str, object]] = {}
    quarantine_relative = quarantine.relative_to(root)
    quarantine_descriptor = _open_relative_directory_descriptor(root, quarantine_relative)
    try:
        for relative in paths:
            _preflight_rollback_targets(root, [relative])
            source_descriptor, leaf_name = _open_relative_parent_descriptor(root, Path(relative))
            try:
                try:
                    metadata = os.stat(
                        leaf_name,
                        dir_fd=source_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                stored_name = hashlib.sha256(os.fsencode(relative)).hexdigest()
                try:
                    os.stat(
                        stored_name,
                        dir_fd=quarantine_descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    pass
                else:
                    raise ValueError(f"Rollback quarantine collision for path: {relative}")
                os.rename(
                    leaf_name,
                    stored_name,
                    src_dir_fd=source_descriptor,
                    dst_dir_fd=quarantine_descriptor,
                )
                manifest[relative] = {
                    "stored_name": stored_name,
                    "type": "symlink" if stat.S_ISLNK(metadata.st_mode) else "regular",
                    "mode": stat.S_IMODE(metadata.st_mode),
                }
            finally:
                os.close(source_descriptor)
    finally:
        os.close(quarantine_descriptor)
    return manifest


def _uses_windows_rollback_path_fallback() -> bool:
    return os.name == "nt"


def _prepare_rollback_quarantine_path(workspace: Path, rollback_id: str) -> Path:
    """Create a private quarantine without relying on Windows dirfd support.

    CPython does not expose directory descriptors or the ``*at`` family on
    Windows.  Keep the stronger descriptor implementation on POSIX, while the
    Windows path revalidates every parent identity and gives both quarantine
    levels a protected owner-private DACL.
    """

    root = require_git_root(workspace)
    root_identity = _require_real_rollback_directory(root, "workspace")
    nest = root / ".nest"
    nest_identity = _require_real_rollback_directory(nest, ".nest")
    parent = nest / "repair_rollback_quarantine"
    try:
        parent_identity = parent.lstat()
    except FileNotFoundError:
        create_owner_private_directory(parent)
        parent_identity = _require_real_rollback_directory(parent, "repair_rollback_quarantine")
    else:
        _validate_real_rollback_directory(parent_identity, "repair_rollback_quarantine")
        validate_owner_private_directory(parent)
        _require_same_rollback_identity(
            parent_identity,
            parent.lstat(),
            "Rollback quarantine parent changed during validation.",
        )

    _revalidate_rollback_path(
        (
            (root, root_identity),
            (nest, nest_identity),
            (parent, parent_identity),
        )
    )
    quarantine = parent / rollback_id
    try:
        create_owner_private_directory(quarantine)
    except FileExistsError:
        raise ValueError(f"Rollback quarantine already exists: {rollback_id}") from None
    quarantine_identity = _require_real_rollback_directory(quarantine, rollback_id)
    _revalidate_rollback_path(
        (
            (root, root_identity),
            (nest, nest_identity),
            (parent, parent_identity),
            (quarantine, quarantine_identity),
        )
    )
    return quarantine


def _quarantine_rollback_targets_path(
    workspace: Path,
    quarantine: Path,
    paths: list[str],
) -> dict[str, dict[str, object]]:
    manifest: dict[str, dict[str, object]] = {}
    root = require_git_root(workspace)
    quarantine_identities = _rollback_directory_identities(
        root,
        quarantine,
        validate_private_leaf=True,
    )
    for relative in paths:
        _preflight_rollback_targets(root, [relative])
        source = root / Path(relative)
        try:
            metadata = source.lstat()
        except FileNotFoundError:
            continue
        parent_identities = _rollback_directory_identities(root, source.parent)
        stored_name = hashlib.sha256(os.fsencode(relative)).hexdigest()
        destination = quarantine / stored_name
        try:
            destination.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError(f"Rollback quarantine collision for path: {relative}")

        _revalidate_rollback_path(parent_identities)
        _revalidate_rollback_path(quarantine_identities)
        os.replace(source, destination)
        moved = destination.lstat()
        _require_same_rollback_identity(
            metadata,
            moved,
            f"Rollback target identity changed while quarantining: {relative}",
        )
        try:
            source.lstat()
        except FileNotFoundError:
            pass
        else:
            raise ValueError(f"Rollback target remained after quarantining: {relative}")
        _revalidate_rollback_path(parent_identities)
        _revalidate_rollback_path(quarantine_identities)
        manifest[relative] = {
            "stored_name": stored_name,
            "type": "symlink" if stat.S_ISLNK(metadata.st_mode) else "regular",
            "mode": stat.S_IMODE(metadata.st_mode),
        }
    return manifest


def _rollback_directory_identities(
    root: Path,
    leaf: Path,
    *,
    validate_private_leaf: bool = False,
) -> tuple[tuple[Path, os.stat_result], ...]:
    relative = leaf.relative_to(root)
    identities: list[tuple[Path, os.stat_result]] = [
        (root, _require_real_rollback_directory(root, "workspace"))
    ]
    current = root
    for component in relative.parts:
        current /= component
        identities.append((current, _require_real_rollback_directory(current, component)))
    if validate_private_leaf:
        validate_owner_private_directory(leaf)
        _revalidate_rollback_path(tuple(identities))
    return tuple(identities)


def _require_real_rollback_directory(path: Path, name: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValueError(f"Rollback directory is unavailable: {name}") from exc
    _validate_real_rollback_directory(metadata, name)
    return metadata


def _validate_real_rollback_directory(metadata: os.stat_result, name: str) -> None:
    if _rollback_metadata_is_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Rollback directory is not a real directory: {name}")


def _rollback_metadata_is_reparse_point(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _require_same_rollback_identity(
    expected: os.stat_result,
    actual: os.stat_result,
    message: str,
) -> None:
    if (
        stat.S_IFMT(expected.st_mode),
        expected.st_dev,
        expected.st_ino,
    ) != (
        stat.S_IFMT(actual.st_mode),
        actual.st_dev,
        actual.st_ino,
    ):
        raise ValueError(message)


def _revalidate_rollback_path(
    identities: tuple[tuple[Path, os.stat_result], ...],
) -> None:
    for path, expected in identities:
        current = _require_real_rollback_directory(path, path.name or str(path))
        _require_same_rollback_identity(
            expected,
            current,
            f"Rollback directory identity changed: {path.name or path}",
        )


def _open_relative_directory_descriptor(root: Path, relative: Path) -> int:
    descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for component in relative.parts:
            child = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_relative_parent_descriptor(root: Path, relative: Path) -> tuple[int, str]:
    if not relative.parts:
        raise ValueError("Rollback path is empty.")
    parent = Path(*relative.parts[:-1]) if len(relative.parts) > 1 else Path()
    return _open_relative_directory_descriptor(root, parent), relative.parts[-1]


def _restore_tracked_paths_from_head(workspace: Path, paths: list[str]) -> list[str]:
    if not paths:
        return []
    root = require_git_root(workspace)
    head = _git_output(root, ["git", "rev-parse", "HEAD"])
    zero_oid = "0" * len(head)
    index_records = bytearray()
    restored: list[str] = []
    for relative in paths:
        entry = _head_tree_entry(root, relative)
        encoded_path = os.fsencode(relative)
        if entry is None:
            index_records.extend(f"0 {zero_oid}\t".encode("ascii"))
            index_records.extend(encoded_path)
            index_records.append(0)
            restored.append(relative)
            continue
        mode, object_id = entry
        blob_size = int(_git_output(root, ["git", "cat-file", "-s", object_id]).strip() or "0")
        if blob_size > 128 * 1024 * 1024:
            raise ValueError(f"Rollback source blob is too large: {relative}")
        blob = subprocess.run(  # nosec
            _hardened_git_command(["cat-file", "blob", object_id], workspace=root),
            cwd=root,
            env=_sanitized_git_environment(None),
            capture_output=True,
            timeout=30,
            check=False,
        )
        if blob.returncode != 0 or len(blob.stdout) != blob_size:
            raise RuntimeError(
                f"Unable to read rollback source for {relative}: "
                + blob.stderr.decode("utf-8", errors="replace")
            )
        _restore_literal_blob(root, relative, mode, blob.stdout)
        index_records.extend(f"{mode} {object_id}\t".encode("ascii"))
        index_records.extend(encoded_path)
        index_records.append(0)
        restored.append(relative)
    indexed = subprocess.run(  # nosec
        _hardened_git_command(["update-index", "-z", "--index-info"], workspace=root),
        cwd=root,
        env=_sanitized_git_environment(None),
        input=bytes(index_records),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if indexed.returncode != 0:
        raise RuntimeError(
            "Unable to synchronize rollback index: "
            + indexed.stderr.decode("utf-8", errors="replace")
        )
    return restored


def _head_tree_entry(workspace: Path, relative: str) -> tuple[str, str] | None:
    completed = subprocess.run(  # nosec
        _hardened_git_command(["ls-tree", "-z", "HEAD", "--", relative], workspace=workspace),
        cwd=workspace,
        env=_sanitized_git_environment(None),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Unable to inspect rollback source for {relative}: "
            + completed.stderr.decode("utf-8", errors="replace")
        )
    if not completed.stdout:
        return None
    record = completed.stdout.rstrip(b"\0")
    metadata, separator, reported_path = record.partition(b"\t")
    parts = metadata.split(b" ")
    if not separator or len(parts) != 3 or os.fsdecode(reported_path) != relative:
        raise ValueError(f"Unexpected Git tree entry for rollback path: {relative}")
    mode, object_type, object_id = (item.decode("ascii") for item in parts)
    if object_type != "blob" or mode not in {"100644", "100755", "120000"}:
        raise ValueError(f"Unsupported Git tree entry for rollback path: {relative}")
    return mode, object_id


def _restore_literal_blob(workspace: Path, relative: str, mode: str, content: bytes) -> None:
    candidate = workspace / Path(relative)
    parent = workspace
    parent_identities: list[tuple[Path, os.stat_result]] = [
        (workspace, _require_real_rollback_directory(workspace, "workspace"))
    ]
    for component in Path(relative).parts[:-1]:
        parent /= component
        try:
            metadata = os.lstat(parent)
        except FileNotFoundError:
            parent.mkdir(mode=0o755)
            metadata = os.lstat(parent)
        if _rollback_metadata_is_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Rollback path traverses an unsafe parent: {relative}")
        parent_identities.append((parent, metadata))
    descriptor, temporary_name = tempfile.mkstemp(prefix=".kestrel-rollback-", dir=parent)
    temporary = Path(temporary_name)
    try:
        if mode == "120000":
            os.close(descriptor)
            descriptor = -1
            temporary.unlink()
            if b"\x00" in content:
                raise ValueError(f"Rollback symlink target contains NUL: {relative}")
            os.symlink(os.fsdecode(content), temporary)
        else:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o755 if mode == "100755" else 0o644)
        if _uses_windows_rollback_path_fallback():
            _revalidate_rollback_path(tuple(parent_identities))
        os.replace(temporary, candidate)
        if _uses_windows_rollback_path_fallback():
            restored = candidate.lstat()
            if mode == "120000":
                if not stat.S_ISLNK(restored.st_mode):
                    raise ValueError(f"Rollback symlink changed type: {relative}")
            elif not stat.S_ISREG(restored.st_mode):
                raise ValueError(f"Rollback file changed type: {relative}")
            _revalidate_rollback_path(tuple(parent_identities))
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
