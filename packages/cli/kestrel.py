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

from kestrel_native import (
    ControlClientError,
    control_socket_available,
    ensure_home_layout,
    install_daemon_service,
    send_control_request,
    send_control_stream,
)

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
    "api_url": "http://localhost:8741",
    "api_key": "",
    "workspace_id": "",
    "control_mode": "auto",
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
    """Native-control client with HTTP compatibility fallback."""

    def __init__(self, config: dict):
        self.base_url = config["api_url"].rstrip("/")
        self.api_key = config.get("api_key", "")
        self.workspace_id = config.get("workspace_id", "")
        self.control_mode = config.get("control_mode", "auto")
        self.paths = ensure_home_layout()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _use_local_control(self) -> bool:
        if self.control_mode == "http":
            return False
        return control_socket_available(self.paths)

    async def _request_http(self, method: str, path: str, body: dict = None) -> dict:
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

    async def _stream_sse(self, path: str, body: dict):
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
        if self._use_local_control():
            try:
                start = await send_control_request(
                    "task.start",
                    {"goal": goal, "workspace_id": ws or "local"},
                    paths=self.paths,
                )
                task_id = ((start or {}).get("task") or {}).get("id")
                if not task_id:
                    raise ControlClientError("Native task start did not return a task id")
                async for envelope in send_control_stream(
                    "task.stream",
                    {"task_id": task_id},
                    paths=self.paths,
                    timeout_seconds=300,
                ):
                    event = envelope.get("event")
                    if event:
                        yield event
                return
            except ControlClientError:
                pass

        path = f"/api/workspaces/{ws}/tasks"
        async for event in self._stream_sse(path, {"goal": goal}):
            yield event

    async def list_tasks(self, status: str = None) -> dict:
        if self._use_local_control():
            try:
                result = await send_control_request("task.list", {"limit": 50}, paths=self.paths)
                tasks = result.get("tasks", [])
                if status:
                    tasks = [task for task in tasks if task.get("status") == status]
                return {"tasks": tasks}
            except ControlClientError:
                pass
        path = f"/api/workspaces/{self.workspace_id}/tasks"
        if status:
            path += f"?status={status}"
        return await self._request_http("GET", path)

    async def cancel_task(self, task_id: str) -> dict:
        return await self._request_http("POST", f"/api/tasks/{task_id}/cancel")

    async def approve_task(self, task_id: str, approval_id: str, approved: bool) -> dict:
        if self._use_local_control():
            try:
                return await send_control_request(
                    "approval",
                    {"action": "resolve", "approval_id": approval_id, "approved": approved},
                    paths=self.paths,
                )
            except ControlClientError:
                pass
        return await self._request_http("POST", f"/api/tasks/{task_id}/approve", {
            "approvalId": approval_id,
            "approved": approved,
        })

    async def list_workflows(self) -> dict:
        if self._use_local_control():
            return {"workflows": []}
        return await self._request_http("GET", "/api/workflows")

    async def list_cron_jobs(self) -> dict:
        if self._use_local_control():
            return {"jobs": []}
        return await self._request_http("GET", f"/api/workspaces/{self.workspace_id}/automation/cron")

    async def list_webhooks(self) -> dict:
        if self._use_local_control():
            return {"webhooks": []}
        return await self._request_http("GET", f"/api/workspaces/{self.workspace_id}/automation/webhooks")

    async def chat(self, prompt: str) -> dict:
        if self._use_local_control():
            try:
                return await send_control_request("chat", {"prompt": prompt}, paths=self.paths)
            except ControlClientError:
                pass
        return {"message": "", "error": "Local control API unavailable"}

    async def status(self) -> dict:
        if self._use_local_control():
            return await send_control_request("status", paths=self.paths)
        return {}

    async def doctor(self) -> dict:
        if self._use_local_control():
            return await send_control_request("doctor", paths=self.paths)
        return {"summary": {"healthy": False, "warnings": 1, "errors": 1}, "checks": []}

    async def runtime_profile(self) -> dict:
        if self._use_local_control():
            return await send_control_request("runtime.profile", paths=self.paths)
        return {}

    async def sync_memory(self) -> dict:
        if self._use_local_control():
            return await send_control_request("memory.sync", paths=self.paths)
        return {"indexed_files": 0, "namespaces": []}

    async def shutdown(self) -> dict:
        if self._use_local_control():
            return await send_control_request("shutdown", paths=self.paths)
        return {"status": "unavailable"}

    async def paired_nodes(self) -> dict:
        if self._use_local_control():
            return await send_control_request("paired_nodes.status", paths=self.paths)
        return {"nodes": []}


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

