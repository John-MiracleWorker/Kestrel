from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.routing.activation_evaluator import (
    ActivationEvaluator,
    EvaluationBindings,
)
from nested_memvid_agent.routing.models import AgentTaskContract
from nested_memvid_agent.routing.qualification_evaluator import ScopeEvaluationInput
from nested_memvid_agent.routing.qualification_ledger import QualificationLedger
from nested_memvid_agent.routing.qualification_records import ActivationGrant
from nested_memvid_agent.routing.runtime import AdaptiveFlockRuntimeConfig
from nested_memvid_agent.state_store import AgentStateStore


def test_disabled_runtime_ignores_staged_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "false")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "shadow")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_POLICY", "balanced")

    config = AdaptiveFlockRuntimeConfig.from_env()

    assert config.enabled is False
    assert config.mode == "off"
    assert config.policy_id == "balanced"


def test_enabling_runtime_uses_staged_shadow_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "true")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "shadow")

    config = AdaptiveFlockRuntimeConfig.from_env()

    assert config.enabled is True
    assert config.mode == "shadow"


def test_enabled_runtime_rejects_explicit_off_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "true")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "off")

    with pytest.raises(ValueError, match="must not be off"):
        AdaptiveFlockRuntimeConfig.from_env()


def _unused_bindings() -> EvaluationBindings:
    return EvaluationBindings(
        project_authority={},
        privacy={},
        target_snapshot={},
        price_snapshot={},
        policy_payload={},
        learned_payload={},
    )


def _unreachable_evidence(grant: ActivationGrant) -> ScopeEvaluationInput:
    raise AssertionError(f"no grant must resolve, got {grant.grant_id}")


def test_replay_verified_env_flag_is_only_a_global_permit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The env flag never authorizes learned routing without a durable grant."""

    monkeypatch.setenv("NEST_AGENT_ENABLE_ADAPTIVE_FLOCK", "1")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_MODE", "adaptive")
    monkeypatch.setenv("NEST_AGENT_ADAPTIVE_FLOCK_LEARNED_REPLAY_VERIFIED", "1")

    config = AdaptiveFlockRuntimeConfig.from_env()

    assert config.enabled is True
    assert config.mode == "adaptive"
    assert config.learned_activation_replay_verified is True

    evaluator = ActivationEvaluator(
        QualificationLedger(AgentStateStore(tmp_path / "state" / "agent.db")),
        bindings=_unused_bindings,
        eligibility=lambda target_id: "eligible",
        evidence=_unreachable_evidence,
    )
    contract = AgentTaskContract(
        task_id="task_1",
        run_id="run_1",
        role="worker",
        task_family="repository_inspection",
        objective="inspect the repository",
        complexity=0.3,
        ambiguity=0.2,
        risk="low",
        required_capabilities=("repository_inspection",),
    )

    result = evaluator.evaluate(contract)

    assert result.effective is False
    assert result.grant_id is None
    assert result.receipt_id is None
    assert result.reason_codes == ("durable_grant_required",)
