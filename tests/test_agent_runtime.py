from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.behavior_delta import (
    BehaviorDelta,
    BehaviorDeltaKind,
    BehaviorDeltaRisk,
    BehaviorDeltaStatus,
    TriggerSpec,
    ValidationPlan,
)
from nested_memvid_agent.behavior_delta_ledger import BehaviorDeltaLedger
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_log import JsonlEventLog
from nested_memvid_agent.llm.base import LLMProvider, ProviderError
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import (
    ChatMessage,
    LLMOptions,
    LLMResponse,
    LLMStreamEvent,
    StrategyProposal,
    ToolCall,
    ToolExecution,
    ToolSpec,
)
from nested_memvid_agent.security_boundary import register_secret_value
from nested_memvid_agent.self_profile import (
    build_onboarding_profile,
    onboarding_record_content,
    soul_communication_contract_from_hits,
)
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry


def test_default_communication_contract_rejects_flat_greeting_posture() -> None:
    contract = soul_communication_contract_from_hits([])

    assert "Avoid flat acknowledgments like" in contract
    assert "I'm here. What do you want to work on first?" in contract
    assert "mirror the user's casual energy" in contract
    assert "not a ticket intake form" in contract


def test_agent_routes_search_slash_command_without_llm(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            id="rec_slash_search",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            title="Needle fact",
            content="slash command needle result",
            confidence=0.9,
            importance=0.8,
        )
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider([LLMResponse(content="should not be used")]),
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat("/search slash command needle", session_id="test")

    assert result.stop_reason == "complete"
    assert len(result.tool_executions) == 1
    assert result.tool_executions[0].call.name == "memory.search"
    assert result.tool_executions[0].success is True
    assert "slash command needle result" in result.assistant_message


