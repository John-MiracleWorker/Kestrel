from __future__ import annotations

from typing import Any

from .server_models import ToolInvokeRequest
from .server_support import execution_response
from .tools.registry import tool_enablement_status


def tool_invoke_response(runs: Any, tool_name: str, request: ToolInvokeRequest) -> dict[str, object]:
    execution = runs.invoke_tool(
        tool_name=tool_name,
        arguments=request.arguments,
        session_id=request.session_id,
        run_id=request.run_id,
    )
    return execution_response(execution)


def register_tool_routes(app: Any, *, runs: Any) -> None:
    @app.get("/api/tools")  # type: ignore[untyped-decorator]
    def list_tools() -> list[dict[str, object]]:
        config = getattr(runs, "config", None)
        tools = []
        for spec in runs.build_registry().specs():
            payload = spec.to_public_dict()
            payload.update(tool_enablement_status(spec, config))
            tools.append(payload)
        return tools

    @app.post("/api/tools/{tool_name}/invoke")  # type: ignore[untyped-decorator]
    def invoke_tool(tool_name: str, request: ToolInvokeRequest) -> dict[str, object]:
        return tool_invoke_response(runs, tool_name, request)
