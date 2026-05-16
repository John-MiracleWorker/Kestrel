from __future__ import annotations

import json
import os
from importlib import import_module
from typing import Any

from ..runtime_models import ChatMessage, LLMOptions, LLMResponse, ToolCall, ToolSpec
from .base import LLMProvider, ProviderError
from .parser import parse_agent_response


class OpenAIResponsesProvider(LLMProvider):
    """Minimal OpenAI Responses API provider.

    This provider intentionally keeps tool calls in the agent-control JSON envelope so the
    runtime remains provider-portable. Codex can later upgrade this to native function
    calling, but this version is already chat-capable when `openai` is installed.
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
        input_payload: Any = [msg.to_openai_dict() for msg in messages]
        request: dict[str, Any] = {
            "model": self.model,
            "input": input_payload,
            "temperature": active_options.temperature,
        }
        if tools:
            request["tools"] = [_to_responses_tool(tool) for tool in tools]
        try:
            response = client.responses.create(**request)
        except Exception as exc:  # noqa: BLE001 - provider boundary maps SDK failures
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc
        return _responses_to_llm_response(response)


def _to_responses_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _responses_to_llm_response(response: Any) -> LLMResponse:
    text = _response_text(response)
    parsed = parse_agent_response(text)
    native_calls = tuple(_response_tool_calls(response))
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
    if not isinstance(output, list):
        return calls
    for item in output:
        item_type = _item_value(item, "type")
        if item_type not in {"function_call", "tool_call"}:
            continue
        name = _item_value(item, "name")
        if not isinstance(name, str) or not name:
            continue
        call_id = _item_value(item, "call_id") or _item_value(item, "id")
        calls.append(
            ToolCall(
                name=name,
                arguments=_arguments_dict(_item_value(item, "arguments")),
                id=str(call_id) if call_id else f"tool_{name}",
            )
        )
    return calls


def _arguments_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}
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
