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
from .parser import (
    ControlMessageError,
    native_tool_arguments,
    native_tool_call_id,
    native_tool_name,
    normalize_tool_calls,
    parse_agent_response,
)


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
            raise RuntimeError(
                "Install the Google Gen AI SDK with `pip install google-genai`."
            ) from exc

        client = genai_module.Client(api_key=self.api_key)
        config = {
            "temperature": active_options.temperature,
            "tools": [{"function_declarations": [_gemini_function(tool) for tool in tools]}]
            if tools
            else None,
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
            raise RuntimeError(
                "Install the Google Gen AI SDK with `pip install google-genai`."
            ) from exc

        client = genai_module.Client(api_key=self.api_key)
        stream_fn = getattr(client.models, "generate_content_stream", None)
        if not callable(stream_fn):
            yield from super().stream(messages, tools, active_options)
            return
        config = {
            "temperature": active_options.temperature,
            "tools": [{"function_declarations": [_gemini_function(tool) for tool in tools]}]
            if tools
            else None,
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
        except ControlMessageError:
            raise
        except Exception as exc:  # noqa: BLE001
            yield LLMStreamEvent(
                type="provider_error",
                content=str(exc),
                data={"code": type(exc).__name__, "retryable": True},
            )
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc


def _gemini_contents(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []

    def append_parts(role: str, parts: list[dict[str, Any]]) -> None:
        if rendered and rendered[-1]["role"] == role:
            rendered[-1]["parts"].extend(parts)
        else:
            rendered.append({"role": role, "parts": parts})

    for message in messages:
        if message.role == "tool":
            append_parts(
                "user",
                [
                    {
                        "function_response": {
                            "name": message.name or "tool",
                            "response": {"output": message.content},
                        }
                    }
                ],
            )
            continue
        role = "model" if message.role == "assistant" else "user"
        parts: list[dict[str, Any]] = []
        if message.content:
            prefix = "SYSTEM:\n" if message.role == "system" else ""
            parts.append({"text": f"{prefix}{message.content}"})
        if message.role == "assistant":
            parts.extend(
                {
                    "function_call": {
                        "name": call.name,
                        "args": call.arguments,
                    }
                }
                for call in message.tool_calls
            )
        if parts:
            append_parts(role, parts)
    return rendered


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
    raw_calls = _value(response, "function_calls")
    if raw_calls is not None:
        if not isinstance(raw_calls, list | tuple):
            raise ControlMessageError(
                "gemini.function_calls must be a list",
                code="invalid_tool_call",
            )
        if raw_calls:
            return [
                _gemini_tool_call(call, location=f"gemini.function_calls[{index}]")
                for index, call in enumerate(raw_calls)
            ]

    calls: list[ToolCall] = []
    raw_candidates = _value(response, "candidates")
    if raw_candidates is None:
        return calls
    if not isinstance(raw_candidates, list | tuple):
        raise ControlMessageError(
            "gemini.candidates must be a list",
            code="invalid_tool_call",
        )
    for candidate_index, candidate in enumerate(raw_candidates):
        candidate_location = f"gemini.candidates[{candidate_index}]"
        if not _is_native_object(candidate):
            raise ControlMessageError(
                f"{candidate_location} must be an object",
                code="invalid_tool_call",
            )
        content = _value(candidate, "content")
        if content is None:
            continue
        if not _is_native_object(content):
            raise ControlMessageError(
                f"{candidate_location}.content must be an object",
                code="invalid_tool_call",
            )
        raw_parts = _value(content, "parts")
        if raw_parts is None:
            continue
        if not isinstance(raw_parts, list | tuple):
            raise ControlMessageError(
                f"{candidate_location}.content.parts must be a list",
                code="invalid_tool_call",
            )
        for part_index, part in enumerate(raw_parts):
            part_location = f"{candidate_location}.content.parts[{part_index}]"
            if not _is_native_object(part):
                raise ControlMessageError(
                    f"{part_location} must be an object",
                    code="invalid_tool_call",
                )
            function_call = _value(part, "function_call")
            if function_call is None:
                continue
            calls.append(_gemini_tool_call(function_call, location=part_location))
    return calls


def _gemini_tool_call(call: Any, *, location: str) -> ToolCall:
    if not _is_native_object(call):
        raise ControlMessageError(
            f"{location}.function_call must be an object",
            code="invalid_tool_call",
        )
    name = native_tool_name(_value(call, "name"), location=location)
    arguments = native_tool_arguments(
        _value(call, "args"),
        tool_name=name,
        location=location,
    )
    return ToolCall(
        name=name,
        arguments=arguments,
        id=native_tool_call_id(_value(call, "id"), location=f"{location}.id"),
    )


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


def _is_native_object(value: Any) -> bool:
    return isinstance(value, dict) or (
        value is not None and not isinstance(value, str | bytes | list | tuple | bool | int | float)
    )
