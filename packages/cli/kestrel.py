#!/usr/bin/env python3
"""
Kestrel CLI — a powerful command-line interface for the Kestrel agent platform.

Provides full access to all Kestrel features without a browser:
  - Interactive chat with streaming responses
  - Task management (start, list, cancel, approve)
  - Workflow templates (list, launch)
  - Memory graph exploration
  - Evidence chain inspection
  - Cron & webhook management
  - Session monitoring
  - /slash commands work directly in the CLI

Usage:
    kestrel                     # Interactive REPL
    kestrel chat "message"      # One-shot message
    kestrel task "goal"         # Start an autonomous task
    kestrel tasks               # List tasks
    kestrel workflows           # List workflow templates
    kestrel graph               # Explore the memory graph
    kestrel evidence <task_id>  # Show evidence chain
    kestrel status              # System status
    kestrel cron                # Manage cron jobs
    kestrel webhooks            # Manage webhooks
    kestrel config              # Configure settings
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from typing import Optional

# ── ANSI Colors ──────────────────────────────────────────────────────

class Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"

    # Kestrel brand colors
    KESTREL = "\033[38;5;208m"      # Orange/amber
    KESTREL_DIM = "\033[38;5;172m"
    PRIMARY = "\033[38;5;75m"       # Sky blue
    SUCCESS = "\033[38;5;114m"      # Green
    WARNING = "\033[38;5;214m"      # Yellow
    ERROR = "\033[38;5;203m"        # Red
    MUTED = "\033[38;5;245m"        # Gray
    CYAN = "\033[38;5;81m"
    PURPLE = "\033[38;5;141m"
    WHITE = "\033[38;5;255m"

    @staticmethod
    def strip(text: str) -> str:
        """Strip ANSI codes from text."""
        import re
        return re.sub(r'\033\[[0-9;]*m', '', text)


def c(text: str, color: str) -> str:
    """Colorize text."""
    return f"{color}{text}{Colors.RESET}"


# ── Configuration ────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "api_url": "http://localhost:3000",
    "api_key": "",
    "workspace_id": "",
    "model": "gpt-4o",
    "thinking_level": "medium",
    "usage_mode": "tokens",
    "verbose": False,
    "theme": "dark",
}


def get_config_path() -> str:
    """Get the config file path."""
    home = os.path.expanduser("~")
    config_dir = os.path.join(home, ".kestrel")
    os.makedirs(config_dir, exist_ok=True)
    return os.path.join(config_dir, "config.json")


def load_config() -> dict:
    """Load configuration from disk."""
    path = get_config_path()
    if os.path.exists(path):
        with open(path, "r") as f:
            stored = json.load(f)
            return {**DEFAULT_CONFIG, **stored}
    return dict(DEFAULT_CONFIG)


def save_config(config: dict) -> None:
    """Save configuration to disk."""
    path = get_config_path()
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


# ── HTTP Client ──────────────────────────────────────────────────────

class KestrelClient:
    """HTTP client for the Kestrel gateway API."""

    def __init__(self, config: dict):
        self.base_url = config["api_url"].rstrip("/")
        self.api_key = config.get("api_key", "")
        self.workspace_id = config.get("workspace_id", "")

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def _request(self, method: str, path: str, body: dict = None) -> dict:
        """Make an HTTP request."""
        import httpx
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=60) as client:
            if method == "GET":
                resp = await client.get(url, headers=self._headers())
            elif method == "POST":
                resp = await client.post(url, headers=self._headers(), json=body)
            elif method == "DELETE":
                resp = await client.delete(url, headers=self._headers())
            else:
                raise ValueError(f"Unknown method: {method}")

            if resp.status_code >= 400:
                return {"error": f"HTTP {resp.status_code}: {resp.text}"}
            return resp.json()

    async def stream_sse(self, path: str, body: dict):
        """Stream SSE events from the API."""
        import httpx
        url = f"{self.base_url}{path}"
        async with httpx.AsyncClient(timeout=300) as client:
            async with client.stream("POST", url, headers=self._headers(), json=body) as response:
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        event_str, buffer = buffer.split("\n\n", 1)
                        for line in event_str.split("\n"):
                            if line.startswith("data: "):
                                try:
                                    yield json.loads(line[6:])
                                except json.JSONDecodeError:
                                    pass

    # ── API Methods ─────────────────────────────────────────────

    async def start_task(self, goal: str, workspace_id: str = None):
        ws = workspace_id or self.workspace_id
        path = f"/api/workspaces/{ws}/tasks"
        async for event in self.stream_sse(path, {"goal": goal}):
            yield event

    async def list_tasks(self, status: str = None) -> dict:
        path = f"/api/workspaces/{self.workspace_id}/tasks"
        if status:
            path += f"?status={status}"
        return await self._request("GET", path)

    async def cancel_task(self, task_id: str) -> dict:
        return await self._request("POST", f"/api/tasks/{task_id}/cancel")

    async def approve_task(self, task_id: str, approval_id: str, approved: bool) -> dict:
        return await self._request("POST", f"/api/tasks/{task_id}/approve", {
            "approvalId": approval_id,
            "approved": approved,
        })

    async def list_workflows(self) -> dict:
        return await self._request("GET", "/api/workflows")

    async def list_cron_jobs(self) -> dict:
        return await self._request("GET", f"/api/workspaces/{self.workspace_id}/automation/cron")

    async def list_webhooks(self) -> dict:
        return await self._request("GET", f"/api/workspaces/{self.workspace_id}/automation/webhooks")


# ── Display Helpers ──────────────────────────────────────────────────

LOGO = r"""
    ╔═══════════════════════════════════╗
    ║                                   ║
    ║   🦅  K E S T R E L   C L I      ║
    ║       Autonomous Agent Platform   ║
    ║                                   ║
    ╚═══════════════════════════════════╝
