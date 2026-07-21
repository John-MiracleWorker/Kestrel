from __future__ import annotations

from dataclasses import dataclass

import pytest

from nested_memvid_agent.llm.base import LLMProvider, ProviderError
from nested_memvid_agent.llm.parser import ControlMessageError
from nested_memvid_agent.llm.resilience import ProviderHealthRegistry, ResilientLLMProvider
from nested_memvid_agent.runtime_models import LLMResponse


@dataclass
class _Clock:
    value: float = 0.0

    def __call__(self) -> float:
        return self.value


class _SequenceProvider(LLMProvider):
    def __init__(self, results: list[LLMResponse | Exception]) -> None:
        self.results = list(results)
        self.calls = 0

    def generate(self, messages, tools, options=None):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_provider_circuit_opens_fails_fast_and_recovers_half_open() -> None:
    clock = _Clock()
    registry = ProviderHealthRegistry(clock=clock)
    provider = _SequenceProvider(
        [
            ProviderError("upstream timeout", code="TimeoutError", retryable=True),
            ProviderError("HTTP 503 unavailable", code="HTTPError", retryable=True),
            LLMResponse(content="recovered"),
        ]
    )
    resilient = ResilientLLMProvider(
        provider,
        provider_id="primary",
        registry=registry,
        failure_threshold=2,
        cooldown_seconds=10,
    )

    with pytest.raises(ProviderError, match="timed out"):
        resilient.generate([], [])
    with pytest.raises(ProviderError, match="unavailable"):
        resilient.generate([], [])
    with pytest.raises(ProviderError) as opened:
        resilient.generate([], [])
    assert opened.value.code == "circuit_open"
    assert opened.value.retryable is True
    assert provider.calls == 2
    assert registry.snapshot("primary")["state"] == "open"

    clock.value = 11
    assert resilient.generate([], []).content == "recovered"
    assert registry.snapshot("primary")["state"] == "healthy"
    assert provider.calls == 3


def test_provider_error_classification_makes_auth_failure_non_retryable() -> None:
    registry = ProviderHealthRegistry()
    provider = _SequenceProvider([ProviderError("HTTP 401 invalid API key", code="HTTPError", retryable=True)])
    resilient = ResilientLLMProvider(provider, provider_id="primary", registry=registry)

    with pytest.raises(ProviderError) as captured:
        resilient.generate([], [])

    assert captured.value.code == "authentication"
    assert captured.value.retryable is False
    health = registry.snapshot("primary")
    assert health["state"] == "degraded"
    assert health["failure_class"] == "authentication"
    assert "API key" not in str(health)


def test_provider_resilience_preserves_precise_invalid_tool_taxonomy() -> None:
    registry = ProviderHealthRegistry()
    provider = _SequenceProvider(
        [
            ControlMessageError(
                "diagnosis.classify missing required arguments: ['failure_text']",
                code="missing_tool_arguments",
            )
        ]
    )
    resilient = ResilientLLMProvider(provider, provider_id="primary", registry=registry)

    with pytest.raises(ProviderError) as captured:
        resilient.generate([], [])

    assert captured.value.code == "missing_tool_arguments"
    assert captured.value.retryable is False
    assert "failure_text" in str(captured.value)


def test_abandoned_half_open_stream_releases_the_probe_slot() -> None:
    clock = _Clock()
    registry = ProviderHealthRegistry(clock=clock)
    provider = _SequenceProvider(
        [
            ProviderError("upstream timeout with token=secret", code="TimeoutError", retryable=True),
            LLMResponse(content="partial"),
            LLMResponse(content="recovered"),
        ]
    )
    resilient = ResilientLLMProvider(
        provider,
        provider_id="primary",
        registry=registry,
        failure_threshold=1,
        cooldown_seconds=10,
    )
    with pytest.raises(ProviderError):
        resilient.generate([], [])
    clock.value = 11

    stream = resilient.stream([], [])
    assert next(stream).content == "partial"
    stream.close()  # type: ignore[attr-defined]

    assert resilient.generate([], []).content == "recovered"
    assert registry.snapshot("primary")["state"] == "healthy"
