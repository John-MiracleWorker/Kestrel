import asyncio
import ast
import json
import hashlib
import logging
import os
import re
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError
from agent.types import RiskLevel, ToolDefinition
from .utils import *

_SECRET_RE = re.compile(
    r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]{8,}['\"]",
    re.IGNORECASE,
)

def _analyze_file_content(content: str, fname: str, rel_path: str, pkg_name: str, lang: str) -> list[dict]:
    """Analyze a single file's content for issues. Pure function, safe to run in threads."""
    file_issues: list[dict] = []
    lines = content.split("\n")

    # Common text checks (applies to all languages)
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        for marker in ("TODO", "FIXME", "HACK", "XXX"):
            if marker in stripped and (stripped.startswith("//") or stripped.startswith("#")):
                file_issues.append({
                    "type": "todo",
                    "severity": "low",
                    "package": pkg_name,
                    "file": rel_path,
                    "line": i,
                    "description": stripped.lstrip("/#/ ").strip(),
                    "suggestion": f"Implement or remove: {stripped.lstrip('/#/ ').strip()[:100]}",
                })
        if _SECRET_RE.search(stripped):
            if "env" not in stripped.lower() and "example" not in stripped.lower():
                file_issues.append({
                    "type": "security",
                    "severity": "high",
                    "package": pkg_name,
                    "file": rel_path,
                    "line": i,
                    "description": "Potential hardcoded secret detected",
                    "suggestion": "Move secrets to environment variables and rotate exposed credentials.",
                })
        if "eval(" in stripped and not stripped.startswith("#") and not stripped.startswith("//"):
            file_issues.append({
                "type": "security",
                "severity": "medium",
                "package": pkg_name,
                "file": rel_path,
                "line": i,
                "description": "Use of eval() detected",
                "suggestion": "Avoid eval(); use safe parsing or explicit dispatch.",
            })

    # Python AST single-pass checks
    if lang == "python" and fname.endswith(".py"):
        try:
            tree = ast.parse(content, filename=fname)
        except SyntaxError as e:
            file_issues.append({
                "type": "syntax_error",
                "severity": "critical",
                "package": pkg_name,
                "file": rel_path,
                "line": e.lineno,
                "description": f"Python syntax error: {e.msg}",
                "suggestion": f"Fix syntax error at line {e.lineno}: {e.text.strip() if e.text else ''}",
            })
            tree = None

        if tree is not None:
            imported_names = []
            used_names: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    func_lines = (node.end_lineno - node.lineno + 1) if getattr(node, "end_lineno", None) else 0
                    if func_lines > 80:
                        file_issues.append({
                            "type": "complexity",
                            "severity": "medium",
                            "package": pkg_name,
                            "file": rel_path,
                            "line": node.lineno,
                            "description": f"Function `{node.name}` is {func_lines} lines long — consider refactoring",
                            "suggestion": f"Break `{node.name}` into smaller helper functions",
                        })
                elif isinstance(node, ast.ExceptHandler) and node.type is None:
                    file_issues.append({
                        "type": "error_handling",
                        "severity": "medium",
                        "package": pkg_name,
                        "file": rel_path,
                        "line": node.lineno,
                        "description": "Bare `except:` clause — catches SystemExit, KeyboardInterrupt etc.",
                        "suggestion": "Use `except Exception:` instead of bare `except:`",
                    })
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.asname or alias.name.split(".")[-1]
                        imported_names.append((name, node.lineno))
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        name = alias.asname or alias.name
                        imported_names.append((name, node.lineno))
                elif isinstance(node, ast.Name):
                    used_names.add(node.id)

            for name, lineno in imported_names:
                if name != "*" and not name.startswith("_") and name not in used_names:
                    file_issues.append({
                        "type": "dead_import",
                        "severity": "low",
                        "package": pkg_name,
                        "file": rel_path,
                        "line": lineno,
                        "description": f"Potentially unused import: `{name}`",
                        "suggestion": f"Remove unused import `{name}` at line {lineno}",
                    })

    return file_issues

