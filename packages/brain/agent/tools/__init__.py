"""
Tool Registry — manages tool definitions and dispatches execution.

Tools are registered at startup and described to the LLM via OpenAI-style
function schemas. The registry handles execution dispatch, timeout enforcement,
and result formatting.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Callable, Dict, Optional

from agent.runtime import set_active_runtime
from agent.types import (
    RiskLevel,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent.task_profiles import allowed_tool_names_for_bundles

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
        self._feature_mode: str = "core"
        self._enabled_bundles: tuple[str, ...] = ()
        self._task_profile: str | None = None

    def register(
        self,
        definition: ToolDefinition,
        handler: Callable,
    ) -> None:
        """Register a tool with its definition and async handler."""
        self._definitions[definition.name] = definition
        self._handlers[definition.name] = handler
        logger.info(f"Tool registered: {definition.name} [{definition.risk_level.value}]")

    @staticmethod
    def _normalize_tool_name(name: str) -> str:
        """Normalize a tool name for resilient lookup across minor format variants."""
        return re.sub(r"[^a-z0-9]", "", (name or "").lower())

    def _resolve_tool_name(self, name: str) -> str | None:
        """Resolve incoming tool names to registered canonical names.

        Prefers exact matches, then falls back to normalized matching
        (e.g. mcp_call -> mcpcall, system_health -> systemhealth).
        """
        if name in self._definitions:
            return name

        normalized_target = self._normalize_tool_name(name)
        if not normalized_target:
            return None

        matches = [
            registered_name
            for registered_name in self._definitions
            if self._normalize_tool_name(registered_name) == normalized_target
        ]
        if len(matches) == 1:
            logger.info("Resolved tool alias '%s' -> '%s'", name, matches[0])
            return matches[0]
        return None

    def list_tools(self) -> list[ToolDefinition]:
        """Return all registered tool definitions."""
        return list(self._definitions.values())

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        """Get a tool definition by name."""
        resolved_name = self._resolve_tool_name(name)
        return self._definitions.get(resolved_name) if resolved_name else None

    def get_risk_level(self, name: str) -> RiskLevel:
        """Get the risk level for a tool."""
        resolved_name = self._resolve_tool_name(name)
        tool = self._definitions.get(resolved_name) if resolved_name else None
        return tool.risk_level if tool else RiskLevel.HIGH  # Unknown = HIGH

    def filter(self, allowed_names: list[str]) -> "ToolRegistry":
        """Return a new registry containing only the named tools."""
        filtered = ToolRegistry()
        for name in allowed_names:
            if name in self._definitions:
                filtered._definitions[name] = self._definitions[name]
                filtered._handlers[name] = self._handlers[name]
        filtered._feature_mode = self._feature_mode
        filtered._enabled_bundles = self._enabled_bundles
        filtered._task_profile = self._task_profile
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
        resolved_name = self._resolve_tool_name(tool_call.name)
        handler = self._handlers.get(resolved_name) if resolved_name else None
        if not handler:
            return ToolResult(
                tool_call_id=tool_call.id,
                success=False,
                error=f"Unknown tool: {tool_call.name}",
            )

        definition = self._definitions[resolved_name]
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
            
            # --- CACHE CHECK ---
            cache_key = None
            if definition.cache_ttl_seconds > 0:
                try:
                    from db import get_redis
                    import hashlib
                    import json
                    redis_client = await get_redis()
                    
                    # Create deterministic cache key (workspace-scoped to prevent cross-workspace leaks)
                    args_json = json.dumps(merged_args, sort_keys=True)
                    ws_id = merged_args.get("workspace_id", "global")
                    key_base = f"{resolved_name}:{ws_id}:{args_json}"
                    cache_key = f"tool_cache:{hashlib.sha256(key_base.encode()).hexdigest()}"
                    
                    cached_data = await redis_client.get(cache_key)
                    if cached_data:
                        logger.debug(f"Cache hit for tool {resolved_name}")
                        parsed = json.loads(cached_data)
                        return ToolResult(
                            tool_call_id=tool_call.id,
                            success=parsed.get("success", True),
                            output=parsed.get("output", ""),
                            error=parsed.get("error", ""),
                            execution_time_ms=0,
                        )
                except Exception as e:
                    logger.warning(f"Failed to check tool cache for {resolved_name}: {e}")
            # -------------------

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

            # Smart truncation: preserve head + tail to keep error messages
            # and final results that are typically at the end of the output
            if len(output) > 10_000:
                head_size = 4_000
                tail_size = 4_000
                omitted = len(output) - head_size - tail_size
                output = (
                    output[:head_size]
                    + f"\n\n... ({omitted:,} chars omitted from middle, {len(output):,} total) ...\n\n"
                    + output[-tail_size:]
                )

            tool_result = ToolResult(
                tool_call_id=tool_call.id,
                success=True,
                output=output,
                execution_time_ms=elapsed_ms,
            )
            
            # --- CACHE STORE ---
            if cache_key and definition.cache_ttl_seconds > 0:
                try:
                    import json
                    cache_payload = {
                        "success": True,
                        "output": output,
                        "error": "",
                    }
                    await redis_client.setex(
                        cache_key,
                        definition.cache_ttl_seconds,
                        json.dumps(cache_payload)
                    )
                except Exception as e:
                    logger.warning(f"Failed to set tool cache for {resolved_name}: {e}")
            # -------------------
            
            return tool_result

        except asyncio.TimeoutError:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.warning(f"Tool {resolved_name} timed out after {definition.timeout_seconds}s")
            return ToolResult(
                tool_call_id=tool_call.id,
                success=False,
                error=f"Tool timed out after {definition.timeout_seconds} seconds",
                execution_time_ms=elapsed_ms,
            )

        except Exception as e:
            elapsed_ms = int((time.monotonic() - start) * 1000)
            logger.error(f"Tool {resolved_name} error: {e}", exc_info=True)
            return ToolResult(
                tool_call_id=tool_call.id,
                success=False,
                error=str(e),
                execution_time_ms=elapsed_ms,
            )


def _register_core_tools(registry, vector_store, native_write_enabled):
    """Register baseline tools available in all modes."""
    from agent.tools.code import register_code_tools
    from agent.tools.web import register_web_tools
    from agent.tools.files import register_file_tools
    from agent.tools.host_files import register_host_file_tools
    from agent.tools.data import register_data_tools
    from agent.tools.memory import register_memory_tools
    from agent.tools.human import register_human_tools
    from agent.tools.system_tools import register_system_tools
    from agent.tools.host_execution import register_host_execution_tools

    register_code_tools(registry)
    register_web_tools(registry)
    register_file_tools(registry)
    register_host_file_tools(registry, vector_store=vector_store, enable_write=native_write_enabled)
    register_data_tools(registry)
    register_memory_tools(registry, vector_store=vector_store)
    register_human_tools(registry)
    register_system_tools(registry)
    register_host_execution_tools(registry)


def _register_ops_tools(registry, pool):
    """Register OPS-tier tools (automation, integrations, notifications)."""
    from agent.tools.moltbook import register_moltbook_tools
    from agent.tools.moltbook_autonomous import register_moltbook_autonomous_tools
    from agent.tools.schedule import register_schedule_tools
    from agent.tools.model_swap import register_model_swap_tools
    from agent.tools.telegram_notify import register_telegram_tools
    from agent.tools.mcp import register_mcp_tools
    from agent.tools.container_control import register_container_tools

    register_moltbook_tools(registry)
    register_moltbook_autonomous_tools(registry)
    register_schedule_tools(registry)
    register_model_swap_tools(registry)
    register_telegram_tools(registry)
    register_mcp_tools(registry, pool=pool)
    register_container_tools(registry)


def _register_labs_tools(registry):
    """Register LABS-tier tools (experimental, advanced automation, delegation)."""
    from agent.tools.git import register_git_tools
    from agent.tools.self_improve import register_self_improve_tools
    from agent.tools.scanner import register_scanner_tools
    from agent.tools.computer_use import register_computer_use_tools
    from agent.tools.media_gen import register_media_gen_tools
    from agent.tools.build_automation import register_build_automation_tools
    from agent.tools.daemon_control import register_daemon_tools
    from agent.tools.time_travel import register_time_travel_tools
    from agent.tools.ui_builder import register_ui_builder_tools

    register_git_tools(registry)
    register_self_improve_tools(registry)
    register_scanner_tools(registry)
    register_computer_use_tools(registry)
    register_media_gen_tools(registry)
    register_build_automation_tools(registry)
    register_daemon_tools(registry)
    register_time_travel_tools(registry)
    register_ui_builder_tools(registry)

    # Multi-agent delegation tools (including dynamic specialist management)
    from agent.tools.delegate import (
        DELEGATE_TOOL,
        DELEGATE_PARALLEL_TOOL,
        CREATE_SPECIALIST_TOOL,
        LIST_SPECIALISTS_TOOL,
        REMOVE_SPECIALIST_TOOL,
    )

    # Coordinator is lazily initialized — needs the agent loop reference
    # which is set after the registry is built
    registry._coordinator = None
    registry._current_task = None

    def _get_coordinator():
        return getattr(registry, '_coordinator', None)

    def _get_current_task():
        return getattr(registry, '_current_task', None)

    async def delegate_handler(goal: str, specialist: str = "researcher") -> dict:
        """Delegate a subtask to a specialist sub-agent."""
        coordinator = _get_coordinator()
        current_task = _get_current_task()

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

    async def delegate_parallel_handler(subtasks: list = None) -> dict:
        """Run multiple specialist sub-agents in parallel."""
        coordinator = _get_coordinator()
        current_task = _get_current_task()

        if not coordinator or not current_task:
            return {
                "success": False,
                "error": "Delegation not available — agent loop not initialized.",
                "hint": "Delegation works during autonomous task mode.",
            }

        if not subtasks:
            return {"success": False, "error": "At least one subtask is required"}

        if not hasattr(coordinator, 'delegate_parallel'):
            return {"success": False, "error": "Coordinator does not support parallel delegation"}

        try:
            results = await coordinator.delegate_parallel(
                parent_task=current_task,
                subtasks=subtasks,
            )
            return {
                "success": True,
                "results": results,
                "count": len(results),
            }
        except Exception as e:
            return {"success": False, "error": f"Parallel delegation failed: {e}"}

    registry.register(definition=DELEGATE_PARALLEL_TOOL, handler=delegate_parallel_handler)

    async def create_specialist_handler(
        type_key: str,
        name: str,
        persona: str,
        allowed_tools: list,
        adjacent_tools: list = None,
        max_iterations: int = 15,
        max_tool_calls: int = 30,
        complexity_weight: float = 1.0,
    ) -> dict:
        """Create a dynamic specialist agent type at runtime."""
        coordinator = _get_coordinator()

        if not coordinator:
            return {
                "success": False,
                "error": "Coordinator not available — cannot create specialists outside task mode.",
            }

        return coordinator.create_specialist(
            type_key=type_key,
            name=name,
            persona=persona,
            allowed_tools=allowed_tools,
            adjacent_tools=adjacent_tools,
            max_iterations=max_iterations,
            max_tool_calls=max_tool_calls,
            complexity_weight=complexity_weight,
        )

    registry.register(definition=CREATE_SPECIALIST_TOOL, handler=create_specialist_handler)

    async def list_specialists_handler() -> dict:
        """List all available specialist types."""
        coordinator = _get_coordinator()

        if not coordinator:
            # Fallback: show built-in specialists even without coordinator
            from agent.coordinator import SPECIALISTS
            return {
                "specialists": [
                    {
                        "type": key,
                        "name": spec.name,
                        "tools": spec.allowed_tools,
                        "dynamic": False,
                    }
                    for key, spec in SPECIALISTS.items()
                ],
                "note": "Coordinator not active — showing built-in specialists only.",
            }

        return {
            "specialists": coordinator.get_specialist_info(),
            "total": len(coordinator.get_specialist_info()),
        }

    registry.register(definition=LIST_SPECIALISTS_TOOL, handler=list_specialists_handler)

    async def remove_specialist_handler(type_key: str) -> dict:
        """Remove a dynamically created specialist type."""
        coordinator = _get_coordinator()

        if not coordinator:
            return {
                "success": False,
                "error": "Coordinator not available.",
            }

        return coordinator.remove_specialist(type_key=type_key)

    registry.register(definition=REMOVE_SPECIALIST_TOOL, handler=remove_specialist_handler)


def build_tool_registry(
    hands_client=None,
    vector_store=None,
    pool=None,
    runtime_policy=None,
    *,
    enabled_bundles: tuple[str, ...] | list[str] | None = None,
    allowed_tool_names: list[str] | None = None,
    task_profile: str | None = None,
    feature_mode: str = "core",
) -> ToolRegistry:
    """
    Build the default tool registry with mode-appropriate tools.

    Only imports and registers tool modules that belong to the active
    feature mode tier (core, ops, labs).  Bundle and task-profile
    filtering is applied afterward.

    Args:
        hands_client: Optional Hands gRPC client for sandboxed code execution.
        vector_store: Optional VectorStore for memory tools.
        runtime_policy: Active runtime policy used by execution-oriented tools.
        feature_mode: One of "core", "ops", "labs".
    """
    registry = ToolRegistry()
    registry._feature_mode = feature_mode

    set_active_runtime(runtime_policy)

    native_write_enabled = os.getenv("KESTREL_ENABLE_HOST_WRITE", "false").lower() in {"1", "true", "yes", "on"}

    # Always register core tools
    _register_core_tools(registry, vector_store, native_write_enabled)

    # OPS-tier: automation, integrations, notifications
    if feature_mode in ("ops", "labs"):
        _register_ops_tools(registry, pool)

    # LABS-tier: experimental, advanced automation, delegation
    if feature_mode == "labs":
        _register_labs_tools(registry)

    registry._enabled_bundles = tuple(enabled_bundles or ())
    registry._task_profile = task_profile

    if enabled_bundles:
        bundle_allowed = allowed_tool_names_for_bundles(registry, tuple(enabled_bundles))
        registry = registry.filter(bundle_allowed)
        registry._enabled_bundles = tuple(enabled_bundles)
        registry._task_profile = task_profile
        registry._feature_mode = feature_mode

    if allowed_tool_names is not None:
        registry = registry.filter(allowed_tool_names)
        registry._enabled_bundles = tuple(enabled_bundles or ())
        registry._task_profile = task_profile
        registry._feature_mode = feature_mode

    logger.info(f"Tool registry built: {len(registry._definitions)} tools")
    return registry
