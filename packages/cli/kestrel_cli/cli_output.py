from __future__ import annotations

from . import cli_core as _cli_core

globals().update({name: value for name, value in vars(_cli_core).items() if not name.startswith("__")})

def print_logo():
    """Print the Kestrel logo."""
    print(c(LOGO, Colors.KESTREL))


def print_header(text: str):
    """Print a styled header."""
    width = max(len(Colors.strip(text)) + 4, 40)
    print()
    print(c("─" * width, Colors.KESTREL_DIM))
    print(c(f"  {text}", Colors.BOLD + Colors.KESTREL))
    print(c("─" * width, Colors.KESTREL_DIM))


def print_table(headers: list[str], rows: list[list[str]], widths: list[int] = None):
    """Print a formatted table."""
    if not widths:
        widths = [max(len(h), max((len(str(r[i])) for r in rows), default=0)) + 2
                  for i, h in enumerate(headers)]

    # Header
    header_line = ""
    for h, w in zip(headers, widths):
        header_line += c(h.ljust(w), Colors.BOLD + Colors.PRIMARY)
    print(header_line)
    print(c("─" * sum(widths), Colors.MUTED))

    # Rows
    for row in rows:
        line = ""
        for i, (cell, w) in enumerate(zip(row, widths)):
            cell_str = str(cell)[:w-1].ljust(w)
            if i == 0:
                line += c(cell_str, Colors.WHITE)
            else:
                line += c(cell_str, Colors.MUTED)
        print(line)


def print_event(event: dict):
    """Print a streaming task event."""
    event_type = event.get("type", "")

    if event_type == "thinking":
        content = event.get("content", "")
        if content:
            print(c(f"  💭 {content[:200]}", Colors.MUTED + Colors.ITALIC))

    elif event_type == "message":
        content = event.get("content", "")
        print(f"  {content}")

    elif event_type == "tool_call":
        tool = event.get("toolName", "")
        print(c(f"  🔧 {tool}", Colors.CYAN), end="")
        args = event.get("toolArgs", "")
        if args:
            print(c(f" {str(args)[:80]}", Colors.DIM), end="")
        print()

    elif event_type == "tool_result":
        result = event.get("toolResult", "")
        if result:
            result_str = str(result)[:150]
            print(c(f"     → {result_str}", Colors.SUCCESS))

    elif event_type == "plan_created":
        summary = event.get("content", "")
        print(c(f"  🗺  {summary}", Colors.PRIMARY))

    elif event_type == "step_started":
        summary = event.get("content", "")
        print(c(f"  ▶  {summary}", Colors.CYAN))

    elif event_type == "step_complete":
        summary = event.get("content", "")
        print(c(f"  ✓ {summary[:180]}", Colors.SUCCESS))

    elif event_type == "approval_needed":
        approval_id = event.get("approvalId", "")
        print()
        print(c("  ⚠️  APPROVAL NEEDED", Colors.WARNING + Colors.BOLD))
        print(c(f"  Action: {event.get('content', '')}", Colors.WARNING))
        print(c(f"  ID: {approval_id}", Colors.DIM))
        print()

    elif event_type == "error":
        print(c(f"  ❌ {event.get('content', 'Unknown error')}", Colors.ERROR))

    elif event_type == "status":
        status = event.get("content", "")
        print(c(f"  📋 {status}", Colors.PRIMARY))

    elif event_type == "task_resumed":
        print(c(f"  ↻ {event.get('content', '')}", Colors.PRIMARY))

    elif event_type == "task_complete":
        print(c(f"  ✅ {event.get('content', '')[:200]}", Colors.SUCCESS))

    elif event_type == "task_failed":
        print(c(f"  ❌ {event.get('content', '')[:200]}", Colors.ERROR))

    elif event_type == "metrics_update":
        metrics = event.get("metrics", {})
        if metrics:
            tokens = metrics.get("tokens", 0)
            cost = metrics.get("cost_usd", 0)
            print(c(f"  📊 {tokens:,} tokens · ${cost:.4f}", Colors.MUTED))


def print_success(msg: str):
    print(c(f"  ✅ {msg}", Colors.SUCCESS))


def print_error(msg: str):
    print(c(f"  ❌ {msg}", Colors.ERROR))


def print_warning(msg: str):
    print(c(f"  ⚠️  {msg}", Colors.WARNING))


def print_info(msg: str):
    print(c(f"  ℹ️  {msg}", Colors.PRIMARY))


# ── Command Handlers ─────────────────────────────────────────────────