def _process_file(args: tuple) -> tuple[str, str, list[dict]]:
    """Read and analyze one file. Returns (rel_path, signature, issues). Safe for threads."""
    filepath, fname, rel_path, pkg_name, lang, cached_files, mode = args
    try:
        signature = _file_signature(filepath)
    except OSError:
        return rel_path, "", []

    cached = cached_files.get(rel_path)
    if mode != "deep" and cached and cached.get("sig") == signature:
        return rel_path, signature, cached.get("issues", [])

    try:
        with open(filepath, "r", errors="ignore") as f:
            content = f.read()
    except Exception:
        return rel_path, signature, []

    return rel_path, signature, _analyze_file_content(content, fname, rel_path, pkg_name, lang)

_CODEBASE_OVERVIEW_TTL = 300

def _deep_scan(package: str = "all", mode: str = "standard") -> dict:
    """
    Deep codebase analysis with incremental caching and single-pass AST checks.

    Modes:
      - quick: scan only files touched in git diff when available
      - standard: incremental scan using file signature cache
      - deep: force full scan of all candidate files

    Files are processed in parallel with a ThreadPoolExecutor for I/O speed.
    """
    mode = (mode or "standard").lower()
    if mode not in {"quick", "standard", "deep"}:
        mode = "standard"

    scan_cache = _load_scan_cache()
    cached_files = scan_cache.get("files", {})
    next_cache: dict = {"files": {}}

    quick_targets: set[str] = set()
    if mode == "quick":
        try:
            diff = subprocess.run(
                ["git", "-C", PROJECT_ROOT, "diff", "--name-only", "HEAD"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if diff.returncode == 0:
                quick_targets = {line.strip() for line in diff.stdout.splitlines() if line.strip()}
        except Exception:
            quick_targets = set()

    packages_to_scan = PACKAGES if package == "all" else {package: PACKAGES.get(package, {})}

    # ── Pass 1: collect candidate file tuples (fast directory walk) ───
    candidates: list[tuple] = []
    for pkg_name, pkg_info in packages_to_scan.items():
        if not pkg_info:
            continue
        pkg_path = os.path.join(PROJECT_ROOT, pkg_info["path"])
        if not os.path.exists(pkg_path):
            continue

        ext = pkg_info["ext"]
        lang = pkg_info["lang"]

        for root, dirs, files in os.walk(pkg_path):
            dirs[:] = [d for d in dirs if d not in ("node_modules", "__pycache__", ".git", "dist", "build", ".next")]
            for fname in files:
                if not fname.endswith(ext) and not fname.endswith(".ts"):
                    continue
                filepath = os.path.join(root, fname)
                rel_path = os.path.relpath(filepath, PROJECT_ROOT)
                if mode == "quick" and quick_targets and rel_path not in quick_targets:
                    continue
                candidates.append((filepath, fname, rel_path, pkg_name, lang, cached_files, mode))

    files_considered = len(candidates)

    # ── Pass 2: process files in parallel ────────────────────────────
    issues: list[dict] = []
    max_workers = min(8, files_considered) if files_considered else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for rel_path, signature, file_issues in executor.map(_process_file, candidates):
            issues.extend(file_issues)
            if signature:
                next_cache["files"][rel_path] = {"sig": signature, "issues": file_issues}

    _save_scan_cache(next_cache)

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    issues.sort(key=lambda x: severity_order.get(x.get("severity", "info"), 4))

    by_severity = {}
    for issue in issues:
        s = issue.get("severity", "info")
        by_severity[s] = by_severity.get(s, 0) + 1

    by_type = {}
    for issue in issues:
        t = issue.get("type", "other")
        by_type[t] = by_type.get(t, 0) + 1

    summary_parts = []
    if by_severity.get("critical"): summary_parts.append(f"🔴 {by_severity['critical']} critical")
    if by_severity.get("high"): summary_parts.append(f"🟠 {by_severity['high']} high")
    if by_severity.get("medium"): summary_parts.append(f"🟡 {by_severity['medium']} medium")
    if by_severity.get("low"): summary_parts.append(f"🔵 {by_severity['low']} low")

    return {
        "mode": mode,
        "files_considered": files_considered,
        "cache_entries": len(next_cache.get("files", {})),
        "total_issues": len(issues),
        "summary": " | ".join(summary_parts) if summary_parts else "✅ Clean — no issues found",
        "by_type": by_type,
        "by_severity": by_severity,
        "issues": issues[:30],
    }