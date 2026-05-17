from __future__ import annotations

from queue import Empty, Queue
from threading import Thread
from typing import Any

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

    def spec_for(self, name: str) -> ToolSpec | None:
        tool = self._tools.get(name)
        return None if tool is None else tool.spec

    def execute(self, call: ToolCall, context: ToolContext) -> ToolExecution:
        if not isinstance(call.arguments, dict):
            return _failure(
                call,
                content=f"Tool {call.name} arguments must be a JSON object.",
                error="invalid_tool_arguments",
            )
        tool = self._tools.get(call.name)
        if tool is None:
            return _failure(call, content=f"Unknown tool: {call.name}", error="unknown_tool")

        arguments = dict(call.arguments)
        if getattr(tool, "needs_call_id", False):
            arguments.setdefault("_tool_call_id", call.id)

        enabled, disabled_reason = _capability_enabled(tool, context)
        if not enabled:
            return _failure(call, content=disabled_reason, error="tool_disabled")

        if tool.spec.requires_approval and context.config.require_approval_for_high_risk_tools:
            if _is_exact_call_approved(call, arguments, context):
                return _run_tool(tool, call, arguments, context)
            if context.approval_handler is not None:
                return context.approval_handler(call, tool.spec, context)
            return _failure(
                call,
                content=f"Tool {call.name} requires explicit approval for this exact call.",
                error="approval_required",
            )

        return _run_tool(tool, call, arguments, context)


def _capability_enabled(tool: AgentTool, context: ToolContext) -> tuple[bool, str]:
    if tool.spec.source == "skill" and "executable-skill" in tool.spec.capabilities:
        if context.config.allow_executable_skills:
            return True, ""
        return (
            False,
            f"Tool {tool.spec.name} is disabled. Enable allow_executable_skills before requesting approval.",
        )
    enablement_attr = _ENABLEMENT_BY_TOOL.get(tool.spec.name)
    if not enablement_attr:
        return True, ""
    if bool(getattr(context.config, enablement_attr)):
        return True, ""
    return False, f"Tool {tool.spec.name} is disabled. Enable {enablement_attr} before requesting approval."


def tool_enablement_status(spec: ToolSpec, config: Any | None) -> dict[str, Any]:
    enablement_attr = _enablement_attr_for_spec(spec)
    if enablement_attr is None:
        return {"enabled": True, "enablement_flag": None}
    return {
        "enabled": bool(config is not None and getattr(config, enablement_attr, False)),
        "enablement_flag": enablement_attr,
    }


def _enablement_attr_for_spec(spec: ToolSpec) -> str | None:
    if spec.source == "skill" and "executable-skill" in spec.capabilities:
        return "allow_executable_skills"
    return _ENABLEMENT_BY_TOOL.get(spec.name)


def _is_exact_call_approved(call: ToolCall, arguments: dict[str, Any], context: ToolContext) -> bool:
    if call.id not in context.approved_tool_call_ids:
        return False
    approved_arguments = context.approved_tool_call_arguments
    if approved_arguments is None or call.id not in approved_arguments:
        return False
    return _arguments_match(approved_arguments[call.id], arguments)


def _arguments_match(approved: dict[str, Any], actual: dict[str, Any]) -> bool:
    if approved == actual:
        return True
    public_actual = {key: value for key, value in actual.items() if not str(key).startswith("_")}
    return approved == public_actual


def _run_tool(tool: AgentTool, call: ToolCall, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
    timeout = max(float(getattr(context.config, "tool_timeout_seconds", 30.0)), 0.001)
    results: Queue[ToolExecution] = Queue(maxsize=1)

    def target() -> None:
        try:
            results.put(tool.run(arguments, context))
        except Exception as exc:  # noqa: BLE001 - registry boundary must never crash agent turns
            results.put(_failure(call, content=f"{type(exc).__name__}: {exc}", error="tool_execution_failed"))

    thread = Thread(target=target, daemon=True)
    thread.start()
    try:
        return results.get(timeout=timeout)
    except Empty:
        _cancel_tool(tool, call.id)
        return _failure(
            call,
            content=f"Tool {call.name} timed out after {timeout:g} seconds.",
            error="tool_timeout",
        )


def _failure(call: ToolCall, *, content: str, error: str) -> ToolExecution:
    return ToolExecution(call=call, success=False, content=content, error=error)


def _cancel_tool(tool: AgentTool, call_id: str) -> None:
    try:
        tool.cancel(call_id)
    except Exception:
        return


_ENABLEMENT_BY_TOOL = {
    "file.write": "allow_file_write",
    "patch.apply": "allow_file_write",
    "shell.run": "allow_shell",
    "test.run": "allow_shell",
    "lint.run": "allow_shell",
    "repair.prepare": "allow_file_write",
    "repair.apply_patch": "allow_file_write",
    "repair.validate": "allow_shell",
    "repair.orchestrate_validate": "allow_shell",
    "repair.review": "allow_file_write",
    "repair.rollback": "allow_file_write",
    "codex.exec": "allow_codex_cli",
    "skill.install": "allow_file_write",
    "plugin.install": "allow_plugin_install",
    "git.commit": "allow_git_commit",
    "memory.import": "allow_memory_import",
    "web.search": "allow_web",
    "web.fetch": "allow_web",
    "self.propose_change": "allow_self_modification",
}
