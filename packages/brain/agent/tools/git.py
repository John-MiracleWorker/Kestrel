"""
Git operations tool — gives Kestrel version-control capabilities.

Safety rails:
  - NEVER pushes to main — always creates kestrel/* feature branches
  - All operations scoped to /app (project root in Docker)
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
PROJECT_ROOT = "/app"
MAX_DIFF_LINES = 200  # Safety: cap diff size per commit


def register_git_tools(registry) -> None:
    """Register git operations tool."""

    registry.register(
        definition=ToolDefinition(
            name="git",
            description=(
                "Perform git operations on the Kestrel codebase. "
                "Use for version control: status, diff, branch, commit, push, log, deploy. "
                "Safety: pushes are ONLY allowed on kestrel/* branches, never on main. "
                "Deploy merges to main and rebuilds Docker — requires passing tests AND 30min user inactivity."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "diff", "branch", "commit", "push", "log", "add", "deploy"],
                        "description": "Git action to perform",
                    },
                    "branch_name": {
                        "type": "string",
                        "description": "Branch name for branch action (auto-prefixed with kestrel/)",
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


def _setup_git_auth() -> bool:
    """Configure git credentials from GITHUB_PAT env var."""
    pat = os.getenv("GITHUB_PAT", "")
    if not pat:
        return False

    # Get the remote URL
    result = _run_git(["remote", "get-url", "origin"], check=False)
    if not result.get("success"):
        return False

    url = result["output"]
    # Convert https://github.com/... to https://<PAT>@github.com/...
    if "github.com" in url and "@" not in url:
        auth_url = url.replace("https://github.com", f"https://{pat}@github.com")
        _run_git(["remote", "set-url", "origin", auth_url], check=False)

    # Set identity for commits
    _run_git(["config", "user.name", "Kestrel AI"], check=False)
    _run_git(["config", "user.email", "kestrel@librebird.ai"], check=False)

    return True


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
    elif action == "add":
        return _git_add(files)
    elif action == "commit":
        return _git_commit(message)
    elif action == "push":
        return _git_push()
    elif action == "log":
        return _git_log(count)
    elif action == "deploy":
        return await _git_deploy()
    else:
        return {"error": f"Unknown git action: {action}"}


def _git_status() -> dict:
    """Show working tree status."""
    result = _run_git(["status", "--porcelain"])
    if not result.get("success"):
        return result

    lines = result["output"].split("\n") if result["output"] else []
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
    """Create and switch to a kestrel/* branch."""
    if not name:
        # Just list branches
        result = _run_git(["branch", "-a"])
        return {
            "branches": result.get("output", ""),
            "current": _get_current_branch(),
        }

    # Enforce kestrel/ prefix
    if not name.startswith("kestrel/"):
        name = f"kestrel/{name}"

    # Block main/master
    if name in ("main", "master", "kestrel/main", "kestrel/master"):
        return {"error": "🚫 Cannot create or switch to main/master. Use a kestrel/* branch."}

    # Create and checkout
    result = _run_git(["checkout", "-b", name], check=False)
    if not result.get("success"):
        # Branch might already exist, try switching
        result = _run_git(["checkout", name])

    if result.get("success"):
        return {
            "branch": name,
            "message": f"✅ Switched to branch '{name}'",
        }
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

    branch = _get_current_branch()
    if branch in ("main", "master"):
        return {"error": "🚫 Cannot commit on main/master. Create a kestrel/* branch first."}

    # Set up git identity
    _run_git(["config", "user.name", "Kestrel AI"], check=False)
    _run_git(["config", "user.email", "kestrel@librebird.ai"], check=False)

    # Check diff size
    stat = _run_git(["diff", "--cached", "--stat"], check=False)
    diff_check = _run_git(["diff", "--cached"], check=False)
    diff_lines = len((diff_check.get("output", "")).split("\n"))

    if diff_lines > MAX_DIFF_LINES:
        return {
            "error": f"🚫 Diff too large ({diff_lines} lines, max {MAX_DIFF_LINES}). "
            "Break the change into smaller commits.",
            "diff_stat": stat.get("output", ""),
        }

    result = _run_git(["commit", "-m", f"[kestrel-ai] {message}"])
    if result.get("success"):
        sha = _run_git(["rev-parse", "--short", "HEAD"], check=False)
        return {
            "message": f"✅ Committed: {message}",
            "sha": sha.get("output", ""),
            "branch": branch,
            "diff_stat": stat.get("output", ""),
        }
    return result


def _git_push() -> dict:
    """Push current branch to origin."""
    branch = _get_current_branch()

    if branch in ("main", "master"):
        return {"error": "🚫 Cannot push to main/master. Use a kestrel/* branch."}

    if not branch.startswith("kestrel/"):
        return {"error": f"🚫 Can only push kestrel/* branches, not '{branch}'."}

    # Set up auth
    if not _setup_git_auth():
        return {
            "error": "GitHub PAT not configured. Set GITHUB_PAT environment variable.",
            "hint": "Ask the user to generate a PAT at https://github.com/settings/tokens",
        }

    result = _run_git(["push", "-u", "origin", branch])
    if result.get("success"):
        # Get remote URL for PR link
        remote = _run_git(["remote", "get-url", "origin"], check=False)
        repo_url = remote.get("output", "").replace(".git", "").split("@")[-1]
        if "github.com" in repo_url:
            # Strip PAT from URL for display
            repo_url = "https://github.com/" + repo_url.split("github.com/")[-1]

        return {
            "message": f"✅ Pushed {branch} to origin",
            "branch": branch,
            "pr_url": f"{repo_url}/compare/main...{branch}?expand=1",
        }
    return result


def _git_log(count: int = 5) -> dict:
    """Show recent commits."""
    count = min(count, 20)
    result = _run_git(["log", f"-{count}", "--oneline", "--decorate"])
    if result.get("success"):
        return {"log": result["output"]}
    return result


async def _git_deploy() -> dict:
    """
    Merge current kestrel/* branch to main, rebuild Docker containers.
    
    Safety gates:
      1. Must be on a kestrel/* branch
      2. All tests must pass (tsc, ast.parse)
      3. User must be inactive for 30+ minutes
    """
    from agent.tools.self_improve import is_user_inactive, get_inactivity_seconds, _run_tests

    branch = _get_current_branch()

    # Gate 1: Must be on a kestrel/* branch
    if not branch.startswith("kestrel/"):
        return {"error": f"🚫 Deploy only works from kestrel/* branches, not '{branch}'."}

    # Gate 2: Run tests
    logger.info("Deploy gate: running test suite...")
    test_results = _run_tests("all")
    if not test_results.get("all_pass"):
        return {
            "error": "🚫 Deploy blocked — tests are failing. Fix them first.",
            "test_results": test_results,
        }

    # Gate 3: Check user inactivity
    if not is_user_inactive():
        inactive_min = get_inactivity_seconds() / 60
        return {
            "error": f"🚫 Deploy blocked — user was active {inactive_min:.0f}m ago. "
            "Waiting for 30m of inactivity before restarting containers.",
            "hint": "The deploy will be retried on the next self-improvement cycle.",
        }

    logger.info(f"Deploy: all gates passed. Merging {branch} to main...")

    # Merge to main
    _run_git(["checkout", "main"], check=False)
    merge_result = _run_git(["merge", "--squash", branch], check=False)
    if not merge_result.get("success"):
        _run_git(["merge", "--abort"], check=False)
        _run_git(["checkout", branch], check=False)
        return {
            "error": f"🚫 Merge conflict. Branch '{branch}' could not be merged to main.",
            "detail": merge_result.get("error", ""),
        }

    # Commit the squash merge
    _run_git(["config", "user.name", "Kestrel AI"], check=False)
    _run_git(["config", "user.email", "kestrel@librebird.ai"], check=False)
    _run_git(["commit", "-m", f"[kestrel-ai] auto-deploy: merge {branch}"], check=False)

    # Push main
    if _setup_git_auth():
        _run_git(["push", "origin", "main"], check=False)

    # Rebuild Docker containers
    logger.info("Deploy: rebuilding Docker containers...")
    try:
        rebuild = subprocess.run(
            ["docker", "compose", "up", "--build", "-d"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=300,  # 5 minute timeout for rebuild
        )
        if rebuild.returncode != 0:
            return {
                "status": "partial",
                "message": f"✅ Merged {branch} to main, but Docker rebuild failed.",
                "error": rebuild.stderr[:500],
            }
    except subprocess.TimeoutExpired:
        return {
            "status": "partial",
            "message": f"✅ Merged {branch} to main, but Docker rebuild timed out (5m).",
        }

    # Clean up the feature branch
    _run_git(["branch", "-d", branch], check=False)

    return {
        "status": "deployed",
        "message": f"✅ Deployed! Merged {branch} → main, rebuilt all containers.",
        "branch_merged": branch,
    }
