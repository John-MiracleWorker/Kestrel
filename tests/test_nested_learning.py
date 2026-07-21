from __future__ import annotations

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayeredMemorySystem, LayerSpec
from nested_memvid_agent.models import EvidenceRef, MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.nested_learning import (
    LearningSignal,
    NestedLearningKernel,
    ValidationEvidence,
    compute_validation_score,
    resolve_validation_evidence,
)


def test_validation_evidence_score_requires_objective_evidence() -> None:
    assert compute_validation_score(ValidationEvidence(human_explicit=True)) == 0.0

    lint_only = ValidationEvidence(
        lint_refs=(EvidenceRef(source="lint.run", locator="ruff-check"),),
        human_explicit=True,
    )
    assert compute_validation_score(lint_only) == 0.3

    objective = ValidationEvidence(
        test_refs=(EvidenceRef(source="test.run", locator="pytest"),),
        lint_refs=(EvidenceRef(source="lint.run", locator="ruff"),),
        review_refs=(EvidenceRef(source="repair.review", locator="review-1"),),
    )
    assert compute_validation_score(objective) == 0.75


def test_kernel_routes_working_signal_to_episodic_context_flow() -> None:
    signal = LearningSignal(
        title="Tool failure",
        content="The build failed because the provider settings were missing.",
        kind=MemoryKind.FAILURE,
        source_layer=MemoryLayer.WORKING,
        confidence=0.55,
        validation_evidence=ValidationEvidence(
            test_refs=(EvidenceRef(source="test.run", locator="pytest -q"),),
            lint_refs=(EvidenceRef(source="lint.run", locator="ruff check"),),
            repair_refs=(EvidenceRef(source="repair.validate", locator="repair-validate"),),
        ),
    )

    decision = NestedLearningKernel().decide(signal)

    assert decision.accepted
    assert decision.target_layer == MemoryLayer.EPISODIC
    assert decision.flow.id == "working_to_episode"
    assert decision.optimizer_trace.effective_confidence > signal.confidence
    assert decision.to_payload()["promotion_requirements"]["observed_validation_evidence"]["test_refs"] == [
        "test.run:pytest -q"
    ]


def test_kernel_rejects_policy_request_without_explicit_repeated_validation() -> None:
    signal = LearningSignal(
        title="Ordinary event",
        content="A single run happened to prefer one command.",
        kind=MemoryKind.POLICY,
        source_layer=MemoryLayer.WORKING,
        requested_target_layer=MemoryLayer.POLICY,
        validation_score=0.99,
        repeat_count=1,
    )

    decision = NestedLearningKernel().decide(signal)
    payload = decision.to_payload()

    assert not decision.accepted
    assert decision.target_layer is None
    assert decision.action == "reject"
    assert payload["promotion_requirements"]["target_layer"] == "policy"
    assert payload["promotion_requirements"]["requires_explicit_instruction"] is True
    assert payload["promotion_requirements"]["min_repeat_count"] == 5
    assert payload["promotion_requirements"]["observed_repeat_count"] == 0


def test_kernel_exposes_procedural_gate_requirements_for_one_off_success() -> None:
    signal = LearningSignal(
        title="One-off repair recipe",
        content="Run pytest once after editing a file.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=0.9,
        repeat_count=1,
        requested_target_layer=MemoryLayer.PROCEDURAL,
    )

    decision = NestedLearningKernel().decide(signal)
    requirements = decision.to_payload()["promotion_requirements"]

    assert not decision.accepted
    assert requirements["target_layer"] == "procedural"
    assert requirements["min_validation_score"] == 0.78
    assert requirements["min_repeat_count"] == 2
    assert requirements["observed_repeat_count"] == 0


def test_kernel_uses_active_layer_specs_for_promotion_gates() -> None:
    custom = dict(DEFAULT_LAYER_SPECS)
    base = custom[MemoryLayer.PROCEDURAL]
    custom[MemoryLayer.PROCEDURAL] = LayerSpec(
        **{
            **base.__dict__,
            "promotion_threshold": 0.9,
            "min_repeat_count_for_promotion": 3,
        }
    )
    signal = LearningSignal(
        title="Repeated repair recipe",
        content="Run compileall and pytest after runtime-memory changes.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.EPISODIC,
        validation_evidence=ValidationEvidence(
            test_refs=(EvidenceRef(source="test.run", locator="pytest"),),
            lint_refs=(EvidenceRef(source="lint.run", locator="compileall"),),
            repair_refs=(EvidenceRef(source="repair.validate", locator="targeted"),),
            review_refs=(EvidenceRef(source="repair.review", locator="review"),),
        ),
        repeat_count=2,
        requested_target_layer=MemoryLayer.PROCEDURAL,
    )

    decision = NestedLearningKernel(specs=custom).decide(signal)

    assert not decision.accepted
    requirements = decision.to_payload()["promotion_requirements"]
    assert requirements["min_validation_score"] == 0.9
    assert requirements["min_repeat_count"] == 3


