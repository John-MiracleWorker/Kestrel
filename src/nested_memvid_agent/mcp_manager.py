from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import threading
import time
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, Literal, cast

from .runtime_models import ToolCall, ToolExecution, ToolSpec
from .state_store import AgentStateStore
from .tools.base import AgentTool, ToolContext

MCP_DEFAULT_RISK_POLICY = "approval_by_default"
MCP_TRUST_MANIFEST_POLICY = "trust_manifest"
MCP_TIMEOUT_SECONDS = 15.0

RiskLevel = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class MCPServerConfig:
    id: str
    name: str
    transport: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    url: str | None = None
    enabled: bool = True
    tools: tuple[dict[str, Any], ...] = ()
    risk_policy: str = MCP_DEFAULT_RISK_POLICY


class MCPManager:
    """Discovers and invokes MCP tools while owning live server sessions."""

    def __init__(self, state: AgentStateStore, *, timeout_seconds: float = MCP_TIMEOUT_SECONDS) -> None:
        self.state = state
        self.timeout_seconds = timeout_seconds
        self._lock = threading.RLock()
        self._sessions: dict[str, _MCPSessionWorker] = {}

    def add_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        server = _normalize_server(payload)
        next_row = _server_to_dict(server)
        try:
            current_row = self.state.get_mcp_server(server.id)
        except KeyError:
            current_row = None
        if current_row is not None and _config_fingerprint(current_row) != _config_fingerprint(next_row):
            self._close_session(server.id)
        return self.state.upsert_mcp_server(next_row)

    def delete_server(self, server_id: str) -> None:
        self._close_session(server_id)
        self.state.delete_mcp_server(server_id)

    def shutdown(self) -> None:
        with self._lock:
            workers = list(self._sessions.values())
            self._sessions.clear()
        for worker in workers:
            worker.close(timeout=self.timeout_seconds)

    def list_servers(self) -> list[dict[str, Any]]:
        return self.state.list_mcp_servers()

    def connect_server(self, server_id: str) -> dict[str, Any]:
        row = self.state.get_mcp_server(server_id)
        server = _server_from_state(row)
        started = time.monotonic()
        try:
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
            tools = self._discover_tools(server, prefer_static=prefer_static)
            row["tools"] = tools
            row["status"] = "online"
            row["session_state"] = "static" if prefer_static else "connected"
            row["error"] = None
            row["last_seen_at"] = _now()
            row["last_synced_at"] = _now()
            row["tool_count"] = len(tools)
            row["capabilities"] = _capabilities_from_tools(tools)
            row["failure_count"] = 0
            row["last_latency_ms"] = _elapsed_ms(started)
            return {"ok": True, "message": f"Connected and discovered {len(tools)} tools.", "server": self.state.upsert_mcp_server(row)}
        except Exception as exc:  # noqa: BLE001 - returned to UI and state
            self._close_session(server_id)
            server_row = self._mark_error(row, exc, latency_ms=_elapsed_ms(started))
            return {"ok": False, "message": str(server_row["error"]), "server": server_row}

    def disconnect_server(self, server_id: str) -> dict[str, Any]:
        row = self.state.get_mcp_server(server_id)
        self._close_session(server_id)
        row["status"] = "configured"
        row["session_state"] = "disconnected"
        row["error"] = None
        return {"ok": True, "message": "Disconnected.", "server": self.state.upsert_mcp_server(row)}

    def restart_server(self, server_id: str) -> dict[str, Any]:
        self.state.get_mcp_server(server_id)
        self._close_session(server_id)
        return self.connect_server(server_id)

    def server_health(self, server_id: str) -> dict[str, Any]:
        row = self.state.get_mcp_server(server_id)
        server = _server_from_state(row)
        started = time.monotonic()
        try:
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
            tools = self._discover_tools(server, prefer_static=prefer_static)
            row["tools"] = tools
            row["status"] = "online"
            row["session_state"] = "static" if prefer_static else "connected"
            row["error"] = None
            row["last_seen_at"] = _now()
            row["tool_count"] = len(tools)
            row["capabilities"] = _capabilities_from_tools(tools)
            row["failure_count"] = 0
            row["last_latency_ms"] = _elapsed_ms(started)
            return {"ok": True, "message": "Live MCP session is healthy.", "server": self.state.upsert_mcp_server(row)}
        except Exception as exc:  # noqa: BLE001
            self._close_session(server_id)
            server_row = self._mark_error(row, exc, latency_ms=_elapsed_ms(started))
            return {"ok": False, "message": str(server_row["error"]), "server": server_row}

    def sync_server(self, server_id: str) -> dict[str, Any]:
        server = self.state.get_mcp_server(server_id)
        started = time.monotonic()
        try:
            prefer_static = _prefer_static_manifest(server)
            tools = self._discover_tools(_server_from_state(server), prefer_static=prefer_static)
            server["tools"] = tools
            server["status"] = "synced"
            server["session_state"] = "static" if prefer_static or not _has_live_endpoint(_server_from_state(server)) else "connected"
            server["error"] = None
            server["last_synced_at"] = _now()
            server["last_seen_at"] = _now()
            server["tool_count"] = len(tools)
            server["capabilities"] = _capabilities_from_tools(tools)
            server["failure_count"] = 0
            server["last_latency_ms"] = _elapsed_ms(started)
        except Exception as exc:  # noqa: BLE001 - stored for UI visibility
            self._close_session(server_id)
            return self._mark_error(server, exc, latency_ms=_elapsed_ms(started))
        return self.state.upsert_mcp_server(server)

    def test_server(self, server_id: str) -> dict[str, Any]:
        return self.server_health(server_id)

    def tool_adapters(self) -> list[AgentTool]:
        adapters: list[AgentTool] = []
        for server in self.state.list_mcp_servers():
            if not server["enabled"]:
                continue
            config = _server_from_state(server)
            for tool in server["tools"]:
                adapters.append(MCPToolAdapter(self, config, tool))
        return adapters

    def call_tool(self, server: MCPServerConfig, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        call = ToolCall(name=f"mcp.{server.id}.{tool_name}", arguments=arguments)
        started = time.monotonic()
        try:
            if not _has_live_endpoint(server):
                raise ValueError("MCP server has static tool metadata but no command or URL to invoke.")
            result = self._call_live_tool(server, tool_name, arguments)
            latency_ms = _elapsed_ms(started)
            return ToolExecution(
                call=call,
                success=True,
                content=result,
                data={"server_id": server.id, "latency_ms": latency_ms, "session_state": "connected"},
            )
        except Exception as exc:  # noqa: BLE001
            self._close_session(server.id)
            return ToolExecution(
                call=call,
                success=False,
                content=f"{type(exc).__name__}: {exc}",
                data={"server_id": server.id, "latency_ms": _elapsed_ms(started), "session_state": "error"},
                error="mcp_tool_failed",
            )

    def invoke_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        row = self.state.get_mcp_server(server_id)
        server = _server_from_state(row)
        remote_name = _remote_name_for(row, tool_name)
        execution = self.call_tool(server, remote_name, arguments)
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
            worker = _MCPSessionWorker(server=server, fingerprint=fingerprint)
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
        row["error"] = f"{type(exc).__name__}: {exc}"
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
            requires_approval=bool(tool.get("requires_approval", risk in {"medium", "high"})),
            source="mcp",
            server_id=server.id,
            capabilities=tuple(str(item) for item in tool.get("capabilities", ["mcp"])),
        )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del context
        return self.manager.invoke_tool(self.server.id, self.remote_tool_name, arguments)


