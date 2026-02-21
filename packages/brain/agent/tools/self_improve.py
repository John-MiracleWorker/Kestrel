"""
Self-Improvement Engine — Kestrel analyzes its own codebase and fixes issues.

Flow:
  1. Scan → identify lint errors, TODOs, type issues, dead code
  2. Pick one → prioritize by impact
  3. Branch → create kestrel/fix-* branch
  4. Fix → apply changes
  5. Test → tsc, ast.parse, basic health checks
  6. Commit & Push → only if tests pass
  7. Report → send summary via Telegram or return to agent

Safety:
  - Max 200 lines changed per cycle
  - Tests must pass before commit
  - Never touches main branch
  - Backs off after 3 consecutive no-op cycles
"""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.self_improve")

PROJECT_ROOT = "/project"

# ── Inactivity Guard ─────────────────────────────────────────────────
# Self-improvement will NOT restart containers or auto-deploy unless
# the user has been inactive for this many seconds.
INACTIVITY_THRESHOLD_SECONDS = 30 * 60  # 30 minutes

# Module-level timestamp updated by Brain server on every user interaction
_last_user_activity: float = time.time()


def touch_user_activity() -> None:
    """Called by Brain server whenever a user sends a message or API request."""
    global _last_user_activity
    _last_user_activity = time.time()


def is_user_inactive() -> bool:
    """Check if the user has been inactive long enough for auto-deploy."""
    return (time.time() - _last_user_activity) > INACTIVITY_THRESHOLD_SECONDS


def get_inactivity_seconds() -> float:
    """Get seconds since last user activity."""
    return time.time() - _last_user_activity


@dataclass
class ImprovementReport:
    """Result of a self-improvement cycle."""
    cycle_id: str = ""
    status: str = "pending"  # pending, improved, no_issues, failed, skipped
    issue_found: str = ""
    fix_description: str = ""
    files_changed: list[str] = field(default_factory=list)
    tests_passed: bool = False
    branch: str = ""
    commit_sha: str = ""
    error: str = ""
    duration_seconds: float = 0

    def to_dict(self) -> dict:
        return {
            "cycle_id": self.cycle_id,
            "status": self.status,
            "issue_found": self.issue_found,
            "fix_description": self.fix_description,
            "files_changed": self.files_changed,
            "tests_passed": self.tests_passed,
            "branch": self.branch,
            "commit_sha": self.commit_sha,
            "error": self.error,
            "duration_seconds": round(self.duration_seconds, 1),
        }

    def telegram_summary(self) -> str:
        """Format report for Telegram notification."""
        if self.status == "improved":
            return (
                f"🔧 *Kestrel Self-Improvement*\n\n"
                f"✅ *Fixed:* {self.issue_found}\n"
                f"📝 {self.fix_description}\n"
                f"📁 Files: {', '.join(self.files_changed)}\n"
                f"🌿 Branch: `{self.branch}`\n"
                f"🔗 Ready for review on GitHub\n"
                f"⏱ {self.duration_seconds:.0f}s"
            )
        elif self.status == "no_issues":
            return "🟢 *Kestrel Self-Improvement*\n\nCodebase scan complete — no issues found. Everything looks clean!"
        elif self.status == "failed":
            return (
                f"⚠️ *Kestrel Self-Improvement*\n\n"
                f"Found issue: {self.issue_found}\n"
                f"But fix failed: {self.error}\n"
                f"No changes committed."
            )
        else:
            return f"ℹ️ *Kestrel Self-Improvement*\n\nCycle {self.status}: {self.error or 'No action taken'}"


