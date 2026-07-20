from __future__ import annotations

import json
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
    native_tool_name,
    normalize_tool_calls,
    parse_agent_response,
)


class OpenAIResponsesProvider(LLMProvider):
    """Minimal OpenAI Responses API provider.

    Provider-neutral runtime messages are mapped to native Responses function-call and
    function-call-output items while the same strict Kestrel validation boundary remains
    authoritative.
    """

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        temperature: float = 0.2,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.getenv(api_key_env or "OPENAI_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            name="openai",
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
            openai_module = import_module("openai")
        except ImportError as exc:
            raise RuntimeError("Install the OpenAI SDK with `pip install openai`.") from exc

        OpenAI = openai_module.OpenAI
        client = OpenAI(
            api_key=self.api_key,
            timeout=active_options.timeout_seconds,
            max_retries=active_options.max_retries,
        )
        request = self._request_payload(messages, tools, active_options)
        try:
            response = client.responses.create(**request)
        except Exception as exc:  # noqa: BLE001 - provider boundary maps SDK failures
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc
        return _responses_to_llm_response(response, tools=tools)

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
            openai_module = import_module("openai")
        except ImportError as exc:
            raise RuntimeError("Install the OpenAI SDK with `pip install openai`.") from exc

        client = openai_module.OpenAI(
            api_key=self.api_key,
            timeout=active_options.timeout_seconds,
            max_retries=active_options.max_retries,
        )
        stream_fn = getattr(client.responses, "stream", None)
        if not callable(stream_fn):
            yield from super().stream(messages, tools, active_options)
            return
        try:
            final_response: Any | None = None
            with stream_fn(**self._request_payload(messages, tools, active_options)) as stream:
                for event in stream:
                    delta = _stream_text_delta(event)
                    if delta:
                        yield LLMStreamEvent(type="token", content=delta)
                get_final = getattr(stream, "get_final_response", None)
                if callable(get_final):
                    final_response = get_final()
            if final_response is not None:
                response = _responses_to_llm_response(final_response, tools=tools)
                for tool_call in response.tool_calls:
                    yield LLMStreamEvent(type="tool_call", tool_call=tool_call)
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

    def _request_payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "input": _to_responses_input(messages),
            "temperature": options.temperature,
        }
        if tools:
            request["tools"] = [_to_responses_tool(tool) for tool in tools]
        return request


def _to_responses_input(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "tool":
            rendered.append(
                {
                    "type": "function_call_output",
                    "call_id": message.tool_call_id or "missing_tool_call_id",
                    "output": message.content,
                }
            )
            continue
        if message.role == "assistant" and message.tool_calls:
            if message.content:
                rendered.append({"role": "assistant", "content": message.content})
            rendered.extend(
                {
                    "type": "function_call",
                    "call_id": call.id,
                    "name": call.name,
                    "arguments": json.dumps(
                        call.arguments,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
                for call in message.tool_calls
            )
            continue
        rendered.append({"role": message.role, "content": message.content})
    return rendered


def _to_responses_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _responses_to_llm_response(response: Any, *, tools: list[ToolSpec] | tuple[ToolSpec, ...]) -> LLMResponse:
    text = _response_text(response)
    parsed = parse_agent_response(text, tools=tools, strict=True)
    native_calls = normalize_tool_calls(_response_tool_calls(response), tools=tools)
    tool_calls = native_calls or parsed.tool_calls
    return LLMResponse(
        content=parsed.content if parsed.raw is not None else text.strip(),
        tool_calls=tool_calls,
        raw=response,
        usage=_usage_dict(response),
        finish_reason=_finish_reason(response),
    )


def _response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if text is not None:
        return str(text)
    output = getattr(response, "output", None)
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            content = _item_value(item, "content")
            if isinstance(content, list):
                for block in content:
                    block_text = _item_value(block, "text")
                    if block_text is not None:
                        parts.append(str(block_text))
        if parts:
            return "\n".join(parts)
    return str(response)


def _response_tool_calls(response: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    output = getattr(response, "output", None)
    if output is None:
        return calls
    if not isinstance(output, list | tuple):
        raise ControlMessageError(
            "responses.output must be a list",
            code="invalid_tool_call",
        )
    for index, item in enumerate(output):
        item_type = _item_value(item, "type")
        if item_type not in {"function_call", "tool_call"}:
            continue
        location = f"responses.output[{index}]"
        if not _is_native_object(item):
            raise ControlMessageError(f"{location} must be an object", code="invalid_tool_call")
        name = native_tool_name(_item_value(item, "name"), location=location)
        arguments = native_tool_arguments(
            _item_value(item, "arguments"),
            tool_name=name,
            location=location,
        )
        call_id = _item_value(item, "call_id") or _item_value(item, "id")
        calls.append(
            ToolCall(
                name=name,
                arguments=arguments,
                id=str(call_id) if call_id else f"tool_{name}",
            )
        )
    return calls


def _stream_text_delta(event: Any) -> str:
    event_type = str(_item_value(event, "type") or "")
    if event_type not in {
        "response.output_text.delta",
        "response.refusal.delta",
        "output_text.delta",
        "text_delta",
    }:
        return ""
    delta = _item_value(event, "delta")
    if delta is None:
        delta = _item_value(event, "text")
    return "" if delta is None else str(delta)


def _usage_dict(response: Any) -> dict[str, Any] | None:
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    if isinstance(usage, dict):
        return dict(usage)
    if hasattr(usage, "model_dump"):
        dumped = usage.model_dump()
        return dict(dumped) if isinstance(dumped, dict) else None
    fields = {
        key: getattr(usage, key)
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if getattr(usage, key, None) is not None
    }
    return fields or None


def _finish_reason(response: Any) -> str | None:
    status = getattr(response, "status", None)
    return str(status) if status is not None else None


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


def _is_native_object(value: Any) -> bool:
    return isinstance(value, dict) or (
        value is not None
        and not isinstance(value, str | bytes | list | tuple | bool | int | float)
    )
