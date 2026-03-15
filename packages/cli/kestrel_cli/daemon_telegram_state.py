from __future__ import annotations

from . import daemon_core as _daemon_core

globals().update({name: value for name, value in vars(_daemon_core).items() if not name.startswith("__")})

class KestrelDaemonTelegramStateMixin:
    def _control_endpoint(self) -> str:
        if os.name == "nt":
            return f"tcp://{self.paths.control_host}:{self.paths.control_port}"
        return str(self.paths.control_socket)

    def _channel_state_path(self) -> Path:
        return self.paths.state_dir / "gateway-channels.json"

    def _load_channel_state(self) -> dict[str, Any]:
        state_path = self._channel_state_path()
        with self.channel_state_lock:
            if not state_path.exists():
                return {}
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return {}
        return payload if isinstance(payload, dict) else {}

    def _save_channel_state(self, payload: dict[str, Any]) -> None:
        with self.channel_state_lock:
            write_json_atomic(self._channel_state_path(), payload)

    def _update_channel_state(
        self,
        *,
        config_updates: dict[str, Any] | None = None,
        state_updates: dict[str, Any] | None = None,
        add_mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._load_channel_state()
        telegram = dict(payload.get("telegram") or {})
        config = dict(telegram.get("config") or {})
        state = dict(telegram.get("state") or {})
        changed = False

        if config_updates:
            for key, value in config_updates.items():
                if value in ("", None):
                    continue
                if config.get(key) != value:
                    config[key] = value
                    changed = True

        mappings = list(state.get("mappings") or [])
        if add_mapping and add_mapping.get("chatId") not in ("", None):
            chat_id = str(add_mapping.get("chatId") or "").strip()
            if chat_id and not any(
                isinstance(item, dict) and str(item.get("chatId") or "").strip() == chat_id
                for item in mappings
            ):
                mappings.append(add_mapping)
                state["mappings"] = mappings
                changed = True

        if state_updates:
            for key, value in state_updates.items():
                if state.get(key) != value:
                    state[key] = value
                    changed = True

        if not changed:
            return payload

        if config:
            config.setdefault("mode", "polling")
            config.setdefault("workspaceId", "default")
            config["updatedAt"] = _now()
            telegram["config"] = config
        if state:
            state["updatedAt"] = _now()
            telegram["state"] = state
        payload["telegram"] = telegram
        self._save_channel_state(payload)
        return payload

    def _telegram_runtime(self) -> dict[str, Any]:
        payload = self._load_channel_state()
        telegram = dict(payload.get("telegram") or {})
        config = dict(telegram.get("config") or {})
        state = dict(telegram.get("state") or {})

        env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        env_mode = os.getenv("TELEGRAM_MODE", "").strip()

        if env_token:
            config["token"] = env_token
        if env_mode:
            config["mode"] = env_mode
        config.setdefault("mode", "polling")
        config.setdefault("workspaceId", "default")

        allowed_chat_ids: list[str] = []
        if env_chat_id:
            allowed_chat_ids.append(env_chat_id)
        for item in state.get("mappings") or []:
            if isinstance(item, dict):
                candidate = str(item.get("chatId") or "").strip()
                if candidate and candidate not in allowed_chat_ids:
                    allowed_chat_ids.append(candidate)

        return {
            "payload": payload,
            "telegram": telegram,
            "config": config,
            "state": state,
            "token": str(config.get("token") or "").strip(),
            "mode": str(config.get("mode") or "polling").strip().lower(),
            "workspace_id": str(config.get("workspaceId") or "default"),
            "allowed_chat_ids": allowed_chat_ids,
        }

    def _sync_telegram_channel_state_from_environment(self) -> None:
        env_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        env_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        env_mode = os.getenv("TELEGRAM_MODE", "").strip() or "polling"
        if not env_token and not env_chat_id:
            return

        mapping: dict[str, Any] | None = None
        if env_chat_id:
            chat_value: Any = int(env_chat_id) if env_chat_id.isdigit() else env_chat_id
            mapping = {
                "userId": f"telegram:{env_chat_id}",
                "chatId": chat_value,
            }
        self._update_channel_state(
            config_updates={
                "token": env_token,
                "workspaceId": "default",
                "mode": env_mode,
            },
            add_mapping=mapping,
        )

    def _read_channel_status(self) -> dict[str, Any]:
        runtime = self._telegram_runtime()
        config = runtime.get("config") or {}
        state = runtime.get("state") or {}
        return {
            "channels": {
                "telegram": {
                    "configured": bool(config.get("token")),
                    "running": bool(self.telegram_poll_task and not self.telegram_poll_task.done()),
                    "workspace_id": config.get("workspaceId") or "",
                    "mode": config.get("mode") or "polling",
                    "updated_at": config.get("updatedAt") or "",
                    "known_mappings": len((state.get("mappings") or [])),
                    "allowed_chat_ids": list(runtime.get("allowed_chat_ids") or []),
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
        workspace_id = str(params.get("workspace_id") or "default")
        mode = str(params.get("mode") or "polling")
        webhook_url = params.get("webhook_url") or ""
        config = {
            "token": token,
            "workspaceId": workspace_id,
            "mode": mode,
            "webhookUrl": webhook_url,
            "updatedAt": _now(),
        }
        mapping = None
        chat_id = str(params.get("chat_id") or "").strip()
        if chat_id:
            mapping = {
                "userId": f"telegram:{chat_id}",
                "chatId": int(chat_id) if chat_id.isdigit() else chat_id,
            }
        self._update_channel_state(config_updates=config, add_mapping=mapping)
        return {"channel": channel, "status": "paired"}

    async def _configure_telegram_transport(self) -> None:
        if self.telegram_poll_task:
            self.telegram_poll_task.cancel()
            try:
                await self.telegram_poll_task
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                pass
            self.telegram_poll_task = None

        runtime = self._telegram_runtime()
        token = runtime.get("token") or ""
        mode = runtime.get("mode") or "polling"
        if not token:
            LOGGER.info("Telegram transport disabled: no bot token configured")
            return
        if mode != "polling":
            LOGGER.warning("Telegram mode %s is not supported by the native daemon; polling is required", mode)
            return
        if httpx is None:
            LOGGER.warning("Telegram transport disabled: httpx is unavailable")
            return

        self.telegram_poll_task = asyncio.create_task(self._telegram_poll_loop())
        LOGGER.info("Telegram polling enabled for workspace %s", runtime.get("workspace_id") or "default")

    async def _telegram_poll_loop(self) -> None:
        backoff_seconds = 1
        while not self.stop_event.is_set():
            try:
                handled = await self._telegram_poll_once()
                if handled or backoff_seconds > 1:
                    backoff_seconds = 1
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                raise
            except Exception as exc:
                LOGGER.warning("Telegram polling failed: %s", exc)
                try:
                    await asyncio.wait_for(self.stop_event.wait(), timeout=backoff_seconds)
                except asyncio.TimeoutError:
                    pass
                backoff_seconds = min(backoff_seconds * 2, 30)

    async def _telegram_poll_once(self) -> int:
        runtime = self._telegram_runtime()
        token = runtime.get("token") or ""
        if not token or runtime.get("mode") != "polling":
            return 0

        state = runtime.get("state") or {}
        offset = int(state.get("pollingOffset") or 0)
        updates = await self._telegram_api_request(
            "getUpdates",
            {
                "offset": offset,
                "timeout": 25,
                "allowed_updates": ["message"],
            },
        )
        handled = 0
        for update in updates if isinstance(updates, list) else []:
            update_id = int(update.get("update_id") or 0)
            if update_id:
                self._update_channel_state(state_updates={"pollingOffset": update_id + 1})
            if self._handle_telegram_update(update):
                handled += 1
        return handled

    def _handle_telegram_update(self, update: dict[str, Any]) -> bool:
        message = update.get("message") or update.get("edited_message")
        if not isinstance(message, dict):
            return False
        if (message.get("from") or {}).get("is_bot"):
            return False

        normalized = self._normalize_telegram_message(message)
        if not normalized:
            return False

        task = asyncio.create_task(self._process_telegram_message(normalized))
        self.telegram_message_tasks.add(task)
        task.add_done_callback(self._on_telegram_message_task_done)
        return True

    def _on_telegram_message_task_done(self, task: asyncio.Task) -> None:
        self.telegram_message_tasks.discard(task)
        try:
            task.result()
        except asyncio.CancelledError:  # pragma: no cover - shutdown path
            return
        except Exception:
            LOGGER.exception("Telegram message handler failed")

    def _normalize_telegram_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        chat_id = str(chat.get("id") or "").strip()
        if not chat_id:
            return None
        text = str(message.get("text") or message.get("caption") or "").strip()
        return {
            "chat_id": chat_id,
            "chat_type": str(chat.get("type") or "private"),
            "message_id": int(message.get("message_id") or 0),
            "text": text,
            "from_id": str(sender.get("id") or "").strip(),
            "from_username": str(sender.get("username") or "").strip(),
            "first_name": str(sender.get("first_name") or "").strip(),
            "attachments": self._normalize_telegram_attachments(message),
            "reply": self._normalize_telegram_reply(message.get("reply_to_message")),
        }

    def _normalize_telegram_reply(self, reply_message: Any) -> dict[str, Any]:
        if not isinstance(reply_message, dict):
            return {}
        sender = reply_message.get("from") or {}
        return {
            "message_id": int(reply_message.get("message_id") or 0),
            "text": str(reply_message.get("text") or reply_message.get("caption") or "").strip(),
            "from_id": str(sender.get("id") or "").strip(),
            "from_username": str(sender.get("username") or "").strip(),
            "from_is_bot": bool(sender.get("is_bot")),
            "attachments": self._normalize_telegram_attachments(reply_message),
        }

    def _normalize_telegram_attachments(self, message: dict[str, Any]) -> list[dict[str, Any]]:
        attachments: list[dict[str, Any]] = []

        photo_variants = message.get("photo") or []
        if isinstance(photo_variants, list) and photo_variants:
            photo = photo_variants[-1] or {}
            attachments.append(
                {
                    "type": "photo",
                    "file_id": str(photo.get("file_id") or "").strip(),
                    "file_name": str(photo.get("file_name") or "").strip(),
                    "mime_type": "image/jpeg",
                    "size": int(photo.get("file_size") or 0),
                }
            )

        media_fields = (
            ("document", "document"),
            ("video", "video"),
            ("audio", "audio"),
            ("voice", "voice"),
            ("animation", "animation"),
            ("video_note", "video_note"),
            ("sticker", "sticker"),
        )
        for field_name, attachment_type in media_fields:
            item = message.get(field_name) or {}
            if not isinstance(item, dict) or not item:
                continue
            attachments.append(
                {
                    "type": attachment_type,
                    "file_id": str(item.get("file_id") or "").strip(),
                    "file_name": str(item.get("file_name") or "").strip(),
                    "mime_type": str(item.get("mime_type") or "").strip(),
                    "size": int(item.get("file_size") or 0),
                }
            )

        return [item for item in attachments if item.get("file_id")]

    def _telegram_chat_allowed(self, chat_id: str) -> bool:
        runtime = self._telegram_runtime()
        allowed = {str(item).strip() for item in runtime.get("allowed_chat_ids") or [] if str(item).strip()}
        return bool(allowed) and chat_id in allowed

    async def _process_telegram_message(self, message: dict[str, Any]) -> None:
        chat_id = str(message.get("chat_id") or "").strip()
        if not chat_id:
            return
        if not self._telegram_chat_allowed(chat_id):
            LOGGER.info("Ignoring Telegram update from unpaired chat %s", chat_id)
            return

        mapping = {
            "userId": f"telegram:{str(message.get('from_id') or chat_id).strip()}",
            "chatId": int(chat_id) if chat_id.isdigit() else chat_id,
        }
        self._update_channel_state(add_mapping=mapping)

        text = str(message.get("text") or "").strip()
        attachments = list(message.get("attachments") or [])
        if not text and not attachments:
            await self._telegram_send_text(
                chat_id,
                "Send a text prompt, or attach a file with an optional caption. Use /help for Telegram controls.",
                reply_to_message_id=int(message.get("message_id") or 0) or None,
            )
            return

        await self._telegram_send_chat_action(chat_id, "typing")
        try:
            if text.startswith("/"):
                await self._handle_telegram_command(message)
                return
            await self._handle_telegram_chat(message)
        except Exception as exc:
            LOGGER.exception("Telegram message processing failed")
            await self._telegram_send_text(
                chat_id,
                f"Kestrel hit an error while processing that message: {exc}",
                reply_to_message_id=int(message.get("message_id") or 0) or None,
            )

