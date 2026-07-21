from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import threading
import time
import weakref
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, NoReturn, cast
from urllib.parse import urlparse
from urllib.request import url2pathname

from .platform_primitives import is_link_or_reparse_point, is_windows_reparse_point
from .private_directory import (
    PrivateDirectoryError,
    create_owner_private_directory,
    create_owner_private_temporary_directory,
    harden_empty_owner_private_directory,
    validate_owner_private_directory,
)
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
MCP_SNAPSHOT_MAX_FILES = 10_000
MCP_SNAPSHOT_MAX_BYTES = 256 * 1024 * 1024
MCP_RAW_FILE_SECRET_BACKENDS = frozenset({"", "json", "file", "local"})

RiskLevel = Literal["low", "medium", "high"]
SecretResolver = Callable[[str], str | None]

_MCP_PROCESS_TRANSITION_LOCK = threading.RLock()
_MCP_MANAGER_REGISTRY: weakref.WeakSet[Any] = weakref.WeakSet()


class MCPLaunchIdentityError(ValueError):
    """Raised when a stdio launch target cannot be identified immutably."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class MCPToolOutcomeIndeterminate(TimeoutError):
    """A remote side-effecting call timed out after it may have committed."""


class MCPRemoteToolError(RuntimeError):
    """The MCP server completed the call with the protocol error bit set."""


@dataclass(frozen=True)
class MCPLaunchIdentity:
    """The exact executable and entry artifact authorized for stdio launch."""

    executable_path: str
    executable_resolved_path: str
    executable_sha256: str
    artifact_kind: str
    artifact_locator: str
    artifact_sha256: str
    artifact_tree_sha256: str | None
    launch_args: tuple[str, ...]

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.to_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "executable_path": self.executable_path,
            "executable_resolved_path": self.executable_resolved_path,
            "executable_sha256": self.executable_sha256,
            "artifact_kind": self.artifact_kind,
            "artifact_locator": self.artifact_locator,
            "artifact_sha256": self.artifact_sha256,
            "artifact_tree_sha256": self.artifact_tree_sha256,
            "launch_args": list(self.launch_args),
        }


@dataclass(frozen=True)
class MCPVerifiedLaunchPlan:
    executable_path: str
    launch_args: tuple[str, ...]
    cwd: str
    source_identity_digest: str
    snapshot_digest: str


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
        workspace: Path | None = None,
        secret_store_path: Path | None = None,
        secret_backend: str = "json",
    ) -> None:
        self.state = state
        self.timeout_seconds = timeout_seconds
        self.allow_network_endpoints = allow_network_endpoints
        self.secret_resolver = secret_resolver
        self.workspace = None if workspace is None else workspace.expanduser().resolve()
        self.secret_store_path = (
            None if secret_store_path is None else secret_store_path.expanduser().resolve()
        )
        self.secret_backend = secret_backend.strip().lower()
        self.capability_policy: Any | None = None
        self._lock = threading.RLock()
        self._sessions: dict[str, _MCPSessionWorker] = {}
        with _MCP_PROCESS_TRANSITION_LOCK:
            _MCP_MANAGER_REGISTRY.add(self)

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
            current_vetting = dict(current_row.get("vetting", {}) or {})
            next_vetting = dict(next_row.get("vetting", {}) or {})
            launch_changed = current_vetting.get("stdio_launch_digest") != next_vetting.get(
                "stdio_launch_digest"
            )
            if _config_fingerprint(current_row) != _config_fingerprint(next_row) or launch_changed:
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

    def shutdown(self) -> bool:
        """Close every live session and report whether termination was verified.

        Workers that do not acknowledge bounded shutdown remain tracked.  This
        lets the owning runtime retain its single-owner lease and fail closed
        instead of reporting a clean exit while an MCP subprocess may survive.
        """

        with self._lock:
            workers = list(self._sessions.items())
        for server_id, worker in workers:
            if worker.close(timeout=self.timeout_seconds):
                with self._lock:
                    if self._sessions.get(server_id) is worker:
                        self._sessions.pop(server_id, None)
        with self._lock:
            fully_stopped = not self._sessions
        if fully_stopped:
            with _MCP_PROCESS_TRANSITION_LOCK:
                _MCP_MANAGER_REGISTRY.discard(self)
        return fully_stopped

    def quiesce_local_stdio_sessions(self) -> tuple[str, ...]:
        """Close every local stdio process before sensitive material appears."""

        with self._lock:
            selected = [
                (server_id, worker)
                for server_id, worker in self._sessions.items()
                if worker.server.transport == "stdio"
            ]
        closed: list[str] = []
        failed: list[str] = []
        for server_id, worker in selected:
            if worker.close(timeout=self.timeout_seconds):
                with self._lock:
                    if self._sessions.get(server_id) is worker:
                        self._sessions.pop(server_id, None)
                closed.append(server_id)
            else:
                failed.append(server_id)
        if failed:
            raise MCPLaunchIdentityError(
                "mcp_stdio_quiesce_failed",
                "Sensitive material was not created because local MCP stdio "
                "session termination could not be verified: " + ", ".join(sorted(failed)),
            )
        return tuple(sorted(closed))

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
            self._validate_secret_store_boundary(server)
            if not _has_live_endpoint(server):
                if server.tools:
                    row["status"] = "online"
                    row["session_state"] = "static"
                    row["error"] = None
                    row["last_seen_at"] = _now()
                    row["last_latency_ms"] = _elapsed_ms(started)
                    return {
                        "ok": True,
                        "message": "Static MCP tool manifest is available.",
                        "server": self.state.upsert_mcp_server(row),
                    }
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
            return {
                "ok": True,
                "message": f"Connected and discovered {len(tools)} tools.",
                "server": self.state.upsert_mcp_server(row),
            }
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
        self._validate_secret_store_boundary(server)
        if server.transport == "stdio" and server.command:
            row = refresh_stdio_launch_vetting(row)
            server = _server_from_state(row)
        vetting = dict(row.get("vetting", {}) or {})
        expected_hash = vetting.get("stdio_command_hash")
        expected_launch_digest = vetting.get("stdio_launch_digest")
        if server.transport == "stdio" and server.command:
            identity = resolve_stdio_launch_identity(server)
            vetting["stdio_launch_snapshot"] = self._create_launch_snapshot(
                server,
                identity,
            )
            expected_hash = _stdio_command_hash(server.command, server.args)
            vetting["stdio_command_hash"] = expected_hash
            vetting["connect_requires_approval"] = True
        vetting["connect_approved"] = True
        vetting["connect_approved_at"] = _now()
        if isinstance(expected_hash, str) and expected_hash:
            vetting["connect_approved_command_hash"] = expected_hash
        if isinstance(expected_launch_digest, str) and expected_launch_digest:
            vetting["connect_approved_launch_digest"] = expected_launch_digest
        row["vetting"] = vetting
        self._close_session(server_id)
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
            self._validate_secret_store_boundary(server)
            if not _has_live_endpoint(server):
                if server.tools:
                    row["status"] = "online"
                    row["session_state"] = "static"
                    row["error"] = None
                    row["last_seen_at"] = _now()
                    row["last_latency_ms"] = _elapsed_ms(started)
                    return {
                        "ok": True,
                        "message": "Static manifest is healthy.",
                        "server": self.state.upsert_mcp_server(row),
                    }
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
            return {
                "ok": True,
                "message": "Live MCP session is healthy.",
                "server": self.state.upsert_mcp_server(row),
            }
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
            _validate_server_endpoint(
                _server_from_state(server), allow_network_endpoints=self.allow_network_endpoints
            )
            self._validate_secret_store_boundary(_server_from_state(server))
            prefer_static = _prefer_static_manifest(server)
            _validate_stdio_command_hash(server)
            approval_result = self._connect_approval_result(server, latency_ms=_elapsed_ms(started))
            if approval_result is not None:
                return dict(approval_result["server"])
            tools = self._discover_tools(_server_from_state(server), prefer_static=prefer_static)
            server["tools"] = tools
            server["status"] = "synced"
            server["session_state"] = (
                "static"
                if prefer_static or not _has_live_endpoint(_server_from_state(server))
                else "connected"
            )
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
                if (
                    include_disabled
                    or self.capability_policy is None
                    or self.capability_policy.tool_decision(adapter.spec).effective_enabled
                ):
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
            self._validate_secret_store_boundary(server)
            if not _has_live_endpoint(server):
                raise ValueError(
                    "MCP server has static tool metadata but no command or URL to invoke."
                )
            result = self._call_live_tool(server, tool_name, arguments)
            latency_ms = _elapsed_ms(started)
            return ToolExecution(
                call=call,
                success=True,
                content=redact_text(result),
                data={
                    "server_id": server.id,
                    "latency_ms": latency_ms,
                    "session_state": "connected",
                },
            )
        except MCPToolOutcomeIndeterminate as exc:
            cleanup_verified = True
            try:
                self._close_session(server.id)
            except MCPLaunchIdentityError:
                cleanup_verified = False
            return ToolExecution(
                call=call,
                success=False,
                content=(
                    "MCP tool response timed out after the remote operation may have committed. "
                    "The outcome is indeterminate; do not retry without reconciling remote state."
                ),
                data={
                    "server_id": server.id,
                    "latency_ms": _elapsed_ms(started),
                    "session_state": (
                        "disconnected" if cleanup_verified else "cleanup_incomplete"
                    ),
                    "outcome_indeterminate": True,
                    "retryable": False,
                    "reconciliation_required": True,
                    "cleanup_verified": cleanup_verified,
                    "timeout_error": _safe_exception_text(exc),
                },
                error="mcp_tool_outcome_indeterminate",
            )
        except MCPRemoteToolError as exc:
            return ToolExecution(
                call=call,
                success=False,
                content=_safe_exception_text(exc),
                data={
                    "server_id": server.id,
                    "latency_ms": _elapsed_ms(started),
                    "session_state": "connected",
                    "remote_error": True,
                    "retryable": False,
                },
                error="mcp_tool_remote_error",
            )
        except Exception as exc:  # noqa: BLE001
            cleanup_verified = True
            try:
                self._close_session(server.id)
            except MCPLaunchIdentityError:
                cleanup_verified = False
            safe_error = _safe_exception_text(exc)
            return ToolExecution(
                call=call,
                success=False,
                content=safe_error,
                data={
                    "server_id": server.id,
                    "latency_ms": _elapsed_ms(started),
                    "session_state": "error" if cleanup_verified else "cleanup_incomplete",
                    "cleanup_verified": cleanup_verified,
                },
                error=(
                    "mcp_tool_failed"
                    if cleanup_verified
                    else "mcp_session_cleanup_incomplete"
                ),
            )

    def invoke_tool(
        self, server_id: str, tool_name: str, arguments: dict[str, Any]
    ) -> ToolExecution:
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
                    data={
                        "server_id": server.id,
                        "session_state": str(row.get("session_state", "disconnected")),
                    },
                    error="tool_disabled",
                )
        try:
            _validate_server_endpoint(server, allow_network_endpoints=self.allow_network_endpoints)
            self._validate_secret_store_boundary(server)
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
            row["session_state"] = str(execution.data.get("session_state") or "error")
            row["error"] = execution.content
            row["failure_count"] = int(row.get("failure_count", 0)) + 1
        self.state.upsert_mcp_server(row)
        return execution

    def _connect_approval_result(
        self, row: dict[str, Any], *, latency_ms: int
    ) -> dict[str, Any] | None:
        if not _connect_requires_approval(row):
            return None
        row["status"] = "approval_required"
        row["session_state"] = "approval_required"
        row["error"] = "MCP connect approval required."
        row["last_latency_ms"] = latency_ms
        return {
            "ok": False,
            "message": "MCP connect approval required.",
            "server": self.state.upsert_mcp_server(row),
        }

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

    def _discover_tools(
        self, server: MCPServerConfig, *, prefer_static: bool = False
    ) -> list[dict[str, Any]]:
        if prefer_static or not _has_live_endpoint(server):
            return [_normalize_tool(server, tool) for tool in server.tools]
        worker = self._worker_for(server)
        tools = worker.list_tools(timeout=self.timeout_seconds)
        return [_normalize_sdk_tool(server, tool) for tool in tools]

    def _call_live_tool(
        self, server: MCPServerConfig, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        worker = self._worker_for(server)
        return worker.call_tool(tool_name, arguments, timeout=self.timeout_seconds)

    def _worker_for(self, server: MCPServerConfig) -> _MCPSessionWorker:
        fingerprint = _config_fingerprint(_server_to_dict(server))
        with self._lock:
            worker = self._sessions.get(server.id)
            if (
                worker is not None
                and worker.fingerprint == fingerprint
                and worker.reusable
            ):
                return worker
            if worker is not None:
                self._close_session(server.id)
            worker = _MCPSessionWorker(
                server=server,
                fingerprint=fingerprint,
                secret_resolver=self.secret_resolver,
                launch_guard=self._validate_secret_store_boundary,
                snapshot_root=self._snapshot_root(),
            )
            self._sessions[server.id] = worker
            return worker

    def _close_session(self, server_id: str) -> None:
        with self._lock:
            worker = self._sessions.get(server_id)
            if worker is None:
                return
            if not worker.close(timeout=self.timeout_seconds):
                raise MCPLaunchIdentityError(
                    "mcp_session_close_failed",
                    "MCP session termination could not be verified; the worker remains "
                    f"tracked and server state was not changed: {server_id}",
                )
            if self._sessions.get(server_id) is worker:
                self._sessions.pop(server_id, None)

    def _mark_error(
        self, row: dict[str, Any], exc: Exception, *, latency_ms: int
    ) -> dict[str, Any]:
        row["status"] = "error"
        row["session_state"] = "error"
        row["error"] = _safe_exception_text(exc)
        row["last_error_at"] = _now()
        row["failure_count"] = int(row.get("failure_count", 0)) + 1
        row["last_latency_ms"] = latency_ms
        return self.state.upsert_mcp_server(row)

    def _validate_secret_store_boundary(self, server: MCPServerConfig) -> None:
        if server.transport != "stdio" or not server.command:
            return
        if (
            self.secret_backend in MCP_RAW_FILE_SECRET_BACKENDS
            and self.secret_store_path is not None
            and _path_exists_or_indeterminate(self.secret_store_path)
        ):
            raise MCPLaunchIdentityError(
                "raw_secret_store_blocks_stdio",
                "MCP stdio launch is disabled while the configured raw JSON secret "
                "vault exists. At-rest storage is not same-account process isolation; "
                "use a remote authenticated MCP endpoint or a contained runtime.",
            )
        if (
            self.secret_backend == "keyring"
            and self.secret_store_path is not None
            and _keyring_metadata_has_material(self.secret_store_path)
        ):
            raise MCPLaunchIdentityError(
                "keyring_secret_store_blocks_stdio",
                "MCP stdio launch is disabled while Kestrel has OS-keyring secret "
                "records. Keyring storage is not same-account process isolation; use "
                "a remote authenticated MCP endpoint or a contained runtime.",
            )
        if self.workspace is not None and _repair_trust_material_exists_or_indeterminate(
            self.workspace
        ):
            raise MCPLaunchIdentityError(
                "repair_trust_blocks_stdio",
                "MCP stdio launch is disabled while repair trust material exists. "
                "Owner-only receipt keys are not isolated from same-account processes; "
                "use a remote authenticated MCP endpoint or a contained runtime.",
            )
        self._validate_secret_script_boundary(server)

    def _validate_secret_script_boundary(self, server: MCPServerConfig) -> None:
        if not server.secret_env or not server.command:
            return
        identity = resolve_stdio_launch_identity(server)
        if (
            identity.artifact_kind != "executable"
            and self._trusted_plugin_artifact_root(server) is None
        ):
            raise MCPLaunchIdentityError(
                "secret_script_requires_plugin_snapshot",
                "MCP secret injection into standalone script launchers is disabled; "
                "use an installed plugin with a verified private source snapshot.",
            )
        if identity.artifact_kind == "executable" and _launch_file_has_shebang(
            Path(identity.executable_resolved_path)
        ):
            raise MCPLaunchIdentityError(
                "secret_shebang_script_forbidden",
                "MCP secret injection into direct shebang script executables is "
                "disabled; use a supported installed-plugin script launcher.",
            )

    def _trusted_plugin_artifact_root(self, server: MCPServerConfig) -> Path | None:
        prefix = "plugin."
        if not server.id.startswith(prefix):
            return None
        plugin_id, separator, _child = server.id[len(prefix) :].partition(".")
        if not separator or not plugin_id:
            return None
        try:
            plugin = self.state.get_plugin(plugin_id)
        except KeyError:
            return None
        expected = (Path(str(plugin["install_path"])) / "source").expanduser().resolve()
        configured = dict(server.vetting or {}).get("plugin_artifact_root")
        if not isinstance(configured, str):
            return None
        try:
            configured_root = Path(configured).expanduser().resolve(strict=True)
        except OSError:
            return None
        if configured_root != expected or not configured_root.is_dir():
            return None
        return configured_root

    def _snapshot_root(self) -> Path:
        return self.state.path.parent / "mcp_artifacts"

    def _create_launch_snapshot(
        self,
        server: MCPServerConfig,
        identity: MCPLaunchIdentity,
    ) -> dict[str, object]:
        snapshot_base = self._snapshot_root()
        _ensure_private_snapshot_directory(
            snapshot_base,
            allow_harden_empty=True,
            harden_existing_posix=True,
        )
        snapshot_dir = snapshot_base / identity.digest.removeprefix("sha256:")
        trusted_plugin_root = self._trusted_plugin_artifact_root(server)
        artifact_source = Path(identity.artifact_locator)
        if identity.artifact_tree_sha256 is not None:
            if trusted_plugin_root is None:
                raise MCPLaunchIdentityError(
                    "untrusted_plugin_tree",
                    "MCP plugin source tree is not bound to an installed plugin.",
                )
            relative_artifact = artifact_source.relative_to(trusted_plugin_root)
            _ensure_private_tree_snapshot(
                trusted_plugin_root,
                snapshot_dir,
                expected_digest=identity.artifact_tree_sha256,
            )
            snapshot_artifact = snapshot_dir / relative_artifact
            snapshot_digest = _hash_private_launch_tree(snapshot_dir)
            snapshot_kind = "plugin_tree"
        else:
            snapshot_artifact = snapshot_dir / artifact_source.name
            executable = identity.artifact_kind == "executable"
            _ensure_private_file_snapshot(
                artifact_source,
                snapshot_artifact,
                expected_digest=identity.artifact_sha256,
                executable=executable,
            )
            snapshot_digest = _hash_private_launch_file(
                snapshot_artifact,
                label="private launch snapshot",
            )
            snapshot_kind = "executable" if executable else "script"

        launch_args = list(identity.launch_args)
        executable_path = identity.executable_path
        executable_resolved_path = identity.executable_resolved_path
        if snapshot_kind == "executable":
            executable_path = str(snapshot_artifact)
            executable_resolved_path = str(snapshot_artifact)
        else:
            artifact_index = _launch_artifact_index(identity.artifact_kind)
            launch_args[artifact_index] = str(snapshot_artifact)
        return {
            "kind": snapshot_kind,
            "root": str(snapshot_dir),
            "artifact_path": str(snapshot_artifact),
            "executable_path": executable_path,
            "executable_resolved_path": executable_resolved_path,
            "executable_sha256": identity.executable_sha256,
            "launch_args": launch_args,
            "source_identity_digest": identity.digest,
            "snapshot_digest": snapshot_digest,
        }


@contextmanager
def mcp_sensitive_material_transition() -> Iterator[tuple[str, ...]]:
    """Serialize stdio launch against secret/receipt creation and quiesce first."""

    with _MCP_PROCESS_TRANSITION_LOCK:
        closed: list[str] = []
        for manager in list(_MCP_MANAGER_REGISTRY):
            closed.extend(manager.quiesce_local_stdio_sessions())
        yield tuple(sorted(set(closed)))


class MCPToolAdapter(AgentTool):
    def __init__(self, manager: MCPManager, server: MCPServerConfig, tool: dict[str, Any]) -> None:
        self.manager = manager
        self.server = server
        self.remote_tool_name = str(tool["remote_name"])
        risk = _risk_level(tool.get("risk", "medium"))
        self.spec = ToolSpec(
            name=str(tool["name"]),
            description=str(
                tool.get("description") or f"MCP tool {self.remote_tool_name} from {server.name}"
            ),
            parameters=dict(tool.get("parameters") or {"type": "object", "properties": {}}),
            risk=risk,
            requires_approval=(
                risk == "high" or bool(tool.get("requires_approval", risk in {"medium", "high"}))
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
    def __init__(
        self,
        *,
        server: MCPServerConfig,
        fingerprint: str,
        secret_resolver: SecretResolver | None = None,
        launch_guard: Callable[[MCPServerConfig], None] | None = None,
        snapshot_root: Path | None = None,
    ) -> None:
        self.server = server
        self.fingerprint = fingerprint
        self.secret_resolver = secret_resolver
        self.launch_guard = launch_guard
        self.snapshot_root = snapshot_root
        self._lock = threading.RLock()
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session_context: Any | None = None
        self._session: Any | None = None
        self._closure_failed = False

    @property
    def is_open(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def reusable(self) -> bool:
        with self._lock:
            return self.is_open and not self._closure_failed

    def list_tools(self, *, timeout: float) -> list[Any]:
        return cast(list[Any], self._submit(self._list_tools(), timeout=timeout))

    def call_tool(self, tool_name: str, arguments: dict[str, Any], *, timeout: float) -> str:
        return str(
            self._submit(
                self._call_tool(tool_name, arguments),
                timeout=timeout,
                outcome_may_have_committed=True,
            )
        )

    def close(self, *, timeout: float) -> bool:
        # A concurrent first call can have published ``_thread`` immediately
        # before the event-loop thread publishes ``_loop``.  Wait for that
        # bounded startup handshake instead of clearing the references and
        # leaving an untracked daemon loop behind.
        with self._lock:
            starting_thread = self._thread
            starting_loop = self._loop
        if (
            starting_thread is not None
            and starting_thread.is_alive()
            and starting_loop is None
            and not self._ready.wait(timeout=timeout)
        ):
            return False
        with self._lock:
            loop = self._loop
            thread = self._thread
            if thread is None:
                self._loop = None
                self._thread = None
                self._closure_failed = False
                return True
            if not thread.is_alive():
                if (
                    self._closure_failed
                    or self._session is not None
                    or self._session_context is not None
                ):
                    # A dead event loop is not proof that a session whose
                    # teardown failed released its transport subprocess.
                    self._closure_failed = True
                    return False
                self._loop = None
                self._thread = None
                self._ready.clear()
                self._closure_failed = False
                return True
            if loop is None or loop.is_closed():
                self._closure_failed = True
                return False
            disconnect_succeeded = True
            try:
                future = asyncio.run_coroutine_threadsafe(self._disconnect(), loop)
                future.result(timeout=timeout)
            except Exception:
                disconnect_succeeded = False
                self._closure_failed = True
                if "future" in locals():
                    future.cancel()
            if disconnect_succeeded:
                try:
                    loop.call_soon_threadsafe(loop.stop)
                except RuntimeError:
                    pass
        thread.join(timeout=timeout)
        closed = not thread.is_alive() and disconnect_succeeded
        with self._lock:
            if closed:
                self._loop = None
                self._thread = None
                self._session = None
                self._session_context = None
                self._ready.clear()
                self._closure_failed = False
        return closed

    def _submit(
        self,
        awaitable: Coroutine[Any, Any, Any],
        *,
        timeout: float,
        outcome_may_have_committed: bool = False,
    ) -> Any:
        loop = self._ensure_loop(timeout=timeout)
        future: concurrent.futures.Future[Any] = asyncio.run_coroutine_threadsafe(awaitable, loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            if outcome_may_have_committed:
                raise MCPToolOutcomeIndeterminate(
                    f"MCP tool operation timed out after {timeout:.1f}s."
                ) from exc
            raise TimeoutError(f"MCP operation timed out after {timeout:.1f}s.") from exc

    def _ensure_loop(self, *, timeout: float) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return self._loop
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name=f"mcp-session-{self.server.id}", daemon=True
            )
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
            self._session_context = _session_context(
                self.server,
                secret_resolver=self.secret_resolver,
                launch_guard=self.launch_guard,
                snapshot_root=self.snapshot_root,
            )
        except TypeError as exc:
            if not any(field in str(exc) for field in ("secret_resolver", "launch_guard")):
                raise
            self._session_context = _session_context(self.server)
        self._session = await self._session_context.__aenter__()
        return self._session

    async def _disconnect(self) -> None:
        if self._session_context is not None:
            await self._session_context.__aexit__(None, None, None)
        # Only erase the teardown handle after it acknowledged success.  If it
        # raises, the owner must retain both the handle and event loop so a
        # bounded retry can reconcile the transport instead of falsely
        # reporting a clean shutdown.
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


def _session_context(
    server: MCPServerConfig,
    *,
    secret_resolver: SecretResolver | None = None,
    launch_guard: Callable[[MCPServerConfig], None] | None = None,
    snapshot_root: Path | None = None,
) -> Any:
    if server.transport == "stdio":
        return _VerifiedStdioSessionContext(
            server,
            secret_resolver=secret_resolver,
            launch_guard=launch_guard,
            snapshot_root=snapshot_root,
        )
    if server.transport == "streamable_http":
        http_mod = import_module("mcp.client.streamable_http")
        return _ClientSessionContext(http_mod.streamablehttp_client(server.url or ""))
    if server.transport == "sse":
        sse_mod = import_module("mcp.client.sse")
        return _ClientSessionContext(sse_mod.sse_client(server.url or ""))
    raise ValueError(f"Unsupported MCP transport: {server.transport}")


class _VerifiedStdioSessionContext:
    """Revalidate launch bytes before secrets and again immediately pre-spawn."""

    def __init__(
        self,
        server: MCPServerConfig,
        *,
        secret_resolver: SecretResolver | None,
        launch_guard: Callable[[MCPServerConfig], None] | None,
        snapshot_root: Path | None,
    ) -> None:
        self.server = server
        self.secret_resolver = secret_resolver
        self.launch_guard = launch_guard
        self.snapshot_root = snapshot_root
        self.inner: _ClientSessionContext | None = None

    async def __aenter__(self) -> Any:
        with _MCP_PROCESS_TRANSITION_LOCK:
            return await self._enter_while_transition_locked()

    async def _enter_while_transition_locked(self) -> Any:
        if self.launch_guard is not None:
            self.launch_guard(self.server)
        source_identity = _approved_launch_identity(self.server)
        before_secrets = _verified_snapshot_launch(
            self.server,
            snapshot_root=self.snapshot_root,
        )
        if before_secrets.source_identity_digest != source_identity.digest:
            raise MCPLaunchIdentityError(
                "snapshot_source_mismatch",
                "MCP private snapshot is not bound to the approved source identity.",
            )
        runtime_env = _runtime_env(
            self.server,
            secret_resolver=self.secret_resolver,
        )
        before_spawn = _verified_snapshot_launch(
            self.server,
            snapshot_root=self.snapshot_root,
        )
        if self.launch_guard is not None:
            self.launch_guard(self.server)
        if before_spawn != before_secrets:
            raise MCPLaunchIdentityError(
                "snapshot_changed_before_spawn",
                "MCP private snapshot changed after validation; refusing to start it.",
            )
        stdio_mod = import_module("mcp.client.stdio")
        client_mod = import_module("mcp")
        params = client_mod.StdioServerParameters(
            command=before_spawn.executable_path,
            args=list(before_spawn.launch_args),
            env=runtime_env or None,
            cwd=before_spawn.cwd,
        )
        final_plan = _verified_snapshot_launch(
            self.server,
            snapshot_root=self.snapshot_root,
        )
        if final_plan != before_spawn:
            raise MCPLaunchIdentityError(
                "snapshot_changed_before_spawn",
                "MCP private snapshot changed immediately before process creation; "
                "refusing to start it.",
            )
        stream_context = stdio_mod.stdio_client(params)
        launch_plan = _verified_snapshot_launch(
            self.server,
            snapshot_root=self.snapshot_root,
        )
        if launch_plan != final_plan:
            raise MCPLaunchIdentityError(
                "snapshot_changed_before_spawn",
                "MCP private snapshot changed at process creation; refusing to start it.",
            )
        self.inner = _ClientSessionContext(stream_context)
        return await self.inner.__aenter__()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.inner is not None:
            await self.inner.__aexit__(exc_type, exc, tb)


def _approved_launch_identity(server: MCPServerConfig) -> MCPLaunchIdentity:
    identity = resolve_stdio_launch_identity(server)
    metadata = dict(server.vetting or {})
    approved_digest = metadata.get("connect_approved_launch_digest")
    if not isinstance(approved_digest, str) or approved_digest != identity.digest:
        raise MCPLaunchIdentityError(
            "approved_artifact_changed",
            "MCP stdio launch artifact does not match the approved identity.",
        )
    return identity


def _verified_snapshot_launch(
    server: MCPServerConfig,
    *,
    snapshot_root: Path | None,
) -> MCPVerifiedLaunchPlan:
    metadata = dict(server.vetting or {})
    raw_snapshot = metadata.get("stdio_launch_snapshot")
    if not isinstance(raw_snapshot, dict):
        raise MCPLaunchIdentityError(
            "snapshot_missing",
            "MCP private launch snapshot is missing; fresh connect approval is required.",
        )
    source_digest = raw_snapshot.get("source_identity_digest")
    approved_digest = metadata.get("connect_approved_launch_digest")
    if not isinstance(source_digest, str) or source_digest != approved_digest:
        raise MCPLaunchIdentityError(
            "snapshot_source_mismatch",
            "MCP private snapshot is not bound to the approved source identity.",
        )
    try:
        raw_root = Path(str(raw_snapshot["root"]))
        raw_artifact = Path(str(raw_snapshot["artifact_path"]))
        _reject_windows_reparse_ancestors(raw_root, label="private snapshot")
        _reject_windows_reparse_ancestors(raw_artifact, label="private snapshot artifact")
        root = raw_root.resolve(strict=True)
        artifact = raw_artifact.resolve(strict=True)
        executable = Path(str(raw_snapshot["executable_path"])).absolute()
        executable_resolved = executable.resolve(strict=True)
        expected_executable_resolved = Path(str(raw_snapshot["executable_resolved_path"])).resolve(
            strict=True
        )
    except (KeyError, OSError) as exc:
        raise MCPLaunchIdentityError(
            "snapshot_unavailable",
            "MCP private launch snapshot is unavailable.",
        ) from exc
    if snapshot_root is not None:
        try:
            _reject_windows_reparse_ancestors(
                snapshot_root,
                label="private snapshot base",
            )
            allowed_root = snapshot_root.resolve(strict=True)
        except OSError as exc:
            raise MCPLaunchIdentityError(
                "snapshot_unavailable",
                "MCP private snapshot base is unavailable.",
            ) from exc
        _assert_private_snapshot_permissions(allowed_root)
        if not _path_within(root, allowed_root):
            raise MCPLaunchIdentityError(
                "snapshot_outside_private_root",
                "MCP private launch snapshot is outside its protected root.",
            )
    else:
        allowed_root = root.parent
        _assert_private_snapshot_permissions(allowed_root)
    if not _path_within(artifact, root):
        raise MCPLaunchIdentityError(
            "snapshot_artifact_outside_root",
            "MCP private launch artifact is outside its protected snapshot.",
        )
    if executable_resolved != expected_executable_resolved:
        raise MCPLaunchIdentityError(
            "executable_path_mismatch",
            "MCP launch executable resolves to a different approved path.",
        )
    _assert_private_snapshot_permissions(root, recursive=True)
    kind = str(raw_snapshot.get("kind") or "")
    expected_snapshot_digest = str(raw_snapshot.get("snapshot_digest") or "")
    if kind == "plugin_tree":
        actual_snapshot_digest = _hash_private_launch_tree(root)
    elif kind in {"script", "executable"}:
        actual_snapshot_digest = _hash_private_launch_file(
            artifact,
            label="private launch snapshot",
        )
    else:
        raise MCPLaunchIdentityError(
            "snapshot_kind_invalid",
            "MCP private launch snapshot kind is invalid.",
        )
    if actual_snapshot_digest != expected_snapshot_digest:
        raise MCPLaunchIdentityError(
            "snapshot_integrity_mismatch",
            "MCP private launch snapshot failed integrity validation.",
        )
    expected_executable_digest = str(raw_snapshot.get("executable_sha256") or "")
    actual_executable_digest = _hash_launch_file(
        executable_resolved,
        label="launch executable",
    )
    if kind != "executable" and actual_executable_digest != expected_executable_digest:
        raise MCPLaunchIdentityError(
            "executable_integrity_mismatch",
            "MCP launch executable changed after approval.",
        )
    launch_args_raw = raw_snapshot.get("launch_args")
    if not isinstance(launch_args_raw, list):
        raise MCPLaunchIdentityError(
            "snapshot_args_invalid",
            "MCP private launch arguments are invalid.",
        )
    launch_args = tuple(str(item) for item in launch_args_raw)
    artifact_index = (
        None
        if kind == "executable"
        else _launch_artifact_index(
            str(dict(raw_snapshot).get("artifact_kind") or _snapshot_artifact_kind(server))
        )
    )
    if artifact_index is not None and (
        len(launch_args) <= artifact_index
        or Path(launch_args[artifact_index]).resolve() != artifact
    ):
        raise MCPLaunchIdentityError(
            "snapshot_args_mismatch",
            "MCP private launch arguments do not reference the verified artifact.",
        )
    plan_payload = {
        "executable_path": str(executable),
        "executable_sha256": actual_executable_digest,
        "launch_args": list(launch_args),
        "snapshot_digest": actual_snapshot_digest,
        "source_identity_digest": source_digest,
    }
    plan_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
    )
    # Keep the access-control boundary launch-adjacent as well as hash-adjacent.
    # A weak parent can rename a protected child, while a weak nested directory
    # can expose files despite a protected snapshot root on Windows.
    _assert_private_snapshot_permissions(allowed_root)
    _assert_private_snapshot_permissions(root, recursive=True)
    return MCPVerifiedLaunchPlan(
        executable_path=str(executable),
        launch_args=launch_args,
        cwd=str(root),
        source_identity_digest=source_digest,
        snapshot_digest=plan_digest,
    )


def _snapshot_artifact_kind(server: MCPServerConfig) -> str:
    command_name = Path(server.command or "").name.lower().removesuffix(".exe")
    if command_name == "deno":
        return "deno_module"
    if command_name in {"node", "nodejs"}:
        return "node_module"
    if _is_python_command(command_name):
        return "python_script"
    return "executable"


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
        try:
            if self.session is not None:
                await self.session.__aexit__(exc_type, exc, tb)
        finally:
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
            raise ValueError(
                f"MCP secret-looking environment variable {key} must be configured via secret_env."
            )
    for target, source in secret_env.items():
        if not _valid_env_name(target) or not (_valid_env_name(source) or is_secret_ref(source)):
            raise ValueError(
                "MCP secret_env keys must be env names and values must be env names or secret:// refs."
            )
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
        "connect_approved_launch_digest",
        "stdio_launch_snapshot",
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


def _server_to_state_fragment(
    server: MCPServerConfig,
    vetting: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": server.id,
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "args": list(server.args),
        "env": dict(server.env or {}),
        "secret_env": dict(server.secret_env or {}),
        "url": server.url,
        "enabled": server.enabled,
        "tools": [dict(tool) for tool in server.tools],
        "risk_policy": server.risk_policy,
        "vetting": dict(vetting or server.vetting or {}),
    }


def _normalize_tool(server: MCPServerConfig, tool: dict[str, Any]) -> dict[str, Any]:
    remote_name = _safe_mcp_identifier(
        str(tool.get("remote_name") or tool.get("name")),
        field="tool name",
    )
    risk, requires_approval = _risk_fields(server, tool)
    parameters = _sanitize_mcp_metadata(
        tool.get("parameters") or tool.get("inputSchema") or {"type": "object", "properties": {}}
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
            redact_text(str(capability)) for capability in tool.get("capabilities", ["mcp"])
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
    return sorted(
        {str(capability) for tool in tools for capability in tool.get("capabilities", [])}
    )


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
        getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
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
        manifest_requires_approval = bool(tool.get("requires_approval", risk in {"medium", "high"}))
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
        resource = stdio_launch_resource(_server_to_state_fragment(server, vetting))
        launch_digest = _launch_resource_digest(resource)
        previous_launch_digest = vetting.get("stdio_launch_digest")
        vetting["stdio_launch_resource"] = resource
        vetting["stdio_launch_digest"] = launch_digest
        if previous_hash != command_hash or previous_launch_digest != launch_digest:
            _clear_connect_approval(vetting)
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


def _runtime_env(
    server: MCPServerConfig, *, secret_resolver: SecretResolver | None = None
) -> dict[str, str]:
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
        raise ValueError(
            "MCP network endpoints are disabled. Enable allow_mcp_network_endpoints first."
        )
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
            raise ValueError("MCP stdio Python commands must name a `.py` script.")
        if server.args[0] == "-m":
            raise ValueError(
                "MCP stdio `python -m` launchers are mutable and are not allowed; "
                "configure the resolved module script path instead."
            )
        elif server.args[0].startswith("-") or not server.args[0].lower().endswith(".py"):
            raise ValueError("MCP stdio Python commands must name a `.py` script.")
        return

    if command_name in {"node", "nodejs"}:
        if (
            not server.args
            or server.args[0].startswith("-")
            or not server.args[0].lower().endswith((".js", ".cjs", ".mjs"))
        ):
            raise ValueError(
                "MCP stdio Node commands must name a JavaScript module file; eval flags are forbidden."
            )
        return

    if command_name in {"npx", "uvx", "bunx"}:
        raise ValueError(
            "MCP stdio package runners are disabled because registry coordinates do not "
            "prove the bytes that will execute; install a reviewed executable or script."
        )

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
        _clear_connect_approval(vetting)
        raise ValueError("MCP stdio command hash mismatch; refusing to connect.")
    current_resource = stdio_launch_resource(row)
    current_digest = _launch_resource_digest(current_resource)
    expected_launch_digest = vetting.get("stdio_launch_digest")
    approved_launch_digest = vetting.get("connect_approved_launch_digest")
    if (
        isinstance(expected_launch_digest, str)
        and expected_launch_digest
        and expected_launch_digest != current_digest
    ):
        vetting["stdio_launch_resource"] = current_resource
        vetting["stdio_launch_digest"] = current_digest
        _clear_connect_approval(vetting)
        raise ValueError("MCP stdio launch artifact changed; connect approval was cleared.")
    if bool(vetting.get("connect_approved")) and (
        not isinstance(approved_launch_digest, str) or approved_launch_digest != current_digest
    ):
        _clear_connect_approval(vetting)
        raise ValueError("MCP stdio approved launch artifact changed; refusing to connect.")


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
        expected_launch_digest = metadata.get("stdio_launch_digest")
        return (
            metadata.get("connect_approved_command_hash") != actual
            or not isinstance(expected_launch_digest, str)
            or metadata.get("connect_approved_launch_digest") != expected_launch_digest
        )
    if not isinstance(expected, str) or not expected:
        return False
    return metadata.get("connect_approved_command_hash") != expected


def _stdio_command_hash(command: object, args: object) -> str:
    arg_list = (
        [str(item) for item in args]
        if isinstance(args, list)
        else [str(item) for item in args]
        if isinstance(args, tuple)
        else []
    )
    payload = json.dumps(
        {"command": "" if command is None else str(command), "args": arg_list},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def resolve_stdio_launch_identity(server: MCPServerConfig) -> MCPLaunchIdentity:
    """Resolve and hash the exact executable plus its entry artifact.

    This function is the single source of truth for configuration review,
    connect approval, capability digests, and the final stdio launch.
    """

    if server.transport != "stdio" or not server.command:
        raise MCPLaunchIdentityError(
            "not_stdio",
            "MCP launch identity is only defined for live stdio servers.",
        )
    _validate_stdio_command(server)
    executable_path, executable_resolved = _resolve_executable(server.command)
    executable_digest = _hash_launch_file(executable_resolved, label="executable")
    launch_args = list(server.args)
    command_name = executable_resolved.name.lower().removesuffix(".exe")

    artifact_kind = "executable"
    artifact_locator = str(executable_resolved)
    artifact_digest = executable_digest
    artifact_tree_digest: str | None = None
    if _is_python_command(command_name):
        if not launch_args or launch_args[0] == "-m":
            raise MCPLaunchIdentityError(
                "mutable_python_module",
                "MCP stdio `python -m` launchers are mutable and are not allowed; "
                "configure the resolved module script path instead.",
            )
        script = _resolve_launch_artifact(launch_args[0], label="Python script")
        launch_args[0] = str(script)
        artifact_kind = "python_script"
        artifact_locator = str(script)
        artifact_digest = _hash_launch_file(script, label="Python script")
    elif command_name in {"node", "nodejs"}:
        script = _resolve_launch_artifact(launch_args[0], label="Node module")
        launch_args[0] = str(script)
        artifact_kind = "node_module"
        artifact_locator = str(script)
        artifact_digest = _hash_launch_file(script, label="Node module")
    elif command_name == "deno":
        script = _resolve_launch_artifact(launch_args[1], label="Deno module")
        launch_args[1] = str(script)
        artifact_kind = "deno_module"
        artifact_locator = str(script)
        artifact_digest = _hash_launch_file(script, label="Deno module")

    plugin_root_raw = dict(server.vetting or {}).get("plugin_artifact_root")
    if isinstance(plugin_root_raw, str) and plugin_root_raw and artifact_kind != "executable":
        plugin_root = _resolve_launch_tree(plugin_root_raw)
        artifact_path = Path(artifact_locator)
        if not _path_within(artifact_path, plugin_root):
            raise MCPLaunchIdentityError(
                "plugin_artifact_outside_tree",
                "MCP plugin launch artifact is outside its reviewed source tree.",
            )
        artifact_tree_digest = _hash_launch_tree(plugin_root)
        artifact_kind = f"plugin_tree_{artifact_kind}"

    return MCPLaunchIdentity(
        executable_path=str(executable_path),
        executable_resolved_path=str(executable_resolved),
        executable_sha256=executable_digest,
        artifact_kind=artifact_kind,
        artifact_locator=artifact_locator,
        artifact_sha256=artifact_digest,
        artifact_tree_sha256=artifact_tree_digest,
        launch_args=tuple(launch_args),
    )


def stdio_launch_resource(row: dict[str, Any]) -> dict[str, object]:
    """Return deterministic launch identity, including a stable invalid state."""

    if str(row.get("transport")) != "stdio" or not row.get("command"):
        return {"status": "not_applicable"}
    server = _server_from_state(row)
    try:
        identity = resolve_stdio_launch_identity(server)
    except MCPLaunchIdentityError as exc:
        return {
            "status": "invalid",
            "reason": exc.code,
            "config_hash": _stdio_command_hash(server.command, server.args),
        }
    return {
        "status": "resolved",
        "digest": identity.digest,
        "identity": identity.to_dict(),
    }


def refresh_stdio_launch_vetting(
    row: dict[str, Any],
    *,
    current_row: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Refresh artifact identity and preserve approval only for an exact match."""

    refreshed = dict(row)
    if str(refreshed.get("transport")) != "stdio" or not refreshed.get("command"):
        return refreshed
    vetting = dict(refreshed.get("vetting", {}) or {})
    command_hash = _stdio_command_hash(refreshed.get("command"), refreshed.get("args", []))
    resource = stdio_launch_resource(refreshed)
    launch_digest = _launch_resource_digest(resource)
    vetting["stdio_command_hash"] = command_hash
    vetting["stdio_launch_resource"] = resource
    vetting["stdio_launch_digest"] = launch_digest
    vetting["connect_requires_approval"] = True

    prior_vetting = dict((current_row or {}).get("vetting", {}) or {})
    approval_matches = (
        bool(prior_vetting.get("connect_approved"))
        and prior_vetting.get("connect_approved_command_hash") == command_hash
        and prior_vetting.get("connect_approved_launch_digest") == launch_digest
    )
    _clear_connect_approval(vetting)
    if approval_matches:
        for key in (
            "connect_approved",
            "connect_approved_at",
            "connect_approved_command_hash",
            "connect_approved_launch_digest",
            "stdio_launch_snapshot",
        ):
            if key in prior_vetting:
                vetting[key] = prior_vetting[key]
    refreshed["vetting"] = vetting
    return refreshed