def test_agent_chat_writes_working_and_episodic_memory(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider([LLMResponse(content="hello back")]),
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat("hello", session_id="test")

    assert result.assistant_message == "hello back"
    assert result.stop_reason == "complete"
    assert len(result.memory_writes) >= 2
    assert memory.backends[MemoryLayer.WORKING].find("hello", k=3)
    assert memory.backends[MemoryLayer.EPISODIC].find("hello back", k=3)
    working_records = memory.backends[MemoryLayer.WORKING].records
    episodic_records = memory.backends[MemoryLayer.EPISODIC].records
    user_record = next(record for record in working_records if record.title == "User message")
    summary_record = next(
        record for record in episodic_records if record.title == "Conversation turn summary"
    )
    assert user_record.metadata["frame_type"] == "raw_chunk"
    assert summary_record.metadata["frame_type"] == "session_summary"
    assert user_record.metadata["parent_ids"] == [summary_record.id]
    assert user_record.id in summary_record.metadata["child_ids"]


def test_agent_redacts_secrets_before_llm_and_memory_boundaries(tmp_path: Path) -> None:
    secret = "opaque-provider-secret-12345"

    class CapturingMockLLM(MockLLMProvider):
        def __init__(self) -> None:
            super().__init__([LLMResponse(content=f"Echoed OPENAI_API_KEY={secret}")])
            self.messages: list[ChatMessage] = []

        def generate(
            self,
            messages: list[ChatMessage],
            tools: list[ToolSpec],
            options: LLMOptions | None = None,
        ) -> LLMResponse:
            self.messages = list(messages)
            return super().generate(messages, tools, options)

    memory = build_memory_system("memory", tmp_path / "memory")
    llm = CapturingMockLLM()
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat(f"OPENAI_API_KEY={secret}", session_id="redaction-test")

    assert secret not in "\n".join(message.content for message in llm.messages)
    assert secret not in result.user_message
    assert secret not in result.assistant_message
    assert "<redacted>" in result.user_message
    stored = "\n".join(
        record.content for layer in MemoryLayer for record in memory.backends[layer].records
    )
    assert secret not in stored
    assert "<redacted>" in stored


def test_layered_memory_redacts_secret_content_at_central_write_boundary(tmp_path: Path) -> None:
    secret = "opaque-memory-secret-12345"
    memory = build_memory_system("memory", tmp_path / "memory")

    memory.put(
        MemoryRecord(
            layer=MemoryLayer.WORKING,
            kind=MemoryKind.OBSERVATION,
            title="Diagnostic output",
            content=f"client_secret: {secret}",
            confidence=0.8,
        )
    )

    stored = memory.backends[MemoryLayer.WORKING].records[-1]
    assert secret not in stored.content
    assert "<redacted>" in stored.content


def test_agent_correction_detection_uses_tight_markers(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider([LLMResponse(content="ok"), LLMResponse(content="noted")]),
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    agent.chat("actually I think you're right", session_id="test")
    assert not [
        record
        for record in memory.backends[MemoryLayer.WORKING].records
        if record.kind == MemoryKind.CORRECTION
    ]

    agent.chat("to clarify: the version is 5.5, not 5.0", session_id="test")
    corrections = [
        record
        for record in memory.backends[MemoryLayer.WORKING].records
        if record.kind == MemoryKind.CORRECTION
    ]
    assert corrections
    assert "version is 5.5" in corrections[-1].content


def test_agent_executes_tool_call_and_continues(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="I will search memory.",
                tool_calls=(ToolCall(name="memory.search", arguments={"query": "needle", "k": 2}),),
            ),
            LLMResponse(content="I checked memory."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
            event_log=event_log,
        )
    )

    result = agent.chat("find needle", session_id="test")

    assert result.assistant_message == "I checked memory."
    assert result.run_id.startswith("run_")
    assert len(result.tool_executions) == 1
    assert result.tool_executions[0].success
    event_types = [event.type for event in event_log.tail(limit=50)]
    assert "context.compile" in event_types
    assert "llm.request" in event_types
    assert "llm.response" in event_types
    assert "tool.request" in event_types
    assert "tool.result" in event_types
    assert "memory.write" in event_types


def test_agent_writes_full_tool_output_and_budgeted_summary(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = ToolRegistry()
    registry.register(LongOutputTool())
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="I will run long output.",
                tool_calls=(ToolCall(name="long.output", arguments={}),),
            ),
            LLMResponse(content="Long output inspected."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=registry,
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat("run long output", session_id="test")

    assert result.stop_reason == "complete"
    tool_record = next(
        record
        for record in memory.backends[MemoryLayer.WORKING].records
        if record.title == "Tool result: long.output"
    )
    assert "TAIL_SENTINEL" in tool_record.content
    assert len(tool_record.content) > 12_000
    summary_record = next(
        record
        for record in memory.backends[MemoryLayer.EPISODIC].records
        if record.title == "Conversation turn summary"
    )
    assert "long.output succeeded" in summary_record.content
    assert len(summary_record.content) < 1600


def test_agent_stops_on_direct_approval_required(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="I need shell access.",
                tool_calls=(
                    ToolCall(name="shell.run", arguments={"command": ["echo", "blocked"]}),
                ),
            ),
            LLMResponse(content="This should not run."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs", allow_shell=True
            ),
            event_log=event_log,
        )
    )

    result = agent.chat("run echo", session_id="test")

    assert result.stop_reason == "approval_required"
    assert result.assistant_message == "I need shell access."
    assert len(result.tool_executions) == 1
    assert result.tool_executions[0].error == "approval_required"
    event_types = [event.type for event in event_log.tail(limit=50)]
    assert "approval.required" in event_types
    assert "tool.error" in event_types
    failures = [
        record
        for record in memory.backends[MemoryLayer.WORKING].records
        if record.kind == MemoryKind.FAILURE
    ]
    assert failures
    assert failures[0].metadata["frame_type"] == "failure_note"


