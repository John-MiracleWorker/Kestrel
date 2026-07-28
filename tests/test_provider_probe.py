from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.model_catalog import (
    MAX_MODEL_CATALOG_BYTES,
    MAX_MODEL_ID_CHARS,
    ProviderModelCatalog,
    _fetch_json,
    model_catalog_for_provider,
)
from nested_memvid_agent.provider_probe import (
    CapabilityEvidence,
    ModelProbeObservation,
    ProviderProbeService,
    routing_constraint_presets,
)
from nested_memvid_agent.routing.models import ProviderProfile


def _profile(*, secret_ref: str | None = None) -> ProviderProfile:
    return ProviderProfile(
        profile_id="local",
        display_name="Local models",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        secret_ref=secret_ref,
        locality="local",
    )


def test_probe_service_combines_catalog_declarations_with_observed_evidence() -> None:
    catalog = ProviderModelCatalog(
        provider="openai-compatible",
        models=("qwen-coder",),
        fallback_models=("local-model",),
        source="provider",
        ok=True,
        fetchable=True,
        fetched_at="2026-07-28T12:00:00+00:00",
        declared_capabilities={"qwen-coder": ("vision",)},
    )

    class Backend:
        def probe(
            self,
            profile: ProviderProfile,
            model: str,
            *,
            timeout_seconds: float,
        ) -> ModelProbeObservation:
            assert profile.profile_id == "local"
            assert model == "qwen-coder"
            assert timeout_seconds == 2.0
            return ModelProbeObservation(
                model=model,
                model_identity="qwen-coder-v2",
                latency_ms=18.25,
                capabilities=(
                    CapabilityEvidence.observed_pass("generation"),
                    CapabilityEvidence.observed_pass("streaming"),
                    CapabilityEvidence.observed_pass("structured_output"),
                    CapabilityEvidence.observed_pass("tools"),
                ),
            )

    service = ProviderProbeService(
        catalog_loader=lambda _profile, _timeout: catalog,
        probe_backend=Backend(),
        clock=lambda: datetime(2026, 7, 28, 12, 1, tzinfo=UTC),
    )

    result = service.discover(
        _profile(),
        max_models=2,
        timeout_seconds=2.0,
        probe_capabilities=True,
    )

    assert result.catalog_digest == catalog.digest
    assert result.catalog_fetched_at == "2026-07-28T12:00:00+00:00"
    assert result.probed_at == "2026-07-28T12:01:00+00:00"
    assert len(result.models) == 1
    model = result.models[0]
    assert model.model == "qwen-coder"
    assert model.model_identity == "qwen-coder-v2"
    assert model.latency_ms == 18.25
    evidence = {item.capability: item for item in model.capabilities}
    assert evidence["generation"].provenance == "observed"
    assert evidence["generation"].status == "pass"
    assert evidence["vision"].supported is True
    assert evidence["vision"].provenance == "provider_declared"


def test_probe_service_does_not_treat_fallback_catalog_as_discovery() -> None:
    catalog = ProviderModelCatalog(
        provider="openai-compatible",
        models=("hard-coded-fallback",),
        fallback_models=("hard-coded-fallback",),
        source="fallback",
        ok=False,
        fetchable=True,
        error="request failed with key=raw-secret-value",
    )
    service = ProviderProbeService(
        catalog_loader=lambda _profile, _timeout: catalog,
        secret_resolver=lambda _ref: "raw-secret-value",
    )

    result = service.discover(
        _profile(secret_ref="secret://local"),
        max_models=1,
        timeout_seconds=1.0,
        probe_capabilities=False,
    )

    assert result.models == ()
    assert result.catalog_ok is False
    assert "raw-secret-value" not in str(result.to_public_payload())
    assert "<redacted>" in str(result.to_public_payload())


