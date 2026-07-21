from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

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
from .provider_urls import validate_provider_http_url


class OllamaNativeProvider(LLMProvider):
    """Ollama native `/api/chat` provider, used for direct Ollama Cloud access."""

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "https://ollama.com/api",
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: int = 60,
        max_retries: int = 2,
        temperature: float = 0.2,
        provider_name: str = "ollama-cloud",
    ) -> None:
        self.model = model
        self.base_url = validate_provider_http_url(base_url)
        self.api_key_env = api_key_env or "OLLAMA_API_KEY"
        self.api_key = api_key or os.getenv(self.api_key_env)
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
            native_tool_limit=12,
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
        payload = self._request_payload(messages, tools, active_options, stream=False)
        try:
            response = _post_json(
                _join_url(self.base_url, "chat"),
                payload,
                timeout_seconds=active_options.timeout_seconds,
                api_key=self.api_key,
            )
        except Exception as exc:  # noqa: BLE001
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc
        return _ollama_response_to_llm_response(
            response, tools=tools, provider_name=self.provider_name
        )

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
        payload = self._request_payload(messages, tools, active_options, stream=True)
        content_parts: list[str] = []
        native_calls: list[ToolCall] = []
        usage: dict[str, Any] | None = None
        finish_reason: str | None = None
        try:
            for chunk in _post_json_lines(
                _join_url(self.base_url, "chat"),
                payload,
                timeout_seconds=active_options.timeout_seconds,
                api_key=self.api_key,
            ):
                message = chunk.get("message") if isinstance(chunk, dict) else None
                if isinstance(message, dict):
                    content = message.get("content")
                    if content:
                        token = str(content)
                        content_parts.append(token)
                        yield LLMStreamEvent(type="token", content=token)
                    native_calls.extend(_ollama_tool_calls(message.get("tool_calls")))
                usage = _usage_dict(chunk) or usage
                done_reason = chunk.get("done_reason") if isinstance(chunk, dict) else None
                finish_reason = str(done_reason) if done_reason else finish_reason
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
        response = LLMResponse(
            content=parsed.content if parsed.raw is not None else text.strip(),
            tool_calls=normalize_tool_calls(native_calls, tools=tools) or parsed.tool_calls,
            raw={"stream_completed": True, "provider": self.provider_name},
            usage=usage,
            finish_reason=finish_reason,
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
        *,
        stream: bool,
    ) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "messages": [_to_ollama_message(message) for message in messages],
            "stream": stream,
            "options": {"temperature": options.temperature},
        }
        if tools:
            request["tools"] = [_to_ollama_tool(tool) for tool in tools]
        return request


def _post_json(
    url: str, payload: dict[str, Any], *, timeout_seconds: float, api_key: str | None
) -> Any:
    request = _json_request(url, payload, api_key=api_key)
    try:
        # _json_request rejects every scheme except HTTP(S).
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama request failed with HTTP {exc.code}: {detail[:240]}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"Ollama request failed: {reason}") from exc
    return json.loads(body)


def _post_json_lines(
    url: str,
    payload: dict[str, Any],
    *,
    timeout_seconds: float,
    api_key: str | None,
) -> Iterator[dict[str, Any]]:
    request = _json_request(url, payload, api_key=api_key)
    try:
        # _json_request rejects every scheme except HTTP(S).
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
            for raw_line in response:
                line = raw_line.decode("utf-8").strip()
                if not line:
                    continue
                loaded = json.loads(line)
                if isinstance(loaded, dict):
                    yield loaded
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Ollama stream failed with HTTP {exc.code}: {detail[:240]}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"Ollama stream failed: {reason}") from exc


def _json_request(url: str, payload: dict[str, Any], *, api_key: str | None) -> Request:
    safe_url = validate_provider_http_url(url)
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return Request(
        safe_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )


def _join_url(base_url: str, suffix: str) -> str:
    safe_base_url = validate_provider_http_url(base_url)
    return validate_provider_http_url(urljoin(f"{safe_base_url.rstrip('/')}/", suffix))


def _to_ollama_message(message: ChatMessage) -> dict[str, Any]:
    if message.role == "tool":
        return {
            "role": "tool",
            "content": message.content,
            "tool_name": message.name or "tool",
        }
    role = "assistant" if message.role == "assistant" else message.role
    payload: dict[str, Any] = {"role": role, "content": message.content}
    if message.role == "assistant" and message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments},
            }
            for call in message.tool_calls
        ]
    return payload


def _to_ollama_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        },
    }


def _ollama_response_to_llm_response(
    response: Any,
    *,
    tools: list[ToolSpec] | tuple[ToolSpec, ...],
    provider_name: str,
) -> LLMResponse:
    message = response.get("message") if isinstance(response, dict) else None
    text = str(message.get("content", "") if isinstance(message, dict) else "")
    parsed = parse_agent_response(text, tools=tools, strict=True)
    native_calls = _ollama_tool_calls(
        message.get("tool_calls") if isinstance(message, dict) else None
    )
    finish_reason = response.get("done_reason") if isinstance(response, dict) else None
    return LLMResponse(
        content=parsed.content if parsed.raw is not None else text.strip(),
        tool_calls=normalize_tool_calls(native_calls, tools=tools) or parsed.tool_calls,
        raw=response if response is not None else {"provider": provider_name},
        usage=_usage_dict(response),
        finish_reason=str(finish_reason) if finish_reason else None,
    )


def _ollama_tool_calls(raw_calls: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    if raw_calls is None:
        return calls
    if not isinstance(raw_calls, list | tuple):
        raise ControlMessageError(
            "ollama.message.tool_calls must be a list",
            code="invalid_tool_call",
        )
    for index, item in enumerate(raw_calls):
        location = f"ollama.message.tool_calls[{index}]"
        if not isinstance(item, dict):
            raise ControlMessageError(f"{location} must be an object", code="invalid_tool_call")
        call_type = item.get("type")
        if call_type is not None and call_type != "function":
            raise ControlMessageError(
                f"{location}.type must be function",
                code="invalid_tool_call",
            )
        function = item.get("function")
        if not isinstance(function, dict):
            raise ControlMessageError(
                f"{location}.function must be an object",
                code="invalid_tool_call",
            )
        name = native_tool_name(function.get("name"), location=location)
        arguments = native_tool_arguments(
            function.get("arguments"),
            tool_name=name,
            location=location,
        )
        calls.append(
            ToolCall(
                name=name,
                arguments=arguments,
                id=native_tool_call_id(item.get("id"), location=f"{location}.id"),
            )
        )
    return calls


def _usage_dict(response: Any) -> dict[str, Any] | None:
    if not isinstance(response, dict):
        return None
    fields = {
        "input_tokens": response.get("prompt_eval_count"),
        "output_tokens": response.get("eval_count"),
    }
    prompt_tokens = fields["input_tokens"]
    output_tokens = fields["output_tokens"]
    if isinstance(prompt_tokens, int) and isinstance(output_tokens, int):
        fields["total_tokens"] = prompt_tokens + output_tokens
    return {key: value for key, value in fields.items() if value is not None} or None