def load_channel_state(paths) -> dict:
    """Load shared Gateway channel state from the local Kestrel home."""
    state_path = paths.state_dir / "gateway-channels.json"
    if not state_path.exists():
        return {}
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


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
    try:
        status = await client.status()
    except Exception:
        status = {}

    if status:
        uptime = int(status.get("uptime_seconds", 0))
        days, rem = divmod(uptime, 86400)
        hrs, rem = divmod(rem, 3600)
        mins, _ = divmod(rem, 60)
        uptime_str = f"{days}d {hrs}h {mins}m" if days > 0 else f"{hrs}h {mins}m"
        print(c(f"  Native daemon running (uptime: {uptime_str})", Colors.SUCCESS))
        runtime = status.get("runtime_profile", {})
        local_models = runtime.get("local_models", {})
        default_provider = local_models.get("default_provider") or "none"
        default_model = local_models.get("default_model") or "none"
        print(f"  {c('Control:', Colors.MUTED)}    {c(status.get('control_socket', 'unknown'), Colors.PRIMARY)}")
        print(f"  {c('Runtime:', Colors.MUTED)}    {c(runtime.get('runtime_mode', 'native'), Colors.WHITE)}")
        print(f"  {c('Model:', Colors.MUTED)}      {c(f'{default_provider}:{default_model}', Colors.WHITE)}")
        print(f"  {c('Approvals:', Colors.MUTED)}  {c(str(len(status.get('pending_approvals', []))), Colors.WHITE)}")
        print(f"  {c('API:', Colors.MUTED)}        {c(config.get('api_url', 'not set'), Colors.PRIMARY)}")
        print(f"  {c('Workspace:', Colors.MUTED)}  {c(config.get('workspace_id', 'not set'), Colors.WHITE)}")
        print(f"  {c('Model pref:', Colors.MUTED)} {c(config.get('model', 'default'), Colors.WHITE)}")
        print(f"  {c('Thinking:', Colors.MUTED)}   {c(config.get('thinking_level', 'medium'), Colors.WHITE)}")
        print(f"  {c('Usage:', Colors.MUTED)}      {c(config.get('usage_mode', 'tokens'), Colors.WHITE)}")
        return
    
    state_file = os.path.expanduser("~/.kestrel/state/heartbeat.json")
    if os.path.exists(state_file):
        try:
            with open(state_file, "r") as f:
                state = json.load(f)
            ago = int(time.time() - state.get("last_heartbeat", 0))
            uptime = int(state.get("uptime", 0))
            days, rem = divmod(uptime, 86400)
            hrs, rem = divmod(rem, 3600)
            mins, seq = divmod(rem, 60)
            uptime_str = f"{days}d {hrs}h {mins}m" if days > 0 else f"{hrs}h {mins}m"
            print(c(f"  🦅 Kestrel Agent OS — Running (uptime: {uptime_str})", Colors.SUCCESS))
            print(c("  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", Colors.MUTED))
            print(f"  {c('HEARTBEAT', Colors.KESTREL)}     Last: {ago}s ago | State: {state.get('status')}")
            print()
        except Exception:
            print(c("  🦅 Kestrel Agent OS — Offline or state unreadable", Colors.ERROR))
            print()
    else:
        print(c("  🦅 Kestrel Agent OS — Offline (daemon not running)", Colors.MUTED))
        print()

    print(f"  {c('API:', Colors.MUTED)}        {c(config.get('api_url', 'not set'), Colors.PRIMARY)}")
    print(f"  {c('Workspace:', Colors.MUTED)}  {c(config.get('workspace_id', 'not set'), Colors.WHITE)}")
    print(f"  {c('Model:', Colors.MUTED)}      {c(config.get('model', 'default'), Colors.WHITE)}")
    print(f"  {c('Thinking:', Colors.MUTED)}   {c(config.get('thinking_level', 'medium'), Colors.WHITE)}")
    print(f"  {c('Usage:', Colors.MUTED)}      {c(config.get('usage_mode', 'tokens'), Colors.WHITE)}")


