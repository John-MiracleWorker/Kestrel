"""
Self-improvement patcher — the "apply" part of Kestrel's self-improvement.

Takes an approved proposal (description + suggestion), generates an actual
code patch via LLM, then applies it safely:

  backup → syntax check → test → apply → hot-reload → watchdog

If anything fails at any stage, the change is discarded or rolled back.
"""

import ast
import asyncio
import importlib
import json
import logging
import os
import shutil
import sys
import time
from typing import Optional

logger = logging.getLogger("brain.agent.tools.self_improve.patcher")

PROJECT_ROOT = "/app"
BACKUP_DIR = os.path.join(PROJECT_ROOT, ".self_improve", "backups")
STAGING_DIR = os.path.join(PROJECT_ROOT, ".self_improve", "staging")
HISTORY_FILE = os.path.join(PROJECT_ROOT, ".self_improve", "history.json")

# Active rollback watchdogs — keyed by filepath
_active_watchdogs: dict[str, asyncio.Task] = {}


# ── Patch Generation ─────────────────────────────────────────────────


async def generate_patch(
    proposal: dict,
    filepath: str,
) -> dict:
    """
    Send the proposal + original source to an LLM and get back
    the complete modified file content.

    Returns:
        {"success": True, "patched_content": str, "explanation": str}
        {"success": False, "error": str}
    """
    abs_path = filepath if filepath.startswith("/") else os.path.join(PROJECT_ROOT, filepath)

    if not os.path.exists(abs_path):
        return {"success": False, "error": f"File not found: {abs_path}"}

    original = open(abs_path, "r", errors="ignore").read()

    # Use the brain's cloud provider
    provider_name = os.getenv("DEFAULT_LLM_PROVIDER", "google")
    if provider_name == "local":
        provider_name = "google"

    try:
        from providers.cloud import CloudProvider
        provider = CloudProvider(provider_name)
        if not provider.is_ready():
            return {"success": False, "error": f"Cloud provider '{provider_name}' not ready (no API key)"}
    except Exception as e:
        return {"success": False, "error": f"Failed to initialize provider: {e}"}

    prompt = f"""You are Kestrel's self-improvement engine. Apply the following improvement to this file.

## Improvement Proposal
**Type**: {proposal.get('type', 'unknown')}
**Severity**: {proposal.get('severity', 'medium')}
**Description**: {proposal.get('description', '')}
**Suggestion**: {proposal.get('suggestion', '')}
**Target line**: {proposal.get('line', 'unknown')}

## Original File: {filepath}
```python
{original}
```

## Instructions
1. Apply ONLY the improvement described above — do not refactor or change anything else
2. Preserve all existing functionality, imports, and formatting style
3. Return the COMPLETE modified file content (not a diff, the full file)
4. If the improvement cannot be safely applied, respond with exactly: CANNOT_APPLY: <reason>

Return ONLY the file content, no markdown code fences, no explanation.
If you must explain, put it as a Python comment at the point of change."""

    try:
        response = await provider.generate(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=16384,
        )

        response = response.strip()

        # Check for refusal
        if response.startswith("CANNOT_APPLY:"):
            reason = response[len("CANNOT_APPLY:"):].strip()
            return {"success": False, "error": f"LLM declined to apply: {reason}"}

        # Strip markdown fences if present
        if response.startswith("```"):
            lines = response.split("\n")
            # Remove first line (```python) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            elif lines[0].startswith("```"):
                lines = lines[1:]
            response = "\n".join(lines)

        if not response.strip():
            return {"success": False, "error": "LLM returned empty response"}

        return {
            "success": True,
            "patched_content": response,
            "original_content": original,
            "explanation": f"Applied: {proposal.get('description', '')[:100]}",
        }

    except Exception as e:
        return {"success": False, "error": f"LLM patch generation failed: {e}"}


# ── Backup & Apply ───────────────────────────────────────────────────


