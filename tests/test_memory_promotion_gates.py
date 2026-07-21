from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem, LayerSpec
from nested_memvid_agent.models import (
    EvidenceRef,
    MemoryKind,
    MemoryLayer,
    MemoryRecord,
    RetrievalQuery,
)
from nested_memvid_agent.nested_learning import (
    LearningSignal,
    NestedLearningKernel,
    ValidationEvidence,
    ValidationStatus,
    resolve_validation_evidence,
)
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools


def test_write_thresholds_accept_working_but_reject_low_confidence_stable_layers(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )

    working_id = memory.put(
        MemoryRecord(
            title="Working threshold",
            content="sentinel_working_threshold_7f3a can enter working memory.",
            layer=MemoryLayer.WORKING,
            confidence=0.21,
        )
    )

    assert working_id
    for layer in (MemoryLayer.SEMANTIC, MemoryLayer.PROCEDURAL, MemoryLayer.SELF, MemoryLayer.POLICY):
        spec = memory.specs[layer]
        try:
            memory.put(
                MemoryRecord(
                    title=f"Low confidence {layer.value}",
                    content=f"sentinel_low_confidence_{layer.value} must be rejected.",
                    layer=layer,
                    kind=MemoryKind.POLICY if layer == MemoryLayer.POLICY else MemoryKind.FACT,
                    confidence=max(spec.min_write_confidence - 0.01, 0.0),
                )
            )
        except ValueError as exc:
            assert "below" in str(exc)
        else:  # pragma: no cover - assertion message is clearer than a bare failure
            raise AssertionError(f"{layer.value} accepted a below-threshold write")


def test_direct_memory_write_cannot_write_policy_even_when_policy_writes_are_enabled(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend)
    registry = build_default_tools()
    arguments = {
        "layer": "policy",
        "kind": "policy",
        "title": "Direct policy block",
        "content": "sentinel_policy_block_9c2e direct policy write must stay blocked.",
        "confidence": 1.0,
    }

    result = registry.execute(
        ToolCall(name="memory.write", arguments=arguments),
        ToolContext(memory=memory, config=AgentConfig(allow_policy_writes=True), workspace=tmp_path),
    )

    assert result.success is False
    assert result.error == "stable_memory_write_rejected"
    assert memory.retrieve(RetrievalQuery(query="sentinel_policy_block_9c2e", layers=(MemoryLayer.POLICY,))) == []


def test_procedural_gate_requires_repeated_validated_success(tmp_path: Path) -> None:
    kernel = NestedLearningKernel()
    one_off = LearningSignal(
        title="One-off recipe",
        content="sentinel_procedural_repeat_gate_1a8b recipe from one success.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(task_count=1),
        repeat_count=1,
        requested_target_layer=MemoryLayer.PROCEDURAL,
    )
    repeated = LearningSignal(
        title="Repeated recipe",
        content="sentinel_procedural_repeat_gate_1a8b recipe from repeated success.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(task_count=2),
        repeat_count=999,
        requested_target_layer=MemoryLayer.PROCEDURAL,
    )

    rejected = kernel.decide(one_off)
    accepted = kernel.decide(repeated)

    assert not rejected.accepted
    assert rejected.promotion_requirements["min_repeat_count"] == 2
    assert accepted.accepted
    assert accepted.target_layer == MemoryLayer.PROCEDURAL


def test_self_memory_requires_self_appropriate_validated_signal() -> None:
    ordinary = LearningSignal(
        title="Ordinary self-like event",
        content="A one-off event mentions a user workflow preference.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=0.7,
        repeat_count=1,
        requested_target_layer=MemoryLayer.SELF,
    )
    validated = LearningSignal(
        title="Validated workflow preference",
        content="The user explicitly prefers implementation after a plan is accepted.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(
            task_count=1,
            human_explicit=True,
            status="human_confirmed",
        ),
        repeat_count=1,
        explicit_instruction=True,
        requested_target_layer=MemoryLayer.SELF,
        metadata={"self_schema": "user_workflow_preference"},
    )

    ordinary_decision = NestedLearningKernel().decide(ordinary)
    validated_decision = NestedLearningKernel().decide(validated)
    record = NestedLearningKernel().to_memory_record(validated, validated_decision)

    assert not ordinary_decision.accepted
    assert validated_decision.accepted
    assert record.layer == MemoryLayer.SELF
    assert record.metadata["source_layer"] == MemoryLayer.EPISODIC.value
    assert record.metadata["validation_score"] == 1.0