async def cmd_doctor(client: KestrelClient, args: argparse.Namespace):
    """Run local runtime diagnostics."""
    print_header("Kestrel Doctor")
    report = await client.doctor()
    summary = report.get("summary", {})
    health_color = Colors.SUCCESS if summary.get("healthy") else Colors.WARNING
    print(f"  {c('Healthy:', Colors.MUTED)}   {c(str(bool(summary.get('healthy'))), health_color)}")
    print(f"  {c('Warnings:', Colors.MUTED)}  {c(str(summary.get('warnings', 0)), Colors.WHITE)}")
    print(f"  {c('Errors:', Colors.MUTED)}    {c(str(summary.get('errors', 0)), Colors.WHITE)}")
    print()

    for check in report.get("checks", []):
        status = check.get("status", "unknown")
        color = Colors.SUCCESS if status == "ok" else Colors.WARNING if status == "warning" else Colors.ERROR
        print(f"  {c(status.upper().ljust(7), color)} {check.get('name', 'check')}: {check.get('detail', '')}")

    paths = report.get("paths", {})
    if paths:
        print()
        print(f"  {c('Home:', Colors.MUTED)}      {c(paths.get('home', ''), Colors.WHITE)}")
        print(f"  {c('Socket:', Colors.MUTED)}    {c(paths.get('control_socket', ''), Colors.WHITE)}")
        print(f"  {c('SQLite:', Colors.MUTED)}    {c(paths.get('sqlite_db', ''), Colors.WHITE)}")

    if getattr(args, "repair", False):
        print()
        print_header("Repair Actions")
        repaired = []
        ensure_home_layout()
        repaired.append("Ensured local Kestrel home layout exists")
        if client._use_local_control():
            memory_result = await client.sync_memory()
            repaired.append(f"Synced markdown memory ({memory_result.get('indexed_files', 0)} files)")
        else:
            repaired.append("Daemon unavailable; skipped live memory sync")
        for item in repaired:
            print_success(item)


async def cmd_onboard(client: KestrelClient, args: argparse.Namespace):
    """Prepare the local Kestrel home and summarize the Telegram-first setup."""
    print_header("Kestrel Onboard")
    paths = ensure_home_layout()
    print_success(f"Prepared local home at {paths.home}")

    state = load_channel_state(paths)
    telegram = state.get("telegram") or {}
    telegram_config = telegram.get("config") or {}
    if telegram_config.get("token"):
        workspace_id = telegram_config.get("workspaceId", "default")
        mode = telegram_config.get("mode", "polling")
        print_info(f"Telegram bot configured for workspace {workspace_id} ({mode})")
    else:
        print_warning("Telegram bot is not configured yet. Use the desktop settings or Gateway integration route.")

    if client._use_local_control():
        doctor = await client.doctor()
        summary = doctor.get("summary", {})
        print_info(
            f"Doctor summary: healthy={summary.get('healthy')} "
            f"warnings={summary.get('warnings', 0)} errors={summary.get('errors', 0)}"
        )
    else:
        print_warning("Local daemon is not connected. Run `kestrel install` to enable background startup.")