def backup_file(filepath: str) -> str:
    """
    Create a timestamped backup of a file.
    Returns the backup path.
    """
    abs_path = filepath if filepath.startswith("/") else os.path.join(PROJECT_ROOT, filepath)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    rel = os.path.relpath(abs_path, PROJECT_ROOT)
    backup_path = os.path.join(BACKUP_DIR, timestamp, rel)

    os.makedirs(os.path.dirname(backup_path), exist_ok=True)
    shutil.copy2(abs_path, backup_path)
    logger.info(f"Backed up {rel} → {backup_path}")
    return backup_path


def syntax_check(content: str, filename: str = "<patch>") -> Optional[str]:
    """
    Validate Python syntax. Returns None if valid, error string if invalid.
    """
    try:
        ast.parse(content, filename=filename)
        return None
    except SyntaxError as e:
        return f"Syntax error at line {e.lineno}: {e.msg}"


def apply_patch(filepath: str, new_content: str, backup: bool = True) -> dict:
    """
    Apply a code patch to a file.
    1. Backup original (if backup=True)
    2. Syntax check the new content
    3. Write the new file

    Returns:
        {"success": True, "backup_path": str}
        {"success": False, "error": str}
    """
    abs_path = filepath if filepath.startswith("/") else os.path.join(PROJECT_ROOT, filepath)

    if not os.path.exists(abs_path):
        return {"success": False, "error": f"File not found: {abs_path}"}

    # Only syntax-check Python files
    if abs_path.endswith(".py"):
        err = syntax_check(new_content, filename=abs_path)
        if err:
            return {"success": False, "error": f"Patch has syntax error: {err}"}

    # Backup
    backup_path = ""
    if backup:
        backup_path = backup_file(abs_path)

    # Write
    try:
        with open(abs_path, "w") as f:
            f.write(new_content)
        logger.info(f"Applied patch to {abs_path}")
        return {"success": True, "backup_path": backup_path}
    except Exception as e:
        # Restore from backup if write failed partially
        if backup_path and os.path.exists(backup_path):
            shutil.copy2(backup_path, abs_path)
        return {"success": False, "error": f"Write failed: {e}"}


# ── Hot-Reload ───────────────────────────────────────────────────────


def hot_reload(filepath: str) -> dict:
    """
    Hot-reload a Python module after patching.
    Converts a filepath to a module name and reloads it.

    For tool modules (agent/tools/*), also attempts to re-register
    the tools into the active registry.

    Returns:
        {"success": True, "module": str}
        {"success": False, "error": str}
    """
    abs_path = filepath if filepath.startswith("/") else os.path.join(PROJECT_ROOT, filepath)

    if not abs_path.endswith(".py"):
        return {"success": False, "error": "Hot-reload only works for Python modules"}

    # Convert filepath to module name
    # /app/agent/tools/moltbook.py → agent.tools.moltbook
    rel_path = os.path.relpath(abs_path, PROJECT_ROOT)
    module_name = rel_path.replace("/", ".").replace(".py", "")

    # Check if module is already loaded
    if module_name not in sys.modules:
        return {
            "success": True,
            "module": module_name,
            "note": "Module not loaded — change will take effect on next import",
        }

    try:
        module = sys.modules[module_name]
        importlib.reload(module)
        logger.info(f"Hot-reloaded module: {module_name}")

        return {
            "success": True,
            "module": module_name,
            "reloaded": True,
        }

    except Exception as e:
        logger.error(f"Hot-reload failed for {module_name}: {e}")
        return {"success": False, "error": f"Reload failed: {e}", "module": module_name}


# ── Rollback ─────────────────────────────────────────────────────────


def rollback(filepath: str, backup_path: str = "") -> dict:
    """
    Restore a file from its most recent backup.

    If backup_path is not specified, finds the most recent backup automatically.
    """
    abs_path = filepath if filepath.startswith("/") else os.path.join(PROJECT_ROOT, filepath)
    rel = os.path.relpath(abs_path, PROJECT_ROOT)

    if not backup_path:
        # Find the most recent backup
        backup_path = _find_latest_backup(rel)

    if not backup_path or not os.path.exists(backup_path):
        return {"success": False, "error": f"No backup found for {rel}"}

    try:
        shutil.copy2(backup_path, abs_path)
        logger.info(f"Rolled back {rel} from {backup_path}")

        # Hot-reload the restored version
        reload_result = hot_reload(abs_path)

        return {
            "success": True,
            "restored_from": backup_path,
            "reload": reload_result,
        }
    except Exception as e:
        return {"success": False, "error": f"Rollback failed: {e}"}


