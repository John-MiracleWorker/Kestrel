import os
from pathlib import Path

source = Path("services/chat_service.py")
content = source.read_text(encoding="utf-8")

# Let's extract Context Builder
# We will create `services/context_builder.py`

context_builder_code = """
import os
import json
import logging
import base64
import httpx as _httpx
from db import get_pool, get_redis
from crud import get_messages

logger = logging.getLogger("brain.services.context")

async def build_chat_context(request, workspace_id, pool, r, runtime, provider_name, model, ws_config, api_key):
    # Convert proto messages to dict format
    messages = []
    role_map = {0: "user", 1: "assistant", 2: "system", 3: "tool"}
    for msg in request.messages:
        messages.append({
            "role": role_map.get(msg.role, "user"),
            "content": msg.content,
        })

    # 1a. Load conversation history
    conversation_id = request.conversation_id
    if conversation_id:
        try:
            history = await get_messages(
                request.user_id,
                workspace_id,
                conversation_id,
            )
            if history:
                history_messages = []
                for h in history:
                    h_role = h["role"]
                    if h_role in ("user", "assistant", "system", "tool"):
                        history_messages.append({
                            "role": h_role,
                            "content": h["content"],
                        })
                if history_messages:
                    history_messages = history_messages[-50:]
                    messages = history_messages + messages
                    logger.info(f"Loaded {len(history_messages)} messages from history")
        except Exception as hist_err:
            logger.warning(f"Failed to load conversation history: {hist_err}")

    # Extract params
    params = dict(request.parameters) if request.parameters else {}
    temperature = float(params.get("temperature", str(ws_config["temperature"])))
    max_tokens = int(params.get("max_tokens", str(ws_config["max_tokens"])))

    # 1b. Process attachments
    attachment_parts = []
    attachment_text = []
    if params.get("attachments"):
        try:
            attachments = json.loads(params["attachments"])
            for att in attachments:
                mime = att.get("mimeType", "application/octet-stream")
                file_url = att.get("url", "")
                filename = att.get("filename", "file")

                if not file_url:
                    continue

                try:
                    if file_url.startswith("/"):
                        gateway_url = os.environ.get("GATEWAY_URL", "http://gateway:8741")
                        file_url = f"{gateway_url}{file_url}"

                    async with _httpx.AsyncClient(timeout=30) as client:
                        resp = await client.get(file_url)
                        resp.raise_for_status()
                        file_bytes = resp.content
                except Exception as dl_err:
                    logger.warning(f"Failed to download attachment {filename}: {dl_err}")
                    attachment_text.append(f"[Attachment: {filename} - failed to download]")
                    continue

                if mime.startswith("image/"):
                    b64 = base64.b64encode(file_bytes).decode("utf-8")
                    attachment_parts.append({
                        "mime_type": mime,
                        "data": b64,
                        "filename": filename,
                    })
                elif mime == "application/pdf":
                    try:
                        import io
                        import pdfplumber
                        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                            text = "\\n".join(page.extract_text() or "" for page in pdf.pages[:20])
                        attachment_text.append(f"\\n--- Attached PDF: {filename} ---\\n{text[:8000]}\\n--- End PDF ---\\n")
                    except Exception as pdf_err:
                        attachment_text.append(f"[PDF: {filename} - extraction failed]")
                else:
                    try:
                        text = file_bytes.decode("utf-8", errors="replace")
                        attachment_text.append(f"\\n--- Attached file: {filename} ---\\n{text[:8000]}\\n--- End file ---\\n")
                    except Exception:
                        attachment_text.append(f"[File: {filename} - could not read]")

        except Exception as att_err:
            logger.warning(f"Attachment error: {att_err}")

    if attachment_text:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i]["content"] += "\\n" + "\\n".join(attachment_text)
                break

    if attachment_parts:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i]["_attachments"] = attachment_parts
                break

    # 2. System prompt + RAG
    base_prompt = ws_config.get("system_prompt", "") or KESTREL_DEFAULT_SYSTEM_PROMPT
    if ws_config["rag_enabled"] and runtime.retrieval:
        user_msg = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        if user_msg:
            augmented = await runtime.retrieval.build_augmented_prompt(
                workspace_id=workspace_id,
                user_message=user_msg,
                system_prompt=base_prompt,
                top_k=ws_config["rag_top_k"],
                min_similarity=ws_config["rag_min_similarity"],
            )
            if augmented:
                base_prompt = augmented

    if messages and messages[0]["role"] == "system":
        messages[0]["content"] = base_prompt
    else:
        messages.insert(0, {"role": "system", "content": base_prompt})

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    time_block = f"\\n\\n## Current Date & Time\\nToday is {now.strftime('%A, %B %d, %Y')} (UTC: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}). Use this for all time-sensitive queries."
    messages[0]["content"] += time_block

    return messages
"""

tool_parser_code = """
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
        thinking_preview = (event.content or "")[:150].replace('\\n', ' ')
        if thinking_preview and not full_response_parts:
            thinking_text = f"\\n\\n💭 *{thinking_preview}...*\\n\\n"
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
        tool_text += "\\n"
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
        result_snippet = (event.tool_result or "")[:200].replace('\\n', ' ')
        if result_snippet:
            result_text = f"✓ {event.tool_name}: {result_snippet}\\n\\n"
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
        approval_text = f"\\n\\n🤔 **I need your input:**\\n\\n{question}\\n\\n*Reply in the chat to continue.*"
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
        if event.content and event.content not in '\\n'.join(full_response_parts):
            words = event.content.split(' ')
            for i, word in enumerate(words):
                chunk = word if i == 0 else ' ' + word
                yield make_response_fn(chunk_type=0, content_delta=chunk)
            full_response_parts.append(event.content)

    elif event_type == "task_failed":
        if full_response_parts:
            combined = '\\n'.join(full_response_parts)
            combined += "\\n\\n*(Note: I ran out of processing steps but here's what I found.)*"
            words = combined.split(' ')
            for i, word in enumerate(words):
                chunk = word if i == 0 else ' ' + word
                yield make_response_fn(chunk_type=0, content_delta=chunk)
        elif tool_results_gathered:
            try:
                summary_prompt = (
                    "You ran out of processing steps while working on a task. "
                    "Summarize what you found from these tool results so far. "
                    "Be helpful and concise:\\n\\n"
                    + "\\n\\n".join(tool_results_gathered[:10])
                )
                summary_msgs = [{"role": "user", "content": summary_prompt}]
                summary_text = ""
                async for chunk in provider.stream(summary_msgs, model=model, api_key=api_key):
                    if isinstance(chunk, str):
                        summary_text += chunk
                        yield make_response_fn(chunk_type=0, content_delta=chunk)

                if summary_text:
                    summary_text += "\\n\\n*(Note: I ran out of processing steps but here's what I found so far.)*"
                    yield make_response_fn(chunk_type=0, content_delta="\\n\\n*(Note: I ran out of processing steps but here's what I found so far.)*")
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
"""

Path("services/context_builder.py").write_text(context_builder_code)
Path("services/tool_parser.py").write_text(tool_parser_code)
print("Files split done.")
