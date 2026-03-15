"""Resolve workspace config, provider, model, and API key for a chat request."""

from __future__ import annotations

from dataclasses import dataclass
import json
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
    channel_name: str
    request_metadata: dict[str, str]
    return_route: dict[str, str]
    selected_skill_packs: list[dict[str, Any]]
    skill_prompt_block: str
    selected_skill_mcp_servers: list[dict[str, Any]]


async def build_request_context(request, workspace_id: str) -> ChatRequestContext:
    """Load workspace config, resolve provider/model/API key, build message context."""
    pool = await get_pool()
    r = await get_redis()
    ws_config = await ProviderConfig(pool).get_config(workspace_id)
    params = dict(request.parameters) if hasattr(request, "parameters") else {}
    channel_name = params.get("channel", "") or "web"
    request_metadata = {
        str(key): str(value)
        for key, value in params.items()
        if value not in (None, "")
    }
    return_route = {}
    if params.get("return_route"):
        try:
            parsed = json.loads(params["return_route"])
            if isinstance(parsed, dict):
                return_route = {str(key): str(value) for key, value in parsed.items()}
        except (TypeError, ValueError, json.JSONDecodeError):
            return_route = {}
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

    selected_skill_packs: list[dict[str, Any]] = []
    skill_prompt_block = ""
    selected_skill_mcp_servers: list[dict[str, Any]] = []
    skill_pack_manager = getattr(runtime, "skill_pack_manager", None)
    if skill_pack_manager is not None and user_content:
        try:
            selection = await skill_pack_manager.select_packs(
                workspace_id,
                user_content,
                history=messages,
            )
            selected_skill_packs = list(selection.get("packs") or [])
            skill_prompt_block = str(selection.get("prompt_block") or "").strip()
            if skill_prompt_block and messages:
                for message in messages:
                    if not isinstance(message, dict):
                        continue
                    if message.get("role") in ("system", 2):
                        message["content"] += "\n\n" + skill_prompt_block
                        break
            if selected_skill_packs:
                selected_skill_mcp_servers = await skill_pack_manager.auto_connect_selected_mcp(
                    workspace_id,
                    selected_skill_packs,
                )
                connected = [item for item in selected_skill_mcp_servers if item.get("connected")]
                if connected and messages:
                    lines = [
                        "## Skill Pack MCP Servers",
                        "These MCP servers were auto-connected from the active skill packs. Use `mcp_call(server_name=..., tool_name=..., arguments=...)` to invoke them.",
                    ]
                    for item in connected:
                        tools = item.get("tools") or []
                        tool_names = ", ".join(
                            str(tool.get("name") or "")
                            for tool in tools
                            if isinstance(tool, dict) and str(tool.get("name") or "").strip()
                        )
                        suffix = f" tools: {tool_names}" if tool_names else ""
                        lines.append(
                            f"- `{item.get('server_name')}` from `{item.get('pack_id')}`.{suffix}"
                        )
                    mcp_block = "\n".join(lines)
                    for message in messages:
                        if not isinstance(message, dict):
                            continue
                        if message.get("role") in ("system", 2):
                            message["content"] += "\n\n" + mcp_block
                            break
        except Exception as exc:
            logger.warning("Failed to resolve skill packs for request: %s", exc)

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
        channel_name=channel_name,
        request_metadata=request_metadata,
        return_route=return_route,
        selected_skill_packs=selected_skill_packs,
        skill_prompt_block=skill_prompt_block,
        selected_skill_mcp_servers=selected_skill_mcp_servers,
    )
