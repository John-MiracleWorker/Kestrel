from __future__ import annotations

import asyncio
import curses
import os
import textwrap
import time
from dataclasses import dataclass, field
from typing import Any

from .cli_core import KestrelClient
from .cli_memory import load_channel_state


TAB_FLIGHT_DECK = "Flight Deck"
TAB_TASKS = "Tasks"
TAB_SKILLS = "Skills"
TAB_CHAT = "Chat"
TABS = [TAB_FLIGHT_DECK, TAB_TASKS, TAB_SKILLS, TAB_CHAT]


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def _wrap(text: str, width: int) -> list[str]:
    if width <= 1:
        return [_string(text)]
    lines: list[str] = []
    for raw_line in _string(text).splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        lines.extend(
            textwrap.wrap(
                raw_line,
                width=width,
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )
    return lines or [""]


def _component_summary(pack: dict[str, Any]) -> str:
    components = pack.get("components") or []
    if not isinstance(components, list) or not components:
        return "none"
    counts: dict[str, int] = {}
    for item in components:
        if not isinstance(item, dict):
            continue
        component_type = _string(item.get("type") or "unknown")
        counts[component_type] = counts.get(component_type, 0) + 1
    return ", ".join(
        f"{component_type} x{count}"
        for component_type, count in sorted(counts.items(), key=lambda pair: pair[0])
    ) or "none"


def summarize_task_events(events: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for event in events:
        event_type = _string(event.get("type") or "").strip()
        content = _string(event.get("content") or "").strip()
        if event_type in {"step_started", "step_complete", "task_complete", "task_failed", "error", "status"} and content:
            prefix = {
                "step_started": "STEP",
                "step_complete": "DONE",
                "task_complete": "COMPLETE",
                "task_failed": "FAILED",
                "error": "ERROR",
                "status": "STATUS",
            }.get(event_type, event_type.upper())
            lines.append(f"{prefix}: {content}")
    return lines[-12:] or ["Task finished without streamed events."]


def build_runtime_lines(
    status: dict[str, Any],
    profile: dict[str, Any],
    config: dict[str, Any],
    channels: dict[str, Any],
) -> list[str]:
    runtime = profile or status.get("runtime_profile") or {}
    local_models = runtime.get("local_models") or {}
    telegram = ((channels.get("telegram") or {}).get("config") or {})
    return [
        f"Runtime mode: {_string(runtime.get('runtime_mode') or 'unknown')}",
        f"Policy: {_string(runtime.get('policy_name') or 'unknown')}",
        f"Model: {_string(local_models.get('default_provider') or 'none')}:{_string(local_models.get('default_model') or 'none')}",
        f"Approvals pending: {len(status.get('pending_approvals') or [])}",
        f"Recent tasks: {len(status.get('recent_tasks') or [])}",
        f"Telegram: {'configured' if telegram.get('token') else 'not configured'}",
        f"API: {_string(config.get('api_url') or 'not set')}",
        f"Workspace: {_string(config.get('workspace_id') or 'local')}",
    ]


def build_task_detail_lines(task: dict[str, Any]) -> list[str]:
    if not task:
        return ["No task selected."]
    lines = [
        f"ID: {_string(task.get('id'))}",
        f"Status: {_string(task.get('status') or 'unknown')}",
        f"Kind: {_string(task.get('kind') or 'task')}",
        f"Created: {_string(task.get('created_at') or 'unknown')}",
        "",
        f"Goal: {_string(task.get('goal') or '')}",
    ]
    result = task.get("result") or {}
    message = _string(result.get("message") or "")
    if message:
        lines.extend(["", "Result:", message])
    metadata = task.get("metadata") or {}
    if metadata:
        provider = _string(metadata.get("provider") or "")
        model = _string(metadata.get("model") or "")
        if provider or model:
            lines.append("")
            lines.append(f"Provider: {provider or 'unknown'}")
            lines.append(f"Model: {model or 'unknown'}")
    return lines


def build_skill_detail_lines(pack: dict[str, Any]) -> list[str]:
    if not pack:
        return ["No skill pack selected."]
    lines = [
        f"ID: {_string(pack.get('pack_id'))}",
        f"Name: {_string(pack.get('name') or pack.get('pack_id'))}",
        f"Version: {_string(pack.get('version') or '0.0.0')}",
        f"Source: {_string(pack.get('source_type') or pack.get('root_kind') or 'unknown')}",
        f"Scope: {_string(pack.get('scope') or pack.get('root_kind') or 'unknown')}",
        f"Enabled: {'yes' if pack.get('enabled') else 'no'}",
        f"Trusted: {'yes' if pack.get('trusted') else 'no'}",
        f"Components: {_component_summary(pack)}",
    ]
    dependencies = pack.get("dependencies") or []
    dependency_ids = [
        _string(item.get("pack_id") or "")
        for item in dependencies
        if isinstance(item, dict) and _string(item.get("pack_id") or "").strip()
    ]
    if dependency_ids:
        lines.append(f"Depends on: {', '.join(dependency_ids)}")
    description = _string(pack.get("description") or "").strip()
    if description:
        lines.extend(["", "Description:", description])
    prompt_preview = _string(pack.get("prompt_preview") or "").strip()
    if prompt_preview:
        lines.extend(["", "Prompt preview:", prompt_preview])
    return lines


@dataclass
class ChatEntry:
    role: str
    text: str
    meta: str = ""


@dataclass
class TuiNotice:
    text: str = ""
    tone: str = "info"
    updated_at: float = 0.0


@dataclass
class TuiState:
    active_tab: int = 0
    status: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    channels: dict[str, Any] = field(default_factory=dict)
    tasks: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, Any]] = field(default_factory=list)
    skill_query: str = ""
    selected_task: int = 0
    selected_skill: int = 0
    chat_draft: str = ""
    chat_entries: list[ChatEntry] = field(default_factory=list)
    mode: str = "normal"
    input_buffer: str = ""
    notice: TuiNotice = field(default_factory=TuiNotice)
    last_refresh_at: float = 0.0
    busy: bool = False


