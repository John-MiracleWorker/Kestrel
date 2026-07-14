from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from ..config import AgentConfig

PROVIDER_OPTIONS: tuple[str, ...] = (
    "mock",
    "lm-studio",
    "ollama",
    "openai",
    "openai-compatible",
    "ollama-cloud",
    "openrouter",
    "deepseek",
    "kimi",
    "anthropic",
    "grok",
    "gemini",
    "codex-cli",
)

STATIC_MODEL_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    "mock": ("mock",),
    "lm-studio": ("local-model",),
    "ollama": ("llama3.1", "qwen2.5-coder", "mistral"),
    "openai": ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini"),
    "openai-compatible": ("local-model",),
    "ollama-cloud": ("gpt-oss:120b", "gpt-oss:20b"),
    "openrouter": ("openai/gpt-5.5", "anthropic/claude-sonnet-4.5"),
    "deepseek": ("deepseek-v4-pro", "deepseek-v4-flash"),
    "kimi": ("kimi-k2.6", "kimi-k2.5"),
    "anthropic": ("claude-sonnet-4.5", "claude-opus-4.1"),
    "grok": ("grok-4.3", "grok-build-0.1", "grok-4.20"),
    "gemini": ("gemini-2.5-pro", "gemini-2.5-flash"),
    "codex-cli": ("gpt-5.5", "gpt-5.4"),
}

DEFAULT_API_KEY_ENVS: dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "kimi": "MOONSHOT_API_KEY",
    "ollama-cloud": "OLLAMA_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "grok": "XAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
}

DEFAULT_BASE_URLS: dict[str, str] = {
    "lm-studio": "http://localhost:1234/v1",
    "ollama": "http://localhost:11434/v1",
    "ollama-cloud": "https://ollama.com/api",
    "openrouter": "https://openrouter.ai/api/v1",
    "deepseek": "https://api.deepseek.com",
    "kimi": "https://api.moonshot.ai/v1",
    "grok": "https://api.x.ai/v1",
}

SecretResolver = Callable[[str | None], str | None]


@dataclass(frozen=True)
class ProviderModelCatalog:
    provider: str
    models: tuple[str, ...]
    fallback_models: tuple[str, ...]
    source: str
    ok: bool
    fetchable: bool
    error: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    api_key_configured: bool = False
    fetched_at: str | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "models": list(self.models),
            "fallback_models": list(self.fallback_models),
            "source": self.source,
            "ok": self.ok,
            "fetchable": self.fetchable,
            "error": self.error,
            "base_url_configured": bool(self.base_url),
            "api_key_env": self.api_key_env,
            "api_key_configured": self.api_key_configured,
            "fetched_at": self.fetched_at,
        }


def default_api_key_env(provider: str, configured: str | None = None) -> str | None:
    return configured or DEFAULT_API_KEY_ENVS.get(provider)


def model_catalog_for_provider(
    config: AgentConfig,
    provider: str,
    *,
    secret_resolver: SecretResolver | None = None,
) -> ProviderModelCatalog:
    provider_name = provider.strip()
    fallback = STATIC_MODEL_SUGGESTIONS.get(provider_name, ())
    if provider_name not in PROVIDER_OPTIONS:
        return ProviderModelCatalog(
            provider=provider_name,
            models=fallback,
            fallback_models=fallback,
            source="fallback",
            ok=False,
            fetchable=False,
            error=f"unsupported provider: {provider_name}",
        )
    if provider_name in {"mock", "codex-cli"}:
        return ProviderModelCatalog(
            provider=provider_name,
            models=fallback,
            fallback_models=fallback,
            source="static",
            ok=True,
            fetchable=False,
        )

    try:
        return _fetch_provider_models(config, provider_name, fallback, secret_resolver=secret_resolver)
    except Exception as exc:  # noqa: BLE001 - model discovery should not break the runtime picker
        return ProviderModelCatalog(
            provider=provider_name,
            models=fallback,
            fallback_models=fallback,
            source="fallback",
            ok=False,
            fetchable=True,
            error=_error_message(exc),
            base_url=_base_url_for_provider(config, provider_name),
            api_key_env=_api_key_env_for_provider(config, provider_name),
            api_key_configured=_api_key_configured(config, provider_name, secret_resolver=secret_resolver),
        )


def all_model_catalogs(config: AgentConfig, *, secret_resolver: SecretResolver | None = None) -> list[ProviderModelCatalog]:
    catalogs: list[ProviderModelCatalog] = []
    for provider in PROVIDER_OPTIONS:
        fallback = STATIC_MODEL_SUGGESTIONS.get(provider, ())
        catalogs.append(
            ProviderModelCatalog(
                provider=provider,
                models=fallback,
                fallback_models=fallback,
                source="static",
                ok=True,
                fetchable=provider not in {"mock", "codex-cli"},
                base_url=_base_url_for_provider(config, provider),
                api_key_env=_api_key_env_for_provider(config, provider),
                api_key_configured=_api_key_configured(config, provider, secret_resolver=secret_resolver),
            )
        )
    return catalogs