async def cmd_channels(client: KestrelClient, args: argparse.Namespace):
    """Show configured companion channels from the shared local store."""
    print_header("Channels")
    state = load_channel_state(client.paths)
    telegram = state.get("telegram") or {}
    config = telegram.get("config") or {}
    session = telegram.get("state") or {}

    if not config:
        print_info("No companion channels configured")
        return

    print(f"  {c('Telegram:', Colors.MUTED)}  {c('configured', Colors.SUCCESS)}")
    print(f"  {c('Workspace:', Colors.MUTED)} {c(config.get('workspaceId', 'default'), Colors.WHITE)}")
    print(f"  {c('Mode:', Colors.MUTED)}      {c(config.get('mode', 'polling'), Colors.WHITE)}")
    mappings = session.get("mappings", [])
    print(f"  {c('Pairings:', Colors.MUTED)}  {c(str(len(mappings)), Colors.WHITE)}")
    if mappings:
        latest = mappings[-1]
        print(
            f"  {c('Latest:', Colors.MUTED)}    "
            f"{c(str(latest.get('chatId')), Colors.WHITE)} -> {c(str(latest.get('userId')), Colors.WHITE)}"
        )


async def cmd_monitor(client: KestrelClient, args: argparse.Namespace):
    """Show a local Telegram-first operator snapshot."""
    print_header("Flight Deck")
    status = await client.status()
    runtime = status.get("runtime_profile", {})
    channels = load_channel_state(client.paths)
    telegram = ((channels.get("telegram") or {}).get("config") or {})

    print(f"  {c('Runtime:', Colors.MUTED)}   {c(runtime.get('runtime_mode', 'unknown'), Colors.WHITE)}")
    print(f"  {c('Model:', Colors.MUTED)}     {c(runtime.get('local_models', {}).get('default_model', 'none'), Colors.WHITE)}")
    print(f"  {c('Approvals:', Colors.MUTED)} {c(str(len(status.get('pending_approvals', []))), Colors.WHITE)}")
    print(f"  {c('Tasks:', Colors.MUTED)}     {c(str(len(status.get('recent_tasks', []))), Colors.WHITE)}")
    print(
        f"  {c('Telegram:', Colors.MUTED)}  "
        f"{c('configured' if telegram.get('token') else 'not configured', Colors.WHITE)}"
    )

    recent_tasks = status.get("recent_tasks", [])
    if recent_tasks:
        print()
        headers = ["ID", "Goal", "Status"]
        rows = [
            [task.get("id", "")[:8], task.get("goal", "")[:42], task.get("status", "")]
            for task in recent_tasks[:5]
        ]
        print_table(headers, rows, [10, 44, 14])


async def cmd_runtime(client: KestrelClient, args: argparse.Namespace):
    """Show native runtime profile."""
    print_header("Runtime Profile")
    profile = await client.runtime_profile()
    if not profile:
        print_error("Native runtime profile unavailable")
        return

    print(f"  {c('Mode:', Colors.MUTED)}      {c(profile.get('runtime_mode', 'unknown'), Colors.WHITE)}")
    print(f"  {c('Policy:', Colors.MUTED)}    {c(profile.get('policy_name', 'unknown'), Colors.WHITE)}")
    print(f"  {c('Updated:', Colors.MUTED)}   {c(profile.get('updated_at', 'unknown'), Colors.WHITE)}")

    local_models = profile.get("local_models", {})
    print(f"  {c('Provider:', Colors.MUTED)}  {c(local_models.get('default_provider', 'none'), Colors.WHITE)}")
    print(f"  {c('Model:', Colors.MUTED)}     {c(local_models.get('default_model', 'none'), Colors.WHITE)}")

    capabilities = profile.get("runtime_capabilities", {})
    if capabilities:
        print()
        for name, value in capabilities.items():
            print(f"  {c(name + ':', Colors.MUTED):<34}{c(str(value), Colors.WHITE)}")


