
import json
import logging
from core.grpc_setup import brain_pb2

logger = logging.getLogger("brain.services.tool_parser")

async def parse_agent_event(item, full_response_parts, tool_results_gathered, provider, model, api_key, make_response_fn):
    msg_type, payload = item

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
            metadata={"agent_status": "thinking", "thinking": event.content[:200]},
        )
        thinking_preview = (event.content or "")[:150].replace('\n', ' ')
        if thinking_preview and not full_response_parts:
            thinking_text = f"\n\n💭 *{thinking_preview}...*\n\n"
            yield make_response_fn(chunk_type=0, content_delta=thinking_text)

    elif event_type == "tool_called":
        yield make_response_fn(
            chunk_type=0,
            metadata={
                "agent_status": "calling",
                "tool_name": event.tool_name,
                "tool_args": event.tool_args[:200] if event.tool_args else "",
            },
        )
        tool_display = event.tool_name or "tool"
        tool_text = f"⚡ Using **{tool_display}**..."
        if event.tool_args and len(event.tool_args) < 100:
            try:
                args_preview = json.loads(event.tool_args)
                if isinstance(args_preview, dict):
                    for key in ("goal", "query", "content", "command", "server_name", "url", "specialist"):
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
                "tool_name": event.tool_name,
                "tool_result": result_preview,
            },
        )
        result_snippet = (event.tool_result or "")[:200].replace('\n', ' ')
        if result_snippet:
            result_text = f"✓ {event.tool_name}: {result_snippet}\n\n"
            yield make_response_fn(chunk_type=0, content_delta=result_text)
        if event.tool_result and len(event.tool_result) > 10:
            tool_results_gathered.append(f"**{event.tool_name}**: {event.tool_result[:500]}")

    elif event_type == "step_complete":
        if event.content:
            words = event.content.split(' ')
            for i, word in enumerate(words):
                chunk = word if i == 0 else ' ' + word
                yield make_response_fn(chunk_type=0, content_delta=chunk)
            full_response_parts.append(event.content)

    elif event_type == "approval_needed":
        question = event.content or "The agent needs your input."
        approval_text = f"\n\n🤔 **I need your input:**\n\n{question}\n\n*Reply in the chat to continue.*"
        words = approval_text.split(' ')
        for i, word in enumerate(words):
            chunk = word if i == 0 else ' ' + word
            yield make_response_fn(chunk_type=0, content_delta=chunk)
        full_response_parts.append(approval_text)
        yield make_response_fn(
            chunk_type=0,
            metadata={
                "agent_status": "waiting_for_human",
                "approval_id": event.approval_id or "",
                "question": question[:300],
            },
        )

    elif event_type == "task_complete":
        if event.content and event.content not in '\n'.join(full_response_parts):
            words = event.content.split(' ')
            for i, word in enumerate(words):
                chunk = word if i == 0 else ' ' + word
                yield make_response_fn(chunk_type=0, content_delta=chunk)
            full_response_parts.append(event.content)

    elif event_type == "task_failed":
        if full_response_parts:
            combined = '\n'.join(full_response_parts)
            combined += "\n\n*(Note: I ran out of processing steps but here's what I found.)*"
            words = combined.split(' ')
            for i, word in enumerate(words):
                chunk = word if i == 0 else ' ' + word
                yield make_response_fn(chunk_type=0, content_delta=chunk)
        elif tool_results_gathered:
            try:
                summary_prompt = (
                    "You ran out of processing steps while working on a task. "
                    "Summarize what you found from these tool results so far. "
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
                    summary_text += "\n\n*(Note: I ran out of processing steps but here's what I found so far.)*"
                    yield make_response_fn(chunk_type=0, content_delta="\n\n*(Note: I ran out of processing steps but here's what I found so far.)*")
                    full_response_parts.append(summary_text)
                else:
                    yield make_response_fn(chunk_type=3, error_message="Agent ran out of processing steps.")
            except Exception as summary_err:
                yield make_response_fn(chunk_type=3, error_message=event.content or "Agent task failed")
        else:
            yield make_response_fn(chunk_type=3, error_message=event.content or "Agent task failed")
        
        yield make_response_fn(chunk_type=2, metadata={"provider": provider.name if hasattr(provider, 'name') else 'unknown', "model": model})

    elif event_type == "plan_created":
        yield make_response_fn(
            chunk_type=0,
            metadata={"agent_status": "planning", "plan": event.content[:300] if event.content else ""},
        )
