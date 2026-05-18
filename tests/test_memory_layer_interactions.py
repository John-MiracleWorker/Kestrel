from __future__ import annotations

import json
from pathlib import Path

from nested_memvid_agent.agent import AgentDependencies, NestedMV2Agent
from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.context_compiler import ContextCompiler, ContextCompilerConfig
from nested_memvid_agent.context_packer import ContextPacker, ContextPackRequest
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.nested_learning import LearningSignal, NestedLearningKernel
from nested_memvid_agent.promotion_ledger import PromotionLedger
from nested_memvid_agent.runtime_models import LLMResponse, ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.state_store import AgentStateStore
from nested_memvid_agent.tools.base import AgentTool, ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.tools.registry import ToolRegistry


def test_memory_correct_tombstones_target_and_records_ledger_outcomes(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    ledger = PromotionLedger(state)
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend, ledger=ledger)
    target_id = memory.put(
            _promoted_record(
                promotion_id="promotion-corrected",
                title="Correctable fact",
                content="sentinel_old_correction_tombstone_19b says the old value is true.",
            )
        )

    result = build_default_tools().execute(
        ToolCall(
            name="memory.correct",
            arguments={
                "target_record_id": target_id,
                "correction_text": "sentinel_new_correction_tombstone_19b says the corrected value is true.",
                "evidence": [{"source": "test", "locator": "memory-layer-interaction"}],
            },
        ),
        ToolContext(memory=memory, config=AgentConfig(state_path=tmp_path / "state.db"), workspace=tmp_path),
    )

    assert result.success
    target = memory.get_record(MemoryLayer.SEMANTIC, target_id, include_inactive=True)
    assert target is not None
    assert target.metadata["active"] is False
    assert target.metadata["tombstone_reason"] == "corrected"
    assert target.metadata["superseded_by"] == result.data["correction_record_id"]
    assert memory.retrieve(RetrievalQuery(query="sentinel_old_correction_tombstone_19b", layers=(MemoryLayer.SEMANTIC,))) == []
    audit_hits = memory.retrieve(
        RetrievalQuery(query="sentinel_old_correction_tombstone_19b", layers=(MemoryLayer.SEMANTIC,), include_inactive=True)
    )
    assert audit_hits and audit_hits[0].record.id == target_id
    summary = ledger.summarize()
    row = summary.rows[0]
    assert row.outcome_counts["corrected"] == 1
    assert row.outcome_counts["tombstoned"] == 1


def test_agent_to_promotion_to_later_context_flow_keeps_policy_untouched(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend)
    registry = ToolRegistry()
    registry.register(EvalObservationTool())
    agent = NestedMV2Agent(
        AgentDependencies(
            memory=memory,
            llm=MockLLMProvider(
                [
                    LLMResponse(
                        content="I will capture the tool result.",
                        tool_calls=(ToolCall(name="eval.observe", arguments={}),),
                    ),
                    LLMResponse(content="Captured sentinel_cross_layer_flow_58bc final answer."),
                ]
            ),
            tools=registry,
            config=AgentConfig(memory_dir=tmp_path / "memory", log_dir=tmp_path / "logs"),
        )
    )

    result = agent.chat("Please process sentinel_cross_layer_flow_58bc.", session_id="session-cross", run_id="run-cross")
    kernel = NestedLearningKernel(memory=memory)
    signal = LearningSignal(
        title="Cross-layer promoted fact",
        content="sentinel_cross_layer_flow_58bc is a validated durable fact for later context.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=0.82,
        repeat_count=1,
        source="test.agent_cross_layer",
        locator=result.run_id,
    )
    decision = kernel.decide(signal)
    promoted = kernel.to_memory_record(signal, decision)
    promoted_id = memory.put(promoted)

    compiled = ContextCompiler(
        memory,
        config=ContextCompilerConfig(total_budget_chars=2500, context_pack_token_budget=650, max_hits_per_layer=1),
    ).compile(
        "Use sentinel_cross_layer_flow_58bc later.",
        query="sentinel_cross_layer_flow_58bc durable fact",
    )
    packed = ContextPacker(memory).pack(
        ContextPackRequest(
            objective="Use sentinel_cross_layer_flow_58bc later.",
            query="sentinel_cross_layer_flow_58bc durable fact",
            token_budget=650,
            k_per_layer=1,
        )
    )

    assert result.stop_reason == "complete"
    assert len(memory.backends[MemoryLayer.WORKING].records) >= 2
    assert len(memory.backends[MemoryLayer.EPISODIC].records) >= 1
    assert len(memory.backends[MemoryLayer.POLICY].records) == 0
    stored = memory.get_record(MemoryLayer.SEMANTIC, promoted_id)
    assert stored is not None
    assert stored.metadata["source_layer"] == "episodic"
    assert stored.metadata["validation_score"] == 0.82
    assert stored.metadata["repeat_count"] == 1
    assert stored.metadata["promotion_status"] == "confirmed"
    assert stored.metadata["promotion_id"]
    assert "Cross-layer promoted fact" in compiled.prompt
    assert [item.frame.layer for item in packed.items[:1]] == [MemoryLayer.SEMANTIC]