def _resolve_executable(command: str) -> tuple[Path, Path]:
    candidate: str | None
    if "/" in command or "\\" in command:
        candidate = command
    else:
        candidate = shutil.which(command)
    if not candidate:
        raise MCPLaunchIdentityError(
            "executable_not_found",
            f"MCP stdio executable could not be resolved: {Path(command).name}",
        )
    try:
        launch_path = Path(candidate).expanduser()
        if not launch_path.is_absolute():
            launch_path = Path.cwd() / launch_path
        launch_path = launch_path.absolute()
        _reject_windows_reparse_ancestors(launch_path, label="executable")
        resolved = launch_path.resolve(strict=True)
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "executable_not_found",
            f"MCP stdio executable could not be resolved: {Path(command).name}",
        ) from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise MCPLaunchIdentityError(
            "executable_not_regular",
            "MCP stdio executable must be an executable regular file.",
        )
    return launch_path, resolved


def _resolve_launch_artifact(raw_path: str, *, label: str) -> Path:
    if not raw_path or raw_path.startswith("-"):
        raise MCPLaunchIdentityError(
            "artifact_not_found",
            f"MCP stdio {label} path is invalid.",
        )
    local_path = _local_launch_artifact_path(raw_path, label=label)
    try:
        candidate = Path(local_path).expanduser()
        _reject_windows_reparse_ancestors(candidate, label=label)
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "artifact_not_found",
            f"MCP stdio {label} could not be resolved.",
        ) from exc
    if not resolved.is_file():
        raise MCPLaunchIdentityError(
            "artifact_not_regular",
            f"MCP stdio {label} must be a regular file.",
        )
    return resolved


