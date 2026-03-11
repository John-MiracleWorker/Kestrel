import asyncio

from agent.tools import ToolRegistry
from agent.types import RiskLevel, ToolCall, ToolDefinition


async def _echo(**kwargs):
    return {"ok": True, "kwargs": kwargs}


def _register(registry: ToolRegistry, name: str) -> None:
    registry.register(
        ToolDefinition(
            name=name,
            description=name,
            parameters={"type": "object", "properties": {}},
            risk_level=RiskLevel.LOW,
            category="test",
        ),
        _echo,
    )


def test_get_tool_resolves_compact_alias():
    registry = ToolRegistry()
    _register(registry, "system_health")

    tool = registry.get_tool("systemhealth")

    assert tool is not None
    assert tool.name == "system_health"


def test_execute_resolves_compact_alias():
    registry = ToolRegistry()
    _register(registry, "mcp_call")

    result = asyncio.run(
        registry.execute(ToolCall(id="1", name="mcpcall", arguments={"server_name": "x"}))
    )

    assert result.success is True
    assert result.error == ""
