from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, Input, OptionList, RichLog, Static
from textual.widgets.option_list import Option

from .store import ChatMessage, NotificationItem, ProcessSnapshot, fuzzy_match, relative_age


ACCENT_COLORS = {
    "cyan": "#00f3ff",
    "green": "#00ff9d",
    "purple": "#bd00ff",
    "error": "#ff0055",
    "warning": "#f59e0b",
    "muted": "#888888",
}


@dataclass
class PaletteAction:
    id: str
    label: str
    category: str
    subtitle: str = ""
    shortcut: str = ""


def render_chat_message(message: ChatMessage) -> Panel:
    border_style = {
        "user": "#00f3ff",
        "assistant": "#00ff9d",
        "task": "#bd00ff",
        "system": "#888888",
        "error": "#ff0055",
    }.get(message.role, "#888888")
    title = Text()
    title.append(message.role.upper(), style=f"bold {border_style}")
    if message.meta:
        title.append(" // ", style="#444444")
        title.append(message.meta, style="#888888")

    body = Text(message.text or "", style="#e0e0e0")
    if message.approval:
        body.append("\n\n")
        body.append("APPROVAL REQUIRED", style="bold #f59e0b")
        body.append(f"\n{message.approval.get('summary') or message.approval.get('command') or ''}", style="#f59e0b")
    if message.artifacts:
        body.append("\n\n")
        body.append("ARTIFACTS", style="bold #00f3ff")
        for artifact in message.artifacts[:5]:
            label = artifact.get("name") or artifact.get("path") or "artifact"
            body.append(f"\n• {label}", style="#00f3ff")

    return Panel(
        body,
        title=title,
        border_style=border_style,
        padding=(1, 2),
        expand=True,
    )


def render_notifications(notifications: Iterable[NotificationItem]) -> list[Panel]:
    panels: list[Panel] = []
    for item in notifications:
        color = {
            "info": "#00f3ff",
            "success": "#00ff9d",
            "warning": "#f59e0b",
            "error": "#ff0055",
        }.get(item.level, "#888888")
        body = Text(item.text, style="#e0e0e0")
        if item.read:
            body.stylize("#666666")
        subtitle_text = f"{relative_age(time_to_iso(item.created_at))} ago"
        if item.occurrences > 1:
            subtitle_text += f"  //  repeated x{item.occurrences}"
        subtitle = Text(subtitle_text, style="#666666")
        panels.append(
            Panel(
                Group(body, subtitle),
                title=Text(item.title.upper(), style=f"bold {color}"),
                border_style=color,
                padding=(1, 2),
                expand=True,
            )
        )
    return panels


def time_to_iso(epoch_seconds: float) -> str:
    import datetime as _datetime

    return _datetime.datetime.utcfromtimestamp(epoch_seconds).strftime("%Y-%m-%dT%H:%M:%SZ")


class StatusOrb(Static):
    DEFAULT_CSS = """
    StatusOrb {
        width: auto;
        height: auto;
        content-align: left middle;
    }
    """

    orb_state = reactive("idle")
    label = reactive("ONLINE")
    motion_level = reactive("high")
    _frame = reactive(0)

    def on_mount(self) -> None:
        self.set_interval(0.28, self._tick)

    def _tick(self) -> None:
        if self.motion_level == "off":
            return
        self._frame = (self._frame + 1) % 4

    def set_status(self, state: str, label: str, *, motion_level: str = "high") -> None:
        self.orb_state = state
        self.label = label
        self.motion_level = motion_level

    def render(self) -> Text:
        palette = {
            "idle": ("#00f3ff", ["◉", "◎", "◉", "◌"]),
            "thinking": ("#bd00ff", ["◐", "◓", "◑", "◒"]),
            "executing": ("#00ff9d", ["◆", "◇", "◆", "◈"]),
            "approval": ("#f59e0b", ["▲", "△", "▲", "△"]),
            "error": ("#ff0055", ["✕", "✸", "✕", "✷"]),
            "offline": ("#666666", ["○", "○", "○", "○"]),
        }
        color, glyphs = palette.get(self.orb_state, palette["idle"])
        glyph = glyphs[0] if self.motion_level == "off" else glyphs[self._frame % len(glyphs)]
        text = Text()
        text.append(f"{glyph} ", style=f"bold {color}")
        text.append(self.label, style=f"bold {color}")
        return text


