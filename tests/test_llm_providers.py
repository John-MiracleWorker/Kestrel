from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.anthropic_provider import AnthropicMessagesProvider
from nested_memvid_agent.llm.base import (
    FallbackLLMProvider,
    LLMProvider,
    ProviderCapabilities,
    ProviderError,
)
from nested_memvid_agent.llm.factory import build_llm_provider
from nested_memvid_agent.llm.gemini_provider import GeminiProvider
from nested_memvid_agent.llm.model_catalog import model_catalog_for_provider
from nested_memvid_agent.llm.ollama_provider import OllamaNativeProvider
from nested_memvid_agent.llm.openai_compatible_provider import OpenAICompatibleProvider
from nested_memvid_agent.llm.openai_provider import OpenAIResponsesProvider
from nested_memvid_agent.llm.parser import ControlMessageError, parse_agent_response
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


def test_factory_builds_provider_parity_aliases() -> None:
    openrouter = build_llm_provider(AgentConfig(provider="openrouter", model="openai/gpt-test"))
    deepseek = build_llm_provider(AgentConfig(provider="deepseek", model="deepseek-v4-pro"))
    kimi = build_llm_provider(AgentConfig(provider="kimi", model="kimi-k2.6"))
    ollama = build_llm_provider(AgentConfig(provider="ollama", model="llama3.1"))
    ollama_cloud = build_llm_provider(AgentConfig(provider="ollama-cloud", model="gpt-oss:120b"))
    anthropic = build_llm_provider(AgentConfig(provider="anthropic", model="claude-test"))
    gemini = build_llm_provider(AgentConfig(provider="gemini", model="gemini-test"))

    assert openrouter.capabilities.name == "openrouter"
    assert deepseek.capabilities.name == "deepseek"
    assert kimi.capabilities.name == "kimi"
    assert ollama.capabilities.name == "ollama"
    assert isinstance(ollama_cloud, OllamaNativeProvider)
    assert ollama_cloud.capabilities.name == "ollama-cloud"
    assert isinstance(anthropic, AnthropicMessagesProvider)
    assert isinstance(gemini, GeminiProvider)


def test_model_catalog_returns_static_models_for_mock() -> None:
    catalog = model_catalog_for_provider(AgentConfig(), "mock")

    assert catalog.ok is True
    assert catalog.fetchable is False
    assert catalog.models == ("mock",)


