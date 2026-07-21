from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from scripts.run_golden_evals import (
    _aggregate_passed,
    _case_config,
    _cost_measurement,
    _eval_honest_test_failure,
    _golden_case_workspace,
    _report_exit_code,
    _run_case,
    _summary,
)


def test_golden_eval_report_exits_nonzero_when_any_case_fails() -> None:
    failed = _run_case("synthetic_failure", lambda: {"passed": False})
    summary = _summary([failed])
    report = {
        "results": [failed],
        "summary": summary,
        "passed": summary["fail_count"] == 0,
    }

    assert report["passed"] is False
    assert _report_exit_code(report) == 1


def test_golden_eval_report_exits_zero_only_for_explicit_pass() -> None:
    assert _report_exit_code({"passed": True}) == 0
    assert _report_exit_code({"passed": False}) == 1
    assert _report_exit_code({}) == 1


def test_unpriced_cases_report_cost_as_unmeasured_not_passing() -> None:
    result = _run_case("unpriced", lambda: {"passed": True})
    summary = _summary([result])

    assert result["cost_estimate_usd"] is None
    assert summary["cost_estimate_usd_total"] is None
    assert summary["acceptance"]["cost"]["measurement_status"] == "unmeasured"
    assert summary["acceptance"]["cost"]["passed"] is None
    assert summary["categories"]["cost"]["score"] is None
    assert summary["categories"]["cost"]["pass_count"] is None


def test_cost_measurement_marks_partial_coverage_without_claiming_acceptance() -> None:
    measurement = _cost_measurement(
        [
            {"cost_estimate_usd": 0.125},
            {"cost_estimate_usd": None},
        ]
    )

    assert measurement["measurement_status"] == "partially_measured"
    assert measurement["cost_estimate_usd_total"] == 0.125
    assert measurement["measured_case_count"] == 1
    assert measurement["unmeasured_case_count"] == 1
    assert measurement["passed"] is None


def test_latency_is_measured_but_not_claimed_as_passing_without_a_gate() -> None:
    result = {
        "name": "synthetic",
        "category": "repo_regression",
        "passed": True,
        "latency_ms": 2500.0,
        "context_chars": 0,
        "tool_count": 0,
        "cost_estimate_usd": None,
    }

    summary = _summary([result])

    assert summary["acceptance"]["latency"]["measurement_status"] == "measured"
    assert summary["acceptance"]["latency"]["gate_configured"] is False
    assert summary["acceptance"]["latency"]["passed"] is None
    assert summary["categories"]["latency"]["score"] is None
    assert _aggregate_passed(summary) is True


def test_configured_latency_gate_participates_in_top_level_acceptance() -> None:
    result = {
        "name": "synthetic",
        "category": "repo_regression",
        "passed": True,
        "latency_ms": 2500.0,
        "context_chars": 0,
        "tool_count": 0,
        "cost_estimate_usd": None,
    }

    passing = _summary([result], max_case_latency_ms=3000.0)
    failing = _summary([result], max_case_latency_ms=2000.0)

    assert passing["acceptance"]["latency"]["passed"] is True
    assert _aggregate_passed(passing) is True
    assert failing["acceptance"]["latency"]["passed"] is False
    assert failing["categories"]["latency"]["fail_count"] == 1
    assert _aggregate_passed(failing) is False


def test_aggregate_acceptance_fails_closed_on_missing_latency_metadata() -> None:
    assert _aggregate_passed({"fail_count": 0}) is False
    assert _aggregate_passed({"fail_count": 0, "acceptance": {"latency": {}}}) is False


def test_case_config_isolates_all_runtime_control_paths(tmp_path: Path) -> None:
    pinned_image = "example.invalid/kestrel-validation@sha256:" + "a" * 64
    config = AgentConfig(
        workspace=tmp_path / "caller-workspace",
        memory_dir=tmp_path / "eval-memory",
        validation_container_image=pinned_image,
    )

    isolated = _case_config(config, "eval-id", "case-name")
    isolation_root = tmp_path / "eval-memory" / "eval-id"

    for path in (
        isolated.memory_dir,
        isolated.log_dir,
        isolated.state_path,
        isolated.secret_store_path,
        isolated.skills_dir,
        isolated.plugins_dir,
        isolated.mcp_config_path,
        isolated.channel_config_path,
        isolated.worker_worktree_dir,
    ):
        path.resolve(strict=False).relative_to(isolation_root.resolve(strict=False))
    assert isolated.workspace == config.workspace
    assert isolated.validation_container_image == pinned_image


def test_golden_case_workspace_is_portable_and_bounded_to_eval_root(tmp_path: Path) -> None:
    config = AgentConfig(memory_dir=tmp_path / "golden" / "case-memory")

    workspace = _golden_case_workspace(config, "patch")

    workspace.resolve().relative_to(config.memory_dir.parent.resolve())
    assert workspace == tmp_path / "golden" / "workspaces" / "case-memory-patch"
    assert workspace.is_dir()
    assert "/private/tmp" not in Path("scripts/run_golden_evals.py").read_text(
        encoding="utf-8"
    )


def test_golden_case_errors_are_redacted() -> None:
    def fail_with_secret() -> dict[str, object]:
        raise RuntimeError(
            "OPENAI_API_KEY=sk-proj-validationsecret123456"  # gitleaks:allow -- synthetic fixture
        )

    result = _run_case("redaction", fail_with_secret)

    assert result["passed"] is False
    assert "validationsecret" not in str(result["error"])


def test_honest_failure_fixture_isolated_from_caller_workspace_secrets(
    tmp_path: Path,
    contained_validation_stub: str,
) -> None:
    caller_workspace = tmp_path / "caller"
    caller_workspace.mkdir()
    (caller_workspace / ".env.telegram").write_text(
        "TELEGRAM_BOT_TOKEN=live-operator-secret\n",
        encoding="utf-8",
    )
    config = AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        workspace=caller_workspace,
        memory_dir=tmp_path / "eval" / "memory",
        log_dir=tmp_path / "eval" / "logs",
        state_path=tmp_path / "eval" / "state" / "agent.db",
        validation_container_image=contained_validation_stub,
    )

    result = _eval_honest_test_failure(config)

    assert result == {
        "passed": True,
        "tool_count": 1,
        "error": "nonzero_exit",
    }
