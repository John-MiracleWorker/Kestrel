from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import nested_memvid_agent.server as server_module
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from nested_memvid_agent.server_web_routes import register_web_routes


class _FakeRuns:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke_tool(self, **kwargs: object) -> ToolExecution:
        self.calls.append(kwargs)
        return ToolExecution(
            call=ToolCall(
                name=str(kwargs["tool_name"]),
                arguments=dict(kwargs["arguments"]),  # type: ignore[arg-type]
                id=f"tool_call_{len(self.calls)}",
            ),
            success=True,
            content="ok",
            data={"result": kwargs["tool_name"]},
        )


def test_web_dist_resolution_prefers_packaged_assets_and_falls_back_to_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_server = tmp_path / "site" / "nested_memvid_agent" / "server.py"
    package_dist = fake_server.parent / "web_dist"
    source_dist = fake_server.parents[2] / "web" / "dist"
    package_dist.mkdir(parents=True)
    source_dist.mkdir(parents=True)
    (package_dist / "index.html").write_text("packaged", encoding="utf-8")
    (source_dist / "index.html").write_text("source", encoding="utf-8")
    monkeypatch.setattr(server_module, "__file__", str(fake_server))

    assert server_module._resolve_web_dist() == package_dist

    (package_dist / "index.html").unlink()
    assert server_module._resolve_web_dist() == source_dist


def test_web_routes_delegate_to_gated_tools_with_optional_limits() -> None:
    app = FastAPI()
    runs = _FakeRuns()
    register_web_routes(app, runs=runs)
    client = TestClient(app)

    search = client.post("/api/web/search", json={"query": "kestrel"})
    fetch = client.post("/api/web/fetch", json={"url": "https://example.test", "max_bytes": 256})

    assert search.status_code == 200
    assert search.json() == {
        "tool": "web.search",
        "tool_call_id": "tool_call_1",
        "success": True,
        "content": "ok",
        "data": {"result": "web.search"},
        "error": None,
    }
    assert fetch.status_code == 200
    assert fetch.json() == {
        "tool": "web.fetch",
        "tool_call_id": "tool_call_2",
        "success": True,
        "content": "ok",
        "data": {"result": "web.fetch"},
        "error": None,
    }
    assert runs.calls == [
        {
            "tool_name": "web.search",
            "arguments": {"query": "kestrel"},
            "session_id": "api",
        },
        {
            "tool_name": "web.fetch",
            "arguments": {"url": "https://example.test", "max_bytes": 256},
            "session_id": "api",
        },
    ]
