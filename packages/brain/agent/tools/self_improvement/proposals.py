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
from .ast_analyzer import _deep_scan

async def _telegram_digest(package: str = "all") -> dict:
    """
    Scan codebase and send a compact Telegram digest of findings.
    Groups by severity and shows top issues.
    """
    results = await asyncio.to_thread(_deep_scan, package)
    all_issues = results.get("issues", [])

    if not all_issues:
        msg = "✅ **Kestrel Code Health**: No issues found. Codebase is clean!"
        _send_summary_to_telegram(msg)
        return {"message": "No issues found. Telegram notified.", "sent": True}

    # Group by severity
    by_severity: dict[str, list] = {}
    for issue in all_issues:
        sev = issue.get("severity", "info")
        by_severity.setdefault(sev, []).append(issue)

    # Build digest
    severity_icons = {
        "critical": "🚨", "high": "🔴", "medium": "🟡",
        "low": "🔵", "info": "ℹ️"
    }

    lines = [f"📊 **Kestrel Code Health Report**\n"]
    lines.append(f"Total: {len(all_issues)} issues across {results.get('packages_scanned', 0)} packages\n")

    for sev in ("critical", "high", "medium", "low", "info"):
        issues = by_severity.get(sev, [])
        if issues:
            icon = severity_icons.get(sev, "•")
            lines.append(f"{icon} **{sev.upper()}**: {len(issues)}")

    # Top 5 most important issues
    top = sorted(all_issues, key=lambda x: {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}.get(x.get("severity", "info"), 5))[:5]
    if top:
        lines.append("\n**Top Issues:**")
        for i, issue in enumerate(top, 1):
            lines.append(f"{i}. [{issue.get('severity', '?').upper()}] {issue.get('description', '?')[:70]}")
            lines.append(f"   📁 `{issue.get('file', '?')}`")

    msg = "\n".join(lines)
    _send_summary_to_telegram(msg)

    return {
        "message": "Telegram digest sent.",
        "sent": True,
        "total_issues": len(all_issues),
        "by_severity": {k: len(v) for k, v in by_severity.items()},
    }

def _run_tests(package: str = "all") -> dict:
    """Run test suites across packages."""
    results = {}

    packages_to_test = PACKAGES.keys() if package == "all" else [package]

    for pkg in packages_to_test:
        pkg_info = PACKAGES.get(pkg)
        if not pkg_info:
            continue

        pkg_path = os.path.join(PROJECT_ROOT, pkg_info["path"])
        if not os.path.exists(pkg_path):
            results[pkg] = {"status": "skip", "error": f"Path not found: {pkg_path}"}
            continue

        if pkg_info["lang"] == "python":
            # Python: ast.parse all files
            try:
                errors = []
                for root, dirs, files in os.walk(pkg_path):
                    dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git", "venv")]
                    for f in files:
                        if f.endswith(".py"):
                            fpath = os.path.join(root, f)
                            try:
                                ast.parse(open(fpath, "r").read(), filename=f)
                            except SyntaxError as e:
                                errors.append(f"{f}:{e.lineno}: {e.msg}")
                results[pkg] = {"status": "pass" if not errors else "fail", "errors": errors}
            except Exception as e:
                results[pkg] = {"status": "fail", "error": str(e)}
        else:
            # TypeScript: try tsc --noEmit via docker exec (gateway/web containers)
            container_name = f"littlebirdalt-{pkg}-1" if pkg == "gateway" else f"littlebirdalt-frontend-1"
            try:
                res = subprocess.run(
                    ["docker", "exec", container_name, "npx", "tsc", "--noEmit"],
                    capture_output=True, text=True, timeout=60,
                )
                results[pkg] = {
                    "status": "pass" if res.returncode == 0 else "fail",
                    "errors": res.stdout[:500] if res.returncode != 0 else None,
                }
            except subprocess.TimeoutExpired:
                results[pkg] = {"status": "timeout"}
            except FileNotFoundError:
                # Docker not available — just check files exist
                results[pkg] = {"status": "skip", "error": "Docker exec not available"}
            except Exception as e:
                results[pkg] = {"status": "fail", "error": str(e)}

    all_pass = all(r.get("status") in ("pass", "skip") for r in results.values())
    return {
        "all_pass": all_pass,
        "summary": "✅ All tests pass" if all_pass else "❌ Some tests failed",
        "results": results,
    }

