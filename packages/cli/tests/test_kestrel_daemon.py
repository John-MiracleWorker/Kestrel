import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kestrel_daemon as daemon
import kestrel_native as native
from kestrel_cli import daemon_core as daemon_core_impl
from kestrel_cli import daemon_telegram_io as daemon_telegram_io_impl
from kestrel_cli import native_models as native_models_impl

_TELEGRAM_STATUS_TEXTS = {
    "Analyzing image",
    "Capturing screenshot",
    "Done",
    "Error",
    "Generating image",
    "Saving to desktop",
    "Sending file",
    "Thinking",
    "Uploading",
}


def _build_daemon(monkeypatch, tmp_path, *, token="test-token", chat_id="7317769764"):
    monkeypatch.setenv("KESTREL_HOME", str(tmp_path / ".kestrel"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    monkeypatch.setenv("TELEGRAM_MODE", "polling")
    monkeypatch.setattr(daemon_core_impl, "MacOSKeychainCredentialStore", lambda: object())
    instance = daemon.KestrelDaemon()
    instance._sync_telegram_channel_state_from_environment()
    return instance


def _build_telegram_message_capture(sent_texts, status_messages):
    async def capture_message(_chat_id, text, **_kwargs):
        if text in _TELEGRAM_STATUS_TEXTS:
            status_messages.append(text)
        else:
            sent_texts.append(text)
        return {"message_id": len(sent_texts) + len(status_messages)}

    return capture_message


def test_daemon_syncs_telegram_state_from_environment(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)

    payload = json.loads(instance._channel_state_path().read_text(encoding="utf-8"))
    telegram = payload["telegram"]

    assert telegram["config"]["token"] == "test-token"
    assert telegram["config"]["mode"] == "polling"
    assert telegram["state"]["mappings"][0]["chatId"] == 7317769764

    status = instance._read_channel_status()["channels"]["telegram"]
    assert status["configured"] is True
    assert status["mode"] == "polling"
    assert status["allowed_chat_ids"] == ["7317769764"]


def test_daemon_exposes_skill_pack_catalog(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)

    result = asyncio.run(instance._dispatch("skill.list", {"include_synthetic": False}))

    assert "snapshot_id" in result
    assert isinstance(result["packs"], list)


def test_daemon_task_start_preserves_chat_kind_and_history(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["task_id"] = task_id
        captured["goal"] = goal
        captured["kind"] = kind
        captured["history"] = list(history or [])
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Refined search complete.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [],
            },
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Refined search complete.",
            provider="fake",
            model="model",
        )

    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)

    async def scenario():
        result = await instance._dispatch(
            "task.start",
            {
                "goal": "Budget tracker",
                "kind": "chat",
                "history": [
                    {
                        "role": "user",
                        "content": "Can you look for a skill that can help me manage my financial life?",
                    },
                    {
                        "role": "assistant",
                        "content": "Do you want me to download that skill or search for something else?",
                    },
                ],
            },
        )
        task_id = result["task"]["id"]
        await instance.active_tasks[task_id]

    asyncio.run(scenario())

    assert captured["goal"] == "Budget tracker"
    assert captured["kind"] == "chat"
    assert captured["history"] == [
        {
            "role": "user",
            "content": "Can you look for a skill that can help me manage my financial life?",
        },
        {
            "role": "assistant",
            "content": "Do you want me to download that skill or search for something else?",
        },
    ]


