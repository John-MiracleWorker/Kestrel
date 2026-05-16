from __future__ import annotations

from nested_memvid_agent.models import MemoryKind, MemoryLayer
from nested_memvid_agent.nested_learning import LearningSignal, NestedLearningKernel


def test_kernel_routes_working_signal_to_episodic_context_flow() -> None:
    signal = LearningSignal(
        title="Tool failure",
        content="The build failed because the provider settings were missing.",
        kind=MemoryKind.FAILURE,
        source_layer=MemoryLayer.WORKING,
        confidence=0.55,
        validation_score=0.72,
    )

    decision = NestedLearningKernel().decide(signal)

    assert decision.accepted
    assert decision.target_layer == MemoryLayer.EPISODIC
    assert decision.flow.id == "working_to_episode"
    assert decision.optimizer_trace.effective_confidence > signal.confidence


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
