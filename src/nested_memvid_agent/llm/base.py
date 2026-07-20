from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass

from ..runtime_models import ChatMessage, LLMOptions, LLMResponse, LLMStreamEvent, ToolSpec


class ProviderError(RuntimeError):
    def __init__(self, message: str, *, code: str = "provider_error", retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ProviderCapabilities:
    name: str
    supports_native_tools: bool = False
    supports_streaming: bool = False
    supports_json_mode: bool = False
    supports_system_messages: bool = True
    max_context_tokens: int | None = None
    token_usage_available: bool = False
    native_tool_limit: int | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "supports_native_tools": self.supports_native_tools,
            "supports_streaming": self.supports_streaming,
            "supports_json_mode": self.supports_json_mode,
            "supports_system_messages": self.supports_system_messages,
            "max_context_tokens": self.max_context_tokens,
            "token_usage_available": self.token_usage_available,
            "native_tool_limit": self.native_tool_limit,
        }


class LLMProvider(ABC):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(name=type(self).__name__)

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


def _combined_native_tool_limit(primary: int | None, secondary: int | None) -> int | None:
    limits = [limit for limit in (primary, secondary) if limit is not None]
    return min(limits) if limits else None


class FallbackLLMProvider(LLMProvider):
    """Try a secondary provider only for retryable primary provider failures."""

    def __init__(self, primary: LLMProvider, secondary: LLMProvider) -> None:
        self.primary = primary
        self.secondary = secondary

    @property
    def capabilities(self) -> ProviderCapabilities:
        primary = self.primary.capabilities
        secondary = self.secondary.capabilities
        return ProviderCapabilities(
            name=f"fallback:{primary.name}->{secondary.name}",
            supports_native_tools=primary.supports_native_tools and secondary.supports_native_tools,
            supports_streaming=primary.supports_streaming and secondary.supports_streaming,
            supports_json_mode=primary.supports_json_mode and secondary.supports_json_mode,
            supports_system_messages=primary.supports_system_messages and secondary.supports_system_messages,
            max_context_tokens=min(
                token for token in (primary.max_context_tokens, secondary.max_context_tokens) if token is not None
            )
            if primary.max_context_tokens is not None or secondary.max_context_tokens is not None
            else None,
            token_usage_available=primary.token_usage_available and secondary.token_usage_available,
            native_tool_limit=_combined_native_tool_limit(
                primary.native_tool_limit,
                secondary.native_tool_limit,
            ),
        )

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        try:
            return self.primary.generate(messages, tools, options)
        except ProviderError as exc:
            if not exc.retryable:
                raise
            response = self.secondary.generate(messages, tools, options)
            raw = response.raw if isinstance(response.raw, dict) else {"secondary_raw": response.raw}
            raw = {
                **raw,
                "provider_fallback": {
                    "from": self.primary.capabilities.name,
                    "to": self.secondary.capabilities.name,
                    "from_error_code": exc.code,
                },
            }
            return LLMResponse(
                content=response.content,
                tool_calls=response.tool_calls,
                raw=raw,
                usage=response.usage,
                finish_reason=response.finish_reason,
            )
