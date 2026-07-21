from __future__ import annotations

import json
from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord, RetrievalQuery
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall, ToolExecution, ToolSpec
from nested_memvid_agent.task_capsule import write_run_capsule
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools


def test_context_pack_tool_works(tmp_path: Path) -> None:
    memory = build_memory_system(
        "memory",
        tmp_path / "memory",
        enforce_stable_write_integrity=False,
    )
    memory.put(
        MemoryRecord(
            title="Context fact",
            content="context packing retrieves compact summaries first.",
            layer=MemoryLayer.SEMANTIC,
            kind=MemoryKind.FACT,
            confidence=0.84,
            metadata={"frame_type": "section_summary"},
        )
    )

    result = build_default_tools().execute(
        ToolCall(name="context.pack", arguments={"query": "context packing", "token_budget": 1200}),
        _ctx(memory, tmp_path),
    )

    payload = json.loads(result.content)
    assert result.success
    assert payload["selected_item_count"] == 1
    assert "MV2 PSEUDO-CONTEXT PACK" in payload["packed_prompt"]


def test_context_expand_handles_missing_id_safely(tmp_path: Path) -> None:
    memory = build_memory_system("memory", tmp_path / "memory")

    result = build_default_tools().execute(
        ToolCall(name="context.expand", arguments={"frame_id": "missing"}),
        _ctx(memory, tmp_path),
    )

    assert not result.success
    assert result.error == "not_found"


def test_capsule_summarize_dry_run_works(tmp_path: Path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory")
    write_run_capsule(
        runs_dir=config.memory_dir.parent / "runs",
        run_id="run_capsule",
        objective="Summarize capsule",
        candidate_facts=("A capsule can feed candidate learning signals.",),
    )
    memory = build_memory_system("memory", config.memory_dir)

    result = build_default_tools().execute(
        ToolCall(name="capsule.summarize", arguments={"run_id": "run_capsule", "dry_run": True}),
        ToolContext(memory=memory, config=config, workspace=tmp_path),
    )

    payload = json.loads(result.content)
    assert result.success
    assert payload["dry_run"] is True
    assert payload["learning_signals"]
    assert payload["nested_learning_decisions"]


def test_capsule_apply_dry_run_plans_without_writing(tmp_path: Path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory")
    write_run_capsule(
        runs_dir=config.memory_dir.parent / "runs",
        run_id="run_apply_preview",
        objective="Preview apply",
        candidate_facts=("Preview facts should not write when dry_run is true.",),
    )
    memory = build_memory_system("memory", config.memory_dir)

    arguments = {"run_id": "run_apply_preview", "dry_run": True}
    result = build_default_tools().execute(
        ToolCall(name="capsule.apply", arguments=arguments, id="tool_apply_preview"),
        ToolContext(
            memory=memory,
            config=config,
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"tool_apply_preview"}),
            approved_tool_call_arguments={"tool_apply_preview": arguments},
        ),
    )

    payload = json.loads(result.content)
    assert result.success
    assert payload["dry_run"] is True
    assert payload["applied"] is False
    assert not memory.retrieve(RetrievalQuery(query="Preview facts", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3))


def test_capsule_apply_requires_auto_consolidation_config(tmp_path: Path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory", enable_auto_consolidation=False)
    write_run_capsule(
        runs_dir=config.memory_dir.parent / "runs",
        run_id="run_apply_disabled",
        objective="Apply disabled",
        candidate_facts=("Disabled auto consolidation must not write.",),
    )
    memory = build_memory_system("memory", config.memory_dir)

    arguments = {"run_id": "run_apply_disabled"}
    result = build_default_tools().execute(
        ToolCall(name="capsule.apply", arguments=arguments, id="tool_apply_disabled"),
        ToolContext(
            memory=memory,
            config=config,
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"tool_apply_disabled"}),
            approved_tool_call_arguments={"tool_apply_disabled": arguments},
        ),
    )

    assert not result.success
    assert result.error == "auto_consolidation_disabled"


