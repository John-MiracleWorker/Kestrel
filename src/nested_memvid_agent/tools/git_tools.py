from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess  # nosec B404
import tempfile
import time
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ..repair_integrity import (
    hardened_readonly_git_command,
    hardened_readonly_git_environment,
    load_review_receipt,
    load_validation_receipt,
    repair_action_lock,
    repair_snapshot,
    require_git_root,
)
from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from ..security_boundary import redact_text
from .base import AgentTool, ToolContext
from .patch_helpers import _validate_patch_paths
from .process_tools import (
    WorkspaceSecretIsolationError,
    _assert_repair_snapshot_workspace_safe,
    _run_subprocess,
    _SubprocessToolTimeout,
)
from .workspace_tools import _assert_workspace_path_allowed, _safe_path


def _safe_branch_name(name: str) -> bool:
    if not name or name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    if ".." in name or "//" in name or "@{" in name or "\\" in name:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
    return all(char in allowed for char in name)


def _is_repair_branch(name: str, worker_branch_prefix: str = "kestrel/worker") -> bool:
    normalized_worker_prefix = worker_branch_prefix.strip().strip("/")
    prefixes = ["codex/", "repair/", "fix/"]
    if normalized_worker_prefix:
        prefixes.append(f"{normalized_worker_prefix}/")
    return name.startswith(tuple(prefixes)) and name not in {"main", "master"}


def _is_protected_branch(name: str, protected_patterns: tuple[str, ...]) -> bool:
    branch = name.strip()
    return bool(branch) and any(fnmatchcase(branch, pattern) for pattern in protected_patterns)


def _git_output(workspace: Path, command: list[str]) -> str:
    completed = subprocess.run(  # nosec
        _hardened_git_command(_git_command_arguments(command), workspace=workspace),
        cwd=workspace,
        capture_output=True,
        text=True,
        env=_sanitized_git_environment(None),
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            redact_text(
                f"git command failed ({completed.returncode}): {' '.join(command)}\n"
                f"{completed.stderr}"
            )
        )
    return redact_text(completed.stdout or "").strip()


def _validate_repair_review_gate(workspace: Path, branch: str, review_id: str) -> dict[str, Any]:
    if not review_id:
        return {
            "ok": False,
            "error": "repair_review_required",
            "content": "Repair branch commits require a repair_review_id from repair.review.",
            "branch": branch,
        }
    try:
        review = load_review_receipt(workspace, review_id)
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "repair_review_not_found",
            "content": f"Repair review artifact not found: {review_id}",
            "branch": branch,
        }
    except ValueError as exc:
        return {
            "ok": False,
            "error": "repair_review_invalid",
            "content": redact_text(str(exc)),
            "branch": branch,
        }
    if review.get("branch") != branch:
        return {
            "ok": False,
            "error": "repair_review_branch_mismatch",
            "content": f"Repair review was created for {review.get('branch')}, not {branch}.",
            "branch": branch,
            "review_id": review_id,
        }
    current_head = _git_output(workspace, ["git", "rev-parse", "HEAD"])
    if review.get("head_sha") != current_head:
        return {
            "ok": False,
            "error": "repair_review_stale",
            "content": "Repair HEAD changed after review; validate and review again before committing.",
            "branch": branch,
            "review_id": review_id,
            "expected_head_sha": review.get("head_sha"),
            "actual_head_sha": current_head,
        }
    if (
        review.get("validation", {}).get("success") is not True
        or review.get("commit_gate", {}).get("commit_allowed") is not True
    ):
        return {
            "ok": False,
            "error": "repair_review_not_approved",
            "content": "Repair review does not contain a successful validation commit gate.",
            "branch": branch,
            "review_id": review_id,
        }
    validation_id = str(review.get("validation_id", "")).strip()
    try:
        validation = load_validation_receipt(workspace, validation_id)
    except (FileNotFoundError, ValueError) as exc:
        return {
            "ok": False,
            "error": "repair_validation_receipt_invalid",
            "content": f"Repair validation receipt cannot be verified: {exc}",
            "branch": branch,
            "review_id": review_id,
        }
    validation_snapshot = validation.get("repair_snapshot")
    review_snapshot = review.get("repair_snapshot")
    if (
        validation.get("success") is not True
        or not isinstance(validation_snapshot, dict)
        or not isinstance(review_snapshot, dict)
        or validation_snapshot.get("diff_digest") != review.get("diff_digest")
        or review_snapshot.get("diff_digest") != review.get("diff_digest")
    ):
        return {
            "ok": False,
            "error": "repair_validation_receipt_invalid",
            "content": "Repair review and validation receipts do not describe the same candidate.",
            "branch": branch,
            "review_id": review_id,
        }
    try:
        snapshot = repair_snapshot(workspace)
    except (RuntimeError, ValueError) as exc:
        return {
            "ok": False,
            "error": "repair_review_invalid",
            "content": f"Unable to fingerprint the current repair: {exc}",
            "branch": branch,
            "review_id": review_id,
        }
    diff_hash = str(snapshot["diff_digest"])
    if review.get("diff_digest") != diff_hash:
        return {
            "ok": False,
            "error": "repair_review_stale",
            "content": "Repair diff changed after review; run repair.review again before committing.",
            "branch": branch,
            "review_id": review_id,
            "expected_diff_hash": review.get("diff_digest"),
            "actual_diff_hash": diff_hash,
        }
    return {
        "ok": True,
        "review_id": review_id,
        "diff_hash": diff_hash,
        "branch": branch,
        "head_sha": current_head,
        "changed_files": snapshot["changed_files"],
        "tracked_files": snapshot["tracked_files"],
        "untracked_files": snapshot["untracked_files"],
        "repair_snapshot": snapshot,
    }


