from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from .behavior_compiler import (
    BehaviorCompiler,
    BehaviorCompilerConfig,
    BehaviorCompileRequest,
    CompiledBehavior,
    ToolPreflightContext,
)
from .behavior_delta import BehaviorDeltaStatus
from .behavior_delta_ledger import BehaviorDeltaLedger
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
from .self_profile import (
    SELF_PROFILE_QUERY,
    soul_communication_contract_from_hits,
    soul_profile_context_from_hits,
)
from .state_store import AgentStateStore
from .summarization import HeuristicSummarizer, LLMSummarizer, TurnSummarizer
from .tools.base import ApprovalHandler, ToolContext
from .tools.registry import ToolRegistry

StreamHandler = Callable[[LLMStreamEvent], None]
ProgressHandler = Callable[[str, dict[str, Any]], None]


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
        self.behavior_compiler = (
            BehaviorCompiler(
                ledger=BehaviorDeltaLedger(AgentStateStore(deps.config.state_path)),
                config=BehaviorCompilerConfig(
                    enabled=True,
                    max_active_deltas_per_run=deps.config.max_active_deltas_per_run,
                ),
            )
            if deps.config.enable_behavior_deltas
            else None
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
        progress_handler: ProgressHandler | None = None,
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
        if self.behavior_compiler is not None:
            if self.config.enable_auto_activate_low_risk_deltas:
                auto_activated = self.behavior_compiler.ledger.auto_activate_low_risk_deltas(
                    run_id=active_run_id,
                    objective=user_message,
                )
                if auto_activated:
                    self._event(
                        "behavior_delta.auto_activate",
                        {
                            "session_id": session,
                            "run_id": active_run_id,
                            "delta_ids": [delta.id for delta in auto_activated],
                            "count": len(auto_activated),
                        },
                    )
            behavior_deltas = self.behavior_compiler.compile(
                BehaviorCompileRequest(
                    objective=user_message,
                    query=user_message,
                    run_id=active_run_id,
                )
            )
            behavior_delta_text = behavior_deltas.text
            behavior_delta_ids = [delta.id for delta in behavior_deltas.deltas]
        else:
            behavior_delta_text = ""
            behavior_delta_ids = []
        context_prompt = _context_with_behavior_deltas(compiled.prompt, behavior_delta_text)
        preflight_lessons = (
            lesson_manager.preflight(objective=user_message) if lesson_manager is not None else []
        )
        context_prompt = _context_with_preflight_lessons(context_prompt, preflight_lessons)
        soul_profile_context, communication_contract = self._soul_profile_contexts()
        context_prompt = _context_with_soul_profile(context_prompt, soul_profile_context)
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
                "active_behavior_deltas": behavior_delta_ids,
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
            ChatMessage(role="system", content=communication_contract),
            ChatMessage(role="system", content=f"Compiled nested memory context:\n{context_prompt}"),
            ChatMessage(role="system", content=f"Available tools:\n{tool_block}"),
            ChatMessage(role="user", content=user_message),
        ]

        final_content = ""
        stop_reason = "complete"
        direct_tool_call = _direct_command_tool_call(user_message)
        for round_index in range(self.config.max_tool_rounds + 1):
            if direct_tool_call is not None and round_index == 0:
                self._event(
                    "command.routed",
                    {
                        "session_id": session,
                        "run_id": active_run_id,
                        "command": "search",
                        "tool": direct_tool_call.name,
                    },
                )
                response = LLMResponse(
                    content="Direct command routed to `memory.search`.",
                    tool_calls=(direct_tool_call,),
                    finish_reason="tool_calls",
                    raw={"direct_command": "search"},
                )
            else:
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
                if progress_handler is not None:
                    progress_handler(
                        "tool.request",
                        {
                            "session_id": session,
                            "run_id": active_run_id,
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "arguments": call.arguments,
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
                tool_preflight = self.tool_preflight_for_call(
                    objective=user_message,
                    call=call,
                    run_id=active_run_id,
                    task_id=None,
                    previous_executions=tuple(executions),
                )
                if tool_preflight.text:
                    self._event(
                        "behavior_delta.preflight",
                        {
                            "session_id": session,
                            "run_id": active_run_id,
                            "tool": call.name,
                            "tool_call_id": call.id,
                            "delta_ids": [delta.id for delta in tool_preflight.deltas],
                            "activation_reasons": {
                                key: list(value) for key, value in tool_preflight.activation_reasons.items()
                            },
                            "preflight_chars": len(tool_preflight.text),
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
                    execution = self.tools.execute(
                        call,
                        _tool_context_with_preflight(tool_context, tool_preflight),
                    )
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
                if progress_handler is not None:
                    progress_handler(
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
                        content=_tool_loop_content(execution.content, tool_preflight.text),
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
            if direct_tool_call is not None and round_index == 0:
                final_content = executions[-1].content if executions else response.content
                stop_reason = "complete" if executions and executions[-1].success else "tool_error"
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

    def tool_preflight_for_call(
        self,
        *,
        objective: str,
        call: ToolCall,
        run_id: str | None,
        task_id: str | None,
        previous_executions: tuple[ToolExecution, ...],
    ) -> CompiledBehavior:
        if self.behavior_compiler is None:
            return CompiledBehavior(text="", deltas=())
        spec = self.tools.spec_for(call.name)
        prior_failure = _prior_failed_execution(call, previous_executions)
        context = ToolPreflightContext(
            run_id=run_id,
            task_id=task_id,
            objective=objective,
            tool_name=call.name,
            tool_arguments=dict(call.arguments),
            prior_failure_signature=_tool_failure_text(prior_failure) if prior_failure is not None else None,
            prior_failed_tool_name=prior_failure.call.name if prior_failure is not None else None,
            prior_failed_arguments_hash=_arguments_hash(prior_failure.call.arguments) if prior_failure is not None else None,
            touched_paths=_tool_touched_paths(call.arguments),
            memory_layers=_tool_memory_layers(call.name, call.arguments),
            risk_tags=_tool_risk_tags(call, spec, prior_failure),
            tool_call_id=call.id,
        )
        return self.behavior_compiler.compile_for_tool_call(
            context,
            self.behavior_compiler.ledger.list_deltas(status=BehaviorDeltaStatus.ACTIVE),
        )

    def close(self) -> None:
        self.memory.close_all()

    def _soul_profile_contexts(self) -> tuple[str, str]:
        try:
            hits = self.memory.retrieve(
                RetrievalQuery(query=SELF_PROFILE_QUERY, layers=(MemoryLayer.SELF,), k_per_layer=8)
            )
        except Exception:
            hits = []
        return soul_profile_context_from_hits(hits), soul_communication_contract_from_hits(hits)

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


def _direct_command_tool_call(user_message: str) -> ToolCall | None:
    stripped = user_message.strip()
    lowered = stripped.lower()
    if lowered.startswith("/search "):
        query = stripped[len("/search ") :].strip()
    elif lowered.startswith("/memory search "):
        query = stripped[len("/memory search ") :].strip()
    else:
        return None
    if not query:
        return None
    return ToolCall(name="memory.search", arguments={"query": query, "k": 5}, id=f"direct_search_{uuid4().hex}")


def _context_with_behavior_deltas(context_prompt: str, behavior_delta_text: str) -> str:
    if not behavior_delta_text:
        return context_prompt
    return f"{context_prompt}\n\n## Active Behavior Deltas\n{behavior_delta_text}"


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


def _tool_context_with_preflight(tool_context: ToolContext, preflight: CompiledBehavior) -> ToolContext:
    return ToolContext(
        memory=tool_context.memory,
        config=tool_context.config,
        workspace=tool_context.workspace,
        event_log=tool_context.event_log,
        session_id=tool_context.session_id,
        run_id=tool_context.run_id,
        approval_handler=tool_context.approval_handler,
        approved_tool_call_ids=tool_context.approved_tool_call_ids,
        approved_tool_call_arguments=tool_context.approved_tool_call_arguments,
        tool_specs=tool_context.tool_specs,
        behavior_preflight=preflight.text,
        behavior_preflight_delta_ids=tuple(delta.id for delta in preflight.deltas),
    )


def _prior_failed_execution(call: ToolCall, previous_executions: tuple[ToolExecution, ...]) -> ToolExecution | None:
    for execution in reversed(previous_executions):
        if execution.call.name == call.name and not execution.success:
            return execution
    return None


def _tool_touched_paths(arguments: dict[str, Any]) -> tuple[str, ...]:
    paths: list[str] = []
    for key in ("path", "paths", "file", "files", "target", "targets"):
        value = arguments.get(key)
        if isinstance(value, str):
            paths.append(value)
        elif isinstance(value, list | tuple):
            paths.extend(str(item) for item in value if isinstance(item, str))
    command = arguments.get("command")
    if isinstance(command, list):
        paths.extend(_command_path_candidates(command))
    elif isinstance(command, str):
        paths.extend(_command_path_candidates(command.split()))
    return tuple(dict.fromkeys(path for path in paths if path))


def _command_path_candidates(command: Sequence[object]) -> list[str]:
    candidates: list[str] = []
    for item in command:
        text = str(item)
        if "/" in text or text.endswith((".py", ".md", ".json", ".toml", ".yaml", ".yml", ".txt")):
            candidates.append(text)
    return candidates


def _tool_memory_layers(tool_name: str, arguments: dict[str, Any]) -> tuple[MemoryLayer, ...]:
    layers: list[MemoryLayer] = []
    for key in ("layer", "target_layer"):
        layer = _memory_layer_from_value(arguments.get(key))
        if layer is not None:
            layers.append(layer)
    raw_layers = arguments.get("layers") or arguments.get("memory_layers")
    if isinstance(raw_layers, list | tuple):
        for item in raw_layers:
            layer = _memory_layer_from_value(item)
            if layer is not None:
                layers.append(layer)
    if tool_name.startswith("memory.") and not layers:
        layers.extend((MemoryLayer.WORKING, MemoryLayer.EPISODIC, MemoryLayer.SEMANTIC, MemoryLayer.PROCEDURAL))
    return tuple(dict.fromkeys(layers))


def _memory_layer_from_value(value: object) -> MemoryLayer | None:
    if value is None:
        return None
    try:
        return MemoryLayer(str(value))
    except ValueError:
        return None


def _tool_risk_tags(call: ToolCall, spec: ToolSpec | None, prior_failure: ToolExecution | None) -> tuple[str, ...]:
    tags: list[str] = []
    if spec is not None:
        tags.append(f"{spec.risk}_risk")
        if spec.requires_approval:
            tags.append("approval_required")
        tags.extend(spec.capabilities)
    if call.name.startswith("memory."):
        tags.append("memory_tool")
    if call.name in {"memory.import", "memory.correct"}:
        tags.append("memory_mutation")
    if prior_failure is not None:
        tags.append("repeated_failure")
        if _arguments_hash(prior_failure.call.arguments) == _arguments_hash(call.arguments):
            tags.append("unchanged_retry")
    return tuple(dict.fromkeys(tags))


def _arguments_hash(arguments: dict[str, Any]) -> str:
    payload = json.dumps(arguments, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(payload.encode("utf-8")).hexdigest()[:16]


def _tool_loop_content(content: str, preflight_text: str) -> str:
    if not preflight_text:
        return content
    bounded = preflight_text[:4000]
    if len(preflight_text) > len(bounded):
        bounded += f"\n[TRUNCATED_PREFLIGHT total_chars={len(preflight_text)}]"
    return f"{bounded}\n\nTOOL RESULT:\n{content}"


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
