from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request

from .config import AgentConfig
from .llm.model_catalog import (
    DEFAULT_BASE_URLS,
    ProviderModelCatalog,
    model_catalog_for_provider,
    read_bounded_http_body,
    urlopen,
)
from .llm.provider_urls import normalize_ollama_openai_base_url, validate_provider_http_url
from .routing.models import ProviderProfile
from .security_boundary import REDACTED, redact_text

CapabilityProvenance = Literal[
    "observed",
    "provider_declared",
    "operator_supplied",
    "unknown",
]
CapabilityStatus = Literal["pass", "fail", "not_run"]

PROBE_CAPABILITIES: tuple[str, ...] = (
    "generation",
    "streaming",
    "structured_output",
    "tools",
    "vision",
)
PROVIDER_DISCOVERY_SCHEMA = "kestrel.routing.provider_discovery.v1"
MAX_DISCOVERY_MODELS = 8
MAX_PROBE_TIMEOUT_SECONDS = 5.0
MAX_PROBE_RESPONSE_BYTES = 256 * 1024
_OPENAI_COMPATIBLE_ADAPTERS = frozenset(
    {
        "lm-studio",
        "ollama",
        "openai-compatible",
        "openrouter",
        "deepseek",
        "kimi",
        "grok",
    }
)

SecretResolver = Callable[[str | None], str | None]
CatalogLoader = Callable[[ProviderProfile, float], ProviderModelCatalog]