def test_daemon_telegram_poll_once_updates_offset(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    seen_updates: list[dict] = []

    async def fake_request(method, payload, **_kwargs):
        assert method == "getUpdates"
        assert payload["offset"] == 0
        return [
            {
                "update_id": 42,
                "message": {
                    "message_id": 7,
                    "text": "hello",
                    "chat": {"id": 7317769764, "type": "private"},
                    "from": {"id": 7317769764, "is_bot": False},
                },
            }
        ]

    monkeypatch.setattr(instance, "_telegram_api_request", fake_request)
    monkeypatch.setattr(instance, "_handle_telegram_update", lambda update: seen_updates.append(update) or True)

    handled = asyncio.run(instance._telegram_poll_once())

    assert handled == 1
    assert seen_updates[0]["update_id"] == 42
    payload = json.loads(instance._channel_state_path().read_text(encoding="utf-8"))
    assert payload["telegram"]["state"]["pollingOffset"] == 43


def test_daemon_processes_telegram_chat_via_native_runner(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_texts: list[str] = []
    status_messages: list[str] = []
    captured: dict[str, object] = {}

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["goal"] = goal
        captured["history"] = list(history or [])
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Telegram reply",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [],
            },
            metadata={"initial_tool_call": initial_tool_call or {}},
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Telegram reply",
            provider="fake",
            model="model",
        )

    async def capture_text(chat_id, text, **_kwargs):
        assert chat_id == "7317769764"
        sent_texts.append(text)

    async def noop(*_args, **_kwargs):
        return None

    async def fake_download(_message):
        return [], []

    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, status_messages))
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 12,
                "text": "say hi from Telegram",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {
                    "message_id": 11,
                    "text": "Previous bot reply",
                    "from_id": "7913484493",
                    "from_username": "KestrelDevbot",
                    "from_is_bot": True,
                    "attachments": [],
                },
            }
        )
    )

    task = instance.state_store.list_tasks(limit=1)[0]
    assert task["goal"] == "say hi from Telegram"
    assert task["metadata"]["source"] == "telegram"
    assert task["metadata"]["telegram_chat_id"] == "7317769764"
    assert task["metadata"]["telegram_result_message_ids"] == [2]
    assert task["metadata"]["telegram_last_reply_text"] == "Telegram reply"
    assert status_messages == ["Thinking"]
    assert sent_texts[-1] == "Telegram reply"
    assert captured["history"] == [{"role": "assistant", "content": "Previous bot reply"}]


def test_daemon_telegram_chat_uses_recent_same_chat_history_without_explicit_reply(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_texts: list[str] = []
    status_messages: list[str] = []
    captured: dict[str, object] = {}
    prior_task = instance.state_store.create_task(
        goal="Can you find me a budgeting skill?",
        kind="chat",
        metadata={
            "source": "telegram",
            "telegram_chat_id": "7317769764",
            "telegram_message_id": 10,
            "telegram_original_text": "Can you find me a budgeting skill?",
        },
    )
    instance.state_store.update_task(
        prior_task["id"],
        status="completed",
        result={
            "message": "I found a budgeting skill you can try.",
            "provider": "fake",
            "model": "model",
            "plan": None,
            "artifacts": [],
        },
        metadata={"telegram_result_message_ids": [11]},
    )

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["goal"] = goal
        captured["history"] = list(history or [])
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Telegram reply",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [],
            },
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Telegram reply",
            provider="fake",
            model="model",
        )

    async def capture_text(chat_id, text, **_kwargs):
        assert chat_id == "7317769764"
        sent_texts.append(text)

    async def noop(*_args, **_kwargs):
        return None

    async def fake_download(_message):
        return [], []

    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, status_messages))
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 12,
                "text": "What about investing?",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {},
            }
        )
    )

    assert status_messages == ["Thinking"]
    assert sent_texts[-1] == "Telegram reply"
    assert captured["history"] == [
        {"role": "user", "content": "Can you find me a budgeting skill?"},
        {"role": "assistant", "content": "I found a budgeting skill you can try."},
    ]


