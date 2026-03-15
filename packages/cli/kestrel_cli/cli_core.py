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

