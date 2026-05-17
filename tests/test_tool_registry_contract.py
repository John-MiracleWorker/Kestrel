from __future__ import annotations

from pathlib import Path
from time import sleep

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.registry import ToolRegistry


class ContractSlowTool(AgentTool):
    spec = ToolSpec(
        name="contract.slow",
        description="Sleeps longer than the configured timeout.",
        parameters={"type": "object", "properties": {}},
    )

    def __init__(self) -> None:
        self.cancelled_call_ids: list[str] = []

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        sleep(0.2)
        return ToolExecution(call=ToolCall(name=self.spec.name, arguments=arguments), success=True, content="late")

    def cancel(self, call_id: str) -> None:
        self.cancelled_call_ids.append(call_id)


def test_agent_tool_has_noop_cancel_contract() -> None:
    class MinimalTool(AgentTool):
        spec = ToolSpec(name="minimal", description="Minimal tool.", parameters={"type": "object"})

        def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
            return ToolExecution(call=ToolCall(name=self.spec.name, arguments=arguments), success=True, content="ok")

    MinimalTool().cancel("call-id")


def test_tool_registry_calls_cancel_on_timeout(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = ToolRegistry()
    tool = ContractSlowTool()
    registry.register(tool)

    result = registry.execute(
        ToolCall(name="contract.slow", arguments={}, id="slow-call"),
        ToolContext(memory=memory, config=AgentConfig(tool_timeout_seconds=0.01), workspace=tmp_path),
    )

    assert result.success is False
    assert result.error == "tool_timeout"
    assert tool.cancelled_call_ids == ["slow-call"]