def test_agent_stops_after_first_approval_in_multi_tool_response(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    first = ToolCall(
        name="shell.run",
        arguments={"command": ["echo", "first"]},
        id="approval_first",
    )
    second = ToolCall(
        name="shell.run",
        arguments={"command": ["echo", "must-not-request"]},
        id="approval_second",
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider(
                [LLMResponse(content="Both need approval.", tool_calls=(first, second))]
            ),
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                allow_shell=True,
            ),
        )
    )
    requested: list[str] = []

    def request_approval(
        call: ToolCall,
        _spec: ToolSpec,
        _context: ToolContext,
    ) -> ToolExecution:
        requested.append(call.id)
        return ToolExecution(
            call=call,
            success=False,
            content="Approval pending.",
            error="approval_pending",
        )

    result = agent.chat(
        "run both",
        session_id="test",
        approval_handler=request_approval,
    )

    assert result.stop_reason == "approval_required"
    assert requested == [first.id]
    assert [execution.call.id for execution in result.tool_executions] == [first.id]
    assert result.tool_executions[0].error == "approval_pending"


def test_agent_direct_approval_requires_exact_arguments(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        name="shell.run", arguments={"command": ["echo", "direct-exact"]}, id="direct_shell"
    )
    llm = MockLLMProvider(
        [
            LLMResponse(content="I will run the approved command.", tool_calls=(call,)),
            LLMResponse(content="Tool finished."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs", allow_shell=True
            ),
        )
    )

    id_only = agent.chat(
        "run approved command",
        session_id="test",
        approved_tool_call_ids=frozenset({"direct_shell"}),
    )

    assert id_only.stop_reason == "approval_required"
    assert id_only.tool_executions[0].error == "approval_required"

    exact_memory = build_memory_system("memory", tmp_path / "memory-exact")
    exact_llm = MockLLMProvider(
        [
            LLMResponse(content="I will run the approved command.", tool_calls=(call,)),
            LLMResponse(content="Tool finished."),
        ]
    )
    exact_agent = NestedMV2Agent(
        AgentDependencies(
            memory=exact_memory,
            llm=exact_llm,
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory-exact",
                log_dir=tmp_path / "logs-exact",
                allow_shell=True,
            ),
        )
    )

    exact = exact_agent.chat(
        "run approved command",
        session_id="test",
        approved_tool_call_ids=frozenset({"direct_shell"}),
        approved_tool_call_arguments={"direct_shell": {"command": ["echo", "direct-exact"]}},
    )

    assert exact.stop_reason == "complete"
    assert exact.tool_executions[0].success is True


@pytest.mark.parametrize("stream", [False, True])
def test_agent_rejects_provider_secret_tool_arguments_before_approved_execution(
    tmp_path: Path,
    stream: bool,
) -> None:
    secret = "opaque-provider-tool-secret-12345"
    register_secret_value(secret)
    seen: list[dict[str, object]] = []

    class DangerEchoTool(AgentTool):
        spec = ToolSpec(
            name="danger.echo",
            description="Record a value for the sensitive-argument boundary test.",
            parameters={"type": "object", "properties": {"message": {"type": "string"}}},
            risk="high",
        )

        def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
            del context
            seen.append(dict(arguments))
            return ToolExecution(
                call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
                success=True,
                content="executed",
            )

    call = ToolCall(
        name="danger.echo",
        arguments={"message": secret},
        id="danger_fixed",
    )
    registry = ToolRegistry()
    registry.register(DangerEchoTool())
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=build_memory_system("memory", tmp_path / "memory"),
            llm=MockLLMProvider([LLMResponse(content="Run danger echo.", tool_calls=(call,))]),
            tools=registry,
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                stream=stream,
            ),
            event_log=event_log,
        )
    )

    result = agent.chat(
        "run the approved tool",
        session_id="test",
        approved_tool_call_ids=frozenset({call.id}),
        approved_tool_call_arguments={call.id: {"message": "<redacted>"}},
    )

    assert seen == []
    assert result.tool_executions[0].success is False
    assert result.tool_executions[0].error == "sensitive_tool_arguments_rejected"
    assert result.tool_executions[0].call.arguments == {"message": "<redacted>"}
    assert secret not in repr(result)
    assert "security.tool_call_rejected" in [
        event.type for event in event_log.tail(limit=50)
    ]


