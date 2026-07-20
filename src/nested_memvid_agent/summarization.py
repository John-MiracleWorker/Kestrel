from __future__ import annotations

from dataclasses import dataclass

from .llm.base import LLMProvider
from .runtime_models import ChatMessage, LLMOptions, ToolExecution

RETRIEVAL_DERIVED_TOOLS = frozenset(
    {
        "context.expand",
        "context.pack",
        "memory.conflicts",
        "memory.export",
        "memory.inspect",
        "memory.ledger",
        "memory.search",
    }
)


def is_retrieval_derived_tool(tool_name: str) -> bool:
    """Return whether a tool output is copied from Kestrel's memory substrate."""

    return tool_name in RETRIEVAL_DERIVED_TOOLS


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
        safe_final_content = _summary_final_content(executions, final_content)
        lines = [
            f"User asked: {_compact(user_message, 240)}",
            f"Final response: {_compact(safe_final_content, 600)}",
        ]
        if executions:
            lines.append(f"Tools: {len(executions)} call(s).")
            for execution in executions[:12]:
                outcome = "succeeded" if execution.success else f"failed ({execution.error or 'unknown_error'})"
                detail = _summary_execution_detail(execution)
                lines.append(f"- {execution.call.name} {outcome}: {detail}")
            if len(executions) > 12:
                lines.append(f"- {len(executions) - 12} additional tool call(s) omitted from summary.")
        else:
            lines.append("Tools: none.")
        return _compact("\n".join(lines), max_chars)


class LLMSummarizer(TurnSummarizer):
    """Optional LLM-backed turn summarizer for deployments that opt into it."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        options: LLMOptions | None = None,
        fallback: TurnSummarizer | None = None,
    ) -> None:
        self.provider = provider
        self.options = options or LLMOptions(stream=False)
        self.fallback = fallback or HeuristicSummarizer()

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
                f"{_summary_execution_detail(execution, max_chars=1000)}"
            )
        transcript.append(f"Assistant: {_summary_final_content(executions, final_content)}")
        fallback_summary = self.fallback.summarize(
            user_message,
            executions,
            final_content,
            max_chars=max_chars,
        )
        try:
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
                options=self.options,
            )
        except Exception:  # noqa: BLE001 - optional summarization must not fail the completed turn
            return fallback_summary
        return _compact(response.content or fallback_summary, max_chars)


def _summary_execution_detail(execution: ToolExecution, *, max_chars: int = 200) -> str:
    if is_retrieval_derived_tool(execution.call.name):
        return (
            "retrieval-derived output omitted from durable summary "
            f"(output_chars={len(execution.content)})"
        )
    return _compact(execution.content, max_chars)


def _summary_final_content(
    executions: list[ToolExecution],
    final_content: str,
) -> str:
    normalized_final = final_content.strip()
    for execution in executions:
        if not is_retrieval_derived_tool(execution.call.name):
            continue
        if normalized_final and normalized_final == execution.content.strip():
            return (
                "Retrieval result returned directly; copied memory content omitted from "
                "durable summary."
            )
    return final_content


def _compact(text: str, max_chars: int) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max(max_chars - 18, 1)].rstrip() + " ...[truncated]"
