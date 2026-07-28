from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from ..config import AgentConfig
from ..security_boundary import REDACTED, redact_text
from .provider_urls import normalize_ollama_openai_base_url, validate_provider_http_url

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
MAX_MODEL_CATALOG_BYTES = 2 * 1024 * 1024
MAX_MODEL_CATALOG_ENTRIES = 2048
MAX_MODEL_ID_CHARS = 512
_SENSITIVE_QUERY_VALUE = re.compile(
    r"([?&](?:api[_-]?key|key|token|access[_-]?token)=)[^&\s]+",
    flags=re.IGNORECASE,
)
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,511}\Z")


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
    declared_capabilities: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        payload = {
            "provider": self.provider,
            "models": list(self.models),
            "source": self.source,
            "declared_capabilities": {
                model: list(capabilities)
                for model, capabilities in sorted(self.declared_capabilities.items())
            },
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

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
            "catalog_digest": self.digest,
            "declared_capabilities": {
                model: list(capabilities)
                for model, capabilities in sorted(self.declared_capabilities.items())
            },
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
        return _catalog(provider, payload, fallback, "provider", None, api_key_env, secret_resolver=secret_resolver)
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
            payload,
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
        return _catalog(provider, payload, fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "deepseek":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, payload, fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "kimi":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, payload, fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "ollama":
        base_url = normalize_ollama_openai_base_url(_base_url_for_provider(config, provider))
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=None,
        )
        return _catalog(provider, payload, fallback, "provider", base_url, None, secret_resolver=secret_resolver)
    if provider == "ollama-cloud":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "tags"),
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, payload, fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
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
        return _catalog(provider, payload, fallback, "provider", None, api_key_env, secret_resolver=secret_resolver)
    if provider == "grok":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        base_url = _base_url_for_provider(config, provider) or DEFAULT_BASE_URLS[provider]
        payload = _fetch_json(
            _join_url(base_url, "models"),
            timeout_seconds=_catalog_timeout(config),
            api_key=api_key,
        )
        return _catalog(provider, payload, fallback, "provider", base_url, api_key_env, secret_resolver=secret_resolver)
    if provider == "gemini":
        api_key_env = _api_key_env_for_provider(config, provider)
        api_key = _required_api_key(api_key_env, secret_resolver=secret_resolver)
        payload = _fetch_json(
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
            timeout_seconds=_catalog_timeout(config),
            api_key=None,
        )
        return _catalog(provider, payload, fallback, "provider", None, api_key_env, secret_resolver=secret_resolver)
    raise ValueError(f"unsupported provider: {provider}")


def _catalog(
    provider: str,
    payload: Any,
    fallback: tuple[str, ...],
    source: str,
    base_url: str | None,
    api_key_env: str | None,
    *,
    secret_resolver: SecretResolver | None = None,
) -> ProviderModelCatalog:
    unique_models = _unique(_model_ids(payload))
    declared_capabilities = _model_capability_declarations(payload)
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
        declared_capabilities={
            model: declared_capabilities[model]
            for model in unique_models
            if model in declared_capabilities
        },
    )


def _fetch_json(
    url: str,
    *,
    timeout_seconds: float,
    api_key: str | None,
    headers: dict[str, str] | None = None,
    use_bearer: bool = True,
) -> Any:
    safe_url = validate_provider_http_url(url)
    request_headers = {"Accept": "application/json", **(headers or {})}
    if api_key and use_bearer:
        request_headers["Authorization"] = f"Bearer {api_key}"
    request = Request(safe_url, headers=request_headers)
    try:
        # The URL is restricted to HTTP(S) with a host immediately above.
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310
            raw_body = response.read(MAX_MODEL_CATALOG_BYTES + 1)
    except HTTPError as exc:
        detail = exc.read(241).decode("utf-8", errors="replace")
        raise RuntimeError(
            f"model list failed with HTTP {exc.code}: "
            f"{_safe_catalog_error(detail, secret=api_key)[:240]}"
        ) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(
            f"model list request failed: {_safe_catalog_error(str(reason), secret=api_key)}"
        ) from exc
    if len(raw_body) > MAX_MODEL_CATALOG_BYTES:
        raise ValueError("model catalog response exceeded the byte limit")
    body = raw_body.decode("utf-8")
    return json.loads(body)