def register_self_improve_tools(registry) -> None:
    """Register the self-improvement tool."""

    registry.register(
        definition=ToolDefinition(
            name="self_improve",
            description=(
                "Analyze the Kestrel codebase and find improvements. "
                "Use action='scan' to identify issues, 'test' to run test suite, "
                "or 'report' to generate a summary of the last cycle. "
                "The agent loop uses this during scheduled self-improvement cycles."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["scan", "test", "report"],
                        "description": "Self-improvement action to perform",
                    },
                    "package": {
                        "type": "string",
                        "enum": ["brain", "gateway", "web", "all"],
                        "description": "Which package to scan/test (default: all)",
                    },
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.MEDIUM,
            timeout_seconds=60,
            category="development",
        ),
        handler=self_improve_action,
    )


async def self_improve_action(
    action: str,
    package: str = "all",
) -> dict:
    """Route to the appropriate self-improvement action."""

    if action == "scan":
        return _scan_codebase(package)
    elif action == "test":
        return _run_tests(package)
    elif action == "report":
        return _get_last_report()
    else:
        return {"error": f"Unknown action: {action}"}


def _run_cmd(cmd: list[str], cwd: str = PROJECT_ROOT, timeout: int = 45) -> dict:
    """Run a command and return output."""
    try:
        result = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[:5000],  # Cap output
            "stderr": result.stderr[:2000],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": "Command timed out", "returncode": -1}
    except Exception as e:
        return {"success": False, "stdout": "", "stderr": str(e), "returncode": -1}


def _scan_codebase(package: str = "all") -> dict:
    """Scan the codebase for issues and improvement opportunities."""
    issues = []

    packages = ["brain", "gateway", "web"] if package == "all" else [package]

    for pkg in packages:
        pkg_path = os.path.join(PROJECT_ROOT, "packages", pkg)

        if not os.path.isdir(pkg_path):
            continue

        # 1. Python syntax check (brain)
        if pkg == "brain":
            py_issues = _scan_python(pkg_path)
            issues.extend(py_issues)

        # 2. TypeScript type check (gateway, web)
        if pkg in ("gateway", "web"):
            ts_issues = _scan_typescript(pkg_path)
            issues.extend(ts_issues)

        # 3. TODO/FIXME/HACK scan
        todo_issues = _scan_todos(pkg_path)
        issues.extend(todo_issues)

        # 4. Dead import detection (Python)
        if pkg == "brain":
            import_issues = _scan_dead_imports(pkg_path)
            issues.extend(import_issues)

    # Sort by priority (errors > warnings > todos)
    priority_order = {"error": 0, "warning": 1, "todo": 2, "style": 3}
    issues.sort(key=lambda x: priority_order.get(x.get("severity", "style"), 99))

    return {
        "package": package,
        "total_issues": len(issues),
        "issues": issues[:20],  # Cap at 20
        "summary": _summarize_issues(issues),
    }


def _scan_python(pkg_path: str) -> list[dict]:
    """Check Python files for syntax errors."""
    issues = []
    src_path = os.path.join(pkg_path)

    for root, _, files in os.walk(src_path):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, PROJECT_ROOT)

            result = _run_cmd(
                ["python3", "-c", f"import ast; ast.parse(open('{fpath}').read())"],
                timeout=10,
            )
            if not result["success"]:
                issues.append({
                    "file": rel,
                    "severity": "error",
                    "type": "syntax_error",
                    "message": result["stderr"][:200],
                })

    return issues


def _scan_typescript(pkg_path: str) -> list[dict]:
    """Run tsc --noEmit and parse errors."""
    issues = []

    result = _run_cmd(
        ["npx", "tsc", "--noEmit", "--pretty", "false"],
        cwd=pkg_path,
        timeout=30,
    )

    if not result["success"] and result["stdout"]:
        for line in result["stdout"].split("\n")[:10]:
            if "error TS" in line:
                issues.append({
                    "file": line.split("(")[0] if "(" in line else "unknown",
                    "severity": "error",
                    "type": "type_error",
                    "message": line[:200],
                })

    return issues