def test_model_catalog_fetches_openai_compatible_models(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_fetch_json(url: str, **kwargs: Any) -> Any:
        calls["url"] = url
        calls["kwargs"] = kwargs
        return {"data": [{"id": "local-a"}, {"id": "local-b"}]}

    monkeypatch.setattr("nested_memvid_agent.llm.model_catalog._fetch_json", fake_fetch_json)
    catalog = model_catalog_for_provider(
        AgentConfig(
            provider="openai-compatible",
            model="local-a",
            base_url="http://127.0.0.1:1234/v1",
        ),
        "openai-compatible",
    )

    assert catalog.ok is True
    assert catalog.models == ("local-a", "local-b")
    assert calls["url"] == "http://127.0.0.1:1234/v1/models"


def test_model_catalog_fetches_deepseek_models(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_fetch_json(url: str, **kwargs: Any) -> Any:
        calls["url"] = url
        calls["kwargs"] = kwargs
        return {"data": [{"id": "deepseek-v4-pro"}, {"id": "deepseek-v4-flash"}]}

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setattr("nested_memvid_agent.llm.model_catalog._fetch_json", fake_fetch_json)
    catalog = model_catalog_for_provider(AgentConfig(), "deepseek")

    assert catalog.ok is True
    assert catalog.models == ("deepseek-v4-pro", "deepseek-v4-flash")
    assert catalog.api_key_env == "DEEPSEEK_API_KEY"
    assert calls["url"] == "https://api.deepseek.com/models"
    assert calls["kwargs"]["api_key"] == "test-key"


def test_model_catalog_fetches_kimi_models(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_fetch_json(url: str, **kwargs: Any) -> Any:
        calls["url"] = url
        calls["kwargs"] = kwargs
        return {"data": [{"id": "kimi-k2.6"}, {"id": "kimi-k2.5"}]}

    monkeypatch.setenv("MOONSHOT_API_KEY", "test-key")
    monkeypatch.setattr("nested_memvid_agent.llm.model_catalog._fetch_json", fake_fetch_json)
    catalog = model_catalog_for_provider(AgentConfig(), "kimi")

    assert catalog.ok is True
    assert catalog.models == ("kimi-k2.6", "kimi-k2.5")
    assert catalog.api_key_env == "MOONSHOT_API_KEY"
    assert calls["url"] == "https://api.moonshot.ai/v1/models"
    assert calls["kwargs"]["api_key"] == "test-key"


def test_model_catalog_falls_back_when_provider_is_unconfigured() -> None:
    catalog = model_catalog_for_provider(AgentConfig(), "ollama-cloud")

    assert catalog.ok is False
    assert catalog.fetchable is True
    assert catalog.models == ("gpt-oss:120b", "gpt-oss:20b")
    assert catalog.api_key_env == "OLLAMA_API_KEY"
    assert "OLLAMA_API_KEY" in str(catalog.error)


def test_strict_control_message_rejects_unknown_tool() -> None:
    with pytest.raises(ControlMessageError, match="unknown tool"):
        parse_agent_response(
            '{"message":"bad","tool_calls":[{"name":"missing.tool","arguments":{}}]}',
            tools=[
                ToolSpec(
                    name="memory.search",
                    description="Search",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                )
            ],
            strict=True,
        )


def test_strict_control_message_allows_plain_json_answers() -> None:
    response = parse_agent_response('{"status":"ok","items":[1,2]}', tools=[], strict=True)

    assert response.content == '{"status":"ok","items":[1,2]}'
    assert response.tool_calls == ()


def test_strict_control_message_validates_arguments() -> None:
    with pytest.raises(ControlMessageError, match="missing required"):
        parse_agent_response(
            '{"message":"bad","tool_calls":[{"name":"memory.search","arguments":{}}]}',
            tools=[
                ToolSpec(
                    name="memory.search",
                    description="Search",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                )
            ],
            strict=True,
        )


def test_control_message_parses_tool_retry_strategy() -> None:
    response = parse_agent_response(
        """
        {
          "message": "retry",
          "tool_calls": [
            {
              "name": "memory.search",
              "arguments": {"query": "pytest", "k": 3},
              "strategy": {
                "changed_strategy": "Search prior failure lessons before retrying.",
                "why_different": "This adds memory evidence.",
                "expected_signal": "Relevant lessons are returned.",
                "fallback_if_fails": "Inspect the current failure directly."
              }
            }
          ]
        }
        """,
        tools=[
            ToolSpec(
                name="memory.search",
                description="Search",
                parameters={
                    "type": "object",
                    "properties": {"query": {"type": "string"}, "k": {"type": "integer"}},
                    "required": ["query"],
                },
            )
        ],
        strict=True,
    )

    assert response.tool_calls[0].strategy is not None
    assert response.tool_calls[0].strategy.changed_strategy.startswith("Search prior failure")


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


def test_openai_compatible_provider_normalizes_native_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeCompletions:
        def create(self, **kwargs: Any) -> Any:
            assert kwargs["tools"][0]["function"]["name"] == "memory.search"
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        finish_reason="tool_calls",
                        message=SimpleNamespace(
                            content="",
                            tool_calls=[
                                SimpleNamespace(
                                    id="call_local",
                                    function=SimpleNamespace(name="memory.search", arguments='{"query":"local","k":1}'),
                                )
                            ],
                        ),
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=3, completion_tokens=2, total_tokens=5),
            )

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
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
        tools=[
            ToolSpec(
                name="memory.search",
                description="Search",
                parameters={"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer"}}},
            )
        ],
    )

    assert response.tool_calls[0].id == "call_local"
    assert response.tool_calls[0].arguments == {"query": "local", "k": 1}
    assert response.usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_ollama_native_provider_uses_chat_api(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    def fake_post_json(url: str, payload: dict[str, Any], **kwargs: Any) -> Any:
        calls["url"] = url
        calls["payload"] = payload
        calls["kwargs"] = kwargs
        return {
            "message": {
                "role": "assistant",
                "content": '{"message":"cloud ok"}',
                "tool_calls": [
                    {
                        "function": {
                            "name": "memory.search",
                            "arguments": {"query": "cloud"},
                        }
                    }
                ],
            },
            "prompt_eval_count": 5,
            "eval_count": 7,
            "done_reason": "stop",
        }

    monkeypatch.setattr("nested_memvid_agent.llm.ollama_provider._post_json", fake_post_json)
    provider = OllamaNativeProvider(model="gpt-oss:120b", api_key="test-key")
    response = provider.generate(
        [ChatMessage(role="user", content="hello")],
        tools=[
            ToolSpec(
                name="memory.search",
                description="Search",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            )
        ],
        options=LLMOptions(timeout_seconds=11, temperature=0.4),
    )

    assert calls["url"] == "https://ollama.com/api/chat"
    assert calls["kwargs"]["api_key"] == "test-key"
    assert calls["payload"]["model"] == "gpt-oss:120b"
    assert calls["payload"]["stream"] is False
    assert calls["payload"]["options"] == {"temperature": 0.4}
    assert calls["payload"]["tools"][0]["function"]["name"] == "memory.search"
    assert response.content == "cloud ok"
    assert response.tool_calls[0].arguments == {"query": "cloud"}
    assert response.usage == {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12}


def test_anthropic_provider_normalizes_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeMessages:
        def create(self, **kwargs: Any) -> Any:
            assert kwargs["tools"][0]["name"] == "memory.search"
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="I will search."),
                    SimpleNamespace(type="tool_use", id="toolu_123", name="memory.search", input={"query": "anthropic"}),
                ],
                usage=SimpleNamespace(input_tokens=4, output_tokens=5),
                stop_reason="tool_use",
            )

    class FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.messages = FakeMessages()

    def fake_import(name: str) -> Any:
        assert name == "anthropic"
        return SimpleNamespace(Anthropic=FakeAnthropic)

    monkeypatch.setattr("nested_memvid_agent.llm.anthropic_provider.import_module", fake_import)
    provider = AnthropicMessagesProvider(model="claude-test", api_key="test-key")
    response = provider.generate(
        [ChatMessage(role="user", content="hello")],
        tools=[
            ToolSpec(
                name="memory.search",
                description="Search",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            )
        ],
    )

    assert response.content == "I will search."
    assert response.tool_calls[0].id == "toolu_123"
    assert response.tool_calls[0].arguments == {"query": "anthropic"}
    assert response.finish_reason == "tool_use"


