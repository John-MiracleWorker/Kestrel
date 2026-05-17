from __future__ import annotations

from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS, LayerSpec
from nested_memvid_agent.models import EvidenceRef, MemoryKind, MemoryLayer
from nested_memvid_agent.nested_learning import (
    LearningSignal,
    NestedLearningKernel,
    ValidationEvidence,
    compute_validation_score,
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
    assert payload["promotion_requirements"]["observed_repeat_count"] == 1


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
    assert requirements["observed_repeat_count"] == 1


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
        validation_score=0.9,
        repeat_count=2,
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