class KestrelTui:
    refresh_interval_seconds = 15.0

    def __init__(self, client: KestrelClient, config: dict[str, Any]):
        self.client = client
        self.config = config
        self.state = TuiState()
        self.screen = None
        self.colors: dict[str, int] = {}

    def run(self, screen) -> None:
        self.screen = screen
        self._setup_curses()
        self._refresh_all()

        while True:
            if time.time() - self.state.last_refresh_at >= self.refresh_interval_seconds and self.state.mode == "normal":
                self._refresh_all()
            self._draw()
            key = self.screen.getch()
            if key == -1:
                continue
            if not self._handle_key(key):
                break

    def _setup_curses(self) -> None:
        curses.curs_set(0)
        curses.noecho()
        curses.cbreak()
        self.screen.keypad(True)
        self.screen.timeout(150)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            if getattr(curses, "COLORS", 0) >= 256:
                self._init_pair("default", curses.COLOR_WHITE, -1, 1)
                self._init_pair("brand", curses.COLOR_BLACK, 208, 2)
                self._init_pair("accent", curses.COLOR_WHITE, 25, 3)
                self._init_pair("surface", curses.COLOR_WHITE, 236, 4)
                self._init_pair("surface_alt", curses.COLOR_WHITE, 238, 5)
                self._init_pair("muted", 245, -1, 6)
                self._init_pair("success", 114, -1, 7)
                self._init_pair("warning", 214, -1, 8)
                self._init_pair("error", 203, -1, 9)
                self._init_pair("selected", curses.COLOR_BLACK, 117, 10)
                self._init_pair("input", curses.COLOR_WHITE, 24, 11)
            else:
                self._init_pair("default", curses.COLOR_WHITE, -1, 1)
                self._init_pair("brand", curses.COLOR_BLACK, curses.COLOR_YELLOW, 2)
                self._init_pair("accent", curses.COLOR_WHITE, curses.COLOR_BLUE, 3)
                self._init_pair("surface", curses.COLOR_WHITE, curses.COLOR_BLACK, 4)
                self._init_pair("surface_alt", curses.COLOR_WHITE, curses.COLOR_BLACK, 5)
                self._init_pair("muted", curses.COLOR_CYAN, -1, 6)
                self._init_pair("success", curses.COLOR_GREEN, -1, 7)
                self._init_pair("warning", curses.COLOR_YELLOW, -1, 8)
                self._init_pair("error", curses.COLOR_RED, -1, 9)
                self._init_pair("selected", curses.COLOR_BLACK, curses.COLOR_CYAN, 10)
                self._init_pair("input", curses.COLOR_WHITE, curses.COLOR_BLUE, 11)
        else:
            self.colors = {name: curses.A_NORMAL for name in [
                "default", "brand", "accent", "surface", "surface_alt", "muted",
                "success", "warning", "error", "selected", "input",
            ]}

    def _init_pair(self, name: str, fg: int, bg: int, number: int) -> None:
        curses.init_pair(number, fg, bg)
        self.colors[name] = curses.color_pair(number)

    def _run_async(self, coroutine):
        return asyncio.run(coroutine)

    def _refresh_all(self) -> None:
        self.state.busy = True
        self._set_notice("Refreshing runtime state", "info")
        try:
            self.state.status = self._run_async(self.client.status()) or {}
            runtime = self._run_async(self.client.runtime_profile()) or {}
            self.state.runtime = runtime or (self.state.status.get("runtime_profile") or {})
            self.state.channels = load_channel_state(self.client.paths)
            task_payload = self._run_async(self.client.list_tasks()) or {}
            self.state.tasks = list(task_payload.get("tasks") or [])
            self._refresh_skills()
            self.state.selected_task = min(self.state.selected_task, max(len(self.state.tasks) - 1, 0))
            self.state.selected_skill = min(self.state.selected_skill, max(len(self.state.skills) - 1, 0))
            self.state.last_refresh_at = time.time()
            self._set_notice("Runtime synchronized", "success")
        except Exception as exc:
            self._set_notice(f"Refresh failed: {exc}", "error")
        finally:
            self.state.busy = False

    def _refresh_skills(self) -> None:
        if self.state.skill_query.strip():
            payload = self._run_async(self.client.skill_search(self.state.skill_query.strip(), include_marketplace=True)) or {}
            self.state.skills = list(payload.get("results") or [])
            return
        payload = self._run_async(self.client.skill_list(include_synthetic=True, include_marketplace=True)) or {}
        self.state.skills = list(payload.get("packs") or [])

    def _send_chat(self) -> None:
        prompt = self.state.chat_draft.strip()
        if not prompt:
            self._set_notice("Chat draft is empty", "warning")
            return

        self.state.chat_entries.append(ChatEntry(role="you", text=prompt))
        self.state.chat_draft = ""
        self.state.busy = True
        self._set_notice("Sending prompt", "info")
        try:
            if prompt.startswith("!"):
                events = self._run_async(self._collect_task_events(prompt[1:].strip()))
                lines = summarize_task_events(events)
                self.state.chat_entries.append(ChatEntry(role="task", text="\n".join(lines), meta="autonomous task"))
                self._set_notice("Task completed", "success")
                self._refresh_all()
                return

            response = self._run_async(self.client.chat(prompt)) or {}
            if response.get("error"):
                self.state.chat_entries.append(ChatEntry(role="error", text=_string(response["error"])))
                self._set_notice("Chat request failed", "error")
                return

            message = _string(response.get("message") or "").strip() or "No message returned."
            plan = response.get("plan") or {}
            artifacts = response.get("artifacts") or []
            extra: list[str] = []
            if plan:
                extra.append(f"Plan: {_string(plan.get('summary') or 'created')}")
            if artifacts:
                extra.append(f"Artifacts: {len(artifacts)}")
            meta = ":".join(filter(None, [_string(response.get("provider") or ""), _string(response.get("model") or "")]))
            if extra:
                message = message + "\n\n" + "\n".join(extra)
            self.state.chat_entries.append(ChatEntry(role="assistant", text=message, meta=meta))
            self._set_notice("Response received", "success")
            if response.get("model"):
                self.config["model"] = _string(response.get("model"))
        except Exception as exc:
            self.state.chat_entries.append(ChatEntry(role="error", text=_string(exc)))
            self._set_notice(f"Chat request failed: {exc}", "error")
        finally:
            self.state.busy = False

    async def _collect_task_events(self, goal: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        async for event in self.client.start_task(goal):
            events.append(event)
        return events

    def _install_selected_skill(self) -> None:
        pack = self._selected_skill()
        if not pack:
            self._set_notice("No skill pack selected", "warning")
            return
        pack_id = _string(pack.get("pack_id") or "")
        if not pack_id:
            self._set_notice("Selected row has no pack id", "error")
            return
        self.state.busy = True
        try:
            payload = self._run_async(self.client.skill_install(pack_id=pack_id, scope="user")) or {}
            if payload.get("error"):
                self._set_notice(_string(payload["error"]), "error")
                return
            self._set_notice(f"Installed {pack_id}", "success")
            self._refresh_all()
        except Exception as exc:
            self._set_notice(f"Install failed: {exc}", "error")
        finally:
            self.state.busy = False

    def _toggle_selected_skill(self) -> None:
        pack = self._selected_skill()
        if not pack:
            self._set_notice("No skill pack selected", "warning")
            return
        pack_id = _string(pack.get("pack_id") or "")
        if not pack_id:
            self._set_notice("Selected row has no pack id", "error")
            return
        self.state.busy = True
        try:
            if pack.get("enabled"):
                payload = self._run_async(self.client.skill_disable(pack_id)) or {}
                verb = "Disabled"
            else:
                payload = self._run_async(self.client.skill_enable(pack_id)) or {}
                verb = "Enabled"
            if payload.get("error"):
                self._set_notice(_string(payload["error"]), "error")
                return
            self._set_notice(f"{verb} {pack_id}", "success")
            self._refresh_all()
        except Exception as exc:
            self._set_notice(f"Toggle failed: {exc}", "error")
        finally:
            self.state.busy = False

    def _selected_task(self) -> dict[str, Any]:
        if not self.state.tasks:
            return {}
        return self.state.tasks[self.state.selected_task]

    def _selected_skill(self) -> dict[str, Any]:
        if not self.state.skills:
            return {}
        return self.state.skills[self.state.selected_skill]

    def _set_notice(self, text: str, tone: str) -> None:
        self.state.notice = TuiNotice(text=text, tone=tone, updated_at=time.time())

    def _handle_key(self, key: int) -> bool:
        if self.state.mode == "skill_search":
            return self._handle_skill_search_key(key)

        if self.state.active_tab == TABS.index(TAB_CHAT) and self._handle_chat_key(key):
            return True

        if key in (ord("q"), ord("Q")):
            return False
        if key in (ord("r"), ord("R")):
            self._refresh_all()
            return True
        if key in (curses.KEY_RIGHT, ord("l"), 9):
            self.state.active_tab = (self.state.active_tab + 1) % len(TABS)
            return True
        if key in (curses.KEY_LEFT, ord("h"), curses.KEY_BTAB):
            self.state.active_tab = (self.state.active_tab - 1) % len(TABS)
            return True
        if key in (ord("1"), ord("2"), ord("3"), ord("4")):
            self.state.active_tab = int(chr(key)) - 1
            return True

        tab_name = TABS[self.state.active_tab]
        if tab_name == TAB_TASKS:
            if key in (curses.KEY_DOWN, ord("j")) and self.state.tasks:
                self.state.selected_task = min(self.state.selected_task + 1, len(self.state.tasks) - 1)
            elif key in (curses.KEY_UP, ord("k")) and self.state.tasks:
                self.state.selected_task = max(self.state.selected_task - 1, 0)
            return True

        if tab_name == TAB_SKILLS:
            if key in (curses.KEY_DOWN, ord("j")) and self.state.skills:
                self.state.selected_skill = min(self.state.selected_skill + 1, len(self.state.skills) - 1)
            elif key in (curses.KEY_UP, ord("k")) and self.state.skills:
                self.state.selected_skill = max(self.state.selected_skill - 1, 0)
            elif key == ord("/"):
                self.state.mode = "skill_search"
                self.state.input_buffer = self.state.skill_query
                self._set_notice("Search skill packs", "info")
            elif key in (ord("i"), ord("I")):
                self._install_selected_skill()
            elif key in (ord("e"), ord("E")):
                self._toggle_selected_skill()
            return True

        return True

    def _handle_chat_key(self, key: int) -> bool:
        if key in (ord("q"), ord("Q")) and not self.state.chat_draft:
            return False
        if key in (curses.KEY_RIGHT, curses.KEY_LEFT, 9, curses.KEY_BTAB) and not self.state.chat_draft:
            return False
        if key in (10, 13):
            self._send_chat()
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.state.chat_draft = self.state.chat_draft[:-1]
            return True
        if key == 21:
            self.state.chat_draft = ""
            return True
        if 32 <= key <= 126:
            self.state.chat_draft += chr(key)
            return True
        return False

    def _handle_skill_search_key(self, key: int) -> bool:
        if key in (27,):
            self.state.mode = "normal"
            self.state.input_buffer = ""
            self._set_notice("Search cancelled", "warning")
            return True
        if key in (10, 13):
            self.state.skill_query = self.state.input_buffer.strip()
            self.state.mode = "normal"
            self.state.input_buffer = ""
            self._refresh_all()
            return True
        if key in (curses.KEY_BACKSPACE, 127, 8):
            self.state.input_buffer = self.state.input_buffer[:-1]
            return True
        if 32 <= key <= 126:
            self.state.input_buffer += chr(key)
            return True
        return True

    def _draw(self) -> None:
        height, width = self.screen.getmaxyx()
        self.screen.erase()
        self._draw_header(width)
        self._draw_tabs(width)

        body_top = 4
        footer_height = 3
        body_height = max(8, height - body_top - footer_height)
        if TABS[self.state.active_tab] == TAB_FLIGHT_DECK:
            self._draw_flight_deck(body_top, body_height, width)
        elif TABS[self.state.active_tab] == TAB_TASKS:
            self._draw_tasks(body_top, body_height, width)
        elif TABS[self.state.active_tab] == TAB_SKILLS:
            self._draw_skills(body_top, body_height, width)
        else:
            self._draw_chat(body_top, body_height, width)

        self._draw_footer(height, width)
        self.screen.refresh()

    def _draw_header(self, width: int) -> None:
        local_models = (self.state.runtime or {}).get("local_models") or {}
        runtime_model = _string(local_models.get("default_model") or "")
        runtime_provider = _string(local_models.get("default_provider") or "")
        if runtime_model:
            model = f"{runtime_provider}:{runtime_model}" if runtime_provider else runtime_model
        else:
            model = _string(self.config.get("model") or "auto")
        thinking = _string(self.config.get("thinking_level") or "medium")
        runtime_mode = _string((self.state.runtime or {}).get("runtime_mode") or "native")
        header = " KESTREL TUI "
        details = f"{model} | think {thinking} | {runtime_mode}"
        self._write(0, 0, " " * max(0, width - 1), self.colors["brand"])
        self._write(0, 2, header, self.colors["brand"] | curses.A_BOLD)
        self._write(0, max(2, width - len(details) - 3), details[: max(0, width - 5)], self.colors["brand"])
        shortcuts = "1-4 tabs | r refresh | q quit | chat: Enter send | skills: / search, i install, e toggle"
        self._write(1, 2, shortcuts[: max(0, width - 4)], self.colors["muted"])

    def _draw_tabs(self, width: int) -> None:
        x = 2
        for index, label in enumerate(TABS):
            text = f" {index + 1}. {label} "
            attr = self.colors["accent"] | curses.A_BOLD if index == self.state.active_tab else self.colors["surface"]
            self._write(2, x, text, attr)
            x += len(text) + 1
        self._write(3, 0, "-" * max(0, width - 1), self.colors["muted"])

    def _draw_flight_deck(self, top: int, height: int, width: int) -> None:
        left_width = max(32, width // 2 - 1)
        right_width = max(24, width - left_width - 3)
        runtime_lines = build_runtime_lines(self.state.status, self.state.runtime, self.config, self.state.channels)
        self._draw_panel(top, 1, min(height, 14), left_width, "Runtime", runtime_lines)

        recent_tasks = self.state.status.get("recent_tasks") or self.state.tasks[:8]
        task_lines = [
            f"{_string(task.get('status') or 'unknown')[:10]:<10} {_string(task.get('goal') or '')}"
            for task in recent_tasks[:8]
        ] or ["No recent tasks."]
        self._draw_panel(top, left_width + 2, min(height, 14), right_width, "Recent Tasks", task_lines)

        channel_state = (self.state.channels.get("telegram") or {})
        telegram_lines = [
            f"Configured: {'yes' if (channel_state.get('config') or {}).get('token') else 'no'}",
            f"Mode: {_string((channel_state.get('config') or {}).get('mode') or 'polling')}",
            f"Pairings: {len(((channel_state.get('state') or {}).get('mappings') or []))}",
        ]
        doctor_summary = (self.state.status.get("doctor_summary") or {})
        if doctor_summary:
            telegram_lines.append(f"Healthy: {_string(doctor_summary.get('healthy'))}")
        self._draw_panel(top + 15, 1, max(6, height - 15), left_width, "Channels", telegram_lines)

        guidance = [
            "Use Tasks to inspect execution state.",
            "Use Skills to browse and install packs.",
            "Use Chat for direct prompts or prefix with ! for an autonomous task.",
        ]
        self._draw_panel(top + 15, left_width + 2, max(6, height - 15), right_width, "Operator Notes", guidance)

    def _draw_tasks(self, top: int, height: int, width: int) -> None:
        list_width = max(30, width // 2 - 2)
        detail_width = max(28, width - list_width - 3)
        list_lines = []
        for task in self.state.tasks:
            list_lines.append(f"{_string(task.get('status') or 'unknown')[:10]:<10} {_string(task.get('goal') or '')}")
        if not list_lines:
            list_lines = ["No tasks available."]
        self._draw_panel(top, 1, height, list_width, "Task Queue", list_lines, selected_index=self.state.selected_task if self.state.tasks else None)
        detail_lines = build_task_detail_lines(self._selected_task())
        self._draw_panel(top, list_width + 2, height, detail_width, "Task Detail", detail_lines)

    def _draw_skills(self, top: int, height: int, width: int) -> None:
        list_width = max(34, width // 2 - 2)
        detail_width = max(28, width - list_width - 3)
        title = "Skill Library"
        if self.state.skill_query:
            title += f" [{self.state.skill_query}]"
        list_lines = []
        for pack in self.state.skills:
            status = "on" if pack.get("enabled") else "--"
            source = _string(pack.get("source_type") or pack.get("root_kind") or "")
            list_lines.append(f"{status:<2} {_string(pack.get('pack_id') or '')} [{source}]")
        if not list_lines:
            list_lines = ["No skill packs matched."]
        self._draw_panel(top, 1, height, list_width, title, list_lines, selected_index=self.state.selected_skill if self.state.skills else None)
        detail_lines = build_skill_detail_lines(self._selected_skill())
        detail_lines.extend(["", "Actions:", "  / search", "  i install selected", "  e enable or disable selected"])
        self._draw_panel(top, list_width + 2, height, detail_width, "Skill Detail", detail_lines)

    def _draw_chat(self, top: int, height: int, width: int) -> None:
        history_height = max(8, height - 5)
        input_height = height - history_height
        history_lines: list[str] = []
        for entry in self.state.chat_entries[-12:]:
            prefix = entry.role.upper()
            header = f"{prefix}"
            if entry.meta:
                header += f" [{entry.meta}]"
            history_lines.append(header)
            history_lines.extend(_wrap(entry.text, max(20, width - 8)))
            history_lines.append("")
        if not history_lines:
            history_lines = [
                "No chat messages yet.",
                "Type directly in the draft bar below.",
                "Prefix with ! to launch an autonomous task.",
            ]
        self._draw_panel(top, 1, history_height, width - 2, "Conversation", history_lines)
        composer_lines = [
            "Draft:",
            self.state.chat_draft or "",
            "",
            "Enter sends. Ctrl+U clears. Use !goal to run an autonomous task.",
        ]
        self._draw_panel(top + history_height, 1, max(4, input_height), width - 2, "Composer", composer_lines, tone="input")

    def _draw_footer(self, height: int, width: int) -> None:
        notice = self.state.notice.text or "Ready"
        tone = self.state.notice.tone
        attr = {
            "info": self.colors["accent"],
            "success": self.colors["success"],
            "warning": self.colors["warning"],
            "error": self.colors["error"],
        }.get(tone, self.colors["accent"])
        mode_text = f"mode={self.state.mode}"
        busy_text = "busy" if self.state.busy else "idle"
        self._write(height - 2, 0, " " * max(0, width - 1), self.colors["surface_alt"])
        self._write(height - 2, 2, notice[: max(0, width - 24)], attr | curses.A_BOLD)
        self._write(height - 2, max(2, width - len(mode_text) - len(busy_text) - 6), f"{mode_text} | {busy_text}", self.colors["muted"])

        input_prompt = ""
        if self.state.mode == "skill_search":
            input_prompt = f"skill search> {self.state.input_buffer}"
        elif TABS[self.state.active_tab] == TAB_CHAT:
            input_prompt = f"chat> {self.state.chat_draft}"
        else:
            input_prompt = "navigate with arrows or h/l"
        self._write(height - 1, 0, " " * max(0, width - 1), self.colors["input"])
        self._write(height - 1, 2, input_prompt[: max(0, width - 4)], self.colors["input"])

    def _draw_panel(
        self,
        top: int,
        left: int,
        height: int,
        width: int,
        title: str,
        lines: list[str],
        *,
        selected_index: int | None = None,
        tone: str = "surface",
    ) -> None:
        if height < 4 or width < 12:
            return
        border_attr = self.colors["muted"]
        fill_attr = self.colors.get(tone, self.colors["surface"])
        self._box(top, left, height, width, border_attr, fill_attr)
        self._write(top, left + 2, f"[ {title} ]"[: max(0, width - 4)], self.colors["accent"] | curses.A_BOLD)

        inner_width = max(1, width - 2)
        row = top + 1
        for index, line in enumerate(lines):
            wrapped = _wrap(line, inner_width - 2)
            for segment in wrapped:
                if row >= top + height - 1:
                    return
                attr = fill_attr
                if selected_index is not None and index == selected_index:
                    attr = self.colors["selected"] | curses.A_BOLD
                self._write(row, left + 1, " " * (inner_width - 1), attr)
                self._write(row, left + 2, segment[: max(0, inner_width - 3)], attr)
                row += 1

    def _box(self, top: int, left: int, height: int, width: int, border_attr: int, fill_attr: int) -> None:
        for y in range(top + 1, top + height - 1):
            self._write(y, left + 1, " " * max(0, width - 2), fill_attr)
        try:
            self.screen.addch(top, left, curses.ACS_ULCORNER, border_attr)
            self.screen.hline(top, left + 1, curses.ACS_HLINE, max(0, width - 2), border_attr)
            self.screen.addch(top, left + width - 1, curses.ACS_URCORNER, border_attr)
            self.screen.vline(top + 1, left, curses.ACS_VLINE, max(0, height - 2), border_attr)
            self.screen.vline(top + 1, left + width - 1, curses.ACS_VLINE, max(0, height - 2), border_attr)
            self.screen.addch(top + height - 1, left, curses.ACS_LLCORNER, border_attr)
            self.screen.hline(top + height - 1, left + 1, curses.ACS_HLINE, max(0, width - 2), border_attr)
            self.screen.addch(top + height - 1, left + width - 1, curses.ACS_LRCORNER, border_attr)
        except curses.error:
            return

    def _write(self, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = self.screen.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        clipped = _string(text)[: max(0, width - x - 1)]
        try:
            self.screen.addstr(y, x, clipped, attr)
        except curses.error:
            return


def launch_tui(client: KestrelClient, config: dict[str, Any]) -> bool:
    try:
        if not os.environ.get("TERM") or os.environ.get("TERM") == "dumb":
            os.environ["TERM"] = "xterm-256color"
        curses.wrapper(lambda screen: KestrelTui(client, config).run(screen))
    except Exception:
        return False
    return True
