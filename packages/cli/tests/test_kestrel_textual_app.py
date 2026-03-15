import asyncio
import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from textual.widgets import Button, DataTable, TextArea

from kestrel_cli import cli_core as cli_core_impl
from kestrel_cli.tui.app import KestrelTextualApp
from kestrel_cli.tui.store import TuiStore, VIEW_COCKPIT, VIEW_TASKS


class FakeClient:
    def __init__(self):
        self.approvals_resolved: list[tuple[str, str, bool]] = []
        self.started: list[tuple[str, str]] = []
        self.installed: set[str] = set()
        self.enabled: set[str] = set()
        self.removed: list[str] = []

    async def status(self):
        return {
            "status": "running",
            "home": "/tmp/.kestrel",
            "control_socket": "/tmp/.kestrel/control.sock",
            "channels": {"telegram": {"config": {"token": "abc", "mode": "polling", "workspaceId": "default"}}},
            "recent_tasks": await self.list_tasks(),
            "pending_approvals": await self.list_pending_approvals(),
        }

    async def runtime_profile(self):
        return {
            "runtime_mode": "native",
            "policy_name": "telegram-first",
            "local_models": {
                "default_provider": "lmstudio",
                "default_model": "ministral-3-14b-instruct-2512",
            },
        }

    async def list_tasks(self, status=None):
        tasks = [
            {
                "id": "task-1",
                "goal": "Review the latest image attachment",
                "status": "waiting",
                "kind": "task",
                "created_at": "2026-03-15T02:00:00Z",
                "metadata": {"provider": "lmstudio", "model": "qwen3.5-9b"},
            },
            {
                "id": "task-live",
                "goal": "Summarize the current workspace health",
                "status": "completed",
                "kind": "chat",
                "created_at": "2026-03-15T02:03:00Z",
                "metadata": {"provider": "lmstudio", "model": "ministral-3-14b-instruct-2512"},
            },
        ]
        if status:
            tasks = [task for task in tasks if task["status"] == status]
        return {"tasks": tasks}

    async def list_pending_approvals(self):
        approvals = []
        if not self.approvals_resolved:
            approvals.append(
                {
                    "id": "apr-1",
                    "task_id": "task-1",
                    "operation": "shell",
                    "command": "cp /tmp/source.jpg ~/Desktop/source.jpg",
                    "status": "pending",
                    "created_at": "2026-03-15T02:01:00Z",
                    "payload": {"cwd": "/Users/tiuni/kestrel"},
                    "resume": {"state": {"step": "copy_local_file"}},
                }
            )
        return {"approvals": approvals}

    async def task_detail(self, task_id):
        payloads = {
            "task-1": {
                "id": "task-1",
                "goal": "Review the latest image attachment",
                "status": "waiting",
                "kind": "task",
                "created_at": "2026-03-15T02:00:00Z",
                "metadata": {"provider": "lmstudio", "model": "qwen3.5-9b"},
                "result": {},
            },
            "task-live": {
                "id": "task-live",
                "goal": "Summarize the current workspace health",
                "status": "completed",
                "kind": "chat",
                "created_at": "2026-03-15T02:03:00Z",
                "metadata": {"provider": "lmstudio", "model": "ministral-3-14b-instruct-2512"},
                "result": {"message": "Workspace is healthy. Two tasks are recent and one approval is pending."},
            },
        }
        return {"task": copy.deepcopy(payloads.get(task_id, {}))}

    async def task_timeline(self, task_id):
        payload = {
            "task-1": [
                {"type": "thinking", "content": "Evaluating the binary image attachment"},
                {"type": "approval_needed", "content": "Need approval to copy the file to Desktop"},
            ],
            "task-live": [
                {"type": "thinking", "content": "Checking runtime and queued work"},
                {"type": "task_complete", "content": "Workspace summary finished"},
            ],
        }
        return {"events": copy.deepcopy(payload.get(task_id, []))}

    async def task_artifacts(self, task_id):
        payload = {
            "task-1": [{"name": "file_7.jpg", "path": "/Users/tiuni/.kestrel/artifacts/telegram/file_7.jpg"}],
            "task-live": [],
        }
        return {"artifacts": copy.deepcopy(payload.get(task_id, []))}

    async def task_approvals(self, task_id, status=None):
        approvals = await self.list_pending_approvals()
        task_approvals = [approval for approval in approvals["approvals"] if approval["task_id"] == task_id]
        return {"approvals": task_approvals}

    async def approve_task(self, task_id, approval_id, approved):
        self.approvals_resolved.append((task_id, approval_id, approved))
        return {"approval": {"id": approval_id, "approved": approved}}

    async def skill_list(self, include_synthetic=True, include_marketplace=True):
        packs = [
            {
                "pack_id": "marketplace-demo-orchestrator",
                "name": "Marketplace Demo Orchestrator",
                "version": "1.0.0",
                "source_type": "marketplace",
                "enabled": "marketplace-demo-orchestrator" in self.enabled,
                "installed": "marketplace-demo-orchestrator" in self.installed,
                "trusted": "marketplace-demo-orchestrator" in self.installed,
                "components": [{"type": "prompt"}, {"type": "native_tool"}],
                "description": "Marketplace pack used to verify install and toggle flows.",
                "prompt_preview": "Activate this pack for operator orchestration.",
            },
            {
                "pack_id": "bundled-runtime-observer",
                "name": "Bundled Runtime Observer",
                "version": "1.2.0",
                "source_type": "bundled",
                "enabled": True,
                "installed": True,
                "trusted": True,
                "components": [{"type": "prompt"}],
                "description": "Always-available runtime monitoring prompts.",
            },
        ]
        return {"packs": copy.deepcopy(packs)}

    async def skill_install(self, *, pack_id="", source_path="", source_url="", scope="user"):
        self.installed.add(pack_id)
        return {"pack": {"pack_id": pack_id}}

    async def skill_enable(self, pack_id):
        self.enabled.add(pack_id)
        return {"pack": {"pack_id": pack_id, "enabled": True}}

    async def skill_disable(self, pack_id):
        self.enabled.discard(pack_id)
        return {"pack": {"pack_id": pack_id, "enabled": False}}

    async def skill_remove(self, pack_id):
        self.installed.discard(pack_id)
        self.enabled.discard(pack_id)
        self.removed.append(pack_id)
        return {"pack": {"pack_id": pack_id}}

    async def start_task(self, goal, workspace_id=None, *, kind="task"):
        self.started.append((goal, kind))
        yield {"type": "thinking", "content": "Analyzing request", "task_id": "task-live"}
        yield {"type": "task_complete", "content": "Completed", "task_id": "task-live"}