def test_daemon_handle_client_suppresses_broken_pipe_during_response(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    logged_exceptions: list[tuple[object, ...]] = []
    dispatched: list[tuple[str, dict[str, object]]] = []

    class FakeReader:
        async def readline(self):
            return b'{"request_id":"req-1","method":"status","params":{}}\n'

    class FakeWriter:
        def __init__(self):
            self.payloads: list[bytes] = []
            self.closed = False
            self.drain_calls = 0
            self.wait_closed_calls = 0

        def write(self, payload):
            self.payloads.append(payload)

        async def drain(self):
            self.drain_calls += 1
            raise BrokenPipeError("client disconnected")

        def close(self):
            self.closed = True

        async def wait_closed(self):
            self.wait_closed_calls += 1
            raise BrokenPipeError("already closed")

    async def fake_dispatch(method, params):
        dispatched.append((method, dict(params)))
        return {"status": "running"}

    monkeypatch.setattr(instance, "_dispatch", fake_dispatch)
    monkeypatch.setattr(daemon_core_impl.LOGGER, "exception", lambda *args, **kwargs: logged_exceptions.append(args))

    writer = FakeWriter()
    asyncio.run(instance._handle_client(FakeReader(), writer))

    assert dispatched == [("status", {})]
    assert writer.drain_calls == 1
    assert writer.wait_closed_calls == 1
    assert writer.closed is True
    assert logged_exceptions == []


def test_daemon_processes_structured_output_failure_as_normal_telegram_task_failure(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_texts: list[str] = []
    status_messages: list[str] = []
    edited_statuses: list[str] = []

    class FakeRunner:
        async def run(
            self,
            *,
            goal,
            history=None,
            task_id="",
            task_kind="task",
            initial_tool_call=None,
            resume_state=None,
            approved=False,
        ):
            raise native_models_impl.StructuredModelOutputError(
                repair_label="planner",
                response_text='{ "action": "tool_call", // invalid',
            )

    async def fake_download(_message):
        return [], []

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)
        return None

    async def capture_edit(_chat_id, _message_id, text, **_kwargs):
        edited_statuses.append(text)
        return True

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_build_agent_runner", lambda *, task_id="", workspace_id=None: FakeRunner())
    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, status_messages))
    monkeypatch.setattr(instance, "_telegram_edit_message_text", capture_edit)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 31,
                "text": "say hi from Telegram",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {},
            }
        )
    )

    task = instance.state_store.list_tasks(limit=1)[0]
    assert task["status"] == "failed"
    assert "planner" in str(task.get("error") or "")
    assert status_messages == ["Thinking"]
    assert edited_statuses == ["Error"]
    assert sent_texts
    assert sent_texts[-1].startswith("Couldn't finish that: Failed to parse planner model response after repair.")
    assert "Kestrel hit an error while processing that message." not in sent_texts


def test_daemon_processes_telegram_attachment_prompt(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    captured: dict[str, object] = {}

    async def fake_download(_message):
        return (
            [
                {
                    "type": "telegram_attachment",
                    "telegram_type": "document",
                    "mime_type": "text/plain",
                    "path": str(tmp_path / "artifact.txt"),
                    "source": "telegram",
                    "size": 42,
                    "message_id": 22,
                }
            ],
            [],
        )

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["goal"] = goal
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Reviewed attachment.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [],
            },
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Reviewed attachment.",
            provider="fake",
            model="model",
        )

    async def noop(*_args, **_kwargs):
        return None

    async def noop_message(*_args, **_kwargs):
        return {"message_id": 1}

    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(instance, "_telegram_send_text", noop)
    monkeypatch.setattr(instance, "_telegram_send_message", noop_message)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 22,
                "text": "",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [{"type": "document", "file_id": "doc-1"}],
                "reply": {},
            }
        )
    )

    assert "Review the attached Telegram file or image" in str(captured["goal"])
    assert str(tmp_path / "artifact.txt") in str(captured["goal"])


def test_daemon_routes_telegram_image_attachment_to_multimodal_helper(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    captured: dict[str, object] = {}
    sent_messages: list[str] = []

    async def fake_download(_message):
        return (
            [
                {
                    "type": "telegram_attachment",
                    "telegram_type": "photo",
                    "mime_type": "image/jpeg",
                    "path": str(image_path),
                    "source": "telegram",
                    "size": 42,
                    "message_id": 23,
                }
            ],
            [],
        )

    async def fake_route_local_image_request(*, prompt, image_paths, config, history=None, temperature=0.1, max_tokens=1200, timeout_seconds=180):
        captured["prompt"] = prompt
        captured["image_paths"] = list(image_paths)
        captured["history"] = list(history or [])
        return {
            "provider": "lmstudio",
            "model": "qwen3.5-9b",
            "action": "respond",
            "reply_text": "The image shows a small test photo.",
        }

    async def fail_run(*_args, **_kwargs):
        raise AssertionError("planner path should not run for Telegram image attachments")

    async def fail_inspect(*_args, **_kwargs):
        raise AssertionError("inspection path should reuse routed multimodal response")

    async def capture_message(_chat_id, text, **_kwargs):
        sent_messages.append(text)
        return {"message_id": len(sent_messages)}

    async def capture_text(_chat_id, text, **_kwargs):
        sent_messages.append(text)
        return None

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_run_native_agent_task", fail_run)
    monkeypatch.setattr(daemon_telegram_io_impl, "route_local_image_request", fake_route_local_image_request)
    monkeypatch.setattr(daemon_telegram_io_impl, "inspect_local_images", fail_inspect)
    monkeypatch.setattr(instance, "_telegram_send_message", capture_message)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 23,
                "text": "",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [{"type": "photo", "file_id": "img-1"}],
                "reply": {},
            }
        )
    )

    task = instance.state_store.list_tasks(limit=1)[0]
    assert task["status"] == "completed"
    assert task["result"]["model"] == "qwen3.5-9b"
    assert task["metadata"]["telegram_multimodal_route"] is True
    assert task["metadata"]["telegram_multimodal_action"] == "respond"
    assert captured["image_paths"] == [str(image_path)]
    assert "Describe the attached image" in str(captured["prompt"])
    assert sent_messages[0] == "Analyzing image"
    assert sent_messages[-1] == "The image shows a small test photo."


