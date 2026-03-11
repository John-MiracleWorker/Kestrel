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


def _classify_ollama(model_id: str) -> str:
    """Classify an Ollama model by parameter count hint in tag name.
    
    Tags like 'qwen3:4b', 'llama3:8b'  -> fast
    Tags like 'qwen3:32b', 'mistral:70b' -> power
    Tags ending in ':cloud' are fast (cloud-relay, minimal local load).
    Unknown tags without size hints default to fast.
    """
    mid = model_id.lower()
    # Extract numeric size hint from the tag (e.g. '70b', '32b', '8b')
    m = re.search(r':(\d+)b', mid)
    if m:
        params_b = int(m.group(1))
        return "power" if params_b >= 20 else "fast"
    if ":cloud" in mid:
        return "fast"   # Cloud-relay models (e.g. glm-5:cloud) are treated as fast
    # No size info — assume fast to avoid over-escalating
    return "fast"


def _classify_lmstudio(model_id: str) -> str:
    """Classify an LM Studio model by parameter count or known model family.
    
    LM Studio model IDs are like:
      'lmstudio-community/Meta-Llama-3.1-8B-Instruct-GGUF'
      'bartowski/Qwen2.5-Coder-32B-Instruct-GGUF'
      'zai-org/glm-4.7-flash'
      'deepseek-coder-v2-lite-instruct'
    """
    mid = model_id.lower()

    # Skip embedding models — never select these for chat
    if "embed" in mid or "embedding" in mid:
        return "other"

    # Explicit parameter count (e.g. "32B", "8B")
    m = re.search(r'(\d+)b', mid)
    if m:
        params_b = int(m.group(1))
        return "power" if params_b >= 20 else "fast"

    # Known large model families without explicit param tag
    # GLM 4.x series: ~9B params, strong multilingual model
    if "glm" in mid:
        return "power"
    # DeepSeek Coder v2: capable coding model
    if "deepseek" in mid and "lite" not in mid:
        return "power"

    return "fast"


_CLASSIFIERS = {
    "google": _classify_google,
    "openai": _classify_openai,
    "anthropic": _classify_anthropic,
    "ollama": _classify_ollama,
    "lmstudio": _classify_lmstudio,
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

    async def get_ollama_models(self) -> list[dict]:
        """Return the list of models actually installed in the local Ollama instance."""
        await self._ensure("ollama")
        return self._cache.get("ollama", [])

    async def get_ollama_fast_model(self) -> str:
        """Best small/fast Ollama model currently installed."""
        await self._ensure("ollama")
        return self._pick("ollama", "fast")

    async def get_ollama_power_model(self) -> str:
        """Best large/power Ollama model currently installed."""
        await self._ensure("ollama")
        m = self._pick("ollama", "power")
        # If no large model exists, fall back to the best fast model
        return m or self._pick("ollama", "fast")

    async def is_ollama_model_available(self, model_id: str) -> bool:
        """Check whether a specific model tag is installed in Ollama."""
        models = await self.get_ollama_models()
        return any(m["id"] == model_id for m in models)

    # ── LM Studio helpers ─────────────────────────────────────────────

    async def get_lmstudio_models(self) -> list[dict]:
        """Return the list of models loaded in the discovered LM Studio instance."""
        await self._ensure("lmstudio")
        return self._cache.get("lmstudio", [])

    async def get_lmstudio_fast_model(self) -> str:
        """Best small/fast LM Studio model currently loaded."""
        await self._ensure("lmstudio")
        return self._pick("lmstudio", "fast")

    async def get_lmstudio_power_model(self) -> str:
        """Best large/power LM Studio model currently loaded."""
        await self._ensure("lmstudio")
        m = self._pick("lmstudio", "power")
        return m or self._pick("lmstudio", "fast")

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
            if provider == "ollama":
                await self._fetch_ollama()
            elif provider == "lmstudio":
                await self._fetch_lmstudio()
            else:
                await self._fetch(provider, api_key)

    async def _fetch_ollama(self):
        """Fetch installed models from the best available Ollama instance."""
        import aiohttp
        # Prefer the best-discovered host; fall back to OLLAMA_HOST env var
        try:
            from providers.ollama_discovery import ollama_discovery
            host = await ollama_discovery.get_best_host()
        except Exception:
            host = os.getenv("OLLAMA_HOST", "http://host.docker.internal:11434")
        url = f"{host}/api/tags"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"Ollama /api/tags returned {resp.status}")
                    data = await resp.json()
            models = [
                {"id": m["name"], "name": m["name"]}
                for m in data.get("models", [])
            ]
            # Sort: true local models (with parameter size) first, cloud-relay tags last.
            # Within each group, sort by version/parameter size descending.
            def _ollama_sort_key(m: dict) -> tuple:
                mid = m["id"].lower()
                is_cloud_relay = ":cloud" in mid
                # Extract the numeric parameter size (higher = more capable)
                param_match = re.search(r':(\d+)b', mid)
                params = -int(param_match.group(1)) if param_match else 0  # negative for desc sort
                return (int(is_cloud_relay), params)
            models.sort(key=_ollama_sort_key)
            self._cache["ollama"] = models
            self._fetched_at["ollama"] = time.time()
            classify = _classify_ollama
            fast_names  = [m["id"] for m in models if classify(m["id"]) == "fast"]
            power_names = [m["id"] for m in models if classify(m["id"]) == "power"]
            logger.info(
                f"Model registry: discovered {len(models)} Ollama models "
                f"(fast={fast_names}, power={power_names})"
            )
        except Exception as e:
            logger.warning(f"Model registry: Ollama discovery failed: {e}")
            if "ollama" not in self._cache:
                self._cache["ollama"] = []

    async def _fetch_lmstudio(self):
        """Fetch loaded models from the best available LM Studio instance."""
        import aiohttp
        # Prefer explicit env vars (docker-compose sets LMSTUDIO_BASE_URL)
        explicit_host = os.getenv("LMSTUDIO_BASE_URL", "") or os.getenv("LMSTUDIO_HOST", "")
        if explicit_host:
            host = explicit_host.rstrip("/")
        else:
            try:
                from providers.lmstudio_discovery import lmstudio_discovery
                host = await lmstudio_discovery.get_best_host()
            except Exception:
                host = f"http://host.docker.internal:{os.getenv('LMSTUDIO_PORT', '1234')}"
        url = f"{host}/v1/models"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"LM Studio /v1/models returned {resp.status}")
                    data = await resp.json()
            models = [
                {"id": m["id"], "name": m["id"]}
                for m in data.get("data", [])
                if "embed" not in m.get("id", "").lower()
            ]
            models.sort(key=lambda m: _version_key(m["id"]))
            self._cache["lmstudio"] = models
            self._fetched_at["lmstudio"] = time.time()
            classify = _classify_lmstudio
            fast_names  = [m["id"] for m in models if classify(m["id"]) == "fast"]
            power_names = [m["id"] for m in models if classify(m["id"]) == "power"]
            logger.info(
                f"Model registry: discovered {len(models)} LM Studio models "
                f"(fast={fast_names}, power={power_names})"
            )
        except Exception as e:
            logger.warning(f"Model registry: LM Studio discovery failed: {e}")
            if "lmstudio" not in self._cache:
                self._cache["lmstudio"] = []

    async def _fetch(self, provider: str, api_key: str = ""):
        """Fetch models from a cloud provider API."""
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
