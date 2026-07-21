from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AgentConfig
from ..event_log import JsonlEventLog
from ..layers import LayeredMemorySystem
from ..runtime_models import ToolCall, ToolExecution, ToolSpec

ApprovalHandler = Callable[[ToolCall, ToolSpec, "ToolContext"], ToolExecution]


@dataclass
class ToolContext:
    memory: LayeredMemorySystem
    config: AgentConfig
    workspace: Path
    event_log: JsonlEventLog | None = None
    session_id: str = "default"
    run_id: str | None = None
    execution_origin: str = "standalone"
    approval_handler: ApprovalHandler | None = None
    approved_tool_call_ids: frozenset[str] = frozenset()
    approved_tool_call_arguments: Mapping[str, dict[str, Any]] | None = None
    approval_receipts: Mapping[str, dict[str, Any]] | None = None
    trusted_request_origin: str | None = None
    tool_specs: tuple[ToolSpec, ...] = ()
    behavior_preflight: str = ""
    behavior_preflight_delta_ids: tuple[str, ...] = ()


class AgentTool(ABC):
    spec: ToolSpec
    wait_for_completion_on_timeout: bool = False

    @abstractmethod
    def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolExecution:
        raise NotImplementedError

    def cancel(self, call_id: str) -> None:
        """Best-effort cancellation hook called when the registry times out a tool call."""
        del call_id

    def _result(
        self,
        call: ToolCall,
        *,
        success: bool,
        content: str,
        data: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> ToolExecution:
        return ToolExecution(
            call=call,
            success=success,
            content=content,
            data=data or {},
            error=error,
        )
