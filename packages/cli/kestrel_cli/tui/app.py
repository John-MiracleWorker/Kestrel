from __future__ import annotations

import asyncio
import json
import traceback
from functools import partial
from typing import Any

from rich.console import Group
from rich.panel import Panel
from rich.text import Text
from textual.app import App, ComposeResult, ScreenStackError
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, RichLog, Static, TextArea

from ..cli_core import KestrelClient
from .store import (
    VIEW_APPROVALS,
    VIEW_CHAT,
    VIEW_COCKPIT,
    VIEW_SKILLS,
    VIEW_TASKS,
    VIEWS,
    TuiStore,
    component_summary,
    relative_age,
    summarize_event,
)
from .widgets import (
    CommandPaletteScreen,
    InspectorPane,
    MetricCard,
    NotificationCenterScreen,
    PaletteAction,
    ProcessBar,
    StatusOrb,
    render_chat_message,
    render_key_value_table,
)


class InspectorModalScreen(ModalScreen[None]):
    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, *, title: str, subtitle: str, renderables: list[object]):
        super().__init__()
        self.title = title
        self.subtitle = subtitle
        self.renderables = renderables

    def compose(self) -> ComposeResult:
        with Vertical(id="inspector-modal"):
            yield Static(self.title, id="inspector-modal-title")
            yield Static(self.subtitle, id="inspector-modal-subtitle")
            yield RichLog(id="inspector-modal-log", wrap=True, highlight=False, markup=False)

    def on_mount(self) -> None:
        log = self.query_one("#inspector-modal-log", RichLog)
        log.clear()
        for renderable in self.renderables:
            log.write(renderable)


class KestrelTextualApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "Kestrel Operator Cockpit"
    SUB_TITLE = "Telegram-first native control"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        ("1", "view_cockpit", "Cockpit"),
        ("2", "view_chat", "Chat"),
        ("3", "view_tasks", "Tasks"),
        ("4", "view_approvals", "Approvals"),
        ("5", "view_skills", "Skills"),
        ("ctrl+k", "open_palette", "Palette"),
        ("ctrl+n", "open_notifications", "Notifications"),
        ("ctrl+r", "refresh_now", "Refresh"),
        ("ctrl+i", "toggle_details", "Details"),
        ("ctrl+enter", "send_chat", "Send"),
    ]

    def __init__(self, client: KestrelClient, config: dict[str, Any]):
        super().__init__()
        self.client = client
        self.config = config
        self.store = TuiStore(config=config)
        self._running = True
        self._task_filter = ""
        self._tagline_index = 0
        self._suspend_table_events = False
        self._ui_ready = False
        self._taglines = [
            "NATIVE CONTROL // TELEGRAM PRIMARY // TASK STREAMS LIVE",
            "COCKPIT ONLINE // APPROVALS HOT // CHANNELS MIRRORED",
            "LOCAL OPERATOR DECK // SKILLS INDEXED // MODELS READY",
        ]

    def compose(self) -> ComposeResult:
        with Vertical(id="chrome-shell"):
            with Horizontal(id="top-bar"):
                with Vertical(id="brand-lockup"):
                    yield Static("KESTREL // OPERATOR COCKPIT", id="brand-title")
                    yield Static(self._taglines[0], id="brand-subtitle")
                with Horizontal(id="runtime-cluster"):
                    yield StatusOrb(id="status-orb")
                    yield Static("BOOTING LOCAL RUNTIME", id="runtime-summary")
                with Horizontal(id="top-actions"):
                    yield Button("NOTIFICATIONS 0", id="notifications-button", classes="utility-button")
                    yield Button("PALETTE", id="palette-button", classes="utility-button")
                    yield Button("DETAILS", id="details-button", classes="utility-button")
            with Horizontal(id="workspace-shell"):
                with Vertical(id="nav-rail"):
                    yield Static("SECTIONS", classes="nav-heading")
                    yield Button("Cockpit", id="nav-cockpit", classes="nav-button")
                    yield Button("Chat", id="nav-chat", classes="nav-button")
                    yield Button("Tasks", id="nav-tasks", classes="nav-button")
                    yield Button("Approvals", id="nav-approvals", classes="nav-button")
                    yield Button("Skills", id="nav-skills", classes="nav-button")
                with Container(id="workspace-stack"):
                    with Container(id="view-cockpit", classes="view-pane"):
                        with Horizontal(id="cockpit-metrics"):
                            yield MetricCard(id="metric-runtime")
                            yield MetricCard(id="metric-model")
                            yield MetricCard(id="metric-tasks")
                            yield MetricCard(id="metric-approvals")
                            yield MetricCard(id="metric-telegram")
                        yield ProcessBar(id="cockpit-process")
                        with Horizontal(id="cockpit-grid"):
                            with Vertical(classes="surface-panel"):
                                yield Static("RECENT TASKS", classes="section-title")
                                yield DataTable(id="cockpit-tasks")
                            with Vertical(classes="surface-panel"):
                                yield Static("PENDING APPROVALS", classes="section-title")
                                yield DataTable(id="cockpit-approvals")
                    with Container(id="view-chat", classes="view-pane"):
                        yield ProcessBar(id="chat-process")
                        with Horizontal(id="chat-approval-strip", classes="surface-panel"):
                            yield Static("No pending approvals", id="chat-approval-banner")
                            yield Button("Approve", id="chat-approve-inline", variant="success")
                            yield Button("Deny", id="chat-deny-inline", variant="error")
                        yield RichLog(id="chat-log", wrap=True, highlight=False, markup=False, auto_scroll=True)
                        yield Static("Artifacts: none", id="chat-artifacts")
                        with Horizontal(id="chat-composer-shell", classes="surface-panel"):
                            yield TextArea(id="chat-composer")
                            with Vertical(id="chat-composer-actions"):
                                yield Button("Send Chat", id="chat-send", variant="primary")
                                yield Button("Launch Task", id="chat-launch-task", variant="success")
                    with Container(id="view-tasks", classes="view-pane"):
                        with Horizontal(id="tasks-layout"):
                            with Vertical(id="tasks-list-panel", classes="surface-panel"):
                                yield Static("TASKS", classes="section-title")
                                yield Input(placeholder="Filter by goal or status", id="tasks-filter")
                                yield DataTable(id="tasks-table")
                            with Vertical(id="tasks-main-panel", classes="surface-panel"):
                                with Horizontal(id="tasks-actions"):
                                    yield Static("TASK TIMELINE", classes="section-title")
                                    yield Button("Refresh", id="tasks-refresh", classes="utility-button")
                                    yield Button("Approve", id="tasks-approve", variant="success")
                                    yield Button("Deny", id="tasks-deny", variant="error")
                                yield RichLog(id="tasks-timeline", wrap=True, highlight=False, markup=False)
                    with Container(id="view-approvals", classes="view-pane"):
                        with Horizontal(id="approvals-layout"):
                            with Vertical(id="approvals-list-panel", classes="surface-panel"):
                                yield Static("APPROVAL QUEUE", classes="section-title")
                                yield DataTable(id="approvals-table")
                            with Vertical(id="approvals-main-panel", classes="surface-panel"):
                                with Horizontal(id="approval-actions"):
                                    yield Static("COMMAND PREVIEW", classes="section-title")
                                    yield Button("Approve", id="approval-approve", variant="success")
                                    yield Button("Deny", id="approval-deny", variant="error")
                                yield RichLog(id="approval-preview", wrap=True, highlight=False, markup=False)
                    with Container(id="view-skills", classes="view-pane"):
                        with Horizontal(id="skills-layout"):
                            with Vertical(id="skills-list-panel", classes="surface-panel"):
                                yield Static("SKILL LIBRARY", classes="section-title")
                                yield Input(placeholder="Search installed and marketplace skill packs", id="skills-search")
                                yield DataTable(id="skills-table")
                            with Vertical(id="skills-main-panel", classes="surface-panel"):
                                with Horizontal(id="skills-actions"):
                                    yield Static("PACK DETAILS", classes="section-title")
                                    yield Button("Install", id="skill-install", variant="primary")
                                    yield Button("Enable / Disable", id="skill-toggle", classes="utility-button")
                                    yield Button("Remove", id="skill-remove", variant="error")
                                yield RichLog(id="skill-preview", wrap=True, highlight=False, markup=False)
                yield InspectorPane(id="inspector")
            with Horizontal(id="bottom-bar"):
                yield Static("", id="notice-bar")
                yield Static("", id="hint-bar")

    async def on_mount(self) -> None:
        self._configure_tables()
        self._sync_layout()
        self._render_state()
        self.set_interval(0.85, self._tick_brandline)
        await self._refresh_all()
        self._ui_ready = True
        self._focus_primary_control()
        self.run_worker(self._poll_runtime_loop(), name="runtime-poller", group="runtime")
        self.run_worker(self._poll_tasks_loop(), name="task-poller", group="tasks")
        self.run_worker(self._poll_approvals_loop(), name="approval-poller", group="approvals")
        self.run_worker(self._poll_skills_loop(), name="skill-poller", group="skills")

    def on_unmount(self) -> None:
        self._running = False

    def on_resize(self, _event) -> None:
        compact = self.size.width < 148
        if self.store.set_compact_mode(compact):
            self._sync_layout()
            self._render_inspector()

    def _configure_tables(self) -> None:
        ui = self._ui_query_one

        def configure(table: DataTable, *columns: str) -> None:
            table.cursor_type = "row"
            table.zebra_stripes = True
            for column in columns:
                table.add_column(column)

        configure(ui("#cockpit-tasks", DataTable), "ID", "Goal", "Status", "Age")
        configure(ui("#cockpit-approvals", DataTable), "ID", "Command", "Task", "Age")
        configure(ui("#tasks-table", DataTable), "ID", "Kind", "Status", "Goal", "Age")
        configure(ui("#approvals-table", DataTable), "ID", "Operation", "Command", "Task", "Age")
        configure(ui("#skills-table", DataTable), "ID", "State", "Source", "Components")

    def _ui_screen(self):
        if self.screen_stack:
            return self.screen_stack[0]
        try:
            return self.screen
        except ScreenStackError:
            return None

    def _ui_query_one(self, selector, expect_type=None):
        screen = self._ui_screen()
        if screen is None:
            raise ScreenStackError("No screens on stack")
        if expect_type is None:
            return screen.query_one(selector)
        return screen.query_one(selector, expect_type)

    async def _poll_runtime_loop(self) -> None:
        try:
            while self._running:
                await self._refresh_runtime()
                await asyncio.sleep(8)
        except asyncio.CancelledError:
            return

    async def _poll_tasks_loop(self) -> None:
        try:
            while self._running:
                await self._refresh_tasks()
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    async def _poll_approvals_loop(self) -> None:
        try:
            while self._running:
                await self._refresh_approvals()
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            return

    async def _poll_skills_loop(self) -> None:
        try:
            while self._running:
                await self._refresh_skills()
                await asyncio.sleep(15)
        except asyncio.CancelledError:
            return

    async def _refresh_all(self) -> None:
        await self._refresh_runtime()
        await self._refresh_tasks()
        await self._refresh_approvals()
        await self._refresh_skills()

    async def _refresh_runtime(self) -> None:
        self.store.mark_busy("runtime", True)
        try:
            status_result, runtime_result = await asyncio.gather(
                self.client.status(),
                self.client.runtime_profile(),
            )
            status = status_result if isinstance(status_result, dict) else {}
            runtime = runtime_result if isinstance(runtime_result, dict) else {}
            channels = status.get("channels") or {}
            self.store.apply_runtime(status=status, runtime=runtime, channels=channels)
            self.store.set_notice("Runtime synchronized", "success")
            self._render_state()
        except Exception as exc:
            self._report_error("Runtime refresh failed", exc, notify=False)
        finally:
            self.store.mark_busy("runtime", False)

    async def _refresh_tasks(self) -> None:
        self.store.mark_busy("tasks", True)
        try:
            result = await self.client.list_tasks()
            tasks = list(result.get("tasks") or [])
            self.store.apply_tasks(tasks)
            selected_task_id = self.store.state.selected_task_id
            if selected_task_id:
                await self._refresh_task_bundle(selected_task_id)
            self._render_state()
        except Exception as exc:
            self._report_error("Task refresh failed", exc, notify=False)
        finally:
            self.store.mark_busy("tasks", False)

    async def _refresh_task_bundle(self, task_id: str) -> None:
        if not task_id:
            return
        try:
            detail_result, timeline_result, artifacts_result, approvals_result = await asyncio.gather(
                self.client.task_detail(task_id),
                self.client.task_timeline(task_id),
                self.client.task_artifacts(task_id),
                self.client.task_approvals(task_id),
            )
            self.store.cache_task_bundle(
                task_id,
                detail=(detail_result or {}).get("task") or {},
                timeline=(timeline_result or {}).get("events") or [],
                artifacts=(artifacts_result or {}).get("artifacts") or [],
                approvals=(approvals_result or {}).get("approvals") or [],
            )
        except Exception as exc:
            self._report_error("Task detail refresh failed", exc, notify=False)

    def _queue_task_bundle_refresh(self, task_id: str, *, name: str, exclusive: bool = False) -> None:
        if not task_id or not self._running or not self.is_mounted:
            return
        self.run_worker(
            partial(self._refresh_task_bundle, task_id),
            name=name,
            group="task-bundle",
            exclusive=exclusive,
        )

    async def _refresh_approvals(self) -> None:
        self.store.mark_busy("approvals", True)
        try:
            result = await self.client.list_pending_approvals()
            approvals = list(result.get("approvals") or [])
            self.store.apply_approvals(approvals)
            selected_task_id = self.store.state.selected_task_id
            if selected_task_id:
                self.store.cache_task_bundle(selected_task_id, approvals=self.store.selected_task_approvals())
            self._render_state()
        except Exception as exc:
            self._report_error("Approval refresh failed", exc, notify=False)
        finally:
            self.store.mark_busy("approvals", False)

    async def _refresh_skills(self) -> None:
        self.store.mark_busy("skills", True)
        try:
            result = await self.client.skill_list()
            packs = list(result.get("packs") or [])
            self.store.apply_skills(packs)
            self._render_state()
        except Exception as exc:
            self._report_error("Skill refresh failed", exc, notify=False)
        finally:
            self.store.mark_busy("skills", False)

    def _format_error_text(self, exc: Exception) -> str:
        raw = str(exc or "").strip()
        if not raw:
            raw = exc.__class__.__name__
        lines = [line.strip() for line in raw.splitlines() if line.strip() and not line.strip().startswith("Traceback")]
        message = lines[0] if lines else raw
        lowered = message.lower()
        if "control socket not found at" in lowered:
            return "Native daemon is offline."
        if "no result received for" in lowered:
            return "Native daemon did not return a response."
        if "unknown control api failure" in lowered:
            return "Native daemon returned an error."
        if "unsupported operand type(s) for |" in message:
            return "Kestrel CLI requires Python 3.11 or newer."
        if len(message) > 220:
            return f"{message[:217]}..."
        return message

    def _report_error(self, title: str, exc: Exception, *, notify: bool = True) -> None:
        detail = self._format_error_text(exc)
        if notify:
            self.store.add_notification(title, detail, level="error")
        self.store.set_notice(f"{title}: {detail}", "error")
        self._render_state()

    def _motion_level(self) -> str:
        return "off" if self.store.state.reduced_motion else self.store.state.motion_level

    def _tick_brandline(self) -> None:
        if self.store.state.reduced_motion or self.store.state.motion_level == "off":
            return
        self._tagline_index = (self._tagline_index + 1) % len(self._taglines)
        try:
            self._ui_query_one("#brand-subtitle", Static).update(self._taglines[self._tagline_index])
        except Exception:
            return

    def _sync_layout(self) -> None:
        active_view = self.store.state.active_view
        for view_name in VIEWS:
            widget = self._ui_query_one(f"#view-{view_name}", Container)
            widget.display = active_view == view_name
        for view_name in VIEWS:
            button = self._ui_query_one(f"#nav-{view_name}", Button)
            button.set_class(active_view == view_name, "is-active")

        inspector = self._ui_query_one("#inspector", InspectorPane)
        details_button = self._ui_query_one("#details-button", Button)
        details_button.display = self.store.state.compact_mode
        inspector.display = not self.store.state.compact_mode and self.store.state.inspector_open
        self._render_bottom_bar()

    def _render_state(self) -> None:
        if not self.is_mounted or self._ui_screen() is None:
            return
        self._sync_layout()
        self._render_chrome()
        self._render_cockpit()
        self._render_chat()
        self._render_tasks()
        self._render_approvals()
        self._render_skills()
        self._render_inspector()

    def _render_chrome(self) -> None:
        runtime = self.store.state.runtime
        local_models = runtime.get("local_models") or {}
        provider = str(local_models.get("default_provider") or runtime.get("provider") or "local")
        model = str(local_models.get("default_model") or runtime.get("preferred_model") or "unknown")
        mode = str(runtime.get("runtime_mode") or "native")
        active_process = self.store.state.active_process
        if active_process.active:
            orb_state = active_process.state
            orb_label = active_process.label
        else:
            orb_state = "idle" if str(self.store.state.status.get("status") or "running") == "running" else "offline"
            orb_label = str(self.store.state.status.get("status") or "READY").upper()

        self._ui_query_one("#status-orb", StatusOrb).set_status(orb_state, orb_label, motion_level=self._motion_level())
        self._ui_query_one("#runtime-summary", Static).update(f"{provider.upper()} // {model} // {mode.upper()}")
        self._ui_query_one("#notifications-button", Button).label = f"NOTIFICATIONS {self.store.unread_notifications()}"

    def _render_bottom_bar(self) -> None:
        notice = self.store.state.notice_text or "Ready"
        hints = "TAB cycle  ARROWS select  ENTER open  CTRL+K palette  CTRL+N notifications"
        self._ui_query_one("#notice-bar", Static).update(notice)
        self._ui_query_one("#hint-bar", Static).update(hints)

    def _render_cockpit(self) -> None:
        runtime = self.store.state.runtime
        local_models = runtime.get("local_models") or {}
        provider = str(local_models.get("default_provider") or runtime.get("provider") or "local")
        model = str(local_models.get("default_model") or runtime.get("preferred_model") or "unknown")
        channels = self.store.state.channels or {}
        telegram = (channels.get("telegram") or {}).get("config") or {}
        self._ui_query_one("#metric-runtime", MetricCard).set_metric(
            title="Runtime",
            value=str(runtime.get("runtime_mode") or "native").upper(),
            subtitle=str(runtime.get("policy_name") or "local policy"),
            accent="#00f3ff",
        )
        self._ui_query_one("#metric-model", MetricCard).set_metric(
            title="Model",
            value=model,
            subtitle=provider,
            accent="#bd00ff",
        )
        self._ui_query_one("#metric-tasks", MetricCard).set_metric(
            title="Tasks",
            value=str(len(self.store.state.tasks)),
            subtitle=f"{sum(1 for task in self.store.state.tasks if str(task.get('status')) == 'running')} active",
            accent="#00ff9d",
        )
        self._ui_query_one("#metric-approvals", MetricCard).set_metric(
            title="Approvals",
            value=str(len(self.store.state.approvals)),
            subtitle="pending operator actions",
            accent="#f59e0b",
        )
        self._ui_query_one("#metric-telegram", MetricCard).set_metric(
            title="Telegram",
            value="ONLINE" if telegram.get("token") else "OFFLINE",
            subtitle=str(telegram.get("mode") or "polling"),
            accent="#00f3ff" if telegram.get("token") else "#ff0055",
        )
        self._ui_query_one("#cockpit-process", ProcessBar).set_process(
            self.store.state.active_process,
            motion_level=self._motion_level(),
        )

        task_rows = []
        for task in self.store.state.tasks[:10]:
            task_rows.append(
                {
                    "key": str(task.get("id") or ""),
                    "cells": (
                        str(task.get("id") or "")[:8],
                        str(task.get("goal") or "")[:44],
                        str(task.get("status") or "unknown"),
                        relative_age(str(task.get("created_at") or "")),
                    ),
                }
            )
        self._populate_table(self._ui_query_one("#cockpit-tasks", DataTable), task_rows, self.store.state.selected_task_id)

        approval_rows = []
        for approval in self.store.state.approvals[:10]:
            approval_rows.append(
                {
                    "key": str(approval.get("id") or ""),
                    "cells": (
                        str(approval.get("id") or "")[:8],
                        str(approval.get("command") or approval.get("operation") or "")[:36],
                        str(approval.get("task_id") or "")[:8],
                        relative_age(str(approval.get("created_at") or "")),
                    ),
                }
            )
        self._populate_table(self._ui_query_one("#cockpit-approvals", DataTable), approval_rows, self.store.state.selected_approval_id)

    def _render_chat(self) -> None:
        self._ui_query_one("#chat-process", ProcessBar).set_process(
            self.store.state.active_process,
            motion_level=self._motion_level(),
        )
        log = self._ui_query_one("#chat-log", RichLog)
        log.clear()
        if not self.store.state.chat_messages:
            log.write(Panel(Text("Start with a chat prompt or launch a task from the composer below.", style="#8492a6"), border_style="#1a3247"))
        else:
            for message in self.store.state.chat_messages:
                log.write(render_chat_message(message))

        approval = self._current_inline_approval()
        banner = self._ui_query_one("#chat-approval-banner", Static)
        approve_button = self._ui_query_one("#chat-approve-inline", Button)
        deny_button = self._ui_query_one("#chat-deny-inline", Button)
        if approval:
            banner.update(str(approval.get("command") or approval.get("operation") or "Approval required"))
            approve_button.disabled = False
            deny_button.disabled = False
        else:
            banner.update("No pending approvals")
            approve_button.disabled = True
            deny_button.disabled = True

        artifacts = self.store.latest_artifacts()
        if artifacts:
            names = ", ".join(str(item.get("name") or item.get("path") or "artifact") for item in artifacts[:4])
            self._ui_query_one("#chat-artifacts", Static).update(f"Artifacts: {names}")
        else:
            self._ui_query_one("#chat-artifacts", Static).update("Artifacts: none")

    def _render_tasks(self) -> None:
        filtered_tasks = []
        for task in self.store.state.tasks:
            if self._task_filter and not (
                self._task_filter in str(task.get("goal") or "").lower()
                or self._task_filter in str(task.get("status") or "").lower()
                or self._task_filter in str(task.get("id") or "").lower()
            ):
                continue
            filtered_tasks.append(task)

        rows = []
        for task in filtered_tasks:
            rows.append(
                {
                    "key": str(task.get("id") or ""),
                    "cells": (
                        str(task.get("id") or "")[:8],
                        str(task.get("kind") or "task"),
                        str(task.get("status") or "unknown"),
                        str(task.get("goal") or "")[:48],
                        relative_age(str(task.get("created_at") or "")),
                    ),
                }
            )
        self._populate_table(self._ui_query_one("#tasks-table", DataTable), rows, self.store.state.selected_task_id)

        selected_task = self.store.selected_task()
        bundle = self.store.task_bundle(self.store.state.selected_task_id)
        timeline_log = self._ui_query_one("#tasks-timeline", RichLog)
        timeline_log.clear()
        if selected_task:
            detail = bundle.get("detail") or selected_task
            provider = str(((detail.get("metadata") or {}).get("provider") or "local"))
            model = str(((detail.get("metadata") or {}).get("model") or "unknown"))
            timeline_log.write(
                render_key_value_table(
                    "Task Summary",
                    [
                        ("ID", str(detail.get("id") or "")),
                        ("Status", str(detail.get("status") or "unknown")),
                        ("Kind", str(detail.get("kind") or "task")),
                        ("Provider", provider),
                        ("Model", model),
                    ],
                    accent="#00f3ff",
                )
            )
            goal = str(detail.get("goal") or "").strip()
            if goal:
                timeline_log.write(Panel(Text(goal, style="#e6f7ff"), title="GOAL", border_style="#1a3247", padding=(1, 2)))
            result_message = str(((detail.get("result") or {}).get("message") or "")).strip()
            if result_message:
                timeline_log.write(Panel(Text(result_message, style="#d8ffef"), title="RESULT", border_style="#00ff9d", padding=(1, 2)))
            approvals = bundle.get("approvals") or []
            if approvals:
                lines = "\n".join(
                    f"{approval.get('status', 'pending').upper()} // {approval.get('command') or approval.get('operation') or ''}"
                    for approval in approvals[:5]
                )
                timeline_log.write(Panel(Text(lines, style="#f59e0b"), title="APPROVALS", border_style="#f59e0b", padding=(1, 2)))
            artifacts = bundle.get("artifacts") or []
            if artifacts:
                lines = "\n".join(str(artifact.get("name") or artifact.get("path") or "artifact") for artifact in artifacts[:8])
                timeline_log.write(Panel(Text(lines, style="#00f3ff"), title="ARTIFACTS", border_style="#00f3ff", padding=(1, 2)))
            events = bundle.get("timeline") or []
            if events:
                for event in events[-16:]:
                    summary = summarize_event(event)
                    if summary:
                        timeline_log.write(Panel(Text(summary, style="#c6d8e5"), border_style="#1a3247", padding=(0, 2)))
            else:
                timeline_log.write(Panel(Text("No timeline events yet", style="#8492a6"), border_style="#1a3247"))
        else:
            timeline_log.write(Panel(Text("Select a task to inspect its timeline and artifacts.", style="#8492a6"), border_style="#1a3247"))

        pending = self.store.pending_approval_for_task(self.store.state.selected_task_id)
        self._ui_query_one("#tasks-approve", Button).disabled = not bool(pending)
        self._ui_query_one("#tasks-deny", Button).disabled = not bool(pending)

    def _render_approvals(self) -> None:
        rows = []
        for approval in self.store.state.approvals:
            rows.append(
                {
                    "key": str(approval.get("id") or ""),
                    "cells": (
                        str(approval.get("id") or "")[:8],
                        str(approval.get("operation") or "action"),
                        str(approval.get("command") or "")[:40],
                        str(approval.get("task_id") or "")[:8],
                        relative_age(str(approval.get("created_at") or "")),
                    ),
                }
            )
        self._populate_table(self._ui_query_one("#approvals-table", DataTable), rows, self.store.state.selected_approval_id)

        approval = self.store.selected_approval()
        preview = self._ui_query_one("#approval-preview", RichLog)
        preview.clear()
        if not approval:
            preview.write(Panel(Text("No pending approvals.", style="#8492a6"), border_style="#1a3247"))
        else:
            preview.write(
                render_key_value_table(
                    "Approval",
                    [
                        ("ID", str(approval.get("id") or "")),
                        ("Task", str(approval.get("task_id") or "")),
                        ("Operation", str(approval.get("operation") or "")),
                        ("Status", str(approval.get("status") or "pending")),
                    ],
                    accent="#f59e0b",
                )
            )
            command = str(approval.get("command") or "").strip()
            if command:
                preview.write(Panel(Text(command, style="#fbd38d"), title="COMMAND", border_style="#f59e0b", padding=(1, 2)))
            payload = approval.get("payload") or {}
            if payload:
                preview.write(self._json_panel("PAYLOAD", payload, "#00f3ff"))
            resume = approval.get("resume") or {}
            if resume:
                preview.write(self._json_panel("RESUME STATE", resume, "#bd00ff"))

        disabled = not bool(approval)
        self._ui_query_one("#approval-approve", Button).disabled = disabled
        self._ui_query_one("#approval-deny", Button).disabled = disabled

    def _render_skills(self) -> None:
        filtered = self.store.filtered_skills()
        if filtered and self.store.state.selected_skill_id not in {str(item.get("pack_id") or "") for item in filtered}:
            self.store.select_skill(str(filtered[0].get("pack_id") or ""))
        rows = []
        for pack in filtered:
            state = "enabled" if pack.get("enabled") else ("installed" if pack.get("installed") else "available")
            source = str(pack.get("source_type") or pack.get("root_kind") or "bundled")
            rows.append(
                {
                    "key": str(pack.get("pack_id") or ""),
                    "cells": (
                        str(pack.get("pack_id") or ""),
                        state,
                        source,
                        component_summary(pack),
                    ),
                }
            )
        self._populate_table(self._ui_query_one("#skills-table", DataTable), rows, self.store.state.selected_skill_id)

        pack = self.store.selected_skill()
        preview = self._ui_query_one("#skill-preview", RichLog)
        preview.clear()
        if not pack:
            preview.write(Panel(Text("Search or select a skill pack to inspect it.", style="#8492a6"), border_style="#1a3247"))
        else:
            preview.write(
                render_key_value_table(
                    "Skill Pack",
                    [
                        ("ID", str(pack.get("pack_id") or "")),
                        ("Name", str(pack.get("name") or pack.get("pack_id") or "")),
                        ("Version", str(pack.get("version") or "0.0.0")),
                        ("Source", str(pack.get("source_type") or pack.get("root_kind") or "bundled")),
                        ("Enabled", "yes" if pack.get("enabled") else "no"),
                        ("Trusted", "yes" if pack.get("trusted") else "no"),
                    ],
                    accent="#bd00ff",
                )
            )
            description = str(pack.get("description") or "").strip()
            if description:
                preview.write(Panel(Text(description, style="#e6f7ff"), title="DESCRIPTION", border_style="#1a3247", padding=(1, 2)))
            dependencies = pack.get("dependencies") or []
            if dependencies:
                preview.write(
                    Panel(
                        Text(
                            "\n".join(str(dep.get("pack_id") or dep) for dep in dependencies[:8]),
                            style="#d8c8ff",
                        ),
                        title="DEPENDENCIES",
                        border_style="#bd00ff",
                        padding=(1, 2),
                    )
                )
            prompt_preview = str(pack.get("prompt_preview") or "").strip()
            if prompt_preview:
                preview.write(Panel(Text(prompt_preview, style="#c6d8e5"), title="PROMPT PREVIEW", border_style="#00f3ff", padding=(1, 2)))

        skill_toggle = self._ui_query_one("#skill-toggle", Button)
        skill_remove = self._ui_query_one("#skill-remove", Button)
        skill_install = self._ui_query_one("#skill-install", Button)
        if not pack:
            skill_toggle.disabled = True
            skill_remove.disabled = True
            skill_install.disabled = True
        else:
            skill_toggle.disabled = False
            skill_remove.disabled = not bool(pack.get("installed") or pack.get("enabled") or pack.get("source_path"))
            skill_install.disabled = bool(pack.get("installed") or pack.get("enabled"))

    def _render_inspector(self) -> None:
        inspector = self._ui_query_one("#inspector", InspectorPane)
        title, subtitle, renderables = self._build_inspector_content()
        inspector.show_renderables(title=title, subtitle=subtitle, renderables=renderables)

    def _build_inspector_content(self) -> tuple[str, str, list[object]]:
        state = self.store.state
        if state.active_view == VIEW_COCKPIT:
            runtime = state.runtime
            renderables = [
                render_key_value_table(
                    "Runtime",
                    [
                        ("Mode", str(runtime.get("runtime_mode") or "native")),
                        ("Policy", str(runtime.get("policy_name") or "local")),
                        ("Home", str(state.status.get("home") or "")),
                        ("Socket", str(state.status.get("control_socket") or "")),
                    ],
                    accent="#00f3ff",
                )
            ]
            channels = state.channels or {}
            telegram = (channels.get("telegram") or {}).get("config") or {}
            renderables.append(
                render_key_value_table(
                    "Channels",
                    [
                        ("Telegram", "configured" if telegram.get("token") else "offline"),
                        ("Mode", str(telegram.get("mode") or "polling")),
                        ("Workspace", str(telegram.get("workspaceId") or "default")),
                    ],
                    accent="#00ff9d",
                )
            )
            return "INSPECTOR", "Runtime and channel state", renderables

        if state.active_view == VIEW_CHAT:
            latest = state.chat_messages[-1] if state.chat_messages else None
            renderables = [
                render_key_value_table(
                    "Process",
                    [
                        ("State", state.active_process.state),
                        ("Label", state.active_process.label),
                        ("Task", state.active_process.task_id or "n/a"),
                    ],
                    accent="#bd00ff" if state.active_process.active else "#00f3ff",
                )
            ]
            if latest:
                renderables.append(
                    Panel(
                        Text(latest.text[:700], style="#e6f7ff"),
                        title=f"LATEST {latest.role.upper()}",
                        border_style="#00f3ff" if latest.role == "user" else "#00ff9d",
                        padding=(1, 2),
                    )
                )
            return "INSPECTOR", "Conversation context", renderables

        if state.active_view == VIEW_TASKS:
            task = self.store.selected_task()
            bundle = self.store.task_bundle(state.selected_task_id)
            if not task:
                return "INSPECTOR", "Select a task", [Panel(Text("No task selected", style="#8492a6"), border_style="#1a3247")]
            detail = bundle.get("detail") or task
            renderables = [
                render_key_value_table(
                    "Selected Task",
                    [
                        ("ID", str(detail.get("id") or "")),
                        ("Status", str(detail.get("status") or "unknown")),
                        ("Kind", str(detail.get("kind") or "task")),
                        ("Created", str(detail.get("created_at") or "unknown")),
                    ],
                    accent="#00f3ff",
                )
            ]
            artifacts = bundle.get("artifacts") or []
            if artifacts:
                renderables.append(
                    Panel(
                        Text("\n".join(str(item.get("path") or item.get("name") or "artifact") for item in artifacts[:8]), style="#00f3ff"),
                        title="ARTIFACTS",
                        border_style="#00f3ff",
                        padding=(1, 2),
                    )
                )
            approvals = bundle.get("approvals") or []
            if approvals:
                renderables.append(
                    Panel(
                        Text(
                            "\n".join(f"{approval.get('status', 'pending').upper()} // {approval.get('command') or approval.get('operation') or ''}" for approval in approvals[:6]),
                            style="#fbd38d",
                        ),
                        title="APPROVALS",
                        border_style="#f59e0b",
                        padding=(1, 2),
                    )
                )
            return "INSPECTOR", str(detail.get("goal") or "Task detail"), renderables

        if state.active_view == VIEW_APPROVALS:
            approval = self.store.selected_approval()
            if not approval:
                return "INSPECTOR", "Approval queue", [Panel(Text("No pending approvals", style="#8492a6"), border_style="#1a3247")]
            return "INSPECTOR", "Approval detail", [
                render_key_value_table(
                    "Approval",
                    [
                        ("ID", str(approval.get("id") or "")),
                        ("Task", str(approval.get("task_id") or "")),
                        ("Operation", str(approval.get("operation") or "")),
                        ("Created", str(approval.get("created_at") or "unknown")),
                    ],
                    accent="#f59e0b",
                ),
                self._json_panel("PAYLOAD", approval.get("payload") or {}, "#00f3ff"),
            ]

        if state.active_view == VIEW_SKILLS:
            pack = self.store.selected_skill()
            if not pack:
                return "INSPECTOR", "Skill library", [Panel(Text("No skill selected", style="#8492a6"), border_style="#1a3247")]
            return "INSPECTOR", str(pack.get("name") or pack.get("pack_id") or "Skill pack"), [
                render_key_value_table(
                    "Skill",
                    [
                        ("ID", str(pack.get("pack_id") or "")),
                        ("Version", str(pack.get("version") or "0.0.0")),
                        ("Source", str(pack.get("source_type") or pack.get("root_kind") or "bundled")),
                        ("Components", component_summary(pack)),
                    ],
                    accent="#bd00ff",
                ),
                Panel(
                    Text(str(pack.get("description") or "No description"), style="#e6f7ff"),
                    title="DESCRIPTION",
                    border_style="#1a3247",
                    padding=(1, 2),
                ),
            ]

        return "INSPECTOR", "No selection", [Panel(Text("Nothing selected", style="#8492a6"), border_style="#1a3247")]

    def _json_panel(self, title: str, payload: dict[str, Any], accent: str) -> Panel:
        rendered = json.dumps(payload, indent=2, ensure_ascii=False) if payload else "{}"
        return Panel(Text(rendered, style="#c6d8e5"), title=title, border_style=accent, padding=(1, 2))

    def _current_inline_approval(self) -> dict[str, Any]:
        active_task_id = self.store.state.active_process.task_id
        if active_task_id:
            approval = self.store.pending_approval_for_task(active_task_id)
            if approval:
                return approval
        return self.store.selected_approval() or (self.store.state.approvals[0] if self.store.state.approvals else {})

    def _populate_table(self, table: DataTable, rows: list[dict[str, Any]], selected_key: str) -> None:
        self._suspend_table_events = True
        try:
            table.clear(columns=False)
            selected_row = 0
            for index, row in enumerate(rows):
                key = row["key"]
                table.add_row(*row["cells"], key=key)
                if selected_key and key == selected_key:
                    selected_row = index
            if rows:
                table.move_cursor(row=min(selected_row, len(rows) - 1), animate=False, scroll=False)
        finally:
            self._suspend_table_events = False

    async def _submit_prompt(self, prompt: str, *, kind: str) -> None:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            return

        task_mode = kind
        if clean_prompt.startswith("!"):
            clean_prompt = clean_prompt[1:].strip()
            task_mode = "task"
        if not clean_prompt:
            return

        composer = self._ui_query_one("#chat-composer", TextArea)
        composer.clear()
        role = "task" if task_mode == "task" else "user"
        meta = "task launch" if task_mode == "task" else "chat request"
        self.store.append_chat(role=role, text=clean_prompt, meta=meta)
        self.store.set_view(VIEW_CHAT)
        self.store.set_notice("Dispatching request", "info")
        self.store.set_process(
            state="thinking",
            label="DISPATCHED",
            detail=clean_prompt[:120],
            phases=["queued"],
            accent="purple",
            active=True,
        )
        self._render_state()

        task_id = ""
        try:
            async for event in self.client.start_task(clean_prompt, kind=task_mode):
                event_task_id = str(event.get("task_id") or "")
                if event_task_id:
                    task_id = event_task_id
                    self.store.select_task(task_id)
                self.store.update_process_from_event(task_id, event)
                if str(event.get("type") or "") in {"approval_needed", "task_failed", "error"}:
                    summary = summarize_event(event)
                    if summary:
                        self.store.append_chat(role="system" if event.get("type") == "approval_needed" else "error", text=summary, meta="event", task_id=task_id)
                self._render_state()

            if task_id:
                await self._refresh_task_bundle(task_id)
                detail = self.store.state.task_details.get(task_id) or {}
                artifacts = self.store.state.task_artifacts.get(task_id) or []
                approvals = self.store.state.task_approvals.get(task_id) or []
                metadata = detail.get("metadata") or {}
                provider = str(metadata.get("provider") or "")
                model = str(metadata.get("model") or "")
                meta_bits = " // ".join(bit for bit in [provider, model] if bit)
                message = str(((detail.get("result") or {}).get("message") or "")).strip()
                if message:
                    self.store.append_chat(
                        role="assistant" if task_mode == "chat" else "task",
                        text=message,
                        meta=meta_bits,
                        task_id=task_id,
                        artifacts=artifacts,
                    )
                elif approvals:
                    approval = approvals[0]
                    approval_text = str(approval.get("command") or approval.get("operation") or "Action pending approval")
                    self.store.append_chat(
                        role="system",
                        text=f"Approval required: {approval_text}",
                        meta="approval",
                        task_id=task_id,
                        approval=approval,
                    )
                elif detail:
                    fallback = str(detail.get("status") or "completed").upper()
                    self.store.append_chat(
                        role="assistant",
                        text=f"{fallback}. No final message returned.",
                        meta=meta_bits,
                        task_id=task_id,
                        artifacts=artifacts,
                    )
            await self._refresh_tasks()
            await self._refresh_approvals()
            self.store.set_notice("Request completed", "success")
        except Exception as exc:
            self.store.append_chat(role="error", text=str(exc), meta="execution error", task_id=task_id)
            self.store.set_notice("Request failed", "error")
            self.store.set_process(
                state="error",
                label="FAILED",
                detail=str(exc),
                phases=["error"],
                accent="error",
                active=False,
                task_id=task_id,
            )
        finally:
            if not self.store.state.active_process.active and self.store.state.active_process.state != "approval":
                self.store.clear_process(label="READY")
            self._render_state()

    async def _resolve_selected_approval(self, approved: bool, *, approval: dict[str, Any] | None = None) -> None:
        current = approval or self.store.selected_approval()
        if not current:
            return
        approval_id = str(current.get("id") or "")
        task_id = str(current.get("task_id") or "")
        if not approval_id or not task_id:
            return
        action = "Approving" if approved else "Denying"
        self.store.set_notice(f"{action} pending action", "warning")
        self._render_state()
        try:
            await self.client.approve_task(task_id, approval_id, approved)
            self.store.add_notification(
                "Approval resolved",
                f"{'Approved' if approved else 'Denied'} {current.get('command') or current.get('operation') or approval_id}",
                level="success" if approved else "warning",
            )
            await self._refresh_approvals()
            await self._refresh_tasks()
            if task_id:
                await self._refresh_task_bundle(task_id)
            self.store.set_notice("Approval resolved", "success")
        except Exception as exc:
            self._report_error("Approval resolution failed", exc)
        finally:
            self._render_state()

    async def _install_selected_skill(self) -> None:
        pack = self.store.selected_skill()
        if not pack:
            return
        try:
            await self.client.skill_install(pack_id=str(pack.get("pack_id") or ""), scope="user")
            self.store.set_notice(f"Installed {pack.get('pack_id')}", "success")
            await self._refresh_skills()
        except Exception as exc:
            self._report_error("Skill install failed", exc)

    async def _toggle_selected_skill(self) -> None:
        pack = self.store.selected_skill()
        if not pack:
            return
        try:
            if pack.get("enabled"):
                await self.client.skill_disable(str(pack.get("pack_id") or ""))
                self.store.set_notice(f"Disabled {pack.get('pack_id')}", "warning")
            else:
                await self.client.skill_enable(str(pack.get("pack_id") or ""))
                self.store.set_notice(f"Enabled {pack.get('pack_id')}", "success")
            await self._refresh_skills()
        except Exception as exc:
            self._report_error("Skill toggle failed", exc)

    async def _remove_selected_skill(self) -> None:
        pack = self.store.selected_skill()
        if not pack:
            return
        try:
            await self.client.skill_remove(str(pack.get("pack_id") or ""))
            self.store.set_notice(f"Removed {pack.get('pack_id')}", "warning")
            await self._refresh_skills()
        except Exception as exc:
            self._report_error("Skill removal failed", exc)

    def _palette_actions(self) -> list[PaletteAction]:
        return [
            PaletteAction("view:cockpit", "Open Cockpit", "navigation", "Runtime, tasks, and channels", "1"),
            PaletteAction("view:chat", "Open Chat", "navigation", "Conversation and live process", "2"),
            PaletteAction("view:tasks", "Open Tasks", "navigation", "Task list and timeline", "3"),
            PaletteAction("view:approvals", "Open Approvals", "navigation", "Pending actions queue", "4"),
            PaletteAction("view:skills", "Open Skills", "navigation", "Skill library and installs", "5"),
            PaletteAction("action:refresh", "Refresh Everything", "actions", "Poll runtime, tasks, approvals, skills", "Ctrl+R"),
            PaletteAction("action:notifications", "Open Notifications", "actions", "Unread notices and alerts", "Ctrl+N"),
            PaletteAction("action:details", "Toggle Details", "actions", "Open or collapse inspector", "Ctrl+I"),
            PaletteAction("action:focus_chat", "Focus Chat Composer", "actions", "Jump to the chat composer", ""),
            PaletteAction("action:quit", "Quit Cockpit", "actions", "Exit the Textual operator deck", ""),
        ]

    def _handle_palette_result(self, result: str | None) -> None:
        if not result:
            return
        if result.startswith("view:"):
            self._activate_view(result.split(":", 1)[1])
            return
        if result == "action:refresh":
            self.run_worker(self._refresh_all(), name="manual-refresh")
            return
        if result == "action:notifications":
            self.action_open_notifications()
            return
        if result == "action:details":
            self.action_toggle_details()
            return
        if result == "action:focus_chat":
            self._activate_view(VIEW_CHAT)
            self._ui_query_one("#chat-composer", TextArea).focus()
            return
        if result == "action:quit":
            self.exit()

    def _handle_notification_result(self, result: str | None) -> None:
        if result == "mark_read":
            self.store.mark_notifications_read()
            self._render_state()

    async def _open_palette_screen(self) -> None:
        result = await self.push_screen_wait(CommandPaletteScreen(self._palette_actions()))
        self._handle_palette_result(result)

    def _activate_view(self, view: str) -> None:
        if self.store.set_view(view):
            self.store.set_notice(f"{view.title()} ready", "info")
        self._sync_layout()
        self._render_state()
        self._focus_primary_control(view)

    def _focus_primary_control(self, view: str | None = None) -> None:
        target_view = view or self.store.state.active_view
        selector_map: dict[str, tuple[str, Any]] = {
            VIEW_COCKPIT: ("#cockpit-tasks", DataTable),
            VIEW_CHAT: ("#chat-composer", TextArea),
            VIEW_TASKS: ("#tasks-table", DataTable),
            VIEW_APPROVALS: ("#approvals-table", DataTable),
            VIEW_SKILLS: ("#skills-table", DataTable),
        }
        selector, expect_type = selector_map.get(target_view, ("", None))
        if not selector or expect_type is None:
            return
        try:
            self._ui_query_one(selector, expect_type).focus()
        except Exception:
            return

    def action_view_cockpit(self) -> None:
        self._activate_view(VIEW_COCKPIT)

    def action_view_chat(self) -> None:
        self._activate_view(VIEW_CHAT)

    def action_view_tasks(self) -> None:
        self._activate_view(VIEW_TASKS)

    def action_view_approvals(self) -> None:
        self._activate_view(VIEW_APPROVALS)

    def action_view_skills(self) -> None:
        self._activate_view(VIEW_SKILLS)

    def action_open_palette(self) -> None:
        self.run_worker(self._open_palette_screen(), name="open-palette")

    def action_open_notifications(self) -> None:
        self.push_screen(NotificationCenterScreen(self.store.state.notifications), self._handle_notification_result)

    def action_refresh_now(self) -> None:
        self.run_worker(self._refresh_all(), name="manual-refresh")

    def action_toggle_details(self) -> None:
        if self.store.state.compact_mode:
            title, subtitle, renderables = self._build_inspector_content()
            self.push_screen(InspectorModalScreen(title=title, subtitle=subtitle, renderables=renderables))
            return
        self.store.toggle_inspector()
        self._sync_layout()
        self._render_inspector()

    def action_send_chat(self) -> None:
        composer = self._ui_query_one("#chat-composer", TextArea)
        prompt = composer.text
        self.run_worker(self._submit_prompt(prompt, kind="chat"), name="chat-submit")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "nav-cockpit":
            self.action_view_cockpit()
            return
        if button_id == "nav-chat":
            self.action_view_chat()
            return
        if button_id == "nav-tasks":
            self.action_view_tasks()
            return
        if button_id == "nav-approvals":
            self.action_view_approvals()
            return
        if button_id == "nav-skills":
            self.action_view_skills()
            return
        if button_id == "palette-button":
            self.action_open_palette()
            return
        if button_id == "notifications-button":
            self.action_open_notifications()
            return
        if button_id == "details-button":
            self.action_toggle_details()
            return
        if button_id == "chat-send":
            self.run_worker(self._submit_prompt(self._ui_query_one("#chat-composer", TextArea).text, kind="chat"), name="chat-submit")
            return
        if button_id == "chat-launch-task":
            self.run_worker(self._submit_prompt(self._ui_query_one("#chat-composer", TextArea).text, kind="task"), name="task-submit")
            return
        if button_id in {"chat-approve-inline", "tasks-approve", "approval-approve"}:
            approval = self._current_inline_approval() if button_id == "chat-approve-inline" else None
            self.run_worker(self._resolve_selected_approval(True, approval=approval), name="approval-approve")
            return
        if button_id in {"chat-deny-inline", "tasks-deny", "approval-deny"}:
            approval = self._current_inline_approval() if button_id == "chat-deny-inline" else None
            self.run_worker(self._resolve_selected_approval(False, approval=approval), name="approval-deny")
            return
        if button_id == "tasks-refresh":
            selected_task_id = self.store.state.selected_task_id
            if selected_task_id:
                self._queue_task_bundle_refresh(selected_task_id, name="task-bundle-refresh")
            else:
                self.run_worker(self._refresh_tasks(), name="task-refresh")
            return
        if button_id == "skill-install":
            self.run_worker(self._install_selected_skill(), name="skill-install")
            return
        if button_id == "skill-toggle":
            self.run_worker(self._toggle_selected_skill(), name="skill-toggle")
            return
        if button_id == "skill-remove":
            self.run_worker(self._remove_selected_skill(), name="skill-remove")

    def on_input_changed(self, event: Input.Changed) -> None:
        input_id = event.input.id or ""
        if input_id == "tasks-filter":
            self._task_filter = event.value.strip().lower()
            self._render_tasks()
            return
        if input_id == "skills-search":
            self.store.state.skill_query = event.value.strip()
            self._render_skills()
            self._render_inspector()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._suspend_table_events or not self._ui_ready:
            return
        row_key = self._extract_row_key(event)
        if not row_key:
            return
        self._handle_table_selection(event.data_table.id or "", row_key, activate=True)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if self._suspend_table_events or not self._ui_ready or not event.data_table.has_focus:
            return
        row_key = self._extract_row_key(event)
        if not row_key:
            return
        self._handle_table_selection(event.data_table.id or "", row_key, activate=False)

    def _extract_row_key(self, event: DataTable.RowSelected | DataTable.RowHighlighted) -> str:
        row_key = getattr(event, "row_key", None)
        if row_key is None:
            return ""
        value = getattr(row_key, "value", row_key)
        return str(value or "")

    def _handle_table_selection(self, table_id: str, row_key: str, *, activate: bool) -> None:
        if table_id in {"cockpit-tasks", "tasks-table"}:
            changed = self.store.state.selected_task_id != row_key
            if not changed and not activate:
                return
            self.store.select_task(row_key)
            if changed and self._running and self.is_mounted:
                self._queue_task_bundle_refresh(row_key, name=f"task-bundle-{row_key}", exclusive=True)
            if activate and table_id == "cockpit-tasks":
                self._activate_view(VIEW_TASKS)
            else:
                self._render_state()
            return
        if table_id in {"cockpit-approvals", "approvals-table"}:
            changed = self.store.state.selected_approval_id != row_key
            if not changed and not activate:
                return
            self.store.select_approval(row_key)
            if activate and table_id == "cockpit-approvals":
                self._activate_view(VIEW_APPROVALS)
            else:
                self._render_state()
            return
        if table_id == "skills-table":
            if self.store.state.selected_skill_id == row_key and not activate:
                return
            self.store.select_skill(row_key)
            self._render_state()


def launch_tui(client: KestrelClient, config: dict[str, Any]) -> bool:
    try:
        app = KestrelTextualApp(client, config)
        mouse_enabled = bool((config.get("tui") or {}).get("mouse_enabled", True))
        app.run(mouse=mouse_enabled)
        return True
    except KeyboardInterrupt:
        return True
    except Exception:
        traceback.print_exc()
        return False
