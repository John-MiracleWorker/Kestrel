from __future__ import annotations

import os
from collections.abc import Iterator
from importlib import import_module
from typing import Any

from ..runtime_models import (
    ChatMessage,
    LLMOptions,
    LLMResponse,
    LLMStreamEvent,
    ToolCall,
    ToolSpec,
)
from .base import LLMProvider, ProviderCapabilities, ProviderError
from .parser import normalize_tool_calls, parse_agent_response


class AnthropicMessagesProvider(LLMProvider):
    """Anthropic Messages provider adapter with Kestrel's strict tool boundary."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.getenv(api_key_env or "ANTHROPIC_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature
        self.max_tokens = max_tokens

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="anthropic",
            supports_native_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_system_messages=True,
            token_usage_available=True,
        )

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        active_options = options or LLMOptions(
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            temperature=self.temperature,
        )
        try:
            anthropic_module = import_module("anthropic")
        except ImportError as exc:
            raise RuntimeError("Install the Anthropic SDK with `pip install anthropic`.") from exc

        client = anthropic_module.Anthropic(api_key=self.api_key, timeout=active_options.timeout_seconds)
        try:
            response = client.messages.create(
                model=self.model,
                system=_system_prompt(messages),
                messages=_anthropic_messages(messages),
                tools=[_anthropic_tool(tool) for tool in tools] or None,
                temperature=active_options.temperature,
                max_tokens=self.max_tokens,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc
        return _anthropic_response_to_llm_response(response, tools=tools)

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> Iterator[LLMStreamEvent]:
        active_options = options or LLMOptions(
            timeout_seconds=self.timeout_seconds,
            max_retries=self.max_retries,
            temperature=self.temperature,
            stream=True,
        )
        try:
            anthropic_module = import_module("anthropic")
        except ImportError as exc:
            raise RuntimeError("Install the Anthropic SDK with `pip install anthropic`.") from exc

        client = anthropic_module.Anthropic(api_key=self.api_key, timeout=active_options.timeout_seconds)
        stream_fn = getattr(client.messages, "stream", None)
        if not callable(stream_fn):
            yield from super().stream(messages, tools, active_options)
            return
        text_parts: list[str] = []
        try:
            final_message: Any | None = None
            with stream_fn(
                model=self.model,
                system=_system_prompt(messages),
                messages=_anthropic_messages(messages),
                tools=[_anthropic_tool(tool) for tool in tools] or None,
                temperature=active_options.temperature,
                max_tokens=self.max_tokens,
            ) as stream:
                for event in stream:
                    delta = _anthropic_stream_text_delta(event)
                    if delta:
                        text_parts.append(delta)
                        yield LLMStreamEvent(type="token", content=delta)
                get_final = getattr(stream, "get_final_message", None) or getattr(stream, "get_final_response", None)
                if callable(get_final):
                    final_message = get_final()
            if final_message is None:
                final_message = {"content": [{"type": "text", "text": "".join(text_parts)}], "stop_reason": "stream_end"}
            response = _anthropic_response_to_llm_response(final_message, tools=tools)
            for call in response.tool_calls:
                yield LLMStreamEvent(type="tool_call", tool_call=call)
            if response.usage:
                yield LLMStreamEvent(type="usage", data=response.usage)
            yield LLMStreamEvent(type="message_complete", response=response)
        except Exception as exc:  # noqa: BLE001
            yield LLMStreamEvent(
                type="provider_error",
                content=str(exc),
                data={"code": type(exc).__name__, "retryable": True},
            )
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc


def _system_prompt(messages: list[ChatMessage]) -> str:
    return "\n\n".join(message.content for message in messages if message.role == "system")


def _anthropic_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        if message.role == "tool":
            rendered.append(
                {
                    "role": "user",
                    "content": f"Tool result from {message.name or 'tool'}:\n{message.content}",
                }
            )
            continue
        role = "assistant" if message.role == "assistant" else "user"
        rendered.append({"role": role, "content": message.content})
    return rendered


def _anthropic_tool(tool: ToolSpec) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "input_schema": tool.parameters}


def _anthropic_response_to_llm_response(
    response: Any,
    *,
    tools: list[ToolSpec] | tuple[ToolSpec, ...],
) -> LLMResponse:
    text_parts: list[str] = []
    native_calls: list[ToolCall] = []
    for block in getattr(response, "content", []) or []:
        block_type = _value(block, "type")
        if block_type == "text":
            text = _value(block, "text")
            if text is not None:
                text_parts.append(str(text))
        elif block_type == "tool_use":
            name = _value(block, "name")
            if isinstance(name, str) and name:
                raw_input = _value(block, "input")
                native_calls.append(
                    ToolCall(
                        name=name,
                        arguments=dict(raw_input) if isinstance(raw_input, dict) else {},
                        id=str(_value(block, "id") or f"tool_{name}"),
                    )
                )
    text = "\n".join(text_parts)
    parsed = parse_agent_response(text, tools=tools, strict=True)
    return LLMResponse(
        content=parsed.content if parsed.raw is not None else text.strip(),
        tool_calls=normalize_tool_calls(native_calls, tools=tools) or parsed.tool_calls,
        raw=response,
        usage=_usage_dict(response),
        finish_reason=None if _value(response, "stop_reason") is None else str(_value(response, "stop_reason")),
    )


def _usage_dict(response: Any) -> dict[str, Any] | None:
    usage = _value(response, "usage")
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    fields = {
        key: _value(usage, key)
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if _value(usage, key) is not None
    }
    return fields or None


def _anthropic_stream_text_delta(event: Any) -> str:
    event_type = str(_value(event, "type") or "")
    delta = _value(event, "delta")
    if event_type in {"content_block_delta", "text_delta"}:
        text = _value(delta, "text")
        if text is not None:
            return str(text)
    if event_type == "content_block_start":
        block = _value(event, "content_block")
        if _value(block, "type") == "text":
            text = _value(block, "text")
            if text is not None:
                return str(text)
    text = _value(event, "text")
    return str(text) if event_type == "text" and text is not None else ""


def _value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)
