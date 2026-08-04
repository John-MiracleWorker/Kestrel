from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime

from ..config import AgentConfig
from ..lan_runtime_authority import (
    LanRuntimeAuthority,
    LanRuntimeAuthorityResolver,
    authenticate_lan_runtime_authority,
)
from .anthropic_provider import AnthropicMessagesProvider
from .base import FallbackLLMProvider, LLMProvider
from .codex_cli_provider import CodexCLIProvider
from .gemini_provider import GeminiProvider
from .lan_openai_compatible_provider import LanOpenAICompatibleProvider
from .lan_runtime_transport import DirectLanRuntimeTransport
from .mock import MockLLMProvider
from .ollama_provider import OllamaNativeProvider
from .openai_compatible_provider import OpenAICompatibleProvider
from .openai_provider import OpenAIResponsesProvider
from .provider_urls import normalize_ollama_openai_base_url
from .resilience import ResilientLLMProvider, global_provider_health_registry

SecretResolver = Callable[[str | None], str | None]


def build_llm_provider(
    config: AgentConfig,
    *,
    secret_resolver: SecretResolver | None = None,
    lan_runtime_authority_resolver: LanRuntimeAuthorityResolver | None = None,
    lan_runtime_utc_clock: Callable[[], datetime] | None = None,
) -> LLMProvider:
    _validate_lan_factory_preflight(
        config,
        authority_resolver=lan_runtime_authority_resolver,
        utc_clock=lan_runtime_utc_clock,
    )
    provider = _build_resilient_provider(
        config,
        provider=config.provider,
        model=config.model,
        base_url=config.base_url,
        api_key_env=config.api_key_env,
        secret_resolver=secret_resolver,
        lan_runtime_authority=config.lan_runtime_authority,
        lan_runtime_authority_resolver=lan_runtime_authority_resolver,
        lan_runtime_utc_clock=lan_runtime_utc_clock,
    )
    if config.fallback_provider:
        fallback = _build_resilient_provider(
            config,
            provider=config.fallback_provider,
            model=config.fallback_model or config.model,
            base_url=config.fallback_base_url,
            api_key_env=config.fallback_api_key_env,
            secret_resolver=secret_resolver,
            lan_runtime_authority=None,
            lan_runtime_authority_resolver=None,
            lan_runtime_utc_clock=None,
        )
        _validate_fallback_compatibility(provider, fallback)
        return FallbackLLMProvider(provider, fallback)
    return provider


def _build_resilient_provider(
    config: AgentConfig,
    *,
    provider: str,
    model: str,
    base_url: str | None,
    api_key_env: str | None,
    secret_resolver: SecretResolver | None,
    lan_runtime_authority: LanRuntimeAuthority | None,
    lan_runtime_authority_resolver: LanRuntimeAuthorityResolver | None,
    lan_runtime_utc_clock: Callable[[], datetime] | None,
) -> LLMProvider:
    inner = _build_single_provider(
        config,
        provider=provider,
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
        secret_resolver=secret_resolver,
        lan_runtime_authority=lan_runtime_authority,
        lan_runtime_authority_resolver=lan_runtime_authority_resolver,
        lan_runtime_utc_clock=lan_runtime_utc_clock,
    )
    return ResilientLLMProvider(
        inner,
        provider_id=_provider_identity(
            provider,
            model,
            base_url,
            api_key_env,
            lan_runtime_authority=lan_runtime_authority,
        ),
        registry=global_provider_health_registry,
        failure_threshold=config.provider_circuit_failure_threshold,
        cooldown_seconds=config.provider_circuit_cooldown_seconds,
    )


def provider_health_id(config: AgentConfig) -> str:
    return _provider_identity(
        str(getattr(config, "provider", "unknown")),
        str(getattr(config, "model", "unknown")),
        getattr(config, "base_url", None),
        getattr(config, "api_key_env", None),
        lan_runtime_authority=getattr(config, "lan_runtime_authority", None),
    )