def test_memory_ledger_tool_reports_outcomes_without_mutating_thresholds(tmp_path: Path) -> None:
    state_path = tmp_path / "state.db"
    state = AgentStateStore(state_path)
    ledger = PromotionLedger(state)
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend, ledger=ledger)
    original_threshold = memory.specs[MemoryLayer.SEMANTIC].promotion_threshold
    record_id = memory.put(
        _promoted_record(
            promotion_id="promotion-ledger-tool",
            title="Ledger fact",
            content="sentinel_ledger_tool_77cd records useful and corrected outcomes.",
        )
    )
    memory.record_promotion_outcome("promotion-ledger-tool", "useful", evidence_record_id=record_id)
    memory.record_promotion_outcome("promotion-ledger-tool", "corrected", evidence_record_id="correction-ledger")

    result = build_default_tools().execute(
        ToolCall(name="memory.ledger", arguments={"target_layer": "semantic"}),
        ToolContext(memory=memory, config=AgentConfig(state_path=state_path), workspace=tmp_path),
    )

    assert result.success
    payload = json.loads(result.content)
    assert payload["target_layer"] == "semantic"
    assert payload["rows"][0]["outcomes"]["useful"] == 1
    assert payload["rows"][0]["outcomes"]["corrected"] == 1
    assert memory.specs[MemoryLayer.SEMANTIC].promotion_threshold == original_threshold


def test_capsule_apply_dry_run_does_not_mutate_memory_and_apply_respects_config(tmp_path: Path) -> None:
    from nested_memvid_agent.task_capsule import write_run_capsule

    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend)
    write_run_capsule(
        runs_dir=tmp_path / "runs",
        run_id="capsule-dry-run",
        objective="Summarize deterministic capsule.",
        backend="memory",
        candidate_facts=("sentinel_capsule_apply_63cc should become semantic only when applied.",),
    )
    registry = build_default_tools()
    dry_args = {"run_id": "capsule-dry-run", "dry_run": True}
    apply_args = {"run_id": "capsule-dry-run", "dry_run": False}
    context = ToolContext(
        memory=memory,
        config=AgentConfig(memory_dir=tmp_path / "memory", enable_auto_consolidation=False),
        workspace=tmp_path,
        approved_tool_call_ids=frozenset({"capsule-dry", "capsule-apply"}),
        approved_tool_call_arguments={"capsule-dry": dry_args, "capsule-apply": apply_args},
    )

    dry_run = registry.execute(
        ToolCall(name="capsule.apply", arguments=dry_args, id="capsule-dry"),
        context,
    )
    blocked = registry.execute(
        ToolCall(name="capsule.apply", arguments=apply_args, id="capsule-apply"),
        context,
    )

    assert dry_run.success
    assert dry_run.data["applied"] is False
    assert not list(memory.iter_records(MemoryLayer.SEMANTIC))
    assert blocked.success is False
    assert blocked.error == "auto_consolidation_disabled"
    assert not list(memory.iter_records(MemoryLayer.SEMANTIC))


class EvalObservationTool(AgentTool):
    spec = ToolSpec(
        name="eval.observe",
        description="Return a deterministic observation for memory layer interaction tests.",
        parameters={"type": "object", "properties": {}},
    )

    def run(self, arguments: dict[str, object], context: ToolContext) -> ToolExecution:
        del context
        return ToolExecution(
            call=ToolCall(name=self.spec.name, arguments=dict(arguments)),
            success=True,
            content="sentinel_cross_layer_flow_58bc tool result validates the later semantic fact.",
        )


def _promoted_record(*, promotion_id: str, title: str, content: str) -> MemoryRecord:
    return MemoryRecord(
        title=title,
        content=content,
        layer=MemoryLayer.SEMANTIC,
        kind=MemoryKind.FACT,
        confidence=0.9,
        metadata={
            "promotion_id": promotion_id,
            "promotion_status": "confirmed",
            "source_layer": MemoryLayer.EPISODIC.value,
            "validation_score": 0.9,
            "repeat_count": 2,
            "explicit_instruction": False,
            "nested_learning": {
                "context_flow": {"source_layers": [MemoryLayer.EPISODIC.value]},
                "decision": {"reason": "test promotion"},
                "optimizer_trace": {"validation_score": 0.9},
            },
        },
    )
