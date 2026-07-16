from __future__ import annotations

from typing import Any

from .mcp_views import mcp_public, mcp_result_public
from .server_models import MCPServerRequest, ToolInvokeRequest


def register_mcp_routes(
    app: Any,
    *,
    http_exception: Any,
    state: Any,
    mcp: Any,
    runs: Any,
    secret_broker: Any,
) -> None:
    @app.get("/api/mcp/servers")  # type: ignore[untyped-decorator]
    def list_mcp_servers() -> list[dict[str, object]]:
        return [mcp_public(server, secret_broker) for server in mcp.list_servers()]

    @app.get("/api/mcp/servers/{server_id}")  # type: ignore[untyped-decorator]
    def get_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp_public(state.get_mcp_server(server_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers")  # type: ignore[untyped-decorator]
    def add_mcp_server(request: MCPServerRequest) -> dict[str, object]:
        try:
            payload = request.model_dump(exclude_unset=True)
            # Newly configured external processes/endpoints require an explicit
            # owner enable action before they can connect.
            payload["enabled"] = False
            return mcp_public(mcp.add_server(payload), secret_broker)
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.put("/api/mcp/servers/{server_id}")  # type: ignore[untyped-decorator]
    def update_mcp_server(server_id: str, request: MCPServerRequest) -> dict[str, object]:
        payload = request.model_dump(exclude_unset=True)
        # Enablement is an audited, revisioned policy decision. Configuration
        # edits must not provide a second path that can bypass the capability
        # control plane.
        payload.pop("enabled", None)
        payload["id"] = server_id
        try:
            return mcp_public(mcp.add_server(payload), secret_broker)
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/mcp/servers/{server_id}")  # type: ignore[untyped-decorator]
    def delete_mcp_server(server_id: str) -> dict[str, bool]:
        try:
            server = state.get_mcp_server(server_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        mcp.delete_server(server_id)
        state.delete_capability_override("mcp_server", server_id, updated_by="owner")
        for tool in server.get("tools", []):
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "")
            if name:
                state.delete_capability_override("tool", name, updated_by="owner")
        return {"ok": True}

    @app.post("/api/mcp/servers/{server_id}/connect")  # type: ignore[untyped-decorator]
    def connect_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp_result_public(mcp.connect_server(server_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/approve-connect")  # type: ignore[untyped-decorator]
    def approve_mcp_server_connect(server_id: str) -> dict[str, object]:
        try:
            return mcp_public(mcp.approve_server_connect(server_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/disconnect")  # type: ignore[untyped-decorator]
    def disconnect_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp_result_public(mcp.disconnect_server(server_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/restart")  # type: ignore[untyped-decorator]
    def restart_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp_result_public(mcp.restart_server(server_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.get("/api/mcp/servers/{server_id}/health")  # type: ignore[untyped-decorator]
    def mcp_server_health(server_id: str) -> dict[str, object]:
        try:
            return mcp_result_public(mcp.server_health(server_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=409, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/sync")  # type: ignore[untyped-decorator]
    def sync_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp_public(mcp.sync_server(server_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/test")  # type: ignore[untyped-decorator]
    def test_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp_result_public(mcp.test_server(server_id), secret_broker)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/tools/{tool_name}/invoke")  # type: ignore[untyped-decorator]
    def invoke_mcp_tool(
        server_id: str,
        tool_name: str,
        request: ToolInvokeRequest,
    ) -> dict[str, object]:
        try:
            state.get_mcp_server(server_id)
            registered_name = (
                tool_name if tool_name.startswith("mcp.") else f"mcp.{server_id}.{tool_name}"
            )
            execution = runs.invoke_tool(
                tool_name=registered_name,
                arguments=request.arguments,
                session_id=request.session_id,
                run_id=request.run_id,
            )
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        return {
            "tool": execution.call.name,
            "tool_call_id": execution.call.id,
            "success": execution.success,
            "content": execution.content,
            "data": execution.data,
            "error": execution.error,
        }
