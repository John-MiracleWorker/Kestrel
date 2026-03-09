"""Agent runtime policy package — execution environments + LangGraph orchestration."""

from __future__ import annotations

from typing import Optional

from agent.runtime.base import ExecutionRuntime
from agent.runtime.policy import build_runtime_policy

_active_runtime: Optional[ExecutionRuntime] = None


def set_active_runtime(runtime: Optional[ExecutionRuntime]) -> None:
    global _active_runtime
    _active_runtime = runtime


def get_active_runtime() -> Optional[ExecutionRuntime]:
    return _active_runtime


# LangGraph orchestration components (lazy imports to avoid dependency on langgraph)
def get_langgraph_engine():
    """Get the LangGraph engine class (lazy import)."""
    from agent.runtime.engine import LangGraphEngine
    return LangGraphEngine


def get_agent_graph_builder():
    """Get the agent graph builder function (lazy import)."""
    from agent.runtime.agent_graph import build_agent_graph
    return build_agent_graph


__all__ = [
    "build_runtime_policy",
    "set_active_runtime",
    "get_active_runtime",
    "get_langgraph_engine",
    "get_agent_graph_builder",
]
