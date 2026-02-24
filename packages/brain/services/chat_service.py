import os
import json
import asyncio
from .base import BaseServicerMixin
from core.grpc_setup import brain_pb2
from core.config import logger
from core.prompts import KESTREL_DEFAULT_SYSTEM_PROMPT
from core import runtime

from db import get_pool, get_redis
from providers_registry import get_provider, CloudProvider, list_provider_configs, resolve_provider
from provider_config import ProviderConfig
from crud import get_messages, ensure_conversation, save_message

class ChatServicerMixin(BaseServicerMixin):
    async def ListModels(self, request, context):
        """List available models for a provider."""
        try:
            # 1. Get provider instance
            if request.provider == "local":
                # TODO: query local models
                models = [
                    {"id": "llama-3-8b-instruct", "name": "Llama 3 8B (Local)", "context_window": "8k"},
                    {"id": "mistral-7b-instruct", "name": "Mistral 7B (Local)", "context_window": "32k"},
                ]
                pb_models = [
                    brain_pb2.Model(id=m["id"], name=m["name"], context_window=m["context_window"])
                    for m in models
                ]
                return brain_pb2.ListModelsResponse(models=pb_models)
            
            provider = get_provider(request.provider)
            if not isinstance(provider, CloudProvider):
                    logger.error(f"Provider {request.provider} is not a CloudProvider")
                    return brain_pb2.ListModelsResponse(models=[])

            # 2. Resolve API Key
            api_key = request.api_key
            if not api_key and request.workspace_id:
                # Try to load from workspace config
                try:
                    pool = await get_pool()
                    ws_config = await ProviderConfig(pool).get_config(request.workspace_id)
                    # Check if this config is for the requested provider?
                    # ProviderConfig.get_config returns *resolved* config (merged with default)
                    # But we specifically want the key for the requested provider if it matches
                    # Actually get_config returns configuration for the *active* provider?
                    # No, let's look at provider_config.py...
                    # It seems get_config fetches "effective" config.
                    
                    # Better approach: Fetch specifically for this provider
                    # We can use list_provider_configs helper or check Redis
                    # Let's check list_provider_configs in server.py
                    configs = await list_provider_configs(request.workspace_id)
                    # configs is a list of records
                    for c in configs:
                        if c["provider"] == request.provider:
                            # Found config for this provider
                            encrypted = c.get("api_key_encrypted")
                            if encrypted and encrypted.startswith("provider_key:"):
                                r = await get_redis()
                                real_key = await r.get(encrypted)
                                if real_key:
                                    api_key = real_key.decode("utf-8")
                            elif encrypted:
                                from encryption import decrypt
                                api_key = decrypt(encrypted)
                            break
                except Exception as e:
                    logger.warning(f"Failed to resolve workspace key for ListModels: {e}")

            # 3. Fetch models
            model_list = await provider.list_models(api_key=api_key)
            
            models = []
            for m in model_list:
                models.append({
                    "id": m["id"],
                    "name": m["name"],
                    "context_window": m.get("context_window", "")
                })

            # 4. Convert to proto
            pb_models = [
                brain_pb2.Model(
                    id=m["id"],
                    name=m["name"],
                    context_window=m["context_window"]
                ) for m in models
            ]
            return brain_pb2.ListModelsResponse(models=pb_models)

        except Exception as e:
            logger.error(f"ListModels error: {e}", exc_info=True)
            return brain_pb2.ListModelsResponse(models=[])

    async def StreamChat(self, request, context):
        """Stream LLM responses back to the caller."""
        user_id = request.user_id
        workspace_id = request.workspace_id
        conversation_id = request.conversation_id

        logger.info(
            f"StreamChat: user={user_id}, workspace={workspace_id}, "
            f"msgs={len(request.messages)}"
        )

        try:
            # ── 1. Load workspace provider config ───────────────────
            pool = await get_pool()
            ws_config = await ProviderConfig(pool).get_config(workspace_id)
            
            # Resolve API Key from Redis if it's a reference
            api_key = ws_config.get("api_key", "")
            if api_key and api_key.startswith("provider_key:"):
                try:
                    r = await get_redis()
                    real_key = await r.get(api_key)
                    if real_key:
                        api_key = real_key.decode("utf-8")
                        logger.info(f"Resolved API key for {workspace_id} from Redis")
                    else:
                        logger.warning(f"API key reference {api_key} not found in Redis")
                        api_key = ""
                except Exception as e:
                    logger.error(f"Redis error resolving API key: {e}")
                    api_key = ""
            
            # DEBUG: Check if API key is present and looks valid
            api_key_status = "PRESENT" if api_key else "MISSING"
            key_debug = f"len={len(api_key)}, prefix={api_key[:4]}..." if api_key else "empty"
            logger.info(f"Loaded config for {workspace_id}: provider={ws_config.get('provider')}, api_key={api_key_status} ({key_debug})")

            # Request can override workspace defaults
            provider_name = request.provider or ws_config["provider"]
            model = request.model or ws_config["model"]

            provider = get_provider(provider_name)

            # Convert proto messages to dict format
            messages = []
            role_map = {0: "user", 1: "assistant", 2: "system", 3: "tool"}
            for msg in request.messages:
                messages.append({
                    "role": role_map.get(msg.role, "user"),
                    "content": msg.content,
                })

            # ── 1a. Load conversation history from database ──────────
            # The gateway only sends the latest user message. We need to
            # load the full conversation history so the LLM has context.
            conversation_id = request.conversation_id
            if conversation_id:
                try:
                    history = await get_messages(
                        request.user_id,
                        workspace_id,
                        conversation_id,
                    )
                    if history:
                        # Convert stored messages to the format the brain expects
                        history_messages = []
                        for h in history:
                            h_role = h["role"]
                            # Normalize role strings from DB
                            if h_role in ("user", "assistant", "system", "tool"):
                                history_messages.append({
                                    "role": h_role,
                                    "content": h["content"],
                                })
                        # Prepend history before the current user message(s)
                        # Limit to last 50 messages to avoid token overflow
                        if history_messages:
                            history_messages = history_messages[-50:]
                            messages = history_messages + messages
                            logger.info(f"Loaded {len(history_messages)} messages from conversation history")
                except Exception as hist_err:
                    logger.warning(f"Failed to load conversation history: {hist_err}")

            # Extract parameters (request overrides → workspace config → defaults)
            params = dict(request.parameters) if request.parameters else {}
            temperature = float(params.get("temperature", str(ws_config["temperature"])))
            max_tokens = int(params.get("max_tokens", str(ws_config["max_tokens"])))

            # ── 1b. Process file attachments ─────────────────────────
            attachment_parts = []  # For multimodal (images)
            attachment_text = []   # For text/code/PDF files
            if params.get("attachments"):
                try:
                    attachments = json.loads(params["attachments"])
                    import base64
                    import httpx as _httpx

                    for att in attachments:
                        mime = att.get("mimeType", "application/octet-stream")
                        file_url = att.get("url", "")
                        filename = att.get("filename", "file")

                        if not file_url:
                            continue

                        # Download the file (could be a local gateway URL or external)
                        try:
                            if file_url.startswith("/"):
                                # Local gateway file — construct full URL
                                gateway_url = os.environ.get("GATEWAY_URL", "http://gateway:8741")
                                file_url = f"{gateway_url}{file_url}"

                            async with _httpx.AsyncClient(timeout=30) as client:
                                resp = await client.get(file_url)
                                resp.raise_for_status()
                                file_bytes = resp.content
                        except Exception as dl_err:
                            logger.warning(f"Failed to download attachment {filename}: {dl_err}")
                            attachment_text.append(f"[Attachment: {filename} — failed to download]")
                            continue

                        if mime.startswith("image/"):
                            # Images → base64 for multimodal LLM
                            b64 = base64.b64encode(file_bytes).decode("utf-8")
                            attachment_parts.append({
                                "mime_type": mime,
                                "data": b64,
                                "filename": filename,
                            })
                            logger.info(f"Processed image attachment: {filename} ({len(file_bytes)} bytes)")
                        elif mime == "application/pdf":
                            # PDF → extract text
                            try:
                                import io
                                try:
                                    import pdfplumber
                                    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                                        text = "\n".join(
                                            page.extract_text() or "" for page in pdf.pages[:20]
                                        )
                                except ImportError:
                                    text = "[PDF text extraction unavailable — install pdfplumber]"
                                attachment_text.append(f"\n--- Attached PDF: {filename} ---\n{text[:8000]}\n--- End PDF ---\n")
                                logger.info(f"Extracted text from PDF: {filename}")
                            except Exception as pdf_err:
                                logger.warning(f"PDF extraction failed for {filename}: {pdf_err}")
                                attachment_text.append(f"[PDF: {filename} — extraction failed]")
                        else:
                            # Text / code files → read as UTF-8
                            try:
                                text = file_bytes.decode("utf-8", errors="replace")
                                attachment_text.append(f"\n--- Attached file: {filename} ---\n{text[:8000]}\n--- End file ---\n")
                                logger.info(f"Read text attachment: {filename} ({len(file_bytes)} bytes)")
                            except Exception:
                                attachment_text.append(f"[File: {filename} — could not read as text]")

                except json.JSONDecodeError:
                    logger.warning("Invalid attachments JSON in parameters")
                except Exception as att_err:
                    logger.warning(f"Attachment processing error: {att_err}")

            # Inject text attachments into the last user message content
            if attachment_text:
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i]["role"] == "user":
                        messages[i]["content"] += "\n" + "\n".join(attachment_text)
                        break

            # Tag the last user message with image attachments for the provider
            if attachment_parts:
                for i in range(len(messages) - 1, -1, -1):
                    if messages[i]["role"] == "user":
                        messages[i]["_attachments"] = attachment_parts
                        break

            # ── 2. System prompt + RAG context injection ────────────
            base_prompt = ws_config.get("system_prompt", "") or KESTREL_DEFAULT_SYSTEM_PROMPT

            if ws_config["rag_enabled"] and runtime.retrieval:
                user_msg = next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user"),
                    "",
                )
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

            # Always inject system prompt
            if messages and messages[0]["role"] == "system":
                messages[0]["content"] = base_prompt
            else:
                messages.insert(0, {"role": "system", "content": base_prompt})

            # Inject current date/time so the LLM knows the actual year
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc)
            time_block = f"\n\n## Current Date & Time\nToday is {now.strftime('%A, %B %d, %Y')} (UTC: {now.strftime('%Y-%m-%dT%H:%M:%SZ')}). Use this for all time-sensitive queries."
            messages[0]["content"] += time_block

            logger.info(f"Using provider={provider_name}, model={model}")

            # ── 3. Save user message before streaming ───────────────
            if conversation_id:
                # Ensure conversation row exists (external channels like
                # Telegram generate deterministic IDs without creating rows).
                channel_name = getattr(request, 'channel', '') or 'web'
                await ensure_conversation(
                    conversation_id, workspace_id,
                    channel=channel_name,
                )

                user_content = next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user"),
                    "",
                )
                if user_content:
                    await save_message(conversation_id, "user", user_content)

            # ── 4. Route through Agent Loop ──────────────────────────
            # Every chat message goes through the full agent loop so
            # Kestrel can autonomously plan, use tools, and reflect.
            from agent.loop import AgentLoop
            from agent.tools import build_tool_registry
            from agent.guardrails import Guardrails
            from agent.types import (
                AgentTask, GuardrailConfig as GCfg, TaskEventType, TaskStatus,
                TaskPlan, TaskStep, StepStatus,
            )

            user_content = next(
                (m["content"] for m in reversed(messages) if m["role"] == "user"),
                "",
            )

            # ── 4a. Intercept /slash commands ───────────────────────
            if runtime.command_parser and runtime.command_parser.is_command(user_content):
                cmd_context = {
                    "model": model,
                    "total_tokens": 0,
                    "cost_usd": 0,
                    "task_status": "idle",
                    "session_type": "main",
                }
                cmd_result = runtime.command_parser.parse(user_content, cmd_context)
                if cmd_result and cmd_result.handled:
                    # Send the command response directly (no agent needed)
                    yield brain_pb2.ChatResponse(
                        text=cmd_result.response,
                        done=False,
                    )
                    yield brain_pb2.ChatResponse(text="", done=True)
                    if conversation_id and cmd_result.response:
                        await save_message(conversation_id, "user", user_content)
                        await save_message(conversation_id, "assistant", cmd_result.response)
                    return

            # Read workspace guardrail settings from DB (user-configured via Settings UI)
            ws_guardrails = {}
            try:
                ws_row = await pool.fetchrow(
                    "SELECT settings FROM workspaces WHERE id = $1",
                    workspace_id,
                )
                if ws_row and ws_row["settings"]:
                    import json as _json
                    ws_settings = ws_row["settings"] if isinstance(ws_row["settings"], dict) else _json.loads(ws_row["settings"])
                    ws_guardrails = ws_settings.get("guardrails", {})
            except Exception as e:
                logger.warning(f"Failed to read workspace guardrails, using defaults: {e}")

            # Build a task with user-configured guardrails (or sensible defaults)
            chat_task = AgentTask(
                user_id=request.user_id,
                workspace_id=workspace_id,
                conversation_id=conversation_id,
                goal=user_content,
                config=GCfg(
                    max_iterations=ws_guardrails.get("maxIterations", 40),
                    max_tool_calls=ws_guardrails.get("maxToolCalls", 50),
                    max_tokens=ws_guardrails.get("maxTokens", 100_000),
                    max_wall_time_seconds=ws_guardrails.get("maxWallTime", 600),
                ),
            )

            # Classify request complexity — complex tasks get full planning,
            # simple conversational messages get a single-step shortcut.
            _COMPLEX_SIGNALS = [
                "audit", "analyze", "review", "build", "create", "deploy",
                "refactor", "debug", "investigate", "migrate", "implement",
                "design", "architect", "scan", "test", "benchmark",
                "compare", "evaluate", "research", "set up", "configure",
                "security", "performance", "optimize", "fix", "diagnose",
                "generate", "write", "plan", "multi", "step-by-step",
                "deep", "comprehensive", "full", "thorough", "complete",
            ]
            user_lower = user_content.lower()
            is_complex = (
                len(user_content.split()) > 12
                or any(sig in user_lower for sig in _COMPLEX_SIGNALS)
            )

            if is_complex:
                # Let the agent loop's TaskPlanner decompose this into
                # a multi-step plan (plan=None triggers planning phase)
                chat_task.plan = None
            else:
                # Simple conversational message — single step, fast response
                chat_task.plan = TaskPlan(
                    goal=user_content,
                    steps=[TaskStep(
                        index=0,
                        description=f"Respond to the user: {user_content[:100]}",
                        status=StepStatus.PENDING,
                    )],
                )

            # Build tool registry and agent loop
            tool_registry = build_tool_registry(hands_client=runtime.hands_client, vector_store=runtime.vector_store, pool=pool)

            # Set workspace context for Moltbook activity logging
            from agent.tools.moltbook import _current_workspace_id as _mwid
            import agent.tools.moltbook as _moltbook_mod
            _moltbook_mod._current_workspace_id = workspace_id

            # Set context for schedule tool (cron jobs)
            import agent.tools.schedule as _schedule_mod
            _schedule_mod._cron_scheduler = runtime.cron_scheduler
            _schedule_mod._current_workspace_id = workspace_id
            _schedule_mod._current_user_id = request.user_id

            # Create per-task evidence chain for auditable decision trail
            from agent.evidence import EvidenceChain
            evidence_chain = EvidenceChain(task_id=chat_task.id, pool=pool)

            # Create per-task learner for post-task lesson extraction
            from agent.learner import TaskLearner
            from agent.memory import WorkingMemory
            task_working_memory = WorkingMemory(
                redis_client=None,
                vector_store=runtime.vector_store,
            )
            task_learner = TaskLearner(
                provider=provider,
                model=model,
                working_memory=task_working_memory,
            )

            # ── Agent activity event queue ──────────────────────────
            # Council, Coordinator, and Reflection modules push events
            # here via callbacks. We now push directly to the output stream
            # so sub-agent events (e.g. delegate_task) appear in real-time.
            import asyncio as _asyncio
            output_queue = _asyncio.Queue()  # Unified output queue for ALL events
            _SENTINEL = object()  # Marks end of stream

            async def _activity_callback(activity_type: str, data: dict):
                """Push activity events directly to the output stream."""
                # Format as a visible chunk so it appears in chat immediately
                specialist = data.get("specialist", "")
                status = data.get("status", "")
                prefix = f"[{specialist}] " if specialist else ""

                if activity_type == "delegation_started":
                    text = f"\n🔀 **Delegating to {specialist}**: {data.get('goal', '')[:150]}\n\n"
                    await output_queue.put(self._make_response(chunk_type=0, content_delta=text))
                elif activity_type == "delegation_progress":
                    if status == "thinking":
                        text = f"💭 {prefix}*{data.get('thinking', '')[:120]}*\n"
                        await output_queue.put(self._make_response(chunk_type=0, content_delta=text))
                    elif status == "tool_calling":
                        tool = data.get("tool", "tool")
                        text = f"⚡ {prefix}Using **{tool}**...\n"
                        await output_queue.put(self._make_response(chunk_type=0, content_delta=text))
                    elif status == "tool_result":
                        tool = data.get("tool", "tool")
                        result_preview = (data.get("tool_result", "") or "")[:150].replace('\n', ' ')
                        text = f"✓ {prefix}{tool}: {result_preview}\n\n"
                        await output_queue.put(self._make_response(chunk_type=0, content_delta=text))
                    elif status == "step_done":
                        pass  # Don't duplicate step content
                elif activity_type == "delegation_complete":
                    status_icon = "✅" if data.get("status") == "complete" else "❌"
                    text = f"\n{status_icon} {prefix}Delegation complete\n\n"
                    await output_queue.put(self._make_response(chunk_type=0, content_delta=text))
                elif activity_type == "routing_info":
                    # Forward model routing info to the frontend
                    await output_queue.put(self._make_response(
                        chunk_type=0,
                        metadata={
                            "agent_status": "routing_info",
                            "provider": data.get("provider", ""),
                            "model": data.get("model", ""),
                            "was_escalated": str(data.get("was_escalated", False)).lower(),
                            "complexity": str(data.get("complexity", 0)),
                        },
                    ))
                else:
                    # Generic activity — send as metadata
                    await output_queue.put(self._make_response(
                        chunk_type=0,
                        metadata={
                            "agent_status": "agent_activity",
                            "activity": json.dumps({
                                "activity_type": activity_type, **data
                            }),
                        },
                    ))

            # Load workspace-specific dynamic skills into the tool registry
            if runtime.skill_manager:
                try:
                    skill_count = await runtime.skill_manager.load_workspace_skills(workspace_id)
                    if skill_count:
                        logger.info(f"Loaded {skill_count} custom skills for workspace")
                        await _activity_callback("skill_activated", {
                            "count": skill_count,
                            "message": f"{skill_count} workspace skills loaded",
                        })
                except Exception as e:
                    logger.warning(f"Failed to load workspace skills: {e}")

            # Create per-request checkpoint manager
            from agent.checkpoints import CheckpointManager
            checkpoint_mgr = CheckpointManager(pool=pool)

            agent_loop = AgentLoop(
                provider=provider,
                tool_registry=tool_registry,
                guardrails=Guardrails(),
                persistence=runtime.agent_persistence,
                model=model,
                api_key=api_key,
                memory_graph=runtime.memory_graph,
                learner=task_learner,
                evidence_chain=evidence_chain,
                checkpoint_manager=checkpoint_mgr,
                event_callback=_activity_callback,
                provider_resolver=resolve_provider,
            )

            # Inject persona context into the system prompt if available
            if runtime.persona_learner:
                try:
                    prefs = await runtime.persona_learner.load_persona(request.user_id)
                    if prefs:
                        persona_block = runtime.persona_learner.format_for_prompt(prefs)
                        if persona_block and messages:
                            # Find the system message and append persona context
                            for msg in messages:
                                if not isinstance(msg, dict):
                                    continue  # Only inject into dict messages
                                if msg.get("role") in ("system", 2):
                                    msg["content"] += "\n\n" + persona_block
                                    break
                except Exception as e:
                    logger.warning(f"Failed to inject persona context: {e}")

            # Inject installed MCP servers into the system prompt so the
            # planner knows about available external tools (GitHub, etc.)
            try:
                mcp_rows = await pool.fetch(
                    """SELECT name, description, server_url
                       FROM installed_tools
                       WHERE workspace_id = $1 AND enabled = true""",
                    workspace_id,
                )
                if mcp_rows:
                    mcp_block = "\n\n## Connected MCP Servers\n"
                    mcp_block += "You have access to these external tool servers via `mcp_call`. "
                    mcp_block += "Use `mcp_call(server_name=..., tool_name=..., arguments=...)` to invoke them.\n"
                    for r in mcp_rows:
                        mcp_block += f"\n- **{r['name']}**: {r['description'] or 'No description'} (command: `{r['server_url']}`)"
                    mcp_block += "\n\nFor GitHub repos, use `mcp_call` with the github server instead of trying to git clone (sandbox has no git/internet)."
                    if messages:
                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue  # Only inject into dict messages
                            if msg.get("role") in ("system", 2):
                                msg["content"] += mcp_block
                                break
            except Exception as e:
                logger.warning(f"Failed to inject MCP server context: {e}")

            # Override the agent's system prompt with our chat system prompt
            # by injecting messages into the task
            chat_task.messages = messages

            # Only skip planning for simple messages (we already set a plan).
            # Complex messages keep PLANNING status so the TaskPlanner runs.
            if chat_task.plan is not None:
                chat_task.status = TaskStatus.EXECUTING

            # Persist the chat task to the DB so FK constraints
            # (e.g. agent_approvals.task_id) are satisfied.
            await runtime.agent_persistence.save_task(chat_task)

            full_response_parts = []
            tool_results_gathered = []  # Accumulate tool results for task_failed fallback

            # ── Attach activity callback to agent sub-modules ──────

            # Attach callback to modules if available
            if hasattr(agent_loop, '_council') and agent_loop._council:
                agent_loop._council._event_callback = _activity_callback
            if hasattr(agent_loop, '_coordinator') and agent_loop._coordinator:
                agent_loop._coordinator._event_callback = _activity_callback
            if hasattr(agent_loop, '_reflection') and agent_loop._reflection:
                agent_loop._reflection._event_callback = _activity_callback

            async def _run_agent_loop():
                """Run agent loop in background, pushing events to output_queue."""
                try:
                    async for event in agent_loop.run(chat_task):
                        await output_queue.put(("agent_event", event))
                except Exception as e:
                    logger.error(f"Agent loop error in background: {e}", exc_info=True)
                    await output_queue.put(("error", str(e)))
                finally:
                    await output_queue.put(_SENTINEL)

            # Start the agent loop as a background task so activity callbacks
            # can push to the same queue concurrently
            agent_task_bg = _asyncio.create_task(_run_agent_loop())

            while True:
                item = await output_queue.get()

                # End of stream sentinel
                if item is _SENTINEL:
                    break

                # Direct response chunks from activity callbacks
                if isinstance(item, dict):
                    yield item
                    continue

                # Tuple from the agent loop background task
                if isinstance(item, tuple):
                    msg_type, payload = item

                    if msg_type == "error":
                        yield self._make_response(
                            chunk_type=3,
                            error_message=payload or "Agent task failed",
                        )
                        continue

                    if msg_type != "agent_event":
                        continue

                    event = payload
                    event_type = event.type.value if hasattr(event.type, "value") else str(event.type)

                    if event_type == "thinking":
                        # Agent is reasoning — send metadata for UI indicators
                        yield self._make_response(
                            chunk_type=0,
                            metadata={"agent_status": "thinking", "thinking": event.content[:200]},
                        )
                        # Also stream a visible thinking indicator so chat isn't blank
                        thinking_preview = (event.content or "")[:150].replace('\n', ' ')
                        if thinking_preview and not full_response_parts:
                            thinking_text = f"\n\n💭 *{thinking_preview}...*\n\n"
                            yield self._make_response(chunk_type=0, content_delta=thinking_text)

                    elif event_type == "tool_called":
                        # Agent is using a tool — send metadata for UI indicators
                        yield self._make_response(
                            chunk_type=0,
                            metadata={
                                "agent_status": "calling",
                                "tool_name": event.tool_name,
                                "tool_args": event.tool_args[:200] if event.tool_args else "",
                            },
                        )
                        # Stream visible tool activity so user sees progress
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
                        yield self._make_response(chunk_type=0, content_delta=tool_text)

                    elif event_type == "tool_result":
                        result_preview = (event.tool_result or "")[:300]
                        yield self._make_response(
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
                            yield self._make_response(chunk_type=0, content_delta=result_text)
                        if event.tool_result and len(event.tool_result) > 10:
                            tool_results_gathered.append(
                                f"**{event.tool_name}**: {event.tool_result[:500]}"
                            )

                    elif event_type == "step_complete":
                        if event.content:
                            words = event.content.split(' ')
                            for i, word in enumerate(words):
                                chunk = word if i == 0 else ' ' + word
                                yield self._make_response(chunk_type=0, content_delta=chunk)
                            full_response_parts.append(event.content)

                    elif event_type == "approval_needed":
                        question = event.content or "The agent needs your input."
                        approval_text = f"\n\n🤔 **I need your input:**\n\n{question}\n\n*Reply in the chat to continue.*"
                        words = approval_text.split(' ')
                        for i, word in enumerate(words):
                            chunk = word if i == 0 else ' ' + word
                            yield self._make_response(chunk_type=0, content_delta=chunk)
                        full_response_parts.append(approval_text)
                        yield self._make_response(
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
                                yield self._make_response(chunk_type=0, content_delta=chunk)
                            full_response_parts.append(event.content)

                    elif event_type == "task_failed":
                        if full_response_parts:
                            logger.info(f"Task budget exceeded but content was gathered, sending it")
                            combined = '\n'.join(full_response_parts)
                            combined += "\n\n*(Note: I ran out of processing steps but here's what I found.)*"
                            words = combined.split(' ')
                            for i, word in enumerate(words):
                                chunk = word if i == 0 else ' ' + word
                                yield self._make_response(chunk_type=0, content_delta=chunk)
                        elif tool_results_gathered:
                            logger.info(f"Task budget exceeded, summarizing {len(tool_results_gathered)} tool results")
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
                                        yield self._make_response(chunk_type=0, content_delta=chunk)

                                if summary_text:
                                    summary_text += "\n\n*(Note: I ran out of processing steps but here's what I found so far.)*"
                                    yield self._make_response(
                                        chunk_type=0,
                                        content_delta="\n\n*(Note: I ran out of processing steps but here's what I found so far.)*",
                                    )
                                    full_response_parts.append(summary_text)
                                else:
                                    yield self._make_response(
                                        chunk_type=3,
                                        error_message="Agent ran out of processing steps. Try a more specific request.",
                                    )
                            except Exception as summary_err:
                                logger.error(f"Failed to generate summary on task failure: {summary_err}")
                                yield self._make_response(
                                    chunk_type=3,
                                    error_message=event.content or "Agent task failed",
                                )
                        else:
                            yield self._make_response(
                                chunk_type=3,
                                error_message=event.content or "Agent task failed",
                            )

                        # Save whatever response we managed to produce
                        final_text = '\n'.join(full_response_parts) if full_response_parts else ""
                        if conversation_id and final_text:
                            await save_message(conversation_id, "assistant", final_text)

                        yield self._make_response(
                            chunk_type=2,  # DONE
                            metadata={"provider": provider_name, "model": model},
                        )
                        return

                    elif event_type == "plan_created":
                        yield self._make_response(
                            chunk_type=0,
                            metadata={"agent_status": "planning", "plan": event.content[:300] if event.content else ""},
                        )

            # Ensure background task is cleaned up
            if not agent_task_bg.done():
                agent_task_bg.cancel()

            # ── 5. Save response + auto-embed + persona observation ──
            full_response = "\n".join(full_response_parts) if full_response_parts else ""
            if conversation_id and full_response:
                await save_message(conversation_id, "assistant", full_response)

                # Auto-embed the Q&A pair for future RAG
                if ws_config["rag_enabled"] and runtime.embedding_pipeline:
                    await runtime.embedding_pipeline.embed_conversation_turn(
                        workspace_id=workspace_id,
                        conversation_id=conversation_id,
                        user_message=user_content,
                        assistant_response=full_response,
                    )

                # Observe communication patterns for persona learning
                if runtime.persona_learner and full_response:
                    try:
                        await runtime.persona_learner.observe_communication(
                            user_id=request.user_id,
                            user_message=user_content,
                            agent_response=full_response,
                        )
                        await runtime.persona_learner.observe_session_timing(
                            user_id=request.user_id,
                        )
                    except Exception as e:
                        logger.warning(f"Persona observation failed: {e}")

                # Update memory graph with conversation context (LLM extraction)
                if runtime.memory_graph and full_response:
                    try:
                        from agent.memory_graph import extract_entities_llm
                        _entities, _relations = await extract_entities_llm(
                            provider=provider,
                            model=model,
                            api_key=api_key,
                            user_message=user_content,
                            assistant_response=full_response,
                        )
                        if _entities:
                            await runtime.memory_graph.extract_and_store(
                                conversation_id=conversation_id,
                                workspace_id=workspace_id,
                                entities=_entities,
                                relations=_relations,
                            )
                            logger.info(f"Memory graph: stored {len(_entities)} entities, {len(_relations)} relations")
                    except Exception as e:
                        logger.warning(f"Memory graph update failed: {e}")

            # Send DONE
            yield self._make_response(
                chunk_type=2,  # DONE
                metadata={"provider": provider_name, "model": model},
            )

        except Exception as e:
            logger.error(f"StreamChat error: {e}", exc_info=True)
            yield self._make_response(
                chunk_type=3,  # ERROR
                error_message=str(e),
            )