def test_anthropic_provider_streams_tokens_and_final_tool_use(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStream:
        def __enter__(self) -> FakeStream:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def __iter__(self) -> Any:
            return iter(
                [
                    SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="hello ")),
                    SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text="world")),
                ]
            )

        def get_final_message(self) -> Any:
            return SimpleNamespace(
                content=[
                    SimpleNamespace(type="text", text="hello world"),
                    SimpleNamespace(type="tool_use", id="toolu_stream", name="memory.search", input={"query": "stream"}),
                ],
                usage=SimpleNamespace(input_tokens=1, output_tokens=2),
                stop_reason="tool_use",
            )

    class FakeMessages:
        def stream(self, **kwargs: Any) -> FakeStream:
            assert kwargs["model"] == "claude-test"
            return FakeStream()

    class FakeAnthropic:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.messages = FakeMessages()

    def fake_import(name: str) -> Any:
        assert name == "anthropic"
        return SimpleNamespace(Anthropic=FakeAnthropic)

    monkeypatch.setattr("nested_memvid_agent.llm.anthropic_provider.import_module", fake_import)
    provider = AnthropicMessagesProvider(model="claude-test", api_key="test-key")
    events = list(
        provider.stream(
            [ChatMessage(role="user", content="hello")],
            tools=[
                ToolSpec(
                    name="memory.search",
                    description="Search",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                )
            ],
        )
    )

    assert [event.content for event in events if event.type == "token"] == ["hello ", "world"]
    tool_event = next(event for event in events if event.type == "tool_call")
    assert tool_event.tool_call is not None
    assert tool_event.tool_call.id == "toolu_stream"
    assert events[-1].type == "message_complete"


