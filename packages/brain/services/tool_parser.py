import json
import logging

from core.grpc_setup import brain_pb2

logger = logging.getLogger("brain.services.tool_parser")


def _format_tool_result_preview(tool_result: str) -> str:
    """Create a compact, human-readable preview for tool results."""
    raw = (tool_result or "").strip()
    if not raw:
        return ""

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw[:200].replace("\n", " ")

    if isinstance(parsed, dict):
        if "success" in parsed:
            success_label = "success" if parsed.get("success") else "failed"
            error = parsed.get("error")
            if error:
                return f"{success_label}: {str(error)[:140]}"
            details = []
            for key in ("branch", "changed_files", "clean", "message"):
                if key in parsed:
                    details.append(f"{key}={parsed[key]}")
            if details:
                return f"{success_label} ({', '.join(details[:3])})"
            return success_label

        items = []
        for key in list(parsed.keys())[:4]:
            if key in ("display_markdown", "instruction"):
                continue
            value = str(parsed[key]).replace("\n", " ")
            items.append(f"{key}={value[:60]}")
        return ", ".join(items)[:200]

    if isinstance(parsed, list):
        return f"{len(parsed)} item(s)"

    return str(parsed)[:200]


def _build_failure_note(reason: str) -> str:
    """Build an accurate trailing note for incomplete tasks."""
    normalized = (reason or "").strip()
    if not normalized:
        return "*(Note: The task stopped before completion.)*"

    lowered = normalized.lower()
    if "iteration limit reached" in lowered or "running too long" in lowered:
        return "*(Note: The task hit its step limit before completion.)*"

    return f"*(Note: Task stopped before completion: {normalized[:160]})*"


def _extract_channel_and_flag(thinking_shown):
    """Support both legacy list state and channel-aware dict state."""
    channel = ""
    shown_flag = thinking_shown
    if isinstance(thinking_shown, dict):
        channel = str(thinking_shown.get("channel", "")).strip().lower()
        raw_flag = thinking_shown.get("shown")
        shown_flag = raw_flag if isinstance(raw_flag, list) else None
    return channel, shown_flag


