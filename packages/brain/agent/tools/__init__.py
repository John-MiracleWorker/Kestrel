"""
Tool Registry — manages tool definitions and dispatches execution.

Tools are registered at startup and described to the LLM via OpenAI-style
function schemas. The registry handles execution dispatch, timeout enforcement,
and result formatting.
"""

import logging
import time
from typing import Callable, Dict, Optional

from agent.types import (
    RiskLevel,
    ToolCall,
    ToolDefinition,
    ToolResult,
)

logger = logging.getLogger("brain.agent.tools")


class ToolRegistry:
    """
    Central registry for all agent tools.

    Tools are registered with definitions (schema + metadata) and handler
    functions. The registry mediates between the agent loop and concrete
    tool implementations.
    """

    def __init__(self):
        self._definitions: Dict[str, ToolDefinition] = {}
        self._handlers: Dict[str, Callable] = {}

    def register(
        self,
        definition: ToolDefinition,
        handler: Callable,
    ) -> None:
        """Register a tool with its definition and async handler."""
        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler
        logger.info(f"Tool registered: {definition.name} [{definition.risk_level.value}]")

    def list_tools(self) -> list[ToolDefinition]:
        """Return all registered tool definitions."""
        return list(self._definitions.values())

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Get a tool definition by name."""
        return self._definitions.get(name)

    def get_risk_level(self, name: str) -> RiskLevel:
        """Get the risk level for a tool."""
        tool = self._definitions.get(name)
        return tool.risk_level if tool else RiskLevel.HIGH  # Unknown = HIGH

    def filter(self, allowed_names: list[str]) -> "ToolRegistry":
        """Return a new registry containing only the named tools."""
        filtered = ToolRegistry()
        for name in allowed_names:
            if name in self._definitions:
                filtered._definitions[name] = self._definitions[name]
                filtered._handlers[name] = self._handlers[name]
        return filtered

    async def execute(self, tool_call: ToolCall, context: dict = None) -> ToolResult:
        """
        Execute a tool call and return the result.
        Handles timeout enforcement and error wrapping.
        
        Args:
            tool_call: The tool call to execute.
            context: Optional context dict (e.g. workspace_id) to inject
                     into the handler kwargs if the handler accepts them.
        """
        handler = self._handlers.get(tool_call.name)
        if not handler:
            return ToolResult(
                tool_call_id=tool_call.id,
                success=False,
                error=f"Unknown tool: {tool_call.name}",
            )

        definition = self._definitions[tool_call.name]
        start = time.monotonic()

        # Merge context into args if handler accepts those kwargs
        merged_args = dict(tool_call.arguments)
        if context:
            import inspect
            sig = inspect.signature(handler)
            for key, value in context.items():
                if key in sig.parameters and key not in merged_args:
                    merged_args[key] = value

        try:
            import asyncio
            result = await asyncio.wait_for(
                handler(**merged_args),
                timeout=definition.timeout_seconds,
            )

            elapsed_ms = int((time.monotonic() - start) * 1000)

            # Normalize result to string
            if isinstance(result, dict):
                import json
                output = json.dumps(result, indent=2, default=str)
            elif isinstance(result, (list, tuple)):
                import json
                output = json.dumps(result, indent=2, default=str)
            else:
                output = str(result)

            # Truncate very long outputs
            if len(output) > 10_000:
                output = output[:9_900] + f"\n\n... (truncated, {len(output)} total chars)"

            return ToolResult(
                tool_call_id=tool_call.id,
                success=True,
                output=output,
                execution_time_ms=elapsed_ms,
            )

        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(f"Tool {tool_call.name} timed out after {definition.timeout_seconds}s")
            return ToolResult(
                tool_call_id=tool_call.id,
                success=False,
                error=f"Tool timed out after {definition.timeout_seconds} seconds",
                execution_time_ms=elapsed_ms,
            )

        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(f"Tool {tool_call.name} error: {e}", exc_info=True)
            return ToolResult(
                tool_call_id=tool_call.id,
                success=False,
                error=str(e),
                execution_time_ms=elapsed_ms,
            )


def build_tool_registry(hands_client=None, vector_store=None, pool=None) -> ToolRegistry:
    """
    Build the default tool registry with all built-in tools.
    Called during server startup.

    Args:
        hands_client: Optional Hands gRPC client for sandboxed code execution.
            If provided, code execution routes through the Hands service.
            If None, code execution will fail safely with an error message.
        vector_store: Optional VectorStore for memory tools.
    """
    registry = ToolRegistry()

    # Import and register all built-in tools
    from agent.tools.code import register_code_tools
    from agent.tools.web import register_web_tools
    from agent.tools.files import register_file_tools
    from agent.tools.host_files import register_host_file_tools
    from agent.tools.data import register_data_tools
    from agent.tools.memory import register_memory_tools
    from agent.tools.human import register_human_tools
    from agent.tools.moltbook import register_moltbook_tools
    from agent.tools.schedule import register_schedule_tools

    register_code_tools(registry, hands_client=hands_client)
    register_web_tools(registry)
    register_file_tools(registry)
    register_host_file_tools(registry, vector_store=vector_store)
    register_data_tools(registry)
    register_memory_tools(registry, vector_store=vector_store)
    register_human_tools(registry)
    register_moltbook_tools(registry)
    register_schedule_tools(registry)

    # MCP discovery + management tools
    from agent.tools.mcp import register_mcp_tools
    register_mcp_tools(registry, pool=pool)

    # Self-improvement + git tools
    from agent.tools.git import register_git_tools
    from agent.tools.self_improve import register_self_improve_tools
    register_git_tools(registry)
    register_self_improve_tools(registry)

    # Multi-agent delegation tool
    from agent.tools.delegate import DELEGATE_TOOL
    from agent.coordinator import Coordinator

    # Coordinator is lazily initialized — needs the agent loop reference
    # which is set after the registry is built
    registry._coordinator = None
    registry._current_task = None

    async def delegate_handler(goal: str, specialist: str = "researcher") -> dict:
        """Delegate a subtask to a specialist sub-agent."""
        coordinator = getattr(registry, '_coordinator', None)
        current_task = getattr(registry, '_current_task', None)

        if not coordinator or not current_task:
            return {
                "success": False,
                "error": "Delegation not available — agent loop not initialized for this session.",
                "hint": "Delegation works during autonomous task mode (!goal or /task).",
            }

        if not goal:
            return {"success": False, "error": "Goal is required"}

        try:
            result = await coordinator.delegate(
                parent_task=current_task,
                goal=goal,
                specialist_type=specialist,
            )
            return {"success": True, "output": result}
        except Exception as e:
            return {"success": False, "error": f"Delegation failed: {e}"}

    registry.register(definition=DELEGATE_TOOL, handler=delegate_handler)

    logger.info(f"Tool registry built: {len(registry._definitions)} tools")
    return registry