class ProcessBar(Static):
    DEFAULT_CSS = """
    ProcessBar {
        height: auto;
        min-height: 6;
    }
    """

    motion_level = reactive("high")
    _frame = reactive(0)

    def __init__(self, snapshot: ProcessSnapshot | None = None, **kwargs):
        super().__init__(**kwargs)
        self.snapshot = snapshot or ProcessSnapshot()

    def on_mount(self) -> None:
        self.set_interval(0.22, self._tick)

    def _tick(self) -> None:
        if self.motion_level == "off":
            return
        self._frame = (self._frame + 1) % 24
        if self.snapshot.active:
            self.refresh()

    def set_process(self, snapshot: ProcessSnapshot, *, motion_level: str = "high") -> None:
        self.snapshot = snapshot
        self.motion_level = motion_level
        self.refresh()

    def render(self) -> Panel:
        accent = ACCENT_COLORS.get(self.snapshot.accent, "#00f3ff")
        phase_text = Text(" ".join(f"[{phase.upper()}]" for phase in self.snapshot.phases) or "[READY]", style=accent)
        header = Text()
        header.append(self.snapshot.label, style=f"bold {accent}")
        if self.snapshot.detail:
            header.append(" // ", style="#444444")
            header.append(self.snapshot.detail[:72], style="#888888")

        progress_width = 28
        track = ["─"] * progress_width
        if self.snapshot.active:
            cursor = self._frame % progress_width
            for offset in range(5):
                track[(cursor + offset) % progress_width] = "━"
        line = Text("".join(track), style="#222222")
        if self.snapshot.active:
            start = self._frame % progress_width
            for offset in range(5):
                line.stylize(accent, (start + offset) % progress_width, ((start + offset) % progress_width) + 1)
        if self.motion_level == "off":
            line = Text("━" * progress_width, style=accent if self.snapshot.active else "#222222")

        body_items: list[object] = [header, line, phase_text]
        if self.snapshot.thinking:
            body_items.append(Text(self.snapshot.thinking[:160], style="#888888"))

        return Panel(
            Group(*body_items),
            title=Text("PROCESS", style=f"bold {accent}"),
            border_style=accent,
            padding=(1, 2),
            expand=True,
        )


class MetricCard(Static):
    DEFAULT_CSS = """
    MetricCard {
        height: auto;
        min-height: 8;
    }
    """

    def __init__(self, title: str = "", value: str = "", subtitle: str = "", accent: str = "#00f3ff", **kwargs):
        super().__init__(**kwargs)
        self.title = title
        self.value = value
        self.subtitle = subtitle
        self.accent = accent

    def set_metric(self, *, title: str, value: str, subtitle: str = "", accent: str = "#00f3ff") -> None:
        self.title = title
        self.value = value
        self.subtitle = subtitle
        self.accent = accent
        self.refresh()

    def render(self) -> Panel:
        body = Text()
        body.append(self.value, style=f"bold {self.accent}")
        if self.subtitle:
            body.append(f"\n{self.subtitle}", style="#888888")
        return Panel(
            body,
            title=Text(self.title.upper(), style=f"bold {self.accent}"),
            border_style=self.accent,
            padding=(1, 2),
            expand=True,
        )


class InspectorBody(RichLog):
    DEFAULT_CSS = """
    InspectorBody {
        background: transparent;
        border: none;
    }
    """

    def write_renderables(self, renderables: list[object]) -> None:
        self.clear()
        for item in renderables:
            self.write(item)


