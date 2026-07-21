from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

from pytest import MonkeyPatch, raises

import benchmarks.real_agent_learning_benchmark as learning_gate
from benchmarks.real_agent_learning_benchmark import (
    BenchmarkMockLLM,
    _build_agent_with_llm,
    _build_config,
    _run_task,
    _scenario_approval_handler,
    analyze_results,
    benchmark_assertions,
    build_task_scenarios,
    main,
    run_learning_gate,
)
from nested_memvid_agent.cognition import LessonManager
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.tools.base import ToolContext


def _scenario(scenario_id: str):
    return next(item for item in build_task_scenarios() if item.id == scenario_id)


def test_task1_uses_production_failure_and_lesson_managers(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    calls = {"record_failure": 0, "write_lesson": 0}
    original_record_failure = LessonManager.record_failure
    original_write_lesson = LessonManager.write_lesson_from_resolution

    def record_failure(*args, **kwargs):
        calls["record_failure"] += 1
        return original_record_failure(*args, **kwargs)

    def write_lesson(*args, **kwargs):
        calls["write_lesson"] += 1
        return original_write_lesson(*args, **kwargs)

    monkeypatch.setattr(LessonManager, "record_failure", record_failure)
    monkeypatch.setattr(LessonManager, "write_lesson_from_resolution", write_lesson)

    report = run_learning_gate(tmp_path)
    task1 = next(
        item
        for item in report["treatment_results"]
        if item["scenario_id"] == "validated_repair_1"
    )
    failures = task1["learning_evidence"]["failure_episodes"]
    lessons = task1["learning_evidence"]["lesson_cards"]

    assert calls["record_failure"] >= 2  # control and treatment Task 1
    assert calls["write_lesson"] >= 2
    assert task1["success"] is True
    assert [item["success"] for item in task1["tool_executions"] if item["tool"] == "test.run"] == [
        False,
        True,
    ]
    assert len(failures) == 1
    assert len(lessons) == 1
    failure = failures[0]
    lesson = lessons[0]
    assert failure["metadata"]["cognition_schema"] == "failure_episode.v1"
    assert failure["metadata"]["validation_status"] == "resolved"
    assert failure["payload"]["resolved"] is True
    assert failure["payload"]["validation_evidence"]
    assert failure["evidence"][0]["source"].startswith("agent_runtime://runs/")
    assert failure["evidence"][0]["locator"] == failure["id"]
    assert lesson["metadata"]["cognition_schema"] == "lesson_card.v1"
    assert lesson["metadata"]["validation_status"] == "validated_once"
    assert lesson["confidence"] >= 0.8
    assert lesson["payload"]["evidence_refs"][0] == failure["id"]
    assert {item["source"] for item in lesson["evidence"]} == {
        "failure_episode",
        "memory_record",
        "validation",
    }


def test_task2_retrieves_created_lesson_and_improves_outcome(tmp_path: Path) -> None:
    report = run_learning_gate(tmp_path)
    control_task2 = next(
        item
        for item in report["control_results"]
        if item["scenario_id"] == "validated_repair_2"
    )
    treatment_task1 = next(
        item
        for item in report["treatment_results"]
        if item["scenario_id"] == "validated_repair_1"
    )
    treatment_task2 = next(
        item
        for item in report["treatment_results"]
        if item["scenario_id"] == "validated_repair_2"
    )
    created_id = treatment_task1["learning_evidence"]["lesson_cards"][0]["id"]
    applied_ids = {
        item["id"]
        for item in treatment_task2["learning_evidence"]["proof"]["lessons_applied"]
    }

    assert report["passed"] is True
    assert report["meta"]["seeded_oracle_lessons"] == 0
    assert control_task2["success"] is False
    assert treatment_task2["success"] is True
    assert created_id in applied_ids
    assert treatment_task2["tool_calls"] == ["file.read", "file.write", "test.run"]
    assert report["analysis"]["task2_transfer"]["outcome_improvement"] == 1.0


def test_treatment_source_contains_no_oracle_memory_injection() -> None:
    source = inspect.getsource(learning_gate.run_treatment)

    assert ".memory.put(" not in source
    assert "_inject_lesson" not in source
    assert not hasattr(learning_gate, "_inject_lesson")


def test_mock_task_selection_prefers_exact_benchmark_marker(tmp_path: Path) -> None:
    scenarios = build_task_scenarios()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _build_config(workspace, tmp_path / "memory", "mock", "mock")
    llm = BenchmarkMockLLM()
    for scenario in scenarios:
        llm.register_scenario(scenario)
    agent = _build_agent_with_llm(config, llm)

    task2 = _run_task(agent, _scenario("validated_repair_2"), workspace)

    assert task2["success"] is False
    assert task2["tool_calls"] == ["file.read", "file.write"]


def test_benchmark_operator_refuses_undeclared_high_risk_arguments(
    tmp_path: Path,
) -> None:
    scenario = _scenario("validated_repair_2")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = _build_config(workspace, tmp_path / "memory", "mock", "mock")
    llm = BenchmarkMockLLM()
    llm.register_scenario(scenario)
    agent = _build_agent_with_llm(config, llm)
    handler, approvals = _scenario_approval_handler(agent, scenario)
    call = ToolCall(
        name="file.write",
        arguments={"path": "candidate.txt", "content": "invented mutation"},
        id="undeclared_exact_call",
    )
    spec = agent.tools.spec_for(call.name)
    assert spec is not None

    execution = handler(
        call,
        spec,
        ToolContext(memory=agent.memory, config=config, workspace=workspace),
    )

    assert execution.success is False
    assert execution.error == "approval_required"
    assert approvals == []
    assert not (workspace / "candidate.txt").exists()


def test_efficiency_metrics_exclude_failed_control_outcome() -> None:
    control = [
        {
            "scenario_id": "validated_repair_2",
            "success": False,
            "tool_rounds": 2,
        }
    ]
    treatment = [
        {
            "scenario_id": "validated_repair_2",
            "success": True,
            "tool_rounds": 3,
        }
    ]

    analysis = analyze_results(control, treatment)

    assert analysis["task2_transfer"]["control_avg_successful_rounds"] is None
    assert analysis["task2_transfer"]["treatment_avg_successful_rounds"] == 3.0
    assert analysis["task2_transfer"]["outcome_improvement"] == 1.0


def test_assertions_fail_when_task2_does_not_apply_task1_lesson(tmp_path: Path) -> None:
    report = run_learning_gate(tmp_path)
    task2 = next(
        item
        for item in report["treatment_results"]
        if item["scenario_id"] == "validated_repair_2"
    )
    task2["learning_evidence"]["proof"]["lessons_applied"] = []

    assertions = benchmark_assertions(
        report["control_results"], report["treatment_results"]
    )
    failed = {item["name"] for item in assertions if not item["passed"]}

    assert "task2_retrieves_and_applies_created_lesson" in failed


def test_benchmark_main_exits_nonzero_and_writes_evidence_on_mismatch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    output_path = tmp_path / "failed-learning-gate.json"
    failed_report = {
        "schema": "kestrel.agent_learning_gate.v1",
        "meta": {},
        "control_results": [],
        "treatment_results": [],
        "analysis": {},
        "assertions": [
            {"name": "task1_production_learning_chain", "passed": False, "details": {}}
        ],
        "passed": False,
    }
    monkeypatch.setattr(
        "benchmarks.real_agent_learning_benchmark.run_learning_gate",
        lambda *_args, **_kwargs: failed_report,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "real_agent_learning_benchmark.py",
            "--output",
            str(output_path),
        ],
    )

    with raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    payload = json.loads(output_path.read_text())
    assert payload["passed"] is False
    assert payload["assertions"][0]["name"] == "task1_production_learning_chain"