async def cmd_paired_nodes(client: KestrelClient, args: argparse.Namespace):
    """Show registered paired nodes."""
    print_header("Paired Nodes")
    payload = await client.paired_nodes()
    nodes = payload.get("nodes", [])
    if not nodes:
        print_info("No paired nodes registered")
        return

    rows = [
        [
            node.get("node_id", ""),
            node.get("node_type", ""),
            node.get("platform", ""),
            node.get("health", ""),
            ",".join((node.get("capabilities", []) or [])[:3]),
        ]
        for node in nodes
    ]
    print_table(["Node", "Type", "Platform", "Health", "Capabilities"], rows)


async def cmd_shutdown(client: KestrelClient, args: argparse.Namespace):
    """Stop the local daemon."""
    print_header("Stopping Kestrel Daemon")
    result = await client.shutdown()
    status = result.get("status", "unknown")
    if status == "stopping":
        print_success("Daemon shutdown requested.")
    else:
        print_error(f"Shutdown failed: {status}")


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


async def cmd_install(client: KestrelClient, args: argparse.Namespace):
    """Install Kestrel as a persistent background daemon (macOS, Linux, Windows)."""
    print_header("Installing Kestrel Daemon")
    cli_dir = os.path.abspath(os.path.dirname(__file__))
    daemon_path = os.path.join(cli_dir, "kestrel_daemon.py")

    if not os.path.exists(daemon_path):
        print_error(f"Daemon script not found at {daemon_path}")
        return
    try:
        paths = ensure_home_layout()
        result = install_daemon_service(
            daemon_path=daemon_path,
            python_executable=sys.executable,
            paths=paths,
        )
        print_success(f"Daemon installed via {result['manager']}.")
        print_info(f"Service file: {result['service_path']}")
        print_info(f"State directory: {paths.home}")
    except Exception as exc:
        print_error(str(exc))


# ── Memory CLI Commands ──────────────────────────────────────────────

def cmd_memory_show(args, config: dict):
    """Show contents of Kestrel Dual Memory markdown files."""
    import glob
    import os
    memory_base = os.path.expanduser(config.get("memory_dir", "~/.kestrel/memory"))
    
    # Check if a category was provided (e.g. "preferences")
    category = args.category.lower() if hasattr(args, "category") and args.category else None
    
    # We look inside the first workspace folder we find, or default
    ws_dirs = [d for d in glob.glob(os.path.join(memory_base, "*")) if os.path.isdir(d)]
    if not ws_dirs:
        print_info("No memory synchronized yet. The daemon will sync memory shortly.")
        return
        
    ws_dir = ws_dirs[0]  # Just use the first one for CLI
    print_info(f"Showing memory for workspace: {os.path.basename(ws_dir)}\n")
    
    if category:
        filename = f"{category}.md" if category.endswith('s') else f"{category}s.md"
        filepaths = [os.path.join(ws_dir, filename)]
        if not os.path.exists(filepaths[0]):
            filepaths = [os.path.join(ws_dir, f"{category}.md")] # Fallback to singular
            if not os.path.exists(filepaths[0]):
                 print_error(f"No memory found for category: {category}")
                 return
    else:
        filepaths = glob.glob(os.path.join(ws_dir, "*.md"))
        
    for fp in filepaths:
        if not os.path.exists(fp):
            continue
        print(c(f"--- {os.path.basename(fp)} ---", Colors.KESTREL))
        try:
            with open(fp, "r") as f:
                print(f.read())
        except Exception as e:
            print_error(f"Could not read {fp}: {e}")
        print()