class InspectorPane(Vertical):
    DEFAULT_CSS = """
    InspectorPane {
        height: 1fr;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("INSPECTOR", id="inspector-title")
        yield Static("Selection details", id="inspector-subtitle")
        yield InspectorBody(id="inspector-body", wrap=True, highlight=False, markup=False)

    def show_renderables(self, *, title: str, subtitle: str = "", renderables: list[object] | None = None) -> None:
        self.query_one("#inspector-title", Static).update(title)
        self.query_one("#inspector-subtitle", Static).update(subtitle)
        self.query_one("#inspector-body", InspectorBody).write_renderables(renderables or [])


class CommandPaletteScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Close"), ("enter", "submit", "Select")]

    def __init__(self, actions: list[PaletteAction]):
        super().__init__()
        self.actions = actions
        self.filtered = list(actions)

    def compose(self) -> ComposeResult:
        with Vertical(id="palette-modal"):
            yield Input(placeholder="Type a command...", id="palette-input")
            yield OptionList(id="palette-options")

    def on_mount(self) -> None:
        self._refresh_options()
        palette_input = self.query_one("#palette-input", Input)
        palette_input.focus()

    def _rank_actions(self, query: str) -> list[PaletteAction]:
        needle = query.strip().lower()
        if not needle:
            return list(self.actions)
        label_direct = [action for action in self.actions if needle in action.label.lower()]
        category_direct = [action for action in self.actions if needle in action.category.lower() and action not in label_direct]
        subtitle_direct = [action for action in self.actions if needle in action.subtitle.lower() and action not in label_direct and action not in category_direct]
        if label_direct or category_direct or subtitle_direct:
            return label_direct + category_direct + subtitle_direct

        label_fuzzy = [action for action in self.actions if fuzzy_match(action.label, needle)]
        category_fuzzy = [
            action for action in self.actions if action not in label_fuzzy and fuzzy_match(action.category, needle)
        ]
        subtitle_fuzzy = [
            action
            for action in self.actions
            if action not in label_fuzzy and action not in category_fuzzy and fuzzy_match(action.subtitle, needle)
        ]
        return label_fuzzy + category_fuzzy + subtitle_fuzzy

    def _refresh_options(self) -> None:
        options = self.query_one("#palette-options", OptionList)
        options.clear_options()
        for action in self.filtered:
            label = Text()
            label.append(action.label, style="bold #e0e0e0")
            if action.shortcut:
                label.append(f"  {action.shortcut}", style="#00f3ff")
            if action.subtitle:
                label.append(f"\n{action.subtitle}", style="#888888")
            options.add_option(Option(label, id=action.id))
        if self.filtered:
            options.highlighted = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        self.filtered = self._rank_actions(query)
        self._refresh_options()

    def action_submit(self) -> None:
        query = self.query_one("#palette-input", Input).value.strip()
        ranked = self._rank_actions(query) or list(self.actions)
        selection = ranked[0].id if ranked else None
        self.dismiss(selection)

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self.action_submit()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class NotificationCenterScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "dismiss(None)", "Close")]

    def __init__(self, notifications: list[NotificationItem]):
        super().__init__()
        self.notifications = notifications

    def compose(self) -> ComposeResult:
        with Vertical(id="notifications-modal"):
            with Horizontal(id="notifications-actions"):
                yield Static("NOTIFICATIONS", id="notifications-title")
                yield Button("MARK ALL READ", id="notifications-mark-read", variant="primary")
                yield Button("CLOSE", id="notifications-close")
            yield RichLog(id="notifications-log", wrap=True, highlight=False, markup=False)

    def on_mount(self) -> None:
        log = self.query_one("#notifications-log", RichLog)
        log.clear()
        if not self.notifications:
            log.write(Panel(Text("No notifications yet", style="#888888"), border_style="#222222"))
            return
        for panel in render_notifications(self.notifications):
            log.write(panel)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "notifications-mark-read":
            self.dismiss("mark_read")
            return
        self.dismiss(None)


def render_key_value_table(title: str, rows: list[tuple[str, str]], *, accent: str = "#00f3ff") -> Panel:
    table = Table.grid(expand=True)
    table.add_column(style="#888888", ratio=1)
    table.add_column(style="#e0e0e0", ratio=2)
    for key, value in rows:
        table.add_row(key, value)
    return Panel(table, title=Text(title.upper(), style=f"bold {accent}"), border_style=accent, padding=(1, 2))
