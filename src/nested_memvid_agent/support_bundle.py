from __future__ import annotations

import json
import os
import platform
import sqlite3
import subprocess
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .event_log import redact_secrets
from .product_readiness import build_product_readiness_report
from .setup_readiness import build_setup_readiness_report

_STATE_TABLES = (
    "runs",
    "run_steps",
    "approval_requests",
    "capability_overrides",
    "capability_change_log",
    "mcp_servers",
    "skill_registry",
    "plugin_registry",
    "task_nodes",
    "subagent_runs",
    "trace_spans",
    "promotion_ledger",
    "promotion_outcomes",
    "behavior_delta_ledger",
    "behavior_delta_activations",
    "behavior_delta_outcomes",
)


@dataclass(frozen=True)
class SupportBundleResult:
    bundle_path: Path
    manifest: dict[str, Any]
    entries: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "kestrel.support_bundle.v1",
            "bundle_path": str(self.bundle_path),
            "entries": list(self.entries),
            "manifest": self.manifest,
        }


def export_support_bundle(
    config: AgentConfig,
    *,
    output_path: Path | None = None,
    log_tail: int = 100,
) -> SupportBundleResult:
    """Write a redacted diagnostic bundle for local support/debugging."""
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    bundle_path = output_path or _default_bundle_path(config, generated_at)
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    sections: dict[str, Any] = {
        "product_readiness.json": build_product_readiness_report().to_dict(),
        "setup_readiness.json": build_setup_readiness_report(config).to_dict(),
        "runtime.json": _runtime_payload(config),
        "git.json": _git_payload(config.workspace),
        "state_summary.json": _state_summary(config.state_path),
        "logs/events_tail.json": _event_tail(config.log_dir / "events.jsonl", limit=log_tail),
        "logs/files.json": _log_files(config.log_dir),
    }
    entries = ("manifest.json", *tuple(sections))
    manifest = {
        "schema": "kestrel.support_bundle.v1",
        "generated_at": generated_at,
        "redaction": {
            "raw_secret_values": "excluded",
            "environment_variables": "presence_only",
            "logs": "pattern_redacted_tail_only",
        },
        "limits": {"log_tail": _bounded_log_tail(log_tail)},
        "entries": list(entries),
    }

    with zipfile.ZipFile(bundle_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_json(archive, "manifest.json", manifest)
        for name, payload in sections.items():
            _write_json(archive, name, payload)

    return SupportBundleResult(bundle_path=bundle_path, manifest=manifest, entries=entries)


def _default_bundle_path(config: AgentConfig, generated_at: str) -> Path:
    timestamp = generated_at.replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    return config.log_dir.parent / "support-bundles" / f"kestrel-support-{timestamp}.zip"


def _runtime_payload(config: AgentConfig) -> dict[str, Any]:
    return {
        "schema": "kestrel.runtime_support.v1",
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
        },
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "provider": config.provider,
        "model": config.model,
        "backend": config.backend,
        "base_url": config.base_url,
        "api_key_env": _env_presence(config.api_key_env),
        "fallback_provider": config.fallback_provider,
        "fallback_model": config.fallback_model,
        "fallback_base_url": config.fallback_base_url,
        "fallback_api_key_env": _env_presence(config.fallback_api_key_env),
        "api_auth_token_env": _env_presence(config.api_auth_token_env),
        "secret_backend": config.secret_backend,
        "paths": {
            "workspace": str(config.workspace),
            "memory_dir": str(config.memory_dir),
            "state_path": str(config.state_path),
            "log_dir": str(config.log_dir),
            "secret_store_path": str(config.secret_store_path),
            "skills_dir": str(config.skills_dir),
            "plugins_dir": str(config.plugins_dir),
            "mcp_config_path": str(config.mcp_config_path),
            "channel_config_path": str(config.channel_config_path),
            "worker_worktree_dir": str(config.worker_worktree_dir),
        },
        "safety_flags": {
            "allow_shell": config.allow_shell,
            "allow_file_write": config.allow_file_write,
            "allow_policy_writes": config.allow_policy_writes,
            "allow_codex_cli": config.allow_codex_cli,
            "allow_plugin_install": config.allow_plugin_install,
            "allow_git_commit": config.allow_git_commit,
            "allow_git_push": config.allow_git_push,
            "allow_remote_mutation": config.allow_remote_mutation,
            "allow_memory_import": config.allow_memory_import,
            "allow_executable_skills": config.allow_executable_skills,
            "allow_mcp_network_endpoints": config.allow_mcp_network_endpoints,
            "allow_web": config.allow_web,
            "allow_self_modification": config.allow_self_modification,
            "require_approval_for_high_risk_tools": config.require_approval_for_high_risk_tools,
            "approval_ttl_seconds": config.approval_ttl_seconds,
            "require_api_auth": config.require_api_auth,
        },
        "learning_flags": {
            "enable_agentic_cycle": config.enable_agentic_cycle,
            "enable_autonomous_scheduler": config.enable_autonomous_scheduler,
            "enable_worker_isolation": config.enable_worker_isolation,
            "enable_task_capsules": config.enable_task_capsules,
            "enable_auto_consolidation": config.enable_auto_consolidation,
            "enable_auto_compact": config.enable_auto_compact,
            "enable_behavior_deltas": config.enable_behavior_deltas,
            "enable_auto_activate_low_risk_deltas": config.enable_auto_activate_low_risk_deltas,
            "enable_auto_skill_materialization": config.enable_auto_skill_materialization,
            "enable_auto_consolidation_shadow": config.enable_auto_consolidation_shadow,
            "enable_auto_consolidation_apply": config.enable_auto_consolidation_apply,
            "enable_diagnosis_to_patch": config.enable_diagnosis_to_patch,
        },
    }


