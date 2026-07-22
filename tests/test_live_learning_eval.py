from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.models import MemoryKind, MemoryLayer, MemoryRecord
from nested_memvid_agent.runtime_models import LLMResponse
from scripts.run_live_learning_eval import (
    DEFAULT_MODEL_ENV_BY_PROVIDER,
    LiveEvalCaseResult,
    _case_correction_frame,
    _case_durable_memory_reopen,
    _case_postflight_memory_integrity,
    _case_procedural_promotion_gate,
    _case_provider_handshake,
    build_live_eval_config,
    provider_readiness,
    run_live_learning_eval,
    summarize_results,
)


def test_provider_readiness_requires_key_and_model_without_leaking_values(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.setenv("KESTREL_IT_OLLAMA_CLOUD_MODEL", "secret-model-name")

    readiness = provider_readiness("ollama-cloud", model=None)

    assert not readiness.available
    assert readiness.provider == "ollama-cloud"
    assert readiness.model == "secret-model-name"
    assert "OLLAMA_API_KEY" in readiness.reason
    assert "secret-model-name" not in readiness.reason


def test_provider_readiness_uses_explicit_model_without_model_env(monkeypatch) -> None:
    monkeypatch.setenv("OLLAMA_API_KEY", "do-not-print-this")
    monkeypatch.delenv("KESTREL_IT_OLLAMA_CLOUD_MODEL", raising=False)

    readiness = provider_readiness("ollama-cloud", model="gpt-oss:120b")

    assert readiness.available
    assert readiness.model == "gpt-oss:120b"
    assert "do-not-print-this" not in readiness.reason


def test_build_live_eval_config_is_isolated_under_output_root(tmp_path: Path) -> None:
    config = build_live_eval_config(
        provider="ollama-cloud",
        model="gpt-oss:120b",
        backend="memvid",
        output_root=tmp_path,
        timeout_seconds=90,
    )

    assert config.provider == "ollama-cloud"
    assert config.model == "gpt-oss:120b"
    assert config.backend == "memvid"
    assert config.memory_dir == tmp_path / "memory"
    assert config.log_dir == tmp_path / "logs"
    assert config.state_path == tmp_path / "state" / "agent.db"
    assert config.secret_store_path == tmp_path / "secrets" / "local_vault.json"
    assert config.skills_dir == tmp_path / "skills"
    assert config.plugins_dir == tmp_path / "plugins"
    assert config.mcp_config_path == tmp_path / "config" / "mcp_servers.json"
    assert config.channel_config_path == tmp_path / "config" / "channels.json"
    assert config.worker_worktree_dir == tmp_path / "worktrees"
    assert config.timeout_seconds == 90
    assert config.allow_shell is False
    assert config.allow_file_write is False
    assert config.allow_policy_writes is False
    assert config.enable_task_capsules is True
    assert config.enable_behavior_deltas is True


def test_summarize_results_counts_cases_and_capabilities() -> None:
    results = [
        LiveEvalCaseResult(name="provider_handshake", passed=True, metrics={"tool_count": 0}),
        LiveEvalCaseResult(
            name="durable_memory_reopen",
            passed=True,
            metrics={"memory_writes": 4, "memory_hits": 1},
        ),
        LiveEvalCaseResult(name="approval_gate", passed=False, error="approval was bypassed"),
    ]

    summary = summarize_results(results)

    assert summary["case_count"] == 3
    assert summary["pass_count"] == 2
    assert summary["fail_count"] == 1
    assert summary["memory_writes"] == 4
    assert summary["memory_hits"] == 1
    assert summary["passed"] is False


def test_summarize_results_rejects_empty_case_set() -> None:
    summary = summarize_results([])

    assert summary["case_count"] == 0
    assert summary["passed"] is False


def test_provider_model_env_map_includes_ollama_cloud() -> None:
    assert DEFAULT_MODEL_ENV_BY_PROVIDER["ollama-cloud"] == "KESTREL_IT_OLLAMA_CLOUD_MODEL"


def test_procedural_promotion_gate_uses_resolved_structured_evidence() -> None:
    result = _case_procedural_promotion_gate()

    assert result["passed"] is True
    assert result["one_off_action"] == "reject"
    assert result["repeated_action"] == "write"
    assert result["policy_action"] == "reject"
    assert result["evidence_mode"] == "synthetic_trusted_kernel_gate_no_memory_write"


def test_provider_handshake_rejects_nonempty_wrong_marker(monkeypatch) -> None:
    class WrongMarkerProvider:
        def generate(self, messages, *, tools, options):  # noqa: ANN001
            del messages, tools, options
            return LLMResponse(content="I did not follow the requested marker")

    monkeypatch.setattr(
        "scripts.run_live_learning_eval.build_llm_provider",
        lambda config: WrongMarkerProvider(),
    )

    result = _case_provider_handshake(AgentConfig(), "expected-marker")

    assert result["passed"] is False
    assert result["marker_match"] is False


def test_provider_handshake_accepts_exact_marker_after_outer_whitespace(monkeypatch) -> None:
    class ExactMarkerProvider:
        def generate(self, messages, *, tools, options):  # noqa: ANN001
            del messages, tools, options
            return LLMResponse(content="  expected-marker\n")

    monkeypatch.setattr(
        "scripts.run_live_learning_eval.build_llm_provider",
        lambda config: ExactMarkerProvider(),
    )

    result = _case_provider_handshake(AgentConfig(), "expected-marker")

    assert result["passed"] is True
    assert result["marker_match"] is True


def test_correction_case_rejects_unrelated_stale_correction(monkeypatch) -> None:
    stale = MemoryRecord(
        id="stale-correction",
        title="Old correction",
        content="An unrelated correction from a prior run.",
        layer=MemoryLayer.WORKING,
        kind=MemoryKind.CORRECTION,
        confidence=0.7,
    )

    class FakeMemory:
        def iter_records(self, *, include_inactive):  # noqa: ANN001
            assert include_inactive is True
            return iter((stale,))

    class FakeAgent:
        memory = FakeMemory()

        def chat(self, message, *, session_id):  # noqa: ANN001
            del message, session_id
            return SimpleNamespace(
                memory_writes=(),
                stop_reason="failed",
                assistant_message="",
            )

        def close(self):
            return None

    monkeypatch.setattr(
        "scripts.run_live_learning_eval.build_agent",
        lambda config: FakeAgent(),
    )

    result = _case_correction_frame(AgentConfig(), "fresh-marker")

    assert result["passed"] is False
    assert result["correction_count"] == 0
    assert result["total_correction_count"] == 1


def test_durable_memory_case_rejects_incomplete_turn_with_persisted_input(
    monkeypatch,
) -> None:
    class FirstAgent:
        def chat(self, message, *, session_id):  # noqa: ANN001
            del message, session_id
            return SimpleNamespace(
                memory_writes=("working", "episodic"),
                stop_reason="failed",
                assistant_message="",
                context_chars=42,
            )

        def close(self):
            return None

    class ReopenedAgent:
        memory = SimpleNamespace(retrieve=lambda query: [SimpleNamespace(record="stale")])

        def close(self):
            return None

    agents = iter((FirstAgent(), ReopenedAgent()))
    monkeypatch.setattr(
        "scripts.run_live_learning_eval.build_agent",
        lambda config: next(agents),
    )

    result = _case_durable_memory_reopen(AgentConfig(), "fresh-marker")

    assert result["passed"] is False
    assert result["memory_hits"] == 1
    assert result["stop_reason"] == "failed"


def test_correction_case_requires_current_marker_bound_write(tmp_path: Path) -> None:
    config = AgentConfig(
        provider="mock",
        model="mock",
        backend="memory",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        state_path=tmp_path / "state" / "agent.db",
        secret_store_path=tmp_path / "secrets" / "vault.json",
        workspace=tmp_path / "workspace",
    )

    result = _case_correction_frame(config, "fresh-marker")

    assert result["passed"] is True
    assert result["correction_count"] == 1


def test_live_eval_refuses_nonempty_direct_output_root(tmp_path: Path) -> None:
    (tmp_path / "stale-evidence.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="must be empty"):
        run_live_learning_eval(
            provider="mock",
            model="mock",
            backend="memory",
            output_root=tmp_path,
        )


def test_postflight_integrity_requires_all_layers_and_no_policy_writes(
    tmp_path: Path,
) -> None:
    config = build_live_eval_config(
        provider="mock",
        model="mock",
        backend="memory",
        output_root=tmp_path,
    )

    result = _case_postflight_memory_integrity(config)

    assert result["passed"] is True
    assert set(result["verified_layers"]) == {layer.value for layer in MemoryLayer}
    assert result["policy_write_count"] == 0
