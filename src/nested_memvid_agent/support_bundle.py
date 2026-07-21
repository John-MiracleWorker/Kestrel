from __future__ import annotations

import errno
import json
import os
import platform
import secrets
import sqlite3
import stat
import subprocess  # nosec B404
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO

from .config import AgentConfig
from .event_log import read_bounded_jsonl_tail, redact_secrets
from .platform_primitives import chmod_descriptor
from .product_readiness import build_product_readiness_report
from .repair_integrity import (
    hardened_readonly_git_command,
    hardened_readonly_git_environment,
)
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
    "routines",
    "routine_occurrences",
)
_ROUTINE_OCCURRENCE_STATUSES = (
    "claimed",
    "running",
    "completed",
    "failed",
    "skipped",
)
_EVENT_SAFE_TEXT_FIELDS = frozenset(
    {
        "backend",
        "blocked_reason",
        "category",
        "channel",
        "classification",
        "code",
        "decision",
        "event_type",
        "finish_reason",
        "frame_type",
        "from",
        "id",
        "kind",
        "layer",
        "method",
        "model",
        "provider",
        "risk",
        "role",
        "schedule_kind",
        "source",
        "status",
        "stop_reason",
        "to",
        "tool",
        "tool_name",
        "transcript_scope",
        "turn_origin",
        "type",
        "validation_status",
    }
)
_EVENT_SAFE_TEXT_FIELD_SUFFIXES = (
    "_at",
    "_category",
    "_code",
    "_id",
    "_ids",
    "_kind",
    "_layer",
    "_origin",
    "_role",
    "_scope",
    "_status",
    "_type",
)
_EVENT_SAFE_TEXT_LIST_FIELDS = frozenset(
    {
        "active_behavior_deltas",
        "memory_writes",
        "similar_lessons",
        "similar_lessons_used",
    }
)
_PRIVATE_BUNDLE_MODE = 0o600
_SUPPORT_BUNDLE_TEMP_PREFIX = ".kestrel-support-"


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
    """Write a redacted diagnostic bundle for local support/debugging.

    Bundle destinations are create-once: an existing file, hard link, symbolic
    link, directory, or concurrent publication is refused rather than replaced.
    """
    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat()
    bundle_path = output_path or _default_bundle_path(config, generated_at)

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
            "logs": "free_form_text_redacted_metadata_allowlist_tail_only",
        },
        "limits": {"log_tail": _bounded_log_tail(log_tail)},
        "entries": list(entries),
    }

    _write_support_archive_exclusive(
        bundle_path,
        manifest=manifest,
        sections=sections,
        expected_entries=entries,
    )

    return SupportBundleResult(bundle_path=bundle_path, manifest=manifest, entries=entries)


