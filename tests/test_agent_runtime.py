from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.models import MemoryLayer
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import LLMResponse, LLMStreamEvent, ToolCall
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


def test_agent_executes_tool_call_and_continues(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
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
        )
    )

    result = agent.chat("find needle", session_id="test")

    assert result.assistant_message == "I checked memory."
    assert len(result.tool_executions) == 1
    assert result.tool_executions[0].success


def test_agent_stops_on_direct_approval_required(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
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
        )
    )

    result = agent.chat("run echo", session_id="test")

    assert result.stop_reason == "approval_required"
    assert result.assistant_message == "I need shell access."
    assert len(result.tool_executions) == 1
    assert result.tool_executions[0].error == "approval_required"


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
