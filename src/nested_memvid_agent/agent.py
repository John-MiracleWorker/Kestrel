from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .config import AgentConfig
from .context_compiler import ContextCompiler, ContextCompilerConfig
from .event_log import AgentEvent, JsonlEventLog
from .layers import LayeredMemorySystem
from .llm.base import LLMProvider, ProviderError
from .models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from .runtime_models import (
    AgentTurnResult,
    ChatMessage,
    LLMOptions,
    LLMResponse,
    LLMStreamEvent,
    ToolCall,
    ToolExecution,
    ToolSpec,
    TurnSource,
)
from .tools.base import ApprovalHandler, ToolContext
from .tools.registry import ToolRegistry

StreamHandler = Callable[[LLMStreamEvent], None]


@dataclass(frozen=True)
class AgentDependencies:
    memory: LayeredMemorySystem
    llm: LLMProvider
    tools: ToolRegistry
    config: AgentConfig
    event_log: JsonlEventLog | None = None


class NestedMV2Agent:
    """A complete chat/tool/memory runtime around nested .mv2 memory layers."""

    def __init__(self, deps: AgentDependencies) -> None:
        self.memory = deps.memory
        self.llm = deps.llm
        self.tools = deps.tools
        self.config = deps.config
        self.event_log = deps.event_log
        self.compiler = ContextCompiler(self.memory, config=ContextCompilerConfig(total_budget_chars=deps.config.context_budget_chars))
        self.system_prompt = _load_system_prompt()

    def chat(
        self,
        user_message: str,
        session_id: str | None = None,
        *,
        run_id: str | None = None,
        approval_handler: ApprovalHandler | None = None,
        approved_tool_call_ids: frozenset[str] = frozenset(),
        stream_handler: StreamHandler | None = None,
        source: TurnSource | None = None,
    ) -> AgentTurnResult:
        session = session_id or f"session_{uuid4().hex}"
        memory_writes: list[str] = []
        executions: list[ToolExecution] = []

        self._event(
            "turn.start",
            {
                "session_id": session,
                "user_message": user_message,
                "source": source.to_public_dict() if source is not None else None,
            },
        )
        memory_writes.append(
            self._write_memory(
                layer=MemoryLayer.WORKING,
                kind=MemoryKind.OBSERVATION,
                title="User message",
                content=user_message,
                confidence=0.6,
                session_id=session,
                source=source,
            )
        )

        compiled = self.compiler.compile(objective=user_message, query=user_message)
        tool_block = "\n\n".join(spec.to_prompt_block() for spec in self.tools.specs())
        messages = [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(role="system", content=f"Compiled nested memory context:\n{compiled.prompt}"),
            ChatMessage(role="system", content=f"Available tools:\n{tool_block}"),
            ChatMessage(role="user", content=user_message),
        ]

        final_content = ""
        stop_reason = "complete"
        for round_index in range(self.config.max_tool_rounds + 1):
            response = self._generate_response(messages, self.tools.specs(), stream_handler)
            if not response.tool_calls:
                final_content = response.content
                break

            if round_index >= self.config.max_tool_rounds:
                final_content = response.content or "Stopped after max tool rounds."
                stop_reason = "max_tool_rounds"
                break

            if response.content:
                messages.append(ChatMessage(role="assistant", content=response.content))
            tool_context = ToolContext(
                memory=self.memory,
                config=self.config,
                workspace=self.config.workspace,
                event_log=self.event_log,
                session_id=session,
                run_id=run_id,
                approval_handler=approval_handler,
                approved_tool_call_ids=approved_tool_call_ids,
            )
            approval_pending = False
            for call in response.tool_calls:
                execution = self.tools.execute(call, tool_context)
                executions.append(execution)
                self._event(
                    "tool.execute",
                    {
                        "session_id": session,
                        "tool": call.name,
                        "success": execution.success,
                        "error": execution.error,
                    },
                )
                messages.append(
                    ChatMessage(
                        role="tool",
                        name=call.name,
                        tool_call_id=call.id,
                        content=execution.content,
                    )
                )
                memory_writes.append(
                    self._write_memory(
                        layer=MemoryLayer.WORKING,
                        kind=MemoryKind.EVENT if execution.success else MemoryKind.FAILURE,
                        title=f"Tool result: {call.name}",
                        content=execution.content[:4000],
                        confidence=0.7 if execution.success else 0.65,
                        session_id=session,
                        source=source,
                    )
                )
                if execution.error in {"approval_pending", "approval_required"}:
                    approval_pending = True
            if approval_pending:
                final_content = response.content or "Waiting for approval before continuing."
                stop_reason = "approval_required"
                break
        else:
            stop_reason = "loop_exhausted"

        if not final_content:
            final_content = "I ran the loop but did not get a final response. Check logs/tool results."
            stop_reason = "empty_response"

        memory_writes.append(
            self._write_memory(
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                title="Conversation turn summary",
                content=f"User: {user_message}\nAssistant: {final_content}",
                confidence=0.7,
                session_id=session,
                source=source,
            )
        )
        self.memory.seal_all()
        self._event(
            "turn.end",
            {
                "session_id": session,
                "stop_reason": stop_reason,
                "memory_writes": memory_writes,
                "tools": len(executions),
            },
        )
        return AgentTurnResult(
            session_id=session,
            user_message=user_message,
            assistant_message=final_content,
            tool_executions=tuple(executions),
            context_chars=compiled.total_chars,
            memory_writes=tuple(memory_writes),
            stop_reason=stop_reason,
            context_prompt=compiled.prompt,
            source=source,
        )

    def _generate_response(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        stream_handler: StreamHandler | None,
    ) -> LLMResponse:
        options = LLMOptions(
            stream=self.config.stream,
            timeout_seconds=self.config.timeout_seconds,
            max_retries=self.config.max_retries,
            temperature=self.config.temperature,
        )
        if not self.config.stream:
            return self.llm.generate(messages, tools, options)

        content_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        completed: LLMResponse | None = None
        emitted_provider_error = False
        try:
            for event in self.llm.stream(messages, tools, options):
                if event.type == "provider_error":
                    if stream_handler is not None:
                        stream_handler(event)
                        emitted_provider_error = True
                    raise ProviderError(event.content or "Provider stream failed", code=str(event.data.get("code", "provider_error")))
                if stream_handler is not None:
                    stream_handler(event)
                if event.type == "token" and event.content:
                    content_parts.append(event.content)
                elif event.type == "tool_call" and event.tool_call is not None:
                    tool_calls.append(event.tool_call)
                elif event.type == "message_complete" and event.response is not None:
                    completed = event.response
        except ProviderError as exc:
            if stream_handler is not None and not emitted_provider_error:
                stream_handler(
                    LLMStreamEvent(
                        type="provider_error",
                        content=str(exc),
                        data={"code": exc.code, "retryable": exc.retryable},
                    )
                )
            raise

        if completed is not None:
            return completed
        return LLMResponse(content="".join(content_parts), tool_calls=tuple(tool_calls), raw={"stream_completed": False})

    def close(self) -> None:
        self.memory.close_all()

    def _write_memory(
        self,
        *,
        layer: MemoryLayer,
        kind: MemoryKind,
        title: str,
        content: str,
        confidence: float,
        session_id: str,
        source: TurnSource | None = None,
    ) -> str:
        metadata: dict[str, object] = {"session_id": session_id}
        evidence_source = "agent_runtime"
        evidence_locator = session_id
        if source is not None:
            metadata.update(
                {
                    "channel": source.channel,
                    "channel_id": source.channel_id,
                    "conversation_id": source.conversation_id,
                }
            )
            if source.user_id is not None:
                metadata["channel_user_id"] = source.user_id
            if source.message_id is not None:
                metadata["channel_message_id"] = source.message_id
            if source.metadata:
                metadata["channel_metadata"] = source.metadata
            evidence_source = f"channel:{source.channel}"
            evidence_locator = source.message_id or source.conversation_id
        record = MemoryRecord(
            layer=layer,
            kind=kind,
            title=title,
            content=content,
            confidence=confidence,
            importance=0.5,
            metadata=metadata,
            evidence=[EvidenceRef(source=evidence_source, locator=evidence_locator)],
        )
        return self.memory.put(record)

    def _event(self, event_type: str, payload: dict[str, object]) -> None:
        if self.event_log is not None:
            self.event_log.append(AgentEvent(type=event_type, payload=payload))


def _load_system_prompt() -> str:
    path = Path(__file__).parent / "prompts" / "system_prompt.md"
    return path.read_text()