def _local_launch_artifact_path(raw_path: str, *, label: str) -> str:
    """Return one local path without treating a Windows drive as a URL scheme."""

    if re.match(r"^[A-Za-z]:[\\/]", raw_path):
        return raw_path
    parsed = urlparse(raw_path)
    if not parsed.scheme:
        return raw_path
    if parsed.scheme != "file" or parsed.netloc not in {"", "localhost"}:
        raise MCPLaunchIdentityError(
            "remote_artifact",
            f"MCP stdio {label} must be a local file.",
        )
    return url2pathname(parsed.path)


def _resolve_launch_tree(raw_path: str) -> Path:
    try:
        candidate = Path(raw_path).expanduser()
        _reject_windows_reparse_ancestors(candidate, label="plugin source tree")
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "plugin_tree_not_found",
            "MCP plugin source tree could not be resolved.",
        ) from exc
    metadata = resolved.lstat()
    if is_link_or_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise MCPLaunchIdentityError(
            "plugin_tree_not_directory",
            "MCP plugin source tree must be a directory.",
        )
    return resolved


def _require_real_launch_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "plugin_tree_unreadable",
            "MCP launch directory could not be read safely.",
        ) from exc
    if is_link_or_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise MCPLaunchIdentityError(
            "plugin_tree_symlink",
            "MCP launch directories cannot be links or reparse points.",
        )
    return metadata


