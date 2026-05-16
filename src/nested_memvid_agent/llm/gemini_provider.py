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


class GeminiProvider(LLMProvider):
    """Google Gemini provider adapter with strict Kestrel control-message validation."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        temperature: float = 0.2,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.getenv(api_key_env or "GEMINI_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="gemini",
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
            genai_module = import_module("google.genai")
        except ImportError as exc:
            raise RuntimeError("Install the Google Gen AI SDK with `pip install google-genai`.") from exc

        client = genai_module.Client(api_key=self.api_key)
        config = {
            "temperature": active_options.temperature,
            "tools": [{"function_declarations": [_gemini_function(tool) for tool in tools]}] if tools else None,
        }
        try:
            response = client.models.generate_content(
                model=self.model,
                contents=_gemini_contents(messages),
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc
        return _gemini_response_to_llm_response(response, tools=tools)

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
            genai_module = import_module("google.genai")
        except ImportError as exc:
            raise RuntimeError("Install the Google Gen AI SDK with `pip install google-genai`.") from exc

        client = genai_module.Client(api_key=self.api_key)
        stream_fn = getattr(client.models, "generate_content_stream", None)
        if not callable(stream_fn):
            yield from super().stream(messages, tools, active_options)
            return
        config = {
            "temperature": active_options.temperature,
            "tools": [{"function_declarations": [_gemini_function(tool) for tool in tools]}] if tools else None,
        }
        text_parts: list[str] = []
        native_calls: list[ToolCall] = []
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        try:
            for chunk in stream_fn(
                model=self.model,
                contents=_gemini_contents(messages),
                config=config,
            ):
                text = str(getattr(chunk, "text", "") or "")
                if text:
                    text_parts.append(text)
                    yield LLMStreamEvent(type="token", content=text)
                native_calls.extend(_gemini_tool_calls(chunk))
                usage = _usage_dict(chunk) or usage
                finish_reason = _finish_reason(chunk) or finish_reason
            combined = "".join(text_parts)
            parsed = parse_agent_response(combined, tools=tools, strict=True)
            response = LLMResponse(
                content=parsed.content if parsed.raw is not None else combined.strip(),
                tool_calls=normalize_tool_calls(native_calls, tools=tools) or parsed.tool_calls,
                raw={"stream_completed": True, "provider": "gemini"},
                usage=usage,
                finish_reason=finish_reason,
            )
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


def _gemini_contents(messages: list[ChatMessage]) -> str:
    return "\n\n".join(
        f"{message.role.upper()}{f' {message.name}' if message.name else ''}:\n{message.content}"
        for message in messages
    )


def _gemini_function(tool: ToolSpec) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _gemini_response_to_llm_response(
    response: Any,
    *,
    tools: list[ToolSpec] | tuple[ToolSpec, ...],
) -> LLMResponse:
    text = str(getattr(response, "text", "") or "")
    native_calls = _gemini_tool_calls(response)
    parsed = parse_agent_response(text, tools=tools, strict=True)
    return LLMResponse(
        content=parsed.content if parsed.raw is not None else text.strip(),
        tool_calls=normalize_tool_calls(native_calls, tools=tools) or parsed.tool_calls,
        raw=response,
        usage=_usage_dict(response),
        finish_reason=_finish_reason(response),
    )


def _gemini_tool_calls(response: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    raw_calls = getattr(response, "function_calls", None)
    if raw_calls:
        for call in raw_calls:
            name = _value(call, "name")
            if isinstance(name, str) and name:
                args = _value(call, "args")
                calls.append(ToolCall(name=name, arguments=dict(args) if isinstance(args, dict) else {}))
    for candidate in getattr(response, "candidates", []) or []:
        content = _value(candidate, "content")
        for part in _value(content, "parts") or []:
            function_call = _value(part, "function_call")
            name = _value(function_call, "name")
            if isinstance(name, str) and name:
                args = _value(function_call, "args")
                calls.append(ToolCall(name=name, arguments=dict(args) if isinstance(args, dict) else {}))
    return calls


def _usage_dict(response: Any) -> dict[str, Any] | None:
    metadata = getattr(response, "usage_metadata", None)
    if metadata is None:
        return None
    fields = {
        "input_tokens": _value(metadata, "prompt_token_count"),
        "output_tokens": _value(metadata, "candidates_token_count"),
        "total_tokens": _value(metadata, "total_token_count"),
    }
    return {key: value for key, value in fields.items() if value is not None} or None


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None)
    if not candidates:
        return None
    reason = _value(candidates[0], "finish_reason")
    return str(reason) if reason is not None else None


def _value(item: Any, key: str) -> Any:
    if item is None:
        return None
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)
