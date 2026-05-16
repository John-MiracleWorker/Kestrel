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
from .parser import normalize_tool_calls, parse_agent_response


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
        return ProviderCapabilities(
            name=self.provider_name,
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
        payload["role"] = "user"
        payload["content"] = f"Tool result from {message.name or 'tool'}:\n{message.content}"
        payload.pop("tool_call_id", None)
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
    raw_calls = getattr(message, "tool_calls", None)
    calls: list[ToolCall] = []
    if not raw_calls:
        return calls
    for item in raw_calls:
        function = getattr(item, "function", None)
        name = getattr(function, "name", None)
        if not isinstance(name, str) or not name:
            continue
        calls.append(
            ToolCall(
                name=name,
                arguments=_arguments_dict(getattr(function, "arguments", None)),
                id=str(getattr(item, "id", "") or f"tool_{name}"),
            )
        )
    return calls


def _accumulate_delta_tool_calls(buffers: dict[int, dict[str, str]], raw_calls: Any) -> None:
    if not raw_calls:
        return
    for item in raw_calls:
        index = int(getattr(item, "index", 0) or 0)
        buffer = buffers.setdefault(index, {"id": "", "name": "", "arguments": ""})
        item_id = getattr(item, "id", None)
        if item_id:
            buffer["id"] = str(item_id)
        function = getattr(item, "function", None)
        name = getattr(function, "name", None)
        if name:
            buffer["name"] += str(name)
        arguments = getattr(function, "arguments", None)
        if arguments:
            buffer["arguments"] += str(arguments)


def _buffered_tool_calls(buffers: dict[int, dict[str, str]]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for item in buffers.values():
        if not item["name"]:
            continue
        calls.append(
            ToolCall(
                name=item["name"],
                arguments=_arguments_dict(item["arguments"]),
                id=item["id"] or f"tool_{item['name']}",
            )
        )
    return calls


def _arguments_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = os.linesep.join(raw.splitlines())
            parsed = json.loads(loaded)
        except Exception:  # noqa: BLE001 - invalid provider argument JSON becomes empty at parse stage
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


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
