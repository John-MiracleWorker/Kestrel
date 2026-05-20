from __future__ import annotations

import os
from pathlib import Path

from scripts.run_live_learning_eval import (
    DEFAULT_MODEL_ENV_BY_PROVIDER,
    LiveEvalCaseResult,
    build_live_eval_config,
    provider_readiness,
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
    assert config.timeout_seconds == 90
    assert config.allow_shell is False
    assert config.allow_file_write is False
    assert config.allow_policy_writes is False
    assert config.enable_task_capsules is True
    assert config.enable_behavior_deltas is True


def test_summarize_results_counts_cases_and_capabilities() -> None:
    results = [
        LiveEvalCaseResult(name="provider_handshake", passed=True, metrics={"tool_count": 0}),
        LiveEvalCaseResult(name="durable_memory_reopen", passed=True, metrics={"memory_writes": 4, "memory_hits": 1}),
        LiveEvalCaseResult(name="approval_gate", passed=False, error="approval was bypassed"),
    ]

    summary = summarize_results(results)

    assert summary["case_count"] == 3
    assert summary["pass_count"] == 2
    assert summary["fail_count"] == 1
    assert summary["memory_writes"] == 4
    assert summary["memory_hits"] == 1
    assert summary["passed"] is False


def test_provider_model_env_map_includes_ollama_cloud() -> None:
    assert DEFAULT_MODEL_ENV_BY_PROVIDER["ollama-cloud"] == "KESTREL_IT_OLLAMA_CLOUD_MODEL"
