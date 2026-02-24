
import os
import json
import logging
import base64
import httpx as _httpx
from core.prompts import KESTREL_DEFAULT_SYSTEM_PROMPT
from db import get_pool
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
                            text = "\n".join(page.extract_text() or "" for page in pdf.pages[:20])
                        attachment_text.append(f"\n--- Attached PDF: {filename} ---\n{text[:8000]}\n--- End PDF ---\n")
                    except Exception as pdf_err:
                        attachment_text.append(f"[PDF: {filename} - extraction failed]")
                else:
                    try:
                        text = file_bytes.decode("utf-8", errors="replace")
                        attachment_text.append(f"\n--- Attached file: {filename} ---\n{text[:8000]}\n--- End file ---\n")
                    except Exception:
                        attachment_text.append(f"[File: {filename} - could not read]")

        except Exception as att_err:
            logger.warning(f"Attachment error: {att_err}")

    if attachment_text:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "user":
                messages[i]["content"] += "\n" + "\n".join(attachment_text)
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
    time_block = f"\n\n## Current Date & Time\nToday is {now.strftime('%A, %B %d, %Y')} (UTC: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}). Use this for all time-sensitive queries."
    messages[0]["content"] += time_block

    return messages
