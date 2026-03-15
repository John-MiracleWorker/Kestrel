#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import copy
import contextlib
import hashlib
import importlib.metadata
import json
import logging
import mimetypes
import os
import re
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

try:
    import httpx
except ModuleNotFoundError:  # pragma: no cover - packaging guard
    httpx = None

from kestrel_native import (
    MacOSKeychainCredentialStore,
    NativeAgentRunner,
    NativeAgentOutcome,
    NativeRuntimePolicy,
    NativeSkillPackManager,
    SQLiteEventJournal,
    SQLiteExactVectorStore,
    SQLiteStateStore,
    build_doctor_report,
    configure_daemon_logging,
    describe_chat_tool_categories,
    detect_local_model_runtime,
    ensure_home_layout,
    inspect_local_images,
    load_native_config,
    route_local_image_request,
    resolve_chat_tool_categories,
    sync_markdown_memory,
    write_json_atomic,
)
from .native_persona import proactivity_settings
from .native_shared import _deep_merge


LOGGER = logging.getLogger("kestrel.daemon")
_TELEGRAM_MESSAGE_LIMIT = 4000
_TELEGRAM_APPROVAL_ID_LENGTH = 8
_TELEGRAM_WORKING_DELAY_SECONDS = 3.0


def _is_terminal_status(status: str) -> bool:
    return status in {"completed", "failed", "cancelled"}


_SCREENSHOT_PATTERNS = (
    r"\b(?:take|capture|grab|send|share|show)\b.*\b(?:screenshot|screen ?shot|screen capture)\b",
    r"\b(?:screenshot|screen ?shot|screen capture)\b.*\b(?:please|now|for me|to me)\b",
    r"\b(?:what(?:'s| is)|show me)\b.*\b(?:on )?(?:my|the) screen\b",
)

_LOCAL_FILE_TOKEN_PATTERNS = (
    r'["\']([^"\']+\.[A-Za-z0-9]{1,10})["\']',
    r"((?:~|/)[^\s,;:()]+?\.[A-Za-z0-9]{1,10})",
    r"\b([A-Za-z0-9][A-Za-z0-9._-]*\.[A-Za-z0-9]{1,10})\b",
)

_LOCAL_FILE_SEARCH_PATTERNS = (
    r"\b(?:includes?|contains?|containing|matching|matches?)\s+(?:the\s+)?(?:word|text|string|name)?\s*[\"']?([A-Za-z0-9._-]+)[\"']?",
    r"\b(?:with|containing)\s+[\"']?([A-Za-z0-9._-]+)[\"']?\s+(?:in|on)\s+(?:its\s+)?name\b",
)

_PROACTIVE_ALLOWLIST = (
    "review",
    "summarize",
    "summary",
    "diagnose",
    "checklist",
    "recover",
    "sync",
    "follow-up",
    "follow up",
)
_PROACTIVE_DENYLIST = (
    "write",
    "edit",
    "delete",
    "install",
    "commit",
    "push",
    "send",
    "shell",
    "command",
    "media",
    "image",
    "video",
    "render",
    "generate",
)
_DEFAULT_HEARTBEAT_TASKS = {
    "refresh runtime profile",
    "sync markdown memory",
    "reindex watched files",
}


def _looks_like_screenshot_request(prompt: str) -> bool:
    lowered = (prompt or "").strip().lower()
    return any(re.search(pattern, lowered) for pattern in _SCREENSHOT_PATTERNS)


def _wants_telegram_delivery(prompt: str) -> bool:
    lowered = (prompt or "").strip().lower()
    return any(
        phrase in lowered
        for phrase in (
            "send it to me",
            "send me",
            "share it with me",
            "telegram",
            "message me",
            "dm me",
        )
    )


def _looks_like_local_file_telegram_request(prompt: str) -> bool:
    lowered = (prompt or "").strip().lower()
    if not _wants_telegram_delivery(prompt):
        return False
    return bool(re.search(r"\b(?:send|share|upload|deliver|message|dm)\b", lowered))


def _extract_local_file_reference(prompt: str) -> str:
    raw_prompt = str(prompt or "")
    for pattern in _LOCAL_FILE_TOKEN_PATTERNS:
        matches = re.findall(pattern, raw_prompt)
        for match in matches:
            candidate = str(match or "").strip().strip(".,;:!?")
            if not candidate or "://" in candidate:
                continue
            return candidate
    return ""


def _extract_local_file_search_terms(prompt: str, *, requested_name: str = "") -> list[str]:
    terms: list[str] = []
    raw_prompt = str(prompt or "")
    for pattern in _LOCAL_FILE_SEARCH_PATTERNS:
        for match in re.findall(pattern, raw_prompt, flags=re.IGNORECASE):
            value = str(match or "").strip().strip(".,;:!?").lower()
            if value and value not in terms:
                terms.append(value)

    stem = Path(requested_name).stem.lower()
    if stem:
        for part in re.split(r"[^a-z0-9]+", stem):
            if len(part) < 2:
                continue
            if part not in terms:
                terms.append(part)
    return terms


def _list_local_file_candidates(search_root: Path, *, limit: int = 256) -> list[Path]:
    candidates: list[Path] = []
    try:
        for child in sorted(search_root.iterdir()):
            if child.is_file():
                candidates.append(child)
                if len(candidates) >= limit:
                    return candidates
    except Exception:
        return candidates
    return candidates


