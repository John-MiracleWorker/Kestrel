from fastapi import FastAPI
from fastapi.testclient import TestClient

from nested_memvid_agent.runtime_models import ToolCall, ToolExecution
from nested_memvid_agent.server_diagnosis_routes import register_diagnosis_routes


class _FakeRuns:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def invoke_tool(self, **kwargs: object) -> ToolExecution:
        self.calls.append(kwargs)
        tool_name = str(kwargs["tool_name"])
        return ToolExecution(
            call=ToolCall(name=tool_name, arguments=dict(kwargs["arguments"])),  # type: ignore[arg-type]
            success=True,
            content='{"ok": true}',
            data={"ok": True},
        )


def test_diagnosis_routes_invoke_tools_with_api_defaults() -> None:
    app = FastAPI()
    runs = _FakeRuns()
    register_diagnosis_routes(app, runs=runs)
    client = TestClient(app)

    classify = client.post("/api/diagnosis/classify", json={"failure_text": "pytest failed"})
    recall = client.post("/api/diagnosis/recall", json={"failure_text": "timeout", "k": 3})

    assert classify.status_code == 200
    assert classify.json() == {"ok": True}
    assert recall.status_code == 200
    assert recall.json() == {"ok": True}
    assert runs.calls == [
        {
            "tool_name": "diagnosis.classify",
            "arguments": {"failure_text": "pytest failed", "source": "api"},
            "session_id": "api",
        },
        {
            "tool_name": "diagnosis.recall",
            "arguments": {"failure_text": "timeout", "source": "api", "k": 3},
            "session_id": "api",
        },
    ]
