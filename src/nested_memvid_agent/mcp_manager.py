from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import re
import socket
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Literal, cast
from urllib.parse import urlparse

from .runtime_models import ToolCall, ToolExecution, ToolSpec
from .secret_broker import is_secret_ref
from .security_boundary import (
    redact_secrets,
    redact_text,
    register_secret_env_names,
    register_secret_value,
)
from .state_store import AgentStateStore
from .tools.base import AgentTool, ToolContext

MCP_DEFAULT_RISK_POLICY = "approval_by_default"
MCP_TRUST_MANIFEST_POLICY = "trust_manifest"
MCP_TIMEOUT_SECONDS = 15.0

RiskLevel = Literal["low", "medium", "high"]
SecretResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class MCPServerConfig:
    id: str
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    secret_env: dict[str, str] | None = None
    url: str | None = None
    enabled: bool = True
    tools: tuple[dict[str, Any], ...] = ()
    risk_policy: str = MCP_DEFAULT_RISK_POLICY
    vetting: dict[str, Any] | None = None


class MCPManager:
    """Discovers and invokes MCP tools while owning live server sessions."""

    def __init__(
        self,
        state: AgentStateStore,
        *,
        timeout_seconds: float = MCP_TIMEOUT_SECONDS,
        allow_network_endpoints: bool = False,
        secret_resolver: SecretResolver | None = None,
    ) -> None:
        self.state = state
        self.timeout_seconds = timeout_seconds
        self.allow_network_endpoints = allow_network_endpoints
        self.secret_resolver = secret_resolver
        self.capability_policy: Any | None = None
        self._lock = threading.RLock()
        self._sessions: dict[str, _MCPSessionWorker] = {}

    def add_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw = dict(payload)
        server_id = str(raw["id"])
        try:
            current_row = self.state.get_mcp_server(server_id)
        except KeyError:
            current_row = None
        if current_row is not None:
            # PUT/PATCH callers only receive redacted public configuration. An
            # omitted field therefore means preserve, never erase a secret
            # binding, argument list, or discovered tool manifest.
            for key in (
                "name",
                "transport",
                "command",
                "args",
                "env",
                "secret_env",
                "url",
                "enabled",
                "tools",
                "risk_policy",
                "vetting",
            ):
                if key not in raw:
                    raw[key] = current_row.get(key)
        server = _normalize_server(raw)
        _validate_server_endpoint(server, allow_network_endpoints=self.allow_network_endpoints)
        next_row = _server_to_dict(server)
        if current_row is not None:
            if _config_fingerprint(current_row) != _config_fingerprint(next_row):
                self._close_session(server.id)
            else:
                _preserve_matching_connect_approval(next_row, current_row)
                for key in (
                    "status",
                    "error",
                    "last_synced_at",
                    "last_seen_at",
                    "session_state",
                    "last_call_at",
                    "last_error_at",
                    "failure_count",
                    "last_latency_ms",
                ):
                    next_row[key] = current_row.get(key)
        return self.state.upsert_mcp_server(next_row)

    def set_enabled(self, server_id: str, enabled: bool) -> dict[str, Any]:
        """Persist a server switch and immediately revoke its live session."""

        row = self.state.get_mcp_server(server_id)
        if not enabled:
            self._close_session(server_id)
            row["status"] = "configured"
            row["session_state"] = "disconnected"
            row["error"] = None
        row["enabled"] = bool(enabled)
        return self.state.upsert_mcp_server(row)

    def delete_server(self, server_id: str) -> None:
        self._close_session(server_id)
        self.state.delete_mcp_server(server_id)

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._sessions.values())
            self._sessions.clear()
        for worker in workers:
            worker.close(timeout=self.timeout_seconds)

    def close_disabled_sessions(self) -> list[str]:
        """Quiesce sessions whose server disappeared or is no longer effective."""

        rows = {str(row["id"]): row for row in self.state.list_mcp_servers()}
        with self._lock:
            session_ids = list(self._sessions)
        closed: list[str] = []
        for server_id in session_ids:
            row = rows.get(server_id)
            if row is not None and self._server_enabled(row):
                continue
            self._close_session(server_id)
            closed.append(server_id)
        return closed

    def list_servers(self) -> list[dict[str, Any]]:
        return self.state.list_mcp_servers()

    def connect_server(self, server_id: str) -> dict[str, Any]:
        row = self.state.get_mcp_server(server_id)
        disabled = self._disabled_result(row)
        if disabled is not None:
            return disabled
        server = _server_from_state(row)
        started = time.monotonic()
        try:
            _validate_server_endpoint(server, allow_network_endpoints=self.allow_network_endpoints)
            if not _has_live_endpoint(server):
                if server.tools:
                    row["status"] = "online"
                    row["session_state"] = "static"
                    row["error"] = None
                    row["last_seen_at"] = _now()
                    row["last_latency_ms"] = _elapsed_ms(started)
                    return {"ok": True, "message": "Static MCP tool manifest is available.", "server": self.state.upsert_mcp_server(row)}
                raise ValueError("MCP server has no command or URL to connect.")
            prefer_static = _prefer_static_manifest(row)
            _validate_stdio_command_hash(row)
            approval_result = self._connect_approval_result(row, latency_ms=_elapsed_ms(started))
            if approval_result is not None:
                return approval_result
            tools = self._discover_tools(server, prefer_static=prefer_static)
            row["tools"] = tools
            row["status"] = "online"
            row["session_state"] = "static" if prefer_static else "connected"
            row["error"] = None
            row["last_seen_at"] = _now()
            row["last_synced_at"] = _now()
            row["tool_count"] = len(tools)
            row["capabilities"] = _capabilities_from_tools(tools)
            row["vetting"] = _vetting_for_server(_server_from_state(row), tools)
            row["failure_count"] = 0
            row["last_latency_ms"] = _elapsed_ms(started)
            return {"ok": True, "message": f"Connected and discovered {len(tools)} tools.", "server": self.state.upsert_mcp_server(row)}
        except Exception as exc:  # noqa: BLE001 - returned to UI and state
            self._close_session(server_id)
            server_row = self._mark_error(row, exc, latency_ms=_elapsed_ms(started))
            return {"ok": False, "message": str(server_row["error"]), "server": server_row}

    def approve_server_connect(self, server_id: str) -> dict[str, Any]:
        row = self.state.get_mcp_server(server_id)
        if not self._server_enabled(row):
            raise ValueError("MCP server is disabled.")
        server = _server_from_state(row)
        _validate_server_endpoint(
            server,
            allow_network_endpoints=self.allow_network_endpoints,
        )
        _validate_stdio_command_hash(row)
        vetting = dict(row.get("vetting", {}) or {})
        expected_hash = vetting.get("stdio_command_hash")
        if server.transport == "stdio" and server.command:
            expected_hash = _stdio_command_hash(server.command, server.args)
            vetting["stdio_command_hash"] = expected_hash
            vetting["connect_requires_approval"] = True
        vetting["connect_approved"] = True
        vetting["connect_approved_at"] = _now()
        if isinstance(expected_hash, str) and expected_hash:
            vetting["connect_approved_command_hash"] = expected_hash
        row["vetting"] = vetting
        if row.get("status") == "approval_required":
            row["status"] = "configured"
            row["session_state"] = "disconnected"
            row["error"] = None
        return self.state.upsert_mcp_server(row)

    def disconnect_server(self, server_id: str) -> dict[str, Any]:
        row = self.state.get_mcp_server(server_id)
        self._close_session(server_id)
        row["status"] = "configured"
        row["session_state"] = "disconnected"
        row["error"] = None
        return {"ok": True, "message": "Disconnected.", "server": self.state.upsert_mcp_server(row)}

    def restart_server(self, server_id: str) -> dict[str, Any]:
        row = self.state.get_mcp_server(server_id)
        disabled = self._disabled_result(row)
        if disabled is not None:
            return disabled
        self._close_session(server_id)
        return self.connect_server(server_id)

    def server_health(self, server_id: str) -> dict[str, Any]:
        row = self.state.get_mcp_server(server_id)
        disabled = self._disabled_result(row)
        if disabled is not None:
            return disabled
        server = _server_from_state(row)
        started = time.monotonic()
        try:
            _validate_server_endpoint(server, allow_network_endpoints=self.allow_network_endpoints)
            if not _has_live_endpoint(server):
                if server.tools:
                    row["status"] = "online"
                    row["session_state"] = "static"
                    row["error"] = None
                    row["last_seen_at"] = _now()
                    row["last_latency_ms"] = _elapsed_ms(started)
                    return {"ok": True, "message": "Static manifest is healthy.", "server": self.state.upsert_mcp_server(row)}
                raise ValueError("MCP server has no command or URL to check.")
            prefer_static = _prefer_static_manifest(row)
            _validate_stdio_command_hash(row)
            approval_result = self._connect_approval_result(row, latency_ms=_elapsed_ms(started))
            if approval_result is not None:
                return approval_result
            tools = self._discover_tools(server, prefer_static=prefer_static)
            row["tools"] = tools
            row["status"] = "online"
            row["session_state"] = "static" if prefer_static else "connected"
            row["error"] = None
            row["last_seen_at"] = _now()
            row["tool_count"] = len(tools)
            row["capabilities"] = _capabilities_from_tools(tools)
            row["vetting"] = _vetting_for_server(_server_from_state(row), tools)
            row["failure_count"] = 0
            row["last_latency_ms"] = _elapsed_ms(started)
            return {"ok": True, "message": "Live MCP session is healthy.", "server": self.state.upsert_mcp_server(row)}
        except Exception as exc:  # noqa: BLE001
            self._close_session(server_id)
            server_row = self._mark_error(row, exc, latency_ms=_elapsed_ms(started))
            return {"ok": False, "message": str(server_row["error"]), "server": server_row}

    def sync_server(self, server_id: str) -> dict[str, Any]:
        server = self.state.get_mcp_server(server_id)
        if not self._server_enabled(server):
            self._close_session(server_id)
            server["status"] = "configured"
            server["session_state"] = "disconnected"
            server["error"] = None
            return self.state.upsert_mcp_server(server)
        started = time.monotonic()
        try:
            _validate_server_endpoint(_server_from_state(server), allow_network_endpoints=self.allow_network_endpoints)
            prefer_static = _prefer_static_manifest(server)
            _validate_stdio_command_hash(server)
            approval_result = self._connect_approval_result(server, latency_ms=_elapsed_ms(started))
            if approval_result is not None:
                return dict(approval_result["server"])
            tools = self._discover_tools(_server_from_state(server), prefer_static=prefer_static)
            server["tools"] = tools
            server["status"] = "synced"
            server["session_state"] = "static" if prefer_static or not _has_live_endpoint(_server_from_state(server)) else "connected"
            server["error"] = None
            server["last_synced_at"] = _now()
            server["last_seen_at"] = _now()
            server["tool_count"] = len(tools)
            server["capabilities"] = _capabilities_from_tools(tools)
            server["vetting"] = _vetting_for_server(_server_from_state(server), tools)
            server["failure_count"] = 0
            server["last_latency_ms"] = _elapsed_ms(started)
        except Exception as exc:  # noqa: BLE001 - stored for UI visibility
            self._close_session(server_id)
            return self._mark_error(server, exc, latency_ms=_elapsed_ms(started))
        return self.state.upsert_mcp_server(server)

    def test_server(self, server_id: str) -> dict[str, Any]:
        return self.server_health(server_id)

    def tool_adapters(self, *, include_disabled: bool = False) -> list[AgentTool]:
        adapters: list[AgentTool] = []
        for server in self.state.list_mcp_servers():
            if not include_disabled and not self._server_enabled(server):
                continue
            config = _server_from_state(server)
            for tool in server["tools"]:
                adapter = MCPToolAdapter(self, config, _normalize_tool(config, tool))
                if include_disabled or self.capability_policy is None or self.capability_policy.tool_decision(adapter.spec).effective_enabled:
                    adapters.append(adapter)
        return adapters

    def call_tool(
        self, server: MCPServerConfig, tool_name: str, arguments: dict[str, Any]
    ) -> ToolExecution:
        """Invoke through the persisted-server approval boundary.

        Keeping this compatibility entry point routed through ``invoke_tool``
        prevents callers with a hand-built ``MCPServerConfig`` from starting a
        process that has not received exact connect approval.
        """

        return self.invoke_tool(server.id, tool_name, arguments)

    def _call_tool_after_connect_approval(
        self,
        server: MCPServerConfig,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> ToolExecution:
        call = ToolCall(name=f"mcp.{server.id}.{tool_name}", arguments=arguments)
        started = time.monotonic()
        try:
            _validate_server_endpoint(server, allow_network_endpoints=self.allow_network_endpoints)
            if not _has_live_endpoint(server):
                raise ValueError("MCP server has static tool metadata but no command or URL to invoke.")
            result = self._call_live_tool(server, tool_name, arguments)
            latency_ms = _elapsed_ms(started)
            return ToolExecution(
                call=call,
                success=True,
                content=redact_text(result),
                data={"server_id": server.id, "latency_ms": latency_ms, "session_state": "connected"},
            )
        except Exception as exc:  # noqa: BLE001
            self._close_session(server.id)
            safe_error = _safe_exception_text(exc)
            return ToolExecution(
                call=call,
                success=False,
                content=safe_error,
                data={"server_id": server.id, "latency_ms": _elapsed_ms(started), "session_state": "error"},
                error="mcp_tool_failed",
            )

    def invoke_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        row = self.state.get_mcp_server(server_id)
        server = _server_from_state(row)
        remote_name = _remote_name_for(row, tool_name)
        if not self._server_enabled(row):
            self._close_session(server_id)
            return ToolExecution(
                call=ToolCall(name=f"mcp.{server.id}.{remote_name}", arguments=arguments),
                success=False,
                content="MCP server is disabled.",
                data={"server_id": server.id, "session_state": "disconnected"},
                error="mcp_server_disabled",
            )
        if self.capability_policy is not None:
            normalized = next(
                (
                    _normalize_tool(server, tool)
                    for tool in row.get("tools", [])
                    if str(tool.get("remote_name") or tool.get("name")) == remote_name
                ),
                None,
            )
            if normalized is None:
                return ToolExecution(
                    call=ToolCall(name=f"mcp.{server.id}.{remote_name}", arguments=arguments),
                    success=False,
                    content=f"Unknown MCP tool: {remote_name}",
                    data={"server_id": server.id},
                    error="unknown_tool",
                )
            spec = MCPToolAdapter(self, server, normalized).spec
            decision = self.capability_policy.tool_decision(spec)
            if not decision.effective_enabled:
                return ToolExecution(
                    call=ToolCall(name=spec.name, arguments=arguments),
                    success=False,
                    content=f"MCP tool is disabled by {', '.join(decision.blocked_by)}.",
                    data={"server_id": server.id, "session_state": str(row.get("session_state", "disconnected"))},
                    error="tool_disabled",
                )
        try:
            _validate_server_endpoint(server, allow_network_endpoints=self.allow_network_endpoints)
            _validate_stdio_command_hash(row)
            approval_result = self._connect_approval_result(row, latency_ms=0)
            if approval_result is not None:
                return ToolExecution(
                    call=ToolCall(name=f"mcp.{server.id}.{remote_name}", arguments=arguments),
                    success=False,
                    content="MCP connect approval required.",
                    data={"server_id": server.id, "session_state": "approval_required"},
                    error="mcp_connect_approval_required",
                )
        except Exception as exc:  # noqa: BLE001
            self._close_session(server_id)
            safe_error = _safe_exception_text(exc)
            row["last_error_at"] = _now()
            row["status"] = "error"
            row["session_state"] = "error"
            row["error"] = safe_error
            row["failure_count"] = int(row.get("failure_count", 0)) + 1
            self.state.upsert_mcp_server(row)
            return ToolExecution(
                call=ToolCall(name=f"mcp.{server.id}.{remote_name}", arguments=arguments),
                success=False,
                content=str(row["error"]),
                data={"server_id": server.id, "session_state": "error"},
                error="mcp_tool_failed",
            )
        execution = self._call_tool_after_connect_approval(server, remote_name, arguments)
        row["last_call_at"] = _now()
        row["last_latency_ms"] = execution.data.get("latency_ms")
        if execution.success:
            row["last_seen_at"] = _now()
            row["status"] = "online"
            row["session_state"] = "connected"
            row["error"] = None
            row["failure_count"] = 0
        else:
            row["last_error_at"] = _now()
            row["status"] = "error"
            row["session_state"] = "error"
            row["error"] = execution.content
            row["failure_count"] = int(row.get("failure_count", 0)) + 1
        self.state.upsert_mcp_server(row)
        return execution

    def _connect_approval_result(self, row: dict[str, Any], *, latency_ms: int) -> dict[str, Any] | None:
        if not _connect_requires_approval(row):
            return None
        row["status"] = "approval_required"
        row["session_state"] = "approval_required"
        row["error"] = "MCP connect approval required."
        row["last_latency_ms"] = latency_ms
        return {"ok": False, "message": "MCP connect approval required.", "server": self.state.upsert_mcp_server(row)}

    def _disabled_result(self, row: dict[str, Any]) -> dict[str, Any] | None:
        if self._server_enabled(row):
            return None
        self._close_session(str(row["id"]))
        row["status"] = "configured"
        row["session_state"] = "disconnected"
        row["error"] = None
        return {
            "ok": False,
            "message": "MCP server is disabled.",
            "server": self.state.upsert_mcp_server(row),
        }

    def _server_enabled(self, row: dict[str, Any]) -> bool:
        if self.capability_policy is None:
            return bool(row.get("enabled", False))
        decision = self.capability_policy.parent_decision(
            "mcp_server",
            str(row["id"]),
            entity_enabled=bool(row.get("enabled", False)),
        )
        return bool(decision.effective_enabled)

    def _discover_tools(self, server: MCPServerConfig, *, prefer_static: bool = False) -> list[dict[str, Any]]:
        if prefer_static or not _has_live_endpoint(server):
            return [_normalize_tool(server, tool) for tool in server.tools]
        worker = self._worker_for(server)
        tools = worker.list_tools(timeout=self.timeout_seconds)
        return [_normalize_sdk_tool(server, tool) for tool in tools]

    def _call_live_tool(self, server: MCPServerConfig, tool_name: str, arguments: dict[str, Any]) -> str:
        worker = self._worker_for(server)
        return worker.call_tool(tool_name, arguments, timeout=self.timeout_seconds)

    def _worker_for(self, server: MCPServerConfig) -> _MCPSessionWorker:
        fingerprint = _config_fingerprint(_server_to_dict(server))
        with self._lock:
            worker = self._sessions.get(server.id)
            if worker is not None and worker.fingerprint == fingerprint and worker.is_open:
                return worker
            if worker is not None:
                worker.close(timeout=self.timeout_seconds)
            worker = _MCPSessionWorker(server=server, fingerprint=fingerprint, secret_resolver=self.secret_resolver)
            self._sessions[server.id] = worker
            return worker

    def _close_session(self, server_id: str) -> None:
        with self._lock:
            worker = self._sessions.pop(server_id, None)
        if worker is not None:
            worker.close(timeout=self.timeout_seconds)

    def _mark_error(self, row: dict[str, Any], exc: Exception, *, latency_ms: int) -> dict[str, Any]:
        row["status"] = "error"
        row["session_state"] = "error"
        row["error"] = _safe_exception_text(exc)
        row["last_error_at"] = _now()
        row["failure_count"] = int(row.get("failure_count", 0)) + 1
        row["last_latency_ms"] = latency_ms
        return self.state.upsert_mcp_server(row)


class MCPToolAdapter(AgentTool):
    def __init__(self, manager: MCPManager, server: MCPServerConfig, tool: dict[str, Any]) -> None:
        self.manager = manager
        self.server = server
        self.remote_tool_name = str(tool["remote_name"])
        risk = _risk_level(tool.get("risk", "medium"))
        self.spec = ToolSpec(
            name=str(tool["name"]),
            description=str(tool.get("description") or f"MCP tool {self.remote_tool_name} from {server.name}"),
            parameters=dict(tool.get("parameters") or {"type": "object", "properties": {}}),
            risk=risk,
            requires_approval=(
                risk == "high"
                or bool(tool.get("requires_approval", risk in {"medium", "high"}))
            ),
            source="mcp",
            server_id=server.id,
            capabilities=tuple(str(item) for item in tool.get("capabilities", ["mcp"])),
            produces_validation=bool(tool.get("produces_validation", False)),
        )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del context
        return self.manager.invoke_tool(self.server.id, self.remote_tool_name, arguments)


class _MCPSessionWorker:
    def __init__(self, *, server: MCPServerConfig, fingerprint: str, secret_resolver: SecretResolver | None = None) -> None:
        self.server = server
        self.fingerprint = fingerprint
        self.secret_resolver = secret_resolver
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session_context: Any | None = None
        self._session: Any | None = None

    @property
    def is_open(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def list_tools(self, *, timeout: float) -> list[Any]:
        return cast(list[Any], self._submit(self._list_tools(), timeout=timeout))

    def call_tool(self, tool_name: str, arguments: dict[str, Any], *, timeout: float) -> str:
        return str(self._submit(self._call_tool(tool_name, arguments), timeout=timeout))

    def close(self, *, timeout: float) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            if loop is None or thread is None:
                self._loop = None
                self._thread = None
                return
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), loop)
            try:
                future.result(timeout=timeout)
            except Exception:
                future.cancel()
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=timeout)
        with self._lock:
            self._loop = None
            self._thread = None
            self._session = None
            self._session_context = None
            self._ready.clear()

    def _submit(self, awaitable: Coroutine[Any, Any, Any], *, timeout: float) -> Any:
        loop = self._ensure_loop(timeout=timeout)
        future: concurrent.futures.Future[Any] = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(f"MCP operation timed out after {timeout:.1f}s.") from exc

    def _ensure_loop(self, *, timeout: float) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            self._ready.clear()
            self._thread = threading.Thread(target=self._run_loop, name=f"mcp-session-{self.server.id}", daemon=True)
            self._thread.start()
        if not self._ready.wait(timeout=timeout):
            raise TimeoutError(f"MCP event loop did not start within {timeout:.1f}s.")
        with self._lock:
            if self._loop is None:
                raise RuntimeError("MCP event loop failed to initialize.")
            return self._loop

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lock:
            self._loop = loop
            self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _connect(self) -> Any:
        if self._session is not None:
            return self._session
        try:
            self._session_context = _session_context(self.server, secret_resolver=self.secret_resolver)
        except TypeError as exc:
            if "secret_resolver" not in str(exc):
                raise
            self._session_context = _session_context(self.server)
        self._session = await self._session_context.__aenter__()
        return self._session

    async def _disconnect(self) -> None:
        try:
            if self._session_context is not None:
                await self._session_context.__aexit__(None, None, None)
        finally:
            self._session = None
            self._session_context = None

    async def _list_tools(self) -> list[Any]:
        session = await self._connect()
        result = await session.list_tools()
        return list(getattr(result, "tools", result))

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        session = await self._connect()
        result = await session.call_tool(tool_name, arguments)
        return _tool_result_to_text(result)


