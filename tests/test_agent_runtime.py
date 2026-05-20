from __future__ import annotations

from pathlib import Path
import json

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
from nested_memvid_agent.self_profile import (
    build_onboarding_profile,
    onboarding_record_content,
    soul_communication_contract_from_hits,
)
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry
from nested_memvid_agent.state_store import AgentStateStore


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
    summary_record = next(record for record in episodic_records if record.title == "Conversation turn summary")
    assert user_record.metadata["frame_type"] == "raw_chunk"
    assert summary_record.metadata["frame_type"] == "session_summary"
    assert user_record.metadata["parent_ids"] == [summary_record.id]
    assert user_record.id in summary_record.metadata["child_ids"]


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
    assert not [record for record in memory.backends[MemoryLayer.WORKING].records if record.kind == MemoryKind.CORRECTION]

    agent.chat("to clarify: the version is 5.5, not 5.0", session_id="test")
    corrections = [record for record in memory.backends[MemoryLayer.WORKING].records if record.kind == MemoryKind.CORRECTION]
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
    tool_record = next(record for record in memory.backends[MemoryLayer.WORKING].records if record.title == "Tool result: long.output")
    assert "TAIL_SENTINEL" in tool_record.content
    assert len(tool_record.content) > 12_000
    summary_record = next(record for record in memory.backends[MemoryLayer.EPISODIC].records if record.title == "Conversation turn summary")
    assert "long.output succeeded" in summary_record.content
    assert len(summary_record.content) < 1600


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
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs", allow_shell=True),
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


def test_agent_direct_approval_requires_exact_arguments(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")
    call = ToolCall(name="shell.run", arguments={"command": ["echo", "direct-exact"]}, id="direct_shell")
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
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs", allow_shell=True),
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
            config=AgentConfig(memory_dir=tmp_path / "memory-exact", log_dir=tmp_path / "logs-exact", allow_shell=True),
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
    diagnosis_events = [event for event in event_log.tail(limit=50) if event.type == "diagnosis.classified"]
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
    assert any("Prior Failure Lessons" in message.content for message in provider.messages if message.role == "system")


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

    assert [execution.error for execution in result.tool_executions] == ["tool_failed", "retry_blocked"]
    assert result.proof_of_work is not None
    assert result.proof_of_work["failures"]
    episodic = memory.backends[MemoryLayer.EPISODIC].records
    assert any(record.metadata.get("cognition_schema") == "failure_episode.v1" for record in episodic)
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


def test_agent_validation_success_uses_tool_spec_contract_not_name_substring(tmp_path: Path) -> None:
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
    ledger.record_delta(_active_tool_delta("delta_disabled_preflight", tool_names=("preflight.inspect",)))
    memory = build_memory_system("memory", tmp_path / "memory")
    event_log = JsonlEventLog(tmp_path / "logs" / "events.jsonl")
    registry = ToolRegistry()
    registry.register(PreflightInspectTool())
    llm = MockLLMProvider(
        [
            LLMResponse(
                content="Inspect preflight.",
                tool_calls=(ToolCall(name="preflight.inspect", arguments={"path": "src/example.py"}),),
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


def test_agent_behavior_delta_preflight_enabled_reaches_tool_context_and_loop(tmp_path: Path) -> None:
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
                tool_calls=(ToolCall(name="preflight.inspect", arguments={"path": "src/example.py"}, id="inspect-1"),),
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


def test_agent_behavior_delta_policy_preflight_does_not_bypass_approval_gate(tmp_path: Path) -> None:
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
                tool_calls=(ToolCall(name="shell.run", arguments={"command": ["echo", "blocked"]}, id="shell-1"),),
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
        return ToolExecution(call=ToolCall(name=self.spec.name, arguments=dict(arguments)), success=True, content=content)


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
        validation_plan=ValidationPlan(replay_scenarios=("tool_preflight",), min_validation_score=0.8),
    )