class _MCPSessionWorker:
    def __init__(self, *, server: MCPServerConfig, fingerprint: str) -> None:
        self.server = server
        self.fingerprint = fingerprint
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


def _session_context(server: MCPServerConfig) -> Any:
    if server.transport == "stdio":
        stdio_mod = import_module("mcp.client.stdio")
        client_mod = import_module("mcp")
        params = client_mod.StdioServerParameters(
            command=server.command or "",
            args=list(server.args),
            env=server.env or None,
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
    transport = str(payload.get("transport", "stdio"))
    if transport == "http":
        transport = "streamable_http"
    return MCPServerConfig(
        id=str(payload["id"]),
        name=str(payload.get("name") or payload["id"]),
        transport=transport,
        command=None if payload.get("command") is None else str(payload.get("command")),
        args=tuple(str(item) for item in payload.get("args", [])),
        env={str(k): str(v) for k, v in dict(payload.get("env", {})).items()},
        url=None if payload.get("url") is None else str(payload.get("url")),
        enabled=bool(payload.get("enabled", True)),
        tools=tuple(dict(item) for item in payload.get("tools", [])),
        risk_policy=_normalize_risk_policy(payload.get("risk_policy", MCP_DEFAULT_RISK_POLICY)),
    )


def _server_to_dict(server: MCPServerConfig) -> dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "args": list(server.args),
        "env": server.env or {},
        "url": server.url,
        "enabled": server.enabled,
        "tools": list(server.tools),
        "status": "configured",
        "error": None,
        "risk_policy": server.risk_policy,
        "session_state": "disconnected",
        "failure_count": 0,
    }