def _resolve_local_file_telegram_request(prompt: str) -> dict[str, Any] | None:
    if not _looks_like_local_file_telegram_request(prompt):
        return None

    file_hint = _extract_local_file_reference(prompt)
    lowered = str(prompt or "").strip().lower()
    desktop_root = Path.home() / "Desktop"
    if not file_hint and "desktop" not in lowered:
        return None

    candidate_path: Path
    requested_name = ""
    if file_hint:
        candidate_path = Path(file_hint).expanduser()
        if not candidate_path.is_absolute():
            if "desktop" in lowered:
                candidate_path = desktop_root / candidate_path.name
            else:
                candidate_path = Path.cwd() / candidate_path
        requested_name = candidate_path.name
    else:
        search_terms = _extract_local_file_search_terms(prompt)
        if not search_terms:
            return None
        requested_name = search_terms[0]
        candidate_path = desktop_root / requested_name

    suggestions: list[str] = []
    search_root = candidate_path.parent
    if not search_root.exists() and "desktop" in lowered:
        search_root = desktop_root

    resolved_path = ""
    if search_root.exists():
        try:
            from difflib import SequenceMatcher, get_close_matches

            requested_stem = Path(requested_name).stem.lower()
            requested_suffix = Path(requested_name).suffix.lower()
            search_terms = _extract_local_file_search_terms(prompt, requested_name=requested_name)
            candidates = _list_local_file_candidates(search_root)
            ranked: list[tuple[int, Path, bool]] = []
            direct_matches: list[Path] = []
            for path in candidates:
                name_lower = path.name.lower()
                stem_lower = path.stem.lower()
                score = 0
                direct = False

                if requested_name:
                    requested_lower = requested_name.lower()
                    if name_lower == requested_lower:
                        resolved_path = str(path)
                        direct = True
                        score += 10_000
                    similarity = SequenceMatcher(None, requested_lower, name_lower).ratio()
                    score += int(similarity * 100)
                    if requested_stem:
                        if requested_stem == stem_lower:
                            direct = True
                            score += 800
                        elif requested_stem in stem_lower:
                            direct = True
                            score += 320
                        elif stem_lower in requested_stem:
                            score += 160
                    if requested_suffix and path.suffix.lower() == requested_suffix:
                        score += 60

                matched_terms = 0
                for term in search_terms:
                    if term in stem_lower:
                        matched_terms += 1
                        score += 140
                    elif term in name_lower:
                        matched_terms += 1
                        score += 90

                if search_terms and matched_terms == len(search_terms):
                    direct = True
                    score += 120
                elif search_terms and not requested_name and matched_terms == 0:
                    continue

                if score <= 0:
                    continue
                ranked.append((score, path, direct))
                if direct:
                    direct_matches.append(path)

            if not resolved_path:
                if len(direct_matches) == 1:
                    resolved_path = str(direct_matches[0])
                elif len(ranked) == 1:
                    resolved_path = str(ranked[0][1])

            ranked.sort(key=lambda item: (-item[0], item[1].name.lower()))
            seen: set[str] = set()
            ranked_names = [item[1].name for item in ranked]
            ranked_paths = [item[1] for item in ranked]
            for name in get_close_matches(requested_name, ranked_names, n=3, cutoff=0.45):
                for path in ranked_paths:
                    if path.name == name and str(path) not in seen:
                        suggestions.append(str(path))
                        seen.add(str(path))
                        break
            for _, path, _ in ranked:
                if str(path) in seen:
                    continue
                suggestions.append(str(path))
                seen.add(str(path))
                if len(suggestions) >= 3:
                    break
        except Exception:
            suggestions = []

    return {
        "requested_path": str(candidate_path),
        "requested_name": requested_name,
        "resolved_path": resolved_path or (str(candidate_path) if candidate_path.exists() and candidate_path.is_file() else ""),
        "suggestions": suggestions,
    }


def datetime_now_hhmm() -> str:
    return time.strftime("%H:%M", time.localtime())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _daemon_version() -> str:
    try:
        return importlib.metadata.version("kestrel-cli")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