def _changed_files_from_status(status: str) -> list[str]:
    files: list[str] = []
    for line in status.splitlines():
        if not line:
            continue
        path = line[3:] if len(line) > 3 and line[2] == " " else line[2:]
        if " -> " in path:
            path = path.split(" -> ", maxsplit=1)[1]
        files.append(path.strip())
    return files


def _snapshot_changed_paths(snapshot: dict[str, Any]) -> list[str]:
    raw_paths = snapshot.get("changed_files", [])
    if not isinstance(raw_paths, list):
        raise ValueError("Repair snapshot path manifest is invalid.")
    return [str(path) for path in raw_paths]


def _staged_git_paths(workspace: Path) -> list[str]:
    return _diff_git_paths(workspace, staged=True)


def _diff_git_paths(workspace: Path, *, staged: bool) -> list[str]:
    arguments = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--name-only",
        "-z",
    ]
    if staged:
        arguments.insert(1, "--cached")
    return _git_path_list(workspace, arguments, operation="diff")


def _status_git_paths(workspace: Path) -> list[str]:
    paths = {
        *_diff_git_paths(workspace, staged=False),
        *_diff_git_paths(workspace, staged=True),
        *_git_path_list(
            workspace,
            ["ls-files", "--others", "--exclude-standard", "-z"],
            operation="status",
        ),
    }
    return sorted(paths)


def _commit_git_paths(workspace: Path, commit_sha: str) -> list[str]:
    return _git_path_list(
        workspace,
        [
            "diff-tree",
            "--root",
            "-m",
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit_sha,
        ],
        operation="commit",
    )