def _provider_identity(
    provider: str,
    model: str,
    base_url: str | None,
    api_key_env: str | None,
    *,
    lan_runtime_authority: LanRuntimeAuthority | None = None,
) -> str:
    if provider == "lan-openai-compatible":
        authority = authenticate_lan_runtime_authority(lan_runtime_authority)
        payload = {
            "schema": "kestrel.lan.provider-health.v1",
            "provider_profile_id": authority.provider_profile_id,
            "model_id": authority.model_id,
            "base_url": base_url,
            "material_binding_digest": authority.reviewed_material_binding_digest,
            "review_digest": authority.review_digest,
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return f"lan-openai-compatible:{hashlib.sha256(encoded).hexdigest()}"
    endpoint_identity = f"{base_url or '<default>'}\0{api_key_env or '<provider-default>'}"
    digest = hashlib.sha256(endpoint_identity.encode("utf-8")).hexdigest()[:12]  # codeql[py/weak-sensitive-data-hashing] — non-crypto identity digest
    return f"{provider}:{model}:{digest}"


def _validate_fallback_compatibility(primary: LLMProvider, fallback: LLMProvider) -> None:
    required_capabilities = (
        "supports_native_tools",
        "supports_json_mode",
        "supports_system_messages",
    )
    incompatible = [
        capability
        for capability in required_capabilities
        if getattr(primary.capabilities, capability) and not getattr(fallback.capabilities, capability)
    ]
    if incompatible:
        raise ValueError(
            "Fallback provider is missing required capabilities: " + ", ".join(sorted(incompatible))
        )


def _build_single_provider(
    config: AgentConfig,
    *,
    provider: str,
    model: str,
    base_url: str | None,
    api_key_env: str | None,
    secret_resolver: SecretResolver | None = None,
    lan_runtime_authority: LanRuntimeAuthority | None = None,
    lan_runtime_authority_resolver: LanRuntimeAuthorityResolver | None = None,
    lan_runtime_utc_clock: Callable[[], datetime] | None = None,
) -> LLMProvider:
    if provider == "lan-openai-compatible":
        authority = authenticate_lan_runtime_authority(lan_runtime_authority)
        if lan_runtime_authority_resolver is None:
            raise ValueError("LAN runtime authority resolver is required")
        utc_clock = lan_runtime_utc_clock or _utc_now
        transport = DirectLanRuntimeTransport(
            authority_resolver=lan_runtime_authority_resolver,
            utc_clock=utc_clock,
        )
        return LanOpenAICompatibleProvider(
            model=model,
            base_url=str(base_url),
            authority=authority,
            authority_resolver=lan_runtime_authority_resolver,
            transport=transport,
            timeout_seconds=config.timeout_seconds,
            temperature=config.temperature,
            utc_clock=utc_clock,
        )
    if provider == "mock":
        return MockLLMProvider()
    if provider == "openai":
        active_api_key_env = api_key_env or "OPENAI_API_KEY"
        return OpenAIResponsesProvider(
            model=model,
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "lm-studio":
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "http://localhost:1234/v1",
            api_key="lm-studio",
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="lm-studio",
        )
    if provider == "openai-compatible":
        if not base_url:
            raise ValueError("openai-compatible provider requires base_url")
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url,
            api_key=_resolve_secret(secret_resolver, api_key_env),
            api_key_env=api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "openrouter":
        active_api_key_env = api_key_env or "OPENROUTER_API_KEY"
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://openrouter.ai/api/v1",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="openrouter",
        )
    if provider == "deepseek":
        active_api_key_env = api_key_env or "DEEPSEEK_API_KEY"
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://api.deepseek.com",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="deepseek",
        )
    if provider == "kimi":
        active_api_key_env = api_key_env or "MOONSHOT_API_KEY"
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://api.moonshot.ai/v1",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="kimi",
        )
    if provider == "ollama":
        return OpenAICompatibleProvider(
            model=model,
            base_url=normalize_ollama_openai_base_url(base_url),
            api_key="ollama",
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="ollama",
        )
    if provider == "ollama-cloud":
        active_api_key_env = api_key_env or "OLLAMA_API_KEY"
        return OllamaNativeProvider(
            model=model,
            base_url=base_url or "https://ollama.com/api",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "anthropic":
        active_api_key_env = api_key_env or "ANTHROPIC_API_KEY"
        return AnthropicMessagesProvider(
            model=model,
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "grok":
        active_api_key_env = api_key_env or "XAI_API_KEY"
        return OpenAICompatibleProvider(
            model=model,
            base_url=base_url or "https://api.x.ai/v1",
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
            provider_name="grok",
        )
    if provider == "gemini":
        active_api_key_env = api_key_env or "GEMINI_API_KEY"
        return GeminiProvider(
            model=model,
            api_key=_resolve_secret(secret_resolver, active_api_key_env),
            api_key_env=active_api_key_env,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            temperature=config.temperature,
        )
    if provider == "codex-cli":
        return CodexCLIProvider(
            model=model,
            workspace=config.workspace,
            sandbox=config.codex_sandbox,
            profile=config.codex_profile,
            skip_git_repo_check=config.codex_skip_git_repo_check,
            ephemeral=config.codex_ephemeral,
            secret_store_path=config.secret_store_path,
            secret_backend=config.secret_backend,
        )
    raise ValueError(f"Unsupported provider: {provider}")


def _resolve_secret(secret_resolver: SecretResolver | None, name_or_ref: str | None) -> str | None:
    if secret_resolver is None or not name_or_ref:
        return None
    return secret_resolver(name_or_ref)


def _validate_lan_factory_preflight(
    config: AgentConfig,
    *,
    authority_resolver: LanRuntimeAuthorityResolver | None,
    utc_clock: Callable[[], datetime] | None,
) -> None:
    if config.fallback_provider == "lan-openai-compatible":
        raise ValueError("LAN runtime provider cannot be configured as fallback")
    if config.provider != "lan-openai-compatible":
        if config.lan_runtime_authority is not None:
            raise ValueError("ordinary providers cannot carry LAN runtime authority")
        return
    authority = authenticate_lan_runtime_authority(config.lan_runtime_authority)
    if not callable(authority_resolver):
        raise ValueError("LAN runtime authority resolver is required")
    if utc_clock is not None and not callable(utc_clock):
        raise TypeError("LAN runtime UTC clock must be callable")
    if config.api_key_env is not None:
        raise ValueError("LAN runtime provider cannot use credentials")
    if config.fallback_provider is not None:
        raise ValueError("LAN runtime provider cannot use fallback")
    if config.stream is not False:
        raise ValueError("LAN runtime provider cannot stream")
    if config.model != authority.model_id:
        raise ValueError("LAN runtime model does not match its authority")
    if config.base_url != _lan_authority_base_url(authority):
        raise ValueError("LAN runtime base URL does not match its authority")


def _lan_authority_base_url(authority: LanRuntimeAuthority) -> str:
    address = authority.endpoint.address
    host = f"[{address}]" if ":" in address else address
    return f"http://{host}:{authority.endpoint.port}/v1"


def _utc_now() -> datetime:
    return datetime.now(UTC)