def _config(**overrides):
    config = copy.deepcopy(cli_core_impl.DEFAULT_CONFIG)
    config["tui"].update(overrides)
    return config


def test_store_emits_notifications_for_new_approvals():
    store = TuiStore(config=_config())

    changed = store.apply_approvals([{"id": "apr-1", "command": "rm -rf /tmp/demo", "task_id": "task-1"}])

    assert changed is True
    assert store.state.notifications
    assert store.state.notifications[0].title == "Approval required"


def test_store_emits_notifications_for_terminal_task_status():
    store = TuiStore(config=_config())
    store.apply_tasks([{"id": "task-1", "status": "running", "goal": "Initial"}])

    store.apply_tasks([{"id": "task-1", "status": "completed", "goal": "Initial"}])

    assert any(item.title == "Task completed" for item in store.state.notifications)


def test_store_collapses_duplicate_notifications():
    store = TuiStore(config=_config())

    store.add_notification("Task refresh failed", "Native daemon is offline.", level="error")
    store.add_notification("Task refresh failed", "Native daemon is offline.", level="error")

    assert len(store.state.notifications) == 1
    assert store.state.notifications[0].occurrences == 2
    assert store.unread_notifications() == 1


def test_textual_app_boots_and_lands_in_cockpit():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            assert app.store.state.active_view == VIEW_COCKPIT
            assert "ministral" in str(app.query_one("#runtime-summary").renderable).lower()
            assert getattr(app.focused, "id", "") == "cockpit-tasks"

    asyncio.run(scenario())


