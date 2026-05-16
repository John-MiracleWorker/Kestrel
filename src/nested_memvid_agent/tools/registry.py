from __future__ import annotations

from ..runtime_models import ToolCall, ToolExecution, ToolSpec
from .base import AgentTool, ToolContext


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        if tool.spec.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.spec.name}")
        self._tools[tool.spec.name] = tool

    def specs(self) -> list[ToolSpec]:
        return [tool.spec for tool in self._tools.values()]

    def execute(self, call: ToolCall, context: ToolContext) -> ToolExecution:
        if not isinstance(call.arguments, dict):
            return ToolExecution(
                call=call,
                success=False,
                content=f"Tool {call.name} arguments must be a JSON object.",
                error="invalid_tool_arguments",
            )
        tool = self._tools.get(call.name)
        if tool is None:
            return ToolExecution(
                call=call,
                success=False,
                content=f"Unknown tool: {call.name}",
                error="unknown_tool",
            )
        if tool.spec.requires_approval and context.config.require_approval_for_high_risk_tools:
            if call.id in context.approved_tool_call_ids:
                return tool.run(call.arguments, context)
            enablement_attr = _ENABLEMENT_BY_TOOL.get(tool.spec.name)
            if enablement_attr and bool(getattr(context.config, enablement_attr)):
                return tool.run(call.arguments, context)
            if context.approval_handler is not None:
                return context.approval_handler(call, tool.spec, context)
            return ToolExecution(
                call=call,
                success=False,
                content=f"Tool {call.name} requires approval or config enablement.",
                error="approval_required",
            )
        arguments = dict(call.arguments)
        if getattr(tool, "needs_call_id", False):
            arguments.setdefault("_tool_call_id", call.id)
        return tool.run(arguments, context)


_ENABLEMENT_BY_TOOL = {
    "file.write": "allow_file_write",
    "patch.apply": "allow_file_write",
    "shell.run": "allow_shell",
    "test.run": "allow_shell",
    "lint.run": "allow_shell",
    "codex.exec": "allow_codex_cli",
}
