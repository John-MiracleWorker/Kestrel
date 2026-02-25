from __future__ import annotations
"""
Token & Cost Observability — real-time metrics for agent execution.

Inspired by OpenClaw's usage tracking (/usage off|tokens|full),
this module tracks:
  - Prompt and completion tokens per LLM call
  - Estimated cost per model (configurable price table)
  - Per-tool execution times
  - Context compaction events (like OpenClaw's /compact)
  - Model failover events

Metrics are emitted as METRICS_UPDATE events to the SSE stream
and displayed in the frontend MetricsBar component.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("brain.agent.observability")


# ── Model Pricing Table (cost per 1M tokens) ────────────────────────

MODEL_PRICES: dict[str, dict[str, float]] = {
    # OpenAI
    "gpt-4o": {"prompt": 2.50, "completion": 10.00},
    "gpt-4o-mini": {"prompt": 0.15, "completion": 0.60},
    "gpt-4-turbo": {"prompt": 10.00, "completion": 30.00},
    "gpt-4.1": {"prompt": 2.00, "completion": 8.00},
    "o3-mini": {"prompt": 1.10, "completion": 4.40},
    # Anthropic
    "claude-sonnet-4-20250514": {"prompt": 3.00, "completion": 15.00},
    "claude-3-5-sonnet-20241022": {"prompt": 3.00, "completion": 15.00},
    "claude-3-haiku-20240307": {"prompt": 0.25, "completion": 1.25},
    "claude-opus-4-20250514": {"prompt": 15.00, "completion": 75.00},
    # Google
    "gemini-2.5-pro": {"prompt": 1.25, "completion": 10.00},
    "gemini-2.5-flash": {"prompt": 0.15, "completion": 0.60},
    # Local (free)
    "local": {"prompt": 0.0, "completion": 0.0},
}


@dataclass
class ToolMetric:
    """Metrics for a single tool execution."""
    tool_name: str
    execution_time_ms: int
    success: bool
    timestamp: float = 0.0

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()


@dataclass
class TaskMetrics:
    """
    Aggregate metrics for an agent task.

    Updated incrementally as the task executes and emitted
    to the SSE stream as METRICS_UPDATE events.
    """
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0
    llm_calls: int = 0
    tool_executions: int = 0
    avg_tool_time_ms: float = 0.0
    total_elapsed_ms: int = 0
    context_compactions: int = 0
    model_failovers: int = 0
    verifier_runs: int = 0
    verifier_failures: int = 0

    # Detailed breakdowns
    tool_metrics: list[ToolMetric] = field(default_factory=list)
    cost_breakdown: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "llm_calls": self.llm_calls,
            "tool_executions": self.tool_executions,
            "avg_tool_time_ms": round(self.avg_tool_time_ms, 1),
            "total_elapsed_ms": self.total_elapsed_ms,
            "context_compactions": self.context_compactions,
            "model_failovers": self.model_failovers,
            "verifier_runs": self.verifier_runs,
            "verifier_failures": self.verifier_failures,
        }

    def to_compact_dict(self) -> dict:
        """Minimal dict for frequent SSE updates."""
        return {
            "tokens": self.total_tokens,
            "cost_usd": round(self.estimated_cost_usd, 6),
            "elapsed_ms": self.total_elapsed_ms,
            "tools": self.tool_executions,
            "llm_calls": self.llm_calls,
        }


class MetricsCollector:
    """
    Collects and aggregates metrics during agent task execution.

    Integrates with the agent loop to track:
    - LLM token usage and estimated cost
    - Tool execution performance
    - Context compaction and model failover events
    """

    def __init__(self, model: str = ""):
        self._model = model
        self._start_time = time.monotonic()
        self._metrics = TaskMetrics()
        self._total_tool_time_ms = 0

    @property
    def metrics(self) -> TaskMetrics:
        """Current metrics snapshot."""
        self._metrics.total_elapsed_ms = int(
            (time.monotonic() - self._start_time) * 1000
        )
        return self._metrics

    def record_llm_call(
        self,
        model: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cached_tokens: int = 0,
    ) -> None:
        """Record an LLM API call with token counts."""
        self._metrics.prompt_tokens += prompt_tokens
        self._metrics.completion_tokens += completion_tokens
        self._metrics.total_tokens = (
            self._metrics.prompt_tokens + self._metrics.completion_tokens
        )
        self._metrics.llm_calls += 1

        # Calculate cost
        cost = self._estimate_cost(model, prompt_tokens, completion_tokens)
        self._metrics.estimated_cost_usd += cost

        self._metrics.cost_breakdown.append({
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "cached_tokens": cached_tokens,
            "cost_usd": round(cost, 6),
        })

        logger.debug(
            f"LLM call: {model} | "
            f"{prompt_tokens}p + {completion_tokens}c tokens | "
            f"${cost:.6f}"
        )

    def record_tool_execution(
        self,
        tool_name: str,
        execution_time_ms: int,
        success: bool,
    ) -> None:
        """Record a tool execution."""
        metric = ToolMetric(
            tool_name=tool_name,
            execution_time_ms=execution_time_ms,
            success=success,
        )
        self._metrics.tool_metrics.append(metric)
        self._metrics.tool_executions += 1
        self._total_tool_time_ms += execution_time_ms

        # Update running average
        self._metrics.avg_tool_time_ms = (
            self._total_tool_time_ms / self._metrics.tool_executions
        )

    def record_compaction(self) -> None:
        """Record a context compaction event (like OpenClaw's /compact)."""
        self._metrics.context_compactions += 1
        logger.info(f"Context compacted (total: {self._metrics.context_compactions})")

    def record_failover(self, from_model: str, to_model: str) -> None:
        """Record a model failover event."""
        self._metrics.model_failovers += 1
        logger.warning(f"Model failover: {from_model} → {to_model}")

    def record_verifier_result(self, passed: bool, critique: str) -> None:
        """Record a verifier gate evaluation result."""
        self._metrics.verifier_runs += 1
        if not passed:
            self._metrics.verifier_failures += 1
            logger.warning(f"Verifier failed: {critique[:100]}...")
        else:
            logger.info("Verifier passed")

    def _estimate_cost(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Estimate cost based on model pricing table."""
        # Try exact match first, then prefix match
        prices = MODEL_PRICES.get(model)
        if not prices:
            # Try matching by prefix (e.g., "gpt-4o-2024..." → "gpt-4o")
            for key, val in MODEL_PRICES.items():
                if model.startswith(key) or key in model:
                    prices = val
                    break

        if not prices:
            # Unknown model — assume local/free
            return 0.0

        prompt_cost = (prompt_tokens / 1_000_000) * prices["prompt"]
        completion_cost = (completion_tokens / 1_000_000) * prices["completion"]
        return prompt_cost + completion_cost

    def summary(self) -> str:
        """Human-readable summary of task metrics."""
        m = self.metrics
        parts = [
            f"Tokens: {m.total_tokens:,} ({m.prompt_tokens:,}p + {m.completion_tokens:,}c)",
            f"Cost: ${m.estimated_cost_usd:.4f}",
            f"LLM calls: {m.llm_calls}",
            f"Tool calls: {m.tool_executions}",
            f"Avg tool time: {m.avg_tool_time_ms:.0f}ms",
            f"Elapsed: {m.total_elapsed_ms/1000:.1f}s",
        ]
        if m.context_compactions:
            parts.append(f"Compactions: {m.context_compactions}")
        if m.model_failovers:
            parts.append(f"Failovers: {m.model_failovers}")
        return " | ".join(parts)


# ── Context Compaction ───────────────────────────────────────────────
# Inspired by OpenClaw's /compact command. Compresses long conversation
# context into a summary to stay within token limits.

COMPACTION_PROMPT = """\
Summarize the following conversation context into a concise but complete summary.
Preserve all important facts, decisions, tool results, and state information.
The summary should be sufficient for the agent to continue its task without
the original messages.

Context to compact:
{context}

Respond with only the summary, no preamble.
"""


class ContextCompactor:
    """
    Compacts agent conversation context when it grows too long.

    Based on OpenClaw's /compact feature — summarizes old messages
    to keep context within token limits while preserving key information.
    """

    def __init__(self, provider, model: str, max_context_tokens: int = 30_000):
        self._provider = provider
        self._model = model
        self._max_tokens = max_context_tokens

    async def maybe_compact(
        self,
        messages: list[dict],
        metrics_collector: MetricsCollector = None,
    ) -> list[dict]:
        """
        Compact messages if they exceed the token limit.
        Returns the (possibly compacted) message list.
        """
        # Rough token estimate: 4 chars per token
        estimated_tokens = sum(
            len(str(m.get("content", ""))) // 4
            for m in messages
        )

        if estimated_tokens < self._max_tokens:
            return messages  # No compaction needed

        # Keep system message and last 4 messages, summarize the rest
        system_msgs = [m for m in messages if m.get("role") == "system"]
        other_msgs = [m for m in messages if m.get("role") != "system"]

        if len(other_msgs) <= 4:
            return messages  # Not enough to compact

        to_summarize = other_msgs[:-4]
        to_keep = other_msgs[-4:]

        # Build context string
        context_lines = []
        for m in to_summarize:
            role = m.get("role", "?")
            content = m.get("content", "")
            if content:
                context_lines.append(f"[{role}]: {content[:500]}")

        context = "\n".join(context_lines)

        try:
            response = await self._provider.generate(
                messages=[
                    {"role": "user", "content": COMPACTION_PROMPT.format(context=context)},
                ],
                model=self._model,
                temperature=0.1,
                max_tokens=2048,
            )

            summary = response.get("content", "")
            if summary:
                compacted = system_msgs + [
                    {
                        "role": "user",
                        "content": f"[CONTEXT SUMMARY]: {summary}",
                    },
                ] + to_keep

                if metrics_collector:
                    metrics_collector.record_compaction()

                logger.info(
                    f"Context compacted: {len(messages)} msgs → {len(compacted)} msgs "
                    f"(~{estimated_tokens} → ~{sum(len(str(m.get('content', ''))) // 4 for m in compacted)} tokens)"
                )
                return compacted

        except Exception as e:
            logger.warning(f"Context compaction failed: {e}")

        return messages  # Fallback: return original