def _session_context(server: MCPServerConfig, *, secret_resolver: SecretResolver | None = None) -> Any:
    if server.transport == "stdio":
        stdio_mod = import_module("mcp.client.stdio")
        client_mod = import_module("mcp")
        params = client_mod.StdioServerParameters(
            command=server.command or "",
            args=list(server.args),
            env=_runtime_env(server, secret_resolver=secret_resolver) or None,
        )
        return _ClientSessionContext(stdio_mod.stdio_client(params))
    if server.transport == "streamable_http":
        http_mod = import_module("mcp.client.streamable_http")
        return _ClientSessionContext(http_mod.streamablehttp_client(server.url or ""))
    if server.transport == "sse":
        sse_mod = import_module("mcp.client.sse")
        return _ClientSessionContext(sse_mod.sse_client(server.url or ""))
    raise ValueError(f"Unsupported MCP transport: {server.transport}")


class _ClientSessionContext:
    def __init__(self, stream_context: Any) -> None:
        self.stream_context = stream_context
        self.stream_cm: Any | None = None
        self.session: Any | None = None

    async def __aenter__(self) -> Any:
        client_mod = import_module("mcp")
        self.stream_cm = await self.stream_context.__aenter__()
        read_stream, write_stream = self.stream_cm[0], self.stream_cm[1]
        self.session = client_mod.ClientSession(read_stream, write_stream)
        session = await self.session.__aenter__()
        await session.initialize()
        return session

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.session is not None:
            await self.session.__aexit__(exc_type, exc, tb)
        await self.stream_context.__aexit__(exc_type, exc, tb)


