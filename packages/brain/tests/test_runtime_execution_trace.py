import json

import pytest

from agent.runtime.native_runtime import NativeRuntime


@pytest.mark.asyncio
async def test_native_runtime_emits_canonical_execution_trace_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_AUDIT_LOG_DIR", str(tmp_path))

    runtime = NativeRuntime()
    result = await runtime.execute(
        tool_name="code_execute",
        payload={
            "language": "python",
            "code": "print('hello from native')",
            "workspace_id": "ws-1",
            "user_id": "user-1",
        },
    )

    assert result["success"] is True
    assert "hello from native" in result["output"]
    assert result["runtime_class"] == "native_host"
    assert result["risk_class"] == "medium"
    assert result["fallback_used"] is False
    assert len(result["action_events"]) == 2
    assert result["action_events"][-1]["metadata"]["runtime_class"] == "native_host"
    assert result["action_events"][-1]["metadata"]["risk_class"] == "medium"
    assert result["action_event_json"]

    audit_files = list(tmp_path.glob("audit-*.jsonl"))
    assert len(audit_files) == 1

    lines = audit_files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["workspace_id"] == "ws-1"
    assert entry["user_id"] == "user-1"
    assert entry["runtime_class"] == "native_host"
    assert entry["risk_class"] == "medium"
    assert len(entry["action_events"]) == 2
