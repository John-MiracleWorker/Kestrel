from __future__ import annotations

from typing import Any

from .server_models import WebFetchRequest, WebSearchRequest
from .server_support import execution_response


def register_web_routes(app: Any, *, runs: Any) -> None:
    @app.post("/api/web/search")  # type: ignore[untyped-decorator]
    def search_web(request: WebSearchRequest) -> dict[str, object]:
        arguments: dict[str, object] = {"query": request.query}
        if request.max_results is not None:
            arguments["max_results"] = request.max_results
        execution = runs.invoke_tool(tool_name="web.search", arguments=arguments, session_id="api")
        return execution_response(execution)

    @app.post("/api/web/fetch")  # type: ignore[untyped-decorator]
    def fetch_web(request: WebFetchRequest) -> dict[str, object]:
        arguments: dict[str, object] = {"url": request.url}
        if request.max_bytes is not None:
            arguments["max_bytes"] = request.max_bytes
        execution = runs.invoke_tool(tool_name="web.fetch", arguments=arguments, session_id="api")
        return execution_response(execution)