async def _propose_improvements() -> dict:
    """Create proposals from scan results, enrich with LLM analysis, send to Telegram."""

    if not _last_scan_results or not _last_scan_results.get("issues"):
        return {"error": "No scan results. Run 'scan' first."}

    issues = _last_scan_results["issues"]

    # Only propose actionable issues (not info/low TODOs in node_modules)
    actionable = [
        i for i in issues
        if i.get("severity") in ("critical", "high", "medium")
        and "node_modules" not in i.get("file", "")
    ]

    # Step 1: LLM deep analysis on the top candidate files
    llm_proposals = await _llm_analyze(actionable[:10])

    # Combine static + LLM proposals (LLM proposals go first — they're smarter)
    all_proposals = llm_proposals + actionable

    if not all_proposals:
        _send_summary_to_telegram(
            "🤖 <b>Kestrel Self-Improvement</b>\n\n"
            "✅ No actionable improvements found. Codebase is clean!\n\n"
            f"📊 Scanned {_last_scan_results.get('total_issues', 0)} items total."
        )
        return {"message": "No actionable improvements. Summary sent to Telegram.", "count": 0}

    # Load existing proposals and add new ones
    pending = _load_proposals()

    # Send top proposals to Telegram
    proposals_sent = 0
    for issue in all_proposals[:5]:  # Cap at 5 proposals per cycle
        proposal_id = str(uuid.uuid4())
        proposal = {
            "id": proposal_id,
            "created_at": time.time(),
            **issue,
        }
        pending[proposal_id] = proposal

        result = _send_proposal_to_telegram(proposal)
        if result.get("ok"):
            proposals_sent += 1
        else:
            logger.error(f"Failed to send proposal {proposal_id}: {result}")

    # Persist proposals to disk
    _save_proposals(pending)

    # Send summary
    llm_label = f" (🧠 {len(llm_proposals)} AI-analyzed)" if llm_proposals else ""
    _send_summary_to_telegram(
        f"🤖 <b>Kestrel Self-Improvement Scan Complete</b>\n\n"
        f"📊 {_last_scan_results.get('total_issues', 0)} issues found\n"
        f"📨 {proposals_sent} proposals sent for approval{llm_label}\n\n"
        f"Reply with ✅ to approve or ❌ to deny each proposal."
    )

    return {
        "proposals_sent": proposals_sent,
        "llm_proposals": len(llm_proposals),
        "total_actionable": len(actionable),
        "message": f"Sent {proposals_sent} proposals to Telegram ({len(llm_proposals)} AI-enhanced)",
    }

