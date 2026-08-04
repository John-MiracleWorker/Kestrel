"""No-policy-memory proof (Adaptive Flock plan, Task 21).

A completed Adaptive Flock qualification run, its terminal receipt, scope
activation, and the learned routing decisions that follow must write zero
memvid records and emit zero policy signals.  The spy wires three hooks at
the authoritative boundaries:

* every ``put`` through a fully wired :class:`LayeredMemorySystem` lands in
  ``MemorySpy.writes``;
* every :class:`NestedLearningKernel` decision for a signal that targets the
  policy layer lands in ``MemorySpy.policy_signals``;
* every :class:`MutationGate` evaluation (the policy-mutation path) lands in
  ``MemorySpy.policy_signals``.

The learned route is verified to be genuinely active before the zero-write
assertions so the test cannot pass vacuously.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_flock_grant_runtime import (
    GrantHarness,
    _configured_ledger,
    _create_task,
    _learned_coordinator,
    _train_learned_winner,
)

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryLayer, MemoryRecord
from nested_memvid_agent.mutation_gate import MutationGate
from nested_memvid_agent.nested_learning import LearningSignal, NestedLearningKernel
from nested_memvid_agent.routing.contracts import compile_task_contract
from nested_memvid_agent.state_store import AgentStateStore


class MemorySpy:
    """Records every memory write and every policy-directed signal."""

    def __init__(self, memory: LayeredMemorySystem) -> None:
        self.memory = memory
        self.writes: list[MemoryRecord] = []
        self.policy_signals: list[object] = []


def _spy_backend_factory(spy_holder: dict[str, MemorySpy]):
    def factory(*, path: Path, layer: MemoryLayer, **kwargs: object) -> InMemoryBackend:
        backend = InMemoryBackend(path=path, layer=layer, **kwargs)
        original_put = backend.put

        def put(record: MemoryRecord) -> str:
            spy = spy_holder.get("spy")
            if spy is not None:
                spy.writes.append(record)
            return original_put(record)

        backend.put = put  # type: ignore[method-assign]
        return backend

    return factory


@pytest.fixture
def memory_spy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MemorySpy:
    holder: dict[str, MemorySpy] = {}
    memory = LayeredMemorySystem.from_backend_factory(
        tmp_path / "memory",
        _spy_backend_factory(holder),
        enforce_stable_write_integrity=False,
    )
    spy = MemorySpy(memory)
    holder["spy"] = spy

    original_decide = NestedLearningKernel.decide

    def spied_decide(
        self: NestedLearningKernel,
        signal: LearningSignal,
        *,
        action: str = "write",
    ):
        if signal.requested_target_layer == MemoryLayer.POLICY:
            spy.policy_signals.append(signal)
        return original_decide(self, signal, action=action)

    monkeypatch.setattr(NestedLearningKernel, "decide", spied_decide)

    original_evaluate = MutationGate.evaluate

    def spied_evaluate(self: MutationGate, delta: object, evidence: object):
        spy.policy_signals.append(delta)
        return original_evaluate(self, delta, evidence)

    monkeypatch.setattr(MutationGate, "evaluate", spied_evaluate)
    return spy


def run_completed_qualification_and_activation(tmp_path: Path) -> None:
    """Run the full chain: qualification -> receipt -> activation -> learned route."""

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    ledger = _configured_ledger(state)
    _train_learned_winner(state, ledger)
    task = _create_task(state, suffix="no-policy-memory")
    contract = compile_task_contract(task)

    # Completed qualification run with terminal receipt plus scope activation.
    harness = GrantHarness(state, contract)
    assert harness.receipt.payload["status"] == "completed"
    assert harness.grant.grant_id

    # Learned routing under the active grant, including outcome recording.
    coordinator = _learned_coordinator(ledger, harness.evaluator)
    durable = coordinator.assign(
        AgentConfig(),
        task,
        subagent_id=None,
        attempt=1,
    )
    assert durable.record.activation_effective is True
    assert durable.assignment.decision.selected_target.target_id == "cheap"
    coordinator.record_outcome(
        durable,
        execution_status="completed",
        validation_passed=True,
        validation_codes=("accepted",),
        input_tokens=1_000,
        output_tokens=500,
        latency_seconds=1.0,
        outcome_labels=("validated_success",),
    )


def test_qualification_and_activation_write_no_memvid_records(
    memory_spy: MemorySpy,
    tmp_path: Path,
) -> None:
    run_completed_qualification_and_activation(tmp_path)

    assert memory_spy.writes == []
    assert memory_spy.policy_signals == []
    for layer in MemoryLayer:
        assert list(memory_spy.memory.iter_records(layer)) == []
