from __future__ import annotations

from nested_memvid_agent.consolidation import Consolidator
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord


def test_working_promotes_to_episodic_after_validation() -> None:
    record = MemoryRecord(
        title="Tool result",
        content="A test failed because the auth profile path was wrong.",
        layer=MemoryLayer.WORKING,
        kind=MemoryKind.FAILURE,
        confidence=0.5,
    )
    consolidator = Consolidator()
    candidate = consolidator.propose(record, validation_score=0.7)
    assert candidate is not None
    assert candidate.target_layer == MemoryLayer.EPISODIC
    promoted = consolidator.promote(candidate)
    assert promoted.layer == MemoryLayer.EPISODIC
    assert promoted.evidence[-1].source == "consolidator"
    assert promoted.metadata["nested_learning"]["context_flow"]["id"] == "working_to_episode"
    assert "optimizer_trace" in promoted.metadata["nested_learning"]


def test_procedural_to_policy_requires_many_repeats() -> None:
    record = MemoryRecord(
        title="Debug recipe",
        content="Inspect provider-specific auth profiles before editing global shell config.",
        layer=MemoryLayer.PROCEDURAL,
        kind=MemoryKind.PROCEDURE,
        confidence=0.9,
    )
    consolidator = Consolidator()
    assert consolidator.propose(record, validation_score=0.96, repeat_count=5) is None
    candidate = consolidator.propose(record, validation_score=0.98, repeat_count=5, explicit_instruction=True)
    assert candidate is not None
    assert candidate.target_layer == MemoryLayer.POLICY


def test_ordinary_episode_does_not_become_policy() -> None:
    record = MemoryRecord(
        title="Single user preference",
        content="Use the blue button first.",
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.EVENT,
        confidence=0.9,
    )

    candidate = Consolidator().propose(record, validation_score=0.99, repeat_count=99)

    assert candidate is not None
    assert candidate.target_layer == MemoryLayer.SEMANTIC
