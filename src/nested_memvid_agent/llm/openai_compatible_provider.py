from __future__ import annotations

import os
from importlib import import_module
from typing import Any

from ..runtime_models import ChatMessage, LLMOptions, LLMResponse, ToolSpec
from .base import LLMProvider, ProviderError
from .parser import parse_agent_response


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
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key or os.getenv(api_key_env or "OPENAI_API_KEY") or "not-needed"
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.temperature = temperature

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        del tools
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
        try:
            response = client.chat.completions.create(
                model=self.model,
                messages=[_to_chat_completion_dict(message) for message in messages],
                temperature=active_options.temperature,
            )
        except Exception as exc:  # noqa: BLE001 - provider boundary maps SDK failures
            raise ProviderError(str(exc), code=type(exc).__name__, retryable=True) from exc
        text = _first_choice_text(response)
        return parse_agent_response(text)


def _to_chat_completion_dict(message: ChatMessage) -> dict[str, Any]:
    payload = message.to_openai_dict()
    if payload["role"] == "tool":
        payload["role"] = "user"
        payload["content"] = f"Tool result from {message.name or 'tool'}:\n{message.content}"
        payload.pop("tool_call_id", None)
        payload.pop("name", None)
    return payload


def _first_choice_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if content is not None:
            return str(content)
    return str(response)
