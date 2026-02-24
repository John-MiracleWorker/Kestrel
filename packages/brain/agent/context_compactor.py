from __future__ import annotations
"""
Context compactor — manages message context to fit within model limits.

When the token count of a conversation approaches the provider's context
window, the compactor summarizes older messages into a single condensed
"memory" block, preserving recent context and critical information.

If compaction alone isn't enough (e.g., a single step has a huge context),
it signals that the step should be escalated to a larger model.
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger("brain.agent.context_compactor")

# Rough token estimate: ~4 chars per token for English
_CHARS_PER_TOKEN = 4

# How many recent messages to ALWAYS preserve (never compacted)
_PRESERVE_RECENT = int(os.getenv("COMPACTOR_PRESERVE_RECENT", "6"))

# Compact when usage exceeds this fraction of the context limit
_COMPACT_THRESHOLD = float(os.getenv("COMPACTOR_THRESHOLD", "0.75"))

# Provider context limits (tokens)
CONTEXT_LIMITS = {
    "ollama": int(os.getenv("OLLAMA_CONTEXT_LENGTH", "16384")),
    "local": 4096,
    "google": 1_000_000,
    "openai": 128_000,
    "anthropic": 200_000,
}


def estimate_tokens(messages: list[dict]) -> int:
    """Rough token estimate for a message list (no tokenizer needed)."""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Multimodal messages
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(part.get("text", ""))
        # Tool call arguments
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            total_chars += len(fn.get("arguments", ""))
            total_chars += len(fn.get("name", ""))
        # Role and overhead
        total_chars += len(msg.get("role", "")) + 10
    return total_chars // _CHARS_PER_TOKEN


def _build_summary_prompt(messages_to_summarize: list[dict]) -> str:
    """Build a prompt asking to summarize older conversation context."""
    lines = []
    for msg in messages_to_summarize:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        if isinstance(content, str) and content.strip():
            # Truncate very long tool results
            if role == "tool" and len(content) > 500:
                content = content[:500] + "... [truncated]"
            lines.append(f"[{role}] {content[:300]}")
        # Summarize tool calls
        for tc in msg.get("tool_calls", []):
            fn = tc.get("function", {})
            lines.append(f"[tool_call] {fn.get('name', '?')}({fn.get('arguments', '')[:100]})")

    conversation_block = "\n".join(lines[-40:])  # Cap at 40 most recent lines to summarize

    return f"""Summarize the following conversation context into a concise memory block.
Preserve:
- Key decisions made
- Important findings and facts
- Tool results that matter
- Current task objective and progress

Be extremely concise — aim for 200-400 words maximum. Use bullet points.

CONVERSATION TO SUMMARIZE:
{conversation_block}

CONCISE SUMMARY:"""


async def compact_context(
    messages: list[dict],
    provider_name: str,
    provider=None,
    model: str = "",
) -> tuple[list[dict], bool]:
    """
    Compact messages if they're approaching the context limit.

    Returns:
        (compacted_messages, was_compacted)
        - compacted_messages: the potentially shortened message list
        - was_compacted: True if compaction was performed

    Strategy:
        1. Estimate token usage
        2. If below threshold → return unchanged
        3. Split into [system, older_messages, recent_messages]
        4. Summarize older_messages into a single assistant "memory" message
        5. Return [system, memory_summary, recent_messages]
    """
    context_limit = CONTEXT_LIMITS.get(provider_name, 128_000)
    token_estimate = estimate_tokens(messages)
    threshold = int(context_limit * _COMPACT_THRESHOLD)

    if token_estimate < threshold:
        return messages, False

    logger.info(
        f"Context compaction triggered: ~{token_estimate} tokens "
        f"(limit={context_limit}, threshold={threshold})"
    )

    # Split messages: system message(s), older, recent
    system_messages = []
    conversation_messages = []

    for msg in messages:
        if msg.get("role") == "system":
            system_messages.append(msg)
        else:
            conversation_messages.append(msg)

    # Always preserve the most recent N messages
    if len(conversation_messages) <= _PRESERVE_RECENT:
        # Not enough messages to compact — can't help here
        logger.warning("Not enough messages to compact, context still too large")
        return messages, False

    older = conversation_messages[:-_PRESERVE_RECENT]
    recent = conversation_messages[-_PRESERVE_RECENT:]

    # Generate summary using the provider itself (if available)
    summary_text = None
    if provider:
        try:
            summary_prompt = _build_summary_prompt(older)
            summary_messages = [
                {"role": "system", "content": "You are a concise context summarizer."},
                {"role": "user", "content": summary_prompt},
            ]

            if hasattr(provider, "generate"):
                summary_text = await provider.generate(
                    messages=summary_messages,
                    model=model,
                    temperature=0.1,
                    max_tokens=512,
                )
        except Exception as e:
            logger.warning(f"LLM-based compaction failed, using extractive fallback: {e}")

    # Fallback: extractive summary (just keep key messages)
    if not summary_text:
        key_lines = []
        for msg in older:
            content = msg.get("content", "")
            role = msg.get("role", "")
            if isinstance(content, str) and content.strip():
                if role == "user":
                    key_lines.append(f"• User: {content[:150]}")
                elif role == "assistant" and len(content) > 30:
                    key_lines.append(f"• Assistant: {content[:150]}")
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                key_lines.append(f"• Tool: {fn.get('name', '?')}()")
        summary_text = "COMPACTED CONTEXT (earlier conversation):\n" + "\n".join(key_lines[-20:])

    # Build compacted message list
    memory_message = {
        "role": "assistant",
        "content": f"[Context Summary — earlier conversation compacted to save context]\n\n{summary_text}",
    }

    compacted = system_messages + [memory_message] + recent

    new_estimate = estimate_tokens(compacted)
    logger.info(
        f"Compaction complete: {token_estimate} → {new_estimate} tokens "
        f"(saved ~{token_estimate - new_estimate} tokens, "
        f"removed {len(older)} messages)"
    )

    return compacted, True


def needs_escalation(
    messages: list[dict],
    provider_name: str,
) -> bool:
    """
    Check if messages are STILL too large for the provider after compaction.
    This signals the caller should escalate to a larger-context provider.
    """
    context_limit = CONTEXT_LIMITS.get(provider_name, 128_000)
    token_estimate = estimate_tokens(messages)

    # If we're still over 90% of the limit after compaction, escalate
    if token_estimate > int(context_limit * 0.9):
        logger.warning(
            f"Context still too large after compaction: ~{token_estimate} tokens "
            f"(limit={context_limit}) — recommending escalation to cloud"
        )
        return True
    return False
