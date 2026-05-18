from __future__ import annotations

import hashlib
import json
import subprocess  # nosec B404
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from .base import AgentTool, ToolContext
from .workspace_tools import _safe_path


def _safe_branch_name(name: str) -> bool:
    if not name or name.startswith(("-", "/")) or name.endswith(("/", ".", ".lock")):
        return False
    if ".." in name or "//" in name or "@{" in name or "\\" in name:
        return False
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._/-")
    return all(char in allowed for char in name)


def _is_repair_branch(name: str) -> bool:
    return name.startswith(("codex/", "repair/", "fix/")) and name not in {"main", "master"}


def _is_protected_branch(name: str, protected_patterns: tuple[str, ...]) -> bool:
    branch = name.strip()
    return bool(branch) and any(fnmatchcase(branch, pattern) for pattern in protected_patterns)


def _git_output(workspace: Path, command: list[str]) -> str:
    completed = subprocess.run(  # nosec
        command,
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"git command failed ({completed.returncode}): {' '.join(command)}\n{completed.stderr}"
        )
    return completed.stdout.strip()


def _validate_repair_review_gate(workspace: Path, branch: str, review_id: str) -> dict[str, Any]:
    if not review_id:
        return {
            "ok": False,
            "error": "repair_review_required",
            "content": "Repair branch commits require a repair_review_id from repair.review.",
            "branch": branch,
        }
    if not review_id.startswith("repair_review_") or "/" in review_id or ".." in review_id:
        return {
            "ok": False,
            "error": "invalid_repair_review_id",
            "content": f"Invalid repair_review_id: {review_id}",
            "branch": branch,
        }
    path = workspace / ".nest" / "repair_reviews" / f"{review_id}.json"
    if not path.exists():
        return {
            "ok": False,
            "error": "repair_review_not_found",
            "content": f"Repair review artifact not found: {review_id}",
            "branch": branch,
        }
    try:
        review = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {
            "ok": False,
            "error": "repair_review_invalid",
            "content": f"Repair review artifact is invalid JSON: {exc}",
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
    diff = _git_output(workspace, ["git", "diff", "HEAD", "--"])
    diff_hash = hashlib.sha256(diff.encode("utf-8")).hexdigest()
    if review.get("diff_hash") != diff_hash:
        return {
            "ok": False,
            "error": "repair_review_stale",
            "content": "Repair diff changed after review; run repair.review again before committing.",
            "branch": branch,
            "review_id": review_id,
            "expected_diff_hash": review.get("diff_hash"),
            "actual_diff_hash": diff_hash,
        }
    return {
        "ok": True,
        "review_id": review_id,
        "diff_hash": diff_hash,
        "branch": branch,
        "changed_files": review.get("changed_files", []),
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


def _git_read(
    call: ToolCall, context: ToolContext, command: list[str], error_code: str
) -> ToolExecution:
    try:
        completed = subprocess.run(  # noqa: S603 - fixed read-only git commands  # nosec
            command,
            cwd=context.workspace,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
        return ToolExecution(
            call=call,
            success=completed.returncode == 0,
            content=content,
            data={"returncode": completed.returncode},
            error=None if completed.returncode == 0 else error_code,
        )
    except Exception as exc:  # noqa: BLE001
        return ToolExecution(call=call, success=False, content=str(exc), error=error_code)


class GitStatusTool(AgentTool):
    spec = ToolSpec(
        name="git.status",
        description="Return read-only git status for the workspace.",
        parameters={"type": "object", "properties": {}},
        aliases=("status",),
    )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del arguments
        call = ToolCall(name=self.spec.name, arguments={})
        return _git_read(
            call, context, ["git", "status", "--short", "--branch"], "git_status_failed"
        )


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
        command = (
            ["git", "diff", "--cached"] if bool(arguments.get("staged", False)) else ["git", "diff"]
        )
        result = _git_read(call, context, command, "git_diff_failed")
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
        command = (
            ["git", "diff", "--cached"] if bool(arguments.get("staged", False)) else ["git", "diff"]
        )
        try:
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments  # nosec
                command,
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            if completed.returncode != 0:
                return self._result(
                    call,
                    success=False,
                    content=f"Unable to export patch. STDERR:\n{completed.stderr}",
                    error="git_export_patch_failed",
                    data={"returncode": completed.returncode},
                )
            patch = completed.stdout
            if not patch.strip():
                return self._result(
                    call, success=False, content="No diff to export.", error="empty_diff"
                )
            path_arg = str(arguments.get("path", "")).strip()
            if path_arg:
                patch_path = _safe_path(context.workspace, path_arg)
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
                    "staged": bool(arguments.get("staged", False)),
                },
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="git_export_patch_failed"
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
        command = ["git", "switch", "-c", branch] if checkout else ["git", "branch", branch]
        try:
            before_branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments  # nosec
                command,
                cwd=context.workspace,
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
                    "checkout": checkout,
                    "previous_branch": before_branch,
                    "returncode": completed.returncode,
                },
                error=None if completed.returncode == 0 else "git_create_branch_failed",
            )
        except Exception as exc:  # noqa: BLE001
            return self._result(
                call, success=False, content=str(exc), error="git_create_branch_failed"
            )


class GitCommitTool(AgentTool):
    spec = ToolSpec(
        name="git.commit",
        description="Commit already-staged workspace changes. Requires explicit approval and never pushes.",
        parameters={
            "type": "object",
            "properties": {"message": {"type": "string"}, "repair_review_id": {"type": "string"}},
            "required": ["message"],
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
        try:
            repair_review_id: str | None = None
            branch = _git_output(context.workspace, ["git", "branch", "--show-current"])
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
            if _is_repair_branch(branch):
                review_check = _validate_repair_review_gate(
                    context.workspace, branch, str(arguments.get("repair_review_id", "")).strip()
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
                changed_files = [
                    str(path) for path in review_check.get("changed_files", []) if str(path).strip()
                ]
                if changed_files:
                    staged = subprocess.run(  # nosec
                        ["git", "add", "--", *changed_files],
                        cwd=context.workspace,
                        capture_output=True,
                        text=True,
                        timeout=30,
                        check=False,
                    )
                    if staged.returncode != 0:
                        return self._result(
                            call,
                            success=False,
                            content=f"Unable to stage reviewed repair files. STDERR:\n{staged.stderr}",
                            error="repair_stage_failed",
                            data={
                                "branch": branch,
                                "repair_review_id": repair_review_id,
                                "changed_files": changed_files,
                                "returncode": staged.returncode,
                            },
                        )
            completed = subprocess.run(  # noqa: S603 - fixed executable and arguments  # nosec
                ["git", "commit", "-m", message],
                cwd=context.workspace,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            content = f"exit_code={completed.returncode}\nSTDOUT:\n{completed.stdout}\nSTDERR:\n{completed.stderr}"
            data: dict[str, Any] = {"returncode": completed.returncode}
            if completed.returncode == 0:
                sha = subprocess.run(  # nosec
                    ["git", "rev-parse", "HEAD"],
                    cwd=context.workspace,
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
        except Exception as exc:  # noqa: BLE001
            return self._result(call, success=False, content=str(exc), error="git_commit_failed")