def test_agent_rejects_duplicate_approved_tool_call_id_after_first_execution(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(
        name="shell.run",
        arguments={"command": ["echo", "single-use"]},
        id="approved_once",
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider([LLMResponse(content="Run once.", tool_calls=(call, call))]),
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                allow_shell=True,
            ),
        )
    )

    result = agent.chat(
        "run the approved command once",
        session_id="test",
        approved_tool_call_ids=frozenset({call.id}),
        approved_tool_call_arguments={call.id: call.arguments},
    )

    assert [execution.success for execution in result.tool_executions] == [True, False]
    assert result.tool_executions[1].error == "duplicate_tool_call_id"
    assert result.tool_executions[0].content.count("single-use") == 1


def test_agent_streams_mock_tokens_without_losing_final_message(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider([LLMResponse(content="streamed hello")]),
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs", stream=True
            ),
        )
    )
    events: list[LLMStreamEvent] = []

    result = agent.chat("hello", session_id="test", stream_handler=events.append)

    assert result.assistant_message == "streamed hello"
    assert [event.content for event in events if event.type == "token"] == ["streamed hello"]
    assert any(event.type == "message_complete" for event in events)


def test_agent_redacts_registered_secret_split_across_stream_tokens(
    tmp_path: Path,
) -> None:
    secret = "opaque-stream-boundary-secret-12345"
    register_secret_value(secret)
    memory = build_memory_system("memory", tmp_path / "memory")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=SplitSecretStreamingProvider(secret),
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                stream=True,
            ),
        )
    )
    events: list[LLMStreamEvent] = []

    result = agent.chat("hello", session_id="test", stream_handler=events.append)

    assert result.assistant_message == "before <redacted> after"
    assert (
        "".join(event.content for event in events if event.type == "token")
        == "before <redacted> after"
    )
    completed = next(event for event in events if event.type == "message_complete")
    assert completed.response is not None
    assert completed.response.content == "before <redacted> after"
    assert secret not in repr(events)


def test_agent_redacts_streamed_provider_error_and_final_result(
    tmp_path: Path,
) -> None:
    secret = "opaque-stream-provider-error-12345"
    register_secret_value(secret)
    memory = build_memory_system("memory", tmp_path / "memory")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=SecretErrorStreamingProvider(secret),
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                stream=True,
            ),
        )
    )
    events: list[LLMStreamEvent] = []

    result = agent.chat("hello", session_id="test", stream_handler=events.append)

    assert result.stop_reason == "provider_error"
    assert secret not in result.assistant_message
    assert result.error is not None
    assert secret not in json.dumps(result.error)
    assert "<redacted>" in result.assistant_message
    assert [event.type for event in events] == ["provider_error"]
    assert secret not in repr(events[0])
    assert "<redacted>" in repr(events[0])


def test_provider_failure_is_structured_logged_and_remembered(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=FailingProvider(),
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
            event_log=event_log,
        )
    )

    result = agent.chat("hello", session_id="test", run_id="run_explicit")

    assert result.run_id == "run_explicit"
    assert result.stop_reason == "provider_error"
    assert result.error == {
        "message": "fake outage",
        "code": "fake_provider_error",
        "retryable": True,
        "error_type": "ProviderError",
    }
    failures = [
        record
        for record in memory.backends[MemoryLayer.WORKING].records
        if record.title == "Provider failure"
    ]
    assert failures
    event_types = [event.type for event in event_log.tail(limit=50)]
    assert "llm.error" in event_types
    assert "runtime.error" in event_types
    diagnosis_events = [
        event for event in event_log.tail(limit=50) if event.type == "diagnosis.classified"
    ]
    assert diagnosis_events
    assert diagnosis_events[0].payload["classification"] == "provider_failure"


