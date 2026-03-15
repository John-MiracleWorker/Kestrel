from __future__ import annotations

from . import daemon_telegram_state as _daemon_telegram_state

globals().update({name: value for name, value in vars(_daemon_telegram_state).items() if not name.startswith("__")})

class KestrelDaemonTelegramIOMixin:
    async def _handle_telegram_command(self, message: dict[str, Any]) -> None:
        chat_id = str(message.get("chat_id") or "")
        message_id = int(message.get("message_id") or 0) or None
        raw_text = str(message.get("text") or "").strip()
        command_parts = raw_text.split(maxsplit=1)
        command = command_parts[0].split("@", 1)[0].lower()
        argument = command_parts[1].strip() if len(command_parts) > 1 else ""

        if command in {"/start", "/help"}:
            await self._telegram_send_text(
                chat_id,
                "\n".join(
                    [
                        "Kestrel is live on Telegram and watching the workspace.",
                        "Send a prompt and I'll run it here first.",
                        "Reply to one of my messages to keep the same thread context.",
                        "Attach a file or image and I'll save it locally before using it.",
                        "/status shows runtime health and the latest heartbeat move.",
                        "/approvals lists pending approvals.",
                        "/approve <id> approves a pending action.",
                        "/deny <id> denies a pending action.",
                    ]
                ),
                reply_to_message_id=message_id,
            )
            return

        if command == "/status":
            runtime = self.state_store.get_runtime_profile() or self._compose_runtime_profile()
            local_models = runtime.get("local_models", {})
            pending = self.state_store.list_pending_approvals()
            await self._telegram_send_text(
                chat_id,
                "\n".join(
                    [
                        "Kestrel daemon is running.",
                        f"Primary channel: {'telegram' if self._telegram_is_primary_channel() else 'not telegram'}",
                        f"Provider: {local_models.get('default_provider', 'unknown')}",
                        f"Model: {local_models.get('default_model', 'unknown')}",
                        f"Pending approvals: {len(pending)}",
                        f"Last heartbeat: {self.last_heartbeat_action}",
                    ]
                ),
                reply_to_message_id=message_id,
            )
            return

        if command == "/approvals":
            approvals = self.state_store.list_pending_approvals()
            if not approvals:
                text = "No pending approvals."
            else:
                lines = [f"Pending approvals: {len(approvals)}"]
                for approval in approvals[:10]:
                    lines.append("")
                    lines.extend(self._format_telegram_approval_lines(approval))
                text = "\n".join(lines)
            await self._telegram_send_text(chat_id, text, reply_to_message_id=message_id)
            return

        if command in {"/approve", "/deny"}:
            approval_token = argument.strip()
            if not approval_token:
                await self._telegram_send_text(
                    chat_id,
                    f"Usage: {command} <approval_id_or_prefix>",
                    reply_to_message_id=message_id,
                )
                return
            approved = command == "/approve"
            try:
                approval = self._resolve_pending_approval(approval_token)
            except Exception as exc:
                await self._telegram_send_text(
                    chat_id,
                    f"Approval command failed: {exc}",
                    reply_to_message_id=message_id,
                )
                return

            await self._telegram_finish_approval_resolution(
                chat_id,
                approval,
                approved=approved,
                reply_to_message_id=message_id,
            )
            return

        await self._telegram_send_text(
            chat_id,
            "Unknown command. Use /help for the supported Telegram controls.",
            reply_to_message_id=message_id,
        )

    async def _process_telegram_callback(self, callback: dict[str, Any]) -> None:
        chat_id = str(callback.get("chat_id") or "").strip()
        if not chat_id or not self._telegram_chat_allowed(chat_id):
            return

        data = str(callback.get("data") or "").strip()
        callback_id = str(callback.get("callback_id") or "").strip()
        message_id = int(callback.get("message_id") or 0) or None
        if not data.startswith("approval:"):
            await self._telegram_answer_callback_query(callback_id, "Unsupported action.")
            return

        parts = data.split(":", 2)
        if len(parts) != 3 or parts[1] not in {"approve", "deny"}:
            await self._telegram_answer_callback_query(callback_id, "Unsupported approval action.")
            return

        approved = parts[1] == "approve"
        approval_token = parts[2]
        try:
            approval = self._resolve_pending_approval(approval_token)
        except Exception as exc:
            await self._telegram_answer_callback_query(callback_id, f"Approval lookup failed: {exc}")
            return

        await self._telegram_answer_callback_query(
            callback_id,
            "Approving..." if approved else "Denying...",
        )
        await self._telegram_finish_approval_resolution(
            chat_id,
            approval,
            approved=approved,
            reply_to_message_id=message_id,
            callback_message_id=message_id,
        )

    async def _handle_telegram_chat(self, message: dict[str, Any]) -> None:
        chat_id = str(message.get("chat_id") or "")
        message_id = int(message.get("message_id") or 0) or None
        runtime = self._telegram_runtime()
        downloaded_attachments, attachment_errors = await self._download_telegram_attachments(message)
        prompt = self._build_telegram_goal(message, downloaded_attachments, attachment_errors)
        history = self._build_telegram_history(message)
        desktop_save_request = self._telegram_desktop_save_fast_path(
            chat_id=chat_id,
            message=message,
            downloaded_attachments=downloaded_attachments,
        )
        local_file_delivery_request = self._telegram_local_file_delivery_fast_path(message)
        image_attachments = self._telegram_image_attachments(downloaded_attachments)
        image_prompt = self._build_telegram_image_prompt(message, image_attachments, attachment_errors)

        metadata = {
            "workspace_id": runtime.get("workspace_id") or "default",
            "source": "telegram",
            "telegram_chat_id": chat_id,
            "telegram_user_id": str(message.get("from_id") or ""),
            "telegram_message_id": message_id or 0,
            "telegram_original_text": str(message.get("text") or ""),
            "telegram_attachments": downloaded_attachments,
            "telegram_attachment_errors": attachment_errors,
            "telegram_reply": dict(message.get("reply") or {}),
            "telegram_primary_channel": self._telegram_is_primary_channel(),
        }
        if desktop_save_request and isinstance(desktop_save_request.get("attachment"), dict):
            metadata["telegram_resolved_attachment"] = dict(desktop_save_request["attachment"])
        if local_file_delivery_request:
            requested_path = str(local_file_delivery_request.get("requested_path") or "").strip()
            resolved_path = str(local_file_delivery_request.get("resolved_path") or "").strip()
            suggestions = list(local_file_delivery_request.get("suggestions") or [])
            if requested_path:
                metadata["telegram_requested_local_file"] = requested_path
            if resolved_path:
                metadata["telegram_resolved_local_file"] = resolved_path
            if suggestions:
                metadata["telegram_local_file_suggestions"] = suggestions

        task = self.state_store.create_task(goal=prompt, kind="chat", metadata=metadata)
        if local_file_delivery_request and local_file_delivery_request.get("missing_message"):
            missing_message = str(local_file_delivery_request.get("missing_message") or "").strip()
            self.state_store.update_task(
                task["id"],
                status="completed",
                result={
                    "message": missing_message,
                    "provider": "",
                    "model": "",
                    "plan": None,
                    "artifacts": [],
                },
            )
            self._publish_event(
                task["id"],
                "task_complete",
                missing_message,
                {
                    "status": "completed",
                    "provider": "",
                    "model": "",
                    "plan": None,
                    "artifacts": [],
                    "final": True,
                },
            )
            await self._telegram_update_task_status(
                task["id"],
                chat_id,
                "Done",
                reply_to_message_id=message_id,
            )
            await self._telegram_send_text(
                chat_id,
                missing_message,
                reply_to_message_id=message_id,
            )
            return
        if image_attachments and not desktop_save_request:
            routed_image_request = None
            try:
                routed_image_request = await route_local_image_request(
                    prompt=image_prompt,
                    image_paths=[str(item.get("path") or "") for item in image_attachments],
                    config=self.config,
                    history=history,
                )
            except Exception as exc:
                LOGGER.warning("Telegram image routing failed for task %s: %s", task["id"], exc)

            route_action = str((routed_image_request or {}).get("action") or "").strip().lower()
            if route_action == "generate_image":
                generation_prompt = str((routed_image_request or {}).get("generation_prompt") or "").strip()
                media_type = str((routed_image_request or {}).get("media_type") or "image").strip().lower() or "image"
                source_image_path = str(image_attachments[0].get("path") or "").strip()
                if media_type not in {"image", "video"}:
                    media_type = "image"
                initial_tool_call = {
                    "tool_name": "generate_image",
                    "arguments": {
                        "prompt": generation_prompt or image_prompt,
                        "negative_prompt": str((routed_image_request or {}).get("negative_prompt") or "").strip(),
                        "media_type": media_type,
                        "source_image_path": source_image_path,
                        "init_image_creativity": float((routed_image_request or {}).get("init_image_creativity") or 0.6),
                        "send_to_telegram": False,
                    },
                }
                self.state_store.update_task(
                    task["id"],
                    metadata={
                        "telegram_multimodal_route": True,
                        "telegram_multimodal_action": "generate_image",
                        "telegram_multimodal_model": str((routed_image_request or {}).get("model") or ""),
                        "telegram_multimodal_provider": str((routed_image_request or {}).get("provider") or ""),
                        "telegram_image_paths": [str(item.get("path") or "") for item in image_attachments],
                        "telegram_generation_prompt": generation_prompt,
                        "telegram_source_image_path": source_image_path,
                        "telegram_init_image_creativity": float((routed_image_request or {}).get("init_image_creativity") or 0.6),
                    },
                )
                await self._telegram_update_task_status(
                    task["id"],
                    chat_id,
                    "Generating image",
                    reply_to_message_id=message_id,
                )
                try:
                    outcome = await self._run_native_agent_task(
                        task["id"],
                        prompt,
                        kind="chat",
                        history=history,
                        initial_tool_call=initial_tool_call,
                    )
                except Exception:
                    await self._telegram_update_task_status(task["id"], chat_id, "Error", reply_to_message_id=message_id)
                    raise
                await self._telegram_respond_with_outcome(chat_id, task["id"], outcome, reply_to_message_id=message_id)
                return

            await self._telegram_update_task_status(
                task["id"],
                chat_id,
                "Analyzing image",
                reply_to_message_id=message_id,
            )
            outcome = await self._run_telegram_multimodal_image_task(
                task["id"],
                prompt=image_prompt,
                history=history,
                image_attachments=image_attachments,
                prefetched_response=routed_image_request if route_action == "respond" else None,
            )
            await self._telegram_respond_with_outcome(chat_id, task["id"], outcome, reply_to_message_id=message_id)
            return

        initial_tool_call = None
        if desktop_save_request:
            initial_tool_call = dict(desktop_save_request.get("initial_tool_call") or {})
        if not initial_tool_call and local_file_delivery_request:
            initial_tool_call = dict(local_file_delivery_request.get("initial_tool_call") or {})
        if not initial_tool_call:
            initial_tool_call = self._detect_fast_path_tool_call(prompt)
        if initial_tool_call and str(initial_tool_call.get("tool_name") or "") in {
            "take_screenshot",
            "generate_image",
            "send_local_file_to_telegram",
        }:
            arguments = dict(initial_tool_call.get("arguments") or {})
            arguments["send_to_telegram"] = False
            initial_tool_call["arguments"] = arguments

        await self._telegram_update_task_status(
            task["id"],
            chat_id,
            self._telegram_initial_status(prompt, initial_tool_call),
            reply_to_message_id=message_id,
        )
        try:
            outcome = await self._run_native_agent_task(
                task["id"],
                prompt,
                kind="chat",
                history=history,
                initial_tool_call=initial_tool_call,
            )
        except Exception:
            await self._telegram_update_task_status(task["id"], chat_id, "Error", reply_to_message_id=message_id)
            raise
        await self._telegram_respond_with_outcome(chat_id, task["id"], outcome, reply_to_message_id=message_id)

    async def _run_telegram_multimodal_image_task(
        self,
        task_id: str,
        *,
        prompt: str,
        history: list[dict[str, Any]],
        image_attachments: list[dict[str, Any]],
        prefetched_response: dict[str, Any] | None = None,
    ) -> NativeAgentOutcome:
        self.state_store.update_task(
            task_id,
            status="running",
            metadata={
                "kind": "chat",
                "telegram_multimodal_route": True,
                "telegram_multimodal_action": "respond",
                "telegram_multimodal_model": str((prefetched_response or {}).get("model") or ""),
                "telegram_multimodal_provider": str((prefetched_response or {}).get("provider") or ""),
                "telegram_image_paths": [str(item.get("path") or "") for item in image_attachments],
            },
        )
        self._publish_event(task_id, "task_started", f"Started: {prompt}", {"status": "running"})

        try:
            response = dict(prefetched_response or {})
            final_message = str(response.get("reply_text") or response.get("content") or "").strip()
            provider = str(response.get("provider") or "lmstudio")
            model = str(response.get("model") or "")
            if not final_message:
                response = await inspect_local_images(
                    prompt=prompt,
                    image_paths=[str(item.get("path") or "") for item in image_attachments],
                    config=self.config,
                    history=history,
                )
                final_message = str(response.get("content") or "").strip()
                provider = str(response.get("provider") or "lmstudio")
                model = str(response.get("model") or "")
            result = {
                "message": final_message,
                "provider": provider,
                "model": model,
                "plan": None,
                "artifacts": [],
            }
            self.state_store.update_task(
                task_id,
                status="completed",
                result=result,
                metadata={
                    "telegram_multimodal_route": True,
                    "telegram_image_count": len(image_attachments),
                },
            )
            self._publish_event(
                task_id,
                "task_complete",
                final_message,
                {
                    "status": "completed",
                    "provider": provider,
                    "model": model,
                    "plan": None,
                    "artifacts": [],
                    "final": True,
                },
            )
            return NativeAgentOutcome(
                status="completed",
                message=final_message,
                provider=provider,
                model=model,
                plan=None,
                artifacts=[],
                state={"telegram_multimodal_route": True},
            )
        except Exception as exc:
            error_text = f"Couldn't inspect that image locally: {exc}"
            self.state_store.update_task(
                task_id,
                status="failed",
                error=error_text,
                metadata={"telegram_multimodal_route": True},
            )
            self._publish_event(
                task_id,
                "task_failed",
                error_text,
                {"status": "failed", "error": error_text, "final": True},
            )
            return NativeAgentOutcome(
                status="failed",
                message=error_text,
                provider="",
                model="",
                plan=None,
                artifacts=[],
                state={"telegram_multimodal_route": True},
            )

    async def _telegram_respond_with_outcome(
        self,
        chat_id: str,
        task_id: str,
        outcome: Any,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        if outcome.status == "waiting_approval":
            approval = outcome.approval or {}
            approval_id = str(approval.get("id") or "").strip()
            short_id = self._short_telegram_approval_id(approval_id)
            task = self.state_store.get_task(task_id) or {}
            await self._telegram_update_task_status(task_id, chat_id, "Waiting for approval", reply_to_message_id=reply_to_message_id)
            lines = ["Approval needed."]
            if task.get("goal"):
                lines.append(f"Task: {self._truncate_telegram_line(str(task.get('goal') or ''), 160)}")
            lines.append(
                f"Action: {self._truncate_telegram_line(str(outcome.message or approval.get('summary') or approval.get('command') or ''), 180)}"
            )
            if approval_id:
                lines.append(f"Approve: /approve {short_id}")
                lines.append(f"Deny: /deny {short_id}")
            await self._telegram_send_message(
                chat_id,
                "\n".join(lines),
                reply_to_message_id=reply_to_message_id,
                reply_markup=self._telegram_approval_reply_markup(approval_id),
            )
            return

        await self._telegram_send_task_result(chat_id, task_id, reply_to_message_id=reply_to_message_id)

    async def _telegram_send_delayed_working_note(
        self,
        chat_id: str,
        task_id: str,
        *,
        reply_to_message_id: int | None = None,
        delay_seconds: float = _TELEGRAM_WORKING_DELAY_SECONDS,
    ) -> None:
        try:
            await asyncio.sleep(max(delay_seconds, 0))
            task = self.state_store.get_task(task_id) or {}
            if _is_terminal_status(str(task.get("status") or "")):
                return
            await self._telegram_send_text(
                chat_id,
                "Working...",
                reply_to_message_id=reply_to_message_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.debug("Telegram delayed working note failed for task %s: %s", task_id, exc)

    async def _telegram_send_task_result(
        self,
        chat_id: str,
        task_id: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        task = self.state_store.get_task(task_id) or {}
        result = task.get("result") or {}
        status = str(task.get("status") or "")
        message = str(result.get("message") or task.get("error") or "").strip()
        artifacts = result.get("artifacts") or self._list_task_artifacts(task_id)
        if status == "completed":
            await self._telegram_update_task_status(task_id, chat_id, "Uploading", reply_to_message_id=reply_to_message_id)
        await self._telegram_send_artifacts(chat_id, artifacts, reply_to_message_id=reply_to_message_id)
        reply_text = self._telegram_result_text(status, message, artifacts)
        if reply_text:
            message_ids: list[int] = []
            for chunk in self._split_telegram_text(reply_text):
                response = await self._telegram_send_message(
                    chat_id,
                    chunk,
                    reply_to_message_id=reply_to_message_id,
                )
                message_id = int(response.get("message_id") or 0) or None
                if message_id:
                    message_ids.append(message_id)
            if message_ids:
                existing_ids = [
                    int(item)
                    for item in list((task.get("metadata") or {}).get("telegram_result_message_ids") or [])
                    if str(item).strip().isdigit()
                ]
                merged_ids = list(dict.fromkeys(existing_ids + message_ids))
                self.state_store.update_task(
                    task_id,
                    metadata={
                        "telegram_result_message_ids": merged_ids,
                        "telegram_last_reply_text": reply_text,
                    },
                )
        await self._telegram_update_task_status(
            task_id,
            chat_id,
            "Done" if status == "completed" else "Error",
            reply_to_message_id=reply_to_message_id,
        )

    async def _telegram_send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        if reply_markup:
            payload["reply_markup"] = reply_markup
        result = await self._telegram_api_request("sendMessage", payload)
        return result if isinstance(result, dict) else {}

    async def _telegram_edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            await self._telegram_api_request("editMessageText", payload)
            return True
        except Exception as exc:
            LOGGER.debug("Telegram editMessageText failed for %s/%s: %s", chat_id, message_id, exc)
            return False

    async def _telegram_answer_callback_query(self, callback_id: str, text: str = "") -> None:
        if not callback_id:
            return
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = self._truncate_telegram_line(text, 180)
        with contextlib.suppress(Exception):
            await self._telegram_api_request("answerCallbackQuery", payload)

    async def _telegram_update_task_status(
        self,
        task_id: str,
        chat_id: str,
        status_text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        try:
            task = self.state_store.get_task(task_id) or {}
            metadata = dict(task.get("metadata") or {})
            existing_message_id = int(metadata.get("telegram_status_message_id") or 0) or None
            if existing_message_id and await self._telegram_edit_message_text(chat_id, existing_message_id, status_text):
                self.state_store.update_task(
                    task_id,
                    metadata={
                        "telegram_status_text": status_text,
                        "telegram_status_updated_at": _now(),
                    },
                )
                return

            result = await self._telegram_send_message(
                chat_id,
                status_text,
                reply_to_message_id=reply_to_message_id,
            )
            message_id = int(result.get("message_id") or 0) or None
            if message_id:
                self.state_store.update_task(
                    task_id,
                    metadata={
                        "telegram_status_message_id": message_id,
                        "telegram_status_text": status_text,
                        "telegram_status_updated_at": _now(),
                    },
                )
        except Exception as exc:
            LOGGER.debug("Telegram status update failed for task %s: %s", task_id, exc)

    async def _telegram_send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        for chunk in self._split_telegram_text(text):
            await self._telegram_send_message(
                chat_id,
                chunk,
                reply_to_message_id=reply_to_message_id,
            )

    async def _telegram_send_chat_action(self, chat_id: str, action: str) -> None:
        try:
            await self._telegram_api_request(
                "sendChatAction",
                {
                    "chat_id": chat_id,
                    "action": action,
                },
            )
        except Exception as exc:
            LOGGER.debug("Telegram chat action failed for %s: %s", chat_id, exc)

    async def _telegram_send_artifacts(
        self,
        chat_id: str,
        artifacts: list[dict[str, Any]] | None,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        seen: set[str] = set()
        for artifact in artifacts or []:
            if not isinstance(artifact, dict):
                continue
            raw_path = artifact.get("path")
            if not raw_path:
                continue
            path = Path(str(raw_path)).expanduser()
            if not path.exists() or str(path) in seen:
                continue
            seen.add(str(path))
            try:
                await self._telegram_send_file(chat_id, path, reply_to_message_id=reply_to_message_id)
            except Exception as exc:
                LOGGER.warning("Failed to send Telegram artifact %s: %s", path, exc)

    async def _telegram_send_file(
        self,
        chat_id: str,
        file_path: Path,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        mime_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        lower_suffix = file_path.suffix.lower()
        if lower_suffix in {".mp4", ".webm", ".mov"} or mime_type.startswith("video/"):
            method = "sendVideo"
            field_name = "video"
        elif lower_suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"} or mime_type.startswith("image/"):
            method = "sendPhoto"
            field_name = "photo"
        else:
            method = "sendDocument"
            field_name = "document"

        payload: dict[str, Any] = {"chat_id": chat_id}
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id
        await self._telegram_api_request(
            method,
            payload,
            file_path=file_path,
            file_field=field_name,
            mime_type=mime_type,
        )

    async def _download_telegram_attachments(self, message: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        downloaded: list[dict[str, Any]] = []
        errors: list[str] = []
        for index, attachment in enumerate(message.get("attachments") or [], start=1):
            try:
                downloaded.append(await self._download_telegram_attachment(message, attachment, index))
            except Exception as exc:
                error_text = f"{attachment.get('type', 'attachment')}: {exc}"
                errors.append(error_text)
                LOGGER.warning("Telegram attachment download failed for chat %s message %s: %s", message.get("chat_id"), message.get("message_id"), exc)
        return downloaded, errors

    async def _download_telegram_attachment(
        self,
        message: dict[str, Any],
        attachment: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        file_id = str(attachment.get("file_id") or "").strip()
        if not file_id:
            raise RuntimeError("missing file_id")
        file_info = await self._telegram_api_request("getFile", {"file_id": file_id})
        remote_file_path = str((file_info or {}).get("file_path") or "").strip()
        if not remote_file_path:
            raise RuntimeError("Telegram getFile returned no file_path")

        base_name = str(attachment.get("file_name") or Path(remote_file_path).name or "").strip()
        filename = self._build_telegram_attachment_filename(
            attachment,
            message_id=int(message.get("message_id") or 0),
            index=index,
            remote_path=remote_file_path,
            base_name=base_name,
        )
        output_dir = self.paths.artifacts_dir / "telegram" / str(message.get("chat_id") or "unknown")
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._deduplicate_telegram_path(output_dir / filename)
        await asyncio.to_thread(self._telegram_download_file_sync, remote_file_path, output_path)
        return {
            "type": "telegram_attachment",
            "telegram_type": str(attachment.get("type") or "attachment"),
            "mime_type": str(attachment.get("mime_type") or "").strip(),
            "path": str(output_path),
            "source": "telegram",
            "size": int(attachment.get("size") or 0),
            "message_id": int(message.get("message_id") or 0),
        }

    def _telegram_download_file_sync(self, remote_file_path: str, output_path: Path, timeout_seconds: int = 60) -> None:
        runtime = self._telegram_runtime()
        token = str(runtime.get("token") or "").strip()
        if not token:
            raise RuntimeError("Telegram bot token is not configured.")
        if httpx is None:
            raise RuntimeError("httpx is unavailable.")

        url = f"https://api.telegram.org/file/bot{token}/{remote_file_path}"
        response = httpx.get(url, timeout=timeout_seconds)
        response.raise_for_status()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)

    def _build_telegram_attachment_filename(
        self,
        attachment: dict[str, Any],
        *,
        message_id: int,
        index: int,
        remote_path: str,
        base_name: str,
    ) -> str:
        telegram_type = str(attachment.get("type") or "attachment").strip().lower()
        mime_type = str(attachment.get("mime_type") or "").strip().lower()
        extension = Path(base_name).suffix if base_name else ""
        if not extension:
            extension = Path(remote_path).suffix
        if not extension and mime_type:
            extension = mimetypes.guess_extension(mime_type) or ""
        if not extension:
            extension = {
                "photo": ".jpg",
                "video": ".mp4",
                "audio": ".mp3",
                "voice": ".ogg",
                "animation": ".gif",
                "video_note": ".mp4",
                "sticker": ".webp",
            }.get(telegram_type, "")

        stem = base_name or f"{telegram_type}-{message_id}-{index}"
        if extension and not stem.endswith(extension):
            stem = f"{Path(stem).stem}{extension}"
        safe_name = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")
        if not safe_name:
            safe_name = f"{telegram_type}-{message_id}-{index}{extension}"
        return safe_name

    def _deduplicate_telegram_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        counter = 2
        while True:
            candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
            if not candidate.exists():
                return candidate
            counter += 1

    def _build_telegram_goal(
        self,
        message: dict[str, Any],
        downloaded_attachments: list[dict[str, Any]],
        attachment_errors: list[str],
    ) -> str:
        base_prompt = str(message.get("text") or "").strip()
        if not base_prompt:
            if downloaded_attachments:
                base_prompt = "Review the attached Telegram file or image and respond to the user."
            else:
                base_prompt = "Respond to the Telegram message."

        notes: list[str] = []
        if downloaded_attachments:
            notes.append("Telegram attachments saved locally:")
            for item in downloaded_attachments:
                details = [value for value in (item.get("telegram_type"), item.get("mime_type")) if value]
                detail_text = f" ({', '.join(details)})" if details else ""
                notes.append(f"- {item['path']}{detail_text}")
            notes.append(
                "Use local file tools to inspect any relevant attachment. If a binary attachment cannot be meaningfully inspected with the available tools, say so plainly."
            )
        if attachment_errors:
            notes.append("Attachment download issues:")
            notes.extend(f"- {item}" for item in attachment_errors[:5])

        if not notes:
            return base_prompt
        return f"{base_prompt}\n\n" + "\n".join(notes)

    def _telegram_wants_desktop_save(self, prompt: str) -> bool:
        lowered = str(prompt or "").strip().lower()
        if "desktop" not in lowered:
            return False
        return bool(re.search(r"\b(?:save|copy|move|put|store|download)\b", lowered))

    def _telegram_prompt_prefers_images(self, prompt: str) -> bool:
        lowered = str(prompt or "").strip().lower()
        return bool(re.search(r"\b(?:image|picture|photo|png|jpg|jpeg|gif|webp|screenshot)\b", lowered))

    def _telegram_attachment_is_image(self, attachment: dict[str, Any]) -> bool:
        mime_type = str(attachment.get("mime_type") or "").strip().lower()
        path = Path(str(attachment.get("path") or "")).expanduser()
        return mime_type.startswith("image/") or path.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
            ".bmp",
            ".tif",
            ".tiff",
            ".heic",
            ".heif",
        }

    def _telegram_recent_attachment_reference(
        self,
        *,
        chat_id: str,
        prefer_images: bool,
        reply_message_id: int | None,
        exclude_message_id: int | None,
    ) -> dict[str, Any] | None:
        tasks = self.state_store.list_tasks(limit=25)

        def _attachment_from_task(task: dict[str, Any]) -> dict[str, Any] | None:
            metadata = dict(task.get("metadata") or {})
            if str(metadata.get("source") or "") != "telegram":
                return None
            if str(metadata.get("telegram_chat_id") or "") != chat_id:
                return None
            task_message_id = int(metadata.get("telegram_message_id") or 0) or None
            if exclude_message_id and task_message_id == exclude_message_id:
                return None
            for attachment in metadata.get("telegram_attachments") or []:
                if not isinstance(attachment, dict):
                    continue
                path = Path(str(attachment.get("path") or "")).expanduser()
                if not path.exists():
                    continue
                if prefer_images and not self._telegram_attachment_is_image(attachment):
                    continue
                return attachment
            return None

        if reply_message_id:
            for task in tasks:
                metadata = dict(task.get("metadata") or {})
                task_message_id = int(metadata.get("telegram_message_id") or 0) or None
                if task_message_id == reply_message_id:
                    match = _attachment_from_task(task)
                    if match:
                        return match

        for task in tasks:
            match = _attachment_from_task(task)
            if match:
                return match
        return None

    def _telegram_desktop_save_fast_path(
        self,
        *,
        chat_id: str,
        message: dict[str, Any],
        downloaded_attachments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        prompt = str(message.get("text") or "").strip()
        if not self._telegram_wants_desktop_save(prompt):
            return None

        prefer_images = self._telegram_prompt_prefers_images(prompt)
        current_candidates = [
            attachment
            for attachment in downloaded_attachments
            if isinstance(attachment, dict) and (not prefer_images or self._telegram_attachment_is_image(attachment))
        ]
        attachment = current_candidates[0] if current_candidates else None
        if attachment is None:
            reply = message.get("reply") or {}
            reply_message_id = int(reply.get("message_id") or 0) or None
            attachment = self._telegram_recent_attachment_reference(
                chat_id=chat_id,
                prefer_images=prefer_images,
                reply_message_id=reply_message_id,
                exclude_message_id=int(message.get("message_id") or 0) or None,
            )
        if not isinstance(attachment, dict):
            return None

        source_path = Path(str(attachment.get("path") or "")).expanduser()
        if not source_path.exists():
            return None
        destination_path = Path.home() / "Desktop" / source_path.name
        return {
            "attachment": dict(attachment),
            "initial_tool_call": {
                "tool_name": "copy_local_file",
                "arguments": {
                    "source_path": str(source_path),
                    "destination_path": str(destination_path),
                },
            },
        }

    def _telegram_local_file_delivery_fast_path(self, message: dict[str, Any]) -> dict[str, Any] | None:
        prompt = str(message.get("text") or "").strip()
        request = _resolve_local_file_telegram_request(prompt)
        if not request:
            return None

        resolved_path = Path(str(request.get("resolved_path") or "")).expanduser()
        if resolved_path.exists() and resolved_path.is_file():
            requested_name = str(request.get("requested_name") or "").strip()
            return {
                "requested_path": str(request.get("requested_path") or ""),
                "requested_name": requested_name,
                "resolved_path": str(resolved_path),
                "suggestions": list(request.get("suggestions") or []),
                "initial_tool_call": {
                    "tool_name": "send_local_file_to_telegram",
                    "arguments": {
                        "path": str(resolved_path),
                        "caption": resolved_path.name,
                        "send_to_telegram": False,
                        "requested_name": requested_name,
                    },
                },
            }

        requested_path = Path(str(request.get("requested_path") or "")).expanduser()
        requested_name = str(request.get("requested_name") or requested_path.name or "").strip()
        suggestions = [Path(item).name for item in request.get("suggestions") or [] if str(item).strip()]
        lines = [f"I couldn't find {requested_name or 'that file'} on your Desktop."]
        if suggestions:
            label = "Closest match" if len(suggestions) == 1 else "Closest matches"
            lines.append(f"{label}: {', '.join(suggestions[:3])}")
        lines.append("Reply with the exact filename and I'll send it here.")
        return {
            "requested_path": str(requested_path),
            "requested_name": requested_name,
            "resolved_path": "",
            "suggestions": suggestions,
            "missing_message": "\n".join(lines),
        }

    def _telegram_image_attachments(self, downloaded_attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        for item in downloaded_attachments:
            if self._telegram_attachment_is_image(item):
                images.append(item)
        return images

    def _build_telegram_image_prompt(
        self,
        message: dict[str, Any],
        image_attachments: list[dict[str, Any]],
        attachment_errors: list[str],
    ) -> str:
        base_prompt = str(message.get("text") or "").strip()
        if not base_prompt:
            if len(image_attachments) == 1:
                base_prompt = "Describe the attached image and answer the user directly."
            else:
                base_prompt = "Describe the attached images and answer the user directly."

        notes: list[str] = []
        if attachment_errors:
            notes.append("Some Telegram attachments could not be downloaded:")
            notes.extend(f"- {item}" for item in attachment_errors[:5])
        if not notes:
            return base_prompt
        return f"{base_prompt}\n\n" + "\n".join(notes)

    def _build_telegram_history(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        chat_id = str(message.get("chat_id") or "").strip()
        current_message_id = int(message.get("message_id") or 0) or None
        reply = message.get("reply") or {}
        reply_message_id = int(reply.get("message_id") or 0) or None
        history: list[dict[str, Any]] = []

        if chat_id:
            history = self._telegram_recent_chat_history(
                chat_id=chat_id,
                reply_message_id=reply_message_id,
                exclude_message_id=current_message_id,
            )

        if not isinstance(reply, dict) or not reply:
            return history

        entry = self._telegram_reply_history_entry(reply)
        if entry and not any(
            str(item.get("role") or "") == entry["role"] and str(item.get("content") or "") == entry["content"]
            for item in history[-2:]
        ):
            history.append(entry)
        return history[-8:]

    def _telegram_reply_history_entry(self, reply: dict[str, Any]) -> dict[str, str] | None:
        lines: list[str] = []
        reply_text = str(reply.get("text") or "").strip()
        if reply_text:
            lines.append(reply_text)
        attachments = reply.get("attachments") or []
        if attachments:
            lines.append("Referenced Telegram attachment(s):")
            for attachment in attachments[:5]:
                details = [value for value in (attachment.get("file_name"), attachment.get("type"), attachment.get("mime_type")) if value]
                lines.append(f"- {', '.join(details) or 'attachment'}")
        if not lines:
            return None
        return {
            "role": "assistant" if reply.get("from_is_bot") else "user",
            "content": "\n".join(lines),
        }

    def _telegram_task_matches_message_id(self, task: dict[str, Any], message_id: int | None) -> bool:
        if not message_id:
            return False
        metadata = dict(task.get("metadata") or {})
        known_ids = {
            int(metadata.get("telegram_message_id") or 0) or None,
            int(metadata.get("telegram_status_message_id") or 0) or None,
        }
        for item in list(metadata.get("telegram_result_message_ids") or []):
            try:
                known_ids.add(int(item))
            except Exception:
                continue
        return message_id in known_ids

    def _telegram_task_history_messages(self, task: dict[str, Any]) -> list[dict[str, str]]:
        metadata = dict(task.get("metadata") or {})
        messages: list[dict[str, str]] = []

        user_text = str(metadata.get("telegram_original_text") or "").strip()
        if not user_text:
            user_text = str(task.get("goal") or "").strip().split("\n\n", 1)[0].strip()
        if user_text:
            messages.append({"role": "user", "content": user_text})

        result = dict(task.get("result") or {})
        assistant_text = str(result.get("message") or task.get("error") or "").strip()
        if assistant_text:
            messages.append({"role": "assistant", "content": assistant_text})
        return messages

    def _telegram_recent_chat_history(
        self,
        *,
        chat_id: str,
        reply_message_id: int | None,
        exclude_message_id: int | None,
        max_tasks: int = 3,
    ) -> list[dict[str, str]]:
        tasks = [
            task
            for task in self.state_store.list_tasks(limit=40)
            if str((task.get("metadata") or {}).get("source") or "") == "telegram"
            and str((task.get("metadata") or {}).get("telegram_chat_id") or "") == chat_id
            and (int((task.get("metadata") or {}).get("telegram_message_id") or 0) or None) != exclude_message_id
        ]
        tasks.sort(
            key=lambda task: (
                int((task.get("metadata") or {}).get("telegram_message_id") or 0),
                str(task.get("created_at") or ""),
            )
        )

        if reply_message_id:
            anchor_index = next(
                (
                    index + 1
                    for index, task in enumerate(tasks)
                    if self._telegram_task_matches_message_id(task, reply_message_id)
                ),
                None,
            )
            if anchor_index is not None:
                tasks = tasks[:anchor_index]

        selected = tasks[-max(1, max_tasks):]
        history: list[dict[str, str]] = []
        for task in selected:
            for item in self._telegram_task_history_messages(task):
                if history and history[-1] == item:
                    continue
                history.append(item)
        return history[-8:]

    def _telegram_initial_status(self, prompt: str, initial_tool_call: dict[str, Any] | None) -> str:
        tool_name = str((initial_tool_call or {}).get("tool_name") or "").strip()
        lowered = str(prompt or "").strip().lower()
        if tool_name == "copy_local_file":
            return "Saving to desktop"
        if tool_name == "send_local_file_to_telegram":
            return "Sending file"
        if tool_name == "take_screenshot":
            return "Capturing screenshot"
        if tool_name == "generate_image":
            return "Generating image"
        if "svg" in lowered and re.search(r"\b(?:png|jpg|jpeg|webp|render|export|convert)\b", lowered):
            return "Generating SVG"
        return "Thinking"

    def _telegram_approval_reply_markup(self, approval_id: str) -> dict[str, Any] | None:
        approval_token = str(approval_id or "").strip()
        if not approval_token:
            return None
        return {
            "inline_keyboard": [
                [
                    {"text": "Approve", "callback_data": f"approval:approve:{approval_token}"},
                    {"text": "Deny", "callback_data": f"approval:deny:{approval_token}"},
                ]
            ]
        }

    def _telegram_compact_error(self, message: str) -> str:
        text = self._truncate_telegram_line(str(message or "").strip(), 280)
        if not text:
            return "Couldn't finish that."
        if "Could not free LM Studio VRAM safely" in text or "VRAM did not clear" in text:
            return "Couldn't generate media safely because LM Studio did not free VRAM."
        if text.lower().startswith("couldn't finish"):
            return text
        return f"Couldn't finish that: {text}"

    def _telegram_result_summary(
        self,
        status: str,
        message: str,
        artifacts: list[dict[str, Any]] | None,
    ) -> str:
        artifact_count = len([item for item in artifacts or [] if isinstance(item, dict)])
        lines = [line.strip() for line in str(message or "").splitlines() if line.strip()]
        summary_line = ""
        for line in lines:
            if line.startswith("/") or line.startswith("~") or re.match(r"^[A-Za-z]:\\\\", line):
                continue
            summary_line = line.replace(" Sent to Telegram.", "").replace("WARNING: LLM reload failed.", "").strip()
            if summary_line:
                break

        if status == "failed":
            return self._telegram_compact_error(message)
        if artifact_count:
            if summary_line.lower().startswith("generated "):
                return self._truncate_telegram_line(summary_line, 240)
            label = "file" if artifact_count == 1 else "files"
            return f"Done. Sent {artifact_count} {label}."
        if status == "waiting_approval":
            return "Approval is required before I can continue."
        if summary_line:
            return self._truncate_telegram_line(summary_line, 240)
        if status == "completed":
            return "Done."
        return ""

    def _telegram_result_text(
        self,
        status: str,
        message: str,
        artifacts: list[dict[str, Any]] | None,
    ) -> str:
        clean_message = str(message or "").strip()
        if status == "completed" and clean_message:
            return clean_message
        return self._telegram_result_summary(status, clean_message, artifacts)

    async def _telegram_finish_approval_resolution(
        self,
        chat_id: str,
        approval: dict[str, Any],
        *,
        approved: bool,
        reply_to_message_id: int | None = None,
        callback_message_id: int | None = None,
    ) -> None:
        try:
            result = await self._dispatch(
                "approval",
                {
                    "action": "resolve",
                    "approval_id": approval["id"],
                    "approved": approved,
                },
            )
        except Exception as exc:
            failure_text = f"Approval command failed: {exc}"
            if callback_message_id:
                await self._telegram_edit_message_text(chat_id, callback_message_id, failure_text)
            else:
                await self._telegram_send_text(
                    chat_id,
                    failure_text,
                    reply_to_message_id=reply_to_message_id,
                )
            return

        approval = result.get("approval") or approval
        short_id = self._short_telegram_approval_id(str(approval.get("id") or ""))
        status_text = f"Denied {short_id}." if not approved else f"Approved {short_id}. Resuming."
        if callback_message_id:
            await self._telegram_edit_message_text(chat_id, callback_message_id, status_text)
        else:
            await self._telegram_send_text(
                chat_id,
                status_text,
                reply_to_message_id=reply_to_message_id,
            )

        task_id = str(approval.get("task_id") or "")
        if task_id:
            await self._telegram_update_task_status(
                task_id,
                chat_id,
                "Resuming" if approved else "Denied",
                reply_to_message_id=reply_to_message_id,
            )
        if not approved:
            if task_id:
                await self._telegram_send_task_result(
                    chat_id,
                    task_id,
                    reply_to_message_id=reply_to_message_id,
                )
            return

        active = self.active_tasks.get(task_id)
        if active:
            try:
                await active
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                return
        if task_id:
            await self._telegram_send_task_result(
                chat_id,
                task_id,
                reply_to_message_id=reply_to_message_id,
            )

    def _resolve_pending_approval(self, identifier: str) -> dict[str, Any]:
        approvals = self.state_store.list_pending_approvals()
        if not approvals:
            raise RuntimeError("No pending approvals.")

        token = str(identifier or "").strip().lower()
        if not token:
            raise RuntimeError("Approval id is required.")
        if token in {"latest", "last"}:
            return approvals[-1]

        exact = [item for item in approvals if str(item.get("id") or "").lower() == token]
        if exact:
            return exact[0]

        matches = [item for item in approvals if str(item.get("id") or "").lower().startswith(token)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise RuntimeError(f"Approval prefix is ambiguous: {identifier}")
        raise RuntimeError(f"No pending approval matches: {identifier}")

    def _format_telegram_approval_lines(self, approval: dict[str, Any]) -> list[str]:
        task = self.state_store.get_task(str(approval.get("task_id") or "")) or {}
        short_id = self._short_telegram_approval_id(str(approval.get("id") or ""))
        lines = [f"{short_id} - {approval.get('operation') or 'approval'}"]
        if task.get("goal"):
            lines.append(f"Task: {self._truncate_telegram_line(str(task.get('goal') or ''), 140)}")
        action_text = approval.get("payload", {}).get("summary") or approval.get("command") or ""
        lines.append(f"Action: {self._truncate_telegram_line(str(action_text), 180)}")
        lines.append(f"Approve: /approve {short_id}")
        lines.append(f"Deny: /deny {short_id}")
        return lines

    def _short_telegram_approval_id(self, approval_id: str) -> str:
        value = str(approval_id or "").strip()
        if not value:
            return value
        return value[:_TELEGRAM_APPROVAL_ID_LENGTH]

    def _truncate_telegram_line(self, text: str, max_chars: int) -> str:
        value = str(text or "").strip()
        if len(value) <= max_chars:
            return value
        return value[: max_chars - 3].rstrip() + "..."

    async def _telegram_api_request(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        file_path: Path | None = None,
        file_field: str = "document",
        mime_type: str = "application/octet-stream",
        timeout_seconds: int = 60,
    ) -> Any:
        runtime = self._telegram_runtime()
        token = runtime.get("token") or ""
        if not token:
            raise RuntimeError("Telegram bot token is not configured.")
        return await asyncio.to_thread(
            self._telegram_api_request_sync,
            token,
            method,
            payload or {},
            file_path,
            file_field,
            mime_type,
            timeout_seconds,
        )

    def _telegram_api_request_sync(
        self,
        token: str,
        method: str,
        payload: dict[str, Any],
        file_path: Path | None,
        file_field: str,
        mime_type: str,
        timeout_seconds: int,
    ) -> Any:
        if httpx is None:
            raise RuntimeError("httpx is unavailable.")

        url = f"https://api.telegram.org/bot{token}/{method}"
        try:
            if file_path is None:
                response = httpx.post(url, json=payload, timeout=timeout_seconds)
            else:
                with file_path.open("rb") as handle:
                    files = {
                        file_field: (
                            file_path.name,
                            handle,
                            mime_type,
                        )
                    }
                    response = httpx.post(url, data=payload, files=files, timeout=timeout_seconds)
            response.raise_for_status()
        except Exception as exc:  # pragma: no cover - network guard
            raise RuntimeError(f"Telegram API request failed for {method}: {exc}") from exc

        try:
            body = response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"Telegram API returned invalid JSON for {method}") from exc

        if not body.get("ok", False):
            raise RuntimeError(str(body.get("description") or f"Telegram API call failed for {method}"))
        return body.get("result")

    def _split_telegram_text(self, text: str) -> list[str]:
        content = str(text or "").strip()
        if not content:
            return []
        if len(content) <= _TELEGRAM_MESSAGE_LIMIT:
            return [content]

        chunks: list[str] = []
        remaining = content
        while remaining:
            if len(remaining) <= _TELEGRAM_MESSAGE_LIMIT:
                chunks.append(remaining)
                break
            split_at = remaining.rfind("\n", 0, _TELEGRAM_MESSAGE_LIMIT)
            if split_at < int(_TELEGRAM_MESSAGE_LIMIT * 0.6):
                split_at = remaining.rfind(" ", 0, _TELEGRAM_MESSAGE_LIMIT)
            if split_at <= 0:
                split_at = _TELEGRAM_MESSAGE_LIMIT
            chunk = remaining[:split_at].strip()
            if chunk:
                chunks.append(chunk)
            remaining = remaining[split_at:].strip()
        return chunks