def test_probe_backend_failure_is_redacted_and_keeps_capabilities_unknown() -> None:
    catalog = ProviderModelCatalog(
        provider="openai-compatible",
        models=("qwen-coder",),
        fallback_models=(),
        source="provider",
        ok=True,
        fetchable=True,
        fetched_at="2026-07-28T12:00:00+00:00",
    )

    class Backend:
        def probe(
            self,
            profile: ProviderProfile,
            model: str,
            *,
            timeout_seconds: float,
        ) -> ModelProbeObservation:
            del profile, model, timeout_seconds
            raise RuntimeError("authorization raw-secret-value rejected")

    service = ProviderProbeService(
        catalog_loader=lambda _profile, _timeout: catalog,
        probe_backend=Backend(),
        secret_resolver=lambda _ref: "raw-secret-value",
    )
    result = service.discover(
        _profile(secret_ref="secret://local"),
        max_models=1,
        timeout_seconds=1.0,
        probe_capabilities=True,
    )

    payload = result.to_public_payload()
    assert "raw-secret-value" not in str(payload)
    assert payload["models"][0]["capabilities"][0]["status"] == "fail"
    assert payload["models"][0]["capabilities"][0]["supported"] is None


def test_routing_presets_are_filter_or_rank_only() -> None:
    presets = routing_constraint_presets()

    assert {preset.preset_id for preset in presets} == {
        "balanced",
        "cheapest-validated",
        "fastest",
        "frontier-review",
        "local-only",
        "privacy-first",
    }
    assert all(preset.effect == "filter_or_rank_only" for preset in presets)
    assert all(preset.can_enable_targets is False for preset in presets)
    assert all(preset.can_change_trust is False for preset in presets)


def test_operator_supplied_capability_is_explicitly_provenanced() -> None:
    evidence = CapabilityEvidence.operator_supplied("tools", supported=True)

    assert evidence.supported is True
    assert evidence.provenance == "operator_supplied"
    assert evidence.status == "pass"


def test_live_catalog_preserves_explicit_provider_capability_declarations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_json(_url: str, **_kwargs: Any) -> Any:
        return {
            "data": [
                {
                    "id": "vision-coder",
                    "capabilities": ["streaming", "tools"],
                    "architecture": {"input_modalities": ["text", "image"]},
                    "supported_parameters": ["response_format"],
                }
            ]
        }

    monkeypatch.setattr("nested_memvid_agent.llm.model_catalog._fetch_json", fake_fetch_json)

    catalog = model_catalog_for_provider(
        AgentConfig(
            provider="openai-compatible",
            model="vision-coder",
            base_url="http://127.0.0.1:1234/v1",
        ),
        "openai-compatible",
    )

    assert catalog.declared_capabilities["vision-coder"] == (
        "streaming",
        "structured_output",
        "tools",
        "vision",
    )


def test_catalog_response_body_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == MAX_MODEL_CATALOG_BYTES + 1
            return b"x" * limit

    monkeypatch.setattr(
        "nested_memvid_agent.llm.model_catalog.urlopen",
        lambda _request, timeout: Response(),
    )

    with pytest.raises(ValueError, match="byte limit"):
        _fetch_json(
            "http://127.0.0.1:1234/v1/models",
            timeout_seconds=1,
            api_key=None,
        )


def test_live_catalog_drops_unbounded_or_control_character_model_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_fetch_json(_url: str, **_kwargs: Any) -> Any:
        return {
            "data": [
                {"id": "valid/model:1"},
                {"id": "bad\nmodel"},
                {"id": "x" * (MAX_MODEL_ID_CHARS + 1)},
            ]
        }

    monkeypatch.setattr("nested_memvid_agent.llm.model_catalog._fetch_json", fake_fetch_json)
    catalog = model_catalog_for_provider(
        AgentConfig(
            provider="openai-compatible",
            model="valid/model:1",
            base_url="http://127.0.0.1:1234/v1",
        ),
        "openai-compatible",
    )

    assert catalog.models == ("valid/model:1",)