def _fetch_provider_models(
    config: AgentConfig,
    provider: str,
    fallback: tuple[str, ...],
    *,
    secret_resolver: SecretResolver | None = None,
) -> ProviderModelCatalog:
    if provider == "openai":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        payload = _fetch_json(
            "https://api.openai.com/v1/models",
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", None, api_key_env, secret_resolver=secret_resolver)
    if provider in {"lm-studio", "openai-compatible"}:
        base_url = _base_url_for_provider(config, provider)
        if not base_url:
            raise ValueError("openai-compatible provider requires NEST_AGENT_BASE_URL or --base-url")
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=_optional_api_key(_api_key_env_for_provider(config, provider), secret_resolver=secret_resolver),
        )
        return _catalog(
            provider,
            _model_ids(payload),
            fallback,
            "provider",
            base_url,
            _api_key_env_for_provider(config, provider),
            secret_resolver=secret_resolver,
        )
    if provider == "openrouter":
        api_key_env = _api_key_env_for_provider(config, provider)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=_optional_api_key(api_key_env, secret_resolver=secret_resolver),
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "deepseek":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "kimi":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "ollama":
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=None,
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", base_url, None, secret_resolver=secret_resolver)
    if provider == "ollama-cloud":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "tags"),
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "anthropic":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        payload = _fetch_json(
            "https://api.anthropic.com/v1/models",
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
            headers={"anthropic-version": "2023-06-01", "x-api-key": api_key},
            use_bearer=False,
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", None, api_key_env, secret_resolver=secret_resolver)
    if provider == "grok":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "gemini":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        payload = _fetch_json(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
            timeout_seconds=_catalog_timeout(config),
            api_key=None,
        )
        return _catalog(provider, _model_ids(payload), fallback, "provider", None, api_key_env, secret_resolver=secret_resolver)
    raise ValueError(f"unsupported provider: {provider}")


def _catalog(
    provider: str,
    models: tuple[str, ...],
    fallback: tuple[str, ...],
    source: str,
    base_url: str | None,
    api_key_env: str | None,
    *,
    secret_resolver: SecretResolver | None = None,
) -> ProviderModelCatalog:
    unique_models = _unique(models)
    if not unique_models:
        return ProviderModelCatalog(
            provider=provider,
            models=fallback,
            fallback_models=fallback,
            source="fallback",
            ok=False,
            fetchable=True,
            error="provider returned no models",
            base_url=base_url,
            api_key_env=api_key_env,
            api_key_configured=_api_key_configured_for_env(api_key_env, secret_resolver=secret_resolver),
        )
    return ProviderModelCatalog(
        provider=provider,
        models=unique_models,
        fallback_models=fallback,
        source=source,
        ok=True,
        fetchable=True,
        base_url=base_url,
        api_key_env=api_key_env,
        api_key_configured=_api_key_configured_for_env(api_key_env, secret_resolver=secret_resolver),
        fetched_at=datetime.now(UTC).isoformat(),
    )


def _fetch_json(
    url: str,
    *,
    timeout_seconds: float,
    api_key: str | None,
    headers: dict[str, str] | None = None,
    use_bearer: bool = True,
) -> Any:
    request_headers = {"Accept": "application/json", **(headers or {})}
    if api_key and use_bearer:
        request_headers["Authorization"] = f"Bearer {api_key}"
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - URLs are fixed provider endpoints or user-configured provider bases
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"model list failed with HTTP {exc.code}: {detail[:240]}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(f"model list request failed: {reason}") from exc
    return json.loads(body)


def _model_ids(payload: Any) -> tuple[str, ...]:
    rows: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(value)
    ids: list[str] = []
    for row in rows:
        if isinstance(row, str):
            ids.append(row)
            continue
        if not isinstance(row, dict):
            continue
        raw_id = row.get("id") or row.get("model") or row.get("name")
        if raw_id is None:
            continue
        model_id = str(raw_id).strip()
        if model_id.startswith("models/"):
            model_id = model_id.removeprefix("models/")
        if model_id:
            ids.append(model_id)
    return tuple(ids)


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return tuple(unique)


def _join_url(base_url: str, suffix: str) -> str:
    return urljoin(f"{base_url.rstrip('/')}/", suffix)


def _catalog_timeout(config: AgentConfig) -> float:
    return float(max(1, min(config.timeout_seconds, 10)))


def _required_api_key(api_key_env: str | None, *, secret_resolver: SecretResolver | None = None) -> str:
    if not api_key_env:
        raise ValueError("provider API key name is not configured")
    api_key = _resolve_api_key(api_key_env, secret_resolver=secret_resolver)
    if not api_key:
        raise ValueError(f"missing provider key for {api_key_env}; store it in Settings or set {api_key_env} in the environment")
    return api_key


def _optional_api_key(api_key_env: str | None, *, secret_resolver: SecretResolver | None = None) -> str | None:
    return _resolve_api_key(api_key_env, secret_resolver=secret_resolver) if api_key_env else None


def _api_key_configured(
    config: AgentConfig,
    provider: str,
    *,
    secret_resolver: SecretResolver | None = None,
) -> bool:
    api_key_env = _api_key_env_for_provider(config, provider)
    return _api_key_configured_for_env(api_key_env, secret_resolver=secret_resolver)


def _api_key_configured_for_env(api_key_env: str | None, *, secret_resolver: SecretResolver | None = None) -> bool:
    return bool(_resolve_api_key(api_key_env, secret_resolver=secret_resolver))


def _resolve_api_key(api_key_env: str | None, *, secret_resolver: SecretResolver | None = None) -> str | None:
    if not api_key_env:
        return None
    if secret_resolver is not None:
        resolved = secret_resolver(api_key_env)
        if resolved:
            return resolved
    return os.getenv(api_key_env)


def _base_url_for_provider(config: AgentConfig, provider: str) -> str | None:
    if config.base_url and config.provider == provider:
        return config.base_url
    return DEFAULT_BASE_URLS.get(provider)


def _api_key_env_for_provider(config: AgentConfig, provider: str) -> str | None:
    configured_env = config.api_key_env if config.provider == provider else None
    return default_api_key_env(provider, configured_env)


def _error_message(exc: Exception) -> str:
    return str(exc) or type(exc).__name__