def _normalize_server(payload: dict[str, Any]) -> MCPServerConfig:
    server_id = _safe_mcp_identifier(str(payload["id"]), field="server id")
    transport = str(payload.get("transport", "stdio"))
    if transport == "http":
        transport = "streamable_http"
    env = {str(k): str(v) for k, v in dict(payload.get("env", {})).items()}
    secret_env = {str(k): str(v) for k, v in dict(payload.get("secret_env", {})).items()}
    for key, value in env.items():
        if _looks_secret(key) or _value_looks_secret(value):
            raise ValueError(f"MCP secret-looking environment variable {key} must be configured via secret_env.")
    for target, source in secret_env.items():
        if not _valid_env_name(target) or not (_valid_env_name(source) or is_secret_ref(source)):
            raise ValueError("MCP secret_env keys must be env names and values must be env names or secret:// refs.")
    args = tuple(str(item) for item in payload.get("args", []))
    _validate_stdio_args(args)
    url = None if payload.get("url") is None else str(payload.get("url"))
    if url is not None:
        _validate_url_has_no_credential_components(url)
    sanitized_vetting = _sanitize_mcp_metadata(dict(payload.get("vetting", {}) or {}))
    vetting = sanitized_vetting if isinstance(sanitized_vetting, dict) else {}
    for approval_field in (
        "connect_approved",
        "connect_approved_at",
        "connect_approved_command_hash",
    ):
        vetting.pop(approval_field, None)
    return MCPServerConfig(
        id=server_id,
        name=redact_text(str(payload.get("name") or server_id)),
        transport=transport,
        command=None if payload.get("command") is None else str(payload.get("command")),
        args=args,
        env=env,
        secret_env=secret_env,
        url=url,
        enabled=bool(payload.get("enabled", True)),
        tools=tuple(dict(item) for item in payload.get("tools", [])),
        risk_policy=_normalize_risk_policy(payload.get("risk_policy", MCP_DEFAULT_RISK_POLICY)),
        vetting=vetting,
    )