async def parse_agent_event(
    item,
    full_response_parts,
    tool_results_gathered,
    provider,
    model,
    api_key,
    make_response_fn,
    thinking_shown=None,
):
    msg_type, payload = item
    channel, shown_flag = _extract_channel_and_flag(thinking_shown)
    verbose_channel = channel != "telegram"

    if msg_type == "error":
        yield make_response_fn(chunk_type=3, error_message=payload or "Agent task failed")
        return

    if msg_type != "agent_event":
        return

    event = payload
    event_type = event.type.value if hasattr(event.type, "value") else str(event.type)

    if event_type == "thinking":
        yield make_response_fn(
            chunk_type=0,
            metadata={"agent_status": "thinking", "thinking": event.content[:500]},
        )
        thinking_text = (event.content or "").strip()
        already_shown = shown_flag and shown_flag[0]
        if verbose_channel and thinking_text and not already_shown:
            formatted = (
                "\n\n<details><summary>\U0001f4ad Thinking...</summary>\n\n"
                f"{thinking_text}\n\n</details>\n\n"
            )
            yield make_response_fn(chunk_type=0, content_delta=formatted)
            full_response_parts.append(formatted)
            if shown_flag is not None:
                shown_flag[0] = True

    elif event_type == "tool_called":
        yield make_response_fn(
            chunk_type=0,
            metadata={
                "agent_status": "calling",
                "tool_name": event.tool_name or "",
                "tool_args": (event.tool_args or "")[:200],
            },
        )
        if verbose_channel:
            tool_display = event.tool_name or "tool"
            tool_text = f"\u26a1 Using **{tool_display}**..."
            if event.tool_args and len(event.tool_args) < 100:
                try:
                    args_preview = json.loads(event.tool_args)
                    if isinstance(args_preview, dict):
                        for key in (
                            "goal",
                            "query",
                            "content",
                            "command",
                            "server_name",
                            "url",
                            "specialist",
                        ):
                            if key in args_preview:
                                tool_text += f" `{args_preview[key][:80]}`"
                                break
                except (json.JSONDecodeError, TypeError):
                    pass
            tool_text += "\n"
            yield make_response_fn(chunk_type=0, content_delta=tool_text)

    elif event_type == "tool_result":
        result_preview = (event.tool_result or "")[:300]
        yield make_response_fn(
            chunk_type=0,
            metadata={
                "agent_status": "result",
                "tool_name": event.tool_name or "",
                "tool_result": result_preview,
            },
        )
        result_snippet = _format_tool_result_preview(event.tool_result or "")
        if verbose_channel and result_snippet:
            result_text = f"\u2713 {event.tool_name}: {result_snippet}\n\n"
            yield make_response_fn(chunk_type=0, content_delta=result_text)
        if event.tool_result and len(event.tool_result) > 10:
            tool_results_gathered.append(f"**{event.tool_name}**: {event.tool_result[:500]}")

    elif event_type == "step_complete":
        if event.content and verbose_channel:
            yield make_response_fn(chunk_type=0, content_delta=event.content)
            full_response_parts.append(event.content)

    elif event_type == "approval_needed":
        question = event.content or "The agent needs your input."

        if not event.approval_id:
            approval_text = (
                "\n\n\U0001f914 **I need your input:**\n\n"
                f"{question}\n\n*Reply in the chat to continue.*"
            )
        elif channel == "telegram":
            approval_text = ""
        else:
            approval_text = (
                "\n\n\U0001f6e1\ufe0f **Security Approval Required:**\n\n"
                f"{question}\n\n*Please use the Approve/Deny buttons in the Task Panel to proceed.*"
            )

        if approval_text:
            words = approval_text.split(" ")
            for i, word in enumerate(words):
                chunk = word if i == 0 else " " + word
                yield make_response_fn(chunk_type=0, content_delta=chunk)
            full_response_parts.append(approval_text)
        yield make_response_fn(
            chunk_type=0,
            metadata={
                "agent_status": "waiting_for_human",
                "approval_id": event.approval_id or "",
                "question": question[:300],
                "task_id": event.task_id or "",
            },
        )

    elif event_type == "task_complete":
        # Only emit task_complete content if nothing was streamed yet.
        # When the response was already streamed token-by-token via
        # step_complete events, full_response_parts will be non-empty
        # and we must NOT re-emit the same text.
        joined_response = "".join(full_response_parts)
        if not joined_response and event.content:
            words = event.content.split(" ")
            for i, word in enumerate(words):
                chunk = word if i == 0 else " " + word
                yield make_response_fn(chunk_type=0, content_delta=chunk)
            full_response_parts.append(event.content)

    elif event_type == "task_failed":
        failure_note = _build_failure_note(event.content or "")
        if full_response_parts:
            combined = "".join(full_response_parts)
            combined += f"\n\n{failure_note}"
            words = combined.split(" ")
            for i, word in enumerate(words):
                chunk = word if i == 0 else " " + word
                yield make_response_fn(chunk_type=0, content_delta=chunk)
        elif tool_results_gathered:
            try:
                summary_prompt = (
                    "A task stopped before completion. "
                    f"Failure reason: {event.content or 'unknown'}. "
                    "Summarize what was learned from these tool results so far. "
                    "Be helpful and concise:\n\n"
                    + "\n\n".join(tool_results_gathered[:10])
                )
                summary_msgs = [{"role": "user", "content": summary_prompt}]
                summary_text = ""
                async for chunk in provider.stream(summary_msgs, model=model, api_key=api_key):
                    if isinstance(chunk, str):
                        summary_text += chunk
                        yield make_response_fn(chunk_type=0, content_delta=chunk)

                if summary_text:
                    summary_text += f"\n\n{failure_note}"
                    yield make_response_fn(chunk_type=0, content_delta=f"\n\n{failure_note}")
                    full_response_parts.append(summary_text)
                else:
                    yield make_response_fn(chunk_type=0, content_delta=failure_note)
                    full_response_parts.append(failure_note)
            except Exception as summary_err:
                logger.warning(f"Failed to generate task failure summary: {summary_err}")
                yield make_response_fn(chunk_type=0, content_delta=failure_note)
                full_response_parts.append(failure_note)
        else:
            yield make_response_fn(chunk_type=0, content_delta=failure_note)
            full_response_parts.append(failure_note)

    elif event_type == "step_started":
        progress = event.progress or {}
        current = progress.get("current_step", 0)
        total = progress.get("total_steps", 0)
        description = (event.content or "Working on task...")[:150]
        step_label = f"Step {current + 1}/{total}" if total else "Working"
        yield make_response_fn(
            chunk_type=0,
            metadata={
                "agent_status": "step_started",
                "tool_name": step_label,
                "thinking": description,
            },
        )

    elif event_type == "plan_created":
        yield make_response_fn(
            chunk_type=0,
            metadata={"agent_status": "planning", "plan": event.content[:300] if event.content else ""},
        )
