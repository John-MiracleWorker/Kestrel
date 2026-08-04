from dataclasses import replace
from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig


def _lan_authority():
    from nested_memvid_agent.lan_discovery_models import (
        NetworkInterface,
        ResolvedLanEndpoint,
    )
    from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
    from nested_memvid_agent.lan_runtime_authority import (
        LAN_OPENAI_RUNTIME_HARDENING_VERSION,
        LanRuntimeAuthority,
        derive_lan_runtime_endpoint_binding_digest,
        derive_lan_runtime_provider_profile_id,
        derive_lan_runtime_target_id,
    )

    interface = NetworkInterface.from_addresses(
        os_identity="darwin:en7",
        display_name="darwin:en7",
        addresses=("192.168.50.7/24",),
    )
    scope = PrivateScanScope.from_request(interface, "192.168.50.0/24")
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.50.8", 1234)
    endpoint_binding_digest = derive_lan_runtime_endpoint_binding_digest(endpoint)
    provider_profile_id = derive_lan_runtime_provider_profile_id(endpoint_binding_digest)
    reviewed_target_id = derive_lan_runtime_target_id(provider_profile_id, "alpha")
    return LanRuntimeAuthority(
        scope=scope,
        endpoint=endpoint,
        source_address="192.168.50.7",
        os_interface_identity="darwin:en7",
        interface_index=7,
        provider_profile_id=provider_profile_id,
        reviewed_target_id=reviewed_target_id,
        model_id="alpha",
        api_shape="openai_compatible",
        runtime_adapter="lan-openai-compatible",
        runtime_hardening_version=LAN_OPENAI_RUNTIME_HARDENING_VERSION,
        endpoint_binding_digest=endpoint_binding_digest,
        endpoint_fingerprint="sha256:" + "3" * 64,
        reviewed_material_binding_digest="sha256:" + "4" * 64,
        review_digest="sha256:" + "5" * 64,
        fresh_until="2026-08-01T12:05:00Z",
    )


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


def test_lan_runtime_authority_is_internal_only_noncomparable_and_nonrepresentable() -> None:
    authority = _lan_authority()
    ordinary = AgentConfig(provider="lan-openai-compatible", model="alpha")
    assigned = replace(ordinary, lan_runtime_authority=authority)

    assert assigned.lan_runtime_authority is authority
    assert assigned == ordinary
    assert hash(assigned) == hash(ordinary)
    assert "lan_runtime_authority" not in repr(assigned)
    assert authority.reviewed_material_binding_digest not in repr(assigned)
    assert "lan_runtime_authority" not in assigned.to_mapping()
    assert authority.reviewed_material_binding_digest not in str(assigned.to_mapping())


def test_mapping_json_and_environment_cannot_supply_lan_runtime_authority(
    tmp_path: Path,
) -> None:
    raw = {
        "provider": "lan-openai-compatible",
        "model": "alpha",
        "lan_runtime_authority": {
            "address": "192.168.50.8",
            "review_digest": "sha256:" + "5" * 64,
        },
    }
    with pytest.raises(ValueError, match="unsupported agent configuration fields"):
        AgentConfig.from_mapping(raw)

    path = tmp_path / "config.json"
    path.write_text(__import__("json").dumps(raw))
    with pytest.raises(ValueError, match="unsupported agent configuration fields"):
        AgentConfig.from_json_file(path)

    from_env = AgentConfig.from_env(
        {
            "NEST_AGENT_PROVIDER": "lan-openai-compatible",
            "NEST_AGENT_MODEL": "alpha",
            "NEST_AGENT_LAN_RUNTIME_AUTHORITY": "forged-runtime-authority",
            "LAN_RUNTIME_AUTHORITY": "forged-runtime-authority",
        }
    )
    assert from_env.lan_runtime_authority is None
    assert "lan_runtime_authority" not in from_env.to_mapping()
