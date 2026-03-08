"""Agent runtime policy package."""

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


__all__ = ["build_runtime_policy", "set_active_runtime", "get_active_runtime"]