def _server_to_dict(server: MCPServerConfig) -> dict[str, Any]:
    vetted_tools = [_normalize_tool(server, tool) for tool in server.tools]
    vetting = {**dict(server.vetting or {}), **_vetting_for_server(server, vetted_tools)}
    return {
        "id": server.id,
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "args": list(server.args),
        "env": server.env or {},
        "secret_env": server.secret_env or {},
        "url": server.url,
        "enabled": server.enabled,
        "tools": vetted_tools,
        "status": "configured",
        "error": None,
        "risk_policy": server.risk_policy,
        "session_state": "disconnected",
        "failure_count": 0,
        "capabilities": _capabilities_from_tools(vetted_tools),
        "vetting": vetting,
    }


def _server_from_state(row: dict[str, Any]) -> MCPServerConfig:
    return MCPServerConfig(
        id=str(row["id"]),
        name=str(row["name"]),
        transport=str(row["transport"]),
        command=row.get("command"),
        args=tuple(str(item) for item in row.get("args", [])),
        env={str(k): str(v) for k, v in dict(row.get("env", {})).items()},
        secret_env={str(k): str(v) for k, v in dict(row.get("secret_env", {})).items()},
        url=row.get("url"),
        enabled=bool(row.get("enabled", True)),
        tools=tuple(dict(item) for item in row.get("tools", [])),
        risk_policy=_normalize_risk_policy(row.get("risk_policy", MCP_DEFAULT_RISK_POLICY)),
        vetting=dict(row.get("vetting", {}) or {}),
    )