def test_background_refresh_failures_do_not_spam_notifications():
    class FailingClient(FakeClient):
        async def status(self):
            raise RuntimeError("Control socket not found at /tmp/.kestrel/run/control.sock")

        async def runtime_profile(self):
            raise RuntimeError("Control socket not found at /tmp/.kestrel/run/control.sock")

        async def list_tasks(self, status=None):
            raise RuntimeError("Control socket not found at /tmp/.kestrel/run/control.sock")

        async def list_pending_approvals(self):
            raise RuntimeError("Control socket not found at /tmp/.kestrel/run/control.sock")

        async def skill_list(self, include_synthetic=True, include_marketplace=True):
            raise RuntimeError("Control socket not found at /tmp/.kestrel/run/control.sock")

    async def scenario():
        app = KestrelTextualApp(FailingClient(), _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            assert app.store.unread_notifications() == 0
            assert "Native daemon is offline." in app.store.state.notice_text

    asyncio.run(scenario())


def test_textual_navigation_button_switches_view():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            await pilot.click("#nav-tasks")
            await pilot.pause(0.1)
            assert app.store.state.active_view == VIEW_TASKS
            assert getattr(app.focused, "id", "") == "tasks-table"

    asyncio.run(scenario())


def test_tasks_keyboard_navigation_updates_selection_without_enter():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            await pilot.click("#nav-tasks")
            await pilot.pause(0.1)
            assert app.store.state.selected_task_id == "task-1"
            await pilot.press("down")
            await pilot.pause(0.2)
            assert app.store.state.selected_task_id == "task-live"

    asyncio.run(scenario())


def test_invalid_row_highlight_is_ignored():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            table = app.query_one("#cockpit-tasks", DataTable)
            table.focus()
            before = app.store.state.selected_task_id
            app.on_data_table_row_highlighted(DataTable.RowHighlighted(table, -1, None))  # type: ignore[arg-type]
            assert app.store.state.selected_task_id == before

    asyncio.run(scenario())


def test_command_palette_switches_to_tasks():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            await pilot.press("ctrl+k")
            await pilot.pause(0.1)
            await pilot.press("t", "a", "s", "enter")
            await pilot.pause(0.2)
            assert app.store.state.active_view == VIEW_TASKS

    asyncio.run(scenario())


def test_chat_send_dispatches_and_renders_reply():
    async def scenario():
        client = FakeClient()
        app = KestrelTextualApp(client, _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            await pilot.click("#nav-chat")
            await pilot.pause(0.1)
            composer = app.query_one("#chat-composer", TextArea)
            composer.load_text("Give me a workspace summary")
            await pilot.click("#chat-send")
            await pilot.pause(0.4)
            assert client.started == [("Give me a workspace summary", "chat")]
            assert any(message.role == "assistant" for message in app.store.state.chat_messages)

    asyncio.run(scenario())


def test_approval_resolve_flow_works_from_ui():
    async def scenario():
        client = FakeClient()
        app = KestrelTextualApp(client, _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            await pilot.click("#nav-approvals")
            await pilot.pause(0.1)
            app.query_one("#approval-approve", Button).press()
            await pilot.pause(0.3)
            assert client.approvals_resolved == [("task-1", "apr-1", True)]

    asyncio.run(scenario())


def test_skill_install_and_toggle_from_ui():
    async def scenario():
        client = FakeClient()
        app = KestrelTextualApp(client, _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            await pilot.click("#nav-skills")
            await pilot.pause(0.1)
            app.query_one("#skill-install", Button).press()
            await pilot.pause(0.25)
            assert "marketplace-demo-orchestrator" in client.installed
            app.query_one("#skill-toggle", Button).press()
            await pilot.pause(0.25)
            assert "marketplace-demo-orchestrator" in client.enabled

    asyncio.run(scenario())


def test_compact_mode_hides_docked_inspector_and_shows_details_button():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config())
        async with app.run_test(headless=True, size=(100, 32)) as pilot:
            await pilot.pause(0.35)
            assert app.store.state.compact_mode is True
            assert app.query_one("#details-button", Button).display is True
            assert app.query_one("#inspector").display is False

    asyncio.run(scenario())


def test_palette_overlay_opens_and_closes_without_crashing():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config())
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            await pilot.click("#palette-button")
            await pilot.pause(0.2)
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert len(app.screen_stack) == 1

    asyncio.run(scenario())


def test_details_overlay_opens_and_closes_without_crashing_in_compact_mode():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config())
        async with app.run_test(headless=True, size=(100, 32)) as pilot:
            await pilot.pause(0.35)
            await pilot.click("#details-button")
            await pilot.pause(0.2)
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert len(app.screen_stack) == 1

    asyncio.run(scenario())


def test_reduced_motion_config_is_respected():
    async def scenario():
        app = KestrelTextualApp(FakeClient(), _config(reduced_motion=True))
        async with app.run_test(headless=True, size=(160, 46)) as pilot:
            await pilot.pause(0.35)
            assert app.store.state.reduced_motion is True
            assert app._motion_level() == "off"

    asyncio.run(scenario())
