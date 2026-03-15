#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import json
import logging
import mimetypes
import os
import re
import signal
import time
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
        self.stop_event = asyncio.Event()
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
        self.channel_state_lock = RLock()

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

    def _telegram_is_primary_channel(self) -> bool:
        prefs = self._communication_preferences()
        return prefs["primary_channel"] == "telegram" and prefs["telegram_native_primary"]

    def _build_agent_runner(self, *, task_id: str = "") -> NativeAgentRunner:
        event_callback = None
        if task_id:
            def _event_callback(event_type: str, content: str, payload: dict[str, Any]) -> None:
                self._publish_event(task_id, event_type, content, payload)
            event_callback = _event_callback
        return NativeAgentRunner(
            paths=self.paths,
            config=self.config,
            runtime_policy=self.runtime_policy,
            vector_store=self.vector_store,
            state_store=self.state_store,
            event_callback=event_callback,
            skill_pack_manager=self.skill_pack_manager,
        )

    async def run(self) -> None:
        self._register_signals()
        await self._startup()
        try:
            await self.stop_event.wait()
        finally:
            await self._shutdown()

    def _register_signals(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except NotImplementedError:  # pragma: no cover - platform fallback
                signal.signal(sig, lambda *_args: self.stop_event.set())

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
        interval = int(self.config.get("heartbeat", {}).get("interval_seconds", 300))
        while not self.stop_event.is_set():
            if not self._in_quiet_hours():
                await self._refresh_model_runtime()
            self._write_state(status="running")
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=max(interval, 1))
            except asyncio.TimeoutError:
                continue

    async def _watch_loop(self) -> None:
        interval = int(self.config.get("watch", {}).get("poll_interval_seconds", 5))
        while not self.stop_event.is_set():
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
                LOGGER.info("Watched files changed: %s", changed_paths)
                self._write_state(status="running", changed_paths=changed_paths)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=max(interval, 1))
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
        if method == "task.list":
            tasks = self.state_store.list_tasks(limit=int(params.get("limit", 25)))
            status_filter = str(params.get("status") or "").strip()
            if status_filter:
                tasks = [task for task in tasks if task.get("status") == status_filter]
            return {"tasks": tasks}
        if method == "task.start":
            task = self.state_store.create_task(
                goal=str(params.get("goal") or ""),
                kind=str(params.get("kind") or "task"),
                metadata={"workspace_id": params.get("workspace_id") or "local"},
            )
            self._publish_event(task["id"], "task_queued", f"Queued: {task['goal']}", {"status": "queued"})
            self.active_tasks[task["id"]] = asyncio.create_task(self._execute_task(task["id"], task["goal"]))
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
            task = self.state_store.create_task(
                goal=prompt,
                kind="chat",
                metadata={"workspace_id": params.get("workspace_id") or "local", "source": "chat"},
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
            self.stop_event.set()
            return {"status": "restarting"}
        if method == "shutdown":
            self.stop_event.set()
            return {"status": "stopping"}
        raise RuntimeError(f"Unsupported control method: {method}")
