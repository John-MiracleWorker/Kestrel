from __future__ import annotations

import hashlib

from . import daemon_telegram_io as _daemon_telegram_io
from .local_operator_contracts import AgentProfile, AutonomyPolicy, VerifierResult
from .native_models import StructuredModelOutputError
from .native_persona import compose_native_system_prompt

globals().update({name: value for name, value in vars(_daemon_telegram_io).items() if not name.startswith("__")})


def _normalize_chat_history(history: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in list(history or [])[-12:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        normalized.append({"role": role, "content": content})
    return normalized


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
        seen: set[tuple[str, str, str]] = set()
        for artifact in self.state_store.list_artifact_manifests(task_id):
            if not isinstance(artifact, dict):
                continue
            key = self._artifact_key(artifact)
            if key in seen:
                continue
            seen.add(key)
            artifacts.append(dict(artifact))
        for event in events:
            artifact_url = event.get("artifact_url") or event.get("url")
            artifact_path = event.get("artifact_path") or event.get("path")
            if artifact_url or artifact_path:
                artifact = {
                    "task_id": task_id,
                    "type": event.get("artifact_type") or event.get("type") or "artifact",
                    "url": artifact_url or "",
                    "path": artifact_path or "",
                    "created_at": event.get("created_at") or "",
                }
                key = self._artifact_key(artifact)
                if key not in seen:
                    seen.add(key)
                    artifacts.append(artifact)
            for artifact in event.get("artifacts") or []:
                if isinstance(artifact, dict):
                    key = self._artifact_key(artifact)
                    if key in seen:
                        continue
                    seen.add(key)
                    artifacts.append(dict(artifact))
        return artifacts

    def _artifact_key(self, artifact: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(artifact.get("type") or artifact.get("artifact_type") or "artifact"),
            str(artifact.get("path") or ""),
            str(artifact.get("url") or ""),
        )

    def _autonomy_policy_for_workspace(self, workspace_id: str = "") -> dict[str, Any]:
        runtime_profile = self.runtime_policy.runtime_profile()
        return AutonomyPolicy.from_config(
            self._config_for_workspace(workspace_id),
            runtime_mode=str(runtime_profile.get("runtime_mode") or "native"),
        ).to_dict()

    def _media_capabilities(self) -> dict[str, Any]:
        categories = set(self._enabled_chat_tool_categories())
        media_server = bool(os.getenv("SWARMUI_BASE_URL") or os.getenv("MEDIA_HOST_URL"))
        vision_enabled = bool(os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY"))
        return {
            "image_generation": "media" in categories,
            "video_generation": "media" in categories and media_server,
            "svg_render": "media" in categories,
            "screenshot_capture": "desktop" in categories,
            "vision_analysis": vision_enabled,
            "artifact_delivery": True,
            "source_snapshots": True,
            "remote_media_server_configured": media_server,
        }

    def _automation_permissions(self) -> dict[str, Any]:
        permissions = self.config.get("permissions") or {}
        channels = self._read_channel_status().get("channels", {})
        return {
            "broad_local_control": bool(permissions.get("broad_local_control", True)),
            "require_approval_for_mutations": bool(
                permissions.get("require_approval_for_mutations", True)
            ),
            "telegram_delivery": bool((channels.get("telegram") or {}).get("configured")),
            "watched_files": True,
            "background_suggestions": True,
        }

    def _control_plane_summary(self, workspace_id: str = "") -> dict[str, Any]:
        return {
            "pending_approvals": len(self.state_store.list_pending_approvals()),
            "pending_suggestions": len(
                self.state_store.list_background_suggestions(
                    status="pending",
                    workspace_id=workspace_id or None,
                    limit=200,
                )
            ),
            "research_sessions": len(
                self.state_store.list_research_sessions(workspace_id=workspace_id or None, limit=200)
            ),
            "procedures": len(self.state_store.list_procedures(workspace_id=workspace_id or None, limit=200)),
            "learning_events": len(
                self.state_store.list_learning_events(workspace_id=workspace_id or None, limit=200)
            ),
        }

    def _record_learning_event(
        self,
        *,
        workspace_id: str,
        task_id: str,
        event_type: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.state_store.append_learning_event(
            event_id=str(uuid.uuid4()),
            workspace_id=workspace_id or "local",
            task_id=task_id,
            event_type=event_type,
            summary=summary,
            payload=payload or {},
        )

    def _research_notebook_path(self, session_id: str) -> Path:
        research_dir = self.paths.memory_dir / "research"
        research_dir.mkdir(parents=True, exist_ok=True)
        return research_dir / f"{session_id}.md"

    def _research_snapshot_dir(self, session_id: str) -> Path:
        snapshot_dir = self.paths.artifacts_dir / "research" / session_id
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        return snapshot_dir

    def _build_research_title(self, prompt: str) -> str:
        compact = " ".join(str(prompt or "").split()).strip()
        if not compact:
            return "Research session"
        return compact[:72]

    def _ensure_research_session(
        self,
        *,
        task_id: str,
        workspace_id: str,
        prompt: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing = self.state_store.get_research_session_for_task(task_id)
        if existing:
            return existing
        session_id = str(uuid.uuid4())
        notebook_path = self._research_notebook_path(session_id)
        notebook_path.write_text(
            "\n".join(
                [
                    f"# {self._build_research_title(prompt)}",
                    "",
                    "Status: queued",
                    "",
                    "## Prompt",
                    prompt.strip(),
                    "",
                    "## Summary",
                    "_Pending_",
                ]
            ).strip()
            + "\n",
            encoding="utf-8",
        )
        return self.state_store.create_research_session(
            session_id=session_id,
            workspace_id=workspace_id or "local",
            task_id=task_id,
            title=self._build_research_title(prompt),
            prompt=prompt,
            notebook_path=str(notebook_path),
            metadata=metadata or {},
        )

    def _collect_research_sources(
        self,
        session_id: str,
        outcome: NativeAgentOutcome,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        snapshot_dir = self._research_snapshot_dir(session_id)
        sources: list[dict[str, Any]] = []
        snapshot_artifacts: list[dict[str, Any]] = []
        evidence = list((outcome.state or {}).get("tool_evidence") or [])
        for index, item in enumerate(evidence, start=1):
            if not isinstance(item, dict):
                continue
            if str(item.get("tool_name") or "") != "fetch_url":
                continue
            data = item.get("data") or {}
            if not isinstance(data, dict):
                continue
            url = str(data.get("url") or "").strip()
            if not url:
                continue
            snapshot_path = ""
            body = str(data.get("body") or "").strip()
            if body:
                path = snapshot_dir / f"source-{index:02d}.txt"
                path.write_text(
                    "\n".join(
                        [
                            f"URL: {url}",
                            f"Content-Type: {str(data.get('content_type') or '')}",
                            f"Status: {str(data.get('status_code') or '')}",
                            "",
                            body,
                        ]
                    ).strip()
                    + "\n",
                    encoding="utf-8",
                )
                snapshot_path = str(path)
                snapshot_artifacts.append(
                    {
                        "type": "research_source_snapshot",
                        "path": snapshot_path,
                        "url": url,
                        "mime_type": "text/plain",
                    }
                )
            sources.append(
                {
                    "url": url,
                    "status_code": data.get("status_code"),
                    "content_type": data.get("content_type"),
                    "snapshot_path": snapshot_path,
                }
            )
        return sources, snapshot_artifacts

    def _sync_research_session(self, task: dict[str, Any], outcome: NativeAgentOutcome) -> list[dict[str, Any]]:
        metadata = dict(task.get("metadata") or {})
        workspace_id = str(metadata.get("workspace_id") or "local")
        session = self._ensure_research_session(
            task_id=str(task.get("id") or ""),
            workspace_id=workspace_id,
            prompt=str(task.get("goal") or ""),
            metadata={"source": metadata.get("source") or "task"},
        )
        session_id = str(session.get("id") or "")
        sources, source_artifacts = self._collect_research_sources(session_id, outcome)
        all_artifacts = list(outcome.artifacts or []) + source_artifacts
        notebook_path = Path(str(session.get("notebook_path") or self._research_notebook_path(session_id)))
        notebook_path.parent.mkdir(parents=True, exist_ok=True)
        notebook_lines = [
            f"# {session.get('title') or self._build_research_title(str(task.get('goal') or ''))}",
            "",
            f"Status: {outcome.status}",
            "",
            "## Prompt",
            str(task.get("goal") or "").strip(),
            "",
            "## Summary",
            str(outcome.message or "").strip() or "_No summary generated._",
        ]
        if sources:
            notebook_lines.extend(["", "## Sources"])
            for source in sources:
                line = f"- {source['url']}"
                if source.get("snapshot_path"):
                    line += f" ({source['snapshot_path']})"
                notebook_lines.append(line)
        if all_artifacts:
            notebook_lines.extend(["", "## Artifacts"])
            for artifact in all_artifacts:
                label = str(artifact.get("path") or artifact.get("url") or "").strip()
                if label:
                    notebook_lines.append(f"- {artifact.get('type') or 'artifact'}: {label}")
        notebook_path.write_text("\n".join(notebook_lines).strip() + "\n", encoding="utf-8")
        completed_at = _now() if outcome.status in {"completed", "failed"} else ""
        self.state_store.update_research_session(
            session_id,
            status=outcome.status,
            notebook_path=str(notebook_path),
            summary=str(outcome.message or ""),
            sources=sources,
            artifacts=all_artifacts,
            metadata={"provider": outcome.provider, "model": outcome.model},
            completed_at=completed_at,
        )
        return all_artifacts

    def _learn_procedure(self, task: dict[str, Any], outcome: NativeAgentOutcome, *, kind: str) -> dict[str, Any] | None:
        plan = outcome.plan if isinstance(outcome.plan, dict) else (outcome.state or {}).get("plan")
        if not isinstance(plan, dict):
            return None
        steps = [step for step in list(plan.get("steps") or []) if isinstance(step, dict)]
        if len(steps) < 2:
            return None
        workspace_id = str(((task.get("metadata") or {}).get("workspace_id")) or "local")
        digest = hashlib.sha1(
            json.dumps(
                {
                    "workspace_id": workspace_id,
                    "summary": plan.get("summary"),
                    "steps": [
                        {
                            "description": step.get("description"),
                            "preferred_tools": step.get("preferred_tools"),
                        }
                        for step in steps
                    ],
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        procedure = self.state_store.upsert_procedure(
            procedure_id=f"procedure-{digest}",
            workspace_id=workspace_id,
            name=str(plan.get("summary") or task.get("goal") or "Learned procedure")[:96],
            description=f"Learned from a successful {kind} task.",
            trigger_text=str(task.get("goal") or ""),
            steps=[
                {
                    "description": str(step.get("description") or ""),
                    "success_criteria": str(step.get("success_criteria") or ""),
                    "preferred_tools": list(step.get("preferred_tools") or []),
                }
                for step in steps
            ],
            source_task_id=str(task.get("id") or ""),
            enabled=True,
            confidence=min(0.95, 0.5 + (0.1 * len(steps))),
            metadata={"kind": kind, "provider": outcome.provider, "model": outcome.model},
        )
        self._record_learning_event(
            workspace_id=workspace_id,
            task_id=str(task.get("id") or ""),
            event_type="procedure_learned",
            summary=f"Learned procedure: {procedure.get('name') or 'procedure'}",
            payload={"procedure_id": procedure.get("id"), "step_count": len(steps)},
        )
        return procedure

    def _verifier_result_from_outcome(self, outcome: NativeAgentOutcome) -> dict[str, Any]:
        verifier = dict((outcome.state or {}).get("verifier_result") or {})
        citations = []
        for item in list((outcome.state or {}).get("tool_evidence") or []):
            if not isinstance(item, dict):
                continue
            data = item.get("data") or {}
            if isinstance(data, dict):
                url = str(data.get("url") or "").strip()
                if url:
                    citations.append(url)
        result = VerifierResult(
            ok=bool(verifier.get("ok", outcome.status == "completed")),
            final_response=str(verifier.get("final_response") or outcome.message or ""),
            reason=str(verifier.get("reason") or ""),
            evidence_count=len(list((outcome.state or {}).get("tool_evidence") or [])),
            citations=citations[:8],
        )
        return result.to_dict()

    def _finalize_local_control_plane(
        self,
        *,
        task_id: str,
        kind: str,
        outcome: NativeAgentOutcome,
    ) -> None:
        task = self.state_store.get_task(task_id)
        if not task:
            return
        workspace_id = str(((task.get("metadata") or {}).get("workspace_id")) or "local")
        artifacts = list(outcome.artifacts or [])
        if kind == "research":
            artifacts = self._sync_research_session(task, outcome)
        self.state_store.record_artifact_manifests(task_id, artifacts)
        verifier_result = self._verifier_result_from_outcome(outcome)
        self._record_learning_event(
            workspace_id=workspace_id,
            task_id=task_id,
            event_type=f"task_{outcome.status}",
            summary=str(outcome.message or task.get("goal") or "").strip()[:200],
            payload={
                "kind": kind,
                "provider": outcome.provider,
                "model": outcome.model,
                "artifact_count": len(artifacts),
                "verifier_result": verifier_result,
            },
        )
        if outcome.status == "completed":
            self._learn_procedure(task, outcome, kind=kind)

    def _compose_runtime_profile(self) -> dict[str, Any]:
        profile = self.runtime_policy.runtime_profile()
        workspace_id = self._resolve_workspace_id()
        profile["local_models"] = self.last_model_runtime
        profile["autonomy_policy"] = self._autonomy_policy_for_workspace(workspace_id)
        profile["media_capabilities"] = self._media_capabilities()
        profile["automation_permissions"] = self._automation_permissions()
        profile["control_plane"] = self._control_plane_summary(workspace_id)
        if getattr(self, "skill_pack_manager", None):
            catalog = self.skill_pack_manager.catalog(include_synthetic=False)
            profile["skill_packs"] = {
                "snapshot_id": catalog.get("snapshot_id"),
                "count": len(catalog.get("packs") or []),
            }
        profile["agent_profile"] = AgentProfile(
            profile_id=f"local-{workspace_id or 'workspace'}",
            workspace_id=workspace_id or "local",
            runtime_mode=str(profile.get("runtime_mode") or "native"),
            autonomy_policy=dict(profile.get("autonomy_policy") or {}),
            local_models=dict(self.last_model_runtime or {}),
            media_capabilities=dict(profile.get("media_capabilities") or {}),
            automation_permissions=dict(profile.get("automation_permissions") or {}),
            control_plane=dict(profile.get("control_plane") or {}),
        ).to_dict()
        profile["updated_at"] = _now()
        return profile

    def _compose_status(self) -> dict[str, Any]:
        tasks = self.state_store.list_tasks(limit=10)
        uptime = int(time.time() - self.start_time)
        recent_background_tasks = [task for task in tasks if self._is_background_task(task)][:3]
        workspace_id = self._resolve_workspace_id()
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
            "last_heartbeat_action": self.last_heartbeat_action,
            "recent_watched_changes": [self._relative_path_label(path) for path in self.recent_watched_changes[:5]],
            "recent_background_tasks": recent_background_tasks,
            "background_suggestions": self.state_store.list_background_suggestions(
                status="pending",
                workspace_id=workspace_id or None,
                limit=10,
            ),
            "research_sessions": self.state_store.list_research_sessions(
                workspace_id=workspace_id or None,
                limit=5,
            ),
            "procedures": self.state_store.list_procedures(
                workspace_id=workspace_id or None,
                limit=5,
            ),
            "learning_events": self.state_store.list_learning_events(
                workspace_id=workspace_id or None,
                limit=10,
            ),
            "home": str(self.paths.home),
        }

    def _write_state(self, *, status: str, changed_paths: list[str] | None = None) -> None:
        payload = {
            "status": status,
            "started_at": self.start_time,
            "uptime": time.time() - self.start_time,
            "last_heartbeat": time.time(),
            "next_heartbeat": time.time() + self._heartbeat_interval_seconds(),
            "changed_paths": changed_paths or [],
            "recent_tasks": [task["id"] for task in self.state_store.list_tasks(limit=5)],
            "pending_approvals": len(self.state_store.list_pending_approvals()),
            "memory_sync": self.last_memory_sync,
            "control_socket": str(self.paths.control_socket),
            "control_endpoint": self._control_endpoint(),
            "last_heartbeat_action": self.last_heartbeat_action,
            "recent_watched_changes": [self._relative_path_label(path) for path in self.recent_watched_changes[:5]],
            "pending_suggestions": len(
                self.state_store.list_background_suggestions(status="pending", limit=200)
            ),
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
        task_record = self.state_store.get_task(task_id) if task_id else None
        workspace_id = str(((task_record or {}).get("metadata") or {}).get("workspace_id") or "local")
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

        try:
            outcome = await self._build_agent_runner(task_id=task_id, workspace_id=workspace_id).run(
                goal=runner_goal,
                history=history or [],
                task_id=task_id,
                task_kind=kind,
                initial_tool_call=initial_tool_call,
                resume_state=resume_state,
                approved=approved,
            )
        except StructuredModelOutputError as exc:
            outcome = NativeAgentOutcome(
                status="failed",
                message=str(exc),
                provider="",
                model="",
                plan=None,
                artifacts=[],
            )
        if outcome.status == "completed":
            self._finalize_local_control_plane(task_id=task_id, kind=kind, outcome=outcome)
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
            await self._notify_background_task_status(
                self.state_store.get_task(task_id),
                status="completed",
                message=outcome.message,
            )
        elif outcome.status == "failed":
            self.state_store.update_task(task_id, status="failed", error=outcome.message)
            self._finalize_local_control_plane(task_id=task_id, kind=kind, outcome=outcome)
            self._publish_event(
                task_id,
                "task_failed",
                outcome.message,
                {"status": "failed", "error": outcome.message, "final": True},
            )
            await self._notify_background_task_status(
                self.state_store.get_task(task_id),
                status="failed",
                message=outcome.message,
            )
        elif outcome.status == "waiting_approval":
            self._finalize_local_control_plane(task_id=task_id, kind=kind, outcome=outcome)
            await self._notify_background_task_status(
                self.state_store.get_task(task_id),
                status="waiting_approval",
                message=outcome.message,
                approval=outcome.approval,
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

    async def _execute_task(
        self,
        task_id: str,
        goal: str,
        *,
        kind: str = "task",
        history: list[dict[str, Any]] | None = None,
    ) -> None:
        try:
            await self._run_native_agent_task(
                task_id,
                goal,
                kind=kind,
                history=_normalize_chat_history(history),
                initial_tool_call=self._detect_fast_path_tool_call(goal),
            )
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            self.state_store.update_task(task_id, status="cancelled", error="Task cancelled")
            self._publish_event(task_id, "task_cancelled", "Task cancelled", {"final": True, "status": "cancelled"})
            await self._notify_background_task_status(
                self.state_store.get_task(task_id),
                status="failed",
                message="Task cancelled",
            )
            raise
        except Exception as exc:
            self.state_store.update_task(task_id, status="failed", error=str(exc))
            self._publish_event(
                task_id,
                "task_failed",
                str(exc),
                {"status": "failed", "error": str(exc), "final": True},
            )
            await self._notify_background_task_status(
                self.state_store.get_task(task_id),
                status="failed",
                message=str(exc),
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
            r'\b(?:i want|i need|i\'d like)\b.*\b(?:image|picture|photo|video)\b',
            r'\b(?:can you|could you|please)\b.*\b(?:generate|create|make|draw|paint|render|produce)\b.*\b(?:image|picture|photo|video|illustration|artwork|art|portrait|drawing|painting|graphic)\b',
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

    def _try_send_local_file_to_telegram(self, prompt: str) -> dict[str, Any] | None:
        if "file" not in self._enabled_chat_tool_categories():
            return None

        request = _resolve_local_file_telegram_request(prompt)
        if not request:
            return None

        resolved_path = Path(str(request.get("resolved_path") or "")).expanduser()
        if not resolved_path.exists() or not resolved_path.is_file():
            return None

        LOGGER.info(
            "Local file Telegram delivery request detected for %s: %r",
            resolved_path,
            prompt[:100],
        )
        return {
            "tool_name": "send_local_file_to_telegram",
            "arguments": {
                "path": str(resolved_path),
                "caption": resolved_path.name,
                "send_to_telegram": True,
                "requested_name": str(request.get("requested_name") or "").strip(),
            },
        }

    def _try_skill_pack_list(self, prompt: str) -> dict[str, Any] | None:
        categories = set(self._enabled_chat_tool_categories())
        if "custom" not in categories or getattr(self, "skill_pack_manager", None) is None:
            return None

        lowered = str(prompt or "").strip().lower()
        if "skill pack" not in lowered and "skill packs" not in lowered:
            return None

        if not any(token in lowered for token in ("list", "show", "what", "available", "installed", "marketplace", "catalog")):
            return None

        LOGGER.info("Skill pack catalog request detected: %r", prompt[:100])
        return {
            "tool_name": "skill_list",
            "arguments": {
                "include_synthetic": True,
                "include_marketplace": True,
            },
        }

    def _detect_fast_path_tool_call(self, prompt: str) -> dict[str, Any] | None:
        return (
            self._try_send_local_file_to_telegram(prompt)
            or
            self._try_screenshot_capture(prompt)
            or self._try_media_generation(prompt)
            or self._try_skill_pack_list(prompt)
        )

    def _build_system_prompt(self) -> str:
        profile = self._compose_runtime_profile()
        mounts = profile.get("host_mounts", [])
        mount_lines = []
        for mount in mounts:
            mount_lines.append(f"  - {mount.get('path', '/')} ({mount.get('mode', 'read-only')})")
        mounts_str = "\n".join(mount_lines) if mount_lines else "  - No host mounts configured"
        categories = self._enabled_chat_tool_categories()
        tool_lines = describe_chat_tool_categories(categories)

        return compose_native_system_prompt(
            config=self._config_for_workspace(self._resolve_workspace_id()),
            role="assistant",
            ambient_state=self._ambient_state_for_workspace(self._resolve_workspace_id()),
            workspace_system_prompt=self._workspace_system_prompt(self._resolve_workspace_id()),
            role_instructions=(
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
                "- Send an existing local file to Telegram when the user explicitly asks for Telegram delivery\n"
                "- Generate media or capture the current screen when the user asks\n"
                "IMPORTANT: Never claim you performed an action without actually calling the appropriate tool.\n"
                "If you need to create a file, use create_file. If you need to run a command, use run_command. "
                "If you need the current desktop image, use take_screenshot. "
                "If you need to send a local file to Telegram, use send_local_file_to_telegram.\n"
            ),
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
        if not isinstance(quiet, dict):
            return False
        enabled = quiet.get("enabled")
        start = str(quiet.get("start") or "").strip()
        end = str(quiet.get("end") or "").strip()
        has_window = bool(start and end)
        if not bool(enabled) and not (enabled is None and has_window):
            return False
        if not has_window:
            return False
        now = datetime_now_hhmm()
        if start <= end:
            return start <= now <= end
        return now >= start or now <= end