def test_daemon_routes_telegram_image_generation_request_to_generate_image(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    captured: dict[str, object] = {}
    sent_messages: list[str] = []

    async def fake_download(_message):
        return (
            [
                {
                    "type": "telegram_attachment",
                    "telegram_type": "photo",
                    "mime_type": "image/jpeg",
                    "path": str(image_path),
                    "source": "telegram",
                    "size": 42,
                    "message_id": 24,
                }
            ],
            [],
        )

    async def fake_route_local_image_request(*, prompt, image_paths, config, history=None, temperature=0.1, max_tokens=1200, timeout_seconds=180):
        captured["router_prompt"] = prompt
        captured["image_paths"] = list(image_paths)
        return {
            "provider": "lmstudio",
            "model": "qwen3.5-9b",
            "action": "generate_image",
            "generation_prompt": "Editorial caricature portrait of a man with curly dark hair, full beard, rectangular black glasses, and exaggerated wide eyes, bold ink lines, colorful magazine illustration.",
            "negative_prompt": "blurry, low quality, extra faces",
            "media_type": "image",
            "init_image_creativity": 0.71,
        }

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["goal"] = goal
        captured["initial_tool_call"] = dict(initial_tool_call or {})
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Generated caricature.",
                "provider": "fake",
                "model": "generate_image",
                "plan": None,
                "artifacts": [],
            },
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Generated caricature.",
            provider="fake",
            model="generate_image",
        )

    async def fail_inspect(*_args, **_kwargs):
        raise AssertionError("inspection path should not run for image-generation requests")

    async def capture_message(_chat_id, text, **_kwargs):
        sent_messages.append(text)
        return {"message_id": len(sent_messages)}

    async def capture_text(_chat_id, text, **_kwargs):
        sent_messages.append(text)
        return None

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(daemon_telegram_io_impl, "route_local_image_request", fake_route_local_image_request)
    monkeypatch.setattr(daemon_telegram_io_impl, "inspect_local_images", fail_inspect)
    monkeypatch.setattr(instance, "_telegram_send_message", capture_message)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 24,
                "text": "Turn me into a caricature",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [{"type": "photo", "file_id": "img-2"}],
                "reply": {},
            }
        )
    )

    task = instance.state_store.list_tasks(limit=1)[0]
    assert task["status"] == "completed"
    assert task["metadata"]["telegram_multimodal_route"] is True
    assert task["metadata"]["telegram_multimodal_action"] == "generate_image"
    assert task["metadata"]["telegram_generation_prompt"].startswith("Editorial caricature portrait")
    assert task["metadata"]["telegram_source_image_path"] == str(image_path)
    assert task["metadata"]["telegram_init_image_creativity"] == 0.71
    assert captured["image_paths"] == [str(image_path)]
    assert captured["goal"].startswith("Turn me into a caricature")
    assert captured["initial_tool_call"]["tool_name"] == "generate_image"
    assert captured["initial_tool_call"]["arguments"]["prompt"].startswith("Editorial caricature portrait")
    assert captured["initial_tool_call"]["arguments"]["negative_prompt"] == "blurry, low quality, extra faces"
    assert captured["initial_tool_call"]["arguments"]["media_type"] == "image"
    assert captured["initial_tool_call"]["arguments"]["source_image_path"] == str(image_path)
    assert captured["initial_tool_call"]["arguments"]["init_image_creativity"] == 0.71
    assert captured["initial_tool_call"]["arguments"]["send_to_telegram"] is False
    assert sent_messages[0] == "Generating image"
    assert sent_messages[-1] == "Generated caricature."