def _default_bundle_path(config: AgentConfig, generated_at: str) -> Path:
    timestamp = generated_at.replace(":", "").replace("+0000", "Z").replace("+00:00", "Z")
    nonce = secrets.token_hex(4)
    return config.log_dir.parent / "support-bundles" / f"kestrel-support-{timestamp}-{nonce}.zip"


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
            "enable_semantic_orchestration": config.enable_semantic_orchestration,
            "enable_autonomous_scheduler": config.enable_autonomous_scheduler,
            "enable_proactive_routines": config.enable_proactive_routines,
            "routine_poll_interval_seconds": config.routine_poll_interval_seconds,
            "routine_claim_ttl_seconds": config.routine_claim_ttl_seconds,
            "max_routines_per_tick": config.max_routines_per_tick,
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
        completed = subprocess.run(  # nosec B603
            hardened_readonly_git_command(list(args), workspace=workspace),
            cwd=workspace,
            env=hardened_readonly_git_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
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
            "routine_summary": _empty_routine_summary(),
        }
    try:
        with closing(sqlite3.connect(resolved.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            schema_version = _schema_version(conn)
            tables = {name: _table_count(conn, name) for name in _STATE_TABLES}
            routine_summary = _routine_summary(conn)
    except sqlite3.Error as exc:
        return {
            "path": str(resolved),
            "exists": True,
            "schema_version": 0,
            "tables": {name: 0 for name in _STATE_TABLES},
            "routine_summary": _empty_routine_summary(),
            "error": str(exc),
        }
    return {
        "path": str(resolved),
        "exists": True,
        "schema_version": schema_version,
        "tables": tables,
        "routine_summary": routine_summary,
    }


def _empty_routine_summary() -> dict[str, Any]:
    return {
        "enabled_definitions": 0,
        "occurrences_by_status": {status: 0 for status in _ROUTINE_OCCURRENCE_STATUSES},
        "expired_claims": 0,
        "oldest_nonterminal": None,
    }


def _routine_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    summary = _empty_routine_summary()
    if _table_exists(conn, "routines"):
        enabled = conn.execute(
            "SELECT COUNT(*) AS count FROM routines WHERE enabled = 1 AND deleted_at IS NULL"
        ).fetchone()
        summary["enabled_definitions"] = int(enabled["count"]) if enabled is not None else 0
    if not _table_exists(conn, "routine_occurrences"):
        return summary

    status_counts = summary["occurrences_by_status"]
    rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM routine_occurrences GROUP BY status"
    ).fetchall()
    for row in rows:
        status = str(row["status"])
        if status in status_counts:
            status_counts[status] = int(row["count"])

    now = datetime.now(UTC).isoformat()
    expired = conn.execute(
        """
        SELECT COUNT(*) AS count FROM routine_occurrences
        WHERE status = 'claimed' AND claim_expires_at IS NOT NULL
          AND julianday(claim_expires_at) <= julianday(?)
        """,
        (now,),
    ).fetchone()
    summary["expired_claims"] = int(expired["count"]) if expired is not None else 0

    oldest = conn.execute(
        """
        SELECT status, scheduled_for, created_at, updated_at
        FROM routine_occurrences
        WHERE status IN ('claimed', 'running')
        ORDER BY scheduled_for ASC, occurrence_id ASC
        LIMIT 1
        """
    ).fetchone()
    if oldest is not None:
        summary["oldest_nonterminal"] = {
            "status": str(oldest["status"]),
            "scheduled_for": str(oldest["scheduled_for"]),
            "created_at": str(oldest["created_at"]),
            "updated_at": str(oldest["updated_at"]),
        }
    return summary


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
    lines = read_bounded_jsonl_tail(path, limit=bounded)
    events: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            events.append({"line": index, "error": f"invalid_json: {exc.msg}"})
            continue
        if isinstance(parsed, dict):
            events.append(_sanitize_event_value(parsed))
    return events


def _sanitize_event_value(value: Any, *, field_name: str | None = None) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize_event_value(item, field_name=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_event_value(item, field_name=field_name) for item in value]
    if isinstance(value, str):
        return value if _is_safe_event_text_field(field_name) else "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "<redacted>"


def _is_safe_event_text_field(field_name: str | None) -> bool:
    if field_name is None:
        return False
    normalized = field_name.strip().lower().replace("-", "_")
    if normalized in _EVENT_SAFE_TEXT_FIELDS or normalized in _EVENT_SAFE_TEXT_LIST_FIELDS:
        return True
    return any(normalized.endswith(suffix) for suffix in _EVENT_SAFE_TEXT_FIELD_SUFFIXES)


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


def _write_support_archive_exclusive(
    bundle_path: Path,
    *,
    manifest: dict[str, Any],
    sections: dict[str, Any],
    expected_entries: tuple[str, ...],
) -> None:
    """Build, validate, and publish a private ZIP without replacing a path."""

    parent = bundle_path.parent
    directory_fd = _open_bundle_directory(parent)
    temporary_name: str | None = None
    temporary_identity: tuple[int, int] | None = None
    published = False
    try:
        _require_absent_bundle_destination(directory_fd, parent, bundle_path.name)
        descriptor, temporary_name, temporary_identity = _create_bundle_temporary(
            directory_fd,
            parent,
        )
        try:
            if os.name != "nt":
                chmod_descriptor(descriptor, _PRIVATE_BUNDLE_MODE)
            temporary_metadata = os.fstat(descriptor)
            _validate_bundle_file(
                temporary_metadata,
                path=parent / temporary_name,
                expected_links=1,
            )
            if (temporary_metadata.st_dev, temporary_metadata.st_ino) != temporary_identity:
                raise ValueError("Support bundle temporary file identity changed after creation.")

            with os.fdopen(descriptor, "w+b") as handle:
                descriptor = -1
                _populate_support_archive(handle, manifest=manifest, sections=sections)
                handle.flush()
                os.fsync(handle.fileno())
                _validate_support_archive(handle, expected_entries=expected_entries)
                _verify_bundle_entry_identity(
                    directory_fd,
                    parent,
                    temporary_name,
                    expected_identity=temporary_identity,
                    expected_links=1,
                )
                _require_absent_bundle_destination(directory_fd, parent, bundle_path.name)
                _publish_bundle_entry(
                    directory_fd,
                    parent,
                    temporary_name,
                    bundle_path.name,
                )
                published = True
                _verify_bundle_entry_identity(
                    directory_fd,
                    parent,
                    bundle_path.name,
                    expected_identity=temporary_identity,
                    expected_links=2,
                )
                _remove_bundle_entry_if_identity(
                    directory_fd,
                    parent,
                    temporary_name,
                    expected_identity=temporary_identity,
                )
                temporary_name = None
                _verify_bundle_entry_identity(
                    directory_fd,
                    parent,
                    bundle_path.name,
                    expected_identity=temporary_identity,
                    expected_links=1,
                )
                _fsync_bundle_directory(directory_fd, parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except BaseException as exc:
        if published and temporary_identity is not None:
            try:
                _remove_bundle_entry_if_identity(
                    directory_fd,
                    parent,
                    bundle_path.name,
                    expected_identity=temporary_identity,
                )
            except OSError as cleanup_error:
                exc.add_note(f"Unable to remove failed support bundle publication: {cleanup_error}")
        if temporary_name is not None and temporary_identity is not None:
            try:
                _remove_bundle_entry_if_identity(
                    directory_fd,
                    parent,
                    temporary_name,
                    expected_identity=temporary_identity,
                )
            except OSError as cleanup_error:
                exc.add_note(f"Unable to remove support bundle temporary file: {cleanup_error}")
        try:
            _fsync_bundle_directory(directory_fd, parent)
        except OSError as cleanup_error:
            exc.add_note(f"Unable to sync support bundle cleanup: {cleanup_error}")
        raise
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def _populate_support_archive(
    handle: BinaryIO,
    *,
    manifest: dict[str, Any],
    sections: dict[str, Any],
) -> None:
    with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write_json(archive, "manifest.json", manifest)
        for name, payload in sections.items():
            _write_json(archive, name, payload)


def _validate_support_archive(
    handle: BinaryIO,
    *,
    expected_entries: tuple[str, ...],
) -> None:
    handle.seek(0)
    with zipfile.ZipFile(handle, "r") as archive:
        actual_entries = tuple(archive.namelist())
        if actual_entries != expected_entries:
            raise ValueError("Support bundle archive entries failed validation.")
        corrupt_entry = archive.testzip()
        if corrupt_entry is not None:
            raise ValueError(f"Support bundle archive entry failed validation: {corrupt_entry}")


def _open_bundle_directory(parent: Path) -> int | None:
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    before_open = os.lstat(parent)
    _validate_bundle_directory(before_open, parent)
    directory_flag = _bundle_directory_fd_flag()
    if directory_flag is None:
        return None

    flags = os.O_RDONLY | directory_flag | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(parent, flags)
    try:
        opened = os.fstat(descriptor)
        after_open = os.lstat(parent)
        _validate_bundle_directory(opened, parent)
        _validate_bundle_directory(after_open, parent)
        if not os.path.samestat(before_open, opened) or not os.path.samestat(opened, after_open):
            raise ValueError("Support bundle destination directory changed during validation.")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _bundle_directory_fd_flag() -> int | None:
    value: object = getattr(os, "O_DIRECTORY", None)
    if (
        os.name == "nt"
        or not isinstance(value, int)
        or os.open not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.link not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
    ):
        return None
    return value


def _validate_bundle_directory(metadata: os.stat_result, path: Path) -> None:
    if stat.S_ISLNK(metadata.st_mode):
        raise ValueError(f"Support bundle destination directory must not be a symbolic link: {path}")
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"Support bundle destination parent is not a directory: {path}")
    mode = stat.S_IMODE(metadata.st_mode)
    if os.name != "nt" and mode & 0o022 and not metadata.st_mode & stat.S_ISVTX:
        raise PermissionError(
            f"Support bundle destination directory must not be group/world writable: {path}"
        )


def _create_bundle_temporary(
    directory_fd: int | None,
    parent: Path,
) -> tuple[int, str, tuple[int, int]]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(64):
        name = f"{_SUPPORT_BUNDLE_TEMP_PREFIX}{secrets.token_hex(16)}.tmp"
        try:
            if directory_fd is None:
                descriptor = os.open(parent / name, flags, _PRIVATE_BUNDLE_MODE)
            else:
                descriptor = os.open(name, flags, _PRIVATE_BUNDLE_MODE, dir_fd=directory_fd)
        except FileExistsError:
            continue
        try:
            metadata = os.fstat(descriptor)
        except BaseException:
            os.close(descriptor)
            _unlink_bundle_entry(directory_fd, parent, name)
            raise
        return descriptor, name, (metadata.st_dev, metadata.st_ino)
    raise FileExistsError("Unable to allocate an exclusive support bundle temporary file.")


def _require_absent_bundle_destination(
    directory_fd: int | None,
    parent: Path,
    name: str,
) -> None:
    try:
        _bundle_entry_stat(directory_fd, parent, name)
    except FileNotFoundError:
        return
    raise FileExistsError(f"Refusing to overwrite existing support bundle destination: {parent / name}")


def _bundle_entry_stat(directory_fd: int | None, parent: Path, name: str) -> os.stat_result:
    if directory_fd is None:
        return os.stat(parent / name, follow_symlinks=False)
    return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)