def _normalize_tool(server: MCPServerConfig, tool: dict[str, Any]) -> dict[str, Any]:
    remote_name = _safe_mcp_identifier(
        str(tool.get("remote_name") or tool.get("name")),
        field="tool name",
    )
    risk, requires_approval = _risk_fields(server, tool)
    parameters = _sanitize_mcp_metadata(
        tool.get("parameters")
        or tool.get("inputSchema")
        or {"type": "object", "properties": {}}
    )
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return {
        "name": f"mcp.{server.id}.{remote_name}",
        "remote_name": remote_name,
        "description": redact_text(str(tool.get("description", ""))),
        "parameters": parameters,
        "risk": risk,
        "requires_approval": requires_approval,
        "capabilities": [
            redact_text(str(capability))
            for capability in tool.get("capabilities", ["mcp"])
        ],
        "produces_validation": bool(tool.get("produces_validation", False)),
    }


def _safe_mcp_identifier(value: str, *, field: str) -> str:
    safe_value = redact_text(value)
    if safe_value != value:
        raise ValueError(f"MCP {field} contains credential material.")
    return value


def _sanitize_mcp_metadata(value: Any) -> Any:
    """Redact both values and attacker-controlled JSON keys from MCP metadata."""

    return _sanitize_mcp_metadata_keys(redact_secrets(value))


