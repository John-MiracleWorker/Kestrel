from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, cast

from .config import AgentConfig
from .event_log import redact_secrets
from .llm.model_catalog import (
    PROVIDER_OPTIONS,
    STATIC_MODEL_SUGGESTIONS,
    default_api_key_env,
)


class ProviderCertificationStatus(StrEnum):
    CERTIFIED = "certified"
    CONFIGURED = "configured"
    BLOCKED = "blocked"
    MANUAL_VALIDATION_REQUIRED = "manual_validation_required"


@dataclass(frozen=True)
class ProviderCertificationEntry:
    provider: str
    status: ProviderCertificationStatus
    model_suggestions: tuple[str, ...]
    api_key_env: dict[str, Any] | None
    base_url_configured: bool
    evidence: tuple[str, ...]
    next_action: str
    live_validation_command: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "model_suggestions": list(self.model_suggestions),
            "api_key_env": self.api_key_env,
            "base_url_configured": self.base_url_configured,
            "evidence": list(self.evidence),
            "next_action": self.next_action,
            "live_validation_command": self.live_validation_command,
        }


@dataclass(frozen=True)
class ProviderCertificationHeadline:
    total_providers: int
    certified_count: int
    configured_count: int
    blocked_count: int
    manual_validation_required_count: int
    release_certified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_providers": self.total_providers,
            "certified_count": self.certified_count,
            "configured_count": self.configured_count,
            "blocked_count": self.blocked_count,
            "manual_validation_required_count": self.manual_validation_required_count,
            "release_certified": self.release_certified,
        }


@dataclass(frozen=True)
class ProviderCertificationReport:
    schema: str
    headline: ProviderCertificationHeadline
    providers: tuple[ProviderCertificationEntry, ...]

    def provider(self, provider_name: str) -> ProviderCertificationEntry:
        for provider in self.providers:
            if provider.provider == provider_name:
                return provider
        raise KeyError(provider_name)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema": self.schema,
            "headline": self.headline.to_dict(),
            "providers": [provider.to_dict() for provider in self.providers],
        }
        return cast(dict[str, Any], redact_secrets(payload))


def build_provider_certification_report(config: AgentConfig) -> ProviderCertificationReport:
    providers = tuple(_provider_entry(config, provider) for provider in PROVIDER_OPTIONS)
    certified_count = sum(
        1 for provider in providers if provider.status == ProviderCertificationStatus.CERTIFIED
    )
    configured_count = sum(
        1 for provider in providers if provider.status == ProviderCertificationStatus.CONFIGURED
    )
    blocked_count = sum(
        1 for provider in providers if provider.status == ProviderCertificationStatus.BLOCKED
    )
    manual_count = sum(
        1
        for provider in providers
        if provider.status == ProviderCertificationStatus.MANUAL_VALIDATION_REQUIRED
    )
    return ProviderCertificationReport(
        schema="kestrel.provider_certification.v1",
        headline=ProviderCertificationHeadline(
            total_providers=len(providers),
            certified_count=certified_count,
            configured_count=configured_count,
            blocked_count=blocked_count,
            manual_validation_required_count=manual_count,
            release_certified=certified_count == len(providers),
        ),
        providers=providers,
    )


def _provider_entry(config: AgentConfig, provider: str) -> ProviderCertificationEntry:
    suggestions = STATIC_MODEL_SUGGESTIONS.get(provider, ())
    command = _live_validation_command(provider)
    if provider == "mock":
        return ProviderCertificationEntry(
            provider=provider,
            status=ProviderCertificationStatus.CERTIFIED,
            model_suggestions=suggestions,
            api_key_env=None,
            base_url_configured=False,
            evidence=(
                "Deterministic mock provider is available without credentials.",
                "Mock provider is covered by the default test and golden-eval path.",
            ),
            next_action="Keep mock coverage in the fast release suite.",
            live_validation_command=command,
        )
    if provider == "codex-cli":
        configured = shutil.which("codex") is not None
        return ProviderCertificationEntry(
            provider=provider,
            status=ProviderCertificationStatus.CONFIGURED
            if configured
            else ProviderCertificationStatus.MANUAL_VALIDATION_REQUIRED,
            model_suggestions=suggestions,
            api_key_env=None,
            base_url_configured=False,
            evidence=(
                "Codex CLI provider is local-process backed and must be validated on the target host.",
                "codex executable is present." if configured else "codex executable was not found on PATH.",
            ),
            next_action="Run the Codex CLI provider smoke and golden flow on a configured workstation.",
            live_validation_command=command,
        )

    if provider in {"openai-compatible", "ollama"}:
        base_url = _base_url_for(config, provider)
        configured = bool(base_url)
        return ProviderCertificationEntry(
            provider=provider,
            status=ProviderCertificationStatus.CONFIGURED
            if configured
            else ProviderCertificationStatus.BLOCKED,
            model_suggestions=suggestions,
            api_key_env=_env_presence(config.api_key_env) if provider == "openai-compatible" else None,
            base_url_configured=configured,
            evidence=(
                "Provider uses an OpenAI-compatible local/model-server endpoint.",
                "Base URL is configured." if configured else "Base URL is missing.",
            ),
            next_action="Run live provider integration and golden evals against the configured endpoint."
            if configured
            else "Set NEST_AGENT_BASE_URL or pass --base-url before certification.",
            live_validation_command=command,
        )

    env_name = default_api_key_env(provider, config.api_key_env if provider == config.provider else None)
    env_present = bool(env_name and os.getenv(env_name))
    return ProviderCertificationEntry(
        provider=provider,
        status=ProviderCertificationStatus.CONFIGURED
        if env_present
        else ProviderCertificationStatus.BLOCKED,
        model_suggestions=suggestions,
        api_key_env=_env_presence(env_name),
        base_url_configured=bool(_base_url_for(config, provider)),
        evidence=(
            f"{provider} provider adapter exists.",
            f"{env_name} is present." if env_present else f"{env_name or 'API key env'} is missing.",
        ),
        next_action="Run credentialed provider integration, golden evals, and live learning checks."
        if env_present
        else "Configure the provider API-key environment variable before release certification.",
        live_validation_command=command,
    )


def _env_presence(name: str | None) -> dict[str, Any] | None:
    if not name:
        return None
    return {"name": name, "present": bool(os.getenv(name))}


def _base_url_for(config: AgentConfig, provider: str) -> str | None:
    if provider == config.provider and config.base_url:
        return config.base_url
    if provider == config.fallback_provider and config.fallback_base_url:
        return config.fallback_base_url
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    if provider == "deepseek":
        return "https://api.deepseek.com"
    if provider == "kimi":
        return "https://api.moonshot.ai/v1"
    if provider == "ollama":
        return "http://localhost:11434/v1"
    if provider == "ollama-cloud":
        return "https://ollama.com/api"
    return None


def _live_validation_command(provider: str) -> str:
    if provider == "mock":
        return "python scripts/run_golden_evals.py --backend memory --provider mock"
    return (
        "RUN_PROVIDER_INTEGRATION=1 python -m pytest -q "
        f"'tests/integration/test_provider_live_integration.py::test_live_provider_generate_smoke[{provider}]' "
        f"'tests/integration/test_provider_live_integration.py::test_live_provider_stream_smoke[{provider}]'"
    )
