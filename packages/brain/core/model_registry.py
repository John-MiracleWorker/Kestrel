"""
Model Registry — centralized, API-driven model discovery.

On startup, queries each provider's API (Google, OpenAI, Anthropic) for the
real list of available models.  Caches the result for `_CACHE_TTL` seconds
so the rest of the codebase never hardcodes a model name.

Usage (from anywhere):
    from core.model_registry import model_registry
    fast  = await model_registry.get_fast_model("google")   # → "gemini-3-flash-preview"
    power = await model_registry.get_power_model("google")  # → "gemini-3.1-pro-preview"
    all_g = await model_registry.list_models("google")      # → [{"id": ..., ...}, ...]
"""

from __future__ import annotations
import asyncio
import logging
import os
import re
import time
from typing import Optional

logger = logging.getLogger("brain.core.model_registry")

_CACHE_TTL = int(os.getenv("MODEL_REGISTRY_TTL", "3600"))  # re-fetch every hour


# ── Tier classification heuristics (provider-specific) ───────────────

def _classify_google(model_id: str) -> str:
    """Classify a Google model into 'power', 'fast', or 'other'.

    Excludes specialty models (image generation, live streaming,
    deep-research, custom-tools, tuning, embedding) so they never
    get auto-selected as the default fast/power model.
    """
    mid = model_id.lower()

    # Specialty models should never be auto-selected as defaults
    _SPECIALTY_MARKERS = (
        "image", "live", "deep-research", "customtools",
        "tuning", "embedding", "vision",
    )
    if any(marker in mid for marker in _SPECIALTY_MARKERS):
        return "other"

    # Deep-think / pro variants → power
    if "pro" in mid or "deep-think" in mid or "ultra" in mid:
        return "power"
    # Flash variants → fast
    if "flash" in mid:
        return "fast"
    return "other"


def _classify_openai(model_id: str) -> str:
    mid = model_id.lower()
    if "codex" in mid or re.search(r"gpt-5\.\d+", mid):
        return "power"
    if "mini" in mid or "nano" in mid:
        return "fast"
    if re.match(r"^gpt-\d", mid) or "o3" in mid:
        return "power"
    return "other"


def _classify_anthropic(model_id: str) -> str:
    mid = model_id.lower()
    if "opus" in mid:
        return "power"
    if "haiku" in mid:
        return "fast"
    if "sonnet" in mid:
        return "power"  # sonnet is mid-tier but closest to power
    return "other"


_CLASSIFIERS = {
    "google": _classify_google,
    "openai": _classify_openai,
    "anthropic": _classify_anthropic,
}


# ── Version sorting helper ──────────────────────────────────────────

def _version_key(model_id: str) -> tuple:
    """
    Sort key that puts higher version numbers first so the best model
    in each tier is models[0].  E.g. gemini-3.1-pro > gemini-3-pro > gemini-2.5.
    """
    nums = re.findall(r"\d+\.?\d*", model_id)
    return tuple(-float(n) for n in nums) if nums else (0,)


# ── Registry ─────────────────────────────────────────────────────────

class ModelRegistry:
    """
    Singleton registry that discovers models from provider APIs on demand.

    First call triggers an API fetch; subsequent calls use cache until TTL
    expires, then quietly re-fetches in the background.
    """

    def __init__(self):
        # provider → list of model dicts
        self._cache: dict[str, list[dict]] = {}
        # provider → timestamp of last fetch
        self._fetched_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    # ── Public helpers ────────────────────────────────────────────────

    async def list_models(self, provider: str, api_key: str = "") -> list[dict]:
        """Sorted list of available models for *provider*."""
        await self._ensure(provider, api_key)
        return self._cache.get(provider, [])

    async def get_power_model(self, provider: str, api_key: str = "") -> str:
        """Best flagship / power model for *provider*."""
        await self._ensure(provider, api_key)
        return self._pick(provider, "power")

    async def get_fast_model(self, provider: str, api_key: str = "") -> str:
        """Best efficient / fast model for *provider*."""
        await self._ensure(provider, api_key)
        return self._pick(provider, "fast")

    async def get_default_model(self, provider: str, api_key: str = "") -> str:
        """Default model — env var if set, otherwise best fast model."""
        env_map = {
            "google": "GOOGLE_DEFAULT_MODEL",
            "openai": "OPENAI_DEFAULT_MODEL",
            "anthropic": "ANTHROPIC_DEFAULT_MODEL",
        }
        env_val = os.getenv(env_map.get(provider, ""), "")
        if env_val:
            return env_val
        return await self.get_fast_model(provider, api_key)

    async def build_failover_chain(self, provider: str, primary_model: str, api_key: str = "") -> list[str]:
        """
        Build a failover chain from discovered models.
        Returns [primary, next_in_same_tier, best_in_other_tier].
        """
        await self._ensure(provider, api_key)
        classify = _CLASSIFIERS.get(provider, lambda _: "other")
        primary_tier = classify(primary_model)
        models = self._cache.get(provider, [])

        chain = [primary_model]
        # Add other models in same tier
        for m in models:
            mid = m["id"]
            if mid != primary_model and classify(mid) == primary_tier:
                chain.append(mid)
                break
        # Add best model from other tier
        other_tier = "fast" if primary_tier == "power" else "power"
        for m in models:
            mid = m["id"]
            if classify(mid) == other_tier:
                chain.append(mid)
                break
        return chain

    def invalidate(self, provider: str = ""):
        """Force re-fetch on next access."""
        if provider:
            self._fetched_at.pop(provider, None)
        else:
            self._fetched_at.clear()

    # ── Private ───────────────────────────────────────────────────────

    def _pick(self, provider: str, tier: str) -> str:
        """Pick the best model in *tier* for *provider*."""
        classify = _CLASSIFIERS.get(provider, lambda _: "other")
        for m in self._cache.get(provider, []):
            if classify(m["id"]) == tier:
                return m["id"]
        # Fallback: any model
        models = self._cache.get(provider, [])
        return models[0]["id"] if models else ""

    async def _ensure(self, provider: str, api_key: str = ""):
        """Populate cache if stale."""
        now = time.time()
        if provider in self._cache and (now - self._fetched_at.get(provider, 0)) < _CACHE_TTL:
            return
        async with self._lock:
            # Double-check after acquiring lock
            if provider in self._cache and (now - self._fetched_at.get(provider, 0)) < _CACHE_TTL:
                return
            await self._fetch(provider, api_key)

    async def _fetch(self, provider: str, api_key: str = ""):
        """Fetch models from the provider API."""
        try:
            from providers.cloud import CloudProvider
            cp = CloudProvider(provider)
            raw = await cp.list_models(api_key=api_key)
            if raw:
                # Sort: highest version first within each tier
                raw.sort(key=lambda m: _version_key(m["id"]))
                self._cache[provider] = raw
                self._fetched_at[provider] = time.time()
                classify = _CLASSIFIERS.get(provider, lambda _: "other")
                power = [m["id"] for m in raw if classify(m["id"]) == "power"]
                fast  = [m["id"] for m in raw if classify(m["id"]) == "fast"]
                logger.info(
                    f"Model registry: discovered {len(raw)} {provider} models "
                    f"(power={power[:3]}, fast={fast[:3]})"
                )
                return
        except Exception as e:
            logger.warning(f"Model registry: API fetch failed for {provider}: {e}")

        # Fallback: use env vars or previous cache
        if provider not in self._cache:
            self._cache[provider] = []
            logger.warning(f"Model registry: no models discovered for {provider}")



# Singleton
model_registry = ModelRegistry()