def test_agent_injects_preflight_lessons_into_context(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.PROCEDURAL,
            kind=MemoryKind.PROCEDURE,
            title="Prior pytest lesson",
            content="When pytest fails in this repo, narrow to the focused failing test first.",
            confidence=0.9,
            metadata={"cognition_schema": "lesson_card.v1", "frame_type": "skill_card"},
        )
    )
    provider = CapturingProvider()
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=provider,
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat("pytest failure", session_id="test")

    assert result.proof_of_work is not None
    assert result.proof_of_work["lessons_applied"]
    assert any(
        "Prior Failure Lessons" in message.content
        for message in provider.messages
        if message.role == "system"
    )


def test_agent_injects_onboarding_profile_from_soul_memory(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    profile = build_onboarding_profile(
        {
            "agent_name": "Northstar",
            "user_name": "Taylor",
            "preferred_name": "Tay",
            "persona": "mentor",
            "working_style": "Show the reasoning before code changes.",
            "goals": ["ship local-first agent workflows"],
        }
    )
    memory.put(
        MemoryRecord(
            layer=MemoryLayer.SELF,
            kind=MemoryKind.FACT,
            title="Kestrel onboarding profile",
            content=onboarding_record_content(profile),
            confidence=0.92,
            importance=0.84,
            metadata={"self_schema": "user_profile", "frame_type": "self_model"},
        )
    )
    provider = CapturingProvider()
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=provider,
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat("help me refactor this", session_id="test")

    assert result.assistant_message == "ok"
    profile_messages = [
        message.content
        for message in provider.messages
        if message.role == "system" and "Active Soul/User Profile" in message.content
    ]
    assert profile_messages
    assert "Northstar" in profile_messages[0]
    assert "Tay" in profile_messages[0]
    assert "Patient Mentor" in profile_messages[0]
    contract_messages = [
        message.content
        for message in provider.messages
        if message.role == "system" and "Active Communication Contract" in message.content
    ]
    assert contract_messages
    assert "Patient Mentor" in contract_messages[0]
    assert "Own mistakes without defensiveness" in contract_messages[0]
    assert "Do not scold the user" in contract_messages[0]


def test_agent_records_failure_episode_and_blocks_unchanged_retry(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    registry = ToolRegistry()
    registry.register(FailingTool())
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="First attempt.",
                tool_calls=(ToolCall(name="fail.tool", arguments={"target": "same"}),),
            ),
            LLMResponse(
                content="Retrying.",
                tool_calls=(ToolCall(name="fail.tool", arguments={"target": "same"}),),
            ),
            LLMResponse(content="Blocked retry noted."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=registry,
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
            event_log=event_log,
        )
    )

    result = agent.chat("run failing tool", session_id="test")

    assert [execution.error for execution in result.tool_executions] == [
        "tool_failed",
        "retry_blocked",
    ]
    assert result.proof_of_work is not None
    assert result.proof_of_work["failures"]
    episodic = memory.backends[MemoryLayer.EPISODIC].records
    assert any(
        record.metadata.get("cognition_schema") == "failure_episode.v1" for record in episodic
    )
    event_types = [event.type for event in event_log.tail(limit=100)]
    assert "diagnosis.classified" in event_types
    assert "retry.blocked" in event_types