def _verify_bundle_entry_identity(
    directory_fd: int | None,
    parent: Path,
    name: str,
    *,
    expected_identity: tuple[int, int],
    expected_links: int,
) -> None:
    metadata = _bundle_entry_stat(directory_fd, parent, name)
    _validate_bundle_file(metadata, path=parent / name, expected_links=expected_links)
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        raise ValueError("Support bundle file identity changed during publication.")


def _validate_bundle_file(
    metadata: os.stat_result,
    *,
    path: Path,
    expected_links: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Support bundle artifacts must be regular files: {path}")
    if metadata.st_nlink != expected_links:
        raise ValueError(f"Support bundle artifact has unsafe hard-link metadata: {path}")
    if os.name != "nt":
        geteuid = getattr(os, "geteuid", None)
        if callable(geteuid) and metadata.st_uid != geteuid():
            raise PermissionError(f"Support bundle artifacts must be owned by the current user: {path}")
        if stat.S_IMODE(metadata.st_mode) != _PRIVATE_BUNDLE_MODE:
            raise PermissionError(f"Support bundle artifacts must be owner-only: {path}")


def _publish_bundle_entry(
    directory_fd: int | None,
    parent: Path,
    temporary_name: str,
    destination_name: str,
) -> None:
    try:
        if directory_fd is None:
            os.link(
                parent / temporary_name,
                parent / destination_name,
                follow_symlinks=False,
            )
        else:
            os.link(
                temporary_name,
                destination_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
    except FileExistsError as exc:
        raise FileExistsError(
            f"Refusing to overwrite existing support bundle destination: {parent / destination_name}"
        ) from exc


def _remove_bundle_entry_if_identity(
    directory_fd: int | None,
    parent: Path,
    name: str,
    *,
    expected_identity: tuple[int, int],
) -> None:
    try:
        metadata = _bundle_entry_stat(directory_fd, parent, name)
    except FileNotFoundError:
        return
    if (metadata.st_dev, metadata.st_ino) != expected_identity or not stat.S_ISREG(
        metadata.st_mode
    ):
        return
    _unlink_bundle_entry(directory_fd, parent, name)


def _unlink_bundle_entry(directory_fd: int | None, parent: Path, name: str) -> None:
    if directory_fd is None:
        os.unlink(parent / name)
    else:
        os.unlink(name, dir_fd=directory_fd)


def _fsync_bundle_directory(directory_fd: int | None, parent: Path) -> None:
    if os.name == "nt":
        return
    opened_here = False
    descriptor = directory_fd
    if descriptor is None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(parent, flags)
        opened_here = True
    try:
        os.fsync(descriptor)
    except OSError as exc:
        unsupported = {
            errno.EINVAL,
            getattr(errno, "ENOTSUP", errno.EINVAL),
            getattr(errno, "EOPNOTSUPP", errno.EINVAL),
        }
        if exc.errno not in unsupported:
            raise
    finally:
        if opened_here:
            os.close(descriptor)


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
