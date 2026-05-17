from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .cognition import FailureEpisode, LessonManager, ProofOfWorkSummary, RetryPolicy
from .config import AgentConfig
from .context_compiler import ContextCompiler, ContextCompilerConfig
from .context_frames import MV2ContextFrame
from .diagnosis import classify_failure
from .event_log import AgentEvent, JsonlEventLog
from .layers import LayeredMemorySystem
from .llm.base import LLMProvider, ProviderError
from .llm.parser import ControlMessageError, validate_llm_response
from .models import MemoryKind, MemoryLayer, RetrievalQuery
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
from .self_profile import SELF_PROFILE_QUERY, soul_profile_context_from_hits
from .summarization import HeuristicSummarizer, LLMSummarizer, TurnSummarizer
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
        self.compiler = ContextCompiler(
            self.memory,
            config=ContextCompilerConfig(
                total_budget_chars=deps.config.context_budget_chars,
                context_pack_token_budget=deps.config.context_pack_token_budget,
                expand_raw=deps.config.context_pack_expand_raw,
            ),
        )
        self.system_prompt = _load_system_prompt()
        self.turn_summarizer: TurnSummarizer = LLMSummarizer(self.llm) if self.config.llm_turn_summaries else HeuristicSummarizer()

    def chat(
        self,
        user_message: str,
        session_id: str | None = None,
        *,
        run_id: str | None = None,
        approval_handler: ApprovalHandler | None = None,
        approved_tool_call_ids: frozenset[str] = frozenset(),
        approved_tool_call_arguments: dict[str, dict[str, Any]] | None = None,
        stream_handler: StreamHandler | None = None,
        source: TurnSource | None = None,
    ) -> AgentTurnResult:
        session = session_id or f"session_{uuid4().hex}"
        active_run_id = run_id or f"run_{uuid4().hex}"
        turn_frame_id = f"turn_{uuid4().hex}"
        summary_frame_id = f"{turn_frame_id}_summary"
        user_frame_id = f"{turn_frame_id}_user"
        child_frame_ids = [user_frame_id]
        memory_writes: list[str] = []
        executions: list[ToolExecution] = []
        tool_frame_index = 0
        error: dict[str, Any] | None = None
        lesson_manager = LessonManager(self.memory) if self.config.enable_agentic_cycle else None
        retry_policy = RetryPolicy() if self.config.enable_agentic_cycle else None
        proof = ProofOfWorkSummary(objective=user_message) if self.config.enable_agentic_cycle else None
        pending_failures: list[FailureEpisode] = []

        self._event(
            "turn.start",
            {
                "session_id": session,
                "run_id": active_run_id,
                "user_message": user_message,
                "source": source.to_public_dict() if source is not None else None,
            },
        )
        memory_writes.append(
            self._write_frame(
                layer=MemoryLayer.WORKING,
                kind=MemoryKind.OBSERVATION,
                title="User message",
                content=user_message,
                frame_type="raw_chunk",
                frame_id=user_frame_id,
                confidence=0.6,
                session_id=session,
                parent_ids=(summary_frame_id,),
                source_uri=f"agent_runtime://sessions/{session}/turns/{turn_frame_id}/user",
                source_span={"role": "user"},
                source=source,
                channel_evidence=True,
            )
        )
        if _looks_like_correction(user_message):
            correction_frame_id = f"{turn_frame_id}_correction"
            child_frame_ids.append(correction_frame_id)
            memory_writes.append(
                self._write_frame(
                    layer=MemoryLayer.WORKING,
                    kind=MemoryKind.CORRECTION,
                    title="User correction",
                    content=user_message,
                    frame_type="correction",
                    frame_id=correction_frame_id,
                    confidence=0.68,
                    session_id=session,
                    parent_ids=(summary_frame_id,),
                    source_uri=f"agent_runtime://sessions/{session}/turns/{turn_frame_id}/correction",
                    source_span={"role": "user", "classification": "correction"},
                    source=source,
                    channel_evidence=True,
                )
            )

        compiled = self.compiler.compile(objective=user_message, query=user_message)
        preflight_lessons = (
            lesson_manager.preflight(objective=user_message) if lesson_manager is not None else []
        )
        context_prompt = _context_with_preflight_lessons(compiled.prompt, preflight_lessons)
        context_prompt = _context_with_soul_profile(context_prompt, self._soul_profile_context())
        if proof is not None:
            proof.lessons_applied.extend(preflight_lessons)
        self._event(
            "context.compile",
            {
                "session_id": session,
                "run_id": active_run_id,
                "context_chars": len(context_prompt),
                "hits": len(compiled.hits),
                "warnings": compiled.warnings,
                "preflight_lessons": len(preflight_lessons),
            },
        )
        if preflight_lessons:
            self._event(
                "lesson.preflight",
                {
                    "session_id": session,
                    "run_id": active_run_id,
                    "lessons": preflight_lessons,
                },
            )
        tool_block = "\n\n".join(spec.to_prompt_block() for spec in self.tools.specs())
        messages = [
            ChatMessage(role="system", content=self.system_prompt),
            ChatMessage(role="system", content=f"Compiled nested memory context:\n{context_prompt}"),
            ChatMessage(role="system", content=f"Available tools:\n{tool_block}"),
            ChatMessage(role="user", content=user_message),
        ]

        final_content = ""
        stop_reason = "complete"
        for round_index in range(self.config.max_tool_rounds + 1):
            self._event(
                "llm.request",
                {
                    "session_id": session,
                    "run_id": active_run_id,
                    "round_index": round_index,
                    "message_count": len(messages),
                    "tool_count": len(self.tools.specs()),
                    "stream": self.config.stream,
                },
            )
            try:
                response = self._generate_response(messages, self.tools.specs(), stream_handler)
            except ProviderError as exc:
                error = _provider_error_payload(exc)
                self._event("llm.error", {"session_id": session, "run_id": active_run_id, **error})
                self._event("runtime.error", {"session_id": session, "run_id": active_run_id, **error})
                self._event(
                    "diagnosis.classified",
                    {
                        "session_id": session,
                        "run_id": active_run_id,
                        "source": "provider",
                        **classify_failure(f"Provider error {error['code']}: {error['message']}", source="provider").to_payload(),
                    },
                )
                failure_frame_id = f"{turn_frame_id}_provider_error"
                child_frame_ids.append(failure_frame_id)
                memory_writes.append(
                    self._write_frame(
                        layer=MemoryLayer.WORKING,
                        kind=MemoryKind.FAILURE,
                        title="Provider failure",
                        content=f"{error['code']}: {error['message']}",
                        frame_type="failure_note",
                        frame_id=failure_frame_id,
                        confidence=0.72,
                        session_id=session,
                        parent_ids=(summary_frame_id,),
                        source_uri=f"provider://{self.config.provider}/{self.config.model}",
                        source_span={"round_index": round_index, "retryable": error["retryable"]},
                        source=source,
                    )
                )
                final_content = f"Provider error ({error['code']}): {error['message']}"
                stop_reason = "provider_error"
                break
            self._event(
                "llm.response",
                {
                    "session_id": session,
                    "run_id": active_run_id,
                    "round_index": round_index,
                    "content_chars": len(response.content),
                    "tool_calls": len(response.tool_calls),
                    "finish_reason": response.finish_reason,
                    "usage": response.usage,
                },
            )
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
                run_id=active_run_id,
                approval_handler=approval_handler,
                approved_tool_call_ids=approved_tool_call_ids,
                approved_tool_call_arguments=approved_tool_call_arguments,
            )
            approval_pending = False
            for call in response.tool_calls:
                self._event(
                    "tool.request",
                    {
                        "session_id": session,
                        "run_id": active_run_id,
                        "tool": call.name,
                        "tool_call_id": call.id,
                    },
                )
                if call.id in approved_tool_call_ids:
                    self._event(
                        "approval.resolved",
                        {
                            "session_id": session,
                            "run_id": active_run_id,
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "decision": "approved",
                        },
                    )
                retry_decision = None
                if retry_policy is not None:
                    retry_decision = retry_policy.assess_call(
                        call,
                        executions,
                        similar_lessons=tuple(str(item.get("id", "")) for item in preflight_lessons),
                    )
                if retry_decision is not None and not retry_decision.retry_allowed:
                    retry_payload = retry_decision.to_payload()
                    execution = ToolExecution(
                        call=call,
                        success=False,
                        content=json.dumps({"retry_gate": retry_payload}, indent=2),
                        data={"retry_gate": retry_payload},
                        error="retry_blocked",
                    )
                    self._event(
                        "retry.blocked",
                        {
                            "session_id": session,
                            "run_id": active_run_id,
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "retry_gate": retry_payload,
                        },
                    )
                else:
                    execution = self.tools.execute(call, tool_context)
                executions.append(execution)
                tool_frame_index += 1
                tool_frame_id = f"{turn_frame_id}_tool_{tool_frame_index}"
                child_frame_ids.append(tool_frame_id)
                if proof is not None:
                    proof.tools_used.append(
                        {
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "success": execution.success,
                            "error": execution.error,
                        }
                    )
                    if execution.success:
                        proof.completed_steps.append(f"{call.name} completed")
                self._event(
                    "tool.execute",
                    {
                        "session_id": session,
                        "run_id": active_run_id,
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "success": execution.success,
                        "error": execution.error,
                    },
                )
                self._event(
                    "tool.result" if execution.success else "tool.error",
                    {
                        "session_id": session,
                        "run_id": active_run_id,
                        "tool": call.name,
                        "tool_call_id": call.id,
                        "success": execution.success,
                        "error": execution.error,
                        "content_chars": len(execution.content),
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
                    self._write_frame(
                        layer=MemoryLayer.WORKING,
                        kind=MemoryKind.EVENT if execution.success else MemoryKind.FAILURE,
                        title=f"Tool result: {call.name}",
                        content=_tool_memory_content(execution.content),
                        frame_type="raw_chunk" if execution.success else "failure_note",
                        frame_id=tool_frame_id,
                        confidence=0.7 if execution.success else 0.65,
                        session_id=session,
                        parent_ids=(summary_frame_id,),
                        source_uri=f"tool://{call.name}/{call.id}",
                        source_span={
                            "round_index": round_index,
                            "tool_call_id": call.id,
                            "success": execution.success,
                            "error": execution.error,
                        },
                        source=source,
                    )
                )
                if lesson_manager is not None and proof is not None:
                    if execution.success:
                        if _is_validation_success(execution, self.tools.spec_for(execution.call.name)):
                            proof.validation_evidence.append(_validation_evidence(execution))
                            if pending_failures and call.strategy is not None:
                                for failure in pending_failures:
                                    lesson, lesson_record_id = lesson_manager.write_lesson_from_resolution(
                                        failure=failure,
                                        validation=execution,
                                        strategy=call.strategy,
                                    )
                                    memory_writes.append(lesson_record_id)
                                    proof.lessons_created.append(lesson.to_payload())
                                    self._event(
                                        "lesson.created",
                                        {
                                            "session_id": session,
                                            "run_id": active_run_id,
                                            "record_id": lesson_record_id,
                                            "lesson": lesson.to_payload(),
                                        },
                                    )
                                pending_failures.clear()
                    else:
                        failure_text = _tool_failure_text(execution)
                        classification = classify_failure(failure_text, source=f"tool:{call.name}")
                        diagnosis_payload = classification.to_payload()
                        recall_hits = lesson_manager.recall_failure(
                            classification=classification,
                            failure_text=failure_text,
                        )
                        episode, episode_record_id = lesson_manager.record_failure(
                            run_id=active_run_id,
                            execution=execution,
                            classification=classification,
                            recall_hits=recall_hits,
                            attempted_strategy=call.strategy.changed_strategy if call.strategy is not None else "",
                        )
                        pending_failures.append(episode)
                        memory_writes.append(episode_record_id)
                        child_frame_ids.append(episode.failure_id)
                        proof.failures.append(episode.to_payload())
                        proof.diagnoses.append(diagnosis_payload)
                        if execution.error not in {"approval_pending", "approval_required", "tool_disabled"}:
                            proof.remaining_risks.append(f"{call.name} failed: {classification.category}")
                        self._event(
                            "diagnosis.classified",
                            {
                                "session_id": session,
                                "run_id": active_run_id,
                                "source": f"tool:{call.name}",
                                "tool_call_id": call.id,
                                **diagnosis_payload,
                            },
                        )
                        self._event(
                            "lesson.recall",
                            {
                                "session_id": session,
                                "run_id": active_run_id,
                                "tool": call.name,
                                "tool_call_id": call.id,
                                "hits": recall_hits,
                            },
                        )
                        self._event(
                            "failure.episode",
                            {
                                "session_id": session,
                                "run_id": active_run_id,
                                "record_id": episode_record_id,
                                "failure": episode.to_payload(),
                            },
                        )
                if execution.error in {"approval_pending", "approval_required"}:
                    self._event(
                        "approval.required",
                        {
                            "session_id": session,
                            "run_id": active_run_id,
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "error": execution.error,
                        },
                    )
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
        proof_payload = None
        if proof is not None:
            proof.stop_reason = stop_reason
            proof_payload = proof.to_payload()

        memory_writes.append(
            self._write_frame(
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.SUMMARY,
                title="Conversation turn summary",
                content=self.turn_summarizer.summarize(user_message, executions, final_content),
                frame_type="session_summary",
                frame_id=summary_frame_id,
                confidence=0.7,
                session_id=session,
                child_ids=tuple(child_frame_ids),
                source_uri=f"agent_runtime://sessions/{session}/turns/{turn_frame_id}",
                source_span={"role": "turn_summary"},
                source=source,
            )
        )
        self.memory.maybe_seal_all(
            write_threshold=self.config.memory_seal_write_threshold,
            interval_seconds=self.config.memory_seal_interval_seconds,
        )
        self._event(
            "turn.end",
            {
                "session_id": session,
                "run_id": active_run_id,
                "stop_reason": stop_reason,
                "memory_writes": memory_writes,
                "tools": len(executions),
                "proof_of_work": proof_payload,
            },
        )
        return AgentTurnResult(
            session_id=session,
            user_message=user_message,
            assistant_message=final_content,
            tool_executions=tuple(executions),
            context_chars=len(context_prompt),
            memory_writes=tuple(memory_writes),
            stop_reason=stop_reason,
            context_prompt=context_prompt,
            source=source,
            run_id=active_run_id,
            error=error,
            proof_of_work=proof_payload,
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
            try:
                return validate_llm_response(self.llm.generate(messages, tools, options), tools=tools)
            except ControlMessageError as exc:
                raise ProviderError(str(exc), code="invalid_control_message", retryable=False) from exc

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
            try:
                return validate_llm_response(completed, tools=tools)
            except ControlMessageError as exc:
                raise ProviderError(str(exc), code="invalid_control_message", retryable=False) from exc
        try:
            return validate_llm_response(
                LLMResponse(content="".join(content_parts), tool_calls=tuple(tool_calls), raw={"stream_completed": False}),
                tools=tools,
            )
        except ControlMessageError as exc:
            raise ProviderError(str(exc), code="invalid_control_message", retryable=False) from exc

    def close(self) -> None:
        self.memory.close_all()

    def _soul_profile_context(self) -> str:
        try:
            hits = self.memory.retrieve(
                RetrievalQuery(query=SELF_PROFILE_QUERY, layers=(MemoryLayer.SELF,), k_per_layer=8)
            )
        except Exception:
            return ""
        return soul_profile_context_from_hits(hits)

    def _write_frame(
        self,
        *,
        layer: MemoryLayer,
        kind: MemoryKind,
        title: str,
        content: str,
        frame_type: str,
        frame_id: str,
        confidence: float,
        session_id: str,
        parent_ids: tuple[str, ...] = (),
        child_ids: tuple[str, ...] = (),
        source_uri: str | None = None,
        source_span: dict[str, object] | None = None,
        source: TurnSource | None = None,
        channel_evidence: bool = False,
    ) -> str:
        metadata: dict[str, object] = {"session_id": session_id}
        resolved_source_uri = source_uri
        resolved_source_span = dict(source_span or {})
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
            if channel_evidence:
                metadata["runtime_source_uri"] = source_uri
                resolved_source_uri = f"channel:{source.channel}"
                resolved_source_span = {
                    **resolved_source_span,
                    "path": source.message_id or source.conversation_id,
                }
        frame = MV2ContextFrame(
            id=frame_id,
            frame_type=frame_type,
            layer=layer,
            kind=kind,
            title=title,
            content=content,
            confidence=confidence,
            importance=0.5,
            parent_ids=parent_ids,
            child_ids=child_ids,
            source_uri=resolved_source_uri,
            source_span=resolved_source_span,
            metadata=metadata,
            tags={"session_id": session_id},
        )
        record_id = self.memory.put_frame(frame)
        self._event(
            "memory.write",
            {
                "record_id": record_id,
                "frame_id": frame_id,
                "layer": layer.value,
                "kind": kind.value,
                "title": title,
                "session_id": session_id,
            },
        )
        return record_id

    def _event(self, event_type: str, payload: dict[str, object]) -> None:
        if self.event_log is not None:
            self.event_log.append(AgentEvent(type=event_type, payload=payload))


def _load_system_prompt() -> str:
    path = Path(__file__).parent / "prompts" / "system_prompt.md"
    return path.read_text()


def _looks_like_correction(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return False
    markers = (
        "correction:",
        "correcting myself",
        "i meant to say",
        "let me correct that",
        "to clarify:",
        "remember:",
    )
    return any(marker in lowered for marker in markers)


def _context_with_preflight_lessons(context_prompt: str, lessons: list[dict[str, Any]]) -> str:
    if not lessons:
        return context_prompt
    lines = ["## Prior Failure Lessons"]
    for lesson in lessons:
        title = str(lesson.get("title", "Prior lesson"))
        snippet = str(lesson.get("snippet", "")).strip()
        layer = str(lesson.get("layer", "memory"))
        lines.append(f"- [{layer}] {title}: {snippet[:500]}")
    return f"{context_prompt}\n\n" + "\n".join(lines)


def _context_with_soul_profile(context_prompt: str, soul_profile_context: str) -> str:
    if not soul_profile_context:
        return context_prompt
    return f"{context_prompt}\n\n## Active Soul/User Profile\n{soul_profile_context}"


def _tool_failure_text(execution: ToolExecution) -> str:
    parts = []
    if execution.error:
        parts.append(str(execution.error))
    if execution.content:
        parts.append(execution.content)
    return "\n".join(parts).strip() or "unknown tool failure"


def _is_validation_success(execution: ToolExecution, spec: ToolSpec | None = None) -> bool:
    if not execution.success:
        return False
    validation = execution.data.get("validation")
    if isinstance(validation, dict):
        return validation.get("success") is True
    if spec is not None and spec.produces_validation:
        return True
    return False


def _validation_evidence(execution: ToolExecution) -> str:
    command = execution.call.arguments.get("command")
    if isinstance(command, list):
        command_text = " ".join(str(item) for item in command)
    elif isinstance(command, str):
        command_text = command
    else:
        command_text = execution.call.name
    return f"{command_text}: success; tool_call_id={execution.call.id}; content_chars={len(execution.content)}"


def _tool_memory_content(content: str) -> str:
    max_chars = 1_000_000
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + f"\n[TRUNCATED_TOOL_OUTPUT total_chars={len(content)}]"


def _provider_error_payload(exc: ProviderError) -> dict[str, object]:
    return {
        "message": str(exc),
        "code": exc.code,
        "retryable": exc.retryable,
        "error_type": type(exc).__name__,
    }
