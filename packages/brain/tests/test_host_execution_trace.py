import json

import pytest

from agent.tools.host_execution import execute_host_shell


@pytest.mark.asyncio
async def test_host_shell_returns_canonical_trace_for_read_only_command(tmp_path, monkeypatch):
    monkeypatch.setenv("KESTREL_AUDIT_LOG_DIR", str(tmp_path))

    result = await execute_host_shell(
        "echo hello-from-host",
        workspace_id="ws-host",
        user_id="user-host",
    )

    assert result["success"] is True
    assert "hello-from-host" in result["output"]
    assert result["runtime_class"] == "native_host"
    assert result["risk_class"] == "high"
    assert len(result["action_events"]) == 2
    assert result["action_events"][-1]["metadata"]["runtime_class"] == "native_host"
    assert result["action_events"][-1]["metadata"]["risk_class"] == "high"

    audit_files = list(tmp_path.glob("audit-*.jsonl"))
    assert len(audit_files) == 1

    entry = json.loads(audit_files[0].read_text(encoding="utf-8").splitlines()[0])
    assert entry["workspace_id"] == "ws-host"
    assert entry["user_id"] == "user-host"
    assert entry["runtime_class"] == "native_host"
    assert entry["risk_class"] == "high"