def test_capsule_apply_requests_approval_when_enabled(tmp_path: Path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory", enable_auto_consolidation=True)
    write_run_capsule(
        runs_dir=config.memory_dir.parent / "runs",
        run_id="run_apply_approval",
        objective="Apply approval",
        candidate_facts=("Enabled auto consolidation still asks for approval.",),
    )
    memory = build_memory_system("memory", config.memory_dir)

    def approval_handler(call: ToolCall, spec: ToolSpec, context: ToolContext) -> ToolExecution:
        del spec, context
        return ToolExecution(call=call, success=False, content="approval pending", error="approval_pending")

    result = build_default_tools().execute(
        ToolCall(name="capsule.apply", arguments={"run_id": "run_apply_approval"}, id="tool_apply"),
        ToolContext(memory=memory, config=config, workspace=tmp_path, approval_handler=approval_handler),
    )

    assert not result.success
    assert result.error == "approval_pending"


def test_capsule_apply_after_approval_stages_unvalidated_fact_and_blocks_policy(
    tmp_path: Path,
) -> None:
    config = AgentConfig(
        memory_dir=tmp_path / "memory",
        enable_auto_consolidation=True,
        allow_policy_writes=True,
    )
    write_run_capsule(
        runs_dir=config.memory_dir.parent / "runs",
        run_id="run_apply_write",
        objective="Apply write",
        candidate_facts=("Approved capsule apply should write this validated fact.",),
        candidate_policy_items=("Policy candidates from capsules still require explicit instruction.",),
    )
    memory = build_memory_system("memory", config.memory_dir)

    arguments = {"run_id": "run_apply_write", "include_policy": True}
    result = build_default_tools().execute(
        ToolCall(name="capsule.apply", arguments=arguments, id="tool_apply"),
        ToolContext(
            memory=memory,
            config=config,
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"tool_apply"}),
            approved_tool_call_arguments={"tool_apply": arguments},
        ),
    )

    payload = json.loads(result.content)
    assert result.success
    assert payload["applied"] is True
    assert not memory.retrieve(
        RetrievalQuery(
            query="Approved capsule apply",
            layers=(MemoryLayer.SEMANTIC,),
            k_per_layer=3,
        )
    )
    staged_hits = memory.retrieve(
        RetrievalQuery(
            query="Approved capsule apply",
            layers=(MemoryLayer.EPISODIC,),
            k_per_layer=3,
        )
    )
    assert staged_hits
    staged = staged_hits[0].record
    assert staged.metadata["capsule_apply_status"] == "unvalidated_episodic_staging"
    assert staged.metadata["actual_layer"] == "episodic"
    assert staged.metadata["requested_stable_layer"] == "semantic"
    assert staged.metadata["validation_status"] == "unresolved"
    assert staged.metadata["stable_recall_eligible"] is False
    assert staged.evidence[0].source == "task_capsule"
    fact_decision = next(
        item for item in payload["decisions"] if item["signal_kind"] == "fact"
    )
    assert fact_decision["write_mode"] == "unvalidated_episodic_staging"
    assert fact_decision["actual_layer"] == "episodic"
    assert fact_decision["requested_stable_layer"] == "semantic"
    assert fact_decision["validation_status"] == "unresolved"
    assert fact_decision["stable_promotion_blocked"] == "authenticated_validation_required"
    assert not memory.retrieve(
        RetrievalQuery(query="Policy candidates", layers=(MemoryLayer.POLICY,), k_per_layer=3)
    )
    policy_decisions = [item for item in payload["decisions"] if item["requested_target_layer"] == "policy"]
    assert policy_decisions[0]["blocked"] == "policy_requires_explicit_instruction"


def test_capsule_apply_rejects_changed_arguments_after_approval(tmp_path: Path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "memory", enable_auto_consolidation=True)
    write_run_capsule(
        runs_dir=config.memory_dir.parent / "runs",
        run_id="run_apply_original",
        objective="Original apply",
        candidate_facts=("Only the originally approved capsule may be applied.",),
    )
    write_run_capsule(
        runs_dir=config.memory_dir.parent / "runs",
        run_id="run_apply_changed",
        objective="Changed apply",
        candidate_facts=("Changed capsule arguments must not write memory.",),
    )
    memory = build_memory_system("memory", config.memory_dir)

    result = build_default_tools().execute(
        ToolCall(name="capsule.apply", arguments={"run_id": "run_apply_changed"}, id="tool_apply_exact"),
        ToolContext(
            memory=memory,
            config=config,
            workspace=tmp_path,
            approved_tool_call_ids=frozenset({"tool_apply_exact"}),
            approved_tool_call_arguments={"tool_apply_exact": {"run_id": "run_apply_original"}},
        ),
    )

    assert not result.success
    assert result.error == "approval_required"
    assert not memory.retrieve(
        RetrievalQuery(query="Changed capsule arguments", layers=(MemoryLayer.SEMANTIC,), k_per_layer=3)
    )


def test_memory_conflicts_returns_structured_output(tmp_path: Path) -> None:
    memory = build_memory_system(
        "memory",
        tmp_path / "memory",
        enforce_stable_write_integrity=False,
    )
    for title, content in [
        ("Flag state", "feature flag omega is enabled."),
        ("Flag state correction", "feature flag omega is not enabled."),
    ]:
        memory.put(
            MemoryRecord(
                title=title,
                content=content,
                layer=MemoryLayer.SEMANTIC,
                kind=MemoryKind.FACT,
                confidence=0.86,
                metadata={"conflict_group_id": "omega"},
            )
        )

    result = build_default_tools().execute(
        ToolCall(name="memory.conflicts", arguments={"query": "feature flag omega", "k": 5}),
        _ctx(memory, tmp_path),
    )

    payload = json.loads(result.content)
    assert result.success
    assert payload["possible_conflicts"]
    assert payload["conflict_warnings"]


def _ctx(memory: object, tmp_path: Path) -> ToolContext:
    return ToolContext(
        memory=memory,
        config=AgentConfig(memory_dir=tmp_path / "memory"),
        workspace=tmp_path,
    )