def _env_presence(name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    return {"name": name, "present": bool(os.getenv(name))}


def _git_payload(workspace: Path) -> dict[str, Any]:
    resolved = workspace.expanduser()
    if not resolved.exists() or not resolved.is_dir():
        return {"workspace": str(resolved), "is_git_repo": False, "error": "workspace_not_found"}
    root = _run_git(resolved, "rev-parse", "--show-toplevel")
    if root["returncode"] != 0:
        return {
            "workspace": str(resolved),
            "is_git_repo": False,
            "error": root.get("stderr") or "not_a_git_repository",
        }
    return {
        "workspace": str(resolved),
        "is_git_repo": True,
        "root": root["stdout"],
        "branch": _run_git(resolved, "branch", "--show-current")["stdout"],
        "head": _run_git(resolved, "rev-parse", "--short", "HEAD")["stdout"],
        "status_short": _run_git(resolved, "status", "--short")["stdout"].splitlines()[:200],
    }


def _run_git(workspace: Path, *args: str) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=workspace,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"returncode": 1, "stdout": "", "stderr": str(exc)}
    return {
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
    }


def _state_summary(path: Path) -> dict[str, Any]:
    resolved = path.expanduser()
    if not resolved.exists():
        return {
            "path": str(resolved),
            "exists": False,
            "schema_version": 0,
            "tables": {name: 0 for name in _STATE_TABLES},
        }
    try:
        with closing(sqlite3.connect(resolved.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            schema_version = _schema_version(conn)
            tables = {name: _table_count(conn, name) for name in _STATE_TABLES}
    except sqlite3.Error as exc:
        return {
            "path": str(resolved),
            "exists": True,
            "schema_version": 0,
            "tables": {name: 0 for name in _STATE_TABLES},
            "error": str(exc),
        }
    return {
        "path": str(resolved),
        "exists": True,
        "schema_version": schema_version,
        "tables": tables,
    }


def _schema_version(conn: sqlite3.Connection) -> int:
    if not _table_exists(conn, "schema_version"):
        return 0
    row = conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
    if row is None:
        return 0
    return int(row["version"])


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()  # nosec B608
    return int(row["count"]) if row is not None else 0


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _event_tail(path: Path, *, limit: int) -> list[dict[str, Any]]:
    bounded = _bounded_log_tail(limit)
    if bounded <= 0 or not _safe_log_file(path, path.parent):
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-bounded:]
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            events.append({"line": index, "error": f"invalid_json: {exc.msg}"})
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _log_files(log_dir: Path) -> list[dict[str, Any]]:
    resolved = log_dir.expanduser()
    if resolved.is_symlink() or not resolved.exists() or not resolved.is_dir():
        return []
    files: list[dict[str, Any]] = []
    for path in sorted(item for item in resolved.iterdir() if _safe_log_file(item, resolved)):
        stat = path.stat()
        files.append(
            {
                "name": path.name,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            }
        )
    return files


def _safe_log_file(path: Path, root: Path) -> bool:
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        return False
    try:
        resolved_root = root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError):
        return False
    return resolved_root in resolved_path.parents


def _bounded_log_tail(limit: int) -> int:
    return max(0, min(int(limit), 500))


def _write_json(archive: zipfile.ZipFile, name: str, payload: Any) -> None:
    safe_payload = redact_secrets(payload)
    archive.writestr(
        name,
        json.dumps(safe_payload, indent=2, sort_keys=True, default=_json_default) + "\n",
    )


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)
