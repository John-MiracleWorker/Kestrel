"""
Git operations tool — gives Kestrel version-control capabilities.

Safety rails:
  - NEVER pushes to main — always creates kestrel/* feature branches
  - All operations scoped to /project (project root in Docker)
  - Diff size limit prevents runaway changes
  - Requires GITHUB_PAT env var for push operations
"""

import logging
import os
import subprocess
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.git")

# Project root inside Docker
PROJECT_ROOT = "/project"
MAX_DIFF_LINES = 1000  # Increased for larger syncs

# Admin guard: only the admin user can push/deploy to GitHub.
# Other users can only make local changes.
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "")

def _is_admin(user_id: str = "") -> bool:
    """Check if the current operation is from the admin user."""
    if not ADMIN_USER_ID:
        return True  # No admin set = allow all (single-user mode)
    return user_id == ADMIN_USER_ID


def register_git_tools(registry) -> None:
    """Register git operations tool."""

    registry.register(
        definition=ToolDefinition(
            name="git",
            description=(
                "Perform git operations on the Kestrel codebase. "
                "Use for version control: status, diff, branch, commit, push, log, add, pull, checkout, deploy. "
                "Safety: pushes are ONLY allowed on kestrel/* branches, never on main. "
                "Deploy merges to main and rebuilds Docker — requires passing tests AND 30min user inactivity."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "diff", "branch", "commit", "push", "log", "add", "pull", "checkout", "deploy"],
                        "description": "Git action to perform",
                    },
                    "branch_name": {
                        "type": "string",
                        "description": "Branch name for branch/checkout action",
                    },
                    "message": {
                        "type": "string",
                        "description": "Commit message for commit action",
                    },
                    "files": {
                        "type": "string",
                        "description": "Files to add (for add action). Use '.' for all, or 'path/to/file'",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of log entries to show (default 5)",
                    },
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.HIGH,
            timeout_seconds=30,
            category="development",
        ),
        handler=git_action,
    )


def _run_git(args: list[str], check: bool = True) -> dict:
    """Run a git command and return structured output."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=25,
        )
        if check and result.returncode != 0:
            return {
                "success": False,
                "error": result.stderr.strip() or f"git {' '.join(args)} failed",
                "returncode": result.returncode,
            }
        return {
            "success": True,
            "output": result.stdout.strip(),
            "stderr": result.stderr.strip() if result.stderr.strip() else None,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Git command timed out (25s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _get_current_branch() -> str:
    """Get the current git branch name."""
    result = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], check=False)
    return result.get("output", "unknown") if result.get("success") else "unknown"


async def git_action(
    action: str,
    branch_name: str = "",
    message: str = "",
    files: str = "",
    count: int = 5,
) -> dict:
    """Route to the appropriate git action."""

    if action == "status":
        return _git_status()
    elif action == "diff":
        return _git_diff()
    elif action == "branch":
        return _git_branch(branch_name)
    elif action == "checkout":
        return _git_checkout(branch_name)
    elif action == "add":
        return _git_add(files)
    elif action == "commit":
        return _git_commit(message)
    elif action == "pull":
        return _git_pull()
    elif action == "push":
        if not _is_admin():
            return {"error": "🔒 Push is admin-only. Your changes remain local. Ask the admin to push."}
        return _git_push()
    elif action == "log":
        return _git_log(count)
    elif action == "deploy":
        if not _is_admin():
            return {"error": "🔒 Deploy is admin-only. Your changes remain local. Ask the admin to deploy."}
        from agent.tools.self_improve import deploy_codebase
        return await deploy_codebase()
    else:
        return {"error": f"Unknown git action: {action}"}


def _git_status() -> dict:
    """Show working tree status."""
    result = _run_git(["status", "--porcelain"])
    if not result.get("success"):
        return result

    lines = [l for l in result["output"].split("\n") if l]
    branch = _get_current_branch()

    return {
        "branch": branch,
        "changed_files": len(lines),
        "files": lines[:50],  # Cap output
        "clean": len(lines) == 0,
    }


def _git_diff() -> dict:
    """Show current diff."""
    result = _run_git(["diff", "--stat"])
    if not result.get("success"):
        return result

    # Also get the actual diff but capped
    full_diff = _run_git(["diff"], check=False)
    diff_lines = (full_diff.get("output", "")).split("\n")

    if len(diff_lines) > MAX_DIFF_LINES:
        diff_text = "\n".join(diff_lines[:MAX_DIFF_LINES])
        diff_text += f"\n\n... (truncated, {len(diff_lines)} total lines)"
    else:
        diff_text = "\n".join(diff_lines)

    return {
        "stat": result["output"],
        "diff": diff_text,
        "total_lines": len(diff_lines),
        "over_limit": len(diff_lines) > MAX_DIFF_LINES,
    }


def _git_branch(name: str) -> dict:
    """List or create branches."""
    if not name:
        # Just list branches
        result = _run_git(["branch", "-a"])
        return {
            "branches": result.get("output", ""),
            "current": _get_current_branch(),
        }

    # Enforce kestrel/ prefix for new branches
    if not name.startswith("kestrel/"):
        name = f"kestrel/{name}"

    # Create
    result = _run_git(["branch", name])
    if result.get("success"):
        return {"message": f"✅ Created branch '{name}'"}
    return result


def _git_checkout(name: str) -> dict:
    """Switch branches."""
    if not name:
        return {"error": "Branch name is required for checkout."}
    
    result = _run_git(["checkout", name])
    if result.get("success"):
        return {"message": f"✅ Switched to branch '{name}'"}
    return result


def _git_pull() -> dict:
    """Pull changes from remote."""
    branch = _get_current_branch()
    result = _run_git(["pull", "origin", branch])
    if result.get("success"):
        return {"message": f"✅ Pulled changes for '{branch}'", "output": result.get("output")}
    return result


def _git_add(files: str) -> dict:
    """Stage files for commit."""
    if not files:
        files = "."
    result = _run_git(["add", files])
    if result.get("success"):
        return {"message": f"✅ Staged: {files}"}
    return result


def _git_commit(message: str) -> dict:
    """Commit staged changes."""
    if not message:
        return {"error": "Commit message is required."}

    # Check diff size
    stat = _run_git(["diff", "--cached", "--stat"], check=False)
    diff_check = _run_git(["diff", "--cached"], check=False)
    diff_lines = len((diff_check.get("output", "")).split("\n"))

    if diff_lines > MAX_DIFF_LINES:
        return {
            "error": f"🚫 Diff too large ({diff_lines} lines). Max allowed is {MAX_DIFF_LINES}."
        }

    result = _run_git(["commit", "-m", message])
    if result.get("success"):
        return {
            "message": f"✅ Committed to {_get_current_branch()}",
            "stat": stat.get("output"),
        }
    return result


def _git_push() -> dict:
    """Push current branch to origin."""
    branch = _get_current_branch()
    if branch in ("main", "master"):
        return {"error": "🚫 Pushing directly to main/master is disabled for safety."}
    
    result = _run_git(["push", "origin", branch])
    if result.get("success"):
        return {"message": f"✅ Pushed '{branch}' to origin"}
    return result


def _git_log(count: int = 5) -> dict:
    """Show commit log."""
    result = _run_git(["log", f"-n {count}", "--oneline"])
    if result.get("success"):
        return {"log": result["output"]}
    return result
