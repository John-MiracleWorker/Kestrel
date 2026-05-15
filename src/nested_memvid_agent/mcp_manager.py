from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from typing import Any, cast

from .runtime_models import ToolCall, ToolExecution, ToolSpec
from .state_store import AgentStateStore
from .tools.base import AgentTool, ToolContext


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


class MCPManager:
    """Discovers and invokes MCP tools while exposing them as local AgentTool adapters."""

    def __init__(self, state: AgentStateStore) -> None:
        self.state = state

    def add_server(self, payload: dict[str, Any]) -> dict[str, Any]:
        server = _normalize_server(payload)
        return self.state.upsert_mcp_server(_server_to_dict(server))

    def delete_server(self, server_id: str) -> None:
        self.state.delete_mcp_server(server_id)

    def list_servers(self) -> list[dict[str, Any]]:
        return self.state.list_mcp_servers()

    def sync_server(self, server_id: str) -> dict[str, Any]:
        server = self.state.get_mcp_server(server_id)
        try:
            tools = self._discover_tools(_server_from_state(server))
            server["tools"] = tools
            server["status"] = "synced"
            server["error"] = None
            server["last_synced_at"] = _now()
            server["last_seen_at"] = _now()
            server["tool_count"] = len(tools)
            server["capabilities"] = _capabilities_from_tools(tools)
        except Exception as exc:  # noqa: BLE001 - stored for UI visibility
            server["status"] = "error"
            server["error"] = f"{type(exc).__name__}: {exc}"
        return self.state.upsert_mcp_server(server)

    def test_server(self, server_id: str) -> dict[str, Any]:
        server = self.state.get_mcp_server(server_id)
        if server.get("tools"):
            server["status"] = "online"
            server["error"] = None
            server["last_seen_at"] = _now()
            return {"ok": True, "message": "Static MCP tool manifest is available.", "server": self.state.upsert_mcp_server(server)}
        try:
            tools = self._discover_tools(_server_from_state(server))
            server["tools"] = tools
            server["status"] = "online"
            server["error"] = None
            server["last_seen_at"] = _now()
            server["last_synced_at"] = _now()
            server["tool_count"] = len(tools)
            server["capabilities"] = _capabilities_from_tools(tools)
            return {"ok": True, "message": f"Connected and discovered {len(tools)} tools.", "server": self.state.upsert_mcp_server(server)}
        except Exception as exc:  # noqa: BLE001
            server["status"] = "error"
            server["error"] = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "message": server["error"], "server": self.state.upsert_mcp_server(server)}

    def tool_adapters(self) -> list[AgentTool]:
        adapters: list[AgentTool] = []
        for server in self.state.list_mcp_servers():
            if not server["enabled"]:
                continue
            for tool in server["tools"]:
                adapters.append(MCPToolAdapter(_server_from_state(server), tool))
        return adapters

    def _discover_tools(self, server: MCPServerConfig) -> list[dict[str, Any]]:
        if server.tools:
            return [_normalize_tool(server, tool) for tool in server.tools]
        return cast(list[dict[str, Any]], _run_async(_discover_tools_with_sdk(server)))

    def call_tool(self, server: MCPServerConfig, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        call = ToolCall(name=f"mcp.{server.id}.{tool_name}", arguments=arguments)
        try:
            result = str(_run_async(_call_tool_with_sdk(server, tool_name, arguments)))
            return ToolExecution(call=call, success=True, content=result, data={"server_id": server.id})
        except Exception as exc:  # noqa: BLE001
            return ToolExecution(call=call, success=False, content=str(exc), error="mcp_tool_failed")

    def invoke_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> ToolExecution:
        row = self.state.get_mcp_server(server_id)
        server = _server_from_state(row)
        remote_name = _remote_name_for(row, tool_name)
        execution = self.call_tool(server, remote_name, arguments)
        row["last_seen_at"] = _now()
        row["status"] = "online" if execution.success else "error"
        row["error"] = None if execution.success else execution.content
        self.state.upsert_mcp_server(row)
        return execution


class MCPToolAdapter(AgentTool):
    def __init__(self, server: MCPServerConfig, tool: dict[str, Any]) -> None:
        self.server = server
        self.remote_tool_name = str(tool["remote_name"])
        risk = str(tool.get("risk", "low"))
        self.spec = ToolSpec(
            name=str(tool["name"]),
            description=str(tool.get("description") or f"MCP tool {self.remote_tool_name} from {server.name}"),
            parameters=dict(tool.get("parameters") or {"type": "object", "properties": {}}),
            risk="high" if risk == "high" else "medium" if risk == "medium" else "low",
            requires_approval=bool(tool.get("requires_approval", risk in {"medium", "high"})),
            source="mcp",
            server_id=server.id,
            capabilities=tuple(str(item) for item in tool.get("capabilities", ["mcp"])),
        )

    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        del context
        return MCPManager(_NullStateStore()).call_tool(self.server, self.remote_tool_name, arguments)


async def _discover_tools_with_sdk(server: MCPServerConfig) -> list[dict[str, Any]]:
    session_cm = _session_context(server)
    async with session_cm as session:
        result = await session.list_tools()
        tools = getattr(result, "tools", result)
        return [_normalize_sdk_tool(server, tool) for tool in tools]


async def _call_tool_with_sdk(server: MCPServerConfig, tool_name: str, arguments: dict[str, Any]) -> str:
    session_cm = _session_context(server)
    async with session_cm as session:
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
        name=str(payload.get("name", payload["id"])),
        transport=transport,
        command=None if payload.get("command") is None else str(payload.get("command")),
        args=tuple(str(item) for item in payload.get("args", [])),
        env={str(k): str(v) for k, v in dict(payload.get("env", {})).items()},
        url=None if payload.get("url") is None else str(payload.get("url")),
        enabled=bool(payload.get("enabled", True)),
        tools=tuple(dict(item) for item in payload.get("tools", [])),
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
    )


def _normalize_tool(server: MCPServerConfig, tool: dict[str, Any]) -> dict[str, Any]:
    remote_name = str(tool.get("remote_name") or tool.get("name"))
    return {
        "name": f"mcp.{server.id}.{remote_name}",
        "remote_name": remote_name,
        "description": str(tool.get("description", "")),
        "parameters": dict(tool.get("parameters") or tool.get("inputSchema") or {"type": "object", "properties": {}}),
        "risk": str(tool.get("risk", "low")),
        "requires_approval": bool(tool.get("requires_approval", False)),
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


def _normalize_sdk_tool(server: MCPServerConfig, tool: Any) -> dict[str, Any]:
    name = str(getattr(tool, "name", "tool"))
    return {
        "name": f"mcp.{server.id}.{name}",
        "remote_name": name,
        "description": str(getattr(tool, "description", "")),
        "parameters": dict(getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}),
        "risk": "low",
        "requires_approval": False,
        "capabilities": ["mcp"],
    }


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


def _run_async(awaitable: Awaitable[Any]) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(cast(Any, awaitable))

    result: dict[str, Any] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(cast(Any, awaitable))
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


class _NullStateStore(AgentStateStore):
    def __init__(self) -> None:
        pass
