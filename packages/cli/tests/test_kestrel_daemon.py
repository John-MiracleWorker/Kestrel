import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import kestrel_daemon as daemon
import kestrel_native as native
from kestrel_cli import daemon_core as daemon_core_impl


def _build_daemon(monkeypatch, tmp_path, *, token="test-token", chat_id="7317769764"):
    monkeypatch.setenv("KESTREL_HOME", str(tmp_path / ".kestrel"))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", chat_id)
    monkeypatch.setenv("TELEGRAM_MODE", "polling")
    monkeypatch.setattr(daemon_core_impl, "MacOSKeychainCredentialStore", lambda: object())
    instance = daemon.KestrelDaemon()
    instance._sync_telegram_channel_state_from_environment()
    return instance


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
    assert sent_texts[-1] == "Telegram reply"
    assert captured["history"] == [{"role": "assistant", "content": "Previous bot reply"}]


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

    monkeypatch.setattr(instance, "_download_telegram_attachments", fake_download)
    monkeypatch.setattr(instance, "_run_native_agent_task", fake_run)
    monkeypatch.setattr(instance, "_telegram_send_text", noop)
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


def test_daemon_telegram_approve_command_resumes_task(monkeypatch, tmp_path):
    instance = _build_daemon(monkeypatch, tmp_path)
    sent_texts: list[str] = []

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

    assert sent_texts == ["Working on it. I'll reply here when it's ready."]