def _sanitize_mcp_metadata_keys(value: Any) -> Any:
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = redact_text(str(raw_key))
            if key in safe:
                raise ValueError("MCP metadata keys collide after credential redaction.")
            safe[key] = _sanitize_mcp_metadata_keys(item)
        return safe
    if isinstance(value, (list, tuple)):
        return [_sanitize_mcp_metadata_keys(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _safe_exception_text(exc: Exception) -> str:
    return redact_text(f"{type(exc).__name__}: {exc}")


def _remote_name_for(row: dict[str, Any], tool_name: str) -> str:
    short_name = tool_name.removeprefix(f"mcp.{row['id']}.")
    for tool in row.get("tools", []):
        if tool.get("name") == tool_name or tool.get("name") == f"mcp.{row['id']}.{tool_name}":
            return str(tool.get("remote_name") or short_name)
        if tool.get("remote_name") == tool_name:
            return str(tool["remote_name"])
    return short_name


def _capabilities_from_tools(tools: list[dict[str, Any]]) -> list[str]:
    return sorted({str(capability) for tool in tools for capability in tool.get("capabilities", [])})


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _elapsed_ms(started: float) -> int:
    return max(0, int((time.monotonic() - started) * 1000))


def _normalize_sdk_tool(server: MCPServerConfig, tool: Any) -> dict[str, Any]:
    name = _safe_mcp_identifier(str(getattr(tool, "name", "tool")), field="tool name")
    description = redact_text(str(getattr(tool, "description", "")))
    risk, _ = _risk_fields(
        server,
        {"name": name, "remote_name": name, "description": description},
    )
    if risk == "low":
        risk = "medium"
    parameters = _sanitize_mcp_metadata(
        getattr(tool, "inputSchema", None)
        or {"type": "object", "properties": {}}
    )
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    return {
        "name": f"mcp.{server.id}.{name}",
        "remote_name": name,
        "description": description,
        "parameters": parameters,
        "risk": risk,
        "requires_approval": True,
        "capabilities": ["mcp"],
        "produces_validation": False,
    }


def _risk_fields(server: MCPServerConfig, tool: dict[str, Any]) -> tuple[RiskLevel, bool]:
    inferred = _infer_tool_risk(tool)
    if server.risk_policy == MCP_TRUST_MANIFEST_POLICY:
        risk = _max_risk(_risk_level(tool.get("risk", "low")), inferred)
        manifest_requires_approval = bool(
            tool.get("requires_approval", risk in {"medium", "high"})
        )
        return risk, risk == "high" or manifest_requires_approval
    risk = _max_risk(_risk_level(tool.get("risk", "medium")), inferred)
    if risk == "low":
        risk = "medium"
    return risk, True


def _infer_tool_risk(tool: dict[str, Any]) -> RiskLevel:
    terms = _risk_terms(
        str(tool.get("remote_name") or tool.get("name") or ""),
        str(tool.get("description") or ""),
    )
    high_markers = {
        "add",
        "write",
        "create",
        "delete",
        "deploy",
        "destroy",
        "disable",
        "enable",
        "execute",
        "grant",
        "insert",
        "install",
        "invoke",
        "modify",
        "mutate",
        "remove",
        "replace",
        "revoke",
        "rotate",
        "set",
        "start",
        "stop",
        "restart",
        "uninstall",
        "update",
        "upload",
        "publish",
        "post",
        "patch",
        "apply",
        "broadcast",
        "charge",
        "commit",
        "push",
        "shell",
        "exec",
        "command",
        "filesystem",
        "file",
        "secret",
        "token",
        "credential",
        "notify",
        "pay",
        "purchase",
        "send",
        "submit",
        "transfer",
    }
    if terms & high_markers:
        return "high"
    network_markers = {"http", "https", "request", "fetch", "email", "message"}
    if terms & network_markers:
        return "medium"
    return "low"


def _risk_terms(*values: str) -> set[str]:
    expanded = " ".join(re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value) for value in values)
    return {term.lower() for term in re.findall(r"[A-Za-z0-9]+", expanded)}


def _max_risk(left: RiskLevel, right: RiskLevel) -> RiskLevel:
    order = {"low": 0, "medium": 1, "high": 2}
    return left if order[left] >= order[right] else right


def _vetting_for_server(server: MCPServerConfig, tools: list[dict[str, Any]]) -> dict[str, Any]:
    vetting = dict(server.vetting or {})
    secret_refs = dict(server.secret_env or {})
    secrets = sorted(set(secret_refs) | {key for key in (server.env or {}) if _looks_secret(key)})
    network_access = server.transport in {"sse", "streamable_http"} or bool(server.url)
    risk_reasons: list[str] = []
    if network_access:
        risk_reasons.append("network")
    if secrets:
        risk_reasons.append("secrets")
    if server.transport not in {"stdio", "sse", "streamable_http"}:
        risk_reasons.append("unknown_transport")
    if any(str(tool.get("risk")) == "high" for tool in tools):
        risk_reasons.append("high_risk_tools")
    if server.risk_policy != MCP_TRUST_MANIFEST_POLICY:
        risk_reasons.append("approval_by_default")
    if server.transport == "stdio" and server.command:
        command_hash = _stdio_command_hash(server.command, server.args)
        previous_hash = vetting.get("stdio_command_hash")
        vetting["stdio_command_hash"] = command_hash
        vetting["connect_requires_approval"] = True
        if previous_hash != command_hash:
            for key in (
                "connect_approved",
                "connect_approved_at",
                "connect_approved_command_hash",
            ):
                vetting.pop(key, None)
        risk_reasons.append("stdio_process")
    recommended_trust = "approval_required" if risk_reasons else "low_risk"
    return {
        **vetting,
        "server_id": server.id,
        "transport": server.transport,
        "network_access": network_access,
        "secrets_required": secrets,
        "secret_env": {
            target: {"source_env": source, "configured": bool(os.getenv(source, "").strip())}
            for target, source in sorted(secret_refs.items())
        },
        "risk_policy": server.risk_policy,
        "recommended_trust": recommended_trust,
        "risk_reasons": sorted(set(risk_reasons)),
        "tools": [
            {
                "name": str(tool.get("remote_name") or tool.get("name")),
                "registered_name": str(tool.get("name")),
                "risk": str(tool.get("risk", "medium")),
                "requires_approval": bool(tool.get("requires_approval", True)),
                "capabilities": list(tool.get("capabilities", [])),
            }
            for tool in tools
        ],
    }


def _looks_secret(name: str) -> bool:
    upper = name.upper()
    return any(
        marker in upper
        for marker in ("AUTH", "TOKEN", "API_KEY", "KEY", "SECRET", "PASSWORD", "CREDENTIAL")
    )


def _value_looks_secret(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("bearer ", "basic ", "secret://", "sk-", "ghp_", "github_pat_"))


def _validate_stdio_args(args: tuple[str, ...]) -> None:
    previous_secret_flag = False
    for argument in args:
        lowered = argument.strip().lower()
        if previous_secret_flag:
            raise ValueError("MCP secrets cannot be embedded in stdio arguments; use secret_env.")
        secret_flag = _looks_secret(lowered.replace("-", "_"))
        if secret_flag and "=" in lowered:
            raise ValueError("MCP secrets cannot be embedded in stdio arguments; use secret_env.")
        previous_secret_flag = secret_flag and lowered.startswith("-")
        if _value_looks_secret(argument):
            raise ValueError("MCP secrets cannot be embedded in stdio arguments; use secret_env.")


def _validate_url_has_no_credential_components(url: str) -> None:
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("MCP endpoint URLs cannot contain credentials, queries, or fragments.")


def _valid_env_name(name: str) -> bool:
    if not name:
        return False
    first = name[0]
    return (first == "_" or first.isalpha()) and all(char == "_" or char.isalnum() for char in name)


def _risk_level(value: object) -> RiskLevel:
    normalized = str(value).strip().lower()
    if normalized == "high":
        return "high"
    if normalized == "low":
        return "low"
    return "medium"


def _normalize_risk_policy(value: object) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"trusted", "trust_manifest", "manifest"}:
        return MCP_TRUST_MANIFEST_POLICY
    return MCP_DEFAULT_RISK_POLICY