@dataclass(frozen=True)
class CapabilityEvidence:
    capability: str
    supported: bool | None
    provenance: CapabilityProvenance
    status: CapabilityStatus
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.capability not in PROBE_CAPABILITIES:
            raise ValueError(f"unsupported probe capability: {self.capability}")
        if self.status == "pass" and self.supported is not True:
            raise ValueError("passing capability evidence must be supported")
        if self.status == "fail" and self.provenance == "unknown":
            raise ValueError("failed capability evidence must identify its provenance")

    @classmethod
    def observed_pass(cls, capability: str) -> CapabilityEvidence:
        return cls(
            capability=capability,
            supported=True,
            provenance="observed",
            status="pass",
        )

    @classmethod
    def observed_failure(cls, capability: str, detail: str) -> CapabilityEvidence:
        return cls(
            capability=capability,
            supported=None,
            provenance="observed",
            status="fail",
            detail=detail,
        )

    @classmethod
    def provider_declared(cls, capability: str) -> CapabilityEvidence:
        return cls(
            capability=capability,
            supported=True,
            provenance="provider_declared",
            status="pass",
        )

    @classmethod
    def operator_supplied(
        cls,
        capability: str,
        *,
        supported: bool,
    ) -> CapabilityEvidence:
        return cls(
            capability=capability,
            supported=True if supported else None,
            provenance="operator_supplied",
            status="pass" if supported else "not_run",
            detail=None if supported else "operator did not claim this capability",
        )

    @classmethod
    def unknown(cls, capability: str, *, detail: str | None = None) -> CapabilityEvidence:
        return cls(
            capability=capability,
            supported=None,
            provenance="unknown",
            status="not_run",
            detail=detail,
        )

    def to_public_payload(self) -> dict[str, object]:
        return {
            "capability": self.capability,
            "supported": self.supported,
            "provenance": self.provenance,
            "status": self.status,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ModelProbeObservation:
    model: str
    model_identity: str | None = None
    latency_ms: float | None = None
    capabilities: tuple[CapabilityEvidence, ...] = ()

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("probe model is required")
        if self.latency_ms is not None:
            if not math.isfinite(self.latency_ms) or self.latency_ms < 0:
                raise ValueError("probe latency must be a finite non-negative number")
        names = [item.capability for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("probe capability evidence must be unique")


class ProviderProbeBackend(Protocol):
    def probe(
        self,
        profile: ProviderProfile,
        model: str,
        *,
        timeout_seconds: float,
    ) -> ModelProbeObservation: ...


@dataclass(frozen=True)
class DiscoveredModelProbe:
    model: str
    model_identity: str | None
    identity_provenance: CapabilityProvenance
    latency_ms: float | None
    capabilities: tuple[CapabilityEvidence, ...]

    def to_public_payload(self) -> dict[str, object]:
        return {
            "model": self.model,
            "model_identity": self.model_identity,
            "identity_provenance": self.identity_provenance,
            "latency_ms": self.latency_ms,
            "capabilities": [item.to_public_payload() for item in self.capabilities],
        }


@dataclass(frozen=True)
class ProviderDiscoveryResult:
    provider_profile_id: str
    provider: str
    catalog_ok: bool
    catalog_source: str
    catalog_digest: str
    catalog_fetched_at: str | None
    catalog_complete: bool
    catalog_truncated: bool
    reported_model_count: int | None
    probed_at: str
    catalog_models: tuple[str, ...]
    models: tuple[DiscoveredModelProbe, ...]
    catalog_error: str | None = None

    def to_public_payload(self) -> dict[str, object]:
        return {
            "schema": PROVIDER_DISCOVERY_SCHEMA,
            "provider_profile_id": self.provider_profile_id,
            "provider": self.provider,
            "catalog_ok": self.catalog_ok,
            "catalog_source": self.catalog_source,
            "catalog_digest": self.catalog_digest,
            "catalog_fetched_at": self.catalog_fetched_at,
            "catalog_complete": self.catalog_complete,
            "catalog_truncated": self.catalog_truncated,
            "reported_model_count": self.reported_model_count,
            "probed_at": self.probed_at,
            "catalog_model_count": len(self.catalog_models),
            "probed_model_count": len(self.models),
            "models": [item.to_public_payload() for item in self.models],
            "catalog_error": self.catalog_error,
        }


@dataclass(frozen=True)
class RoutingConstraintPreset:
    preset_id: str
    display_name: str
    constraints: Mapping[str, object]
    effect: Literal["filter_or_rank_only"] = "filter_or_rank_only"
    can_enable_targets: bool = False
    can_change_trust: bool = False

    def to_public_payload(self) -> dict[str, object]:
        return {
            "preset_id": self.preset_id,
            "display_name": self.display_name,
            "constraints": dict(self.constraints),
            "effect": self.effect,
            "can_enable_targets": self.can_enable_targets,
            "can_change_trust": self.can_change_trust,
        }


def routing_constraint_presets() -> tuple[RoutingConstraintPreset, ...]:
    """Return presets that can filter/rank existing eligible targets, never authorize them."""

    return (
        RoutingConstraintPreset(
            preset_id="local-only",
            display_name="Local Only",
            constraints={
                "allowed_localities": ["local"],
                "require_enabled": True,
                "require_healthy": True,
            },
        ),
        RoutingConstraintPreset(
            preset_id="balanced",
            display_name="Balanced",
            constraints={
                "require_enabled": True,
                "require_healthy": True,
                "rank_by": ["validated_success", "quality", "cost", "latency"],
            },
        ),
        RoutingConstraintPreset(
            preset_id="cheapest-validated",
            display_name="Cheapest Validated",
            constraints={
                "require_enabled": True,
                "require_validation_evidence": True,
                "require_known_cost": True,
                "rank_by": ["estimated_cost_usd", "validated_success"],
            },
        ),
        RoutingConstraintPreset(
            preset_id="fastest",
            display_name="Fastest",
            constraints={
                "require_enabled": True,
                "require_healthy": True,
                "require_observed_latency": True,
                "rank_by": ["observed_latency_ms", "validated_success"],
            },
        ),
        RoutingConstraintPreset(
            preset_id="frontier-review",
            display_name="Frontier Review",
            constraints={
                "require_enabled": True,
                "minimum_quality_tier": 4,
                "task_roles": ["reviewer"],
                "require_validation_evidence": True,
            },
        ),
        RoutingConstraintPreset(
            preset_id="privacy-first",
            display_name="Privacy First",
            constraints={
                "require_enabled": True,
                "forbid_unapproved_cloud": True,
                "rank_by": ["locality", "validated_success"],
            },
        ),
    )


class ProviderProbeService:
    def __init__(
        self,
        *,
        catalog_loader: CatalogLoader | None = None,
        probe_backend: ProviderProbeBackend | None = None,
        secret_resolver: SecretResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._catalog_loader = catalog_loader or self._load_catalog
        self._probe_backend = probe_backend or BoundedOpenAICompatibleProbeBackend(
            secret_resolver=secret_resolver
        )
        self._clock = clock or (lambda: datetime.now(UTC))

    def discover(
        self,
        profile: ProviderProfile,
        *,
        max_models: int,
        timeout_seconds: float,
        probe_capabilities: bool,
    ) -> ProviderDiscoveryResult:
        if isinstance(max_models, bool) or not 1 <= max_models <= MAX_DISCOVERY_MODELS:
            raise ValueError(f"max_models must be between 1 and {MAX_DISCOVERY_MODELS}")
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or not 0.25 <= timeout_seconds <= MAX_PROBE_TIMEOUT_SECONDS
        ):
            raise ValueError(
                f"timeout_seconds must be between 0.25 and {MAX_PROBE_TIMEOUT_SECONDS}"
            )
        now = _format_timestamp(self._clock())
        secret = self._resolve_secret(profile.secret_ref)
        sensitive_values = (secret, profile.secret_ref)
        try:
            catalog = self._catalog_loader(profile, timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - provider boundary becomes safe evidence
            catalog = ProviderModelCatalog(
                provider=profile.adapter,
                models=(),
                fallback_models=(),
                source="error",
                ok=False,
                fetchable=True,
                error=_safe_error(str(exc), secrets=sensitive_values),
            )
        catalog_error = _safe_error(catalog.error, secrets=sensitive_values)
        catalog_is_current = (
            catalog.ok
            and catalog.source == "provider"
            and catalog.provider == profile.adapter
            and catalog.fetched_at is not None
        )
        if not catalog_is_current:
            if catalog.provider != profile.adapter:
                catalog_error = "provider catalog identity does not match the profile adapter"
            elif catalog.ok and catalog.source == "provider" and catalog.fetched_at is None:
                catalog_error = "provider catalog did not include freshness evidence"
            return ProviderDiscoveryResult(
                provider_profile_id=profile.profile_id,
                provider=profile.adapter,
                catalog_ok=False,
                catalog_source=catalog.source,
                catalog_digest=catalog.digest,
                catalog_fetched_at=catalog.fetched_at,
                catalog_complete=False,
                catalog_truncated=catalog.catalog_truncated,
                reported_model_count=catalog.reported_model_count,
                probed_at=now,
                catalog_models=(),
                models=(),
                catalog_error=catalog_error or "live provider catalog is unavailable",
            )

        discovered: list[DiscoveredModelProbe] = []
        for model in catalog.models[:max_models]:
            declaration = set(catalog.declared_capabilities.get(model, ()))
            observation: ModelProbeObservation | None = None
            probe_error: str | None = None
            if probe_capabilities:
                try:
                    observation = self._probe_backend.probe(
                        profile,
                        model,
                        timeout_seconds=timeout_seconds,
                    )
                    if observation.model != model:
                        raise ValueError("probe observation model does not match catalog model")
                except Exception as exc:  # noqa: BLE001 - provider boundary becomes evidence
                    probe_error = _safe_error(str(exc), secrets=sensitive_values)

            observed = (
                {item.capability: item for item in observation.capabilities}
                if observation is not None
                else {}
            )
            capabilities: list[CapabilityEvidence] = []
            for capability in PROBE_CAPABILITIES:
                if capability in observed:
                    evidence = observed[capability]
                    capabilities.append(
                        CapabilityEvidence(
                            capability=evidence.capability,
                            supported=evidence.supported,
                            provenance=evidence.provenance,
                            status=evidence.status,
                            detail=_safe_error(
                                evidence.detail,
                                secrets=sensitive_values,
                            ),
                        )
                    )
                elif capability in declaration:
                    capabilities.append(CapabilityEvidence.provider_declared(capability))
                elif probe_error is not None:
                    capabilities.append(
                        CapabilityEvidence.observed_failure(capability, probe_error)
                    )
                else:
                    capabilities.append(CapabilityEvidence.unknown(capability))
            observed_identity = (
                _safe_identity(
                    observation.model_identity,
                    secrets=sensitive_values,
                )
                if observation is not None
                else None
            )
            discovered.append(
                DiscoveredModelProbe(
                    model=model,
                    model_identity=observed_identity or model,
                    identity_provenance=(
                        "observed" if observed_identity else "provider_declared"
                    ),
                    latency_ms=observation.latency_ms if observation is not None else None,
                    capabilities=tuple(capabilities),
                )
            )

        return ProviderDiscoveryResult(
            provider_profile_id=profile.profile_id,
            provider=profile.adapter,
            catalog_ok=True,
            catalog_source=catalog.source,
            catalog_digest=catalog.digest,
            catalog_fetched_at=catalog.fetched_at,
            catalog_complete=catalog.catalog_complete,
            catalog_truncated=catalog.catalog_truncated,
            reported_model_count=catalog.reported_model_count,
            probed_at=now,
            catalog_models=catalog.models,
            models=tuple(discovered),
            catalog_error=None,
        )

    def _load_catalog(
        self,
        profile: ProviderProfile,
        timeout_seconds: float,
    ) -> ProviderModelCatalog:
        config = AgentConfig(
            provider=profile.adapter,
            model="kestrel-discovery-probe",
            base_url=profile.base_url,
            api_key_env=profile.secret_ref,
            timeout_seconds=max(1, math.ceil(timeout_seconds)),
            max_retries=0,
        )
        return model_catalog_for_provider(
            config,
            profile.adapter,
            secret_resolver=self._secret_resolver,
        )

    def _resolve_secret(self, secret_ref: str | None) -> str | None:
        if self._secret_resolver is None or secret_ref is None:
            return None
        try:
            return self._secret_resolver(secret_ref)
        except Exception:  # noqa: BLE001 - resolver failure must not disclose secret state
            return None


class BoundedOpenAICompatibleProbeBackend:
    """Small explicit probes for OpenAI-compatible endpoints.

    Each model gets one overall deadline, every generation is capped at eight
    tokens, and every response body is size-limited.
    """

    def __init__(
        self,
        *,
        secret_resolver: SecretResolver | None = None,
        monotonic_clock: Callable[[], float] = monotonic,
    ) -> None:
        self._secret_resolver = secret_resolver
        self._monotonic = monotonic_clock

    def probe(
        self,
        profile: ProviderProfile,
        model: str,
        *,
        timeout_seconds: float,
    ) -> ModelProbeObservation:
        if profile.adapter not in _OPENAI_COMPATIBLE_ADAPTERS:
            raise ValueError(
                f"bounded active probes are not implemented for adapter {profile.adapter}"
            )
        base_url = _probe_base_url(profile)
        endpoint = _join_url(base_url, "chat/completions")
        secret = (
            self._secret_resolver(profile.secret_ref)
            if self._secret_resolver is not None and profile.secret_ref is not None
            else None
        )
        deadline = self._monotonic() + timeout_seconds
        common: dict[str, object] = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK only."}],
            "max_tokens": 8,
            "temperature": 0,
        }

        generation_started = self._monotonic()
        generation, generation_evidence = self._attempt_json(
            "generation",
            endpoint,
            common,
            deadline=deadline,
            secret=secret,
            validator=_valid_generation,
        )
        latency_ms = (
            max(0.0, (self._monotonic() - generation_started) * 1000.0)
            if generation_evidence.status == "pass"
            else None
        )
        model_identity = _model_identity(generation) if generation is not None else None

        _stream_payload, streaming_evidence = self._attempt_bytes(
            "streaming",
            endpoint,
            {**common, "stream": True},
            deadline=deadline,
            secret=secret,
            validator=_valid_stream,
        )
        _structured, structured_evidence = self._attempt_json(
            "structured_output",
            endpoint,
            {
                **common,
                "messages": [
                    {
                        "role": "user",
                        "content": 'Return exactly one JSON object: {"ok":true}.',
                    }
                ],
                "response_format": {"type": "json_object"},
            },
            deadline=deadline,
            secret=secret,
            validator=_valid_structured_output,
        )
        _tools, tools_evidence = self._attempt_json(
            "tools",
            endpoint,
            {
                **common,
                "messages": [
                    {
                        "role": "user",
                        "content": "Call the kestrel_probe tool with value ok.",
                    }
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "kestrel_probe",
                            "description": "Return the bounded probe value.",
                            "parameters": {
                                "type": "object",
                                "properties": {"value": {"type": "string"}},
                                "required": ["value"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
                "tool_choice": "required",
            },
            deadline=deadline,
            secret=secret,
            validator=_valid_tool_call,
        )
        return ModelProbeObservation(
            model=model,
            model_identity=model_identity,
            latency_ms=latency_ms,
            capabilities=(
                generation_evidence,
                streaming_evidence,
                structured_evidence,
                tools_evidence,
            ),
        )

    def _attempt_json(
        self,
        capability: str,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        deadline: float,
        secret: str | None,
        validator: Callable[[Mapping[str, Any]], bool],
    ) -> tuple[Mapping[str, Any] | None, CapabilityEvidence]:
        try:
            response = _post_json(
                endpoint,
                payload,
                secret=secret,
                deadline=deadline,
                monotonic_clock=self._monotonic,
            )
            if not validator(response):
                raise ValueError("provider response did not satisfy the bounded probe")
            return response, CapabilityEvidence.observed_pass(capability)
        except Exception as exc:  # noqa: BLE001 - captured as non-authoritative evidence
            return None, CapabilityEvidence.observed_failure(
                capability,
                _safe_error(str(exc), secrets=(secret,)) or type(exc).__name__,
            )

    def _attempt_bytes(
        self,
        capability: str,
        endpoint: str,
        payload: Mapping[str, object],
        *,
        deadline: float,
        secret: str | None,
        validator: Callable[[bytes], bool],
    ) -> tuple[bytes | None, CapabilityEvidence]:
        try:
            response = _post_bytes(
                endpoint,
                payload,
                secret=secret,
                deadline=deadline,
                monotonic_clock=self._monotonic,
            )
            if not validator(response):
                raise ValueError("provider stream did not satisfy the bounded probe")
            return response, CapabilityEvidence.observed_pass(capability)
        except Exception as exc:  # noqa: BLE001 - captured as non-authoritative evidence
            return None, CapabilityEvidence.observed_failure(
                capability,
                _safe_error(str(exc), secrets=(secret,)) or type(exc).__name__,
            )


def _probe_base_url(profile: ProviderProfile) -> str:
    configured = profile.base_url or DEFAULT_BASE_URLS.get(profile.adapter)
    if not configured:
        raise ValueError(f"provider profile {profile.profile_id} has no probe endpoint")
    if profile.adapter == "ollama":
        return normalize_ollama_openai_base_url(configured)
    return validate_provider_http_url(configured)


def _join_url(base_url: str, suffix: str) -> str:
    safe_base_url = validate_provider_http_url(base_url)
    return validate_provider_http_url(urljoin(f"{safe_base_url.rstrip('/')}/", suffix))


def _remaining_seconds(deadline: float, clock: Callable[[], float]) -> float:
    remaining = deadline - clock()
    if remaining <= 0:
        raise TimeoutError("provider probe deadline exceeded")
    return max(0.001, remaining)


def _post_json(
    url: str,
    payload: Mapping[str, object],
    *,
    timeout_seconds: float | None = None,
    secret: str | None,
    deadline: float | None = None,
    monotonic_clock: Callable[[], float] = monotonic,
) -> Mapping[str, Any]:
    body = _post_bytes(
        url,
        payload,
        timeout_seconds=timeout_seconds,
        secret=secret,
        deadline=deadline,
        monotonic_clock=monotonic_clock,
    )
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("provider probe response must be a JSON object")
    return parsed


def _post_bytes(
    url: str,
    payload: Mapping[str, object],
    *,
    timeout_seconds: float | None = None,
    secret: str | None,
    deadline: float | None = None,
    monotonic_clock: Callable[[], float] = monotonic,
) -> bytes:
    active_deadline = _active_deadline(
        timeout_seconds=timeout_seconds,
        deadline=deadline,
        monotonic_clock=monotonic_clock,
    )
    safe_url = validate_provider_http_url(url)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if secret:
        headers["Authorization"] = f"Bearer {secret}"
    request = Request(
        safe_url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        # URL schemes and hosts are validated immediately above.
        with urlopen(
            request,
            timeout=_remaining_seconds(active_deadline, monotonic_clock),
        ) as response:  # nosec B310
            body = read_bounded_http_body(
                response,
                max_bytes=MAX_PROBE_RESPONSE_BYTES,
                deadline=active_deadline,
                monotonic_clock=monotonic_clock,
            )
    except HTTPError as exc:
        try:
            detail_bytes = read_bounded_http_body(
                exc,
                max_bytes=240,
                deadline=active_deadline,
                monotonic_clock=monotonic_clock,
            )
            detail = detail_bytes.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - error-body diagnostics are best effort
            detail = "response detail unavailable"
        raise RuntimeError(
            f"provider probe failed with HTTP {exc.code}: "
            f"{_safe_error(detail, secrets=(secret,))}"
        ) from exc
    except URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise RuntimeError(
            f"provider probe request failed: {_safe_error(str(reason), secrets=(secret,))}"
        ) from exc
    return body


def _valid_generation(payload: Mapping[str, Any]) -> bool:
    message = _response_message(payload)
    if message is None:
        return False
    content = message.get("content")
    return isinstance(content, str) and bool(content.strip())


def _valid_structured_output(payload: Mapping[str, Any]) -> bool:
    message = _response_message(payload)
    if message is None:
        return False
    content = message.get("content")
    if not isinstance(content, str):
        return False
    try:
        return bool(json.loads(content) == {"ok": True})
    except json.JSONDecodeError:
        return False


def _valid_tool_call(payload: Mapping[str, Any]) -> bool:
    message = _response_message(payload)
    if message is None:
        return False
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return False
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict) or function.get("name") != "kestrel_probe":
            continue
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                continue
        if arguments == {"value": "ok"}:
            return True
    return False


def _valid_stream(payload: bytes) -> bool:
    observed_content = False
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.lower().startswith(b"event:") and b"error" in line.lower():
            return False
        if not line.startswith(b"data:"):
            continue
        raw_data = line.removeprefix(b"data:").strip()
        if raw_data == b"[DONE]":
            continue
        try:
            event = json.loads(raw_data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not isinstance(event, dict) or event.get("error") is not None:
            return False
        choices = event.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                observed_content = True
    return observed_content


def _active_deadline(
    *,
    timeout_seconds: float | None,
    deadline: float | None,
    monotonic_clock: Callable[[], float],
) -> float:
    if deadline is None:
        if timeout_seconds is None or timeout_seconds <= 0:
            raise ValueError("a positive provider probe timeout is required")
        return monotonic_clock() + timeout_seconds
    if timeout_seconds is None:
        return deadline
    if timeout_seconds <= 0:
        raise ValueError("provider probe timeout must be positive")
    return min(deadline, monotonic_clock() + timeout_seconds)


def _response_message(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    return message if isinstance(message, dict) else None


def _model_identity(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None:
        return None
    model = payload.get("model")
    if not isinstance(model, str):
        return None
    stripped = model.strip()
    return stripped[:512] if stripped else None


def _safe_error(value: str | None, *, secrets: tuple[str | None, ...]) -> str | None:
    if value is None:
        return None
    safe = value
    for secret in sorted(
        {item for item in secrets if isinstance(item, str) and item},
        key=len,
        reverse=True,
    ):
        safe = safe.replace(secret, REDACTED)
    return redact_text(safe)[:512]


def _safe_identity(
    value: str | None,
    *,
    secrets: tuple[str | None, ...],
) -> str | None:
    safe = _safe_error(value, secrets=secrets)
    if safe is None:
        return None
    printable = "".join(character for character in safe if character.isprintable()).strip()
    return printable or None


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("provider probe clock must return a timezone-aware datetime")
    return value.astimezone(UTC).isoformat()
