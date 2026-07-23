from __future__ import annotations

import pytest

from nested_memvid_agent.routing.runtime import AdaptiveFlockRuntimeConfig


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
