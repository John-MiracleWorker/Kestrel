from __future__ import annotations

from pathlib import Path

import pytest

from nested_memvid_agent.routing import ModelTarget, ProviderProfile, RoutingLedger
from nested_memvid_agent.state_store import AgentStateStore


def test_routing_metadata_allows_non_secret_token_fields(tmp_path: Path) -> None:
    ledger = RoutingLedger(AgentStateStore(tmp_path / "state" / "agent.db"))
    ledger.put_provider_profile(
        ProviderProfile(
            profile_id="local",
            display_name="Local model server",
            adapter="openai-compatible",
            base_url="http://127.0.0.1:1234/v1",
            locality="local",
            metadata={
                "max_context_tokens": 131_072,
                "token_usage_available": True,
                "input_token_cost": 0.0,
            },
        )
    )
    stored = ledger.put_model_target(
        ModelTarget(
            target_id="local-worker",
            provider_profile_id="local",
            provider="openai-compatible",
            model="qwen-coder",
            locality="local",
            max_context_tokens=131_072,
            supports_tools=True,
            quality_tier=3,
            metadata={
                "output_token_cost": 0.0,
                "tokens_per_second": 22.5,
            },
        )
    )

    assert stored.target.metadata["tokens_per_second"] == 22.5


@pytest.mark.parametrize(
    "secret_key",
    ["api_key", "client_secret", "access_token", "refresh-token", "password"],
)
def test_routing_metadata_rejects_secret_bearing_fields(
    tmp_path: Path,
    secret_key: str,
) -> None:
    ledger = RoutingLedger(AgentStateStore(tmp_path / secret_key / "agent.db"))

    with pytest.raises(ValueError, match="secret-bearing key"):
        ledger.put_provider_profile(
            ProviderProfile(
                profile_id="unsafe",
                display_name="Unsafe profile",
                adapter="openai-compatible",
                locality="local",
                metadata={secret_key: "not-allowed"},
            )
        )