def test_gemini_provider_normalizes_function_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeModels:
        def generate_content(self, **kwargs: Any) -> Any:
            assert kwargs["config"]["tools"][0]["function_declarations"][0]["name"] == "memory.search"
            return SimpleNamespace(
                text="",
                function_calls=[SimpleNamespace(name="memory.search", args={"query": "gemini"})],
                usage_metadata=SimpleNamespace(prompt_token_count=2, candidates_token_count=3, total_token_count=5),
                candidates=[SimpleNamespace(finish_reason="STOP")],
            )

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.models = FakeModels()

    def fake_import(name: str) -> Any:
        assert name == "google.genai"
        return SimpleNamespace(Client=FakeClient)

    monkeypatch.setattr("nested_memvid_agent.llm.gemini_provider.import_module", fake_import)
    provider = GeminiProvider(model="gemini-test", api_key="test-key")
    response = provider.generate(
        [ChatMessage(role="user", content="hello")],
        tools=[
            ToolSpec(
                name="memory.search",
                description="Search",
                parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            )
        ],
    )

    assert response.tool_calls[0].name == "memory.search"
    assert response.tool_calls[0].arguments == {"query": "gemini"}
    assert response.usage == {"input_tokens": 2, "output_tokens": 3, "total_tokens": 5}


def test_gemini_provider_streams_tokens_and_function_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeModels:
        def generate_content_stream(self, **kwargs: Any) -> Any:
            assert kwargs["model"] == "gemini-test"
            return iter(
                [
                    SimpleNamespace(text="hello ", function_calls=[]),
                    SimpleNamespace(
                        text="world",
                        function_calls=[SimpleNamespace(name="memory.search", args={"query": "gemini-stream"})],
                        usage_metadata=SimpleNamespace(
                            prompt_token_count=1,
                            candidates_token_count=2,
                            total_token_count=3,
                        ),
                        candidates=[SimpleNamespace(finish_reason="STOP")],
                    ),
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.models = FakeModels()

    def fake_import(name: str) -> Any:
        assert name == "google.genai"
        return SimpleNamespace(Client=FakeClient)

    monkeypatch.setattr("nested_memvid_agent.llm.gemini_provider.import_module", fake_import)
    provider = GeminiProvider(model="gemini-test", api_key="test-key")
    events = list(
        provider.stream(
            [ChatMessage(role="user", content="hello")],
            tools=[
                ToolSpec(
                    name="memory.search",
                    description="Search",
                    parameters={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
                )
            ],
        )
    )

    assert [event.content for event in events if event.type == "token"] == ["hello ", "world"]
    tool_event = next(event for event in events if event.type == "tool_call")
    assert tool_event.tool_call is not None
    assert tool_event.tool_call.arguments == {"query": "gemini-stream"}
    usage_event = next(event for event in events if event.type == "usage")
    assert usage_event.data == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    assert events[-1].type == "message_complete"


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


def test_openai_responses_provider_streams_deltas_and_final_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeStream:
        def __enter__(self) -> FakeStream:
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        def __iter__(self) -> Any:
            return iter(
                [
                    SimpleNamespace(type="response.output_text.delta", delta="hello "),
                    SimpleNamespace(type="response.output_text.delta", delta="world"),
                ]
            )

        def get_final_response(self) -> Any:
            return SimpleNamespace(
                output_text="hello world",
                output=[
                    SimpleNamespace(
                        type="function_call",
                        name="memory.search",
                        arguments='{"query":"stream"}',
                        call_id="call_stream",
                    )
                ],
                usage={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                status="completed",
            )

    class FakeResponses:
        def stream(self, **kwargs: Any) -> FakeStream:
            assert kwargs["model"] == "gpt-test"
            return FakeStream()

    class FakeOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            del kwargs
            self.responses = FakeResponses()

    def fake_import(name: str) -> Any:
        assert name == "openai"
        return SimpleNamespace(OpenAI=FakeOpenAI)

    monkeypatch.setattr("nested_memvid_agent.llm.openai_provider.import_module", fake_import)
    provider = OpenAIResponsesProvider(model="gpt-test", api_key="test-key")

    events = list(provider.stream([ChatMessage(role="user", content="hello")], tools=[]))

    assert [event.content for event in events if event.type == "token"] == ["hello ", "world"]
    tool_event = next(event for event in events if event.type == "tool_call")
    assert tool_event.tool_call is not None
    assert tool_event.tool_call.name == "memory.search"
    assert events[-1].type == "message_complete"