async def _llm_analyze(candidate_issues: list[dict]) -> list[dict]:
    """
    Send the codebase to the user's preferred cloud LLM for deep analysis.
    
    Two-phase approach:
    1. Build a full project tree + file summaries so the LLM sees the architecture
    2. Send the most important source files for deep code review
    
    The LLM reviews the full picture and provides intelligent suggestions for:
    - Logic bugs and edge cases
    - Architecture improvements  
    - Performance optimizations
    - Security vulnerabilities
    - Better error handling
    - Refactoring opportunities
    """

    # Determine the user's preferred provider
    provider_name = os.getenv("DEFAULT_LLM_PROVIDER", "google")
    if provider_name == "local":
        provider_name = "google"  # Fall back to cloud for analysis

    try:
        from providers.cloud import CloudProvider
        provider = CloudProvider(provider_name)
        if not provider.is_ready():
            logger.warning(f"CloudProvider '{provider_name}' not ready (no API key?). Skipping LLM analysis.")
            return []
    except Exception as e:
        logger.error(f"Failed to initialize CloudProvider: {e}")
        return []

    # ── Phase 1: Build codebase overview (cached for 5 min to skip redundant walk) ──
    global _codebase_overview_cache
    now = time.time()
    if _codebase_overview_cache and (now - _codebase_overview_cache.get("built_at", 0)) < _CODEBASE_OVERVIEW_TTL:
        codebase_tree = _codebase_overview_cache["tree"]
        key_exports = _codebase_overview_cache["exports"]
        logger.debug("LLM analysis: using cached codebase overview")
    else:
        tree_lines = []
        file_summaries = []

        for pkg_name, pkg_info in PACKAGES.items():
            pkg_path = os.path.join(PROJECT_ROOT, pkg_info["path"])
            if not os.path.exists(pkg_path):
                continue

            tree_lines.append(f"\n📦 {pkg_name} ({pkg_info['lang']})")
            ext = pkg_info["ext"]

            for root, dirs, files in os.walk(pkg_path):
                dirs[:] = [d for d in dirs if d not in (
                    "node_modules", "__pycache__", ".git", "dist", "build", ".next",
                    "venv", ".venv", "coverage", "test", "tests"
                )]

                depth = root.replace(pkg_path, "").count(os.sep)
                indent = "  " * (depth + 1)
                rel_dir = os.path.relpath(root, os.path.join(PROJECT_ROOT, pkg_info["path"]))
                if rel_dir != ".":
                    tree_lines.append(f"{indent}📁 {rel_dir}/")

                for fname in sorted(files):
                    if not (fname.endswith(ext) or fname.endswith(".ts") or fname.endswith(".tsx")):
                        continue
                    filepath = os.path.join(root, fname)
                    try:
                        content = open(filepath, "r", errors="ignore").read()
                        line_count = content.count("\n") + 1
                        tree_lines.append(f"{indent}  {fname} ({line_count} lines)")

                        # Extract key exports/classes/functions for summary
                        if pkg_info["lang"] == "python" and fname.endswith(".py"):
                            try:
                                tree = ast.parse(content, filename=fname)
                                names = []
                                for node in ast.iter_child_nodes(tree):
                                    if isinstance(node, ast.ClassDef):
                                        names.append(f"class {node.name}")
                                    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                        names.append(f"def {node.name}")
                                if names:
                                    rel = os.path.relpath(filepath, PROJECT_ROOT)
                                    file_summaries.append(f"  {rel}: {', '.join(names[:8])}")
                            except SyntaxError:
                                pass
                        elif fname.endswith((".ts", ".tsx")):
                            exports = re.findall(
                                r'export\s+(?:default\s+)?(?:function|class|const|interface|type)\s+(\w+)',
                                content,
                            )
                            if exports:
                                rel = os.path.relpath(filepath, PROJECT_ROOT)
                                file_summaries.append(f"  {rel}: {', '.join(exports[:8])}")
                    except Exception:
                        continue

        codebase_tree = "\n".join(tree_lines)
        key_exports = "\n".join(file_summaries[:40])
        _codebase_overview_cache = {"tree": codebase_tree, "exports": key_exports, "built_at": now}

    # ── Phase 2: Collect source files for deep review ─────────────────
    files_to_analyze: dict[str, str] = {}
    
    # Include flagged files from static analysis
    for issue in (candidate_issues or []):
        filepath = os.path.join(PROJECT_ROOT, issue.get("file", ""))
        if filepath in files_to_analyze or not os.path.exists(filepath):
            continue
        try:
            content = open(filepath, "r", errors="ignore").read()
            lines = content.split("\n")
            if len(lines) > 300:
                content = "\n".join(lines[:300]) + f"\n\n... ({len(lines)} total lines, truncated)"
            files_to_analyze[issue.get("file", "")] = content
        except Exception:
            continue
        if len(files_to_analyze) >= 3:
            break

    # Also include key architectural files even if no static issues
    key_files = [
        "packages/brain/server.py",
        "packages/brain/agent/loop.py",
        "packages/brain/agent/coordinator.py",
        "packages/gateway/src/server.ts",
        "packages/gateway/src/routes/features.ts",
    ]
    for kf in key_files:
        if len(files_to_analyze) >= 5:
            break
        if kf in files_to_analyze:
            continue
        filepath = os.path.join(PROJECT_ROOT, kf)
        if os.path.exists(filepath):
            try:
                content = open(filepath, "r", errors="ignore").read()
                lines = content.split("\n")
                if len(lines) > 200:
                    content = "\n".join(lines[:200]) + f"\n\n... ({len(lines)} total lines, truncated)"
                files_to_analyze[kf] = content
            except Exception:
                continue

    files_section = "\n\n".join(
        f"### {fname}\n```\n{content}\n```"
        for fname, content in files_to_analyze.items()
    )

    issues_section = ""
    if candidate_issues:
        issues_section = "## Static Analysis Already Found\n" + "\n".join(
            f"- [{i.get('severity')}] {i.get('file')}:{i.get('line', '?')} — {i.get('description', '')}"
            for i in candidate_issues[:10]
        )

    # ── Phase 3: LLM prompt ──────────────────────────────────────────
    prompt = f"""You are Kestrel's self-improvement engine. You are analyzing the FULL Kestrel AI platform codebase to find the most impactful improvements.

## Codebase Structure
{codebase_tree}

## Key Exports / Definitions
{key_exports}

{issues_section}

## Source Files (for deep review)
{files_section}

## Your Task
Think DEEPLY and SYSTEM-WIDE. Consider:
1. **Logic bugs** — edge cases, race conditions, off-by-one errors, null safety
2. **Architecture** — coupling issues, missing abstractions, circular dependencies, design patterns that would improve the system
3. **Performance** — unnecessary allocations, N+1 patterns, blocking calls in async code, missing caches
4. **Security** — injection risks, auth bypasses, data leaks, missing input validation
5. **Error handling** — unhandled failures, silent swallows, missing retries, error messages that leak internals
6. **Code quality** — dead code paths, unclear naming, missing types, code that should be shared between packages
7. **Missing features** — gaps in the system, obvious improvements, integration opportunities

Be specific and actionable. Don't flag trivial style issues.

Respond with a JSON array of improvement proposals. Each proposal should have:
- "type": one of "bug", "architecture", "performance", "security", "error_handling", "quality", "feature"
- "severity": one of "critical", "high", "medium"
- "file": relative file path
- "line": approximate line number (0 if system-wide)
- "description": clear description of the issue (2-3 sentences)
- "suggestion": specific fix suggestion (2-4 sentences, be concrete)

Return ONLY the JSON array, no markdown code fences. Limit to 5 most impactful proposals."""

    try:
        logger.info(f"LLM analysis: sending {len(files_to_analyze)} files + codebase tree to {provider_name}...")
        response = await provider.generate(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=8192,
        )

        # Parse the JSON response — robust extraction
        response = response.strip()
        proposals = _extract_json_array(response)

        if proposals is None:
            logger.error(f"Could not extract JSON from LLM response ({len(response)} chars)")
            logger.debug(f"Raw response preview: {response[:300]}")
            return []

        if not isinstance(proposals, list):
            proposals = [proposals]

        # Tag each as LLM-generated and add package info
        for p in proposals:
            p["source"] = "llm"
            p["llm_provider"] = provider_name
            fpath = p.get("file", "")
            if "brain" in fpath:
                p["package"] = "brain"
            elif "gateway" in fpath:
                p["package"] = "gateway"
            elif "web" in fpath:
                p["package"] = "web"
            else:
                p["package"] = "unknown"

        logger.info(f"LLM analysis: got {len(proposals)} proposals from {provider_name}")
        return proposals

    except json.JSONDecodeError as e:
        logger.error(f"LLM response wasn't valid JSON: {e}")
        return []
    except Exception as e:
        logger.error(f"LLM analysis failed: {e}")
        return []

