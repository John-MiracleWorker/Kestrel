from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig


def test_from_env_uses_only_the_explicit_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NEST_AGENT_PROVIDER", "host-provider")
    monkeypatch.setenv("NEST_AGENT_API_KEY_ENV", "HOST_PROVIDER_SECRET")
    monkeypatch.setenv("NEST_AGENT_FALLBACK_MODEL", "host-fallback")
    monkeypatch.setenv("NEST_AGENT_ALLOW_WEB", "0")
    monkeypatch.setenv("NEST_AGENT_TIMEOUT_SECONDS", "999")
    monkeypatch.setenv("NEST_AGENT_TEMPERATURE", "0.99")
    monkeypatch.setenv("NEST_AGENT_PROTECTED_BRANCHES", "host/*")
    monkeypatch.setenv("NEST_AGENT_LAYER_CONFIG", "/host/layers.json")
    environment = {
        "NEST_AGENT_PROVIDER": "injected-provider",
        "NEST_AGENT_API_KEY_ENV": "INJECTED_PROVIDER_SECRET",
        "NEST_AGENT_ALLOW_WEB": "true",
        "NEST_AGENT_TIMEOUT_SECONDS": "17",
        "NEST_AGENT_TEMPERATURE": "0.25",
        "NEST_AGENT_PROTECTED_BRANCHES": "main,release/*",
        "NEST_AGENT_LAYER_CONFIG": "/injected/layers.json",
    }

    config = AgentConfig.from_env(environment)

    assert config.provider == "injected-provider"
    assert config.api_key_env == "INJECTED_PROVIDER_SECRET"
    assert config.fallback_model is None
    assert config.allow_web is True
    assert config.timeout_seconds == 17
    assert config.temperature == 0.25
    assert config.protected_branches == ("main", "release/*")
    assert config.layer_config_path == Path("/injected/layers.json")