def test_agent_creates_lesson_after_changed_strategy_validation(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = ToolRegistry()
    registry.register(FailingTool())
    registry.register(ValidationCheckTool())
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="First attempt.",
                tool_calls=(ToolCall(name="fail.tool", arguments={"target": "same"}),),
            ),
            LLMResponse(
                content="Validate changed strategy.",
                tool_calls=(
                    ToolCall(
                        name="validation.check",
                        arguments={"target": "focused"},
                        strategy=StrategyProposal(
                            changed_strategy="Validate the focused target instead of repeating the failed broad action.",
                            why_different="This checks a narrower signal.",
                            expected_signal="The focused validation passes.",
                            fallback_if_fails="Inspect the failure output before another retry.",
                        ),
                    ),
                ),
            ),
            LLMResponse(content="Validation passed."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=registry,
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat("learn from failure", session_id="test")

    assert result.proof_of_work is not None
    assert result.proof_of_work["lessons_created"]
    procedural = memory.backends[MemoryLayer.PROCEDURAL].records
    assert any(record.metadata.get("cognition_schema") == "lesson_card.v1" for record in procedural)


def test_agent_validation_success_uses_tool_spec_contract_not_name_substring(
    tmp_path: Path,
) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    registry = ToolRegistry()
    registry.register(FailingTool())
    registry.register(VerifyThingTool())
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="First attempt.",
                tool_calls=(ToolCall(name="fail.tool", arguments={"target": "same"}),),
            ),
            LLMResponse(
                content="Verify changed strategy.",
                tool_calls=(
                    ToolCall(
                        name="verify.thing",
                        arguments={"target": "focused"},
                        strategy=StrategyProposal(
                            changed_strategy="Use a focused verification tool instead of repeating the failure.",
                            why_different="This checks a separate success signal.",
                            expected_signal="The verification passes.",
                            fallback_if_fails="Inspect verification details.",
                        ),
                    ),
                ),
            ),
            LLMResponse(content="Verification passed."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=registry,
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat("learn with custom verification", session_id="test")

    assert result.proof_of_work is not None
    assert result.proof_of_work["lessons_created"]


def test_agent_behavior_delta_preflight_disabled_preserves_tool_behavior(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    ledger = BehaviorDeltaLedger(AgentStateStore(state_path))
    ledger.record_delta(
        _active_tool_delta("delta_disabled_preflight", tool_names=("preflight.inspect",))
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    registry = ToolRegistry()
    registry.register(PreflightInspectTool())
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="Inspect preflight.",
                tool_calls=(
                    ToolCall(name="preflight.inspect", arguments={"path": "src/example.py"}),
                ),
            ),
            LLMResponse(content="Tool completed."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=registry,
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                state_path=state_path,
                enable_behavior_deltas=False,
            ),
            event_log=event_log,
        )
    )

    result = agent.chat("run the inspector", session_id="test", run_id="run_disabled")
    payload = json.loads(result.tool_executions[0].content)

    assert result.stop_reason == "complete"
    assert payload["preflight"] == ""
    assert payload["delta_ids"] == []
    assert ledger.list_activations("delta_disabled_preflight") == []
    assert "behavior_delta.preflight" not in [event.type for event in event_log.tail(limit=50)]


