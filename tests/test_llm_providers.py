from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.llm.factory import build_llm_provider
from nested_memvid_agent.llm.openai_compatible_provider import OpenAICompatibleProvider
from nested_memvid_agent.runtime_models import ChatMessage, LLMOptions


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
