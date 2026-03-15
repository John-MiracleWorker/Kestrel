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
    NativeRuntimePolicy,
    SQLiteEventJournal,
    SQLiteExactVectorStore,
    SQLiteStateStore,
    build_doctor_report,
    configure_daemon_logging,
    describe_chat_tool_categories,
    detect_local_model_runtime,
    ensure_home_layout,
    load_native_config,
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

        self.last_model_runtime = await detect_local_model_runtime(self.config)
        self.state_store.set_runtime_profile(self._compose_runtime_profile())
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
                self.last_model_runtime = await detect_local_model_runtime(self.config)
                profile = self._compose_runtime_profile()
                self.state_store.set_runtime_profile(profile)
                write_json_atomic(self.paths.runtime_profile_json, profile)
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
                    writer.write(
                        (json.dumps({"request_id": request_id, "ok": True, "event": event}) + "\n").encode("utf-8")
                    )
                    await writer.drain()
                writer.write(
                    (json.dumps({"request_id": request_id, "ok": True, "done": True, "result": {"status": "complete"}}) + "\n").encode("utf-8")
                )
                await writer.drain()
                return

            result = await self._dispatch(method, params)
            writer.write(
                (json.dumps({"request_id": request_id, "ok": True, "done": True, "result": result}) + "\n").encode("utf-8")
            )
            await writer.drain()
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
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "status":
            return self._compose_status()
        if method == "doctor":
            return build_doctor_report(
                paths=self.paths,
                config=self.config,
                runtime_profile=self._compose_runtime_profile(),
                model_runtime=self.last_model_runtime,
            )
        if method == "runtime.profile":
            return self._compose_runtime_profile()
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
