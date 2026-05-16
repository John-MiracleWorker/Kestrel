from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_log import JsonlEventLog
from nested_memvid_agent.llm.base import LLMProvider, ProviderError
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.models import MemoryKind, MemoryLayer
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import (
    ChatMessage,
    LLMOptions,
    LLMResponse,
    LLMStreamEvent,
    ToolCall,
    ToolSpec,
)
from nested_memvid_agent.tools.builtin import build_default_tools


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
    summary_record = next(record for record in episodic_records if record.title == "Conversation turn summary")
    assert user_record.metadata["frame_type"] == "raw_chunk"
    assert summary_record.metadata["frame_type"] == "session_summary"
    assert user_record.metadata["parent_ids"] == [summary_record.id]
    assert user_record.id in summary_record.metadata["child_ids"]


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


def test_agent_stops_on_direct_approval_required(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="I need shell access.",
                tool_calls=(ToolCall(name="shell.run", arguments={"command": ["echo", "blocked"]}),),
            ),
            LLMResponse(content="This should not run."),
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


def test_agent_streams_mock_tokens_without_losing_final_message(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider([LLMResponse(content="streamed hello")]),
            tools=build_default_tools(),
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs", stream=True),
        )
    )
    events: list[LLMStreamEvent] = []

    result = agent.chat("hello", session_id="test", stream_handler=events.append)

    assert result.assistant_message == "streamed hello"
    assert [event.content for event in events if event.type == "token"] == ["streamed hello"]
    assert any(event.type == "message_complete" for event in events)


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


class FailingProvider(LLMProvider):
    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del messages, tools, options
        raise ProviderError("fake outage", code="fake_provider_error", retryable=True)
