"""Deterministic learning-path regression benchmark for Kestrel.

Measures five dimensions of bounded learning-path behavior:
  1. Few-Shot Tool Selection      — does episodic memory improve task accuracy?
  2. Mistake Avoidance            — does the agent avoid previously-recorded errors?
  3. Promotion Gate Conformance   — do decisions match the declared evidence gate?
  4. Router Calibration           — does a held-out replay project safer utility?
  5. Procedural Consolidation     — do repeated successes become reusable procedures?

Usage:
    python benchmarks/learning_benchmark.py --output benchmark_results/learning.json

The promotion dimension is declared-gate conformance, not independently labeled
usefulness. Router utility is a held-out synthetic projection without counterfactual
outcome ground truth. Real capability claims require the production-path and optional
live-provider learning evaluations described in ``benchmarks/README.md``.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.learned_routing import (
    OutcomeCalibratedRouter,
    RoutingExample,
    evaluate_routing_examples,
)
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
    compute_validation_score,
    resolve_validation_evidence,
    validation_evidence_is_resolved,
)

# ────────────────────────────────
# Dimension 1: Few-Shot Tool Selection
# ────────────────────────────────


@dataclass(frozen=True)
class ToolSelectionTask:
    task_id: str
    description: str
    correct_tool: str
    distractors: tuple[str, ...]


TOOL_TASKS: tuple[ToolSelectionTask, ...] = (
    ToolSelectionTask(
        "t1",
        "Run the test suite for the auth module",
        "test.run",
        ("lint.run", "shell.run", "git.commit"),
    ),
    ToolSelectionTask(
        "t2",
        "Check code style before committing",
        "lint.run",
        ("test.run", "shell.run", "repo.map"),
    ),
    ToolSelectionTask(
        "t3",
        "Find all files matching '*.py' in the repo",
        "repo.search",
        ("repo.map", "read.file", "shell.run"),
    ),
    ToolSelectionTask(
        "t4",
        "Stage changes and create a commit",
        "git.commit",
        ("git.diff", "shell.run", "write.file"),
    ),
    ToolSelectionTask(
        "t5",
        "Apply a code patch from review feedback",
        "patch.apply",
        ("write.file", "shell.run", "repair.apply"),
    ),
    ToolSelectionTask(
        "t6",
        "Validate a repair before merging",
        "repair.validate",
        ("test.run", "lint.run", "shell.run"),
    ),
    ToolSelectionTask(
        "t7", "Get the current branch name", "git.branch", ("git.status", "shell.run", "repo.map")
    ),
    ToolSelectionTask(
        "t8",
        "Debug a failing build by reading logs",
        "read.file",
        ("shell.run", "diagnosis.classify", "repo.search"),
    ),
)


def _simulate_tool_choice(
    task: ToolSelectionTask, memory: LayeredMemorySystem, session_id: str, attempt: int
) -> tuple[str, bool]:
    """Simulate an agent choosing a tool, possibly using memory from prior attempts."""
    query = f"What tool should I use to: {task.description}"
    # Retrieve episodic memories about this task
    hits = memory.retrieve(
        RetrievalQuery(
            query=query, k_per_layer=4, layers=(MemoryLayer.EPISODIC, MemoryLayer.PROCEDURAL)
        )
    )

    # Naive first attempt: 40% chance of picking correctly
    if attempt == 1 or not hits:
        if random.random() < 0.40:
            return task.correct_tool, True
        return random.choice(task.distractors), False

    # With memory: boost based on relevant hits
    relevant = [
        h for h in hits if task.task_id in h.record.content or task.correct_tool in h.record.content
    ]
    if relevant:
        # Higher score = better memory = higher chance of correct choice
        best_score = max(h.score for h in relevant)
        success_prob = min(0.40 + best_score * 0.50, 0.95)
        if random.random() < success_prob:
            return task.correct_tool, True
        return random.choice(task.distractors), False

    return random.choice(task.distractors), False


def _record_tool_attempt(
    memory: LayeredMemorySystem, task: ToolSelectionTask, chosen: str, success: bool, attempt: int
) -> None:
    outcome = "success" if success else "failure"
    content = (
        f"Attempt {attempt} for task {task.task_id}: {task.description}. "
        f"Chose tool '{chosen}'. Outcome: {outcome}. "
        f"Correct tool would have been '{task.correct_tool}'."
    )
    record = MemoryRecord(
        id=f"{task.task_id}_attempt_{attempt}_{int(time.time() * 1000)}",
        title=f"Tool selection: {task.task_id}",
        content=content,
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.EVENT,
        confidence=0.75 if success else 0.55,
        metadata={
            "task_id": task.task_id,
            "attempt": attempt,
            "outcome": outcome,
            "chosen_tool": chosen,
        },
    )
    memory.put(record)

    # This dimension intentionally measures episodic few-shot recall. Durable,
    # validated procedural promotion is exercised independently below and by
    # ``real_agent_learning_benchmark.py``.


def benchmark_few_shot_tool_selection(*, seed: int = 42, sessions: int = 5) -> dict[str, Any]:
    """Measure whether episodic/procedural memory improves tool selection accuracy."""
    random.seed(seed)
    temporary = tempfile.TemporaryDirectory(prefix="kestrel-learn-bench-fewshot-")
    memory_dir = Path(temporary.name) / "memory"
    memory = LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)
    results: list[dict[str, Any]] = []

    for session in range(1, sessions + 1):
        session_correct = 0
        for task in TOOL_TASKS:
            chosen, success = _simulate_tool_choice(task, memory, f"sess_{session}", session)
            _record_tool_attempt(memory, task, chosen, success, session)
            if success:
                session_correct += 1
        accuracy = session_correct / len(TOOL_TASKS)
        results.append(
            {
                "session": session,
                "correct": session_correct,
                "total": len(TOOL_TASKS),
                "accuracy": round(accuracy, 3),
            }
        )

    # Compute learning curve metrics
    first_acc = results[0]["accuracy"]
    last_acc = results[-1]["accuracy"]
    accuracies = [r["accuracy"] for r in results]

    payload = {
        "name": "few_shot_tool_selection",
        "description": "Does episodic/procedural memory improve tool selection over sessions?",
        "sessions": sessions,
        "session_results": results,
        "first_session_accuracy": first_acc,
        "last_session_accuracy": last_acc,
        "accuracy_delta": round(last_acc - first_acc, 3),
        "improved": last_acc > first_acc,
        "avg_accuracy": round(statistics.mean(accuracies), 3),
    }
    memory.close_all()
    temporary.cleanup()
    return payload


# ────────────────────────────────
# Dimension 2: Mistake Avoidance
# ────────────────────────────────

MISTAKE_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "m1",
        "situation": "Running pytest on the entire repo when only auth tests changed",
        "wrong_action": "pytest -q",
        "correct_action": "pytest tests/test_auth.py -q",
        "diagnosis": "wasted_time",
    },
    {
        "id": "m2",
        "situation": "Committing without running lint first",
        "wrong_action": "git commit -m 'fix'",
        "correct_action": "lint.run && git commit -m 'fix'",
        "diagnosis": "skipped_validation",
    },
    {
        "id": "m3",
        "situation": "Applying a patch to the wrong file",
        "wrong_action": "patch.apply --file wrong.py",
        "correct_action": "patch.apply --file correct.py",
        "diagnosis": "wrong_target",
    },
    {
        "id": "m4",
        "situation": "Running shell commands without checking git status first",
        "wrong_action": "rm -rf build/",
        "correct_action": "git.status && rm -rf build/",
        "diagnosis": "untracked_deletion",
    },
    {
        "id": "m5",
        "situation": "Using shell.run for file reads instead of read.file",
        "wrong_action": "shell.run cat src/main.py",
        "correct_action": "read.file src/main.py",
        "diagnosis": "wrong_tool_choice",
    },
)


def _simulate_mistake_scenario(
    memory: LayeredMemorySystem, scenario: dict[str, Any], attempt: int
) -> tuple[str, bool]:
    """Simulate agent action, retrieving past mistake memory if available."""
    query = scenario["situation"]
    hits = memory.retrieve(
        RetrievalQuery(
            query=query, k_per_layer=6, layers=(MemoryLayer.EPISODIC, MemoryLayer.PROCEDURAL)
        )
    )
    relevant = [h for h in hits if scenario["id"] in h.record.content]

    if attempt == 1 or not relevant:
        # First time: makes the mistake
        return scenario["wrong_action"], False

    # With memory: chance of avoiding mistake based on memory quality
    best_score = max(h.score for h in relevant)
    avoid_prob = min(0.30 + best_score * 0.60, 0.90)
    if random.random() < avoid_prob:
        return scenario["correct_action"], True
    return scenario["wrong_action"], False


def _record_mistake(
    memory: LayeredMemorySystem, scenario: dict[str, Any], action: str, success: bool, attempt: int
) -> None:
    outcome = "avoided" if success else "repeated"
    content = (
        f"Mistake scenario {scenario['id']}: {scenario['situation']}. "
        f"Attempt {attempt}: action='{action}', outcome={outcome}. "
        f"Wrong action is '{scenario['wrong_action']}', correct is '{scenario['correct_action']}'."
    )
    record = MemoryRecord(
        id=f"{scenario['id']}_attempt_{attempt}",
        title=f"Mistake: {scenario['id']}",
        content=content,
        layer=MemoryLayer.EPISODIC,
        kind=MemoryKind.FAILURE if not success else MemoryKind.OBSERVATION,
        confidence=0.7,
        metadata={
            "scenario_id": scenario["id"],
            "attempt": attempt,
            "outcome": outcome,
            "diagnosis": scenario["diagnosis"],
        },
    )
    memory.put(record)

    # Mistake avoidance here is an episodic-recall measure. The separate
    # consolidation dimension verifies the stable procedural sink.


def benchmark_mistake_avoidance(*, seed: int = 42, rounds: int = 4) -> dict[str, Any]:
    """Measure whether the agent avoids previously-recorded mistakes."""
    random.seed(seed)
    temporary = tempfile.TemporaryDirectory(prefix="kestrel-learn-bench-mistake-")
    memory_dir = Path(temporary.name) / "memory"
    memory = LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)

    results: list[dict[str, Any]] = []
    for r in range(1, rounds + 1):
        avoided = 0
        for scenario in MISTAKE_SCENARIOS:
            action, success = _simulate_mistake_scenario(memory, scenario, r)
            _record_mistake(memory, scenario, action, success, r)
            if success:
                avoided += 1
        results.append(
            {
                "round": r,
                "avoided": avoided,
                "total": len(MISTAKE_SCENARIOS),
                "avoidance_rate": round(avoided / len(MISTAKE_SCENARIOS), 3),
            }
        )

    first_rate = results[0]["avoidance_rate"]
    last_rate = results[-1]["avoidance_rate"]
    payload = {
        "name": "mistake_avoidance",
        "description": "Does the agent avoid previously-recorded mistakes over rounds?",
        "rounds": rounds,
        "round_results": results,
        "first_round_avoidance": first_rate,
        "last_round_avoidance": last_rate,
        "avoidance_delta": round(last_rate - first_rate, 3),
        "improved": last_rate > first_rate,
    }
    memory.close_all()
    temporary.cleanup()
    return payload


# ────────────────────────────────
# Dimension 3: Promotion Gate Conformance
# ────────────────────────────────


def benchmark_promotion_gate_conformance(*, seed: int = 42, n_signals: int = 100) -> dict[str, Any]:
    """Measure conformance to the declared deterministic promotion gate.

    This is a contract-regression check, not an independent usefulness or outcome
    accuracy benchmark: the expected label is derived from the public gate contract.
    """
    random.seed(seed)
    # This dimension measures only the kernel's deterministic classification
    # rules. Synthetic evidence is explicitly resolved by the trusted benchmark
    # harness, and no memory record is written. The stable sink is exercised by
    # the procedural and end-to-end learning gates.
    kernel = NestedLearningKernel()

    accepted_count = 0
    rejected_count = 0
    true_positive = 0
    false_positive = 0
    true_negative = 0
    false_negative = 0

    for i in range(n_signals):
        # Generate signal with varying quality
        validation_score = random.uniform(0.3, 0.95)
        repeat_count = random.choices([1, 2, 3, 5], weights=[40, 30, 20, 10])[0]
        has_test_evidence = random.random() < validation_score
        has_lint_evidence = random.random() < validation_score * 0.8
        has_repair = random.random() < validation_score * 0.6
        has_review = random.random() < validation_score * 0.5

        unresolved = ValidationEvidence(
            test_refs=(EvidenceRef(source="test.run", locator=f"test-{i}"),)
            if has_test_evidence
            else (),
            lint_refs=(EvidenceRef(source="lint.run", locator=f"lint-{i}"),)
            if has_lint_evidence
            else (),
            repair_refs=(EvidenceRef(source="repair.validate", locator=f"repair-{i}"),)
            if has_repair
            else (),
            review_refs=(EvidenceRef(source="repair.review", locator=f"review-{i}"),)
            if has_review
            else (),
        )
        artifact_ids = tuple(dict.fromkeys(ref.locator for ref in unresolved.all_refs()))
        evidence = (
            resolve_validation_evidence(
                ValidationEvidence(
                    test_refs=unresolved.test_refs,
                    lint_refs=unresolved.lint_refs,
                    repair_refs=unresolved.repair_refs,
                    review_refs=unresolved.review_refs,
                    task_refs=unresolved.all_refs(),
                ),
                status="runtime_validated",
                artifact_ids=artifact_ids,
            )
            if artifact_ids
            else unresolved
        )

        signal = LearningSignal(
            title=f"Signal {i}",
            content=f"Content for signal {i} with score {validation_score:.2f}",
            kind=MemoryKind.FACT,
            source_layer=MemoryLayer.EPISODIC,
            validation_score=None,
            validation_evidence=evidence,
            repeat_count=repeat_count,
        )

        decision = kernel.decide(signal)
        computed = compute_validation_score(evidence)

        # Expected contract label, deliberately independent of ``decision`` but not
        # independent of the kernel's declared thresholds. Do not call this an
        # empirical usefulness ground truth.
        should_pass_declared_gate = (
            validation_evidence_is_resolved(evidence) and computed >= 0.65 and repeat_count >= 1
        )

        if decision.accepted:
            accepted_count += 1
            if should_pass_declared_gate:
                true_positive += 1
            else:
                false_positive += 1
        else:
            rejected_count += 1
            if should_pass_declared_gate:
                false_negative += 1
            else:
                true_negative += 1

    precision = (
        true_positive / (true_positive + false_positive)
        if (true_positive + false_positive) > 0
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if (true_positive + false_negative) > 0
        else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    accuracy = (true_positive + true_negative) / n_signals

    return {
        "name": "promotion_gate_conformance",
        "description": "Do kernel decisions conform to the declared evidence-and-repeat promotion gate?",
        "n_signals": n_signals,
        "accepted": accepted_count,
        "rejected": rejected_count,
        "true_positive": true_positive,
        "false_positive": false_positive,
        "true_negative": true_negative,
        "false_negative": false_negative,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "f1_score": round(f1, 3),
        "accuracy": round(accuracy, 3),
        "evidence_mode": "synthetic_declared_gate_conformance_no_memory_write",
        "expected_label_source": "declared_kernel_contract_not_independent_outcomes",
    }


# ────────────────────────────────
# Dimension 4: Router Calibration
# ────────────────────────────────


def _make_routing_example(
    promotion_id: str,
    *,
    target: MemoryLayer | None,
    reward: float,
    outcomes: tuple[str, ...],
    validation_score: float = 0.76,
    repeat_count: int = 1,
) -> RoutingExample:
    target_name = "" if target is None else target.value
    return RoutingExample(
        signal_features={
            "source_layer": MemoryLayer.EPISODIC.value,
            "memory_kind": MemoryKind.FACT.value,
            "requested_target_layer": "",
            "validation_score": validation_score,
            "repeat_count": repeat_count,
            "explicit_instruction": False,
            "confidence": 0.65,
            "importance": 0.5,
            "promotion_status": "confirmed",
            "rule_target_layer": target_name,
            "semantic_margin": round(validation_score - 0.78, 4),
            "semantic_provisional_margin": round(validation_score - 0.65, 4),
            "semantic_repeat_margin": 0.0,
            "episodic_margin": round(validation_score - 0.65, 4),
            "episodic_provisional_margin": round(validation_score - 0.50, 4),
            "episodic_repeat_margin": 0.0,
        },
        rule_action="reject" if target is None else "promote",
        rule_target_layer=target,
        chosen_action="reject" if target is None else "promote",
        chosen_target_layer=target,
        outcome_reward=reward,
        promotion_id=promotion_id,
        outcome_labels=outcomes,
    )


def benchmark_router_calibration(*, seed: int = 42) -> dict[str, Any]:
    """Train on synthetic history and project utility on disjoint held-out rows.

    The replay has no counterfactual outcome oracle for actions not taken, so its
    utility result is explicitly a model projection rather than measured lift.
    """
    random.seed(seed)

    # Build a synthetic training history
    train_examples = (
        _make_routing_example(
            "e1",
            target=MemoryLayer.SEMANTIC,
            reward=-1.10,
            outcomes=("corrected",),
            validation_score=0.82,
        ),
        _make_routing_example(
            "e2",
            target=MemoryLayer.SEMANTIC,
            reward=-1.10,
            outcomes=("contradicted",),
            validation_score=0.79,
        ),
        _make_routing_example(
            "e3",
            target=MemoryLayer.EPISODIC,
            reward=0.95,
            outcomes=("useful",),
            validation_score=0.70,
        ),
        _make_routing_example(
            "e4",
            target=MemoryLayer.EPISODIC,
            reward=0.95,
            outcomes=("useful",),
            validation_score=0.72,
        ),
        _make_routing_example(
            "e5",
            target=MemoryLayer.SEMANTIC,
            reward=-0.50,
            outcomes=("tombstoned",),
            validation_score=0.81,
        ),
        _make_routing_example(
            "e6",
            target=MemoryLayer.PROCEDURAL,
            reward=1.00,
            outcomes=("useful",),
            validation_score=0.85,
            repeat_count=3,
        ),
        _make_routing_example(
            "e7",
            target=MemoryLayer.PROCEDURAL,
            reward=-0.20,
            outcomes=("superseded",),
            validation_score=0.80,
            repeat_count=2,
        ),
        _make_routing_example(
            "e8",
            target=MemoryLayer.EPISODIC,
            reward=0.95,
            outcomes=("useful",),
            validation_score=0.68,
        ),
        _make_routing_example("e9", target=None, reward=0.0, outcomes=(), validation_score=0.45),
        _make_routing_example("e10", target=None, reward=0.0, outcomes=(), validation_score=0.50),
    )
    held_out_examples = (
        _make_routing_example(
            "h1",
            target=MemoryLayer.SEMANTIC,
            reward=-1.10,
            outcomes=("corrected",),
            validation_score=0.83,
        ),
        _make_routing_example(
            "h2",
            target=MemoryLayer.SEMANTIC,
            reward=-0.50,
            outcomes=("tombstoned",),
            validation_score=0.80,
        ),
        _make_routing_example(
            "h3",
            target=MemoryLayer.EPISODIC,
            reward=0.95,
            outcomes=("useful",),
            validation_score=0.71,
        ),
        _make_routing_example(
            "h4",
            target=MemoryLayer.EPISODIC,
            reward=0.95,
            outcomes=("useful",),
            validation_score=0.69,
        ),
        _make_routing_example(
            "h5",
            target=MemoryLayer.PROCEDURAL,
            reward=1.00,
            outcomes=("useful",),
            validation_score=0.86,
            repeat_count=3,
        ),
        _make_routing_example(
            "h6",
            target=MemoryLayer.PROCEDURAL,
            reward=-0.20,
            outcomes=("superseded",),
            validation_score=0.80,
            repeat_count=2,
        ),
        _make_routing_example("h7", target=None, reward=0.0, outcomes=(), validation_score=0.46),
        _make_routing_example("h8", target=None, reward=0.0, outcomes=(), validation_score=0.52),
    )

    router = OutcomeCalibratedRouter.fit(
        train_examples,
        mode="constrained",
        confidence_threshold=0.0,
        min_examples_per_target=1,
    )

    report = evaluate_routing_examples(held_out_examples, router)
    payload = report.to_payload()

    return {
        "name": "router_calibration",
        "description": "Does learned routing pass a disjoint held-out synthetic utility projection?",
        "training_examples": len(train_examples),
        "evaluation_examples": len(held_out_examples),
        "training_promotion_ids": [item.promotion_id for item in train_examples],
        "evaluation_promotion_ids": [item.promotion_id for item in held_out_examples],
        "evaluation_mode": "synthetic_held_out_projected_utility",
        "counterfactual_outcome_ground_truth": False,
        "oracle_gate_violations": payload["oracle"]["gate_violations"],
        "oracle_abstention_rate": payload["oracle"]["abstention_rate"],
        "projected_expected_utility_delta": round(
            payload["improvement"]["expected_utility_delta"], 4
        ),
        "projection_passes": payload["improvement"]["passes"],
        "per_target_stats": payload.get("per_target", {}),
    }


# ────────────────────────────────
# Dimension 5: Procedural Consolidation
# ────────────────────────────────

PROCEDURE_TASKS: tuple[dict[str, Any], ...] = (
    {
        "id": "p1",
        "problem": "Fix failing auth tests",
        "recipe": "1. Read test output. 2. Identify mock mismatch. 3. Update mock expectations. 4. Re-run tests.",
    },
    {
        "id": "p2",
        "problem": "Add a new API endpoint",
        "recipe": "1. Define route in router. 2. Add handler function. 3. Write tests. 4. Run lint and tests.",
    },
    {
        "id": "p3",
        "problem": "Refactor duplicate code",
        "recipe": "1. Identify duplicates with repo.search. 2. Extract shared function. 3. Update call sites. 4. Validate.",
    },
)


def benchmark_procedural_consolidation(*, seed: int = 42, repetitions: int = 5) -> dict[str, Any]:
    """Measure whether repeated successful solutions get promoted to procedural memory
    and are retrievable for future similar problems."""
    random.seed(seed)
    temporary = tempfile.TemporaryDirectory(prefix="kestrel-learn-bench-procedural-")
    memory_dir = Path(temporary.name) / "memory"
    memory = LayeredMemorySystem.from_backend_factory(memory_dir, InMemoryBackend)
    kernel = NestedLearningKernel(memory=memory)

    task_results: list[dict[str, Any]] = []

    for task in PROCEDURE_TASKS:
        procedure_formed = False
        procedure_form_round: int | None = None
        run_id = f"learning-benchmark-{task['id']}"
        session_id = "learning-benchmark"
        title = f"Solved: {task['problem']}"
        candidate_id = memory.put(
            MemoryRecord(
                title=title,
                content=task["recipe"],
                layer=MemoryLayer.EPISODIC,
                kind=MemoryKind.PROCEDURE,
                confidence=0.95,
                metadata={"session_id": session_id, "run_id": run_id},
                evidence=[EvidenceRef(source="benchmark", locator=run_id)],
            )
        )
        receipts: list[tuple[str, str]] = []
        validation_buckets = ("test", "lint", "repair", "review")
        for r in range(1, repetitions + 1):
            bucket = validation_buckets[min(r - 1, len(validation_buckets) - 1)]
            receipt_id = memory.put_runtime_validation_receipt(
                tool_name=f"benchmark.{bucket}",
                tool_call_id=f"{task['id']}-{bucket}-{r}",
                evidence_bucket=bucket,
                command=("benchmark-validate", task["id"], str(r)),
                output_sha256=sha256(f"{task['id']}:{bucket}:{r}".encode()).hexdigest(),
                session_id=session_id,
                run_id=run_id,
                subject_record_id=candidate_id,
            )
            receipts.append((bucket, receipt_id))
            refs_by_bucket = {
                name: tuple(
                    EvidenceRef(source="memory_record", locator=stored_id)
                    for stored_bucket, stored_id in receipts
                    if stored_bucket == name
                )
                for name in validation_buckets
            }
            task_refs = tuple(
                EvidenceRef(source="memory_record", locator=stored_id)
                for _stored_bucket, stored_id in receipts
            )
            evidence = resolve_validation_evidence(
                ValidationEvidence(
                    test_refs=refs_by_bucket["test"],
                    lint_refs=refs_by_bucket["lint"],
                    repair_refs=refs_by_bucket["repair"],
                    review_refs=refs_by_bucket["review"],
                    task_refs=task_refs,
                ),
                status="runtime_validated",
                artifact_ids=tuple(stored_id for _bucket, stored_id in receipts),
            )
            signal = LearningSignal(
                title=title,
                content=task["recipe"],
                kind=MemoryKind.PROCEDURE,
                source_layer=MemoryLayer.EPISODIC,
                validation_score=None,
                validation_evidence=evidence,
                repeat_count=r,
                source="memory_record",
                locator=candidate_id,
                metadata={"session_id": session_id, "run_id": run_id},
                requested_target_layer=MemoryLayer.PROCEDURAL,
            )
            decision = kernel.decide(signal, action="promote")
            if decision.action == "promote" and decision.target_layer == MemoryLayer.PROCEDURAL:
                proc_record = kernel.to_memory_record(signal, decision)
                memory.put_validated(
                    proc_record,
                    authority="nested_learning",
                    source_record_ids=(
                        candidate_id,
                        *(stored_id for _bucket, stored_id in receipts),
                    ),
                    validation_evidence=evidence,
                )
                procedure_formed = True
                procedure_form_round = r
                break

        # Now test retrieval
        hits = memory.retrieve(
            RetrievalQuery(query=task["problem"], k_per_layer=3, layers=(MemoryLayer.PROCEDURAL,))
        )
        retrieved_recipe = any(task["recipe"] in h.record.content for h in hits)

        task_results.append(
            {
                "task_id": task["id"],
                "problem": task["problem"],
                "procedure_formed": procedure_formed,
                "procedure_form_round": procedure_form_round,
                "retrieved_after_formation": retrieved_recipe,
            }
        )

    formed = sum(1 for t in task_results if t["procedure_formed"])
    retrievable = sum(1 for t in task_results if t["retrieved_after_formation"])

    payload = {
        "name": "procedural_consolidation",
        "description": "Do repeated successes become reusable procedures in procedural memory?",
        "repetitions_available": repetitions,
        "tasks": task_results,
        "procedures_formed": formed,
        "procedures_retrievable": retrievable,
        "formation_rate": round(formed / len(PROCEDURE_TASKS), 3),
        "retrieval_rate": round(retrievable / len(PROCEDURE_TASKS), 3),
        "evidence_mode": "authenticated_runtime_receipts_and_validated_sink",
    }
    memory.close_all()
    temporary.cleanup()
    return payload


# ────────────────────────────────
# Utilities
# ────────────────────────────────

# ────────────────────────────────
# Main
# ────────────────────────────────


def run_learning_benchmark(*, seed: int = 42) -> dict[str, Any]:
    print("\n=== Kestrel Learning Benchmark ===\n", file=sys.stderr)

    dim1 = benchmark_few_shot_tool_selection(seed=seed)
    print(
        f"[1/5] Few-Shot Tool Selection: {dim1['first_session_accuracy']:.1%} → {dim1['last_session_accuracy']:.1%} (delta={dim1['accuracy_delta']:+.3f})",
        file=sys.stderr,
    )

    dim2 = benchmark_mistake_avoidance(seed=seed)
    print(
        f"[2/5] Mistake Avoidance: {dim2['first_round_avoidance']:.1%} → {dim2['last_round_avoidance']:.1%} (delta={dim2['avoidance_delta']:+.3f})",
        file=sys.stderr,
    )

    dim3 = benchmark_promotion_gate_conformance(seed=seed)
    print(
        f"[3/5] Promotion Gate Conformance: precision={dim3['precision']:.3f}, recall={dim3['recall']:.3f}, f1={dim3['f1_score']:.3f}",
        file=sys.stderr,
    )

    dim4 = benchmark_router_calibration(seed=seed)
    print(
        f"[4/5] Router Calibration: projected_utility_delta={dim4['projected_expected_utility_delta']:+.4f}, passes={dim4['projection_passes']}",
        file=sys.stderr,
    )

    dim5 = benchmark_procedural_consolidation(seed=seed)
    print(
        f"[5/5] Procedural Consolidation: formed={dim5['formation_rate']:.1%}, retrievable={dim5['retrieval_rate']:.1%}",
        file=sys.stderr,
    )

    assertions = {
        "few_shot_improved": bool(dim1["improved"]),
        "mistake_avoidance_improved": bool(dim2["improved"]),
        "promotion_gate_conformance_f1_at_least_0_95": float(dim3["f1_score"]) >= 0.95,
        "router_held_out_projection_passes": bool(dim4["projection_passes"]),
        "all_procedures_formed": float(dim5["formation_rate"]) == 1.0,
        "all_procedures_retrievable": float(dim5["retrieval_rate"]) == 1.0,
    }
    return {
        "schema": "kestrel.learning_benchmark.v1",
        "config": {"seed": seed},
        "dimensions": [dim1, dim2, dim3, dim4, dim5],
        "assertions": assertions,
        "passed": all(assertions.values()),
        "summary": {
            "few_shot_improved": dim1["improved"],
            "mistake_avoidance_improved": dim2["improved"],
            "promotion_gate_conformance_f1": dim3["f1_score"],
            "router_projected_utility_lift": dim4["projected_expected_utility_delta"],
            "procedural_formation_rate": dim5["formation_rate"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Kestrel learning benchmark.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--output", type=Path, default=Path("benchmark_results/learning_benchmark.json")
    )
    args = parser.parse_args()

    result = run_learning_benchmark(seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))
    print(f"\nWrote results to {args.output}", file=sys.stderr)

    # Summary table
    print("\n" + "=" * 80)
    print("KESTREL LEARNING BENCHMARK RESULTS")
    print("=" * 80)
    for dim in result["dimensions"]:
        print(f"\n{dim['name'].upper().replace('_', ' ')}")
        print(f"  {dim['description']}")
        if "accuracy_delta" in dim:
            print(
                f"  Result: accuracy improved by {dim['accuracy_delta']:+.3f} ({dim['first_session_accuracy']:.1%} → {dim['last_session_accuracy']:.1%})"
            )
        if "avoidance_delta" in dim:
            print(
                f"  Result: avoidance improved by {dim['avoidance_delta']:+.3f} ({dim['first_round_avoidance']:.1%} → {dim['last_round_avoidance']:.1%})"
            )
        if "f1_score" in dim:
            print(
                f"  Result: F1={dim['f1_score']:.3f}, precision={dim['precision']:.3f}, recall={dim['recall']:.3f}"
            )
        if "projected_expected_utility_delta" in dim:
            print(
                f"  Result: held-out projected utility delta = {dim['projected_expected_utility_delta']:+.4f} (passes={dim['projection_passes']})"
            )
        if "formation_rate" in dim:
            print(
                f"  Result: {dim['formation_rate']:.1%} of tasks formed procedures, {dim['retrieval_rate']:.1%} retrievable"
            )
    print("=" * 80)
    print(f"Learning benchmark gate: {'PASS' if result['passed'] else 'FAIL'}")

    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
