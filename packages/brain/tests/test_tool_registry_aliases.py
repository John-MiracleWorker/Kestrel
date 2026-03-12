import asyncio

from agent.tools import ToolRegistry
from agent.types import RiskLevel, ToolCall, ToolDefinition


async def _echo(**kwargs):
    return {"ok": True, "kwargs": kwargs}


async def _runtime_failure(**kwargs):
    return {
        "success": False,
        "error": "native execution denied",
        "runtime_class": "hybrid_native_fallback",
        "risk_class": "high",
        "fallback_used": True,
        "fallback_from": "sandboxed_docker",
        "fallback_to": "hybrid_native_fallback",
        "action_events": [{"status": "denied"}],
    }


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


def test_execute_preserves_structured_failure_metadata():
    registry = ToolRegistry()
    registry.register(
        ToolDefinition(
            name="code_execute",
            description="code_execute",
            parameters={"type": "object", "properties": {}},
            risk_level=RiskLevel.MEDIUM,
            category="test",
        ),
        _runtime_failure,
    )

    result = asyncio.run(
        registry.execute(ToolCall(id="1", name="code_execute", arguments={"language": "python"}))
    )

    assert result.success is False
    assert result.error == "native execution denied"
    assert result.metadata["execution"]["runtime_class"] == "hybrid_native_fallback"
    assert result.metadata["execution"]["fallback_from"] == "sandboxed_docker"