def test_daemon_routes_recent_telegram_image_reference_to_desktop_copy(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    image_path = tmp_path / "photo.jpg"
    image_path.write_bytes(b"\xff\xd8\xff\xd9")
    captured: dict[str, object] = {}
    sent_texts: list[str] = []
    status_messages: list[str] = []
    prior_task = instance.state_store.create_task(
        goal="Review the attached Telegram file or image and respond to the user.",
        kind="chat",
        metadata={
            "source": "telegram",
            "telegram_chat_id": "7317769764",
            "telegram_message_id": 14578,
            "telegram_attachments": [
                {
                    "type": "telegram_attachment",
                    "telegram_type": "photo",
                    "mime_type": "image/jpeg",
                    "path": str(image_path),
                    "source": "telegram",
                    "size": 42,
                    "message_id": 14578,
                }
            ],
        },
    )
    instance.state_store.update_task(prior_task["id"], status="completed")

    async def fake_download(_message):
        return [], []

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["goal"] = goal
        captured["initial_tool_call"] = dict(initial_tool_call or {})
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Queued copy to desktop.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [],
            },
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Queued copy to desktop.",
            provider="fake",
            model="model",
        )

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, status_messages))
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 14584,
                "text": "Can you save that image to my desktop?",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {},
            }
        )
    )

    task = instance.state_store.list_tasks(limit=1)[0]
    assert task["status"] == "completed"
    assert captured["initial_tool_call"]["tool_name"] == "copy_local_file"
    assert captured["initial_tool_call"]["arguments"]["source_path"] == str(image_path)
    assert captured["initial_tool_call"]["arguments"]["destination_path"].endswith("/Desktop/photo.jpg")
    assert status_messages[0] == "Saving to desktop"
    assert sent_texts[-1] == "Queued copy to desktop."


def test_daemon_routes_desktop_file_request_to_telegram_send(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    instance = _build_daemon(monkeypatch, tmp_path)
    desktop = tmp_path / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    file_path = desktop / "hand.png"
    file_path.write_bytes(b"fake-image-bytes")
    captured: dict[str, object] = {}
    sent_texts: list[str] = []
    status_messages: list[str] = []

    async def fake_download(_message):
        return [], []

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["goal"] = goal
        captured["initial_tool_call"] = dict(initial_tool_call or {})
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Sent hand.png.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [{"type": "file", "path": str(file_path), "name": file_path.name}],
            },
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Sent hand.png.",
            provider="fake",
            model="model",
            artifacts=[{"type": "file", "path": str(file_path), "name": file_path.name}],
        )

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, status_messages))
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 14585,
                "text": "There's a file on my desktop called hand.png I want you to send it to me on telegram here",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {},
            }
        )
    )

    task = instance.state_store.list_tasks(limit=1)[0]
    assert task["status"] == "completed"
    assert captured["initial_tool_call"]["tool_name"] == "send_local_file_to_telegram"
    assert captured["initial_tool_call"]["arguments"]["path"] == str(file_path)
    assert captured["initial_tool_call"]["arguments"]["send_to_telegram"] is False
    assert status_messages[0] == "Sending file"
    assert sent_texts[-1] == "Sent hand.png."


def test_daemon_routes_closest_desktop_file_match_to_telegram_send(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    instance = _build_daemon(monkeypatch, tmp_path)
    desktop = tmp_path / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    similar_path = desktop / "hand_image.png"
    similar_path.write_bytes(b"fake-image-bytes")
    captured: dict[str, object] = {}
    sent_texts: list[str] = []
    status_messages: list[str] = []

    async def fake_download(_message):
        return [], []

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["initial_tool_call"] = dict(initial_tool_call or {})
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Sent hand_image.png as the closest match.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [{"type": "file", "path": str(similar_path), "name": similar_path.name}],
            },
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Sent hand_image.png as the closest match.",
            provider="fake",
            model="model",
            artifacts=[{"type": "file", "path": str(similar_path), "name": similar_path.name}],
        )

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, status_messages))
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 14586,
                "text": "There's a file on my desktop called hand.png I want you to send it to me on telegram here",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {},
            }
        )
    )

    task = instance.state_store.list_tasks(limit=1)[0]
    assert task["status"] == "completed"
    assert captured["initial_tool_call"]["tool_name"] == "send_local_file_to_telegram"
    assert captured["initial_tool_call"]["arguments"]["path"] == str(similar_path)
    assert captured["initial_tool_call"]["arguments"]["requested_name"] == "hand.png"
    assert status_messages[0] == "Sending file"
    assert sent_texts[-1] == "Sent hand_image.png as the closest match."