def test_agent_behavior_delta_preflight_enabled_reaches_tool_context_and_loop(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    ledger = BehaviorDeltaLedger(AgentStateStore(state_path))
    ledger.record_delta(
        _active_tool_delta(
            "delta_retry_preflight",
            tool_names=("preflight.inspect",),
            behavior_change=(
                "Before retrying the same validation tool with unchanged arguments, "
                "require a changed strategy or changed arguments."
            ),
        )
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    registry = ToolRegistry()
    registry.register(PreflightInspectTool())
    provider = CapturingSequenceProvider(
        [
            LLMResponse(
                content="Inspect preflight.",
                tool_calls=(
                    ToolCall(
                        name="preflight.inspect",
                        arguments={"path": "src/example.py"},
                        id="inspect-1",
                    ),
                ),
            ),
            LLMResponse(content="Preflight observed."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=provider,
            tools=registry,
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                state_path=state_path,
                enable_behavior_deltas=True,
            ),
            event_log=event_log,
        )
    )

    result = agent.chat("run the inspector", session_id="test", run_id="run_enabled")
    payload = json.loads(result.tool_executions[0].content)

    assert "TOOL BEHAVIOR-DELTA PREFLIGHT" in payload["preflight"]
    assert "changed strategy or changed arguments" in payload["preflight"]
    assert payload["delta_ids"] == ["delta_retry_preflight"]
    assert len(ledger.list_activations("delta_retry_preflight")) == 1
    assert any(event.type == "behavior_delta.preflight" for event in event_log.tail(limit=50))
    assert any(
        "TOOL BEHAVIOR-DELTA PREFLIGHT" in message.content
        for message in provider.requests[1]
        if message.role == "tool"
    )


def test_agent_auto_activates_validated_low_risk_delta_before_compile(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    ledger = BehaviorDeltaLedger(AgentStateStore(state_path))
    delta = BehaviorDelta(
        id="delta_auto_validate",
        title="Inspect validation before retry",
        kind=BehaviorDeltaKind.PROCEDURE,
        target_layer=MemoryLayer.PROCEDURAL,
        risk=BehaviorDeltaRisk.LOW,
        status=BehaviorDeltaStatus.STAGED,
        trigger=TriggerSpec(query_patterns=("validation",), task_types=("debugging",)),
        behavior_change="When validation fails, inspect the prior command before retrying.",
        evidence_refs=(
            EvidenceRef(
                source="fixture", locator="delta_auto_validate", quote="validated low-risk lesson"
            ),
        ),
        validation_plan=ValidationPlan(
            required_checks=("behavior_delta_review",),
            min_validation_score=0.6,
            min_repeat_count=1,
        ),
        metadata={"validation_score": 0.86, "repeat_count": 1},
    )
    ledger.record_delta(delta)
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider([LLMResponse(content="I will inspect first.")]),
            tools=ToolRegistry(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                state_path=state_path,
                enable_behavior_deltas=True,
                enable_auto_activate_low_risk_deltas=True,
            ),
            event_log=event_log,
        )
    )

    result = agent.chat(
        "validation failed; decide how to retry", session_id="test", run_id="run_auto"
    )

    stored = ledger.get_delta(delta.id)
    assert stored is not None
    assert stored.status == BehaviorDeltaStatus.ACTIVE
    assert "ACTIVE PROCEDURES" in result.context_prompt
    assert "inspect the prior command" in result.context_prompt
    activations = ledger.list_activations(delta.id)
    assert activations[0].activation_reason == "auto_activated_low_risk_threshold_met"
    assert activations[0].run_id == "run_auto"
    assert any(event.type == "behavior_delta.auto_activate" for event in event_log.tail(limit=50))


def test_agent_behavior_delta_policy_preflight_does_not_bypass_approval_gate(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.db"
    ledger = BehaviorDeltaLedger(AgentStateStore(state_path))
    ledger.record_delta(
        _active_tool_delta(
            "delta_shell_approval_preflight",
            kind=BehaviorDeltaKind.APPROVAL_GATE_RULE,
            target_layer=MemoryLayer.POLICY,
            risk=BehaviorDeltaRisk.HIGH,
            tool_names=("shell.run",),
            behavior_change="Before running approval-gated tools, verify exact-call approval gates remain active.",
        )
    )
    memory = build_memory_system("memory", tmp_path / "memory")
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="I need shell access.",
                tool_calls=(
                    ToolCall(
                        name="shell.run", arguments={"command": ["echo", "blocked"]}, id="shell-1"
                    ),
                ),
            ),
            LLMResponse(content="This should not run."),
        ]
    )
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=llm,
            tools=build_default_tools(),
            config=AgentConfig(
                memory_dir=tmp_path / "memory",
                log_dir=tmp_path / "logs",
                state_path=state_path,
                enable_behavior_deltas=True,
                allow_shell=True,
            ),
        )
    )

    result = agent.chat("run echo", session_id="test", run_id="run_approval")

    assert result.stop_reason == "approval_required"
    assert result.tool_executions[0].error == "approval_required"
    assert len(ledger.list_activations("delta_shell_approval_preflight")) == 1


class FailingProvider(LLMProvider):
    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del messages, tools, options
        raise ProviderError("fake outage", code="fake_provider_error", retryable=True)


class SplitSecretStreamingProvider(LLMProvider):
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del messages, tools, options
        raise AssertionError("streaming path must be used")

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> Iterator[LLMStreamEvent]:
        del messages, tools, options
        content = f"before {self.secret} after"
        split_at = len(self.secret) // 2
        yield LLMStreamEvent(type="token", content=f"before {self.secret[:split_at]}")
        yield LLMStreamEvent(type="token", content=f"{self.secret[split_at:]} after")
        yield LLMStreamEvent(
            type="message_complete",
            response=LLMResponse(
                content=content,
                raw={"provider_echo": self.secret},
            ),
        )