def _scan_todos(pkg_path: str) -> list[dict]:
    """Find TODO/FIXME/HACK comments."""
    issues = []

    result = _run_cmd(
        ["grep", "-rn", "--include=*.py", "--include=*.ts", "--include=*.tsx",
         "-E", r"(TODO|FIXME|HACK|XXX):", pkg_path],
        timeout=10,
    )

    if result["stdout"]:
        for line in result["stdout"].split("\n")[:15]:
            if not line.strip():
                continue
            rel = os.path.relpath(line.split(":")[0], PROJECT_ROOT) if ":" in line else line
            issues.append({
                "file": rel,
                "severity": "todo",
                "type": "todo_comment",
                "message": line.split(":", 2)[-1].strip()[:150] if ":" in line else line[:150],
            })

    return issues


def _scan_dead_imports(pkg_path: str) -> list[dict]:
    """Quick dead import check for Python files."""
    issues = []

    # Use a simple heuristic: find imports then grep for usage
    result = _run_cmd(
        ["grep", "-rn", "--include=*.py", r"^import \|^from .* import ", pkg_path],
        timeout=10,
    )

    # This is a lightweight scan — the agent can do deeper analysis
    if result["stdout"]:
        import_count = len(result["stdout"].split("\n"))
        if import_count > 0:
            issues.append({
                "file": "packages/brain/",
                "severity": "style",
                "type": "import_audit",
                "message": f"{import_count} imports found — agent can analyze for unused ones",
            })

    return issues


def _summarize_issues(issues: list[dict]) -> str:
    """Create a human-readable summary."""
    if not issues:
        return "✅ No issues found — codebase looks clean!"

    errors = sum(1 for i in issues if i.get("severity") == "error")
    warnings = sum(1 for i in issues if i.get("severity") == "warning")
    todos = sum(1 for i in issues if i.get("severity") == "todo")
    style = sum(1 for i in issues if i.get("severity") == "style")

    parts = []
    if errors:
        parts.append(f"🔴 {errors} errors")
    if warnings:
        parts.append(f"🟡 {warnings} warnings")
    if todos:
        parts.append(f"📝 {todos} TODOs")
    if style:
        parts.append(f"💅 {style} style issues")

    return " | ".join(parts)


def _run_tests(package: str = "all") -> dict:
    """Run test suite for specified package(s)."""
    results = {}
    packages = ["brain", "gateway", "web"] if package == "all" else [package]

    for pkg in packages:
        pkg_path = os.path.join(PROJECT_ROOT, "packages", pkg)

        if not os.path.isdir(pkg_path):
            results[pkg] = {"status": "skipped", "reason": "Package not found"}
            continue

        if pkg == "brain":
            # Python: AST parse all files
            r = _run_cmd(
                ["python3", "-c",
                 "import ast, pathlib; "
                 "[ast.parse(f.read_text()) for f in pathlib.Path('.').rglob('*.py')]; "
                 "print('OK')"],
                cwd=pkg_path,
                timeout=20,
            )
            results[pkg] = {
                "status": "pass" if r["success"] else "fail",
                "output": r["stdout"][:500],
                "error": r["stderr"][:500] if not r["success"] else None,
            }

        elif pkg in ("gateway", "web"):
            # TypeScript: tsc --noEmit
            r = _run_cmd(["npx", "tsc", "--noEmit"], cwd=pkg_path, timeout=30)
            results[pkg] = {
                "status": "pass" if r["success"] else "fail",
                "output": r["stdout"][:500] if r["stdout"] else "Clean",
                "error": r["stderr"][:500] if not r["success"] else None,
            }

    all_pass = all(r.get("status") == "pass" for r in results.values()
                   if r.get("status") != "skipped")

    return {
        "all_pass": all_pass,
        "results": results,
        "summary": "✅ All tests pass" if all_pass else "❌ Some tests failed",
    }


# Last report storage (in-memory, persists across calls within a session)
_last_report: Optional[ImprovementReport] = None


def _get_last_report() -> dict:
    """Return the last improvement cycle report."""
    if _last_report:
        return _last_report.to_dict()
    return {"status": "no_data", "message": "No self-improvement cycles have run yet."}
