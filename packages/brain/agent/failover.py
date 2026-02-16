"""
Model Failover — automatic fallback when an LLM provider fails.

Inspired by OpenClaw's model failover system. Provides:
  - Configurable fallback chains per provider
  - Health checks with exponential backoff
  - Automatic retry with the next model in the chain
  - Cool-down periods for failed providers
  - Per-model error tracking for smart routing
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger("brain.agent.failover")


@dataclass
class ModelHealth:
    """Health tracking for a single model endpoint."""
    model: str
    provider: str
    is_healthy: bool = True
    consecutive_failures: int = 0
    total_failures: int = 0
    total_successes: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0
    cooldown_until: float = 0.0  # Timestamp until which this model is skipped

    @property
    def success_rate(self) -> float:
        total = self.total_failures + self.total_successes
        if total == 0:
            return 1.0
        return self.total_successes / total

    def record_success(self) -> None:
        self.is_healthy = True
        self.consecutive_failures = 0
        self.total_successes += 1
        self.last_success_time = time.time()
        self.cooldown_until = 0.0

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        self.total_failures += 1
        self.last_failure_time = time.time()

        # Exponential backoff: 10s, 30s, 90s, 270s, max 600s
        backoff = min(10 * (3 ** (self.consecutive_failures - 1)), 600)
        self.cooldown_until = time.time() + backoff

        if self.consecutive_failures >= 3:
            self.is_healthy = False
            logger.warning(
                f"Model {self.model} marked unhealthy "
                f"({self.consecutive_failures} consecutive failures, "
                f"cooldown {backoff}s)"
            )

    def is_available(self) -> bool:
        """Check if model is available (healthy or cooldown expired)."""
        if self.is_healthy:
            return True
        if time.time() > self.cooldown_until:
            # Cooldown expired — allow a retry
            return True
        return False


# ── Default Fallback Chains ──────────────────────────────────────────

DEFAULT_CHAINS: dict[str, list[str]] = {
    # OpenAI chain
    "gpt-4o": ["gpt-4o", "gpt-4o-mini", "gpt-4.1"],
    "gpt-4o-mini": ["gpt-4o-mini", "gpt-4o"],
    "o3-mini": ["o3-mini", "gpt-4o"],

    # Anthropic chain
    "claude-sonnet-4-20250514": [
        "claude-sonnet-4-20250514",
        "claude-3-5-sonnet-20241022",
        "claude-3-haiku-20240307",
    ],
    "claude-opus-4-20250514": [
        "claude-opus-4-20250514",
        "claude-sonnet-4-20250514",
    ],

    # Google chain
    "gemini-2.5-pro": ["gemini-2.5-pro", "gemini-2.5-flash"],
    "gemini-2.5-flash": ["gemini-2.5-flash", "gemini-2.5-pro"],
}


class ModelFailover:
    """
    Manages model failover with health tracking and retry logic.

    Usage:
        failover = ModelFailover(primary_model="gpt-4o")
        result = await failover.call_with_failover(
            generate_fn=provider.generate,
            messages=messages,
        )
    """

    def __init__(
        self,
        primary_model: str,
        custom_chain: list[str] = None,
        max_retries: int = 3,
        metrics_collector=None,
    ):
        self._primary = primary_model
        self._chain = custom_chain or DEFAULT_CHAINS.get(primary_model, [primary_model])
        self._max_retries = max_retries
        self._metrics = metrics_collector
        self._health: dict[str, ModelHealth] = {}

        # Initialize health tracking for all models in chain
        for model in self._chain:
            provider = self._infer_provider(model)
            self._health[model] = ModelHealth(model=model, provider=provider)

    async def call_with_failover(
        self,
        generate_fn: Callable[..., Coroutine],
        messages: list[dict],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        **kwargs,
    ) -> dict[str, Any]:
        """
        Call the LLM with automatic failover on errors.

        Tries each model in the chain until one succeeds or all fail.
        Returns the response dict with an added 'model_used' field.
        """
        last_error = None

        for model in self._chain:
            health = self._health.get(model)
            if health and not health.is_available():
                logger.debug(f"Skipping {model} (in cooldown)")
                continue

            try:
                response = await generate_fn(
                    messages=messages,
                    model=model,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **kwargs,
                )

                # Record success
                if health:
                    health.record_success()

                response["model_used"] = model

                # Track failover in metrics if this wasn't the primary
                if model != self._primary and self._metrics:
                    self._metrics.record_failover(self._primary, model)

                return response

            except Exception as e:
                last_error = e
                logger.warning(f"Model {model} failed: {e}")

                if health:
                    health.record_failure()

                # Brief pause before trying next model
                await asyncio.sleep(0.5)

        # All models failed
        raise RuntimeError(
            f"All models in chain exhausted. "
            f"Chain: {self._chain}. "
            f"Last error: {last_error}"
        )

    def get_health_report(self) -> list[dict]:
        """Get health status for all models in the chain."""
        return [
            {
                "model": h.model,
                "provider": h.provider,
                "is_healthy": h.is_healthy,
                "is_available": h.is_available(),
                "success_rate": round(h.success_rate, 3),
                "consecutive_failures": h.consecutive_failures,
                "total_calls": h.total_failures + h.total_successes,
            }
            for h in self._health.values()
        ]

    def reset_health(self, model: str = None) -> None:
        """Reset health tracking for a model or all models."""
        if model and model in self._health:
            self._health[model] = ModelHealth(
                model=model,
                provider=self._infer_provider(model),
            )
        elif not model:
            for m in self._health:
                self._health[m] = ModelHealth(
                    model=m,
                    provider=self._infer_provider(m),
                )

    @staticmethod
    def _infer_provider(model: str) -> str:
        """Infer provider from model name."""
        if "gpt" in model or "o3" in model or "o1" in model:
            return "openai"
        if "claude" in model:
            return "anthropic"
        if "gemini" in model:
            return "google"
        if "mistral" in model or "mixtral" in model:
            return "mistral"
        return "unknown"
