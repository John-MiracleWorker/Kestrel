from __future__ import annotations

import shutil
import textwrap

from . import cli_core as _cli_core

globals().update({name: value for name, value in vars(_cli_core).items() if not name.startswith("__")})


def _terminal_width(default: int = 96) -> int:
    return max(72, shutil.get_terminal_size((default, 24)).columns)


def _plain(text: str) -> str:
    return Colors.strip(str(text))


def _truncate(text: str, width: int) -> str:
    plain = _plain(text)
    if len(plain) <= width:
        return plain
    if width <= 1:
        return plain[:width]
    return plain[: width - 1] + "…"


def badge(text: str, fg: str = Colors.WHITE, bg: str = Colors.MUTED_BG) -> str:
    return c(f" {text} ", Colors.BOLD + fg + bg)


def print_panel(title: str, body: str | list[str], tone: str = "default") -> None:
    palette = {
        "default": (Colors.KESTREL_DIM, Colors.SOFT),
        "info": (Colors.PRIMARY, Colors.SOFT),
        "success": (Colors.SUCCESS, Colors.SOFT),
        "warning": (Colors.WARNING, Colors.SOFT),
        "error": (Colors.ERROR, Colors.SOFT),
    }
    border_color, text_color = palette.get(tone, palette["default"])
    raw_lines = body if isinstance(body, list) else str(body).splitlines()
    content_width = max(36, min(_terminal_width() - 4, 88))

    wrapped_lines: list[str] = []
    for line in raw_lines or [""]:
        plain = _plain(line)
        if not plain:
            wrapped_lines.append("")
            continue
        wrapped_lines.extend(
            textwrap.wrap(
                plain,
                width=content_width,
                replace_whitespace=False,
                drop_whitespace=False,
            )
            or [""]
        )

    inner_width = min(
        max(len(_plain(title)) + 6, max((len(_plain(line)) for line in wrapped_lines), default=0), 36),
        content_width,
    )
    title_text = f" {title.upper()} "
    top = "╭" + "─" + title_text + "─" * max(0, inner_width - len(title_text) - 1) + "╮"
    bottom = "╰" + "─" * inner_width + "╯"

    print(c(top, border_color))
    for line in wrapped_lines or [""]:
        plain = _plain(line)
        padding = " " * max(0, inner_width - len(plain))
        print(f"{c('│', border_color)}{c(plain + padding, text_color)}{c('│', border_color)}")
    print(c(bottom, border_color))


def repl_prompt(context: dict | None = None) -> str:
    ctx = context or {}
    model = _truncate(str(ctx.get("model") or "auto"), 28)
    thinking = str(ctx.get("thinking_level") or "medium")
    usage = str(ctx.get("usage_mode") or "tokens")
    return (
        f"{badge('KESTREL', Colors.INK, Colors.KESTREL_BG)} "
        f"{badge(model, Colors.WHITE, Colors.SURFACE_ACCENT)} "
        f"{c(f'think {thinking}', Colors.MUTED)} {c('·', Colors.KESTREL_DIM)} {c(usage, Colors.MUTED)}\n"
        f"{c('╰─', Colors.KESTREL_DIM)} {c('›', Colors.PRIMARY + Colors.BOLD)} "
    )


def print_logo(context: dict | None = None):
    """Print the Kestrel logo."""
    ctx = context or {}
    model = _truncate(str(ctx.get("model") or "auto"), 28)
    thinking = str(ctx.get("thinking_level") or "medium")
    usage = str(ctx.get("usage_mode") or "tokens")
    print()
    print(
        f"{badge('KESTREL', Colors.INK, Colors.KESTREL_BG)} "
        f"{badge('CLI', Colors.WHITE, Colors.PRIMARY_BG)} "
        f"{c('Telegram-first operator shell', Colors.BOLD + Colors.WHITE)}"
    )
    print(c("  Tasks, skills, approvals, runtime control, and local agent loops in one terminal surface.", Colors.MUTED))
    print(
        "  "
        + badge("TASKS", Colors.WHITE, Colors.SURFACE_ACCENT)
        + " "
        + badge("SKILLS", Colors.WHITE, Colors.SURFACE_ACCENT)
        + " "
        + badge("APPROVALS", Colors.WHITE, Colors.SURFACE_ACCENT)
        + " "
        + badge("RUNTIME", Colors.WHITE, Colors.SURFACE_ACCENT)
    )
    print(
        "  "
        + badge(model, Colors.WHITE, Colors.SURFACE_ALT)
        + " "
        + badge(f"thinking {thinking}", Colors.WHITE, Colors.MUTED_BG)
        + " "
        + badge(usage, Colors.WHITE, Colors.SURFACE_SOFT)
    )
    print(
        "  "
        + badge("/help", Colors.WHITE, Colors.SURFACE_SOFT)
        + " "
        + badge("/status", Colors.WHITE, Colors.SURFACE_SOFT)
        + " "
        + badge("/think", Colors.WHITE, Colors.SURFACE_SOFT)
        + " "
        + badge("!goal", Colors.WHITE, Colors.SURFACE_SOFT)
    )
    print(c("  " + "─" * min(_terminal_width() - 2, 92), Colors.KESTREL_DIM))


def print_header(text: str):
    """Print a styled header."""
    width = min(max(len(Colors.strip(text)) + 18, 40), _terminal_width() - 2)
    print()
    print(c("─" * width, Colors.KESTREL_DIM))
    print(f"{badge(text, Colors.INK, Colors.KESTREL_BG)} {c('Operator View', Colors.MUTED)}")
    print(c("─" * width, Colors.KESTREL_DIM))