def test_policy_promotion_matrix_and_tool_config_gate(tmp_path: Path) -> None:
    kernel = NestedLearningKernel()
    cases = [
        LearningSignal(
            title="High score without explicit instruction",
            content="Never write this from an ordinary event.",
            kind=MemoryKind.POLICY,
            source_layer=MemoryLayer.PROCEDURAL,
            validation_score=0.99,
            repeat_count=5,
            explicit_instruction=False,
            requested_target_layer=MemoryLayer.POLICY,
        ),
        LearningSignal(
            title="Explicit but not repeated",
            content="Policy needs repeated evidence.",
            kind=MemoryKind.POLICY,
            source_layer=MemoryLayer.PROCEDURAL,
            validation_score=0.99,
            repeat_count=1,
            explicit_instruction=True,
            requested_target_layer=MemoryLayer.POLICY,
        ),
        LearningSignal(
            title="Repeated explicit below threshold",
            content="Policy also needs high validation.",
            kind=MemoryKind.POLICY,
            source_layer=MemoryLayer.PROCEDURAL,
            validation_score=0.8,
            repeat_count=5,
            explicit_instruction=True,
            requested_target_layer=MemoryLayer.POLICY,
        ),
    ]

    assert [kernel.decide(case).accepted for case in cases] == [False, False, False]

    memory = LayeredMemorySystem.from_backend_factory(tmp_path / "memory", InMemoryBackend)
    registry = build_default_tools()
    arguments = {
        "title": "Approved policy candidate",
        "content": "sentinel_policy_approved_path_31ee must only write through memory.learn.",
        "kind": "policy",
        "source_layer": "procedural",
        "target_layer": "policy",
        "validation_score": 0.99,
        "repeat_count": 5,
        "explicit_instruction": True,
        "confidence": 0.98,
    }

    disabled = registry.execute(
        ToolCall(name="memory.learn", arguments=arguments),
        ToolContext(memory=memory, config=AgentConfig(allow_policy_writes=False), workspace=tmp_path),
    )
    enabled = registry.execute(
        ToolCall(name="memory.learn", arguments=arguments),
        ToolContext(memory=memory, config=AgentConfig(allow_policy_writes=True), workspace=tmp_path),
    )

    assert disabled.success is False
    assert disabled.error == "policy_write_disabled"
    assert enabled.success is False
    assert enabled.error == "policy_approval_required"
    hits = memory.retrieve(RetrievalQuery(query="sentinel_policy_approved_path_31ee", layers=(MemoryLayer.POLICY,)))
    assert hits == []


def test_provisional_records_are_visible_but_cannot_promote_further_and_confirm_without_duplicate(tmp_path: Path) -> None:
    custom = dict(DEFAULT_LAYER_SPECS)
    semantic = custom[MemoryLayer.SEMANTIC]
    custom[MemoryLayer.SEMANTIC] = LayerSpec(**{**semantic.__dict__, "provisional_threshold": 0.64})
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        InMemoryBackend,
        specs=custom,
        enforce_stable_write_integrity=False,
    )
    kernel = NestedLearningKernel(specs=custom, memory=memory)
    provisional = LearningSignal(
        title="Provisional fact slot",
        content="sentinel_provisional_slot_22aa starts as provisional.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(bucket_count=3),
        repeat_count=1,
    )
    confirmed = LearningSignal(
        title="Provisional fact slot",
        content="sentinel_provisional_slot_22aa starts as provisional.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(bucket_count=4),
        repeat_count=1,
    )

    provisional_record = _with_unsafe_test_envelope(
        kernel.to_memory_record(provisional, kernel.decide(provisional)),
        "provisional-source",
    )
    provisional_id = memory.put(provisional_record)
    blocked = kernel.decide(
        LearningSignal(
            title="Do not promote provisional",
            content="A provisional source should not become a procedure.",
            kind=MemoryKind.PROCEDURE,
            source_layer=MemoryLayer.SEMANTIC,
            validation_score=0.95,
            repeat_count=3,
            metadata={"promotion_status": "provisional"},
        )
    )
    confirmed_id = memory.put(
        _with_unsafe_test_envelope(
            kernel.to_memory_record(confirmed, kernel.decide(confirmed)),
            "confirmed-source",
        )
    )

    hits = memory.retrieve(RetrievalQuery(query="sentinel_provisional_slot_22aa", layers=(MemoryLayer.SEMANTIC,)))
    records = list(memory.iter_records(MemoryLayer.SEMANTIC))
    assert hits
    assert not blocked.accepted
    assert confirmed_id == provisional_id
    assert len(records) == 1
    assert records[0].metadata["promotion_status"] == "confirmed"


def _resolved_validation_evidence(
    *,
    bucket_count: int = 4,
    task_count: int = 1,
    human_explicit: bool = False,
    status: ValidationStatus = "runtime_validated",
) -> ValidationEvidence:
    bucket_refs = tuple(
        EvidenceRef(source="memory_record", locator=f"receipt-bucket-{index}")
        for index in range(bucket_count)
    )
    task_refs = tuple(
        EvidenceRef(source="memory_record", locator=f"receipt-task-{index}")
        for index in range(task_count)
    )
    buckets = [(), (), (), ()]
    for index, ref in enumerate(bucket_refs):
        buckets[index] = (ref,)
    evidence = ValidationEvidence(
        test_refs=buckets[0],
        lint_refs=buckets[1],
        repair_refs=buckets[2],
        review_refs=buckets[3],
        task_refs=task_refs,
        human_explicit=human_explicit,
    )
    return resolve_validation_evidence(
        evidence,
        status=status,
        artifact_ids=tuple(ref.locator for ref in (*bucket_refs, *task_refs)),
    )


def _with_unsafe_test_envelope(record: MemoryRecord, source_id: str) -> MemoryRecord:
    record.metadata["stable_write_envelope"] = {
        "version": 1,
        "authority": "nested_learning",
        "target_layer": record.layer.value,
        "source_record_ids": [source_id],
        "evidence_resolved": True,
    }
    record.evidence.append(EvidenceRef(source="memory_record", locator=source_id))
    return record
