#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from kestrel_native import (
    MacOSKeychainCredentialStore,
    NativeRuntimePolicy,
    SQLiteEventJournal,
    SQLiteExactVectorStore,
    SQLiteStateStore,
    build_doctor_report,
    complete_local_prompt,
    configure_daemon_logging,
    detect_local_model_runtime,
    ensure_home_layout,
    load_native_config,
    sync_markdown_memory,
    write_json_atomic,
)


LOGGER = logging.getLogger("kestrel.daemon")


def _is_terminal_status(status: str) -> bool:
    return status in {"completed", "failed", "cancelled"}


class KestrelDaemon:
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
        self.active_tasks: dict[str, asyncio.Task] = {}
        self.active_processes: dict[str, asyncio.subprocess.Process] = {}
        self.stream_subscribers: dict[str, list[asyncio.Queue]] = {}
        self.last_model_runtime: dict[str, Any] = {}
        self.last_memory_sync: dict[str, Any] = {}
        self.last_watch_snapshot: dict[str, int] = {}

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
        self._write_state(status="running")
        self.heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self.watch_task = asyncio.create_task(self._watch_loop())
        LOGGER.info("Kestrel daemon started on %s", self._control_endpoint())

    async def _shutdown(self) -> None:
        for task in (self.heartbeat_task, self.watch_task):
            if task:
                task.cancel()
        for task in list(self.active_tasks.values()):
            task.cancel()
        for process in list(self.active_processes.values()):
            if process.returncode is None:
                process.terminate()
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
            completion = await complete_local_prompt(
                prompt=self._build_prompt(prompt),
                config=self.config,
            )
            return {
                "message": completion["content"],
                "provider": completion["provider"],
                "model": completion["model"],
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
                    self.active_tasks[task["id"]] = asyncio.create_task(
                        self._execute_shell_task(task["id"], approval["command"], skip_approval=True)
                    )
                else:
                    self.state_store.update_task(task["id"], status="failed", error="Command approval denied")
                    self._publish_event(
                        task["id"],
                        "approval_denied",
                        f"Denied command: {approval['command']}",
                        {"final": True, "status": "failed"},
                    )
                return {"approval": approval}
            raise RuntimeError(f"Unsupported approval action: {action}")
        if method == "channel.status":
            return self._read_channel_status()
        if method == "channel.pair":
            return self._pair_channel(params)
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

    def _control_endpoint(self) -> str:
        if os.name == "nt":
            return f"tcp://{self.paths.control_host}:{self.paths.control_port}"
        return str(self.paths.control_socket)

    def _channel_state_path(self) -> Path:
        return self.paths.state_dir / "gateway-channels.json"

    def _read_channel_status(self) -> dict[str, Any]:
        state_path = self._channel_state_path()
        if not state_path.exists():
            return {"channels": {}}
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"channels": {}}
        telegram = (payload.get("telegram") or {}) if isinstance(payload, dict) else {}
        config = telegram.get("config") or {}
        state = telegram.get("state") or {}
        return {
            "channels": {
                "telegram": {
                    "configured": bool(config.get("token")),
                    "workspace_id": config.get("workspaceId") or "",
                    "mode": config.get("mode") or "polling",
                    "updated_at": config.get("updatedAt") or "",
                    "known_mappings": len((state.get("mappings") or [])),
                }
            }
        }

    def _pair_channel(self, params: dict[str, Any]) -> dict[str, Any]:
        channel = str(params.get("channel") or "telegram")
        if channel != "telegram":
            raise RuntimeError(f"Unsupported channel pairing: {channel}")
        token = str(params.get("token") or "")
        if not token:
            raise RuntimeError("Telegram pairing requires a bot token")
        state_path = self._channel_state_path()
        payload: dict[str, Any] = {}
        if state_path.exists():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                payload = {}
        telegram = dict(payload.get("telegram") or {})
        telegram["config"] = {
            "token": token,
            "workspaceId": str(params.get("workspace_id") or "default"),
            "mode": str(params.get("mode") or "polling"),
            "webhookUrl": params.get("webhook_url") or "",
            "updatedAt": _now(),
        }
        payload["telegram"] = telegram
        state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return {"channel": channel, "status": "paired"}

    async def _cancel_task(self, task_id: str) -> dict[str, Any]:
        if not task_id:
            raise RuntimeError("task.cancel requires task_id")
        task = self.state_store.get_task(task_id)
        if not task:
            raise RuntimeError(f"Unknown task {task_id}")
        process = self.active_processes.get(task_id)
        if process and process.returncode is None:
            process.terminate()
        active = self.active_tasks.get(task_id)
        if active:
            active.cancel()
        else:
            self.state_store.update_task(task_id, status="cancelled", error="Task cancelled")
            self._publish_event(
                task_id,
                "task_cancelled",
                "Task cancelled",
                {"final": True, "status": "cancelled"},
            )
        return {"success": True, "task_id": task_id}

    def _list_task_artifacts(self, task_id: str) -> list[dict[str, Any]]:
        events = self.event_journal.list_events(task_id)
        artifacts: list[dict[str, Any]] = []
        for event in events:
            artifact_url = event.get("artifact_url") or event.get("url")
            artifact_path = event.get("artifact_path") or event.get("path")
            if artifact_url or artifact_path:
                artifacts.append(
                    {
                        "task_id": task_id,
                        "type": event.get("artifact_type") or event.get("type") or "artifact",
                        "url": artifact_url or "",
                        "path": artifact_path or "",
                        "created_at": event.get("created_at") or "",
                    }
                )
            for artifact in event.get("artifacts") or []:
                if isinstance(artifact, dict):
                    artifacts.append(dict(artifact))
        return artifacts

    def _compose_runtime_profile(self) -> dict[str, Any]:
        profile = self.runtime_policy.runtime_profile()
        profile["local_models"] = self.last_model_runtime
        profile["updated_at"] = _now()
        return profile

    def _compose_status(self) -> dict[str, Any]:
        tasks = self.state_store.list_tasks(limit=10)
        uptime = int(time.time() - self.start_time)
        return {
            "status": "running",
            "uptime_seconds": uptime,
            "control_socket": str(self.paths.control_socket),
            "control_endpoint": self._control_endpoint(),
            "runtime_profile": self.state_store.get_runtime_profile() or self._compose_runtime_profile(),
            "recent_tasks": tasks,
            "last_memory_sync": self.last_memory_sync,
            "last_model_runtime": self.last_model_runtime,
            "pending_approvals": self.state_store.list_pending_approvals(),
            "channels": self._read_channel_status().get("channels", {}),
            "paired_nodes": self.state_store.list_paired_nodes(),
            "home": str(self.paths.home),
        }

    def _write_state(self, *, status: str, changed_paths: list[str] | None = None) -> None:
        payload = {
            "status": status,
            "started_at": self.start_time,
            "uptime": time.time() - self.start_time,
            "last_heartbeat": time.time(),
            "next_heartbeat": time.time() + int(self.config.get("heartbeat", {}).get("interval_seconds", 300)),
            "changed_paths": changed_paths or [],
            "recent_tasks": [task["id"] for task in self.state_store.list_tasks(limit=5)],
            "pending_approvals": len(self.state_store.list_pending_approvals()),
            "memory_sync": self.last_memory_sync,
            "control_socket": str(self.paths.control_socket),
            "control_endpoint": self._control_endpoint(),
        }
        self.state_store.set_daemon_state(payload)
        write_json_atomic(self.paths.heartbeat_state_json, payload)

    def _publish_event(
        self,
        task_id: str,
        event_type: str,
        content: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_payload = dict(payload or {})
        event_payload.setdefault("content", content)
        event_payload.setdefault("task_id", task_id)
        event_payload.setdefault("type", event_type)
        event = self.event_journal.append_event(task_id, event_type, event_payload)
        for queue in list(self.stream_subscribers.get(task_id, [])):
            queue.put_nowait(event)
        return event

    async def _stream_task_events(self, task_id: str) -> Any:
        task = self.state_store.get_task(task_id)
        if not task:
            raise RuntimeError(f"Unknown task {task_id}")
        for event in self.event_journal.list_events(task_id):
            yield event
        if _is_terminal_status(task["status"]):
            return
        queue: asyncio.Queue = asyncio.Queue()
        self.stream_subscribers.setdefault(task_id, []).append(queue)
        try:
            while True:
                if _is_terminal_status((self.state_store.get_task(task_id) or {}).get("status", "")):
                    while not queue.empty():
                        yield queue.get_nowait()
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                yield event
                if event.get("final"):
                    break
        finally:
            self.stream_subscribers.get(task_id, []).remove(queue)

    async def _execute_task(self, task_id: str, goal: str) -> None:
        try:
            self.state_store.update_task(task_id, status="running")
            self._publish_event(task_id, "task_started", f"Started: {goal}", {"status": "running"})
            if goal.lower().startswith("shell:"):
                command = goal.split(":", 1)[1].strip()
                await self._execute_shell_task(task_id, command)
                return
            completion = await complete_local_prompt(
                prompt=self._build_prompt(goal),
                config=self.config,
            )
            result = {
                "message": completion["content"],
                "provider": completion["provider"],
                "model": completion["model"],
            }
            self.state_store.update_task(task_id, status="completed", result=result)
            self._publish_event(
                task_id,
                "task_complete",
                completion["content"],
                {
                    "status": "completed",
                    "provider": completion["provider"],
                    "model": completion["model"],
                    "final": True,
                },
            )
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            self.state_store.update_task(task_id, status="cancelled", error="Task cancelled")
            self._publish_event(task_id, "task_cancelled", "Task cancelled", {"final": True, "status": "cancelled"})
            raise
        except Exception as exc:
            self.state_store.update_task(task_id, status="failed", error=str(exc))
            self._publish_event(
                task_id,
                "task_failed",
                str(exc),
                {"status": "failed", "error": str(exc), "final": True},
            )
        finally:
            self.active_tasks.pop(task_id, None)

    async def _execute_shell_task(self, task_id: str, command: str, *, skip_approval: bool = False) -> None:
        decision = self.runtime_policy.evaluate_command(command)
        if not decision["allowed"]:
            raise RuntimeError(f"Blocked by native runtime policy: {command}")
        if decision["approval_required"] and not skip_approval:
            approval = self.state_store.create_approval(
                task_id=task_id,
                operation="shell",
                command=command,
            )
            self.state_store.update_task(
                task_id,
                status="waiting_approval",
                metadata={"approval_id": approval["id"], "pending_command": command},
            )
            self._publish_event(
                task_id,
                "approval_needed",
                f"Approval required for command: {command}",
                {
                    "status": "waiting_approval",
                    "approval_id": approval["id"],
                },
            )
            return

        self.state_store.update_task(task_id, status="running", metadata={"command": command})
        self._publish_event(task_id, "command_started", command, {"status": "running", "command": command})
        started_at = time.time()
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.active_processes[task_id] = process
        try:
            stdout, stderr = await process.communicate()
            duration_ms = int((time.time() - started_at) * 1000)
            result = {
                "command": command,
                "stdout": stdout.decode("utf-8", errors="replace").strip(),
                "stderr": stderr.decode("utf-8", errors="replace").strip(),
                "exit_code": process.returncode,
                "duration_ms": duration_ms,
                "risk_class": decision["risk_class"],
            }
            if process.returncode == 0:
                self.state_store.update_task(task_id, status="completed", result=result)
                self._publish_event(
                    task_id,
                    "task_complete",
                    result["stdout"] or f"Command completed: {command}",
                    {"status": "completed", "final": True, **result},
                )
            else:
                self.state_store.update_task(task_id, status="failed", result=result, error=result["stderr"] or "Command failed")
                self._publish_event(
                    task_id,
                    "task_failed",
                    result["stderr"] or f"Command failed: {command}",
                    {"status": "failed", "final": True, **result},
                )
        finally:
            self.active_processes.pop(task_id, None)

    def _build_prompt(self, goal: str) -> str:
        workspace = self.paths.workspace_md.read_text(encoding="utf-8")
        heartbeat = self.paths.heartbeat_md.read_text(encoding="utf-8")
        return f"{workspace}\n\n{heartbeat}\n\nUser goal:\n{goal}"

    def _snapshot_watched_files(self) -> dict[str, int]:
        snapshot: dict[str, int] = {}
        watched = [
            self.paths.config_yml,
            self.paths.heartbeat_md,
            self.paths.workspace_md,
            self.paths.watchlist_yml,
        ]
        watched.extend(sorted(self.paths.memory_dir.rglob("*.md")))
        for path in watched:
            if path.exists():
                snapshot[str(path)] = path.stat().st_mtime_ns
        return snapshot

    def _detect_watched_changes(self) -> list[str]:
        current = self._snapshot_watched_files()
        changed = [
            path
            for path, mtime in current.items()
            if self.last_watch_snapshot.get(path) != mtime
        ]
        removed = [path for path in self.last_watch_snapshot if path not in current]
        self.last_watch_snapshot = current
        return sorted(changed + removed)

    def _in_quiet_hours(self) -> bool:
        quiet = self.config.get("heartbeat", {}).get("quiet_hours", {})
        if not quiet.get("enabled"):
            return False
        start = str(quiet.get("start", "23:00"))
        end = str(quiet.get("end", "07:00"))
        now = datetime_now_hhmm()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end


def datetime_now_hhmm() -> str:
    return time.strftime("%H:%M", time.localtime())


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


async def main() -> None:
    daemon = KestrelDaemon()
    await daemon.run()


if __name__ == "__main__":
    asyncio.run(main())