def _reject_windows_reparse_ancestors(path: Path, *, label: str) -> None:
    absolute = Path(os.path.abspath(path))
    for candidate in tuple(reversed(absolute.parents)) + (absolute,):
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            continue
        if is_windows_reparse_point(metadata):
            raise MCPLaunchIdentityError(
                "artifact_reparse_point",
                f"MCP stdio {label} crosses a Windows reparse point.",
            )


def _remove_private_snapshot_tree(root: Path) -> None:
    _assert_private_snapshot_permissions(root)
    expected = _require_real_launch_directory(root)
    try:
        with os.scandir(root) as scanned:
            names = sorted(entry.name for entry in scanned)
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "snapshot_cleanup_failed",
            "MCP private snapshot cleanup could not inspect its temporary tree.",
        ) from exc
    for name in names:
        path = root / name
        metadata = path.lstat()
        if is_link_or_reparse_point(metadata):
            raise MCPLaunchIdentityError(
                "snapshot_cleanup_failed",
                "MCP private snapshot cleanup refused a linked temporary entry.",
            )
        if stat.S_ISDIR(metadata.st_mode):
            _remove_private_snapshot_tree(path)
        elif stat.S_ISREG(metadata.st_mode):
            path.unlink()
        else:
            raise MCPLaunchIdentityError(
                "snapshot_cleanup_failed",
                "MCP private snapshot cleanup refused a special temporary entry.",
            )
    visible = _require_real_launch_directory(root)
    if not os.path.samestat(expected, visible):
        raise MCPLaunchIdentityError(
            "snapshot_cleanup_failed",
            "MCP private snapshot root changed during cleanup.",
        )
    root.rmdir()


