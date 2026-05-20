from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from nested_memvid_agent.learning_eval import (
    LearningEvalOptions,
    LearningEvalReport,
    LearningEvalResult,
    LearningEvalStep,
    load_learning_eval_scenario,
    run_learning_eval,
    write_learning_eval_markdown,
)


def _options(tmp_path: Path, **overrides: object) -> LearningEvalOptions:
    values = {"provider": "mock", "backend": "memory", "workspace": tmp_path}
    values.update(overrides)
    return LearningEvalOptions(**values)


def test_mock_eval_scenario_runs_end_to_end_and_passes(tmp_path: Path) -> None:
    scenario = load_learning_eval_scenario("changed_strategy_after_failed_validation")

    result = run_learning_eval(scenario, _options(tmp_path))

    assert result.status == "pass"
    assert result.llm_calls == 2
    assert result.tool_calls == 1
    assert {stage.name: stage.status for stage in result.stages}["tool_aware_preflight"] == "pass"
    assert result.deltas["summary"]["active_deltas"] == 1
    assert result.deltas["summary"]["outcomes"]["useful"] == 1


def test_openai_without_key_and_live_flag_skips_cleanly(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("RUN_LIVE_LEARNING_EVALS", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    scenario = load_learning_eval_scenario("live_provider_smoke_learning_loop")

    result = run_learning_eval(scenario, _options(tmp_path, provider="openai", model="gpt-5-mini"))

    assert result.status == "skip"
    assert result.llm_calls == 0
    assert "RUN_LIVE_LEARNING_EVALS=1" in str(result.skipped_reason)


def test_live_eval_refuses_to_run_unless_enabled(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("RUN_LIVE_LEARNING_EVALS", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-testsecret123456789")
    scenario = load_learning_eval_scenario("live_provider_smoke_learning_loop")

    result = run_learning_eval(scenario, _options(tmp_path, provider="openai", model="gpt-5-mini"))

    assert result.status == "skip"
    assert "RUN_LIVE_LEARNING_EVALS=1" in str(result.skipped_reason)
    assert "sk-proj-testsecret" not in json.dumps(result.to_payload())


def test_call_guard_stops_eval_before_exceeding_max_calls(tmp_path: Path) -> None:
    scenario = load_learning_eval_scenario("repeated_tool_failure_generates_delta")

    result = run_learning_eval(scenario, _options(tmp_path, max_llm_calls=0))

    assert result.status == "fail"
    assert result.llm_calls == 0
    assert "LLM call guard would be exceeded" in result.failures[0]


def test_markdown_report_redacts_fake_api_keys_and_tokens(tmp_path: Path) -> None:
    report_path = tmp_path / "learning-report.md"
    secret = "sk-proj-testsecret123456789"
    result = LearningEvalResult(
        scenario_id="secret_redaction",
        title="Secret redaction",
        provider="openai",
        model="gpt-5-mini",
        backend="memory",
        status="fail",
        stages=(LearningEvalStep("provider_smoke", "fail", f"Authorization: Bearer {secret}"),),
        llm_calls=0,
        tool_calls=0,
        estimated_cost_usd=0.0,
        failures=(f"OPENAI_API_KEY={secret}",),
    )

    write_learning_eval_markdown(LearningEvalReport(results=(result,), report_path=report_path), report_path)
    text = report_path.read_text(encoding="utf-8")

    assert secret not in text
    assert "sk-<redacted>" in text or "<redacted>" in text


def test_mv2_canonical_scenario_does_not_activate_replacement_policy(tmp_path: Path) -> None:
    scenario = load_learning_eval_scenario("mv2_canonical_memory_constraint")

    result = run_learning_eval(scenario, _options(tmp_path))

    assert result.status == "pass"
    assert result.deltas["summary"]["active_deltas"] == 0
    rows = result.deltas["deltas"]
    assert rows[0]["kind"] == "policy"
    assert rows[0]["status"] == "staged"
    assert "mv2_replaced" not in result.failures


def test_policy_write_scenario_remains_blocked_without_approval_evidence(tmp_path: Path) -> None:
    scenario = load_learning_eval_scenario("policy_write_requires_approval")

    result = run_learning_eval(scenario, _options(tmp_path))
    mutation_stage = next(stage for stage in result.stages if stage.name == "mutation_gate")
    decision = next(iter(mutation_stage.metrics["decisions"].values()))

    assert result.status == "pass"
    assert decision["status"] == "staged"
    assert "exact_call_approval_missing" in decision["blocked_by"]
    assert "policy_delta_activation_disabled" in decision["blocked_by"]
    assert result.deltas["summary"]["active_deltas"] == 0


def test_rollback_scenario_disables_active_delta_and_preserves_history(tmp_path: Path) -> None:
    scenario = load_learning_eval_scenario("rollback_disables_active_delta")

    result = run_learning_eval(scenario, _options(tmp_path))
    rollback_stage = next(stage for stage in result.stages if stage.name == "rollback")

    assert result.status == "pass"
    assert rollback_stage.status == "pass"
    assert rollback_stage.metrics["after_status"] == "rolled_back"
    assert rollback_stage.metrics["future_compilation_ignored_delta"] is True
    assert result.deltas["summary"]["outcomes"]["rolled_back"] == 1
    assert result.deltas["deltas"][0]["status"] == "rolled_back"


def test_cli_json_output_is_valid_and_contains_all_stage_statuses(tmp_path: Path) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/eval_learning_architecture.py",
            "--provider",
            "mock",
            "--backend",
            "memory",
            "--scenario",
            "repeated_tool_failure_generates_delta",
            "--json",
            "--workspace",
            str(tmp_path),
        ],
        check=True,
        text=True,
        capture_output=True,
    )

    payload = json.loads(completed.stdout)
    result = payload["results"][0]
    assert payload["status"] == "pass"
    assert result["scenario_id"] == "repeated_tool_failure_generates_delta"
    assert {stage["name"] for stage in result["stages"]} == {
        "setup",
        "provider_smoke",
        "agent_run",
        "capsule_trace_extraction",
        "mutation_gate",
        "replay_validation",
        "behavior_compilation",
        "tool_aware_preflight",
        "outcome_ledger",
        "rollback",
    }
    assert all(stage["status"] in {"pass", "fail", "skip"} for stage in result["stages"])


def test_replay_result_includes_baseline_delta_and_improvement(tmp_path: Path) -> None:
    scenario = load_learning_eval_scenario("repeated_tool_failure_generates_delta")

    result = run_learning_eval(scenario, _options(tmp_path))

    assert set(result.replay) >= {"baseline_score", "delta_score", "improvement"}
    assert result.replay["baseline_score"] < result.replay["delta_score"]
    assert result.replay["improvement"] > 0