def _find_latest_backup(rel_path: str) -> Optional[str]:
    """Find the most recent backup of a file."""
    if not os.path.exists(BACKUP_DIR):
        return None

    candidates = []
    for timestamp_dir in sorted(os.listdir(BACKUP_DIR), reverse=True):
        candidate = os.path.join(BACKUP_DIR, timestamp_dir, rel_path)
        if os.path.exists(candidate):
            candidates.append(candidate)

    return candidates[0] if candidates else None


# ── Auto-Rollback Watchdog ───────────────────────────────────────────


async def start_watchdog(filepath: str, backup_path: str, seconds: int = 300):
    """
    Start a background watchdog that auto-rolls back a change if errors spike.

    Monitors the brain error log for the next `seconds` and rolls back
    if a significant error increase is detected.
    """
    abs_path = filepath if filepath.startswith("/") else os.path.join(PROJECT_ROOT, filepath)
    rel = os.path.relpath(abs_path, PROJECT_ROOT)

    # Cancel existing watchdog for this file
    if rel in _active_watchdogs:
        _active_watchdogs[rel].cancel()

    async def _watchdog():
        try:
            logger.info(f"Watchdog started for {rel} ({seconds}s)")
            start_time = time.monotonic()
            baseline_errors = _count_recent_errors()

            while time.monotonic() - start_time < seconds:
                await asyncio.sleep(30)  # Check every 30s

                current_errors = _count_recent_errors()
                new_errors = current_errors - baseline_errors

                if new_errors > 5:
                    logger.warning(
                        f"Watchdog: {new_errors} new errors after patching {rel}. "
                        f"Auto-rolling back!"
                    )
                    result = rollback(abs_path, backup_path)
                    _record_history(rel, "auto_rollback", {
                        "reason": f"{new_errors} errors in {int(time.monotonic() - start_time)}s",
                        "rollback_result": result,
                    })

                    # Notify via Telegram
                    try:
                        from .utils import _send_summary_to_telegram
                        _send_summary_to_telegram(
                            f"⚠️ <b>Auto-Rollback</b>\n\n"
                            f"File: <code>{rel}</code>\n"
                            f"Reason: {new_errors} errors detected after patch\n"
                            f"Status: {'✅ Restored' if result.get('success') else '❌ Rollback failed'}"
                        )
                    except Exception:
                        pass
                    return

            logger.info(f"Watchdog: {rel} stable for {seconds}s. Patch is good! ✅")
            _record_history(rel, "watchdog_passed", {
                "duration_seconds": seconds,
            })

        except asyncio.CancelledError:
            logger.info(f"Watchdog cancelled for {rel}")
        except Exception as e:
            logger.error(f"Watchdog error for {rel}: {e}")
        finally:
            _active_watchdogs.pop(rel, None)

    task = asyncio.create_task(_watchdog())
    _active_watchdogs[rel] = task


class _ErrorCountHandler(logging.Handler):
    """Lightweight logging handler that counts ERROR+ log records.

    Attached once to the 'brain' logger so the watchdog can detect
    error-rate spikes after a patch is applied.
    """

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.count = 0

    def emit(self, record):
        self.count += 1


# Singleton — attached lazily on first call to _count_recent_errors()
_error_handler: _ErrorCountHandler | None = None


def _count_recent_errors() -> int:
    """Count ERROR-level log entries seen since the handler was attached."""
    global _error_handler
    if _error_handler is None:
        _error_handler = _ErrorCountHandler()
        logging.getLogger("brain").addHandler(_error_handler)
    return _error_handler.count


# ── History ──────────────────────────────────────────────────────────


