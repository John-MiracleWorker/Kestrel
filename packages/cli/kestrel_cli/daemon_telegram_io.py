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
                        "Kestrel is live on Telegram.",
                        "Send any text prompt to start a native agent run.",
                        "Reply to a previous Telegram message to carry that thread context forward.",
                        "Attach a file or image and Kestrel will save it locally and use it when possible.",
                        "/status shows runtime health.",
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
                        f"Provider: {local_models.get('default_provider', 'unknown')}",
                        f"Model: {local_models.get('default_model', 'unknown')}",
                        f"Pending approvals: {len(pending)}",
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
                result = await self._dispatch(
                    "approval",
                    {
                        "action": "resolve",
                        "approval_id": approval["id"],
                        "approved": approved,
                    },
                )
            except Exception as exc:
                await self._telegram_send_text(
                    chat_id,
                    f"Approval command failed: {exc}",
                    reply_to_message_id=message_id,
                )
                return

            approval = result.get("approval") or {}
            short_id = self._short_telegram_approval_id(str(approval.get("id") or ""))
            if not approved:
                await self._telegram_send_text(
                    chat_id,
                    f"Denied {short_id}.",
                    reply_to_message_id=message_id,
                )
                return

            await self._telegram_send_text(
                chat_id,
                f"Approved {short_id}. Resuming the task.",
                reply_to_message_id=message_id,
            )
            task_id = str(approval.get("task_id") or "")
            active = self.active_tasks.get(task_id)
            if active:
                try:
                    await active
                except asyncio.CancelledError:  # pragma: no cover - shutdown path
                    return
            await self._telegram_send_task_result(chat_id, task_id, reply_to_message_id=message_id)
            return

        await self._telegram_send_text(
            chat_id,
            "Unknown command. Use /help for the supported Telegram controls.",
            reply_to_message_id=message_id,
        )

    async def _handle_telegram_chat(self, message: dict[str, Any]) -> None:
        chat_id = str(message.get("chat_id") or "")
        message_id = int(message.get("message_id") or 0) or None
        runtime = self._telegram_runtime()
        downloaded_attachments, attachment_errors = await self._download_telegram_attachments(message)
        prompt = self._build_telegram_goal(message, downloaded_attachments, attachment_errors)
        history = self._build_telegram_history(message)

        task = self.state_store.create_task(
            goal=prompt,
            kind="chat",
            metadata={
                "workspace_id": runtime.get("workspace_id") or "default",
                "source": "telegram",
                "telegram_chat_id": chat_id,
                "telegram_user_id": str(message.get("from_id") or ""),
                "telegram_message_id": message_id or 0,
                "telegram_original_text": str(message.get("text") or ""),
                "telegram_attachments": downloaded_attachments,
                "telegram_attachment_errors": attachment_errors,
                "telegram_reply": dict(message.get("reply") or {}),
            },
        )
        initial_tool_call = self._detect_fast_path_tool_call(prompt)
        if initial_tool_call and str(initial_tool_call.get("tool_name") or "") == "take_screenshot":
            arguments = dict(initial_tool_call.get("arguments") or {})
            arguments["send_to_telegram"] = False
            initial_tool_call["arguments"] = arguments

        progress_task = asyncio.create_task(
            self._telegram_send_delayed_working_note(
                chat_id,
                task["id"],
                reply_to_message_id=message_id,
            )
        )
        try:
            outcome = await self._run_native_agent_task(
                task["id"],
                prompt,
                kind="chat",
                history=history,
                initial_tool_call=initial_tool_call,
            )
        finally:
            progress_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_task
        await self._telegram_respond_with_outcome(chat_id, task["id"], outcome, reply_to_message_id=message_id)

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
            lines = ["Approval required."]
            if task.get("goal"):
                lines.append(f"Task: {self._truncate_telegram_line(str(task.get('goal') or ''), 160)}")
            lines.append(f"Action: {self._truncate_telegram_line(str(outcome.message or approval.get('summary') or ''), 180)}")
            if approval_id:
                lines.append(f"Approve: /approve {short_id}")
                lines.append(f"Deny: /deny {short_id}")
                lines.append("Use /approvals to inspect the full pending queue.")
            await self._telegram_send_text(chat_id, "\n".join(lines), reply_to_message_id=reply_to_message_id)
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
                "Working on it. I'll reply here when it's ready.",
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

        if status == "failed" and not message:
            message = "The task failed."
        if status == "waiting_approval" and not message:
            message = "Approval is required before I can continue."
        if status == "completed" and not message:
            message = "Done."

        artifacts = result.get("artifacts") or self._list_task_artifacts(task_id)
        if message:
            await self._telegram_send_text(chat_id, message, reply_to_message_id=reply_to_message_id)
        await self._telegram_send_artifacts(chat_id, artifacts, reply_to_message_id=reply_to_message_id)

    async def _telegram_send_text(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        for chunk in self._split_telegram_text(text):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_to_message_id:
                payload["reply_to_message_id"] = reply_to_message_id
            await self._telegram_api_request("sendMessage", payload)

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

    def _build_telegram_history(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        reply = message.get("reply") or {}
        if not isinstance(reply, dict) or not reply:
            return []

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
            return []

        return [
            {
                "role": "assistant" if reply.get("from_is_bot") else "user",
                "content": "\n".join(lines),
            }
        ]

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
        lines.append(f"Action: {self._truncate_telegram_line(str(approval.get('command') or ''), 180)}")
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

