from __future__ import annotations

from collections.abc import Iterable

from ..runtime_models import ChatMessage, LLMOptions, LLMResponse, ToolCall, ToolSpec
from .base import LLMProvider, ProviderCapabilities


class MockLLMProvider(LLMProvider):
    """Deterministic provider for tests, dry-runs, and Codex scaffolding.

    If canned responses are supplied, they are returned in order. Otherwise the provider
    echoes the latest user message and asks for memory.search when the user message starts
    with '/search '.
    """

    def __init__(self, canned: Iterable[LLMResponse] | None = None) -> None:
        self._responses = list(canned or [])

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="mock",
            supports_native_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_system_messages=True,
            token_usage_available=False,
        )

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del options
        if self._responses:
            return self._responses.pop(0)
        if messages and messages[-1].role == "tool":
            return LLMResponse(content="I checked memory.")
        latest_user = next((msg.content for msg in reversed(messages) if msg.role == "user"), "")
        if latest_user.startswith("/search "):
            query = latest_user.removeprefix("/search ").strip()
            return LLMResponse(
                content="I’ll check memory for that.",
                tool_calls=(ToolCall(name="memory.search", arguments={"query": query, "k": 5}),),
            )
        return LLMResponse(content=f"Mock response: {latest_user}")