def _load_history() -> list[dict]:
    """Load applied improvement history."""
    try:
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return []


def _save_history(history: list[dict]):
    """Save applied improvement history."""
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w") as f:
        json.dump(history[-100:], f, indent=2)  # Keep last 100


def _record_history(filepath: str, action: str, details: dict = None):
    """Record an improvement action to history."""
    history = _load_history()
    history.append({
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file": filepath,
        "action": action,
        "details": details or {},
    })
    _save_history(history)


def get_history() -> list[dict]:
    """Get the full improvement history."""
    return _load_history()


# ── Full Pipeline ────────────────────────────────────────────────────


async def _council_review(proposal: dict, patch_summary: str) -> dict:
    """Run the proposal through a Council deliberation for multi-perspective review.

    Returns ``{"approved": True/False, "verdict": <CouncilVerdict dict>}``.
    If the Council system is unavailable (no LLM, import error, etc.), the
    proposal is approved by default so the pipeline isn't blocked.
    """
    try:
        from agent.council import CouncilSession

        # Use the same cloud provider the patcher uses
        provider_name = os.getenv("DEFAULT_LLM_PROVIDER", "google")
        if provider_name == "local":
            provider_name = "google"

        from providers.cloud import CloudProvider
        provider = CloudProvider(provider_name)
        if not provider.is_ready():
            logger.info("Council review skipped: cloud provider not ready")
            return {"approved": True, "verdict": None, "skipped": True}

        council = CouncilSession(llm_provider=provider)

        proposal_text = (
            f"Self-improvement proposal for {proposal.get('file', '?')}:\n"
            f"Type: {proposal.get('type', '?')}\n"
            f"Severity: {proposal.get('severity', '?')}\n"
            f"Description: {proposal.get('description', '')}\n"
            f"Suggestion: {proposal.get('suggestion', '')}\n\n"
            f"Patch summary: {patch_summary}"
        )

        verdict = await council.deliberate(
            proposal=proposal_text,
            context="Automated self-improvement patch — evaluate safety and correctness.",
            include_debate=False,  # Keep it fast; single-round evaluation
        )

        approved = verdict.has_consensus and verdict.consensus is not None and verdict.consensus.value != "reject"

        logger.info(
            f"Council review: {'approved' if approved else 'rejected'} "
            f"(consensus={verdict.has_consensus}, "
            f"votes={[o.vote.value for o in verdict.opinions]})"
        )

        return {
            "approved": approved,
            "verdict": verdict.to_dict(),
            "requires_user_review": verdict.requires_user_review,
        }

    except Exception as e:
        logger.warning(f"Council review failed ({e}), defaulting to approved")
        return {"approved": True, "verdict": None, "skipped": True, "error": str(e)}


