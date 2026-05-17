from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404
from pathlib import Path
from typing import Any

from ..cognition import RetryPolicy
from ..diagnosis import classify_failure
from ..runtime_models import StrategyProposal, ToolCall, ToolExecution, ToolSpec
from .base import AgentTool, ToolContext
from .diagnosis_tools import _recall_failure_lessons, _recall_hit_titles
from .git_tools import (
    _changed_files_from_status,
    _git_output,
    _is_repair_branch,
    _safe_branch_name,
)
from .patch_helpers import _validate_patch_paths
from .process_tools import (
    _cancel_running_subprocess,
    _normalize_python_command,
    _run_subprocess,
    _SubprocessToolTimeout,
)
from .validation_helpers import (
    _merge_validation_evidence_payloads,
    _tool_validation_evidence_payload,
)


class RepairPrepareTool(AgentTool):
    spec = ToolSpec(
        name="repair.prepare",
        description="Prepare an isolated repair branch from the current clean workspace and record the base SHA. Requires approval.",
        parameters={
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "allow_dirty": {"type": "boolean"},
            },
            "required": ["branch"],
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "git-isolation"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        branch = str(arguments.get("branch", "")).strip()
        if not branch:
            return self._result(
                call, success=False, content="Missing branch", error="missing_branch"
            )
        if not _safe_branch_name(branch):
            return self._result(
                call,
                success=False,
                content=f"Unsafe branch name: {branch}",
                error="unsafe_branch_name",
            )
        try:
            base = subprocess.run(  # nosec
                ["git", "rev-parse", "HEAD"],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if base.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=f"Unable to resolve base SHA. STDERR:\n{base.stderr}",
                    error="git_base_failed",
                    data={"returncode": base.returncode},
                )
            status = subprocess.run(  # nosec
                ["git", "status", "--porcelain"],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if status.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=f"Unable to inspect worktree. STDERR:\n{status.stderr}",
                    error="git_status_failed",
                    data={"returncode": status.returncode},
                )
            if status.stdout.strip() and not bool(arguments.get("allow_dirty", False)):
                return self._result(
                    call,
                    success=False,
                    content="Refusing to prepare repair branch with uncommitted changes.",
                    error="dirty_worktree",
                    data={"dirty_status": status.stdout},
                )
            created = subprocess.run(  # nosec
                ["git", "switch", "-c", branch],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={created.returncode}\nSTDOUT:\n{created.stdout}\nSTDERR:\n{created.stderr}"
            success = created.returncode == 0
            return self._result(
                call,
                success=success,
                content=content,
                data={
                    "mode": "branch",
                    "branch": branch,
                    "base_sha": base.stdout.strip(),
                    "returncode": created.returncode,
                    "approval_required_before_commit": True,
                },
                error=None if success else "repair_prepare_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="repair_prepare_failed"
            )


class RepairStatusTool(AgentTool):
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
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            head = _git_output(context.workspace, ["git", "rev-parse", "HEAD"])
            status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            changed_files = _changed_files_from_status(status)
            base_sha = str(arguments.get("base_sha", "")).strip() or None
            payload = {
                "branch": branch,
                "head_sha": head,
                "base_sha": base_sha,
                "active_repair_branch": _is_repair_branch(branch),
                "dirty": bool(status.strip()),
                "changed_files": changed_files,
                "raw_status": status,
            }
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_status_failed")


class RepairApplyPatchTool(AgentTool):
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
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(
                    call,
                    success=False,
                    content=f"Refusing to apply repair patch on non-repair branch: {branch}",
                    error="not_repair_branch",
                    data={"branch": branch},
                )
            _validate_patch_paths(context.workspace, patch_text)
            command = (
                ["git", "apply", "--check"]
                if bool(arguments.get("check", False))
                else ["git", "apply", "--whitespace=nowarn"]
            )
            completed = subprocess.run(  # nosec
                command,
                cwd=context.workspace,
                input=patch_text,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
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
            return self._result(call, success=False, content=str(exc), error="repair_patch_failed")


class RepairValidateTool(AgentTool):
    spec = ToolSpec(
        name="repair.validate",
        description="Run a bounded repair validation command on an active repair branch and classify failures.",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer"},
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
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(
            isinstance(item, str) for item in command_raw
        ):
            return self._result(
                call, success=False, content="command must be list[str]", error="bad_command"
            )
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(
                call,
                success=False,
                content="Command is not allowlisted",
                error="command_not_allowlisted",
            )
        command = _normalize_python_command(command)
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(
                    call,
                    success=False,
                    content=f"Not on a repair branch: {branch}",
                    error="not_repair_branch",
                )
            completed = _run_subprocess(
                command, context=context, arguments=arguments, default_timeout=120
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            diagnosis = (
                classify_failure(content, source="repair.validate").to_payload()
                if completed.returncode != 0
                else None
            )
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data={
                    "branch": branch,
                    "returncode": completed.returncode,
                    "diagnosis": diagnosis,
                    "validation_evidence": _tool_validation_evidence_payload(
                        "repair_refs",
                        "repair.validate",
                        command,
                        content,
                        completed.returncode == 0,
                    ),
                },
                error=None if completed.returncode == 0 else "repair_validation_failed",
            )
        except _SubprocessToolTimeout as exc:
            return self._result(call, success=False, content=str(exc), error="tool_timeout")
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="repair_validation_failed"
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class RepairOrchestrateValidateTool(AgentTool):
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
        call = ToolCall(name=self.spec.name, arguments=arguments)
        command_raw = arguments.get("command")
        if not isinstance(command_raw, list) or not all(
            isinstance(item, str) for item in command_raw
        ):
            return self._result(
                call, success=False, content="command must be list[str]", error="bad_command"
            )
        command = list(command_raw)
        if not command or Path(command[0]).name not in self.allowed_first_tokens:
            return self._result(
                call,
                success=False,
                content="Command is not allowlisted",
                error="command_not_allowlisted",
            )
        command = _normalize_python_command(command)
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(
                    call,
                    success=False,
                    content=f"Not on a repair branch: {branch}",
                    error="not_repair_branch",
                    data={"branch": branch},
                )
            status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            completed = _run_subprocess(
                command, context=context, arguments=arguments, default_timeout=120
            )
            validation_content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            validation = {
                "success": completed.returncode == 0,
                "returncode": completed.returncode,
                "content": validation_content,
                "validation_evidence": _tool_validation_evidence_payload(
                    "repair_refs",
                    "repair.orchestrate_validate",
                    command,
                    validation_content,
                    completed.returncode == 0,
                ),
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
                if completed.returncode == 0
                else "No similar lesson was found; follow the diagnostic playbook.",
                "strategy_changed": True,
            }
            next_action = (
                "create_repair_review_before_commit"
                if completed.returncode == 0
                else "retry_with_diagnostic_playbook"
            )
            if completed.returncode != 0:
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
                "changed_files": _changed_files_from_status(status),
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
        except _SubprocessToolTimeout as exc:
            return self._result(call, success=False, content=str(exc), error="tool_timeout")
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="repair_orchestration_failed"
            )

    def cancel(self, call_id: str) -> None:
        _cancel_running_subprocess(call_id)


class RepairReviewTool(AgentTool):
    spec = ToolSpec(
        name="repair.review",
        description="Create a durable reviewer gate artifact for a validated repair diff before commit.",
        parameters={
            "type": "object",
            "properties": {
                "validation": {"type": "object"},
                "summary": {"type": "string"},
                "risks": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["validation"],
        },
        risk="medium",
        requires_approval=True,
        capabilities=("safe-repair", "review-gate"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        validation = arguments.get("validation")
        if not isinstance(validation, dict):
            return self._result(
                call, success=False, content="validation must be an object", error="bad_validation"
            )
        if validation.get("success") is not True:
            return self._result(
                call,
                success=False,
                content="Repair review requires successful validation.",
                error="validation_not_successful",
            )
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(
                    call,
                    success=False,
                    content=f"Not on a repair branch: {branch}",
                    error="not_repair_branch",
                    data={"branch": branch},
                )
            diff = _git_output(context.workspace, ["git", "diff", "HEAD", "--"])
            if not diff.strip():
                return self._result(
                    call,
                    success=False,
                    content="No repair diff found to review.",
                    error="empty_repair_diff",
                    data={"branch": branch},
                )
            status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            head = _git_output(context.workspace, ["git", "rev-parse", "HEAD"])
            diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
            review_id = f"repair_review_{diff_hash[:16]}"
            risks_arg = arguments.get("risks")
            risks = [str(item) for item in risks_arg] if isinstance(risks_arg, list) else []
            payload = {
                "review_id": review_id,
                "branch": branch,
                "head_sha": head,
                "diff_hash": diff_hash,
                "changed_files": _changed_files_from_status(status),
                "summary": str(arguments.get("summary", "")).strip(),
                "risks": risks,
                "validation": validation,
                "validation_evidence": validation.get("validation_evidence"),
                "commit_gate": {
                    "commit_allowed": True,
                    "approval_required_before_commit": True,
                    "reason": "Successful validation and reviewer artifact are present; commit still requires exact-call approval.",
                },
            }
            review_dir = context.workspace / ".nest" / "repair_reviews"
            review_dir.mkdir(parents=True, exist_ok=True)
            (review_dir / f"{review_id}.json").write_text(
                json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
            )
            payload["validation_evidence"] = _merge_validation_evidence_payloads(
                validation.get("validation_evidence"),
                {
                    "review_refs": [
                        {
                            "source": "repair.review",
                            "locator": review_id,
                            "quote": str(arguments.get("summary", "")).strip()[:240],
                        }
                    ],
                    "source_evidence_chars": len(json.dumps(payload, sort_keys=True)),
                },
            )
            return self._result(
                call, success=True, content=json.dumps(payload, indent=2), data=payload
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="repair_review_failed")


class RepairRollbackTool(AgentTool):
    spec = ToolSpec(
        name="repair.rollback",
        description="Rollback uncommitted changes on an active repair branch. Requires approval and never runs on main/master.",
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "review_id": {"type": "string"},
            },
        },
        risk="high",
        requires_approval=True,
        capabilities=("safe-repair", "rollback"),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        try:
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not _is_repair_branch(branch):
                return self._result(
                    call,
                    success=False,
                    content=f"Not on a repair branch: {branch}",
                    error="not_repair_branch",
                )
            before_status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            before_diff = _git_output(context.workspace, ["git", "diff", "HEAD", "--"])
            before_payload = {
                "status": before_status,
                "changed_files": [
                    path
                    for path in _changed_files_from_status(before_status)
                    if not path.startswith(".nest/") and path != ".nest"
                ],
                "diff_hash": hashlib.sha256(before_diff.encode("utf-8")).hexdigest()
                if before_diff
                else "",
            }
            reset = subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )  # nosec
            clean = subprocess.run(
                ["git", "clean", "-fd"],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )  # nosec
            after_status = _git_output(context.workspace, ["git", "status", "--porcelain"])
            success = reset.returncode == 0 and clean.returncode == 0
            reason = str(arguments.get("reason", "manual_rollback")).strip() or "manual_rollback"
            review_id = str(arguments.get("review_id", "")).strip()
            artifact_payload = {
                "branch": branch,
                "reason": reason,
                "review_id": review_id or None,
                "before": before_payload,
                "after": {
                    "status": after_status,
                    "changed_files": _changed_files_from_status(after_status),
                },
                "commands": {
                    "reset": {
                        "returncode": reset.returncode,
                        "stdout": reset.stdout,
                        "stderr": reset.stderr,
                    },
                    "clean": {
                        "returncode": clean.returncode,
                        "stdout": clean.stdout,
                        "stderr": clean.stderr,
                    },
                },
                "success": success,
            }
            artifact_seed = json.dumps(
                {
                    "branch": branch,
                    "reason": reason,
                    "review_id": review_id,
                    "before": before_payload,
                },
                sort_keys=True,
            )
            artifact_id = (
                f"repair_rollback_{hashlib.sha256(artifact_seed.encode('utf-8')).hexdigest()[:16]}"
            )
            artifact_relpath = Path(".nest") / "repair_rollbacks" / f"{artifact_id}.json"
            artifact_path = context.workspace / artifact_relpath
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            artifact_path.write_text(
                json.dumps(artifact_payload, indent=2, sort_keys=True), encoding="utf-8"
            )
            content = (
                f"reset_exit_code={reset.returncode}\nRESET_STDOUT:\n{reset.stdout}\nRESET_STDERR:\n{reset.stderr}\n"
                f"clean_exit_code={clean.returncode}\nCLEAN_STDOUT:\n{clean.stdout}\nCLEAN_STDERR:\n{clean.stderr}\n"
                f"rollback_artifact={artifact_relpath.as_posix()}"
            )
            return self._result(
                call,
                success=success,
                content=content,
                data={
                    "branch": branch,
                    "reason": reason,
                    "review_id": review_id or None,
                    "reset_returncode": reset.returncode,
                    "clean_returncode": clean.returncode,
                    "rollback_artifact": artifact_relpath.as_posix(),
                    "before": before_payload,
                    "after": artifact_payload["after"],
                },
                error=None if success else "repair_rollback_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="repair_rollback_failed"
            )
