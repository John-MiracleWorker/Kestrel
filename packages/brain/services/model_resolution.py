"""Create a ModelRouter with workspace-aware provider probing."""

from __future__ import annotations

import time
from typing import Any

from core.config import logger
from providers_registry import get_provider, resolve_provider


def build_model_router(
    provider_name: str,
    provider: Any,
    provider_settings: dict,
    model: str,
):
    """Create ModelRouter with workspace-aware provider checker and probe cache.

    Args:
        provider_name: Active provider identifier (e.g. "ollama", "openai").
        provider: Resolved provider instance.
        provider_settings: Workspace-level settings dict (may contain ollama_host, lmstudio_host).
        model: Active model identifier.

    Returns:
        Configured ModelRouter instance.
    """
    from agent.model_router import ModelRouter

    _probe_cache: dict[str, tuple[bool, float]] = {}
    _PROBE_TTL = 60  # seconds

    def custom_provider_checker(name: str) -> bool:
        # Check cache first
        cached = _probe_cache.get(name)
        if cached and (time.time() - cached[1]) < _PROBE_TTL:
            return cached[0]

        result = False

        if name in ("ollama", "local") and provider_settings.get("ollama_host"):
            try:
                import httpx
                _probe_url = provider_settings["ollama_host"].rstrip("/")
                resp = httpx.get(f"{_probe_url}/api/tags", timeout=3)
                result = resp.status_code == 200
                logger.info(f"Ollama probe {_probe_url}: {'OK' if result else resp.status_code}")
            except Exception as _e:
                logger.warning(f"Ollama probe failed for {provider_settings['ollama_host']}: {_e}")
                result = False
            _probe_cache[name] = (result, time.time())
            return result

        if name == "lmstudio" and provider_settings.get("lmstudio_host"):
            try:
                import httpx
                _probe_url = provider_settings["lmstudio_host"].rstrip("/")
                resp = httpx.get(f"{_probe_url}/v1/models", timeout=15)
                result = resp.status_code == 200
                logger.info(f"LM Studio probe {_probe_url}: {'OK' if result else resp.status_code}")
            except Exception as _e:
                logger.warning(f"LM Studio probe failed for {provider_settings['lmstudio_host']}: {_e}")
                result = False
            _probe_cache[name] = (result, time.time())
            return result

        if name == provider_name and getattr(provider, "is_ready", lambda: False)():
            return True
        try:
            return get_provider(name).is_ready()
        except Exception:
            return False

    return ModelRouter(
        provider_checker=custom_provider_checker,
        workspace_provider=provider_name,
        workspace_model=model,
    )


def workspace_resolve_provider(provider_settings: dict):
    """Return a provider resolver that injects workspace-specific URLs."""

    def _resolve(name: str):
        p = resolve_provider(name)
        if name == "ollama" and provider_settings.get("ollama_host"):
            _host = provider_settings["ollama_host"].rstrip("/")
            p.set_explicit_url(_host)
        if name == "lmstudio" and provider_settings.get("lmstudio_host"):
            _host = provider_settings["lmstudio_host"].rstrip("/")
            p.set_explicit_url(_host)
        return p

    return _resolve