def _launch_tree_files(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    total_bytes = 0

    def visit(current: Path) -> None:
        nonlocal total_bytes
        before = _require_real_launch_directory(current)
        try:
            with os.scandir(current) as scanned:
                names = sorted(entry.name for entry in scanned)
        except OSError as exc:
            raise MCPLaunchIdentityError(
                "plugin_tree_unreadable",
                "MCP plugin source tree could not be read safely.",
            ) from exc
        after = _require_real_launch_directory(current)
        if not os.path.samestat(before, after):
            raise MCPLaunchIdentityError(
                "plugin_tree_changed",
                "MCP plugin source tree changed while it was being inspected.",
            )
        for name in names:
            if name == ".git":
                continue
            candidate = current / name
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                raise MCPLaunchIdentityError(
                    "plugin_tree_unreadable",
                    "MCP plugin source tree could not be read safely.",
                ) from exc
            if is_link_or_reparse_point(metadata):
                raise MCPLaunchIdentityError(
                    "plugin_tree_symlink",
                    "MCP plugin source trees cannot contain links or reparse points.",
                )
            if stat.S_ISDIR(metadata.st_mode):
                visit(candidate)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise MCPLaunchIdentityError(
                    "plugin_tree_special_file",
                    "MCP plugin source trees can contain regular files only.",
                )
            total_bytes += metadata.st_size
            if len(files) >= MCP_SNAPSHOT_MAX_FILES or total_bytes > MCP_SNAPSHOT_MAX_BYTES:
                raise MCPLaunchIdentityError(
                    "plugin_tree_too_large",
                    "MCP plugin source tree exceeds the private snapshot limit.",
                )
            files.append((candidate.relative_to(root).as_posix(), candidate))

    visit(root)
    return sorted(files)


def _hash_launch_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for relative, path in _launch_tree_files(root):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_hash_launch_file(path, label="plugin file").encode("ascii"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _hash_private_launch_tree(root: Path) -> str:
    _assert_private_snapshot_permissions(root, recursive=True)
    digest = _hash_launch_tree(root)
    _assert_private_snapshot_permissions(root, recursive=True)
    return digest


def _hash_private_launch_file(path: Path, *, label: str) -> str:
    _assert_private_snapshot_permissions(path.parent, recursive=True)
    digest = _hash_launch_file(path, label=label)
    _assert_private_snapshot_permissions(path.parent, recursive=True)
    return digest


def _ensure_private_snapshot_directory(
    path: Path,
    *,
    allow_harden_empty: bool,
    harden_existing_posix: bool,
) -> None:
    _reject_windows_reparse_ancestors(path, label="private snapshot")
    if _uses_windows_snapshot_acls():
        try:
            create_owner_private_directory(path)
        except FileExistsError:
            try:
                validate_owner_private_directory(path)
            except PrivateDirectoryError as validation_error:
                if not allow_harden_empty:
                    _raise_private_snapshot_boundary_error(validation_error)
                try:
                    harden_empty_owner_private_directory(path)
                except PrivateDirectoryError as harden_error:
                    _raise_private_snapshot_boundary_error(harden_error)
        except PrivateDirectoryError as exc:
            _raise_private_snapshot_boundary_error(exc)
        _assert_private_snapshot_permissions(path)
        return

    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "snapshot_unavailable",
            "MCP private snapshot directory could not be created safely.",
        ) from exc
    if harden_existing_posix:
        try:
            os.chmod(path, 0o700)
        except OSError as exc:
            raise MCPLaunchIdentityError(
                "snapshot_permissions",
                "MCP private snapshot permissions could not be hardened.",
            ) from exc
    _assert_private_snapshot_permissions(path)


def _ensure_private_snapshot_subdirectory(root: Path, destination: Path) -> None:
    try:
        relative = destination.relative_to(root)
    except ValueError as exc:
        raise MCPLaunchIdentityError(
            "snapshot_outside_private_root",
            "MCP private snapshot directory escaped its protected root.",
        ) from exc
    current = root
    for component in relative.parts:
        current /= component
        _ensure_private_snapshot_directory(
            current,
            allow_harden_empty=False,
            harden_existing_posix=False,
        )


def _raise_private_snapshot_boundary_error(
    exc: PrivateDirectoryError,
) -> NoReturn:
    message = str(exc)
    code = (
        "snapshot_unavailable"
        if any(
            marker in message
            for marker in ("unavailable", "not_real", "identity_changed")
        )
        else "snapshot_permissions"
    )
    raise MCPLaunchIdentityError(
        code,
        "MCP private snapshot does not have a verified owner-private "
        "access-control boundary.",
    ) from exc


def _ensure_private_tree_snapshot(
    source_root: Path,
    destination: Path,
    *,
    expected_digest: str,
) -> None:
    if os.path.lexists(destination):
        if _hash_private_launch_tree(destination) != expected_digest:
            raise MCPLaunchIdentityError(
                "snapshot_integrity_mismatch",
                "Existing MCP private snapshot failed integrity validation.",
            )
        return
    _reject_windows_reparse_ancestors(destination.parent, label="private snapshot root")
    _ensure_private_snapshot_directory(
        destination.parent,
        allow_harden_empty=True,
        harden_existing_posix=False,
    )
    try:
        temporary = create_owner_private_temporary_directory(
            prefix=".mcp-snapshot-",
            parent=destination.parent,
        )
    except (OSError, PrivateDirectoryError) as exc:
        raise MCPLaunchIdentityError(
            "snapshot_permissions",
            "MCP private snapshot temporary directory could not be secured.",
        ) from exc
    _assert_private_snapshot_permissions(temporary)
    try:
        source_before = _hash_launch_tree(source_root)
        if source_before != expected_digest:
            raise MCPLaunchIdentityError(
                "source_changed_before_snapshot",
                "MCP plugin source changed before its private snapshot was created.",
            )
        for relative, source in _launch_tree_files(source_root):
            target = temporary / relative
            _ensure_private_snapshot_subdirectory(temporary, target.parent)
            _copy_private_launch_file(source, target, executable=False)
        if _hash_launch_tree(source_root) != expected_digest:
            raise MCPLaunchIdentityError(
                "source_changed_during_snapshot",
                "MCP plugin source changed while its private snapshot was created.",
            )
        if _hash_private_launch_tree(temporary) != expected_digest:
            raise MCPLaunchIdentityError(
                "snapshot_integrity_mismatch",
                "MCP private snapshot failed integrity validation.",
            )
        try:
            temporary.rename(destination)
        except FileExistsError:
            if _hash_private_launch_tree(destination) != expected_digest:
                raise MCPLaunchIdentityError(
                    "snapshot_integrity_mismatch",
                    "Concurrent MCP private snapshot failed integrity validation.",
                ) from None
    finally:
        if os.path.lexists(temporary):
            _remove_private_snapshot_tree(temporary)
    _assert_private_snapshot_permissions(destination, recursive=True)


def _ensure_private_file_snapshot(
    source: Path,
    destination: Path,
    *,
    expected_digest: str,
    executable: bool,
) -> None:
    if os.path.lexists(destination):
        destination_metadata = destination.lstat()
        if is_link_or_reparse_point(destination_metadata):
            raise MCPLaunchIdentityError(
                "snapshot_unavailable",
                "MCP private launch snapshot is unavailable.",
            )
        if (
            _hash_private_launch_file(
                destination,
                label="private launch snapshot",
            )
            != expected_digest
        ):
            raise MCPLaunchIdentityError(
                "snapshot_integrity_mismatch",
                "Existing MCP private snapshot failed integrity validation.",
            )
        return
    _reject_windows_reparse_ancestors(destination.parent, label="private snapshot root")
    _ensure_private_snapshot_directory(
        destination.parent,
        allow_harden_empty=True,
        harden_existing_posix=False,
    )
    temporary = (
        destination.parent / f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        if _hash_launch_file(source, label="launch artifact") != expected_digest:
            raise MCPLaunchIdentityError(
                "source_changed_before_snapshot",
                "MCP launch artifact changed before its private snapshot was created.",
            )
        _copy_private_launch_file(source, temporary, executable=executable)
        if _hash_launch_file(source, label="launch artifact") != expected_digest:
            raise MCPLaunchIdentityError(
                "source_changed_during_snapshot",
                "MCP launch artifact changed while its private snapshot was created.",
            )
        if (
            _hash_private_launch_file(
                temporary,
                label="private launch snapshot",
            )
            != expected_digest
        ):
            raise MCPLaunchIdentityError(
                "snapshot_integrity_mismatch",
                "MCP private snapshot failed integrity validation.",
            )
        try:
            temporary.replace(destination)
        except OSError as exc:
            raise MCPLaunchIdentityError(
                "snapshot_publish_failed",
                "MCP private snapshot could not be published safely.",
            ) from exc
    finally:
        if os.path.lexists(temporary):
            temporary_metadata = temporary.lstat()
            if is_link_or_reparse_point(temporary_metadata):
                raise MCPLaunchIdentityError(
                    "snapshot_cleanup_failed",
                    "MCP private snapshot cleanup refused a linked temporary path.",
                )
            temporary.unlink()
    _assert_private_snapshot_permissions(destination.parent, recursive=True)


def _copy_private_launch_file(
    source: Path,
    destination: Path,
    *,
    executable: bool,
) -> None:
    source_before = source.lstat()
    if is_link_or_reparse_point(source_before) or not stat.S_ISREG(source_before.st_mode):
        raise MCPLaunchIdentityError(
            "artifact_not_regular",
            "MCP launch artifact must be a regular non-linked file.",
        )
    _assert_private_snapshot_permissions(destination.parent)
    read_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_CLOEXEC"):
        read_flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        read_flags |= os.O_NOFOLLOW
    write_flags = (
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    )
    if hasattr(os, "O_CLOEXEC"):
        write_flags |= os.O_CLOEXEC
    source_fd = os.open(source, read_flags)
    try:
        destination_fd = os.open(destination, write_flags, 0o700 if executable else 0o600)
        try:
            while chunk := os.read(source_fd, 1024 * 1024):
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    source_after = source.lstat()
    destination_after = destination.lstat()
    if (
        is_link_or_reparse_point(source_after)
        or is_link_or_reparse_point(destination_after)
        or not os.path.samestat(source_before, source_after)
        or not stat.S_ISREG(destination_after.st_mode)
    ):
        raise MCPLaunchIdentityError(
            "artifact_changed_during_snapshot",
            "MCP launch artifact changed while its private snapshot was created.",
        )


def _assert_private_snapshot_permissions(
    root: Path,
    *,
    recursive: bool = False,
) -> None:
    _assert_private_snapshot_directory(root)
    if not recursive:
        return

    pending = [root]
    while pending:
        current = pending.pop()
        before = _snapshot_directory_metadata(current)
        try:
            with os.scandir(current) as entries:
                children = sorted(
                    (current / entry.name for entry in entries),
                    key=lambda path: path.name,
                )
        except OSError as exc:
            raise MCPLaunchIdentityError(
                "snapshot_unavailable",
                "MCP private snapshot permissions could not be inspected.",
            ) from exc
        after = _snapshot_directory_metadata(current)
        if not os.path.samestat(before, after):
            raise MCPLaunchIdentityError(
                "snapshot_unavailable",
                "MCP private snapshot changed during permission validation.",
            )
        for child in children:
            try:
                metadata = child.lstat()
            except OSError as exc:
                raise MCPLaunchIdentityError(
                    "snapshot_unavailable",
                    "MCP private snapshot changed during permission validation.",
                ) from exc
            if is_link_or_reparse_point(metadata):
                raise MCPLaunchIdentityError(
                    "snapshot_unavailable",
                    "MCP private snapshot contains a link or reparse point.",
                )
            if stat.S_ISDIR(metadata.st_mode):
                _assert_private_snapshot_directory(child)
                pending.append(child)
            elif not stat.S_ISREG(metadata.st_mode):
                raise MCPLaunchIdentityError(
                    "snapshot_unavailable",
                    "MCP private snapshot contains a non-regular entry.",
                )


def _assert_private_snapshot_directory(root: Path) -> None:
    _reject_windows_reparse_ancestors(root, label="private snapshot")
    metadata = _snapshot_directory_metadata(root)
    if _uses_windows_snapshot_acls():
        try:
            validate_owner_private_directory(root)
        except PrivateDirectoryError as exc:
            _raise_private_snapshot_boundary_error(exc)
        return
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise MCPLaunchIdentityError(
            "snapshot_permissions",
            "MCP private snapshot permissions are not owner-only.",
        )


def _snapshot_directory_metadata(root: Path) -> os.stat_result:
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "snapshot_unavailable",
            "MCP private snapshot is unavailable.",
        ) from exc
    if is_link_or_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise MCPLaunchIdentityError(
            "snapshot_unavailable",
            "MCP private snapshot is unavailable.",
        )
    return metadata


def _uses_windows_snapshot_acls() -> bool:
    return os.name == "nt"


def _launch_artifact_index(artifact_kind: str) -> int:
    return 1 if "deno_module" in artifact_kind else 0


def _launch_file_has_shebang(path: Path) -> bool:
    path_before = path.lstat()
    if is_link_or_reparse_point(path_before) or not stat.S_ISREG(path_before.st_mode):
        raise MCPLaunchIdentityError(
            "artifact_not_regular",
            "MCP stdio executable must be a regular non-linked file.",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "artifact_unreadable",
            "MCP stdio executable could not be opened safely.",
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MCPLaunchIdentityError(
                "artifact_not_regular",
                "MCP stdio executable must be a regular file.",
            )
        prefix = os.read(descriptor, 2)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MCPLaunchIdentityError(
                "artifact_changed_during_validation",
                "MCP stdio executable changed while it was being validated.",
            )
        path_after = path.lstat()
        if is_link_or_reparse_point(path_after) or not os.path.samestat(
            path_before, path_after
        ):
            raise MCPLaunchIdentityError(
                "artifact_changed_during_validation",
                "MCP stdio executable changed while it was being validated.",
            )
        return prefix == b"#!"
    finally:
        os.close(descriptor)


def _hash_launch_file(path: Path, *, label: str) -> str:
    try:
        path_before = path.lstat()
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "artifact_unreadable",
            f"MCP stdio {label} could not be opened safely.",
        ) from exc
    if is_link_or_reparse_point(path_before) or not stat.S_ISREG(path_before.st_mode):
        raise MCPLaunchIdentityError(
            "artifact_not_regular",
            f"MCP stdio {label} must be a regular non-linked file.",
        )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise MCPLaunchIdentityError(
            "artifact_unreadable",
            f"MCP stdio {label} could not be opened safely.",
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise MCPLaunchIdentityError(
                "artifact_not_regular",
                f"MCP stdio {label} must be a regular file.",
            )
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise MCPLaunchIdentityError(
                "artifact_changed_during_validation",
                f"MCP stdio {label} changed while it was being validated.",
            )
        path_after = path.lstat()
        if is_link_or_reparse_point(path_after) or not os.path.samestat(
            path_before, path_after
        ):
            raise MCPLaunchIdentityError(
                "artifact_changed_during_validation",
                f"MCP stdio {label} changed while it was being validated.",
            )
        return "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def _launch_resource_digest(resource: dict[str, object]) -> str:
    resolved_digest = resource.get("digest")
    if resource.get("status") == "resolved" and isinstance(resolved_digest, str):
        return resolved_digest
    canonical = json.dumps(
        resource,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clear_connect_approval(vetting: dict[str, Any]) -> None:
    for key in (
        "connect_approved",
        "connect_approved_at",
        "connect_approved_command_hash",
        "connect_approved_launch_digest",
        "stdio_launch_snapshot",
    ):
        vetting.pop(key, None)


def _is_python_command(command_name: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:python|pythonw|pypy|py|pyw)(?:\d+(?:\.\d+)*)?",
            command_name,
        )
    )


def _path_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _path_exists_or_indeterminate(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def _keyring_metadata_has_material(path: Path) -> bool:
    """Inspect keyring metadata without resolving or enumerating raw values."""

    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        return True
    descriptor = -1
    try:
        if metadata.st_size > 4 * 1024 * 1024:
            return True
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if (
            (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino)
            or not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size > 4 * 1024 * 1024
        ):
            return True
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = -1
            payload = json.loads(handle.read(4 * 1024 * 1024 + 1))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict):
        return True
    return bool(payload.get("secrets", {})) or bool(
        payload.get("keyring_pending_cleanup", {})
    )


def _repair_trust_material_exists_or_indeterminate(workspace: Path) -> bool:
    root = workspace / ".nest"
    for name in (
        "repair_receipt_signing.key",
        ".repair_receipt_signing.key.tmp",
        "repair_receipt_signing.v2.key",
        ".repair_receipt_signing.v2.key.tmp",
    ):
        if _path_exists_or_indeterminate(root / name):
            return True
    for directory_name in ("repair_validations", "repair_reviews"):
        try:
            with os.scandir(root / directory_name) as entries:
                if any(True for _entry in entries):
                    return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _preserve_matching_connect_approval(
    next_row: dict[str, Any],
    current_row: dict[str, Any],
) -> None:
    next_vetting = dict(next_row.get("vetting", {}) or {})
    current_vetting = dict(current_row.get("vetting", {}) or {})
    command_hash = next_vetting.get("stdio_command_hash")
    launch_digest = next_vetting.get("stdio_launch_digest")
    if (
        not isinstance(command_hash, str)
        or not isinstance(launch_digest, str)
        or current_vetting.get("stdio_command_hash") != command_hash
        or current_vetting.get("stdio_launch_digest") != launch_digest
        or current_vetting.get("connect_approved_command_hash") != command_hash
        or current_vetting.get("connect_approved_launch_digest") != launch_digest
        or not bool(current_vetting.get("connect_approved"))
    ):
        return
    for key in (
        "connect_approved",
        "connect_approved_at",
        "connect_approved_command_hash",
        "connect_approved_launch_digest",
        "stdio_launch_snapshot",
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
    content = (
        result.get("content")
        if isinstance(result, dict)
        else getattr(result, "content", None)
    )
    if isinstance(content, list):
        parts = []
        for item in content:
            text = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            if text is not None:
                parts.append(str(text))
            else:
                parts.append(str(item))
        rendered = "\n".join(parts)
    elif content is not None:
        rendered = str(content)
    else:
        rendered = json.dumps(result, default=str)
    is_error = (
        bool(result.get("isError") or result.get("is_error"))
        if isinstance(result, dict)
        else bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    )
    if is_error:
        raise MCPRemoteToolError(rendered or "MCP server reported a tool error.")
    return rendered