def _model_ids(payload: Any) -> tuple[str, ...]:
    rows: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(value)
    ids: list[str] = []
    for row in rows[:MAX_MODEL_CATALOG_ENTRIES]:
        if isinstance(row, str):
            string_model_id = row.strip()
            if _MODEL_ID.fullmatch(string_model_id):
                ids.append(string_model_id)
            continue
        if not isinstance(row, dict):
            continue
        model_id = _model_id(row)
        if model_id is not None:
            ids.append(model_id)
    return tuple(ids)


def _model_capability_declarations(payload: Any) -> dict[str, tuple[str, ...]]:
    rows: list[Any] = []
    if isinstance(payload, dict):
        for key in ("data", "models"):
            value = payload.get(key)
            if isinstance(value, list):
                rows.extend(value)
    declarations: dict[str, tuple[str, ...]] = {}
    for row in rows[:MAX_MODEL_CATALOG_ENTRIES]:
        if not isinstance(row, dict):
            continue
        model_id = _model_id(row)
        if model_id is None:
            continue
        capabilities: set[str] = set()
        raw_capabilities = row.get("capabilities")
        if isinstance(raw_capabilities, list):
            for value in raw_capabilities:
                normalized = _normalized_capability(value)
                if normalized is not None:
                    capabilities.add(normalized)
        supported_parameters = row.get("supported_parameters")
        if isinstance(supported_parameters, list):
            for value in supported_parameters:
                normalized = _normalized_capability(value)
                if normalized is not None:
                    capabilities.add(normalized)
        architecture = row.get("architecture")
        modality_sources: list[Any] = [
            row.get("input_modalities"),
            row.get("modalities"),
        ]
        if isinstance(architecture, dict):
            modality_sources.extend(
                [
                    architecture.get("input_modalities"),
                    architecture.get("modalities"),
                ]
            )
        for raw_modalities in modality_sources:
            if isinstance(raw_modalities, list):
                values = {str(value).strip().lower() for value in raw_modalities}
                if values & {"image", "images", "vision"}:
                    capabilities.add("vision")
        if capabilities:
            declarations[model_id] = tuple(sorted(capabilities))
    return declarations


def _model_id(row: dict[Any, Any]) -> str | None:
    raw_id = row.get("id") or row.get("model") or row.get("name")
    if raw_id is None:
        return None
    model_id = str(raw_id).strip()
    if model_id.startswith("models/"):
        model_id = model_id.removeprefix("models/")
    if not _MODEL_ID.fullmatch(model_id):
        return None
    return model_id


def _normalized_capability(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("-", "_")
    aliases = {
        "generate": "generation",
        "generation": "generation",
        "text": "generation",
        "stream": "streaming",
        "streaming": "streaming",
        "json": "structured_output",
        "json_mode": "structured_output",
        "response_format": "structured_output",
        "structured_output": "structured_output",
        "structured_outputs": "structured_output",
        "tool_calling": "tools",
        "tool_use": "tools",
        "tools": "tools",
        "image": "vision",
        "images": "vision",
        "vision": "vision",
    }
    return aliases.get(normalized)


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
    safe_base_url = validate_provider_http_url(base_url)
    return validate_provider_http_url(urljoin(f"{safe_base_url.rstrip('/')}/", suffix))


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
    return _safe_catalog_error(str(exc) or type(exc).__name__, secret=None)


def _safe_catalog_error(value: str, *, secret: str | None) -> str:
    safe = value.replace(secret, REDACTED) if secret else value
    safe = _SENSITIVE_QUERY_VALUE.sub(r"\1<redacted>", safe)
    return redact_text(safe)[:512]
