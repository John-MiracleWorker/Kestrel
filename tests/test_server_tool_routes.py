from fastapi import FastAPI
from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.server_models import ToolInvokeRequest
from nested_memvid_agent.server_tool_routes import register_tool_routes, tool_invoke_response


class _FakeRuns:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.config = AgentConfig()

    def build_registry(self) -> object:
        class _Registry:
            def specs(self) -> list[ToolSpec]:
                return [
                    ToolSpec(
                        name="echo",
                        description="Echo input",
                        parameters={"type": "object"},
                    )
                ]

        return _Registry()

    def invoke_tool(self, **kwargs: object) -> ToolExecution:
        self.calls.append(kwargs)
        return ToolExecution(
            call=ToolCall(
                name=str(kwargs["tool_name"]),
                arguments=dict(kwargs["arguments"]),  # type: ignore[arg-type]
                id="tool_call_1",
            ),
            success=True,
            content="done",
            data={"ok": True},
        )


def test_tool_routes_list_specs_and_invoke_tools() -> None:
    app = FastAPI()
    runs = _FakeRuns()
    register_tool_routes(app, runs=runs)
    client = TestClient(app)

    listed = client.get("/api/tools")
    invoked = client.post(
        "/api/tools/echo/invoke",
        json={"arguments": {"message": "hello"}, "session_id": "session_1", "run_id": "run_1"},
    )

    assert listed.status_code == 200
    assert listed.json() == [
        {
            "name": "echo",
            "description": "Echo input",
            "parameters": {"type": "object"},
            "risk": "low",
            "requires_approval": False,
            "source": "builtin",
            "server_id": None,
            "skill_id": None,
            "capabilities": [],
            "produces_validation": False,
            "aliases": [],
            "enabled": True,
            "enablement_flag": None,
        }
    ]
    assert invoked.status_code == 200
    assert invoked.json() == {
        "tool": "echo",
        "tool_call_id": "tool_call_1",
        "success": True,
        "content": "done",
        "data": {"ok": True},
        "error": None,
    }
    assert runs.calls == [
        {
            "tool_name": "echo",
            "arguments": {"message": "hello"},
            "session_id": "session_1",
            "run_id": "run_1",
        }
    ]


def test_tool_routes_report_config_enablement_status() -> None:
    class _ShellRuns(_FakeRuns):
        def build_registry(self) -> object:
            class _Registry:
                def specs(self) -> list[ToolSpec]:
                    return [
                        ToolSpec(
                            name="shell.run",
                            description="Run a command",
                            parameters={"type": "object"},
                            risk="high",
                            requires_approval=True,
                        )
                    ]

            return _Registry()

    app = FastAPI()
    runs = _ShellRuns()
    register_tool_routes(app, runs=runs)
    client = TestClient(app)

    disabled = client.get("/api/tools")
    runs.config = AgentConfig(allow_shell=True)
    enabled = client.get("/api/tools")

    assert disabled.status_code == 200
    assert disabled.json()[0]["enablement_flag"] == "allow_shell"
    assert disabled.json()[0]["enabled"] is False
    assert enabled.status_code == 200
    assert enabled.json()[0]["enablement_flag"] == "allow_shell"
    assert enabled.json()[0]["enabled"] is True


def test_tool_invoke_response_matches_route_payload_for_skill_reuse() -> None:
    runs = _FakeRuns()
    request = ToolInvokeRequest(arguments={"topic": "review"}, session_id="api", run_id=None)

    assert tool_invoke_response(runs, "skill.review.run", request) == {
        "tool": "skill.review.run",
        "tool_call_id": "tool_call_1",
        "success": True,
        "content": "done",
        "data": {"ok": True},
        "error": None,
    }
    assert runs.calls == [
        {
            "tool_name": "skill.review.run",
            "arguments": {"topic": "review"},
            "session_id": "api",
            "run_id": None,
        }
    ]