async def _handle_approval(proposal_id: str, approved: bool) -> dict:
    """Handle approval or denial of a proposal — now actually applies the change."""
    if not proposal_id:
        return {"error": "proposal_id is required"}

    pending = _load_proposals()

    # Check full ID or prefix match
    matching = None
    for pid, proposal in pending.items():
        if pid == proposal_id or pid.startswith(proposal_id):
            matching = proposal
            proposal_id = pid
            break

    if not matching:
        return {"error": f"Proposal not found: {proposal_id}", "pending": list(pending.keys())}

    if approved:
        # Remove from pending
        del pending[proposal_id]
        _save_proposals(pending)

        # Actually apply the improvement
        from .patcher import apply_proposal
        result = await apply_proposal(matching)

        if result.get("success"):
            _send_summary_to_telegram(
                f"✅ <b>Applied:</b> {matching.get('description', '')[:200]}\n\n"
                f"File: <code>{result.get('file', '?')}</code>\n"
                f"Hot-reloaded ✅ | Watchdog active (5min)"
            )
        else:
            _send_summary_to_telegram(
                f"⚠️ <b>Approved but failed to apply:</b>\n"
                f"{matching.get('description', '')[:200]}\n\n"
                f"Error: {result.get('error', 'unknown')}"
            )

        return {
            "status": "approved",
            "applied": result.get("success", False),
            "message": result.get("message", result.get("error", "")),
            "stages": result.get("stages", {}),
            "proposal": matching,
        }
    else:
        # Remove from pending
        del pending[proposal_id]
        _save_proposals(pending)

        _send_summary_to_telegram(
            f"❌ <b>Denied:</b> {matching.get('description', '')[:200]}\n\n"
            f"Proposal discarded."
        )

        return {
            "status": "denied",
            "message": f"Proposal {proposal_id[:8]} denied and discarded.",
        }