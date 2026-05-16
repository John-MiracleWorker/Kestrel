from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.base import (
    FallbackLLMProvider,
    LLMProvider,
    ProviderCapabilities,
    ProviderError,
)
from nested_memvid_agent.llm.factory import build_llm_provider
from nested_memvid_agent.llm.openai_compatible_provider import OpenAICompatibleProvider
from nested_memvid_agent.llm.openai_provider import OpenAIResponsesProvider
from nested_memvid_agent.runtime_models import ChatMessage, LLMOptions, LLMResponse, ToolSpec


class RecordingProvider(LLMProvider):
    def __init__(self, response: LLMResponse | None = None, error: ProviderError | None = None) -> None:
        self.response = response or LLMResponse(content="ok")
        self.error = error
        self.calls = 0

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del messages, tools, options
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def test_provider_capabilities_have_serializable_metadata() -> None:
    caps = ProviderCapabilities(
        name="test-provider",
        supports_native_tools=True,
        supports_streaming=True,
        supports_json_mode=False,
        supports_system_messages=True,
        max_context_tokens=8192,
        token_usage_available=True,
    )

    assert caps.to_payload() == {
        "name": "test-provider",
        "supports_native_tools": True,
        "supports_streaming": True,
        "supports_json_mode": False,
        "supports_system_messages": True,
        "max_context_tokens": 8192,
        "token_usage_available": True,
    }


def test_fallback_provider_uses_secondary_for_retryable_primary_error() -> None:
    primary = RecordingProvider(error=ProviderError("temporary", code="rate_limit", retryable=True))
    secondary = RecordingProvider(response=LLMResponse(content="secondary ok"))
    provider = FallbackLLMProvider(primary, secondary)

    response = provider.generate([ChatMessage(role="user", content="hello")], tools=[])

    assert response.content == "secondary ok"
    assert primary.calls == 1
    assert secondary.calls == 1
    assert response.raw["provider_fallback"]["from_error_code"] == "rate_limit"


def test_fallback_provider_does_not_fallback_for_non_retryable_error() -> None:
    primary = RecordingProvider(error=ProviderError("bad key", code="auth_error", retryable=False))
    secondary = RecordingProvider(response=LLMResponse(content="should not run"))
    provider = FallbackLLMProvider(primary, secondary)

    with pytest.raises(ProviderError):
        provider.generate([ChatMessage(role="user", content="hello")], tools=[])

    assert primary.calls == 1
    assert secondary.calls == 0


def test_factory_wraps_configured_fallback_provider() -> None:
    provider = build_llm_provider(AgentConfig(provider="mock", fallback_provider="mock"))

    assert isinstance(provider, FallbackLLMProvider)
    assert provider.capabilities.name == "fallback:mock->mock"


def test_factory_builds_openai_compatible_provider() -> None:
    provider = build_llm_provider(
        AgentConfig(
            provider="openai-compatible",
            model="local-model",
            base_url="http://127.0.0.1:1234/v1",
        )
    )

    assert isinstance(provider, OpenAICompatibleProvider)


def test_openai_compatible_provider_uses_chat_completions(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            calls["request"] = kwargs
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content='{"message": "local ok"}'))]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            calls["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    def fake_import(name: str) -> Any:
        assert name == "openai"
        return SimpleNamespace(OpenAI=FakeOpenAI)

    monkeypatch.setattr("nested_memvid_agent.llm.openai_compatible_provider.import_module", fake_import)
    provider = OpenAICompatibleProvider(
        model="local-model",
        base_url="http://127.0.0.1:1234/v1",
        api_key="local-key",
    )

    response = provider.generate(
        [ChatMessage(role="user", content="hello")],
        tools=[],
        options=LLMOptions(timeout_seconds=7, max_retries=4, temperature=0.1),
    )

    assert response.content == "local ok"
    assert calls["client"] == {
        "api_key": "local-key",
        "base_url": "http://127.0.0.1:1234/v1",
        "timeout": 7,
        "max_retries": 4,
    }
    assert calls["request"]["model"] == "local-model"
    assert calls["request"]["temperature"] == 0.1
    assert calls["request"]["messages"][-1] == {"role": "user", "content": "hello"}


def test_openai_responses_provider_normalizes_native_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class FakeResponses:
        def create(self, **kwargs: Any) -> Any:
            calls["request"] = kwargs
            return SimpleNamespace(
                output_text="I should inspect memory.",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="memory.search",
                        arguments='{"query":"needle","k":2}',
                        call_id="call_123",
                    )
                ],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
                status="completed",
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            calls["client"] = kwargs
            self.responses = FakeResponses()

    def fake_import(name: str) -> Any:
        assert name == "openai"
        return SimpleNamespace(OpenAI=FakeOpenAI)

    monkeypatch.setattr("nested_memvid_agent.llm.openai_provider.import_module", fake_import)
    provider = OpenAIResponsesProvider(model="gpt-test", api_key="test-key")

    response = provider.generate(
        [ChatMessage(role="user", content="hello")],
        tools=[
            ToolSpec(
                name="memory.search",
                description="Search memory",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}},
            )
        ],
        options=LLMOptions(timeout_seconds=3, max_retries=1, temperature=0.0),
    )

    assert response.content == "I should inspect memory."
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].id == "call_123"
    assert response.tool_calls[0].name == "memory.search"
    assert response.tool_calls[0].arguments == {"query": "needle", "k": 2}
    assert response.usage == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
    assert response.finish_reason == "completed"
    assert calls["request"]["tools"][0]["type"] == "function"
    assert calls["request"]["tools"][0]["name"] == "memory.search"


def test_openai_responses_provider_keeps_json_envelope_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponses:
        def create(self, **kwargs: Any) -> Any:
            del kwargs
            return SimpleNamespace(
                output_text='{"message":"fallback","tool_calls":[{"name":"memory.search","arguments":{"query":"fallback"}}]}',
                output=[],
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.responses = FakeResponses()

    def fake_import(name: str) -> Any:
        assert name == "openai"
        return SimpleNamespace(OpenAI=FakeOpenAI)

    monkeypatch.setattr("nested_memvid_agent.llm.openai_provider.import_module", fake_import)
    provider = OpenAIResponsesProvider(model="gpt-test", api_key="test-key")

    response = provider.generate([ChatMessage(role="user", content="hello")], tools=[])

    assert response.content == "fallback"
    assert response.tool_calls[0].name == "memory.search"
    assert response.tool_calls[0].arguments == {"query": "fallback"}
