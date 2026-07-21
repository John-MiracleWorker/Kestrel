from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import RLock
from time import monotonic
from typing import Any

from ..runtime_models import ChatMessage, LLMOptions, LLMResponse, LLMStreamEvent, ToolSpec
from .base import LLMProvider, ProviderCapabilities, ProviderError
from .parser import ControlMessageError


@dataclass
class _ProviderHealth:
    state: str = "unknown"
    consecutive_failures: int = 0
    total_successes: int = 0
    total_failures: int = 0
    failure_class: str | None = None
    retryable: bool | None = None
    opened_at: float | None = None
    half_open_in_flight: bool = False
    last_success_at: str | None = None
    last_failure_at: str | None = None
    total_latency_seconds: float = 0.0
    last_latency_seconds: float | None = None


class ProviderHealthRegistry:
    """Process-local operational health and circuit state without secret-bearing errors."""

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._clock = clock
        self._lock = RLock()
        self._health: dict[str, _ProviderHealth] = {}

    def before_call(self, provider_id: str, *, cooldown_seconds: float) -> None:
        with self._lock:
            health = self._health.setdefault(provider_id, _ProviderHealth())
            if health.state != "open":
                if health.state == "half_open":
                    if health.half_open_in_flight:
                        raise ProviderError("Provider circuit half-open probe is already in progress.", code="circuit_open", retryable=True)
                    health.half_open_in_flight = True
                return
            opened_at = health.opened_at if health.opened_at is not None else self._clock()
            if self._clock() - opened_at < cooldown_seconds:
                raise ProviderError("Provider circuit is open.", code="circuit_open", retryable=True)
            if health.half_open_in_flight:
                raise ProviderError("Provider circuit half-open probe is already in progress.", code="circuit_open", retryable=True)
            health.state = "half_open"
            health.half_open_in_flight = True

    def record_success(self, provider_id: str, *, latency_seconds: float | None = None) -> None:
        with self._lock:
            health = self._health.setdefault(provider_id, _ProviderHealth())
            health.state = "healthy"
            health.consecutive_failures = 0
            health.total_successes += 1
            health.failure_class = None
            health.retryable = None
            health.opened_at = None
            health.half_open_in_flight = False
            health.last_success_at = datetime.now(UTC).isoformat()
            if latency_seconds is not None:
                health.last_latency_seconds = max(0.0, latency_seconds)
                health.total_latency_seconds += max(0.0, latency_seconds)

    def record_failure(
        self,
        provider_id: str,
        *,
        failure_class: str,
        retryable: bool,
        failure_threshold: int,
    ) -> None:
        with self._lock:
            health = self._health.setdefault(provider_id, _ProviderHealth())
            health.total_failures += 1
            health.failure_class = failure_class
            health.retryable = retryable
            health.last_failure_at = datetime.now(UTC).isoformat()
            health.half_open_in_flight = False
            if retryable:
                health.consecutive_failures += 1
                if health.state == "half_open" or health.consecutive_failures >= max(1, failure_threshold):
                    health.state = "open"
                    health.opened_at = self._clock()
                    return
            else:
                health.consecutive_failures = 0
            health.state = "degraded"

    def release_probe(self, provider_id: str) -> None:
        with self._lock:
            health = self._health.get(provider_id)
            if health is not None and health.state == "half_open":
                health.half_open_in_flight = False

    def snapshot(self, provider_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            if provider_id is not None:
                return _health_payload(self._health.get(provider_id, _ProviderHealth()))
            return {key: _health_payload(value) for key, value in sorted(self._health.items())}

    def reset(self) -> None:
        with self._lock:
            self._health.clear()


def _health_payload(health: _ProviderHealth) -> dict[str, Any]:
    return {
        "state": health.state,
        "consecutive_failures": health.consecutive_failures,
        "total_successes": health.total_successes,
        "total_failures": health.total_failures,
        "failure_class": health.failure_class,
        "retryable": health.retryable,
        "last_success_at": health.last_success_at,
        "last_failure_at": health.last_failure_at,
        "last_latency_seconds": health.last_latency_seconds,
        "average_latency_seconds": (
            health.total_latency_seconds / health.total_successes if health.total_successes else None
        ),
    }


class ResilientLLMProvider(LLMProvider):
    def __init__(
        self,
        inner: LLMProvider,
        *,
        provider_id: str,
        registry: ProviderHealthRegistry,
        failure_threshold: int = 3,
        cooldown_seconds: float = 30.0,
    ) -> None:
        self.inner = inner
        self.provider_id = provider_id
        self.registry = registry
        self.failure_threshold = max(1, failure_threshold)
        self.cooldown_seconds = max(0.0, cooldown_seconds)

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self.inner.capabilities

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        self.registry.before_call(self.provider_id, cooldown_seconds=self.cooldown_seconds)
        started = monotonic()
        try:
            response = self.inner.generate(messages, tools, options)
        except Exception as exc:
            normalized = classify_provider_error(exc)
            self.registry.record_failure(
                self.provider_id,
                failure_class=normalized.code,
                retryable=normalized.retryable,
                failure_threshold=self.failure_threshold,
            )
            raise normalized from exc
        self.registry.record_success(self.provider_id, latency_seconds=monotonic() - started)
        return response

    def stream(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> Iterator[LLMStreamEvent]:
        self.registry.before_call(self.provider_id, cooldown_seconds=self.cooldown_seconds)
        started = monotonic()
        try:
            yield from self.inner.stream(messages, tools, options)
        except Exception as exc:
            normalized = classify_provider_error(exc)
            self.registry.record_failure(
                self.provider_id,
                failure_class=normalized.code,
                retryable=normalized.retryable,
                failure_threshold=self.failure_threshold,
            )
            raise normalized from exc
        except BaseException:
            self.registry.release_probe(self.provider_id)
            raise
        self.registry.record_success(self.provider_id, latency_seconds=monotonic() - started)


def classify_provider_error(exc: Exception) -> ProviderError:
    if isinstance(exc, ControlMessageError):
        return ProviderError(str(exc), code=exc.code, retryable=False)
    message = str(exc)
    lowered = message.lower()
    code = exc.code if isinstance(exc, ProviderError) else type(exc).__name__
    normalized_code = str(code).lower()
    if any(marker in lowered or marker in normalized_code for marker in ("401", "403", "unauthor", "forbidden", "api key", "authentication")):
        return ProviderError("Provider authentication failed.", code="authentication", retryable=False)
    if any(marker in lowered or marker in normalized_code for marker in ("429", "rate limit", "ratelimit")):
        return ProviderError("Provider rate limit exceeded.", code="rate_limit", retryable=True)
    if any(marker in lowered or marker in normalized_code for marker in ("timeout", "timed out")):
        return ProviderError("Provider request timed out.", code="timeout", retryable=True)
    if any(
        marker in lowered or marker in normalized_code
        for marker in ("400", "404", "invalid request", "not found", "context length", "badrequest", "notfound")
    ):
        return ProviderError("Provider rejected the request.", code="invalid_request", retryable=False)
    if any(marker in lowered or marker in normalized_code for marker in ("500", "502", "503", "504", "unavailable", "connection")):
        return ProviderError("Provider is unavailable.", code="unavailable", retryable=True)
    if isinstance(exc, ProviderError):
        return ProviderError("Provider request failed.", code=exc.code, retryable=exc.retryable)
    return ProviderError("Provider request failed.", code=type(exc).__name__, retryable=False)


global_provider_health_registry = ProviderHealthRegistry()