def cmd_memory_edit(args, config: dict):
    """Open Kestrel memory directory in the default editor."""
    import subprocess
    import platform
    memory_base = os.path.expanduser(config.get("memory_dir", "~/.kestrel/memory"))

    print_info(f"Opening memory directory: {memory_base}")
    if not os.path.exists(memory_base):
        os.makedirs(memory_base, exist_ok=True)
        print_info("Created new memory directory.")

    editor = os.environ.get("EDITOR", "")
    plat = platform.system()

    def open_folder_native():
        """Open the folder in the OS file manager."""
        try:
            if plat == "Darwin":
                subprocess.run(["open", memory_base])
            elif plat == "Windows":
                os.startfile(memory_base)  # type: ignore[attr-defined]
            else:
                subprocess.run(["xdg-open", memory_base])
        except Exception as e:
            print_error(f"Could not open folder: {e}")
            print_info(f"Memory files are at: {memory_base}")

    terminal_editors = ("nano", "vim", "vi", "emacs", "pico", "micro")
    if editor and editor not in terminal_editors:
        try:
            subprocess.run([editor, memory_base])
            return
        except Exception as e:
            print_error(f"Failed to launch editor ({editor}): {e}")

    # Fall back to native folder opener for terminal editors or when EDITOR is unset
    open_folder_native()


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
            if client._use_local_control():
                response = await client.chat(user_input)
                if response.get("error"):
                    print_error(response["error"])
                else:
                    print(c(response.get("message", ""), Colors.WHITE))
                    provider = response.get("provider") or "unknown"
                    model = response.get("model") or "unknown"
                    print(c(f"\n  {provider}:{model}\n", Colors.MUTED))
                continue
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

    # doctor
    doctor_p = subparsers.add_parser("doctor", help="Run local daemon diagnostics")
    doctor_p.add_argument("--repair", action="store_true", help="Apply safe local repair steps")

    # onboard
    subparsers.add_parser("onboard", help="Prepare the local Telegram-first Kestrel home")

    # channels
    subparsers.add_parser("channels", help="Show configured companion channels")

    # monitor
    subparsers.add_parser("monitor", help="Show a local Flight Deck snapshot")

    # runtime
    subparsers.add_parser("runtime", help="Show native runtime profile")

    # paired-nodes
    subparsers.add_parser("paired-nodes", help="Show registered paired nodes")

    # shutdown
    subparsers.add_parser("shutdown", help="Stop the local daemon")

    # install
    subparsers.add_parser("install", help="Install Kestrel as a background macOS daemon")

    # config
    config_p = subparsers.add_parser("config", help="Configure settings")
    config_p.add_argument("key", nargs="?", help="Config key")
    config_p.add_argument("value", nargs="?", help="Config value")

    # memory
    memory_parser = subparsers.add_parser("memory", help="Manage Kestrel transparent knowledge memory")
    memory_subparsers = memory_parser.add_subparsers(dest="memory_cmd", help="Memory subcommand")
    
    mem_show_parser = memory_subparsers.add_parser("show", help="Show memory contents")
    mem_show_parser.add_argument("category", nargs="?", help="Specific memory category to show (e.g. preferences)")
    
    mem_edit_parser = memory_subparsers.add_parser("edit", help="Open memory directory in default editor")

    memory_subparsers.add_parser("sync", help="Sync markdown memory into the native index")

    return parser


def main():
    """Main entry point."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

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
        "doctor": cmd_doctor,
        "onboard": cmd_onboard,
        "channels": cmd_channels,
        "monitor": cmd_monitor,
        "runtime": cmd_runtime,
        "paired-nodes": cmd_paired_nodes,
        "shutdown": cmd_shutdown,
        "config": cmd_config,
        "install": cmd_install,
    }

    if args.command == "memory":
        if args.memory_cmd == "show":
            cmd_memory_show(args, config)
        elif args.memory_cmd == "edit":
            cmd_memory_edit(args, config)
        elif args.memory_cmd == "sync":
            result = asyncio.run(client.sync_memory())
            print_success(f"Indexed {result.get('indexed_files', 0)} markdown files.")
            namespaces = result.get("namespaces", [])
            if namespaces:
                print_info(f"Namespaces: {', '.join(namespaces)}")
        else:
            parser.parse_args(["memory", "--help"])
        return

    if args.command and args.command in command_map:
        asyncio.run(command_map[args.command](client, args))
    else:
        # No subcommand — launch interactive REPL
        asyncio.run(interactive_repl(client, config))


if __name__ == "__main__":
    main()
