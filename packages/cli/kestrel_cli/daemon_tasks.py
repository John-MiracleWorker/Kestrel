from __future__ import annotations

from . import daemon_telegram_io as _daemon_telegram_io

globals().update({name: value for name, value in vars(_daemon_telegram_io).items() if not name.startswith("__")})

class KestrelDaemonTaskMixin:
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

    async def _run_native_agent_task(
        self,
        task_id: str,
        goal: str,
        *,
        kind: str,
        history: list[dict[str, Any]] | None = None,
        initial_tool_call: dict[str, Any] | None = None,
        resume_state: dict[str, Any] | None = None,
        approved: bool = False,
    ):
        if resume_state:
            self.state_store.update_task(
                task_id,
                status="running",
                metadata={"resumed_at": _now(), "approval_resumed": True},
            )
            self._publish_event(
                task_id,
                "task_resumed",
                f"Resumed: {goal}",
                {"status": "running"},
            )
            runner_goal = goal
        else:
            self.state_store.update_task(task_id, status="running", metadata={"kind": kind})
            self._publish_event(task_id, "task_started", f"Started: {goal}", {"status": "running"})
            runner_goal = self._build_prompt(goal)

        outcome = await self._build_agent_runner(task_id=task_id).run(
            goal=runner_goal,
            history=history or [],
            task_id=task_id,
            task_kind=kind,
            initial_tool_call=initial_tool_call,
            resume_state=resume_state,
            approved=approved,
        )
        if outcome.status == "completed":
            self._publish_event(
                task_id,
                "task_complete",
                outcome.message,
                {
                    "status": "completed",
                    "provider": outcome.provider,
                    "model": outcome.model,
                    "plan": outcome.plan,
                    "artifacts": outcome.artifacts,
                    "final": True,
                },
            )
        elif outcome.status == "failed":
            self.state_store.update_task(task_id, status="failed", error=outcome.message)
            self._publish_event(
                task_id,
                "task_failed",
                outcome.message,
                {"status": "failed", "error": outcome.message, "final": True},
            )
        return outcome

    async def _resume_task_from_approval(self, task_id: str, approval: dict[str, Any]) -> None:
        try:
            task = self.state_store.get_task(task_id)
            if not task:
                raise RuntimeError(f"Unknown task {task_id}")
            resume = approval.get("resume") or {}
            resume_state = resume.get("state")
            if not isinstance(resume_state, dict) or not resume_state:
                metadata = task.get("metadata") or {}
                resume_state = metadata.get("agent_state")
            if not isinstance(resume_state, dict) or not resume_state:
                raise RuntimeError(f"Approval {approval['id']} is missing resumable agent state")
            await self._run_native_agent_task(
                task_id,
                task["goal"],
                kind=str(task.get("kind") or "task"),
                resume_state=resume_state,
                approved=True,
            )
        finally:
            self.active_tasks.pop(task_id, None)

    async def _execute_task(self, task_id: str, goal: str) -> None:
        try:
            await self._run_native_agent_task(
                task_id,
                goal,
                kind="task",
                initial_tool_call=self._detect_fast_path_tool_call(goal),
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

    def _try_media_generation(self, prompt: str) -> dict[str, Any] | None:
        """
        Detect image/video generation requests and return a deterministic
        tool call so the agent loop can execute it with normal events.
        """
        lower = prompt.lower().strip()
        if "svg" in lower and re.search(r"\b(?:png|jpg|jpeg|webp|render|export|convert)\b", lower):
            return None

        # Match patterns like "generate an image of...", "create a picture of...",
        # "draw me a...", "make a photo of...", "can you generate an image..."
        media_patterns = [
            r'\b(?:generate|create|make|draw|paint|render|produce|design)\b.*\b(?:image|picture|photo|illustration|artwork|art|portrait|drawing|painting|graphic)\b',
            r'\b(?:image|picture|photo|illustration|artwork|portrait)\b.*\b(?:of|showing|depicting|with)\b',
            r'\b(?:generate|create|make|render|produce)\b.*\b(?:video|animation|clip|gif)\b',
            r'\b(?:i want|i need|i\'d like|can you|could you|please)\b.*\b(?:image|picture|photo|video)\b',
            r'\b(?:render|export|convert)\b.*\b(?:image|picture|photo|art|video)\b',
        ]

        is_media_request = any(re.search(pat, lower) for pat in media_patterns)
        if not is_media_request:
            return None

        # Determine media type
        is_video = bool(re.search(r'\b(?:video|animation|clip|gif|animate)\b', lower))
        media_type = "video" if is_video else "image"

        _log = logging.getLogger("kestrel.daemon")
        _log.info(f"Media generation request detected (type={media_type}): {prompt[:100]!r}")
        return {
            "tool_name": "generate_image",
            "arguments": {
                "prompt": prompt,
                "media_type": media_type,
                "send_to_telegram": _wants_telegram_delivery(prompt),
            },
        }

    def _try_screenshot_capture(self, prompt: str) -> dict[str, Any] | None:
        if "desktop" not in self._enabled_chat_tool_categories():
            return None
        if not _looks_like_screenshot_request(prompt):
            return None

        send_to_telegram = _wants_telegram_delivery(prompt)
        LOGGER.info(
            "Screenshot request detected (send_to_telegram=%s): %r",
            send_to_telegram,
            prompt[:100],
        )
        return {
            "tool_name": "take_screenshot",
            "arguments": {
                "send_to_telegram": send_to_telegram,
                "caption": "Kestrel screenshot",
            },
        }

    def _detect_fast_path_tool_call(self, prompt: str) -> dict[str, Any] | None:
        return self._try_screenshot_capture(prompt) or self._try_media_generation(prompt)

    def _build_system_prompt(self) -> str:
        profile = self._compose_runtime_profile()
        mounts = profile.get("host_mounts", [])
        mount_lines = []
        for mount in mounts:
            mount_lines.append(f"  - {mount.get('path', '/')} ({mount.get('mode', 'read-only')})")
        mounts_str = "\n".join(mount_lines) if mount_lines else "  - No host mounts configured"
        categories = self._enabled_chat_tool_categories()
        tool_lines = describe_chat_tool_categories(categories)

        return (
            "You are Kestrel, a local autonomous agent OS focused on concise, actionable assistance.\n\n"
            "## Runtime Environment\n"
            f"- Runtime mode: {profile.get('runtime_mode', 'native')}\n"
            f"- Policy: {profile.get('policy_name', 'unknown')}\n"
            f"- Host mounts:\n{mounts_str}\n"
            f"- Home directory: {Path.home()}\n\n"
            "## Tool Categories\n"
            f"{tool_lines}\n\n"
            "## Capabilities\n"
            "You have tools to interact with the local system. ALWAYS use the provided tools when asked to:\n"
            "- Create, read, or modify files\n"
            "- List directory contents\n"
            "- Execute shell commands\n"
            "- Generate media or capture the current screen when the user asks\n"
            "IMPORTANT: Never claim you performed an action without actually calling the appropriate tool.\n"
            "If you need to create a file, use create_file. If you need to run a command, use run_command. "
            "If you need the current desktop image, use take_screenshot.\n"
        )

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
            self._channel_state_path(),
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
