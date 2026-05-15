from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from ..runtime_models import ChatMessage, LLMOptions, LLMResponse, LLMStreamEvent, ToolSpec


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LLMProvider(ABC):
    @abstractmethod
    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        raise NotImplementedError

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> Iterator[LLMStreamEvent]:
        response = self.generate(messages, tools, options)
        if response.content:
            yield LLMStreamEvent(type="token", content=response.content)
        for tool_call in response.tool_calls:
            yield LLMStreamEvent(type="tool_call", tool_call=tool_call)
        yield LLMStreamEvent(type="message_complete", response=response)