def print_table(headers: list[str], rows: list[list[str]], widths: list[int] = None):
    """Print a formatted table."""
    if not widths:
        widths = [max(len(h), max((len(str(r[i])) for r in rows), default=0)) + 2
                  for i, h in enumerate(headers)]

    def border(left: str, join: str, right: str) -> str:
        return c(
            left + join.join("─" * (w + 2) for w in widths) + right,
            Colors.KESTREL_DIM,
        )

    def cell(text: str, width: int, fg: str, bg: str = "") -> str:
        content = _truncate(str(text), width)
        padding = " " * max(0, width - len(_plain(content)))
        return c(f" {content}{padding} ", fg + bg)

    print(border("╭", "┬", "╮"))
    header_row = "│" + "│".join(
        cell(header, width, Colors.WHITE + Colors.BOLD, Colors.SURFACE_ACCENT)
        for header, width in zip(headers, widths)
    ) + "│"
    print(c(header_row, Colors.KESTREL_DIM))
    print(border("├", "┼", "┤"))

    for index, row in enumerate(rows):
        row_bg = Colors.SURFACE if index % 2 == 0 else Colors.SURFACE_ALT
        body_row = "│" + "│".join(
            cell(
                row[i] if i < len(row) else "",
                widths[i],
                Colors.WHITE if i == 0 else Colors.SOFT,
                row_bg,
            )
            for i in range(len(widths))
        ) + "│"
        print(c(body_row, Colors.KESTREL_DIM))

    print(border("╰", "┴", "╯"))


def print_event(event: dict):
    """Print a streaming task event."""
    event_type = event.get("type", "")

    if event_type == "thinking":
        content = event.get("content", "")
        if content:
            print(f"  {badge('THINK', Colors.WHITE, Colors.MUTED_BG)} {c(content[:200], Colors.MUTED + Colors.ITALIC)}")

    elif event_type == "message":
        content = event.get("content", "")
        print(c(f"  {content}", Colors.SOFT))

    elif event_type == "tool_call":
        tool = event.get("toolName", "")
        print(f"  {badge('TOOL', Colors.WHITE, Colors.PRIMARY_BG)} {c(tool, Colors.CYAN + Colors.BOLD)}", end="")
        args = event.get("toolArgs", "")
        if args:
            print(c(f" {str(args)[:80]}", Colors.DIM), end="")
        print()

    elif event_type == "tool_result":
        result = event.get("toolResult", "")
        if result:
            result_str = str(result)[:150]
            print(f"    {badge('RESULT', Colors.WHITE, Colors.SUCCESS_BG)} {c(result_str, Colors.SUCCESS)}")

    elif event_type == "plan_created":
        summary = event.get("content", "")
        print(f"  {badge('PLAN', Colors.WHITE, Colors.PRIMARY_BG)} {c(summary, Colors.PRIMARY)}")

    elif event_type == "step_started":
        summary = event.get("content", "")
        print(f"  {badge('STEP', Colors.WHITE, Colors.SURFACE_ACCENT)} {c(summary, Colors.CYAN)}")

    elif event_type == "step_complete":
        summary = event.get("content", "")
        print(f"  {badge('DONE', Colors.WHITE, Colors.SUCCESS_BG)} {c(summary[:180], Colors.SUCCESS)}")

    elif event_type == "approval_needed":
        approval_id = event.get("approvalId", "")
        print()
        print(f"  {badge('APPROVAL', Colors.WHITE, Colors.WARNING_BG)} {c(event.get('content', ''), Colors.WARNING + Colors.BOLD)}")
        print(c(f"  id: {approval_id}", Colors.DIM))
        print()

    elif event_type == "error":
        print(f"  {badge('ERROR', Colors.WHITE, Colors.ERROR_BG)} {c(event.get('content', 'Unknown error'), Colors.ERROR)}")

    elif event_type == "status":
        status = event.get("content", "")
        print(f"  {badge('STATUS', Colors.WHITE, Colors.SURFACE_ACCENT)} {c(status, Colors.PRIMARY)}")

    elif event_type == "task_resumed":
        print(f"  {badge('RESUME', Colors.WHITE, Colors.PRIMARY_BG)} {c(event.get('content', ''), Colors.PRIMARY)}")

    elif event_type == "task_complete":
        print(f"  {badge('COMPLETE', Colors.WHITE, Colors.SUCCESS_BG)} {c(event.get('content', '')[:200], Colors.SUCCESS)}")

    elif event_type == "task_failed":
        print(f"  {badge('FAILED', Colors.WHITE, Colors.ERROR_BG)} {c(event.get('content', '')[:200], Colors.ERROR)}")

    elif event_type == "metrics_update":
        metrics = event.get("metrics", {})
        if metrics:
            tokens = metrics.get("tokens", 0)
            cost = metrics.get("cost_usd", 0)
            print(f"  {badge('METRICS', Colors.WHITE, Colors.SURFACE_ACCENT)} {c(f'{tokens:,} tokens · ${cost:.4f}', Colors.MUTED)}")


def print_success(msg: str):
    print(f"  {badge('OK', Colors.WHITE, Colors.SUCCESS_BG)} {c(msg, Colors.SUCCESS)}")


def print_error(msg: str):
    print(f"  {badge('ERROR', Colors.WHITE, Colors.ERROR_BG)} {c(msg, Colors.ERROR)}")


def print_warning(msg: str):
    print(f"  {badge('WARN', Colors.WHITE, Colors.WARNING_BG)} {c(msg, Colors.WARNING)}")


def print_info(msg: str):
    print(f"  {badge('INFO', Colors.WHITE, Colors.PRIMARY_BG)} {c(msg, Colors.PRIMARY)}")


# ── Command Handlers ─────────────────────────────────────────────────