"""

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

async def cmd_task(client: KestrelClient, args: argparse.Namespace):
    """Start an autonomous agent task."""
    goal = " ".join(args.goal)
    print_header("Starting Task")
    print(c(f"  Goal: {goal}", Colors.WHITE))
    print()

    start_time = time.time()
    async for event in client.start_task(goal):
        print_event(event)

    elapsed = time.time() - start_time
    print()
    print(c(f"  ⏱  Completed in {elapsed:.1f}s", Colors.MUTED))


async def cmd_tasks(client: KestrelClient, args: argparse.Namespace):
    """List agent tasks."""
    print_header("Tasks")
    result = await client.list_tasks(args.status if hasattr(args, "status") else None)

    tasks = result.get("tasks", [])
    if not tasks:
        print_info("No tasks found")
        return

    headers = ["ID", "Goal", "Status", "Created"]
    rows = []
    for t in tasks:
        rows.append([
            t.get("id", "")[:8],
            (t.get("goal", ""))[:40],
            t.get("status", ""),
            t.get("created_at", "")[:16],
        ])

    print_table(headers, rows, [10, 42, 12, 18])


async def cmd_workflows(client: KestrelClient, args: argparse.Namespace):
    """List workflow templates."""
    print_header("Workflow Templates")
    result = await client.list_workflows()

    workflows = result.get("workflows", [])
    if not workflows:
        print_info("No workflows available")
        return

    for wf in workflows:
        icon = wf.get("icon", "📋")
        name = wf.get("name", "")
        desc = wf.get("description", "")[:60]
        category = wf.get("category", "")
        print(f"  {icon} {c(name, Colors.BOLD + Colors.WHITE)}  {c(f'[{category}]', Colors.MUTED)}")
        print(c(f"     {desc}", Colors.DIM))
        print()


async def cmd_cron(client: KestrelClient, args: argparse.Namespace):
    """List cron jobs."""
    print_header("Cron Jobs")
    result = await client.list_cron_jobs()

    jobs = result.get("jobs", [])
    if not jobs:
        print_info("No cron jobs configured")
        return

    headers = ["Name", "Schedule", "Status", "Runs", "Last Run"]
    rows = []
    for j in jobs:
        rows.append([
            j.get("name", "")[:20],
            j.get("cron_expression", ""),
            j.get("status", ""),
            str(j.get("run_count", 0)),
            (j.get("last_run", "never") or "never")[:16],
        ])

    print_table(headers, rows, [22, 16, 10, 6, 18])


async def cmd_webhooks(client: KestrelClient, args: argparse.Namespace):
    """List webhook endpoints."""
    print_header("Webhook Endpoints")
    result = await client.list_webhooks()

    webhooks = result.get("webhooks", [])
    if not webhooks:
        print_info("No webhooks configured")
        return

    headers = ["Name", "Status", "Triggers", "Has Secret"]
    rows = []
    for w in webhooks:
        rows.append([
            w.get("name", "")[:25],
            w.get("status", ""),
            str(w.get("trigger_count", 0)),
            "✓" if w.get("has_secret") else "✗",
        ])

    print_table(headers, rows, [27, 10, 10, 12])


async def cmd_status(client: KestrelClient, args: argparse.Namespace):
    """Show system status."""
    config = load_config()
    print_header("System Status")
    print(f"  {c('API:', Colors.MUTED)}        {c(config['api_url'], Colors.PRIMARY)}")
    print(f"  {c('Workspace:', Colors.MUTED)}  {c(config.get('workspace_id', 'not set'), Colors.WHITE)}")
    print(f"  {c('Model:', Colors.MUTED)}      {c(config.get('model', 'default'), Colors.WHITE)}")
    print(f"  {c('Thinking:', Colors.MUTED)}   {c(config.get('thinking_level', 'medium'), Colors.WHITE)}")
    print(f"  {c('Usage:', Colors.MUTED)}      {c(config.get('usage_mode', 'tokens'), Colors.WHITE)}")


async def cmd_config(client: KestrelClient, args: argparse.Namespace):
    """Configure Kestrel CLI settings."""
    config = load_config()

    if hasattr(args, "key") and args.key:
        key = args.key
        if hasattr(args, "value") and args.value:
            config[key] = args.value
            save_config(config)
            print_success(f"{key} = {args.value}")
        else:
            val = config.get(key, "(not set)")
            print(f"  {c(key, Colors.PRIMARY)} = {c(str(val), Colors.WHITE)}")
    else:
        print_header("Configuration")
        for k, v in config.items():
            if k == "api_key" and v:
                v = v[:8] + "..." + v[-4:]
            print(f"  {c(k, Colors.PRIMARY):>30} = {c(str(v), Colors.WHITE)}")
        print()
        print(c("  Set a value: kestrel config <key> <value>", Colors.DIM))


# ── Interactive REPL ─────────────────────────────────────────────────

async def interactive_repl(client: KestrelClient, config: dict):
    """Run the interactive Kestrel REPL."""
    from agent.commands import CommandParser  # noqa: delayed import

    parser = CommandParser()
    print_logo()
    print(c("  Type a message to chat, /help for commands, or Ctrl+C to exit.", Colors.DIM))
    print()

    context = {
        "model": config.get("model", ""),
        "total_tokens": 0,
        "cost_usd": 0,
        "task_status": "idle",
        "session_type": "main",
        "thinking_level": config.get("thinking_level", "medium"),
        "usage_mode": config.get("usage_mode", "tokens"),
    }

    while True:
        try:
            prompt = c("🦅 kestrel", Colors.KESTREL) + c(" ❯ ", Colors.MUTED)
            user_input = input(prompt).strip()

            if not user_input:
                continue

            # Check for /commands
            if parser.is_command(user_input):
                result = parser.parse(user_input, context)
                if result:
                    print(f"\n{result.response}\n")

                    # Apply side effects
                    se = result.side_effects
                    if se.get("action") == "set_thinking_level":
                        context["thinking_level"] = se["value"]
                        config["thinking_level"] = se["value"]
                        save_config(config)
                    elif se.get("action") == "set_usage_mode":
                        context["usage_mode"] = se["value"]
                        config["usage_mode"] = se["value"]
                        save_config(config)
                    elif se.get("action") == "set_model":
                        context["model"] = se["value"]
                        config["model"] = se["value"]
                        save_config(config)
                    elif se.get("action") == "reset_session":
                        context["total_tokens"] = 0
                        context["cost_usd"] = 0
                continue

            # Check for task prefix
            if user_input.startswith("!"):
                # Direct task: !goal launches an autonomous task
                goal = user_input[1:].strip()
                if goal:
                    print()
                    start_time = time.time()
                    async for event in client.start_task(goal):
                        print_event(event)
                    elapsed = time.time() - start_time
                    print(c(f"\n  ⏱  Task completed in {elapsed:.1f}s\n", Colors.MUTED))
                continue

            # Regular chat message — stream via SSE
            print()
            async for event in client.start_task(user_input):
                print_event(event)
            print()

        except KeyboardInterrupt:
            print(c("\n\n  Goodbye! 🦅\n", Colors.KESTREL))
            break
        except EOFError:
            break
        except Exception as e:
            print_error(str(e))


# ── Main Entry Point ─────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="kestrel",
        description="🦅 Kestrel CLI — Autonomous Agent Platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  kestrel                              Interactive REPL
  kestrel task "review auth module"    Start an autonomous task
  kestrel tasks                        List all tasks
  kestrel workflows                    Browse workflow templates
  kestrel cron                         Manage scheduled jobs
  kestrel webhooks                     Manage webhook endpoints
  kestrel status                       Show system status
  kestrel config api_url http://...    Set configuration
        """,
    )

    subparsers = parser.add_subparsers(dest="command")

    # task
    task_p = subparsers.add_parser("task", help="Start an autonomous agent task")
    task_p.add_argument("goal", nargs="+", help="Task goal")

    # tasks
    tasks_p = subparsers.add_parser("tasks", help="List agent tasks")
    tasks_p.add_argument("--status", help="Filter by status")

    # workflows
    subparsers.add_parser("workflows", help="List workflow templates")

    # cron
    subparsers.add_parser("cron", help="List cron jobs")

    # webhooks
    subparsers.add_parser("webhooks", help="List webhook endpoints")

    # status
    subparsers.add_parser("status", help="Show system status")

    # config
    config_p = subparsers.add_parser("config", help="Configure settings")
    config_p.add_argument("key", nargs="?", help="Config key")
    config_p.add_argument("value", nargs="?", help="Config value")

    return parser


def main():
    """Main entry point."""
    parser = build_parser()
    args = parser.parse_args()

    config = load_config()
    client = KestrelClient(config)

    command_map = {
        "task": cmd_task,
        "tasks": cmd_tasks,
        "workflows": cmd_workflows,
        "cron": cmd_cron,
        "webhooks": cmd_webhooks,
        "status": cmd_status,
        "config": cmd_config,
    }

    if args.command and args.command in command_map:
        asyncio.run(command_map[args.command](client, args))
    else:
        # No subcommand — launch interactive REPL
        asyncio.run(interactive_repl(client, config))


if __name__ == "__main__":
    main()