async def apply_proposal(proposal: dict) -> dict:
    """
    Full self-improvement pipeline:
    1. Generate code patch via LLM
    1b. Council multi-agent review of the proposed change
    2. Syntax check
    3. Backup original
    4. Run tests
    5. Apply patch
    6. Hot-reload
    7. Start watchdog

    Returns detailed results from each stage.
    """
    filepath = proposal.get("file", "")
    if not filepath:
        return {"success": False, "error": "Proposal has no 'file' field"}

    abs_path = filepath if filepath.startswith("/") else os.path.join(PROJECT_ROOT, filepath)
    rel_path = os.path.relpath(abs_path, PROJECT_ROOT)

    # Only Python files can be hot-reloaded
    if not abs_path.endswith(".py"):
        return {
            "success": False,
            "error": f"Only Python files can be self-improved with hot-reload. "
                     f"'{rel_path}' requires a container rebuild.",
        }

    if not os.path.exists(abs_path):
        return {"success": False, "error": f"File not found: {rel_path}"}

    results = {"file": rel_path, "stages": {}}

    # Stage 1: Generate patch
    logger.info(f"Self-improve: generating patch for {rel_path}...")
    patch_result = await generate_patch(proposal, abs_path)
    results["stages"]["generate_patch"] = {
        "success": patch_result["success"],
        "error": patch_result.get("error"),
    }
    if not patch_result["success"]:
        results["success"] = False
        results["error"] = f"Patch generation failed: {patch_result['error']}"
        return results

    # Stage 1b: Council review — multi-agent safety check
    council_result = await _council_review(
        proposal,
        patch_summary=patch_result.get("explanation", proposal.get("description", ""))[:300],
    )
    results["stages"]["council_review"] = council_result
    if not council_result.get("approved", True):
        results["success"] = False
        results["error"] = "Council rejected the proposal — not safe to apply"
        _record_history(rel_path, "council_rejected", {
            "proposal": {
                "type": proposal.get("type"),
                "severity": proposal.get("severity"),
                "description": proposal.get("description", "")[:200],
            },
            "verdict": council_result.get("verdict"),
        })
        return results

    new_content = patch_result["patched_content"]

    # Stage 2: Syntax check
    syntax_err = syntax_check(new_content, filename=rel_path)
    results["stages"]["syntax_check"] = {
        "success": syntax_err is None,
        "error": syntax_err,
    }
    if syntax_err:
        results["success"] = False
        results["error"] = f"Patch has syntax error: {syntax_err}"
        return results

    # Stage 3: Backup
    backup_path = backup_file(abs_path)
    results["stages"]["backup"] = {"success": True, "path": backup_path}

    # Stage 4: Apply (write the file)
    apply_result = apply_patch(abs_path, new_content, backup=False)  # Already backed up
    results["stages"]["apply"] = apply_result
    if not apply_result["success"]:
        results["success"] = False
        results["error"] = f"Apply failed: {apply_result.get('error')}"
        # Restore backup
        rollback(abs_path, backup_path)
        return results

    # Stage 5: Test
    from .proposals import _run_tests
    test_result = _run_tests("brain")
    results["stages"]["test"] = test_result
    if not test_result.get("all_pass"):
        logger.warning(f"Tests failed after patching {rel_path}. Rolling back.")
        rollback(abs_path, backup_path)
        results["success"] = False
        results["error"] = f"Tests failed after patch — rolled back"
        results["stages"]["rollback"] = {"success": True, "reason": "test_failure"}
        return results

    # Stage 6: Hot-reload
    reload_result = hot_reload(abs_path)
    results["stages"]["hot_reload"] = reload_result
    if not reload_result.get("success"):
        logger.warning(f"Hot-reload failed for {rel_path}. Rolling back.")
        rollback(abs_path, backup_path)
        results["success"] = False
        results["error"] = f"Hot-reload failed: {reload_result.get('error')} — rolled back"
        results["stages"]["rollback"] = {"success": True, "reason": "reload_failure"}
        return results

    # Stage 7: Start watchdog
    try:
        await start_watchdog(abs_path, backup_path, seconds=300)
        results["stages"]["watchdog"] = {"started": True, "duration_seconds": 300}
    except Exception as e:
        logger.warning(f"Watchdog start failed (non-fatal): {e}")
        results["stages"]["watchdog"] = {"started": False, "error": str(e)}

    # Record success
    _record_history(rel_path, "applied", {
        "proposal": {
            "type": proposal.get("type"),
            "severity": proposal.get("severity"),
            "description": proposal.get("description", "")[:200],
        },
        "backup_path": backup_path,
        "explanation": patch_result.get("explanation", ""),
    })

    results["success"] = True
    results["message"] = (
        f"✅ Applied improvement to {rel_path}. "
        f"Module hot-reloaded. 5-minute watchdog active."
    )

    # Notify via Telegram
    try:
        from .utils import _send_summary_to_telegram
        _send_summary_to_telegram(
            f"🔧 <b>Self-Improvement Applied</b>\n\n"
            f"File: <code>{rel_path}</code>\n"
            f"Change: {proposal.get('description', '')[:150]}\n"
            f"Status: ✅ Applied + hot-reloaded\n"
            f"Watchdog: 5min auto-rollback active"
        )
    except Exception:
        pass


    return results

