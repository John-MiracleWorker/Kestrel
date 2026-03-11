"""Resolve workspace config, provider, model, and API key for a chat request."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.config import logger
from core import runtime
from db import get_pool, get_redis
from provider_config import ProviderConfig
from providers_registry import get_provider


@dataclass
class ChatRequestContext:
    pool: Any
    redis: Any
    ws_config: dict
    provider_name: str
    model: str
    api_key: str
    provider: Any
    provider_settings: dict
    messages: list[dict]
    user_content: str


async def build_request_context(request, workspace_id: str) -> ChatRequestContext:
    """Load workspace config, resolve provider/model/API key, build message context."""
    pool = await get_pool()
    r = await get_redis()
    ws_config = await ProviderConfig(pool).get_config(workspace_id)
    provider_name = request.provider or ws_config["provider"]
    model = request.model or ws_config["model"]

    # Resolve API Key from Redis if it's a reference
    api_key = ws_config.get("api_key", "")
    if api_key and api_key.startswith("provider_key:"):
        try:
            real_key = await r.get(api_key)
            api_key = real_key.decode("utf-8") if real_key else ""
        except Exception:
            api_key = ""

    provider = get_provider(provider_name)

    # If the workspace selected a specific Ollama server, override the
    # provider's base URL so it talks to that host instead of the
    # default (localhost / host.docker.internal).
    provider_settings = ws_config.get("settings") or {}
    if provider_name in ("ollama", "local") and provider_settings.get("ollama_host"):
        ollama_host_url = provider_settings["ollama_host"].rstrip("/")
        logger.info(f"Using workspace Ollama host: {ollama_host_url}")
        provider.set_explicit_url(ollama_host_url)
        # Invalidate stale health cache so is_ready() re-checks the new URL
        from providers.ollama import _health_cache
        _health_cache["checked_at"] = 0

    if provider_name == "lmstudio" and provider_settings.get("lmstudio_host"):
        lmstudio_host_url = provider_settings["lmstudio_host"].rstrip("/")
        logger.info(f"Using workspace LM Studio host: {lmstudio_host_url}")
        provider.set_explicit_url(lmstudio_host_url)
        from providers.lmstudio import _health_cache as _lm_health_cache
        _lm_health_cache["checked_at"] = 0

    from services.context_builder import build_chat_context
    messages = await build_chat_context(
        request, workspace_id, pool, r, runtime, provider_name, model, ws_config, api_key
    )

    user_content = next(
        (m["content"] for m in reversed(messages) if m["role"] == "user"),
        "",
    )

    return ChatRequestContext(
        pool=pool,
        redis=r,
        ws_config=ws_config,
        provider_name=provider_name,
        model=model,
        api_key=api_key,
        provider=provider,
        provider_settings=provider_settings,
        messages=messages,
        user_content=user_content,
    )