class SecretErrorStreamingProvider(LLMProvider):
    def __init__(self, secret: str) -> None:
        self.secret = secret

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del messages, tools, options
        raise AssertionError("streaming path must be used")

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> Iterator[LLMStreamEvent]:
        del messages, tools, options
        yield LLMStreamEvent(type="token", content="unsafe partial output")
        yield LLMStreamEvent(
            type="provider_error",
            content=f"upstream echoed {self.secret}",
            data={
                "code": "upstream_failure",
                "retryable": True,
                "detail": self.secret,
            },
        )


class CapturingProvider(LLMProvider):
    def __init__(self) -> None:
        self.messages: list[ChatMessage] = []

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del tools, options
        self.messages = list(messages)
        return LLMResponse(content="ok")


class CapturingSequenceProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[list[ChatMessage]] = []

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del tools, options
        self.requests.append(list(messages))
        if not self.responses:
            return LLMResponse(content="ok")
        return self.responses.pop(0)


class FailingTool(AgentTool):
    spec = ToolSpec(
        name="fail.tool",
        description="Always fails for cognitive-cycle tests.",
        parameters={"type": "object", "properties": {"target": {"type": "string"}}},
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        del context
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
            success=False,
            content="AssertionError: expected fixed",
            error="tool_failed",
        )


class ValidationCheckTool(AgentTool):
    spec = ToolSpec(
        name="validation.check",
        description="Succeeds as a validation step.",
        parameters={"type": "object", "properties": {"target": {"type": "string"}}},
        produces_validation=True,
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        del context
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
            success=True,
            content="validation passed",
        )


class VerifyThingTool(AgentTool):
    spec = ToolSpec(
        name="verify.thing",
        description="Succeeds as a validation step without validation in the name.",
        parameters={"type": "object", "properties": {"target": {"type": "string"}}},
        produces_validation=True,
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        del context
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
            success=True,
            content="verified the thing",
            data={"validation": {"success": True, "details": "focused check passed"}},
        )


class LongOutputTool(AgentTool):
    spec = ToolSpec(
        name="long.output",
        description="Returns output longer than the old memory boundary.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        del context
        content = "A" * 12_000 + "TAIL_SENTINEL"
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
            success=True,
            content=content,
        )


class PreflightInspectTool(AgentTool):
    spec = ToolSpec(
        name="preflight.inspect",
        description="Returns the behavior-delta preflight supplied to the tool context.",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        payload = {
            "arguments": dict(arguments),
            "preflight": context.behavior_preflight,
            "delta_ids": list(context.behavior_preflight_delta_ids),
        }
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
            success=True,
            content=json.dumps(payload, sort_keys=True),
            data=payload,
        )


def _active_tool_delta(
    delta_id: str,
    *,
    tool_names: tuple[str, ...],
    behavior_change: str = "Before running this tool, check the relevant behavior-delta preflight.",
    kind: BehaviorDeltaKind = BehaviorDeltaKind.TOOL_HEURISTIC,
    target_layer: MemoryLayer = MemoryLayer.PROCEDURAL,
    risk: BehaviorDeltaRisk = BehaviorDeltaRisk.MEDIUM,
) -> BehaviorDelta:
    return BehaviorDelta(
        id=delta_id,
        title=delta_id.replace("_", " "),
        kind=kind,
        target_layer=target_layer,
        risk=risk,
        status=BehaviorDeltaStatus.ACTIVE,
        trigger=TriggerSpec(tool_names=tool_names, risk_tags=("approval_required",)),
        behavior_change=behavior_change,
        evidence_refs=(EvidenceRef(source="fixture", locator=delta_id, quote="validated"),),
        validation_plan=ValidationPlan(
            replay_scenarios=("tool_preflight",), min_validation_score=0.8
        ),
    )