def _server_from_state(row: dict[str, Any]) -> MCPServerConfig:
    return MCPServerConfig(
        id=str(row["id"]),
        name=str(row["name"]),
        transport=str(row["transport"]),
        command=row.get("command"),
        args=tuple(str(item) for item in row.get("args", [])),
        env={str(k): str(v) for k, v in dict(row.get("env", {})).items()},
        url=row.get("url"),
        enabled=bool(row.get("enabled", True)),
        tools=tuple(dict(item) for item in row.get("tools", [])),
        risk_policy=_normalize_risk_policy(row.get("risk_policy", MCP_DEFAULT_RISK_POLICY)),
    )


def _normalize_tool(server: MCPServerConfig, tool: dict[str, Any]) -> dict[str, Any]:
    remote_name = str(tool.get("remote_name") or tool.get("name"))
    risk, requires_approval = _risk_fields(server, tool)
    return {
        "name": f"mcp.{server.id}.{remote_name}",
        "remote_name": remote_name,
        "description": str(tool.get("description", "")),
        "parameters": dict(tool.get("parameters") or tool.get("inputSchema") or {"type": "object", "properties": {}}),
        "risk": risk,
        "requires_approval": requires_approval,
        "capabilities": list(tool.get("capabilities", ["mcp"])),
    }


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
    name = str(getattr(tool, "name", "tool"))
    risk, requires_approval = _risk_fields(server, {})
    return {
        "name": f"mcp.{server.id}.{name}",
        "remote_name": name,
        "description": str(getattr(tool, "description", "")),
        "parameters": dict(getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}),
        "risk": risk,
        "requires_approval": requires_approval,
        "capabilities": ["mcp"],
    }


def _risk_fields(server: MCPServerConfig, tool: dict[str, Any]) -> tuple[RiskLevel, bool]:
    if server.risk_policy == MCP_TRUST_MANIFEST_POLICY or bool(tool.get("trusted") or tool.get("allow_autonomous")):
        risk = _risk_level(tool.get("risk", "low"))
        return risk, bool(tool.get("requires_approval", risk in {"medium", "high"}))
    risk = _risk_level(tool.get("risk", "medium"))
    if risk == "low":
        risk = "medium"
    return risk, True


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
        "url": row.get("url"),
        "enabled": bool(row.get("enabled", True)),
        "risk_policy": _normalize_risk_policy(row.get("risk_policy", MCP_DEFAULT_RISK_POLICY)),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


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