class KestrelDaemonCore:
    def __init__(self) -> None:
        self.paths = ensure_home_layout()
        configure_daemon_logging(self.paths)
        self.config = load_native_config(self.paths)
        self.state_store = SQLiteStateStore(self.paths.sqlite_db)
        self.state_store.initialize()
        self.event_journal = SQLiteEventJournal(self.paths.sqlite_db)
        self.event_journal.initialize()
        self.vector_store = SQLiteExactVectorStore(self.paths.sqlite_db)
        self.vector_store.initialize()
        self.credential_store = MacOSKeychainCredentialStore()
        self.runtime_policy = NativeRuntimePolicy(self.config)
        self.skill_pack_manager = NativeSkillPackManager(
            paths=self.paths,
            config=self.config,
            state_store=self.state_store,
        )
        self.start_time = time.time()
        self.stop_event: asyncio.Event | None = None
        self.server: asyncio.AbstractServer | None = None
        self.heartbeat_task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None
        self.telegram_poll_task: asyncio.Task | None = None
        self.active_tasks: dict[str, asyncio.Task] = {}
        self.active_processes: dict[str, asyncio.subprocess.Process] = {}
        self.stream_subscribers: dict[str, list[asyncio.Queue]] = {}
        self.telegram_message_tasks: set[asyncio.Task] = set()
        self.last_model_runtime: dict[str, Any] = {}
        self.last_memory_sync: dict[str, Any] = {}
        self.last_watch_snapshot: dict[str, int] = {}
        self.recent_watched_changes: list[str] = []
        self.last_heartbeat_action = "Daemon started."
        self.proactive_cooldowns: dict[str, float] = {}
        self.channel_state_lock = RLock()
        self.gateway_state_lock = RLock()

    def _ensure_stop_event(self) -> asyncio.Event:
        if self.stop_event is None:
            self.stop_event = asyncio.Event()
        return self.stop_event

    async def _refresh_model_runtime(self) -> dict[str, Any]:
        self.last_model_runtime = await detect_local_model_runtime(self.config)
        profile = self._compose_runtime_profile()
        self.state_store.set_runtime_profile(profile)
        write_json_atomic(self.paths.runtime_profile_json, profile)
        return self.last_model_runtime

    def _enabled_chat_tool_categories(self) -> tuple[str, ...]:
        return resolve_chat_tool_categories(self.config)

    def _communication_preferences(self) -> dict[str, Any]:
        communication = self.config.get("communication") or {}
        if not isinstance(communication, dict):
            communication = {}
        telegram = communication.get("telegram") or {}
        if not isinstance(telegram, dict):
            telegram = {}
        mirror_channels = communication.get("mirror_channels")
        if not isinstance(mirror_channels, list):
            mirror_channels = ["web"]
        return {
            "primary_channel": str(communication.get("primary_channel") or "telegram").strip().lower(),
            "mirror_channels": [
                str(item).strip().lower()
                for item in mirror_channels
                if str(item).strip()
            ],
            "telegram_native_primary": bool(telegram.get("native_primary", True)),
            "telegram_mobile_concise": bool(telegram.get("mobile_concise", True)),
        }

    def _heartbeat_interval_seconds(self) -> int:
        heartbeat = self.config.get("heartbeat") or {}
        if not isinstance(heartbeat, dict):
            return 300
        raw_value = heartbeat.get("interval_seconds")
        if raw_value in (None, ""):
            raw_value = heartbeat.get("interval")
        try:
            value = int(raw_value)
        except (TypeError, ValueError):
            value = 300
        return max(value, 1)

    def _gateway_local_state_path(self) -> Path:
        return self.paths.state_dir / "gateway-local.json"

    def _default_gateway_local_document(self) -> dict[str, Any]:
        return {
            "version": 1,
            "users": [],
            "workspaces": [],
            "conversations": [],
            "providerConfigs": [],
            "notifications": [],
            "installedTools": [],
        }

    def _load_gateway_local_state(self) -> dict[str, Any]:
        state_path = self._gateway_local_state_path()
        with self.gateway_state_lock:
            if not state_path.exists():
                return self._default_gateway_local_document()
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return self._default_gateway_local_document()
        if not isinstance(payload, dict):
            return self._default_gateway_local_document()
        document = self._default_gateway_local_document()
        document.update(payload)
        for key in ("users", "workspaces", "conversations", "providerConfigs", "notifications", "installedTools"):
            value = document.get(key)
            document[key] = value if isinstance(value, list) else []
        return document

    def _save_gateway_local_state(self, payload: dict[str, Any]) -> None:
        with self.gateway_state_lock:
            write_json_atomic(self._gateway_local_state_path(), payload)

    def _resolve_workspace_record(self, document: dict[str, Any], workspace_id: str = "") -> dict[str, Any] | None:
        requested = str(workspace_id or "").strip()
        workspaces = [item for item in list(document.get("workspaces") or []) if isinstance(item, dict)]
        if requested:
            for workspace in workspaces:
                if str(workspace.get("id") or "").strip() == requested:
                    return workspace
        telegram_workspace_id = str((self._telegram_runtime() or {}).get("workspace_id") or "").strip()
        if telegram_workspace_id:
            for workspace in workspaces:
                if str(workspace.get("id") or "").strip() == telegram_workspace_id:
                    return workspace
        return workspaces[0] if workspaces else None

    def _resolve_workspace_id(self, workspace_id: str = "") -> str:
        record = self._resolve_workspace_record(self._load_gateway_local_state(), workspace_id)
        return str((record or {}).get("id") or workspace_id or "local")

    def _workspace_settings(self, workspace_id: str = "") -> dict[str, Any]:
        record = self._resolve_workspace_record(self._load_gateway_local_state(), workspace_id)
        settings = (record or {}).get("settings") or {}
        return settings if isinstance(settings, dict) else {}

    def _workspace_system_prompt(self, workspace_id: str = "") -> str:
        document = self._load_gateway_local_state()
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        provider_configs = [
            item
            for item in list(document.get("providerConfigs") or [])
            if isinstance(item, dict) and str(item.get("workspaceId") or "").strip() == resolved_workspace_id
        ]
        if not provider_configs:
            provider_configs = [
                item for item in list(document.get("providerConfigs") or []) if isinstance(item, dict)
            ]
        selected = next((item for item in provider_configs if bool(item.get("isDefault"))), None)
        if selected is None and provider_configs:
            selected = provider_configs[0]
        return str((selected or {}).get("systemPrompt") or "").strip()

    def _config_for_workspace(self, workspace_id: str = "") -> dict[str, Any]:
        merged = copy.deepcopy(self.config)
        workspace_settings = self._workspace_settings(workspace_id)
        agent_settings = workspace_settings.get("agent") or {}
        if isinstance(agent_settings, dict) and agent_settings:
            merged = _deep_merge(merged, {"agent": agent_settings})
        return merged

    def _relative_path_label(self, raw_path: str) -> str:
        path = Path(str(raw_path or "")).expanduser()
        if not str(path):
            return ""
        try:
            return str(path.relative_to(self.paths.home))
        except ValueError:
            try:
                return str(path.relative_to(Path.cwd()))
            except ValueError:
                return str(path)

    def _ambient_state_for_workspace(self, workspace_id: str = "") -> dict[str, Any]:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        pending_approvals = []
        for approval in self.state_store.list_pending_approvals()[:5]:
            if not isinstance(approval, dict):
                continue
            pending_approvals.append(
                {
                    "id": approval.get("id"),
                    "operation": approval.get("operation"),
                    "command": approval.get("command"),
                    "summary": ((approval.get("payload") or {}).get("summary") if isinstance(approval.get("payload"), dict) else "") or approval.get("command") or approval.get("operation"),
                }
            )
        recent_background_tasks = []
        for task in self.state_store.list_tasks(limit=12):
            if not self._is_background_task(task):
                continue
            metadata = task.get("metadata") or {}
            if resolved_workspace_id and str(metadata.get("workspace_id") or "") not in {"", resolved_workspace_id}:
                continue
            recent_background_tasks.append(
                {
                    "id": task.get("id"),
                    "goal": task.get("goal"),
                    "status": task.get("status"),
                    "source": metadata.get("proactive_source") or metadata.get("source") or "background",
                }
            )
            if len(recent_background_tasks) >= 3:
                break
        background_suggestions = self.state_store.list_background_suggestions(
            status="pending",
            workspace_id=resolved_workspace_id or None,
            limit=3,
        )
        return {
            "workspace_id": resolved_workspace_id,
            "pending_approvals_count": len(self.state_store.list_pending_approvals()),
            "pending_approvals": pending_approvals,
            "last_heartbeat_action": self.last_heartbeat_action,
            "watched_changes": [self._relative_path_label(path) for path in self.recent_watched_changes[:5]],
            "recent_background_tasks": recent_background_tasks,
            "background_suggestions": background_suggestions,
        }

    def _parse_iso_timestamp(self, raw_value: str) -> float | None:
        value = str(raw_value or "").strip()
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    def _task_age_seconds(self, task: dict[str, Any]) -> float:
        timestamp = self._parse_iso_timestamp(str(task.get("updated_at") or task.get("created_at") or ""))
        if timestamp is None:
            return 0.0
        return max(time.time() - timestamp, 0.0)

    def _proactive_cooldown_seconds(self, workspace_id: str = "") -> int:
        mode = proactivity_settings(self._config_for_workspace(workspace_id)).get("mode")
        if mode == "conservative":
            return 3600
        if mode == "moderate":
            return 1800
        return 900

    def _proactive_fingerprint(self, *parts: Any) -> str:
        raw = "||".join(str(part).strip() for part in parts if str(part).strip())
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _proactive_goal_is_safe(self, goal: str) -> bool:
        lowered = str(goal or "").strip().lower()
        if not lowered:
            return False
        sanitized = re.sub(
            r"\bdo not\s+(?:write|edit|delete|install|commit|push|send|run)\b[^.\n]*",
            " ",
            lowered,
        )
        sanitized = re.sub(r"\bdo not\s+run\s+shell\s+commands?\b[^.\n]*", " ", sanitized)
        if any(token in sanitized for token in _PROACTIVE_DENYLIST):
            return False
        return any(token in sanitized for token in _PROACTIVE_ALLOWLIST)

    def _proactive_fingerprint_active(self, fingerprint: str, workspace_id: str = "") -> bool:
        if not fingerprint:
            return False
        last_seen = self.proactive_cooldowns.get(fingerprint, 0.0)
        return (time.time() - last_seen) < self._proactive_cooldown_seconds(workspace_id)

    def _mark_proactive_fingerprint(self, fingerprint: str) -> None:
        if fingerprint:
            self.proactive_cooldowns[fingerprint] = time.time()

    def _parse_heartbeat_markdown_tasks(self) -> list[str]:
        try:
            content = self.paths.heartbeat_md.read_text(encoding="utf-8")
        except Exception:
            return []
        tasks: list[str] = []
        in_section = False
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("## "):
                in_section = line.lower() == "## every heartbeat"
                continue
            if not in_section or not line.startswith("- "):
                continue
            task = line[2:].strip()
            if task:
                tasks.append(task)
        return tasks

    def _build_proactive_candidates(self, workspace_id: str) -> list[dict[str, Any]]:
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        candidates: list[dict[str, Any]] = []
        pending_approvals = self.state_store.list_pending_approvals()
        if pending_approvals:
            approval_ids = ",".join(str(item.get("id") or "") for item in pending_approvals[:5])
            candidates.append(
                {
                    "type": "notification",
                    "notification_type": "warning",
                    "title": "Background task needs approval",
                    "body": f"{len(pending_approvals)} pending approval(s) are waiting.",
                    "source": "approval_queue",
                    "workspace_id": resolved_workspace_id,
                    "fingerprint": self._proactive_fingerprint("approval_queue", approval_ids),
                }
            )

        watched_changes = [path for path in self.recent_watched_changes[:5] if path]
        if watched_changes:
            label = ", ".join(self._relative_path_label(path) for path in watched_changes)
            goal = (
                "Review and summarize the recent watched workspace changes. "
                "Focus on intent, risk, and next steps. Do not edit files.\n"
                f"Changed paths: {label}"
            )
            candidates.append(
                {
                    "type": "task",
                    "title": "Background review available",
                    "body": f"I noticed watched-file changes and prepared a review for: {label}",
                    "started_title": "Started background review",
                    "started_body": f"I noticed watched-file changes and started a background review for: {label}",
                    "goal": goal,
                    "source": "watched_changes",
                    "workspace_id": resolved_workspace_id,
                    "fingerprint": self._proactive_fingerprint("watched_changes", *watched_changes),
                    "auto_start_allowed": True,
                }
            )

        for task in self.state_store.list_tasks(limit=20):
            if not isinstance(task, dict) or self._is_background_task(task):
                continue
            metadata = task.get("metadata") or {}
            task_workspace_id = str(metadata.get("workspace_id") or "")
            if resolved_workspace_id and task_workspace_id and task_workspace_id != resolved_workspace_id:
                continue
            if task.get("status") == "failed":
                goal = (
                    "Diagnose the recent task failure and propose next steps. "
                    "Keep the result read-only and do not run shell commands.\n"
                    f"Failed task goal: {task.get('goal')}\n"
                    f"Failure: {task.get('error') or 'Unknown error'}"
                )
                candidates.append(
                    {
                        "type": "task",
                        "title": "Background diagnosis available",
                        "body": f"I noticed a failed task and prepared a read-only diagnosis for: {task.get('goal')}",
                        "started_title": "Started background diagnosis",
                        "started_body": f"I noticed a failed task and started a read-only diagnosis for: {task.get('goal')}",
                        "goal": goal,
                        "source": "failed_task",
                        "workspace_id": resolved_workspace_id,
                        "fingerprint": self._proactive_fingerprint("failed_task", task.get("id")),
                        "auto_start_allowed": True,
                    }
                )
                break

        stalled_threshold_seconds = 20 * 60
        for task in self.state_store.list_tasks(limit=20):
            if not isinstance(task, dict) or self._is_background_task(task):
                continue
            metadata = task.get("metadata") or {}
            task_workspace_id = str(metadata.get("workspace_id") or "")
            if resolved_workspace_id and task_workspace_id and task_workspace_id != resolved_workspace_id:
                continue
            if task.get("status") not in {"queued", "running", "recovering"}:
                continue
            if self._task_age_seconds(task) < stalled_threshold_seconds:
                continue
            goal = (
                "Recover or diagnose the stalled task and return a concise checklist. "
                "Do not edit files or run shell commands.\n"
                f"Task goal: {task.get('goal')}\n"
                f"Task status: {task.get('status')}"
            )
            candidates.append(
                {
                    "type": "task",
                    "title": "Background recovery check available",
                    "body": f"I noticed a stalled task and prepared a recovery check for: {task.get('goal')}",
                    "started_title": "Started background recovery check",
                    "started_body": f"I noticed a stalled task and started a recovery check for: {task.get('goal')}",
                    "goal": goal,
                    "source": "stalled_task",
                    "workspace_id": resolved_workspace_id,
                    "fingerprint": self._proactive_fingerprint("stalled_task", task.get("id")),
                    "auto_start_allowed": True,
                }
            )
            break

        for raw_task in self._parse_heartbeat_markdown_tasks():
            normalized = raw_task.strip().lower()
            if normalized in _DEFAULT_HEARTBEAT_TASKS:
                continue
            goal = f"{raw_task}. Keep it read-only unless approval is requested."
            candidates.append(
                {
                    "type": "task",
                    "title": "Heartbeat opportunity noticed",
                    "body": f"I found a heartbeat task worth following up on: {raw_task}",
                    "started_title": "Started heartbeat follow-up",
                    "started_body": f"I found a heartbeat task and started following up on it: {raw_task}",
                    "goal": goal,
                    "source": "heartbeat_task",
                    "workspace_id": resolved_workspace_id,
                    "fingerprint": self._proactive_fingerprint("heartbeat_task", raw_task),
                    "auto_start_allowed": True,
                }
            )
            break
        return candidates

    def _operator_user_id(self) -> str:
        document = self._load_gateway_local_state()
        for user in list(document.get("users") or []):
            if isinstance(user, dict):
                candidate = str(user.get("id") or "").strip()
                if candidate:
                    return candidate
        return "local-operator"

    def _is_background_task(self, task: dict[str, Any] | None) -> bool:
        metadata = ((task or {}).get("metadata") or {}) if isinstance(task, dict) else {}
        return bool(metadata.get("background")) or str(metadata.get("source") or "").strip().lower() == "proactive"

    def _record_local_notification(
        self,
        *,
        notification_type: str,
        title: str,
        body: str,
        source: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        document = self._load_gateway_local_state()
        notification = {
            "id": str(uuid.uuid4()),
            "userId": self._operator_user_id(),
            "type": str(notification_type or "info"),
            "title": str(title or "").strip(),
            "body": str(body or "").strip(),
            "source": str(source or "native").strip(),
            "data": dict(data or {}),
            "read": False,
            "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        notifications = [item for item in list(document.get("notifications") or []) if isinstance(item, dict)]
        notifications.insert(0, notification)
        del notifications[200:]
        document["notifications"] = notifications
        self._save_gateway_local_state(document)
        return notification

    async def _mirror_notification_to_telegram(self, notification: dict[str, Any]) -> None:
        if not self._telegram_is_primary_channel():
            return
        runtime = self._telegram_runtime()
        allowed_chat_ids = list(runtime.get("allowed_chat_ids") or [])
        if not allowed_chat_ids:
            return
        text = "\n".join(
            part for part in (str(notification.get("title") or "").strip(), str(notification.get("body") or "").strip()) if part
        )
        if not text:
            return
        try:
            await self._telegram_send_text(str(allowed_chat_ids[0]), text)
        except Exception as exc:
            LOGGER.debug("Failed to mirror Telegram notification: %s", exc)

    async def _record_and_mirror_notification(
        self,
        *,
        notification_type: str,
        title: str,
        body: str,
        source: str,
        data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        notification = self._record_local_notification(
            notification_type=notification_type,
            title=title,
            body=body,
            source=source,
            data=data,
        )
        await self._mirror_notification_to_telegram(notification)
        return notification

    async def _notify_background_task_status(
        self,
        task: dict[str, Any] | None,
        *,
        status: str,
        message: str,
        approval: dict[str, Any] | None = None,
    ) -> None:
        if not self._is_background_task(task):
            return
        task = task or {}
        metadata = task.get("metadata") or {}
        source = str(metadata.get("proactive_source") or metadata.get("source") or "background").strip() or "background"
        workspace_id = str(metadata.get("workspace_id") or self._resolve_workspace_id())
        title = "Background task update"
        notification_type = "info"
        if status == "completed":
            title = "Background task complete"
            notification_type = "task_complete"
        elif status == "failed":
            title = "Background task failed"
            notification_type = "warning"
        elif status == "waiting_approval":
            title = "Background task needs approval"
            notification_type = "warning"
        await self._record_and_mirror_notification(
            notification_type=notification_type,
            title=title,
            body=str(message or task.get("goal") or "").strip(),
            source="proactive",
            data={
                "task_id": task.get("id"),
                "workspace_id": workspace_id,
                "background": True,
                "proactive_source": source,
                "approval_id": (approval or {}).get("id") or "",
            },
        )

    def _persist_background_suggestion(self, candidate: dict[str, Any], *, workspace_id: str) -> dict[str, Any]:
        fingerprint = str(candidate.get("fingerprint") or "") or self._proactive_fingerprint(
            candidate.get("source"),
            candidate.get("goal"),
            candidate.get("body"),
        )
        suggestion_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"kestrel:{workspace_id}:{fingerprint}"))
        return self.state_store.upsert_background_suggestion(
            suggestion_id=suggestion_id,
            workspace_id=workspace_id or "local",
            title=str(candidate.get("title") or "Background suggestion"),
            body=str(candidate.get("body") or candidate.get("goal") or ""),
            goal=str(candidate.get("goal") or ""),
            source=str(candidate.get("source") or "heartbeat"),
            fingerprint=fingerprint,
            notification_type=str(candidate.get("notification_type") or "info"),
            task_kind=str(candidate.get("task_kind") or "task"),
            auto_start_allowed=bool(candidate.get("auto_start_allowed")),
            metadata={
                "workspace_id": workspace_id or "local",
                "background": True,
                "proactive_source": candidate.get("source") or "heartbeat",
            },
        )

    async def _accept_background_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        suggestion = self.state_store.get_background_suggestion(suggestion_id)
        if not suggestion:
            raise RuntimeError(f"Unknown suggestion {suggestion_id}")
        if str(suggestion.get("status") or "") != "pending":
            raise RuntimeError(f"Suggestion {suggestion_id} is already {suggestion.get('status')}")
        task = await self._start_proactive_task(
            {
                "workspace_id": suggestion.get("workspace_id") or self._resolve_workspace_id(),
                "title": suggestion.get("title") or "Background suggestion",
                "body": suggestion.get("body") or suggestion.get("goal") or "",
                "goal": suggestion.get("goal") or "",
                "source": suggestion.get("source") or "heartbeat",
                "fingerprint": suggestion.get("fingerprint") or "",
                "task_kind": suggestion.get("task_kind") or "task",
                "auto_start_allowed": bool(suggestion.get("auto_start_allowed")),
            },
            suggestion_id=suggestion_id,
        )
        task_record = task.get("task") if isinstance(task, dict) and isinstance(task.get("task"), dict) else task
        resolved = self.state_store.get_background_suggestion(suggestion_id)
        return {"suggestion": resolved, "task": task_record}

    def _dismiss_background_suggestion(self, suggestion_id: str) -> dict[str, Any]:
        suggestion = self.state_store.resolve_background_suggestion(
            suggestion_id,
            status="dismissed",
            metadata={"dismissed_at": _now()},
        )
        if not suggestion:
            raise RuntimeError(f"Unknown suggestion {suggestion_id}")
        workspace_id = str(suggestion.get("workspace_id") or "local")
        self.state_store.append_learning_event(
            event_id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            task_id="",
            event_type="suggestion_dismissed",
            summary=str(suggestion.get("title") or "Dismissed suggestion"),
            payload={"suggestion_id": suggestion_id, "source": suggestion.get("source") or ""},
        )
        return {"suggestion": suggestion}

    async def _start_proactive_task(self, candidate: dict[str, Any], suggestion_id: str = "") -> dict[str, Any]:
        workspace_id = str(candidate.get("workspace_id") or self._resolve_workspace_id())
        task_kind = str(candidate.get("task_kind") or "task") or "task"
        task = self.state_store.create_task(
            goal=str(candidate.get("goal") or "").strip(),
            kind=task_kind,
            metadata={
                "workspace_id": workspace_id,
                "source": "proactive",
                "background": True,
                "proactive_source": candidate.get("source") or "heartbeat",
                "proactive_fingerprint": candidate.get("fingerprint") or "",
                "proactive_title": candidate.get("title") or "",
                "suggestion_id": suggestion_id,
            },
        )
        if task_kind == "research":
            self._ensure_research_session(
                task_id=task["id"],
                workspace_id=workspace_id,
                prompt=task["goal"],
                metadata={"source": "proactive"},
            )
        self._publish_event(task["id"], "task_queued", f"Queued: {task['goal']}", {"status": "queued"})
        await self._record_and_mirror_notification(
            notification_type="info",
            title=str(candidate.get("started_title") or candidate.get("title") or "Started background task"),
            body=str(candidate.get("started_body") or candidate.get("body") or task["goal"]),
            source="proactive",
            data={
                "task_id": task["id"],
                "workspace_id": workspace_id,
                "background": True,
                "proactive_source": candidate.get("source") or "heartbeat",
                "suggestion_id": suggestion_id,
            },
        )
        if suggestion_id:
            self.state_store.resolve_background_suggestion(
                suggestion_id,
                status="accepted",
                task_id=task["id"],
                metadata={"accepted_at": _now()},
            )
            self.state_store.append_learning_event(
                event_id=str(uuid.uuid4()),
                workspace_id=workspace_id,
                task_id=task["id"],
                event_type="suggestion_accepted",
                summary=str(candidate.get("title") or task["goal"]),
                payload={"suggestion_id": suggestion_id, "source": candidate.get("source") or "heartbeat"},
            )
        self.active_tasks[task["id"]] = asyncio.create_task(
            self._execute_task(task["id"], task["goal"], kind=task_kind, history=[])
        )
        return task

    async def _run_proactive_heartbeat(self) -> None:
        workspace_id = self._resolve_workspace_id()
        settings = proactivity_settings(self._config_for_workspace(workspace_id))
        surfaced_anything = False
        for candidate in self._build_proactive_candidates(workspace_id):
            fingerprint = str(candidate.get("fingerprint") or "")
            if self._proactive_fingerprint_active(fingerprint, workspace_id):
                continue
            if candidate.get("type") == "notification":
                await self._record_and_mirror_notification(
                    notification_type=str(candidate.get("notification_type") or "info"),
                    title=str(candidate.get("title") or "Background notice"),
                    body=str(candidate.get("body") or ""),
                    source="proactive",
                    data={
                        "workspace_id": workspace_id,
                        "background": True,
                        "proactive_source": candidate.get("source") or "heartbeat",
                    },
                )
                self._mark_proactive_fingerprint(fingerprint)
                self.last_heartbeat_action = str(candidate.get("body") or candidate.get("title") or "Noticed pending work.")
                surfaced_anything = True
                continue

            background_execution = str(settings.get("background_execution") or "suggest_first")
            auto_start_allowed = bool(candidate.get("auto_start_allowed")) and self._proactive_goal_is_safe(
                str(candidate.get("goal") or "")
            )
            if background_execution == "auto_start_safe" and auto_start_allowed:
                await self._start_proactive_task(candidate)
                self._mark_proactive_fingerprint(fingerprint)
                self.last_heartbeat_action = str(
                    candidate.get("started_body")
                    or candidate.get("started_title")
                    or candidate.get("body")
                    or candidate.get("title")
                    or "Started background task."
                )
                surfaced_anything = True
                break

            title = "Background opportunity noticed" if background_execution == "notify_only" else "Background suggestion ready"
            suggestion = self._persist_background_suggestion(candidate, workspace_id=workspace_id)
            await self._record_and_mirror_notification(
                notification_type="info",
                title=title,
                body=str(candidate.get("body") or candidate.get("goal") or ""),
                source="proactive",
                data={
                    "workspace_id": workspace_id,
                    "background": True,
                    "proactive_source": candidate.get("source") or "heartbeat",
                    "goal": candidate.get("goal") or "",
                    "suggestion_id": suggestion.get("id") or "",
                },
            )
            self._mark_proactive_fingerprint(fingerprint)
            self.last_heartbeat_action = str(
                candidate.get("body")
                or candidate.get("title")
                or "Queued a background suggestion for review."
            )
            surfaced_anything = True
            break

        if not surfaced_anything:
            self.last_heartbeat_action = "No proactive opportunities right now."

    def _telegram_is_primary_channel(self) -> bool:
        prefs = self._communication_preferences()
        return prefs["primary_channel"] == "telegram" and prefs["telegram_native_primary"]

    def _build_agent_runner(self, *, task_id: str = "", workspace_id: str = "") -> NativeAgentRunner:
        event_callback = None
        if task_id:
            def _event_callback(event_type: str, content: str, payload: dict[str, Any]) -> None:
                self._publish_event(task_id, event_type, content, payload)
            event_callback = _event_callback
        resolved_workspace_id = self._resolve_workspace_id(workspace_id)
        workspace_settings = self._workspace_settings(resolved_workspace_id)
        workspace_config = self._config_for_workspace(resolved_workspace_id)
        return NativeAgentRunner(
            paths=self.paths,
            config=workspace_config,
            runtime_policy=self.runtime_policy,
            vector_store=self.vector_store,
            state_store=self.state_store,
            event_callback=event_callback,
            skill_pack_manager=self.skill_pack_manager,
            workspace_id=resolved_workspace_id,
            workspace_settings=workspace_settings,
            workspace_system_prompt=self._workspace_system_prompt(resolved_workspace_id),
            ambient_state=self._ambient_state_for_workspace(resolved_workspace_id),
        )

    async def run(self) -> None:
        stop_event = self._ensure_stop_event()
        self._register_signals()
        await self._startup()
        try:
            await stop_event.wait()
        finally:
            await self._shutdown()

    def _register_signals(self) -> None:
        loop = asyncio.get_running_loop()
        stop_event = self._ensure_stop_event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:  # pragma: no cover - platform fallback
                signal.signal(sig, lambda *_args: stop_event.set())

    async def _startup(self) -> None:
        if os.name != "nt" and self.paths.control_socket.exists():
            self.paths.control_socket.unlink()

        await self._refresh_model_runtime()
        self.last_memory_sync = sync_markdown_memory(self.paths, self.vector_store)

        recovered = self.state_store.recover_inflight_tasks()
        for task in recovered:
            self._publish_event(
                task["id"],
                "task_recovered",
                f"Task recovered after daemon restart: {task['goal']}",
                {"status": task["status"], "final": True},
            )

        if os.name == "nt":
            self.server = await asyncio.start_server(
                self._handle_client,
                host=self.paths.control_host,
                port=self.paths.control_port,
            )
        else:
            self.server = await asyncio.start_unix_server(
                self._handle_client,
                path=str(self.paths.control_socket),
            )
            os.chmod(self.paths.control_socket, 0o600)
        self.last_watch_snapshot = self._snapshot_watched_files()
        self._sync_telegram_channel_state_from_environment()
        self._write_state(status="running")
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self.watch_task = asyncio.create_task(self._watch_loop())
        await self._configure_telegram_transport()
        prefs = self._communication_preferences()
        LOGGER.info(
            "Kestrel daemon started on %s (version=%s, primary_channel=%s, mirror_channels=%s, telegram_owner=%s, code=%s)",
            self._control_endpoint(),
            _daemon_version(),
            prefs["primary_channel"],
            ",".join(prefs["mirror_channels"]) or "none",
            "native_daemon" if prefs["telegram_native_primary"] else "gateway",
            Path(__file__).resolve(),
        )

    async def _shutdown(self) -> None:
        for task in (self.heartbeat_task, self.watch_task, self.telegram_poll_task):
            if task:
                task.cancel()
        for task in list(self.active_tasks.values()):
            task.cancel()
        for task in list(self.telegram_message_tasks):
            task.cancel()
        for process in list(self.active_processes.values()):
            if process.returncode is None:
                process.terminate()
        for task in list(self.telegram_message_tasks):
            try:
                await task
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                pass
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        if os.name != "nt" and self.paths.control_socket.exists():
            self.paths.control_socket.unlink()
        self._write_state(status="stopped")
        LOGGER.info("Kestrel daemon stopped")

    async def _heartbeat_loop(self) -> None:
        stop_event = self._ensure_stop_event()
        while not stop_event.is_set():
            interval = self._heartbeat_interval_seconds()
            if not self._in_quiet_hours():
                await self._refresh_model_runtime()
                await self._run_proactive_heartbeat()
            else:
                self.last_heartbeat_action = "Quiet hours active; proactive work is paused."
            self._write_state(status="running")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(interval, 1))
            except asyncio.TimeoutError:
                continue

    async def _watch_loop(self) -> None:
        interval = int(self.config.get("watch", {}).get("poll_interval_seconds", 5))
        stop_event = self._ensure_stop_event()
        while not stop_event.is_set():
            changed_paths = self._detect_watched_changes()
            if changed_paths:
                if str(self.paths.config_yml) in changed_paths:
                    self.config = load_native_config(self.paths)
                    self.runtime_policy = NativeRuntimePolicy(self.config)
                    await self._refresh_model_runtime()
                memory_changed = any(path.endswith(".md") for path in changed_paths)
                if memory_changed:
                    self.last_memory_sync = sync_markdown_memory(self.paths, self.vector_store)
                if str(self._channel_state_path()) in changed_paths:
                    await self._configure_telegram_transport()
                self.recent_watched_changes = list(changed_paths)
                LOGGER.info("Watched files changed: %s", changed_paths)
                self._write_state(status="running", changed_paths=changed_paths)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=max(interval, 1))
            except asyncio.TimeoutError:
                continue

    async def _write_control_response(
        self,
        writer: asyncio.StreamWriter,
        payload: dict[str, Any],
    ) -> bool:
        try:
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
            return True
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.debug("Control client disconnected before response delivery")
            return False

    async def _close_control_writer(self, writer: asyncio.StreamWriter) -> None:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            request = json.loads(raw.decode("utf-8"))
            request_id = request.get("request_id", "")
            method = request.get("method", "")
            params = request.get("params") or {}
            if method == "task.stream":
                async for event in self._stream_task_events(params["task_id"]):
                    if not await self._write_control_response(
                        writer,
                        {"request_id": request_id, "ok": True, "event": event},
                    ):
                        return
                await self._write_control_response(
                    writer,
                    {"request_id": request_id, "ok": True, "done": True, "result": {"status": "complete"}},
                )
                return

            result = await self._dispatch(method, params)
            await self._write_control_response(
                writer,
                {"request_id": request_id, "ok": True, "done": True, "result": result},
            )
        except (BrokenPipeError, ConnectionResetError):
            LOGGER.debug("Control client disconnected during request handling")
        except Exception as exc:  # pragma: no cover - integration guard
            LOGGER.exception("Control request failed")
            payload = {
                "request_id": request.get("request_id", "") if "request" in locals() else "",
                "ok": False,
                "done": True,
                "error": {
                    "message": str(exc),
                    "code": exc.__class__.__name__,
                },
            }
            await self._write_control_response(writer, payload)
        finally:
            await self._close_control_writer(writer)

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "status":
            await self._refresh_model_runtime()
            return self._compose_status()
        if method == "doctor":
            await self._refresh_model_runtime()
            return build_doctor_report(
                paths=self.paths,
                config=self.config,
                runtime_profile=self._compose_runtime_profile(),
                model_runtime=self.last_model_runtime,
            )
        if method == "runtime.profile":
            await self._refresh_model_runtime()
            return self._compose_runtime_profile()
        if method == "skill.list":
            return self.skill_pack_manager.catalog(
                include_synthetic=bool(params.get("include_synthetic", True)),
                include_marketplace=bool(params.get("include_marketplace", True)),
            )
        if method == "skill.search":
            return self.skill_pack_manager.search(
                str(params.get("query") or ""),
                include_marketplace=bool(params.get("include_marketplace", True)),
            )
        if method == "skill.inspect":
            pack_id = str(params.get("pack_id") or "").strip().lower()
            if not pack_id:
                raise RuntimeError("skill.inspect requires pack_id")
            result = self.skill_pack_manager.inspect(pack_id)
            if result is None:
                raise RuntimeError(f"Unknown skill pack: {pack_id}")
            return {"pack": result}
        if method == "skill.install":
            result = self.skill_pack_manager.install(
                pack_id=str(params.get("pack_id") or "").strip().lower(),
                source_path=str(params.get("source_path") or "").strip(),
                source_url=str(params.get("source_url") or "").strip(),
                scope=str(params.get("scope") or "user").strip().lower() or "user",
            )
            return result
        if method == "skill.import":
            result = self.skill_pack_manager.import_pack(
                source_path=str(params.get("source_path") or "").strip(),
                scope=str(params.get("scope") or "user").strip().lower() or "user",
            )
            return result
        if method == "skill.enable":
            return self.skill_pack_manager.enable(str(params.get("pack_id") or "").strip().lower())
        if method == "skill.disable":
            return self.skill_pack_manager.disable(str(params.get("pack_id") or "").strip().lower())
        if method == "skill.remove":
            return self.skill_pack_manager.remove(str(params.get("pack_id") or "").strip().lower())
        if method == "memory.sync":
            self.last_memory_sync = sync_markdown_memory(self.paths, self.vector_store)
            self._write_state(status="running")
            return self.last_memory_sync
        if method == "suggestion.list":
            return {
                "suggestions": self.state_store.list_background_suggestions(
                    status=str(params.get("status") or "") or None,
                    workspace_id=self._resolve_workspace_id(str(params.get("workspace_id") or "")) if params.get("workspace_id") else None,
                    limit=int(params.get("limit") or 25),
                )
            }
        if method == "suggestion.resolve":
            action = str(params.get("action") or "").strip().lower()
            suggestion_id = str(params.get("suggestion_id") or "").strip()
            if not suggestion_id:
                raise RuntimeError("suggestion.resolve requires suggestion_id")
            if action == "accept":
                return await self._accept_background_suggestion(suggestion_id)
            if action == "dismiss":
                return self._dismiss_background_suggestion(suggestion_id)
            raise RuntimeError(f"Unsupported suggestion action: {action}")
        if method == "research.start":
            workspace_id = self._resolve_workspace_id(str(params.get("workspace_id") or ""))
            prompt = str(params.get("prompt") or params.get("goal") or "").strip()
            if not prompt:
                raise RuntimeError("research.start requires prompt")
            history = [
                {
                    "role": str(item.get("role") or "").strip().lower(),
                    "content": str(item.get("content") or "").strip(),
                }
                for item in list(params.get("history") or [])[-12:]
                if isinstance(item, dict)
                and str(item.get("role") or "").strip().lower() in {"user", "assistant"}
                and str(item.get("content") or "").strip()
            ]
            task = self.state_store.create_task(
                goal=prompt,
                kind="research",
                metadata={"workspace_id": workspace_id, "source": "research"},
            )
            session = self._ensure_research_session(
                task_id=task["id"],
                workspace_id=workspace_id,
                prompt=prompt,
                metadata={"source": "research"},
            )
            self.state_store.update_task(task["id"], metadata={"research_session_id": session["id"]})
            self._publish_event(task["id"], "task_queued", f"Queued: {task['goal']}", {"status": "queued"})
            self.active_tasks[task["id"]] = asyncio.create_task(
                self._execute_task(task["id"], task["goal"], kind="research", history=history)
            )
            return {"task": self.state_store.get_task(task["id"]), "research_session": session}
        if method == "research.list":
            return {
                "research_sessions": self.state_store.list_research_sessions(
                    workspace_id=self._resolve_workspace_id(str(params.get("workspace_id") or "")) if params.get("workspace_id") else None,
                    status=str(params.get("status") or "") or None,
                    limit=int(params.get("limit") or 25),
                )
            }
        if method == "research.detail":
            session_id = str(params.get("session_id") or "").strip()
            if not session_id:
                raise RuntimeError("research.detail requires session_id")
            return {"research_session": self.state_store.get_research_session(session_id)}
        if method == "procedure.list":
            return {
                "procedures": self.state_store.list_procedures(
                    workspace_id=self._resolve_workspace_id(str(params.get("workspace_id") or "")) if params.get("workspace_id") else None,
                    enabled_only=bool(params.get("enabled_only")),
                    limit=int(params.get("limit") or 25),
                )
            }
        if method == "learning.list":
            return {
                "events": self.state_store.list_learning_events(
                    workspace_id=self._resolve_workspace_id(str(params.get("workspace_id") or "")) if params.get("workspace_id") else None,
                    task_id=str(params.get("task_id") or "") or None,
                    event_type=str(params.get("event_type") or "") or None,
                    limit=int(params.get("limit") or 50),
                )
            }
        if method == "task.list":
            tasks = self.state_store.list_tasks(limit=int(params.get("limit", 25)))
            status_filter = str(params.get("status") or "").strip()
            if status_filter:
                tasks = [task for task in tasks if task.get("status") == status_filter]
            return {"tasks": tasks}
        if method == "task.start":
            history = [
                {
                    "role": str(item.get("role") or "").strip().lower(),
                    "content": str(item.get("content") or "").strip(),
                }
                for item in list(params.get("history") or [])[-12:]
                if isinstance(item, dict)
                and str(item.get("role") or "").strip().lower() in {"user", "assistant"}
                and str(item.get("content") or "").strip()
            ]
            task_kind = str(params.get("kind") or "task")
            workspace_id = self._resolve_workspace_id(str(params.get("workspace_id") or ""))
            task = self.state_store.create_task(
                goal=str(params.get("goal") or ""),
                kind=task_kind,
                metadata={"workspace_id": workspace_id},
            )
            if task_kind == "research":
                session = self._ensure_research_session(
                    task_id=task["id"],
                    workspace_id=workspace_id,
                    prompt=task["goal"],
                    metadata={"source": "task.start"},
                )
                self.state_store.update_task(task["id"], metadata={"research_session_id": session["id"]})
            self._publish_event(task["id"], "task_queued", f"Queued: {task['goal']}", {"status": "queued"})
            self.active_tasks[task["id"]] = asyncio.create_task(
                self._execute_task(
                    task["id"],
                    task["goal"],
                    kind=task_kind,
                    history=history,
                )
            )
            return {"task": task}
        if method == "task.cancel":
            task_id = str(params.get("task_id") or "")
            return await self._cancel_task(task_id)
        if method == "task.detail":
            task_id = str(params.get("task_id") or "")
            return {"task": self.state_store.get_task(task_id)}
        if method == "task.timeline":
            task_id = str(params.get("task_id") or "")
            return {"events": self.event_journal.list_events(task_id)}
        if method == "task.artifacts":
            task_id = str(params.get("task_id") or "")
            return {"artifacts": self._list_task_artifacts(task_id)}
        if method == "task.approvals":
            return {
                "approvals": self.state_store.list_approvals(
                    task_id=str(params.get("task_id") or "") or None,
                    status=str(params.get("status") or "") or None,
                )
            }
        if method == "chat":
            prompt = str(params.get("prompt") or "")
            history = params.get("history") or []
            workspace_id = self._resolve_workspace_id(str(params.get("workspace_id") or ""))
            task = self.state_store.create_task(
                goal=prompt,
                kind="chat",
                metadata={"workspace_id": workspace_id, "source": "chat"},
            )
            initial_tool_call = self._detect_fast_path_tool_call(prompt)
            outcome = await self._run_native_agent_task(
                task["id"],
                prompt,
                kind="chat",
                history=history,
                initial_tool_call=initial_tool_call,
            )
            response: dict[str, Any] = {
                "message": "" if outcome.status == "failed" else outcome.message,
                "provider": outcome.provider,
                "model": outcome.model,
                "status": outcome.status,
                "task_id": task["id"],
            }
            if outcome.plan is not None:
                response["plan"] = outcome.plan
            if outcome.approval is not None:
                response["approval"] = outcome.approval
            if outcome.artifacts:
                response["artifacts"] = outcome.artifacts
            if outcome.status == "failed":
                response["error"] = outcome.message
            return {
                **response,
            }
        if method == "approval":
            action = str(params.get("action") or "list")
            if action == "list":
                return {"approvals": self.state_store.list_pending_approvals()}
            if action == "resolve":
                approval_id = str(params.get("approval_id") or "")
                approved = bool(params.get("approved"))
                approval = self.state_store.resolve_approval(approval_id, approved)
                if not approval:
                    raise RuntimeError(f"Unknown approval {approval_id}")
                task = self.state_store.get_task(approval["task_id"])
                if not task:
                    raise RuntimeError(f"Unknown task for approval {approval_id}")
                if approved:
                    self._publish_event(
                        task["id"],
                        "approval_resolved",
                        f"Approved: {approval['command']}",
                        {"status": "running", "approval_id": approval["id"]},
                    )
                    resume_state = ((approval.get("resume") or {}).get("state") or {})
                    if isinstance(resume_state, dict) and resume_state:
                        self.active_tasks[task["id"]] = asyncio.create_task(
                            self._resume_task_from_approval(task["id"], approval)
                        )
                    elif approval["operation"] in {"shell", "shell_command"}:
                        self.active_tasks[task["id"]] = asyncio.create_task(
                            self._execute_shell_task(task["id"], approval["command"], skip_approval=True)
                        )
                    else:
                        raise RuntimeError(f"Approval {approval_id} cannot be resumed without stored state")
                else:
                    self.state_store.update_task(
                        task["id"],
                        status="failed",
                        error=f"Approval denied: {approval['command']}",
                    )
                    self._publish_event(
                        task["id"],
                        "approval_denied",
                        f"Denied action: {approval['command']}",
                        {"final": True, "status": "failed"},
                    )
                return {"approval": approval}
            raise RuntimeError(f"Unsupported approval action: {action}")
        if method == "channel.status":
            return self._read_channel_status()
        if method == "channel.pair":
            result = self._pair_channel(params)
            await self._configure_telegram_transport()
            return result
        if method == "paired_nodes.status":
            return {"nodes": self.state_store.list_paired_nodes()}
        if method == "paired_nodes.register":
            node_id = str(params.get("node_id") or "")
            if not node_id:
                raise RuntimeError("paired_nodes.register requires node_id")
            node = self.state_store.upsert_paired_node(
                node_id=node_id,
                node_type=str(params.get("node_type") or "generic"),
                capabilities=list(params.get("capabilities") or []),
                platform_name=str(params.get("platform") or ""),
                health=str(params.get("health") or "ok"),
                address=str(params.get("address") or ""),
                auth=dict(params.get("auth") or {}),
                workspace_binding=str(params.get("workspace_binding") or ""),
                metadata=dict(params.get("metadata") or {}),
            )
            return {"node": node}
        if method == "service.restart":
            self._ensure_stop_event().set()
            return {"status": "restarting"}
        if method == "shutdown":
            self._ensure_stop_event().set()
            return {"status": "stopping"}
        raise RuntimeError(f"Unsupported control method: {method}")