def test_daemon_routes_keyword_desktop_file_search_to_telegram_send(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    instance = _build_daemon(monkeypatch, tmp_path)
    desktop = tmp_path / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    matching_path = desktop / "hand_image.png"
    matching_path.write_bytes(b"fake-image-bytes")
    captured: dict[str, object] = {}
    sent_texts: list[str] = []
    status_messages: list[str] = []

    async def fake_download(_message):
        return [], []

    async def fake_run(task_id, goal, *, kind, history=None, initial_tool_call=None, resume_state=None, approved=False):
        captured["initial_tool_call"] = dict(initial_tool_call or {})
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Sent hand_image.png.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [{"type": "file", "path": str(matching_path), "name": matching_path.name}],
            },
        )
        return native.NativeAgentOutcome(
            status="completed",
            message="Sent hand_image.png.",
            provider="fake",
            model="model",
            artifacts=[{"type": "file", "path": str(matching_path), "name": matching_path.name}],
        )

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, status_messages))
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 14587,
                "text": "Look for a file on my desktop that includes the word hand and send it to me in telegram",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {},
            }
        )
    )

    task = instance.state_store.list_tasks(limit=1)[0]
    assert task["status"] == "completed"
    assert captured["initial_tool_call"]["tool_name"] == "send_local_file_to_telegram"
    assert captured["initial_tool_call"]["arguments"]["path"] == str(matching_path)
    assert captured["initial_tool_call"]["arguments"]["requested_name"] == "hand"
    assert status_messages[0] == "Sending file"
    assert sent_texts[-1] == "Sent hand_image.png."


def test_daemon_telegram_approve_command_resumes_task(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_texts: list[str] = []
    status_messages: list[str] = []

    task = instance.state_store.create_task(goal="create a README section", kind="task")
    approval = instance.state_store.create_approval(
        task_id=task["id"],
        operation="file_write",
        command="Write README section",
        resume={"state": {"goal": task["goal"], "plan": {"steps": []}}},
    )

    async def fake_resume(task_id, approval_payload):
        assert approval_payload["id"] == approval["id"]
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Approved and completed.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [],
            },
        )

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_resume_task_from_approval", fake_resume)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, status_messages))
    monkeypatch.setattr(instance, "_telegram_edit_message_text", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_send_delayed_working_note", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 30,
                "text": f"/approve {approval['id'][:8]}",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {},
            }
        )
    )

    assert instance.state_store.list_pending_approvals() == []
    assert any("Approved" in text for text in sent_texts)
    assert any("Approved and completed." in text for text in sent_texts)


def test_daemon_formats_pending_approvals_cleanly(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_texts: list[str] = []

    task = instance.state_store.create_task(goal="write docs", kind="task")
    approval = instance.state_store.create_approval(
        task_id=task["id"],
        operation="file_write",
        command="Write README section",
    )

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_chat_action", noop)

    asyncio.run(
        instance._process_telegram_message(
            {
                "chat_id": "7317769764",
                "chat_type": "private",
                "message_id": 31,
                "text": "/approvals",
                "from_id": "7317769764",
                "from_username": "tiuni",
                "first_name": "Tiuni",
                "attachments": [],
                "reply": {},
            }
        )
    )

    output = sent_texts[-1]
    assert approval["id"][:8] in output
    assert "Task: write docs" in output
    assert f"/approve {approval['id'][:8]}" in output


def test_daemon_sends_delayed_working_note(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_texts: list[str] = []
    task = instance.state_store.create_task(goal="slow task", kind="chat")
    instance.state_store.update_task(task["id"], status="running")

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)

    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)

    asyncio.run(
        instance._telegram_send_delayed_working_note(
            "7317769764",
            task["id"],
            reply_to_message_id=40,
            delay_seconds=0,
        )
    )

    assert sent_texts == ["Working..."]