def _git_path_list(
    workspace: Path,
    arguments: list[str],
    *,
    operation: str,
) -> list[str]:
    completed = subprocess.run(  # noqa: S603 - fixed read-only git command  # nosec
        _hardened_git_command(arguments, workspace=workspace),
        cwd=workspace,
        env=_sanitized_git_environment(None),
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(
            redact_text(f"Unable to inspect Git {operation} paths: {stderr}")
        )
    return [os.fsdecode(path) for path in completed.stdout.split(b"\0") if path]


def _assert_git_paths_allowed(context: ToolContext, paths: list[str]) -> None:
    for relative in paths:
        candidate = _safe_path(context.workspace, relative)
        _assert_workspace_path_allowed(
            context,
            candidate,
            requested_path=relative,
        )


def _git_read(
    call: ToolCall,
    context: ToolContext,
    command: list[str],
    error_code: str,
    *,
    validate_rendered_patch: bool = False,
) -> ToolExecution:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed read-only git commands  # nosec
            _hardened_git_command(
                _git_command_arguments(command), workspace=context.workspace
            ),
            cwd=context.workspace,
            env=_sanitized_git_environment(None),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode == 0 and validate_rendered_patch:
            try:
                _validate_patch_paths(
                    context.workspace,
                    completed.stdout or "",
                    context=context,
                )
            except (OSError, RuntimeError, ValueError):
                return ToolExecution(
                    call=call,
                    success=False,
                    content=(
                        "Git diff includes a protected workspace path and was not rendered."
                    ),
                    error="git_diff_path_blocked",
                )
        content = redact_text(
            f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
            f"STDERR:\n{completed.stderr}"
        )
        return ToolExecution(
            call=call,
            success=completed.returncode == 0,
            content=content,
            data={"returncode": completed.returncode},
            error=None if completed.returncode == 0 else error_code,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolExecution(
            call=call,
            success=False,
            content=redact_text(str(exc)),
            error=error_code,
        )


class GitStatusTool(AgentTool):
    spec = ToolSpec(
        name="git.status",
        description=(
            "Return read-only git status plus an exact branch, HEAD, and staged-tree "
            "preview for git.commit."
        ),
        parameters={"type": "object", "properties": {}},
        aliases=("status",),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        try:
            _assert_git_paths_allowed(context, _status_git_paths(context.workspace))
        except (OSError, RuntimeError, ValueError):
            return self._result(
                call,
                success=False,
                content="Git status includes a protected workspace path and was not rendered.",
                error="git_status_path_blocked",
            )
        result = _git_read(
            call, context, ["git", "status", "--short", "--branch"], "git_status_failed"
        )
        if result.success:
            try:
                branch = _git_output(
                    context.workspace, ["git", "branch", "--show-current"]
                )
                head_sha = _git_output(
                    context.workspace, ["git", "rev-parse", "HEAD"]
                )
                staged_tree_sha = _current_index_tree_sha(context.workspace)
                result.data.update(
                    {
                        "branch": branch or None,
                        "head_sha": head_sha,
                        "staged_tree_sha": staged_tree_sha,
                        "commit_preview": {
                            "expected_branch": branch,
                            "expected_head_sha": head_sha,
                            "expected_tree_sha": staged_tree_sha,
                        }
                        if branch
                        else None,
                    }
                )
            except (RuntimeError, ValueError):
                result.data["staged_tree_sha"] = None
                result.data["commit_preview"] = None
        return result


class GitDiffTool(AgentTool):
    spec = ToolSpec(
        name="git.diff",
        description="Return read-only git diff for the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        staged = bool(arguments.get("staged", False))
        try:
            _assert_git_paths_allowed(
                context,
                _diff_git_paths(context.workspace, staged=staged),
            )
        except (OSError, RuntimeError, ValueError):
            return self._result(
                call,
                success=False,
                content="Git diff includes a protected workspace path and was not rendered.",
                error="git_diff_path_blocked",
            )
        command = ["git", "diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            command.append("--cached")
        result = _git_read(
            call,
            context,
            command,
            "git_diff_failed",
            validate_rendered_patch=True,
        )
        max_chars = int(arguments.get("max_chars", 40_000))
        if len(result.content) > max_chars:
            return self._result(
                call,
                success=result.success,
                content=result.content[:max_chars] + "\n... truncated ...",
                data={**result.data, "truncated": True},
                error=result.error,
            )
        return result


class GitExportPatchTool(AgentTool):
    needs_call_id = True
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="git.export_patch",
        description="Export the current git diff to a local .kestrel/improvements patch file. Never pushes.",
        parameters={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean"},
                "path": {"type": "string"},
            },
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        staged = bool(arguments.get("staged", False))
        try:
            _assert_git_paths_allowed(
                context,
                _diff_git_paths(context.workspace, staged=staged),
            )
        except (OSError, RuntimeError, ValueError):
            return self._result(
                call,
                success=False,
                content="Git patch export includes a protected workspace path; nothing was written.",
                error="git_export_path_blocked",
            )
        command = ["git", "diff", "--no-ext-diff", "--no-textconv"]
        if staged:
            command.append("--cached")
        try:
            completed = _run_subprocess(
                _hardened_git_command(command[1:], workspace=context.workspace),
                context=context,
                arguments=arguments,
                default_timeout=30,
                environment=_sanitized_git_environment(None),
            )
            if completed.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=redact_text(
                        f"Unable to export patch. STDERR:\n{completed.stderr}"
                    ),
                    error="git_export_patch_failed",
                    data={"returncode": completed.returncode},
                )
            try:
                _validate_patch_paths(
                    context.workspace,
                    completed.stdout or "",
                    context=context,
                )
            except (OSError, RuntimeError, ValueError):
                return self._result(
                    call,
                    success=False,
                    content=(
                        "Git patch export includes a protected workspace path; "
                        "nothing was written."
                    ),
                    error="git_export_path_blocked",
                )
            patch = redact_text(completed.stdout or "")
            if not patch.strip():
                return self._result(
                    call, success=False, content="No diff to export.", error="empty_diff"
                )
            path_arg = str(arguments.get("path", "")).strip()
            if path_arg:
                patch_path = _safe_path(context.workspace, path_arg)
                _assert_workspace_path_allowed(
                    context, patch_path, requested_path=path_arg
                )
                relpath = patch_path.relative_to(context.workspace.resolve())
                if relpath.parts[:2] != (".kestrel", "improvements"):
                    return self._result(
                        call,
                        success=False,
                        content="Patch exports must stay under .kestrel/improvements/.",
                        error="invalid_patch_path",
                    )
            else:
                patch_id = hashlib.sha256(patch.encode("utf-8")).hexdigest()[:16]
                relpath = (
                    Path(".kestrel") / "improvements" / f"improvement_{patch_id}" / "diff.patch"
                )
                patch_path = context.workspace.resolve() / relpath
            patch_path.parent.mkdir(parents=True, exist_ok=True)
            patch_path.write_text(patch, encoding="utf-8")
            return self._result(
                call,
                success=True,
                content=f"Exported patch to {relpath.as_posix()}",
                data={
                    "path": relpath.as_posix(),
                    "chars": len(patch),
                    "staged": staged,
                },
            )
        except _SubprocessToolTimeout as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="tool_timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="git_export_patch_failed",
            )


class GitBranchTool(AgentTool):
    spec = ToolSpec(
        name="git.branch",
        description="Return read-only branch information for the workspace.",
        parameters={"type": "object", "properties": {"all": {"type": "boolean"}}},
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        command = (
            ["git", "branch", "--all"]
            if bool(arguments.get("all", False))
            else ["git", "branch", "--show-current"]
        )
        return _git_read(
            ToolCall(name=self.spec.name, arguments=arguments),
            context,
            command,
            "git_branch_failed",
        )


class GitCreateLocalBranchTool(AgentTool):
    needs_call_id = True
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="git.create_local_branch",
        description="Create a local git branch in the workspace. Never pushes or tracks a remote.",
        parameters={
            "type": "object",
            "properties": {
                "branch": {"type": "string"},
                "checkout": {"type": "boolean", "default": True},
            },
            "required": ["branch"],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        branch = str(arguments.get("branch", "")).strip()
        checkout = bool(arguments.get("checkout", True))
        if not _safe_branch_name(branch):
            return self._result(
                call,
                success=False,
                content=f"Invalid branch name: {branch}",
                error="invalid_branch",
            )
        if _is_protected_branch(branch, context.config.protected_branches):
            return self._result(
                call,
                success=False,
                content=f"Refusing to create protected branch name: {branch}",
                error="protected_branch",
                data={
                    "branch": branch,
                    "protected_branches": list(context.config.protected_branches),
                },
            )
        command = ["switch", "-c", branch] if checkout else ["branch", branch]
        try:
            before_branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            completed = _run_subprocess(
                _hardened_git_command(command, workspace=context.workspace),
                context=context,
                arguments=arguments,
                default_timeout=30,
                environment=_sanitized_git_environment(None),
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
                    "checkout": checkout,
                    "previous_branch": before_branch,
                    "returncode": completed.returncode,
                },
                error=None if completed.returncode == 0 else "git_create_branch_failed",
            )
        except _SubprocessToolTimeout as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="tool_timeout",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="git_create_branch_failed",
            )


class GitLogTool(AgentTool):
    spec = ToolSpec(
        name="git.log",
        description="Return recent git commits for the workspace. Read-only.",
        parameters={
            "type": "object",
            "properties": {"max_count": {"type": "integer", "minimum": 1, "maximum": 50}},
        },
        aliases=("log",),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        max_count = max(1, min(int(arguments.get("max_count", 10)), 50))
        try:
            completed = subprocess.run(  # noqa: S603 - fixed read-only git command  # nosec
                _hardened_git_command(
                    [
                        "log",
                        f"--max-count={max_count}",
                        "--pretty=format:%H%x1f%h%x1f%ct%x1f%an%x1f%s",
                    ],
                    workspace=context.workspace,
                ),
                cwd=context.workspace,
                env=_sanitized_git_environment(None),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=redact_text(completed.stderr or ""),
                    data={"returncode": completed.returncode},
                    error="git_log_failed",
                )
            commits = []
            for line in redact_text(completed.stdout or "").splitlines():
                full, short, timestamp, author, subject = (
                    line.split("\x1f", maxsplit=4) + ["", "", "", "", ""]
                )[:5]
                commits.append(
                    {
                        "commit": full,
                        "short": short,
                        "timestamp": int(timestamp) if timestamp.isdigit() else None,
                        "author": author,
                        "subject": subject,
                    }
                )
            return self._result(
                call,
                success=True,
                content=json.dumps({"commits": commits}, indent=2),
                data={"commits": commits, "returncode": completed.returncode},
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="git_log_failed"
            )


class GitShowTool(AgentTool):
    spec = ToolSpec(
        name="git.show",
        description="Show a bounded read-only git object, commit, or path diff from the workspace.",
        parameters={
            "type": "object",
            "properties": {
                "rev": {"type": "string"},
                "path": {"type": "string"},
                "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000},
            },
        },
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        rev = str(arguments.get("rev", "HEAD")).strip() or "HEAD"
        if rev.startswith("-") or "\x00" in rev or ":" in rev:
            return self._result(
                call, success=False, content="Invalid revision.", error="invalid_revision"
            )
        max_chars = max(1000, min(int(arguments.get("max_chars", 40_000)), 200_000))
        try:
            commit_sha = _git_output(
                context.workspace,
                ["git", "rev-parse", "--verify", f"{rev}^{{commit}}"],
            )
        except RuntimeError:
            return self._result(
                call,
                success=False,
                content="Revision does not resolve to a commit.",
                error="invalid_revision",
            )
        command = [
            "git",
            "show",
            "--no-ext-diff",
            "--no-textconv",
            "--stat",
            "--patch",
            "--format=fuller",
            commit_sha,
        ]
        path_arg = str(arguments.get("path", "")).strip()
        if path_arg:
            try:
                path = _safe_path(context.workspace, path_arg)
                _assert_workspace_path_allowed(
                    context, path, requested_path=path_arg
                )
                command.extend(["--", str(path.relative_to(context.workspace.resolve()))])
            except Exception as exc:  # noqa: BLE001
                return self._result(
                    call,
                    success=False,
                    content=redact_text(str(exc)),
                    error="invalid_path",
                )
        else:
            try:
                _assert_git_paths_allowed(
                    context,
                    _commit_git_paths(context.workspace, commit_sha),
                )
            except (OSError, RuntimeError, ValueError):
                return self._result(
                    call,
                    success=False,
                    content="Git commit includes a protected workspace path and was not rendered.",
                    error="git_show_path_blocked",
                )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed read-only git command  # nosec
                _hardened_git_command(
                    _git_command_arguments(command), workspace=context.workspace
                ),
                cwd=context.workspace,
                env=_sanitized_git_environment(None),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=redact_text(completed.stderr or ""),
                    data={"returncode": completed.returncode},
                    error="git_show_failed",
                )
            safe_stdout = redact_text(completed.stdout or "")
            truncated = len(safe_stdout) > max_chars
            content = safe_stdout[:max_chars] + ("\n... truncated ..." if truncated else "")
            return self._result(
                call,
                success=True,
                content=content,
                data={"returncode": completed.returncode, "truncated": truncated},
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=redact_text(str(exc)), error="git_show_failed"
            )


class GitCommitTool(AgentTool):
    wait_for_completion_on_timeout = True
    spec = ToolSpec(
        name="git.commit",
        description="Commit already-staged workspace changes. Requires explicit approval and never pushes.",
        parameters={
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "repair_review_id": {"type": "string"},
                "expected_branch": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                },
                "expected_head_sha": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
                "expected_tree_sha": {"type": "string", "pattern": "^[0-9a-f]{40,64}$"},
            },
            "required": ["message"],
            "anyOf": [
                {"required": ["repair_review_id"]},
                {
                    "required": [
                        "expected_branch",
                        "expected_head_sha",
                        "expected_tree_sha",
                    ]
                },
            ],
        },
        risk="high",
        requires_approval=True,
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        call = ToolCall(name=self.spec.name, arguments=arguments)
        message = str(arguments.get("message", "")).strip()
        if not message:
            return self._result(
                call, success=False, content="Missing commit message", error="missing_message"
            )
        if context.config.git_write_mode != "local_branch":
            return self._result(
                call,
                success=False,
                content=(
                    "git.commit is only available when git_write_mode=local_branch; "
                    f"current mode is {context.config.git_write_mode!r}."
                ),
                error="git_write_mode_blocked",
            )
        try:
            repair_review_id: str | None = None
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            if not branch:
                return self._result(
                    call,
                    success=False,
                    content="Refusing to commit from a detached HEAD.",
                    error="detached_head",
                )
            if _is_protected_branch(branch, context.config.protected_branches):
                return self._result(
                    call,
                    success=False,
                    content=f"Refusing to commit on protected branch: {branch}",
                    error="protected_branch",
                    data={
                        "branch": branch,
                        "protected_branches": list(context.config.protected_branches),
                    },
                )
            if _is_repair_branch(branch, context.config.worker_branch_prefix):
                with repair_action_lock(context.workspace):
                    _assert_repair_snapshot_workspace_safe(context)
                    review_check = _validate_repair_review_gate(
                        context.workspace,
                        branch,
                        str(arguments.get("repair_review_id", "")).strip(),
                    )
                    if not review_check["ok"]:
                        return self._result(
                            call,
                            success=False,
                            content=str(review_check["content"]),
                            error=str(review_check["error"]),
                            data={
                                key: value
                                for key, value in review_check.items()
                                if key not in {"ok", "content", "error"}
                            },
                        )
                    repair_review_id = str(review_check["review_id"])
                    final_check = _validate_repair_review_gate(
                        context.workspace, branch, repair_review_id
                    )
                    if not final_check["ok"]:
                        return self._result(
                            call,
                            success=False,
                            content=(
                                "Reviewed repair changed before exact commit construction; "
                                f"commit was not attempted. {final_check['content']}"
                            ),
                            error=str(final_check["error"]),
                            data={
                                key: value
                                for key, value in final_check.items()
                                if key not in {"ok", "content", "error"}
                            },
                        )
                    final_snapshot = dict(final_check["repair_snapshot"])
                    _assert_git_paths_allowed(
                        context,
                        _snapshot_changed_paths(final_snapshot),
                    )
                    completed = _run_exact_repair_commit(
                        context.workspace,
                        branch=branch,
                        expected_head=str(final_check["head_sha"]),
                        snapshot=final_snapshot,
                        message=message,
                    )
            else:
                expected_branch = str(arguments.get("expected_branch", "")).strip()
                expected_head = str(arguments.get("expected_head_sha", "")).strip()
                expected_tree = str(arguments.get("expected_tree_sha", "")).strip()
                if not expected_branch or not expected_head or not expected_tree:
                    return self._result(
                        call,
                        success=False,
                        content=(
                            "Non-repair commits require expected_branch, expected_head_sha, and "
                            "expected_tree_sha from git.status commit_preview so exact-call approval "
                            "is bound to the destination and staged tree."
                        ),
                        error="commit_preview_required",
                    )
                if (
                    not _safe_branch_name(expected_branch)
                    or not _valid_git_object_id(expected_head)
                    or not _valid_git_object_id(expected_tree)
                ):
                    return self._result(
                        call,
                        success=False,
                        content="The non-repair commit preview contains an invalid Git identity.",
                        error="commit_preview_invalid",
                    )
                _verify_nonrepair_commit_target(
                    context.workspace,
                    expected_branch=expected_branch,
                    expected_head=expected_head,
                )
                actual_tree = _current_index_tree_sha(context.workspace)
                if actual_tree != expected_tree:
                    return self._result(
                        call,
                        success=False,
                        content="The staged tree changed after commit approval was prepared.",
                        error="commit_preview_stale",
                        data={
                            "expected_tree_sha": expected_tree,
                            "actual_tree_sha": actual_tree,
                        },
                    )
                _assert_git_paths_allowed(context, _staged_git_paths(context.workspace))
                head_tree = _git_output(
                    context.workspace,
                    ["git", "rev-parse", f"{expected_head}^{{tree}}"],
                )
                if actual_tree == head_tree:
                    return self._result(
                        call,
                        success=False,
                        content="No staged changes to commit.",
                        error="nothing_to_commit",
                    )
                completed = _run_exact_index_commit(
                    context.workspace,
                    branch=expected_branch,
                    expected_head=expected_head,
                    tree_sha=actual_tree,
                    message=message,
                )
            content = redact_text(
                f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\n"
                f"STDERR:\n{completed.stderr}"
            )
            data: dict[str, Any] = {"returncode": completed.returncode}
            if completed.returncode == 0:
                sha = subprocess.run(  # nosec
                    _hardened_git_command(
                        ["rev-parse", "HEAD"], workspace=context.workspace
                    ),
                    cwd=context.workspace,
                    env=_sanitized_git_environment(None),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if sha.returncode == 0:
                    data["commit_sha"] = sha.stdout.strip()
            if repair_review_id:
                data["repair_review_id"] = repair_review_id
            return self._result(
                call,
                success=completed.returncode == 0,
                content=content,
                data=data,
                error=None if completed.returncode == 0 else "git_commit_failed",
            )
        except WorkspaceSecretIsolationError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="workspace_secret_isolation_required",
            )
        except _CommitPreviewStaleError as exc:
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                data=exc.data,
                error="commit_preview_stale",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call,
                success=False,
                content=redact_text(str(exc)),
                error="git_commit_failed",
            )


def _current_index_tree_sha(workspace: Path) -> str:
    completed = subprocess.run(  # noqa: S603 - fixed git executable and structured argv  # nosec
        _hardened_git_command(["write-tree"], workspace=workspace),
        cwd=workspace,
        env=_sanitized_git_environment(None),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            redact_text(f"Unable to preview staged Git tree: {completed.stderr}")
        )
    tree_sha = redact_text(completed.stdout or "").strip()
    if not _valid_git_object_id(tree_sha):
        raise ValueError("Git returned an invalid staged tree identity.")
    return tree_sha


def _valid_git_object_id(value: str) -> bool:
    return len(value) in {40, 64} and all(
        char in "0123456789abcdef" for char in value
    )


class _CommitPreviewStaleError(RuntimeError):
    def __init__(self, data: dict[str, Any]) -> None:
        super().__init__(
            "The branch or HEAD changed after commit approval was prepared; "
            "the approved commit was not attached."
        )
        self.data = data


def _verify_nonrepair_commit_target(
    workspace: Path,
    *,
    expected_branch: str,
    expected_head: str,
) -> None:
    actual_branch = _git_output(workspace, ["git", "branch", "--show-current"])
    actual_head = _git_output(workspace, ["git", "rev-parse", "HEAD"])
    drift_fields: list[str] = []
    if actual_branch != expected_branch:
        drift_fields.append("branch")
    if actual_head != expected_head:
        drift_fields.append("head_sha")
    if drift_fields:
        raise _CommitPreviewStaleError(
            {
                "expected_branch": expected_branch,
                "actual_branch": actual_branch or None,
                "expected_head_sha": expected_head,
                "actual_head_sha": actual_head,
                "drift_fields": drift_fields,
            }
        )


def _run_exact_index_commit(
    workspace: Path,
    *,
    branch: str,
    expected_head: str,
    tree_sha: str,
    message: str,
) -> subprocess.CompletedProcess[str]:
    deadline = time.monotonic() + 30.0
    environment = _sanitized_git_environment(None)
    _verify_nonrepair_commit_target(
        workspace,
        expected_branch=branch,
        expected_head=expected_head,
    )
    commit = _run_repair_git(
        workspace,
        ["commit-tree", tree_sha, "-p", expected_head, "-m", message],
        environment=environment,
        deadline=deadline,
    )
    if commit.returncode != 0:
        return _decoded_failure(commit, "Unable to create exact staged-tree commit.")
    commit_sha = commit.stdout.decode("ascii", errors="strict").strip()
    _verify_nonrepair_commit_target(
        workspace,
        expected_branch=branch,
        expected_head=expected_head,
    )
    updated = _run_repair_git(
        workspace,
        [
            "update-ref",
            "-m",
            f"commit: {message}",
            f"refs/heads/{branch}",
            commit_sha,
            expected_head,
        ],
        environment=environment,
        deadline=deadline,
    )
    if updated.returncode != 0:
        _verify_nonrepair_commit_target(
            workspace,
            expected_branch=branch,
            expected_head=expected_head,
        )
        return _decoded_failure(
            updated,
            "Git HEAD changed before atomic branch update; the exact commit was not attached.",
        )
    return subprocess.CompletedProcess(
        ["git", "update-ref"],
        0,
        stdout=f"[{branch} {commit_sha[:12]}] {message}\ntree={tree_sha}\n",
        stderr="",
    )


def _run_exact_repair_commit(
    workspace: Path,
    *,
    branch: str,
    expected_head: str,
    snapshot: dict[str, Any],
    message: str,
) -> subprocess.CompletedProcess[str]:
    """Build a literal reviewed tree in a private index and CAS the branch.

    Worktree bytes are hashed with ``hash-object --stdin`` and written into a
    temporary index, so repository clean/smudge filters, hooks, signing, and the
    caller's mutable index cannot change the approved candidate.  The real index
    is touched only after a successful compare-and-swap branch update.
    """

    root = require_git_root(workspace)
    if not _safe_branch_name(branch):
        return _completed_failure("git update-ref", "Unsafe repair branch name.")
    manifest = snapshot.get("changed_manifest")
    if not isinstance(manifest, list) or not manifest:
        return _completed_failure("git read-tree", "Reviewed repair manifest is empty.")
    if snapshot.get("head_sha") != expected_head:
        return _completed_failure("git read-tree", "Reviewed repair HEAD identity is invalid.")

    deadline = time.monotonic() + 30.0
    initial_index = _index_fingerprint(root)
    descriptor, temporary_name = tempfile.mkstemp(prefix="kestrel-repair-index-")
    os.close(descriptor)
    temporary_index = Path(temporary_name)
    temporary_index.unlink()
    environment = _sanitized_git_environment(temporary_index)
    try:
        read_tree = _run_repair_git(
            root,
            ["read-tree", expected_head],
            environment=environment,
            deadline=deadline,
        )
        if read_tree.returncode != 0:
            return _decoded_failure(read_tree, "Unable to initialize private repair index.")

        index_records = bytearray()
        zero_oid = "0" * len(expected_head)
        for raw_entry in manifest:
            if time.monotonic() >= deadline:
                return _completed_failure("git hash-object", "Exact repair commit timed out.")
            if not isinstance(raw_entry, dict):
                return _completed_failure("git update-index", "Repair manifest entry is invalid.")
            try:
                path, entry_type, mode, content = _read_reviewed_manifest_entry(root, raw_entry)
            except (OSError, ValueError) as exc:
                return _completed_failure("git hash-object", str(exc))
            encoded_path = os.fsencode(path)
            if entry_type == "deleted":
                index_records.extend(f"0 {zero_oid}\t".encode("ascii"))
                index_records.extend(encoded_path)
                index_records.append(0)
                continue
            hashed = _run_repair_git(
                root,
                ["hash-object", "-w", "--stdin"],
                environment=environment,
                deadline=deadline,
                input_bytes=content,
            )
            if hashed.returncode != 0:
                return _decoded_failure(hashed, f"Unable to hash reviewed repair path: {path}")
            object_id = hashed.stdout.decode("ascii", errors="strict").strip()
            index_records.extend(f"{mode} {object_id}\t".encode("ascii"))
            index_records.extend(encoded_path)
            index_records.append(0)

        indexed = _run_repair_git(
            root,
            ["update-index", "-z", "--index-info"],
            environment=environment,
            deadline=deadline,
            input_bytes=bytes(index_records),
        )
        if indexed.returncode != 0:
            return _decoded_failure(indexed, "Unable to build exact reviewed repair index.")
        tree = _run_repair_git(
            root,
            ["write-tree"],
            environment=environment,
            deadline=deadline,
        )
        if tree.returncode != 0:
            return _decoded_failure(tree, "Unable to capture exact reviewed repair tree.")
        tree_sha = tree.stdout.decode("ascii", errors="strict").strip()
        commit = _run_repair_git(
            root,
            ["commit-tree", tree_sha, "-p", expected_head, "-m", message],
            environment=environment,
            deadline=deadline,
        )
        if commit.returncode != 0:
            return _decoded_failure(commit, "Unable to create reviewed repair commit.")
        commit_sha = commit.stdout.decode("ascii", errors="strict").strip()
        updated = _run_repair_git(
            root,
            [
                "update-ref",
                "-m",
                f"commit: {message}",
                f"refs/heads/{branch}",
                commit_sha,
                expected_head,
            ],
            environment=environment,
            deadline=deadline,
        )
        if updated.returncode != 0:
            return _decoded_failure(
                updated,
                "Repair HEAD changed before atomic branch update; the reviewed commit was not attached.",
            )

        warning = ""
        if _index_fingerprint(root) == initial_index:
            real_environment = _sanitized_git_environment(None)
            synchronized = _run_repair_git(
                root,
                ["update-index", "-z", "--index-info"],
                environment=real_environment,
                deadline=deadline,
                input_bytes=bytes(index_records),
            )
            if synchronized.returncode != 0:
                warning = (
                    "Commit succeeded, but the real index could not be synchronized; "
                    "inspect git status before continuing.\n"
                    + synchronized.stderr.decode("utf-8", errors="replace")
                )
        else:
            warning = (
                "Commit succeeded, but the real index changed concurrently and was left untouched; "
                "inspect git status before continuing."
            )
        return subprocess.CompletedProcess(
            ["git", "update-ref"],
            0,
            stdout=f"[{branch} {commit_sha[:12]}] {message}\ntree={tree_sha}\n",
            stderr=warning,
        )
    finally:
        for candidate in (temporary_index, Path(f"{temporary_index}.lock")):
            try:
                candidate.unlink()
            except FileNotFoundError:
                pass


def _hardened_git_command(
    arguments: list[str],
    *,
    workspace: Path | None = None,
) -> list[str]:
    return hardened_readonly_git_command(
        ["-c", "commit.gpgSign=false", *arguments], workspace=workspace
    )


def _git_command_arguments(command: list[str]) -> list[str]:
    if not command or Path(command[0]).name.casefold() not in {"git", "git.exe"}:
        raise ValueError("Expected a structured Git command.")
    return command[1:]


def _sanitized_git_environment(index_path: Path | None) -> dict[str, str]:
    environment = hardened_readonly_git_environment()
    if index_path is not None:
        environment["GIT_INDEX_FILE"] = str(index_path)
    return environment


def _run_repair_git(
    workspace: Path,
    arguments: list[str],
    *,
    environment: dict[str, str],
    deadline: float,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return subprocess.CompletedProcess(
            _hardened_git_command(arguments, workspace=workspace),
            124,
            stdout=b"",
            stderr=b"Exact repair commit timed out.",
        )
    try:
        return subprocess.run(  # noqa: S603 - fixed git executable and structured argv  # nosec
            _hardened_git_command(arguments, workspace=workspace),
            cwd=workspace,
            env=environment,
            input=input_bytes,
            capture_output=True,
            timeout=remaining,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            _hardened_git_command(arguments, workspace=workspace),
            124,
            stdout=b"",
            stderr=b"Exact repair commit timed out.",
        )


def _read_reviewed_manifest_entry(
    workspace: Path,
    entry: dict[str, Any],
) -> tuple[str, str, str, bytes]:
    path = str(entry.get("path", ""))
    pure = Path(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "\x00" in path:
        raise ValueError(f"Unsafe path in reviewed repair manifest: {path!r}")
    candidate = workspace / pure
    entry_type = str(entry.get("type", ""))
    current = workspace
    for component in pure.parts[:-1]:
        current /= component
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if entry_type == "deleted":
                return path, entry_type, "0", b""
            raise ValueError(f"Reviewed repair path parent disappeared: {path}") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Reviewed repair path traverses an unsafe parent: {path}")
    if entry_type == "deleted":
        try:
            os.lstat(candidate)
        except FileNotFoundError:
            return path, entry_type, "0", b""
        raise ValueError(f"Reviewed deleted path now exists: {path}")
    if entry_type == "symlink":
        before = os.lstat(candidate)
        if not stat.S_ISLNK(before.st_mode):
            raise ValueError(f"Reviewed symlink changed type: {path}")
        content = os.fsencode(os.readlink(candidate))
        after = os.lstat(candidate)
        _verify_manifest_content(entry, before, after, content, path)
        return path, entry_type, "120000", content
    if entry_type != "regular":
        raise ValueError(f"Unsupported reviewed repair path type: {entry_type!r}")
    before = os.lstat(candidate)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Reviewed repair path changed type: {path}")
    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened_before = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 128 * 1024 * 1024:
                raise ValueError(f"Reviewed repair path is too large: {path}")
            chunks.append(chunk)
        opened_after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.lstat(candidate)
    if (
        not os.path.samestat(before, opened_before)
        or not os.path.samestat(opened_before, opened_after)
        or not os.path.samestat(opened_after, after)
        or _git_mutable_stat_fields(opened_before) != _git_mutable_stat_fields(opened_after)
        or _git_mutable_stat_fields(opened_after) != _git_mutable_stat_fields(after)
    ):
        raise ValueError(f"Reviewed repair path changed while committing: {path}")
    content = b"".join(chunks)
    _verify_manifest_content(entry, before, after, content, path)
    mode = "100755" if stat.S_IMODE(after.st_mode) & 0o111 else "100644"
    return path, entry_type, mode, content


def _verify_manifest_content(
    entry: dict[str, Any],
    before: os.stat_result,
    after: os.stat_result,
    content: bytes,
    path: str,
) -> None:
    if (
        not os.path.samestat(before, after)
        or int(entry.get("mode", -1)) != stat.S_IMODE(after.st_mode)
        or int(entry.get("size", -1)) != len(content)
        or str(entry.get("sha256", "")) != hashlib.sha256(content).hexdigest()
    ):
        raise ValueError(f"Reviewed repair manifest no longer matches: {path}")


def _git_mutable_stat_fields(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


def _index_fingerprint(workspace: Path) -> tuple[str, int, int] | None:
    raw = _git_output(workspace, ["git", "rev-parse", "--git-path", "index"])
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        before = os.lstat(candidate)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_size > 512 * 1024 * 1024:
        raise ValueError("Git index is not a bounded regular file.")
    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        opened = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after = os.lstat(candidate)
    if not os.path.samestat(before, opened) or not os.path.samestat(opened, after):
        raise ValueError("Git index changed while fingerprinting.")
    return digest.hexdigest(), after.st_size, after.st_mtime_ns


def _decoded_failure(
    completed: subprocess.CompletedProcess[bytes],
    prefix: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout=redact_text(completed.stdout.decode("utf-8", errors="replace")),
        stderr=redact_text(
            f"{prefix}\n{completed.stderr.decode('utf-8', errors='replace')}"
        ),
    )


def _completed_failure(command: str, message: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        command.split(), 1, stdout="", stderr=redact_text(message)
    )