def _has_live_endpoint(server: MCPServerConfig) -> bool:
    if server.transport == "stdio":
        return bool(server.command)
    if server.transport in {"streamable_http", "sse"}:
        return bool(server.url)
    return False


def _prefer_static_manifest(row: dict[str, Any]) -> bool:
    return bool(row.get("tools")) and not row.get("last_synced_at")


def _config_fingerprint(row: dict[str, Any]) -> str:
    payload = {
        "transport": row.get("transport"),
        "command": row.get("command"),
        "args": list(row.get("args", [])),
        "env": dict(row.get("env", {})),
        "secret_env": dict(row.get("secret_env", {})),
        "url": row.get("url"),
        "enabled": bool(row.get("enabled", True)),
        "risk_policy": _normalize_risk_policy(row.get("risk_policy", MCP_DEFAULT_RISK_POLICY)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _runtime_env(server: MCPServerConfig, *, secret_resolver: SecretResolver | None = None) -> dict[str, str]:
    env = dict(server.env or {})
    for target, source in dict(server.secret_env or {}).items():
        if is_secret_ref(source) and callable(secret_resolver):
            value = str(secret_resolver(source) or "")
        else:
            register_secret_env_names({source})
            value = os.getenv(source, "")
        if not value:
            raise ValueError(f"Missing MCP secret environment variable: {source}")
        register_secret_value(value)
        env[target] = value
    return env


def _validate_server_endpoint(server: MCPServerConfig, *, allow_network_endpoints: bool) -> None:
    if server.transport not in {"stdio", "sse", "streamable_http"}:
        raise ValueError(f"Unsupported MCP transport: {server.transport}")
    if server.transport == "stdio":
        _validate_stdio_command(server)
        return
    if not allow_network_endpoints:
        raise ValueError("MCP network endpoints are disabled. Enable allow_mcp_network_endpoints first.")
    _validate_network_url(server.url or "")


def _validate_stdio_command(server: MCPServerConfig) -> None:
    if not server.command:
        return
    command = server.command.strip()
    raw_command_name = re.split(r"[/\\]", command)[-1].lower()
    command_name = raw_command_name.removesuffix(".exe")
    if raw_command_name.endswith((".cmd", ".bat")):
        command_name = raw_command_name.rsplit(".", 1)[0]
        if command_name not in {"npx", "bunx"}:
            raise ValueError("MCP stdio batch launchers are not allowed.")
    if not command or _has_stdio_control_characters(command):
        raise ValueError("MCP stdio commands must be a single executable path.")
    proxy_launchers = {
        "sh",
        "bash",
        "zsh",
        "fish",
        "csh",
        "tcsh",
        "ksh",
        "dash",
        "pwsh",
        "powershell",
        "cmd",
        "env",
        "xargs",
        "sudo",
        "doas",
        "nohup",
        "setsid",
        "open",
        "osascript",
        "wscript",
        "cscript",
        "mshta",
        "rundll32",
        "regsvr32",
    }
    if command_name in proxy_launchers:
        raise ValueError("MCP stdio shell and proxy launchers are not allowed.")
    if any(_has_stdio_control_characters(argument) for argument in server.args):
        raise ValueError("MCP stdio arguments cannot contain control characters.")

    if re.fullmatch(r"(?:python|pythonw|pypy|py|pyw)(?:\d+(?:\.\d+)*)?", command_name):
        if not server.args:
            raise ValueError("MCP stdio Python commands must use `python -m <module>` or a `.py` script.")
        if server.args[0] == "-m":
            if len(server.args) < 2 or not _valid_python_module(server.args[1]):
                raise ValueError("MCP stdio Python module names are invalid.")
        elif server.args[0].startswith("-") or not server.args[0].lower().endswith(".py"):
            raise ValueError("MCP stdio Python commands must use `python -m <module>` or a `.py` script.")
        return

    if command_name in {"node", "nodejs"}:
        if not server.args or server.args[0].startswith("-") or not server.args[0].lower().endswith((".js", ".cjs", ".mjs")):
            raise ValueError("MCP stdio Node commands must name a JavaScript module file; eval flags are forbidden.")
        return

    if command_name in {"npx", "uvx", "bunx"}:
        if not server.args or not _valid_package_name(server.args[0]):
            raise ValueError(f"MCP stdio {command_name} commands must name a valid package.")
        return

    if command_name == "deno":
        if len(server.args) < 2 or server.args[0] != "run":
            raise ValueError("MCP stdio Deno commands must use `deno run`; eval mode is forbidden.")
        return

    if re.fullmatch(r"(?:ruby|perl|php|lua)(?:\d+(?:\.\d+)*)?", command_name):
        raise ValueError("General-purpose MCP stdio interpreter launchers are not allowed.")


def _validate_stdio_command_hash(row: dict[str, Any]) -> None:
    vetting = row.get("vetting")
    if not isinstance(vetting, dict):
        return
    expected = vetting.get("stdio_command_hash")
    if not isinstance(expected, str) or not expected:
        return
    actual = _stdio_command_hash(row.get("command"), row.get("args", []))
    if actual != expected:
        raise ValueError("MCP stdio command hash mismatch; refusing to connect.")


def _connect_requires_approval(row: dict[str, Any]) -> bool:
    vetting = row.get("vetting")
    metadata = vetting if isinstance(vetting, dict) else {}
    live_stdio = str(row.get("transport")) == "stdio" and bool(row.get("command"))
    if not live_stdio and not bool(metadata.get("connect_requires_approval")):
        return False
    if not bool(metadata.get("connect_approved")):
        return True
    expected = metadata.get("stdio_command_hash")
    if live_stdio:
        actual = _stdio_command_hash(row.get("command"), row.get("args", []))
        if not isinstance(expected, str) or expected != actual:
            return True
        return metadata.get("connect_approved_command_hash") != actual
    if not isinstance(expected, str) or not expected:
        return False
    return metadata.get("connect_approved_command_hash") != expected


def _stdio_command_hash(command: object, args: object) -> str:
    arg_list = [str(item) for item in args] if isinstance(args, list) else [str(item) for item in args] if isinstance(args, tuple) else []
    payload = json.dumps({"command": "" if command is None else str(command), "args": arg_list}, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _preserve_matching_connect_approval(
    next_row: dict[str, Any],
    current_row: dict[str, Any],
) -> None:
    next_vetting = dict(next_row.get("vetting", {}) or {})
    current_vetting = dict(current_row.get("vetting", {}) or {})
    command_hash = next_vetting.get("stdio_command_hash")
    if (
        not isinstance(command_hash, str)
        or current_vetting.get("stdio_command_hash") != command_hash
        or current_vetting.get("connect_approved_command_hash") != command_hash
        or not bool(current_vetting.get("connect_approved"))
    ):
        return
    for key in (
        "connect_approved",
        "connect_approved_at",
        "connect_approved_command_hash",
    ):
        if key in current_vetting:
            next_vetting[key] = current_vetting[key]
    next_row["vetting"] = next_vetting


def _valid_python_module(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", value))


def _valid_package_name(value: str) -> bool:
    return bool(re.fullmatch(r"(@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", value))


def _has_stdio_control_characters(value: str) -> bool:
    return any(character in value for character in ("\x00", "\r", "\n"))


def _validate_network_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("MCP network endpoints must use https URLs.")
    if parsed.username or parsed.password:
        raise ValueError("MCP endpoint URLs cannot include credentials.")
    if parsed.query or parsed.fragment:
        raise ValueError("MCP endpoint URLs cannot include queries or fragments.")
    host = parsed.hostname or ""
    if not host:
        raise ValueError("MCP endpoint URL must include a host.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(sockaddr[0])
                for _family, _type, _proto, _canonname, sockaddr in socket.getaddrinfo(
                    host,
                    parsed.port or 443,
                    type=socket.SOCK_STREAM,
                )
            }
        except (socket.gaierror, ValueError) as exc:
            raise ValueError("MCP endpoint hostname could not be resolved safely.") from exc
    else:
        addresses = {address}
    if any(
        address.is_link_local
        or address.is_loopback
        or address.is_private
        or address.is_reserved
        or address.is_multicast
        for address in addresses
    ):
        raise ValueError("MCP endpoint URL host is not allowed by default.")


def _tool_result_to_text(result: Any) -> str:
    content = getattr(result, "content", None)
    if isinstance(content, list):
        parts = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if content is not None:
        return str(content)
    return json.dumps(result, default=str)
