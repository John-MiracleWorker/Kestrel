from __future__ import annotations

from dataclasses import dataclass

from .llm.base import LLMProvider
from .runtime_models import ChatMessage, LLMOptions, ToolExecution


@dataclass(frozen=True)
class TurnSummarizer:
    """Summarize one agent turn for future retrieval."""

    def summarize(
        self,
        user_message: str,
        executions: list[ToolExecution],
        final_content: str,
        *,
        max_chars: int = 1200,
    ) -> str:
        raise NotImplementedError


class HeuristicSummarizer(TurnSummarizer):
    """Deterministic summary that avoids an extra LLM call."""

    def summarize(
        self,
        user_message: str,
        executions: list[ToolExecution],
        final_content: str,
        *,
        max_chars: int = 1200,
    ) -> str:
        lines = [
            f"User asked: {_compact(user_message, 240)}",
            f"Final response: {_compact(final_content, 600)}",
        ]
        if executions:
            lines.append(f"Tools: {len(executions)} call(s).")
            for execution in executions[:12]:
                outcome = "succeeded" if execution.success else f"failed ({execution.error or 'unknown_error'})"
                detail = _compact(execution.content, 200)
                lines.append(f"- {execution.call.name} {outcome}: {detail}")
            if len(executions) > 12:
                lines.append(f"- {len(executions) - 12} additional tool call(s) omitted from summary.")
        else:
            lines.append("Tools: none.")
        return _compact("\n".join(lines), max_chars)


class LLMSummarizer(TurnSummarizer):
    """Optional LLM-backed turn summarizer for deployments that opt into it."""

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider

    def summarize(
        self,
        user_message: str,
        executions: list[ToolExecution],
        final_content: str,
        *,
        max_chars: int = 1200,
    ) -> str:
        transcript = [f"User: {user_message}"]
        for execution in executions:
            transcript.append(
                f"Tool {execution.call.name} success={execution.success} error={execution.error}: "
                f"{_compact(execution.content, 1000)}"
            )
        transcript.append(f"Assistant: {final_content}")
        response = self.provider.generate(
            [
                ChatMessage(
                    role="system",
                    content=(
                        "Summarize this Kestrel turn for future memory retrieval. "
                        "Capture user intent, tool outcomes, validation signals, and unresolved risks."
                    ),
                ),
                ChatMessage(role="user", content="\n\n".join(transcript)),
            ],
            tools=[],
            options=LLMOptions(stream=False),
        )
        return _compact(response.content or HeuristicSummarizer().summarize(user_message, executions, final_content), max_chars)


def _compact(text: str, max_chars: int) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(max_chars - 18, 1)].rstrip() + " ...[truncated]"