def test_daemon_sends_full_completed_telegram_reply_without_artifacts(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_messages: list[str] = []
    edited_texts: list[str] = []
    long_message = "A" * 4501
    task = instance.state_store.create_task(goal="long reply", kind="chat")
    instance.state_store.update_task(
        task["id"],
        status="completed",
        result={
            "message": long_message,
            "provider": "fake",
            "model": "model",
            "plan": None,
            "artifacts": [],
        },
        metadata={"telegram_status_message_id": 77},
    )

    async def capture_message(_chat_id, text, **_kwargs):
        sent_messages.append(text)
        return {"message_id": len(sent_messages)}

    async def capture_edit(_chat_id, _message_id, text, **_kwargs):
        edited_texts.append(text)
        return True

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_telegram_send_message", capture_message)
    monkeypatch.setattr(instance, "_telegram_edit_message_text", capture_edit)
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)

    asyncio.run(
        instance._telegram_send_task_result(
            "7317769764",
            task["id"],
            reply_to_message_id=55,
        )
    )

    assert edited_texts == ["Uploading", "Done"]
    assert len(sent_messages) == 2
    assert "".join(sent_messages) == long_message


def test_daemon_processes_telegram_callback_approval(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_texts: list[str] = []
    edited_texts: list[str] = []
    callback_answers: list[str] = []

    task = instance.state_store.create_task(goal="create a README section", kind="task")
    approval = instance.state_store.create_approval(
        task_id=task["id"],
        operation="file_write",
        command="Write README section",
        resume={"state": {"goal": task["goal"], "plan": {"steps": []}}},
    )

    async def fake_resume(task_id, approval_payload):
        assert approval_payload["id"] == approval["id"]
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Approved and completed.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [],
            },
        )

    async def capture_text(_chat_id, text, **_kwargs):
        sent_texts.append(text)

    async def capture_edit(_chat_id, _message_id, text, **_kwargs):
        edited_texts.append(text)
        return True

    async def capture_answer(_callback_id, text=""):
        callback_answers.append(text)

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(instance, "_resume_task_from_approval", fake_resume)
    monkeypatch.setattr(instance, "_telegram_send_text", capture_text)
    monkeypatch.setattr(instance, "_telegram_send_message", _build_telegram_message_capture(sent_texts, []))
    monkeypatch.setattr(instance, "_telegram_send_artifacts", noop)
    monkeypatch.setattr(instance, "_telegram_update_task_status", noop)
    monkeypatch.setattr(instance, "_telegram_edit_message_text", capture_edit)
    monkeypatch.setattr(instance, "_telegram_answer_callback_query", capture_answer)

    asyncio.run(
        instance._process_telegram_callback(
            {
                "callback_id": "cb-1",
                "data": f"approval:approve:{approval['id']}",
                "chat_id": "7317769764",
                "message_id": 44,
                "from_id": "7317769764",
            }
        )
    )

    assert callback_answers == ["Approving..."]
    assert any("Approved" in text for text in edited_texts)
    assert any("Approved and completed." in text for text in sent_texts)


