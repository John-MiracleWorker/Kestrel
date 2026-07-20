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
    native_tool_name,
    normalize_tool_calls,
    parse_agent_response,
)


class OpenAICompatibleProvider(LLMProvider):
    """OpenAI-compatible chat completions provider for local servers such as LM Studio."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        temperature: float = 0.2,
        provider_name: str = "openai-compatible",
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or os.getenv(api_key_env or "OPENAI_API_KEY") or "not-needed"
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature
        self.provider_name = provider_name

    @property
    def capabilities(self) -> ProviderCapabilities:
        constrained_local_provider = self.provider_name in {
            "lm-studio",
            "ollama",
            "openai-compatible",
        }
        return ProviderCapabilities(
            name=self.provider_name,
            supports_native_tools=True,
            supports_streaming=True,
            supports_json_mode=True,
            supports_system_messages=True,
            token_usage_available=True,
            native_tool_limit=12 if constrained_local_provider else None,
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
            base_url=self.base_url,
            timeout=active_options.timeout_seconds,
            max_retries=active_options.max_retries,
        )
        request = self._request_payload(messages, tools, active_options)
        try:
            response = client.chat.completions.create(**request)
        except Exception as exc:  # noqa: BLE001 - provider boundary maps SDK failures
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc
        return _chat_completion_to_llm_response(response, tools=tools)

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
            base_url=self.base_url,
            timeout=active_options.timeout_seconds,
            max_retries=active_options.max_retries,
        )
        request = {**self._request_payload(messages, tools, active_options), "stream": True}
        content_parts: list[str] = []
        tool_buffers: dict[int, dict[str, str]] = {}
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        try:
            for chunk in client.chat.completions.create(**request):
                usage = _usage_dict(chunk) or usage
                choice = _first_choice(chunk)
                if choice is None:
                    continue
                finish_reason = str(getattr(choice, "finish_reason", finish_reason) or finish_reason or "")
                delta = getattr(choice, "delta", None)
                token = getattr(delta, "content", None)
                if token:
                    content_parts.append(str(token))
                    yield LLMStreamEvent(type="token", content=str(token))
                _accumulate_delta_tool_calls(tool_buffers, getattr(delta, "tool_calls", None))
        except ControlMessageError:
            raise
        except Exception as exc:  # noqa: BLE001
            yield LLMStreamEvent(
                type="provider_error",
                content=str(exc),
                data={"code": type(exc).__name__, "retryable": True},
            )
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc

        text = "".join(content_parts)
        parsed = parse_agent_response(text, tools=tools, strict=True)
        native_calls = normalize_tool_calls(_buffered_tool_calls(tool_buffers), tools=tools)
        response = LLMResponse(
            content=parsed.content if parsed.raw is not None else text.strip(),
            tool_calls=native_calls or parsed.tool_calls,
            raw={"stream_completed": True, "provider": self.provider_name},
            usage=usage,
            finish_reason=finish_reason or None,
        )
        for call in response.tool_calls:
            yield LLMStreamEvent(type="tool_call", tool_call=call)
        if response.usage:
            yield LLMStreamEvent(type="usage", data=response.usage)
        yield LLMStreamEvent(type="message_complete", response=response)

    def _request_payload(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_chat_completion_dict(message) for message in messages],
            "temperature": options.temperature,
        }
        if tools:
            request["tools"] = [_to_chat_completion_tool(tool) for tool in tools]
            request["tool_choice"] = "auto"
        return request


def _to_chat_completion_dict(message: ChatMessage) -> dict[str, Any]:
    payload = message.to_openai_dict()
    if payload["role"] == "tool":
        # Chat Completions requires a native assistant tool_calls item followed
        # by a tool result carrying the same tool_call_id.  The agent runtime
        # now preserves that pair in provider-neutral ChatMessage fields.
        payload.pop("name", None)
    return payload


def _to_chat_completion_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _chat_completion_to_llm_response(response: Any, *, tools: list[ToolSpec] | tuple[ToolSpec, ...]) -> LLMResponse:
    text = _first_choice_text(response)
    parsed = parse_agent_response(text, tools=tools, strict=True)
    native_calls = normalize_tool_calls(_chat_completion_tool_calls(response), tools=tools)
    return LLMResponse(
        content=parsed.content if parsed.raw is not None else text.strip(),
        tool_calls=native_calls or parsed.tool_calls,
        raw=response,
        usage=_usage_dict(response),
        finish_reason=_choice_finish_reason(response),
    )


def _first_choice_text(response: Any) -> str:
    choice = _first_choice(response)
    if choice is not None:
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        if content is not None:
            return str(content)
    return str(response)


def _first_choice(response: Any) -> Any | None:
    choices = getattr(response, "choices", None)
    if choices:
        return choices[0]
    return None


def _chat_completion_tool_calls(response: Any) -> list[ToolCall]:
    choice = _first_choice(response)
    if choice is None:
        return []
    message = getattr(choice, "message", None)
    raw_calls = _item_value(message, "tool_calls")
    calls: list[ToolCall] = []
    if raw_calls is None:
        return calls
    if not isinstance(raw_calls, list | tuple):
        raise ControlMessageError(
            "chat.completions.tool_calls must be a list",
            code="invalid_tool_call",
        )
    for index, item in enumerate(raw_calls):
        location = f"chat.completions.tool_calls[{index}]"
        function = _native_function_block(item, location=location)
        name = native_tool_name(_item_value(function, "name"), location=location)
        arguments = native_tool_arguments(
            _item_value(function, "arguments"),
            tool_name=name,
            location=location,
        )
        calls.append(
            ToolCall(
                name=name,
                arguments=arguments,
                id=str(_item_value(item, "id") or f"tool_{name}"),
            )
        )
    return calls


def _accumulate_delta_tool_calls(buffers: dict[int, dict[str, str]], raw_calls: Any) -> None:
    if raw_calls is None:
        return
    if not isinstance(raw_calls, list | tuple):
        raise ControlMessageError(
            "chat.completions.delta.tool_calls must be a list",
            code="invalid_tool_call",
        )
    for position, item in enumerate(raw_calls):
        location = f"chat.completions.delta.tool_calls[{position}]"
        if not _is_native_object(item):
            raise ControlMessageError(f"{location} must be an object", code="invalid_tool_call")
        raw_index = _item_value(item, "index")
        if raw_index is None:
            index = 0
        elif isinstance(raw_index, int) and not isinstance(raw_index, bool) and raw_index >= 0:
            index = raw_index
        else:
            raise ControlMessageError(
                f"{location}.index must be a non-negative integer",
                code="invalid_tool_call",
            )
        buffer = buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
        item_id = _item_value(item, "id")
        if item_id is not None:
            if not isinstance(item_id, str):
                raise ControlMessageError(
                    f"{location}.id must be a string",
                    code="invalid_tool_call",
                )
            buffer["id"] = item_id
        function = _item_value(item, "function")
        if function is None:
            continue
        if not _is_native_object(function):
            raise ControlMessageError(
                f"{location}.function must be an object",
                code="invalid_tool_call",
            )
        name = _item_value(function, "name")
        if name is not None:
            if not isinstance(name, str):
                raise ControlMessageError(
                    f"{location} tool name fragments must be strings",
                    code="invalid_tool_name",
                )
            buffer["name"] += name
        arguments = _item_value(function, "arguments")
        if arguments is not None:
            if not isinstance(arguments, str):
                raise ControlMessageError(
                    f"{location} argument fragments must be strings",
                    code="invalid_tool_arguments",
                )
            buffer["arguments"] += arguments


def _buffered_tool_calls(buffers: dict[int, dict[str, str]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, item in sorted(buffers.items()):
        location = f"chat.completions.delta.tool_calls[{index}]"
        name = native_tool_name(item["name"], location=location)
        arguments = native_tool_arguments(
            item["arguments"],
            tool_name=name,
            location=location,
        )
        calls.append(
            ToolCall(
                name=name,
                arguments=arguments,
                id=item["id"] or f"tool_{name}",
            )
        )
    return calls


def _native_function_block(item: Any, *, location: str) -> Any:
    if not _is_native_object(item):
        raise ControlMessageError(f"{location} must be an object", code="invalid_tool_call")
    function = _item_value(item, "function")
    if not _is_native_object(function):
        raise ControlMessageError(
            f"{location}.function must be an object",
            code="invalid_tool_call",
        )
    return function


def _is_native_object(value: Any) -> bool:
    return isinstance(value, dict) or (
        value is not None
        and not isinstance(value, str | bytes | list | tuple | bool | int | float)
    )


def _item_value(item: Any, key: str) -> Any:
    if isinstance(item, dict):
        return item.get(key)
    return getattr(item, key, None)


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
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if getattr(usage, key, None) is not None
    }
    return fields or None


def _choice_finish_reason(response: Any) -> str | None:
    choice = _first_choice(response)
    if choice is None:
        return None
    reason = getattr(choice, "finish_reason", None)
    return str(reason) if reason is not None else None
