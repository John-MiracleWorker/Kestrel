from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from nested_memvid_agent.mcp_manager import MCPManager
from nested_memvid_agent.state_store import AgentStateStore

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_MCP_INTEGRATION") != "1",
    reason="Set RUN_MCP_INTEGRATION=1 to run live stdio MCP integration tests.",
)


def test_live_stdio_mcp_discovery_and_invoke(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "stdio_mcp_server.py"
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state, timeout_seconds=10)
    manager.add_server(
        {
            "id": "stdio_fixture",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(fixture)],
        }
    )

    try:
        connected = manager.connect_server("stdio_fixture")
        assert connected["ok"] is True
        tools = connected["server"]["tools"]
        assert {tool["remote_name"] for tool in tools} == {"echo"}

        result = manager.invoke_tool("stdio_fixture", "echo", {"message": "hello"})
        assert result.success is True
        assert "echo:hello" in result.content
    finally:
        manager.shutdown()