def test_daemon_heartbeat_persists_background_suggestion_in_suggest_first(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    instance.config.setdefault("agent", {}).setdefault("proactivity", {})["background_execution"] = "suggest_first"
    watched_file = tmp_path / "workspace" / "notes.md"
    watched_file.parent.mkdir(parents=True, exist_ok=True)
    watched_file.write_text("change", encoding="utf-8")
    instance.recent_watched_changes = [str(watched_file)]
    notifications: list[dict[str, object]] = []

    async def capture_notification(**kwargs):
        notifications.append(dict(kwargs))
        return {"id": "notification"}

    monkeypatch.setattr(instance, "_record_and_mirror_notification", capture_notification)

    asyncio.run(instance._run_proactive_heartbeat())

    suggestions = instance.state_store.list_background_suggestions(status="pending")
    assert len(suggestions) == 1
    assert suggestions[0]["status"] == "pending"
    assert suggestions[0]["goal"]
    assert instance.active_tasks == {}
    assert notifications[0]["data"]["suggestion_id"] == suggestions[0]["id"]


def test_daemon_accept_background_suggestion_starts_task(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    suggestion = instance.state_store.upsert_background_suggestion(
        suggestion_id="suggestion-1",
        workspace_id="local",
        title="Background review available",
        body="Prepared a review for changed files.",
        goal="Review recent workspace changes without editing files.",
        source="watched_changes",
        fingerprint="fp-1",
        notification_type="info",
        task_kind="task",
        auto_start_allowed=False,
    )
    captured: dict[str, object] = {}

    async def fake_execute(task_id, goal, *, kind="task", history=None):
        captured["task_id"] = task_id
        captured["goal"] = goal
        captured["kind"] = kind
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={
                "message": "Background review complete.",
                "provider": "fake",
                "model": "model",
                "plan": None,
                "artifacts": [],
            },
        )

    async def capture_notification(**_kwargs):
        return {"id": "notification"}

    monkeypatch.setattr(instance, "_execute_task", fake_execute)
    monkeypatch.setattr(instance, "_record_and_mirror_notification", capture_notification)

    async def scenario():
        result = await instance._dispatch(
            "suggestion.resolve",
            {"suggestion_id": suggestion["id"], "action": "accept"},
        )
        await instance.active_tasks[result["task"]["id"]]
        return result

    result = asyncio.run(scenario())

    resolved = instance.state_store.get_background_suggestion(suggestion["id"])
    assert resolved["status"] == "accepted"
    assert resolved["task_id"] == result["task"]["id"]
    assert captured["kind"] == "task"
    assert instance.state_store.list_learning_events(event_type="suggestion_accepted", limit=5)


def test_daemon_research_start_creates_research_session(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)

    async def fake_execute(task_id, goal, *, kind="task", history=None):
        instance.state_store.update_task(
            task_id,
            status="completed",
            result={"message": f"Completed {goal}", "provider": "fake", "model": "model", "plan": None, "artifacts": []},
        )

    monkeypatch.setattr(instance, "_execute_task", fake_execute)

    async def scenario():
        result = await instance._dispatch("research.start", {"prompt": "Research local-first model routing"})
        await instance.active_tasks[result["task"]["id"]]
        return result

    result = asyncio.run(scenario())

    session = result["research_session"]
    assert result["task"]["kind"] == "research"
    assert session["task_id"] == result["task"]["id"]
    assert Path(session["notebook_path"]).exists()
    assert "Research local-first model routing" in Path(session["notebook_path"]).read_text(encoding="utf-8")


def test_daemon_completed_research_persists_sessions_artifacts_and_procedures(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    task = instance.state_store.create_task(
        goal="Research local-first model routing",
        kind="research",
        metadata={"workspace_id": "local", "source": "research"},
    )

    report_path = tmp_path / "report.md"
    report_path.write_text("# report\n", encoding="utf-8")

    class FakeRunner:
        async def run(self, **_kwargs):
            return native.NativeAgentOutcome(
                status="completed",
                message="Research complete with citations.",
                provider="fake",
                model="model",
                plan={
                    "summary": "Research local-first routing",
                    "steps": [
                        {"description": "Collect sources", "success_criteria": "Sources collected", "preferred_tools": ["fetch_url"]},
                        {"description": "Synthesize findings", "success_criteria": "Summary written", "preferred_tools": []},
                    ],
                },
                artifacts=[{"type": "report", "path": str(report_path), "mime_type": "text/markdown"}],
                state={
                    "tool_evidence": [
                        {
                            "tool_name": "fetch_url",
                            "success": True,
                            "data": {
                                "url": "https://example.com/research",
                                "status_code": 200,
                                "content_type": "text/html",
                                "body": "Evidence body",
                            },
                        }
                    ],
                    "verifier_result": {
                        "ok": True,
                        "final_response": "Research complete with citations.",
                        "reason": "Grounded in source evidence.",
                    },
                },
            )

    monkeypatch.setattr(instance, "_build_agent_runner", lambda *, task_id="", workspace_id="": FakeRunner())

    asyncio.run(instance._run_native_agent_task(task["id"], task["goal"], kind="research"))

    sessions = instance.state_store.list_research_sessions(limit=5)
    assert len(sessions) == 1
    session = sessions[0]
    assert session["status"] == "completed"
    assert session["sources"][0]["url"] == "https://example.com/research"
    assert Path(session["sources"][0]["snapshot_path"]).exists()

    artifacts = instance.state_store.list_artifact_manifests(task["id"])
    assert any(item["artifact_type"] == "report" for item in artifacts)
    assert any(item["artifact_type"] == "research_source_snapshot" for item in artifacts)

    procedures = instance.state_store.list_procedures(limit=5)
    assert len(procedures) == 1
    assert procedures[0]["source_task_id"] == task["id"]

    learning_events = instance.state_store.list_learning_events(limit=10)
    event_types = {item["event_type"] for item in learning_events}
    assert "task_completed" in event_types
    assert "procedure_learned" in event_types