def test_kernel_builds_record_with_optimizer_metadata() -> None:
    signal = LearningSignal(
        title="Repeatable recipe",
        content="Run pytest -q after provider and runtime wiring changes.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(task_count=2),
        repeat_count=999,
    )
    kernel = NestedLearningKernel()
    decision = kernel.decide(signal)

    record = kernel.to_memory_record(signal, decision)

    assert record.layer == MemoryLayer.PROCEDURAL
    assert record.kind == MemoryKind.PROCEDURE
    assert record.metadata["frame_type"] == "skill_card"
    nested = record.metadata["nested_learning"]
    assert nested["context_flow"]["id"] == "episode_to_procedural"
    assert nested["optimizer_trace"]["repeat_count"] == 2


def test_optimizer_trace_uses_source_evidence_chars_for_compression_ratio() -> None:
    signal = LearningSignal(
        title="Compact fact",
        content="Short summary.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=0.9,
        source_evidence_chars=120,
    )

    decision = NestedLearningKernel().decide(signal)
    trace = decision.optimizer_trace.to_metadata()

    assert trace["compression_ratio"] == round(len(signal.content) / 120, 4)
    assert trace["confidence_delta_kind"] == "expected"


def test_semantic_near_miss_is_admitted_as_provisional() -> None:
    signal = LearningSignal(
        title="Near miss fact",
        content="The local workbench defaults to mock provider.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(bucket_count=3),
        repeat_count=1,
    )

    kernel = NestedLearningKernel()
    decision = kernel.decide(signal)
    record = kernel.to_memory_record(signal, decision)

    assert decision.accepted
    assert decision.action == "promote_provisional"
    assert decision.target_layer == MemoryLayer.SEMANTIC
    assert record.metadata["promotion_status"] == "provisional"
    assert record.confidence == DEFAULT_LAYER_SPECS[MemoryLayer.SEMANTIC].min_write_confidence
    assert record.expires_at is not None


def test_confirmed_followup_confirms_matching_provisional_without_duplicate(tmp_path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        InMemoryBackend,
        enforce_stable_write_integrity=False,
    )
    kernel = NestedLearningKernel(memory=memory)
    first = LearningSignal(
        title="Provider fact",
        content="The workbench provider selector defaults to mock.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(bucket_count=3),
        repeat_count=1,
    )
    first_record = _with_unsafe_test_envelope(
        kernel.to_memory_record(first, kernel.decide(first)),
        "first-source",
    )
    first_id = memory.put(first_record)
    followup = LearningSignal(
        title="Provider fact",
        content="The workbench provider selector defaults to mock.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=None,
        validation_evidence=_resolved_validation_evidence(bucket_count=4),
        repeat_count=1,
    )
    followup_record = _with_unsafe_test_envelope(
        kernel.to_memory_record(followup, kernel.decide(followup)),
        "followup-source",
    )

    followup_id = memory.put(followup_record)
    records = list(memory.iter_records(MemoryLayer.SEMANTIC))

    assert followup_id == first_id
    assert len(records) == 1
    assert records[0].metadata["promotion_status"] == "confirmed"
    assert records[0].expires_at is None


def test_provisional_record_cannot_source_promote_further() -> None:
    signal = LearningSignal(
        title="Provisional source",
        content="A provisional fact should not become a procedure.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.SEMANTIC,
        validation_score=0.9,
        repeat_count=3,
        metadata={"promotion_status": "provisional"},
    )

    decision = NestedLearningKernel().decide(signal)

    assert not decision.accepted
    assert decision.reason == "Cannot promote from provisional record; await confirmation evidence."


def test_signal_below_provisional_gate_is_rejected() -> None:
    signal = LearningSignal(
        title="Weak fact",
        content="A weak signal should not enter semantic memory.",
        kind=MemoryKind.FACT,
        source_layer=MemoryLayer.EPISODIC,
        validation_score=0.50,
        repeat_count=1,
    )

    decision = NestedLearningKernel().decide(signal)

    assert not decision.accepted
    assert decision.action == "reject"


def test_repeat_count_blocks_provisional_procedural_admission() -> None:
    signal = LearningSignal(
        title="One-off near recipe",
        content="Restart the local server after a transient failure.",
        kind=MemoryKind.PROCEDURE,
        source_layer=MemoryLayer.EPISODIC,
        requested_target_layer=MemoryLayer.PROCEDURAL,
        validation_score=0.70,
        repeat_count=1,
    )

    decision = NestedLearningKernel().decide(signal)

    assert not decision.accepted
    assert decision.to_payload()["promotion_requirements"]["min_repeat_count"] == 2


def _resolved_validation_evidence(
    *,
    bucket_count: int = 4,
    task_count: int = 1,
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
    )
    return resolve_validation_evidence(
        evidence,
        status="runtime_validated",
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
