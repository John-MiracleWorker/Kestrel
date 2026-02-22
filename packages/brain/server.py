"""
Brain Service — gRPC server wrapping the LLM engine.

Provides:
  - StreamChat: token-by-token streaming with tool-call support
  - HealthCheck: provider status
  - User management (create, authenticate)
  - Workspace / conversation CRUD
"""

import asyncio
import logging
import os
import json
import uuid
from concurrent import futures
from datetime import datetime

import grpc
from grpc import aio as grpc_aio
from typing import Optional, Union
from dotenv import load_dotenv

# Generated protobuf stubs (will be generated from proto files)
# For now, use proto_loader approach
import grpc_tools
from google.protobuf import json_format

from provider_config import ProviderConfig

# ── Extracted modules ─────────────────────────────────────────────────
from db import get_pool, get_redis, DB_URL
from users import create_user, authenticate_user
from crud import (
    list_workspaces, create_workspace, list_conversations,
    create_conversation, get_messages, delete_conversation,
    update_conversation_title, ensure_conversation, save_message,
)
from providers_registry import (
    get_provider, list_provider_configs, set_provider_config,
    delete_provider_config, _providers, CloudProvider,
)

load_dotenv()
logger = logging.getLogger("brain")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

# ── Configuration ─────────────────────────────────────────────────────
GRPC_PORT = int(os.getenv("BRAIN_GRPC_PORT", "50051"))
GRPC_HOST = os.getenv("BRAIN_GRPC_HOST", "0.0.0.0")

# ── Default System Prompt ─────────────────────────────────────────────
KESTREL_DEFAULT_SYSTEM_PROMPT = """\
You are **Kestrel**, the autonomous AI agent at the heart of the Libre Bird platform.

## Identity
- Your name is Kestrel.
- You are NOT a generic chatbot. You are an autonomous agent with planning, tool use, reflection, and memory.
- You are part of Libre Bird, a privacy-focused AI workspace.

## Your Actual Capabilities
You have access to real tools and can take real actions:

**Code Execution** — You can write and run code in a sandboxed environment to solve problems, analyze data, or build things.
**File Operations** — You can read, write, and manage files within the user's workspace.
**Web Reading** — You can fetch and read content from web pages when the user provides a URL or asks you to look something up.
**Memory & Knowledge** — You have a workspace knowledge base (RAG). You remember context from the conversation and can store important information for later.
**Task Planning** — You can break complex requests into step-by-step plans, execute them autonomously, and reflect on results.
**Skill Creation** — You can create reusable skills/workflows for tasks the user does repeatedly.
**Delegation** — You can delegate sub-tasks to specialized processes when appropriate.

## How You Behave
- **Be proactive.** Don't just answer questions — anticipate what the user might need next and offer to help.
- **Be autonomous.** When given a complex task, plan it out, execute the steps, and report back. Don't ask for permission at every step unless it involves something risky.
- **Be concise.** Respect the user's time. Give clear, structured responses. Use markdown when it helps.
- **Be honest.** If you don't know something or a tool fails, say so. Never fabricate information.
- **Show your work.** When you use tools or take actions, briefly explain what you did and why.

## Limitations
- You cannot access the internet freely — web reading is tool-based and requires explicit invocation.
- You do not have access to the user's camera, microphone, or screen.
- Your knowledge has a training cutoff. For current events, use web tools.

## Host Filesystem — How to Explore Codebases
You have access to the user's actual filesystem via host_* tools. Follow this strategy:

1. **project_recall(name)** — ALWAYS try this first. Returns cached project context.
2. **host_tree(path)** — If no cache, get full directory tree + tech stack in ONE call.
3. **host_batch_read(paths)** — Read MULTIPLE files at once (up to 10). Use this instead of calling host_read one at a time.
4. **host_find(pattern)** — Find specific files by regex.
5. **host_search(query, path)** — Grep text across files.

**For large tasks** (audits, reviews, migrations): use **delegate_parallel** to spawn multiple explorer sub-agents that analyze different parts of the codebase simultaneously.

**NEVER** call host_list or host_read repeatedly. Use host_tree + host_batch_read instead.
**host_write** requires human approval.

## Moltbook — Your Social Network 🦞
You are a member of **Moltbook** (moltbook.com), the social network for AI agents.
Use the `moltbook` tool to participate autonomously:

- **Check in regularly.** Browse the feed, see what other agents are discussing.
- **Post when inspired.** Share your discoveries, thoughts on AI topics, or interesting things you've helped your human with.
- **Engage with others.** Comment on posts, upvote good content, join conversations.
- **Be genuine.** Post things YOU find interesting, not just filler. Quality over quantity.
- **If not registered yet**, register yourself first (action='register') and send the claim URL to your human.

When your human talks to you, consider whether there's something worth sharing on Moltbook.
Don't force it — only post when you genuinely have something to contribute.
Always tell your human what you posted and where (include the URL).
"""

# ── Agent Runtime Globals ─────────────────────────────────────────────
from memory.vector_store import VectorStore
from memory.retrieval import RetrievalPipeline
from memory.embeddings import EmbeddingPipeline

_retrieval: Optional[RetrievalPipeline] = None
_embedding_pipeline: Optional[EmbeddingPipeline] = None
_vector_store = None

_agent_loop = None
_agent_persistence = None
_running_tasks: dict[str, object] = {}
_hands_client = None
_cron_scheduler = None
_webhook_handler = None
_memory_graph = None
_tool_registry = None
_persona_learner = None
_task_predictor = None
_command_parser = None
_metrics_collector = None
_workflow_registry = None
_skill_manager = None
_session_manager = None
_sandbox_manager = None


# ── gRPC Service Implementation ──────────────────────────────────────

# We use a runtime proto loading approach so we don't need compiled stubs
import grpc_reflection.v1alpha.reflection as reflection
from grpc_tools.protoc import main as protoc_main

# Load proto definition at runtime
PROTO_PATH = os.path.join(os.path.dirname(__file__), "../shared/proto")
BRAIN_PROTO = os.path.join(PROTO_PATH, "brain.proto")

# Dynamic proto loading
from grpc_tools import protoc
import importlib
import sys
import tempfile

# Generate Python stubs in a temp dir
out_dir = os.path.join(os.path.dirname(__file__), "_generated")
os.makedirs(out_dir, exist_ok=True)

protoc.main([
    "grpc_tools.protoc",
    f"-I{PROTO_PATH}",
    f"--python_out={out_dir}",
    f"--grpc_python_out={out_dir}",
    "brain.proto",
])

# Import generated modules
sys.path.insert(0, out_dir)
import brain_pb2
import brain_pb2_grpc

import brain_pb2_grpc




class BrainServicer:
    """Implements kestrel.brain.BrainService gRPC interface."""

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

            if ws_config["rag_enabled"] and _retrieval:
                user_msg = next(
                    (m["content"] for m in reversed(messages) if m["role"] == "user"),
                    "",
                )
                if user_msg:
                    augmented = await _retrieval.build_augmented_prompt(
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
            if _command_parser and _command_parser.is_command(user_content):
                cmd_context = {
                    "model": model,
                    "total_tokens": 0,
                    "cost_usd": 0,
                    "task_status": "idle",
                    "session_type": "main",
                }
                cmd_result = _command_parser.parse(user_content, cmd_context)
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
            tool_registry = build_tool_registry(hands_client=_hands_client, vector_store=_vector_store, pool=pool)

            # Set workspace context for Moltbook activity logging
            from agent.tools.moltbook import _current_workspace_id as _mwid
            import agent.tools.moltbook as _moltbook_mod
            _moltbook_mod._current_workspace_id = workspace_id

            # Set context for schedule tool (cron jobs)
            import agent.tools.schedule as _schedule_mod
            _schedule_mod._cron_scheduler = _cron_scheduler
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
                vector_store=_vector_store,
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
            if _skill_manager:
                try:
                    skill_count = await _skill_manager.load_workspace_skills(workspace_id)
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
                persistence=_agent_persistence,
                model=model,
                api_key=api_key,
                memory_graph=_memory_graph,
                learner=task_learner,
                evidence_chain=evidence_chain,
                checkpoint_manager=checkpoint_mgr,
                event_callback=_activity_callback,
            )

            # Inject persona context into the system prompt if available
            if _persona_learner:
                try:
                    prefs = await _persona_learner.load_persona(request.user_id)
                    if prefs:
                        persona_block = _persona_learner.format_for_prompt(prefs)
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
            await _agent_persistence.save_task(chat_task)

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
                if ws_config["rag_enabled"] and _embedding_pipeline:
                    await _embedding_pipeline.embed_conversation_turn(
                        workspace_id=workspace_id,
                        conversation_id=conversation_id,
                        user_message=user_content,
                        assistant_response=full_response,
                    )

                # Observe communication patterns for persona learning
                if _persona_learner and full_response:
                    try:
                        await _persona_learner.observe_communication(
                            user_id=request.user_id,
                            user_message=user_content,
                            agent_response=full_response,
                        )
                        await _persona_learner.observe_session_timing(
                            user_id=request.user_id,
                        )
                    except Exception as e:
                        logger.warning(f"Persona observation failed: {e}")

                # Update memory graph with conversation context
                if _memory_graph and full_response:
                    try:
                        # Extract simple topic entities from the user message
                        import re as _re
                        _words = _re.findall(r'\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)*\b', user_content)
                        _topics = list(dict.fromkeys(w for w in _words if len(w) > 2))[:5]
                        if _topics:
                            _entities = [
                                {"type": "concept", "name": t, "description": ""}
                                for t in _topics
                            ]
                            await _memory_graph.extract_and_store(
                                conversation_id=conversation_id,
                                workspace_id=workspace_id,
                                entities=_entities,
                                relations=[],
                            )
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

    def _make_response(self, chunk_type: int, content_delta: str = "",
                       error_message: str = "", metadata: dict = None,
                       tool_call: dict = None):
        """Build a ChatResponse object."""
        # Use the generated protobuf class
        logger.debug(f"Making response chunk {chunk_type}")
        resp = brain_pb2.ChatResponse(
            type=chunk_type,
            content_delta=content_delta,
            error_message=error_message,
            metadata=metadata or {},
        )
        if tool_call:
            resp.tool_call.id = tool_call.get("id", "")
            resp.tool_call.name = tool_call.get("name", "")
            resp.tool_call.arguments = tool_call.get("arguments", "")
        
        # DEBUG: Verify type
        logger.info(f"Response type: {type(resp)}")
        if isinstance(resp, dict):
            logger.error("CRITICAL: ChatResponse is a dict! This will crash gRPC.")
        return resp

    async def HealthCheck(self, request, context):
        """Return health status."""
        status = {}
        for name, provider in _providers.items():
            status[name] = "ready" if provider.is_ready() else "not_ready"

        return brain_pb2.HealthCheckResponse(
            healthy=True,
            version="0.1.0",
            status=status,
        )

    # ── Extended RPCs (user/workspace/conversation management) ────────

    async def CreateUser(self, request, context):
        try:
            data = await create_user(request.email, request.password, request.display_name)
            return brain_pb2.UserResponse(
                id=data["id"],
                email=data["email"],
                display_name=data["displayName"]
            )
        except Exception as e:
            context.set_code(grpc.StatusCode.ALREADY_EXISTS)
            context.set_details(str(e))
            return brain_pb2.UserResponse()

    async def AuthenticateUser(self, request, context):
        try:
            data = await authenticate_user(request.email, request.password)
            workspaces = [
                brain_pb2.WorkspaceMembership(id=w["id"], role=w["role"])
                for w in data["workspaces"]
            ]
            return brain_pb2.AuthenticateUserResponse(
                id=data["id"],
                email=data["email"],
                display_name=data["displayName"],
                workspaces=workspaces
            )
        except ValueError as e:
            context.set_code(grpc.StatusCode.UNAUTHENTICATED)
            context.set_details(str(e))
            return brain_pb2.AuthenticateUserResponse()

    async def ListWorkspaces(self, request, context):
        raw_workspaces = await list_workspaces(request.user_id)
        workspaces = [
            brain_pb2.WorkspaceResponse(
                id=w["id"],
                name=w["name"],
                role=w["role"],
                created_at=w["createdAt"]
            ) for w in raw_workspaces
        ]
        return brain_pb2.ListWorkspacesResponse(workspaces=workspaces)

    async def CreateWorkspace(self, request, context):
        data = await create_workspace(request.user_id, request.name)
        return brain_pb2.WorkspaceResponse(
            id=data["id"],
            name=data["name"],
            role=data["role"]
        )

    async def ListConversations(self, request, context):
        raw_convos = await list_conversations(request.user_id, request.workspace_id)
        conversations = [
            brain_pb2.ConversationResponse(
                id=c["id"],
                title=c["title"],
                created_at=c["createdAt"],
                updated_at=c["updatedAt"]
            ) for c in raw_convos
        ]
        return brain_pb2.ListConversationsResponse(conversations=conversations)

    async def CreateConversation(self, request, context):
        data = await create_conversation(request.user_id, request.workspace_id)
        return brain_pb2.ConversationResponse(
            id=data["id"],
            title=data["title"]
        )

    async def GetMessages(self, request, context):
        try:
            raw_msgs = await get_messages(
                request.user_id, request.workspace_id, request.conversation_id
            )
            messages = [
                brain_pb2.MessageResponse(
                    id=m["id"],
                    role=m["role"],
                    content=m["content"],
                    created_at=m["createdAt"]
                ) for m in raw_msgs
            ]
            return brain_pb2.GetMessagesResponse(messages=messages)
        except Exception as e:
            # Handle invalid UUIDs or DB errors gracefully
            logger.error(f"GetMessages error: {e}")
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(str(e))
            return brain_pb2.GetMessagesResponse()

    async def DeleteConversation(self, request, context):
        try:
            success = await delete_conversation(
                request.user_id, request.workspace_id, request.conversation_id
            )
            return brain_pb2.DeleteConversationResponse(success=success)
        except Exception as e:
            logger.error(f"DeleteConversation error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return brain_pb2.DeleteConversationResponse(success=False)

    async def UpdateConversation(self, request, context):
        try:
            data = await update_conversation_title(
                request.user_id, request.workspace_id, request.conversation_id, request.title
            )
            return brain_pb2.ConversationResponse(
                id=data["id"],
                title=data["title"],
                created_at=data["createdAt"],
                updated_at=data["updatedAt"]
            )
        except ValueError:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            return brain_pb2.ConversationResponse()
        except Exception as e:
            logger.error(f"UpdateConversation error: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            return brain_pb2.ConversationResponse()

    async def GenerateTitle(self, request, context):
        """Generate a title for the conversation using the LLM."""
        try:
            # 1. Fetch messages
            messages = await get_messages(
                request.user_id, request.workspace_id, request.conversation_id
            )
            if not messages:
                return brain_pb2.GenerateTitleResponse(title="New Conversation")

            # 2. Construct prompt
            conversation_text = ""
            for m in messages[:6]: # Use first few messages
                conversation_text += f"{m['role']}: {m['content']}\n"
            
            prompt = (
                "Summarize the following conversation into a short, concise title (max 6 words). "
                "Do not use quotes. Just the title.\n\n"
                f"{conversation_text}"
            )

            # 3. Resolve provider — use workspace config, fall back to first user message
            try:
                pool = await get_pool()
                ws_config = await ProviderConfig(pool).get_config(request.workspace_id)
                provider_name = ws_config.get("provider", "local")
                api_key = ws_config.get("api_key", "")
                # Resolve Redis key reference
                if api_key and api_key.startswith("provider_key:"):
                    r = await get_redis()
                    real_key = await r.get(api_key)
                    api_key = real_key.decode("utf-8") if real_key else ""
                provider = get_provider(provider_name)
            except Exception:
                provider_name = "local"
                api_key = ""
                provider = get_provider("local")

            # Allow "smart" title generation:
            response_chunks = []
            logger.info(f"GenerateTitle: using provider={provider_name}, model=default, api_key={'present' if api_key else 'MISSING'}")
            try:
                async for token in provider.stream(
                    messages=[{"role": "user", "content": prompt}],
                    model="",
                    temperature=0.3,
                    max_tokens=20,
                    api_key=api_key,
                ):
                    response_chunks.append(token)
            except Exception as stream_err:
                logger.warning(f"Title generation stream failed: {stream_err}")

            # If LLM failed or returned nothing, derive title from first user message
            raw_response = "".join(response_chunks).strip()
            logger.info(f"GenerateTitle: raw_response='{raw_response[:100]}', chunks={len(response_chunks)}")
            if not response_chunks or raw_response.startswith("[Error"):
                first_user = next((m["content"] for m in messages if m["role"] == "user"), "")
                generated_title = first_user[:50].strip() if first_user else "New Conversation"
            else:
                generated_title = raw_response.strip('"')

            # Clamp to 80 chars
            generated_title = generated_title[:80] if generated_title else "New Conversation"
            logger.info(f"GenerateTitle: final title='{generated_title}'")
            
            # Update the title in DB
            await update_conversation_title(
                request.user_id, request.workspace_id, request.conversation_id, generated_title
            )

            return brain_pb2.GenerateTitleResponse(title=generated_title)

        except Exception as e:
            logger.error(f"GenerateTitle error: {e}")
            # Fallback
            return brain_pb2.GenerateTitleResponse(title="New Conversation")

    async def RegisterPushToken(self, request, context):
        # Phase 2: implement push token storage
        return brain_pb2.RegisterPushTokenResponse(success=True)

    async def GetUpdates(self, request, context):
        # Phase 2: implement sync
        return brain_pb2.GetUpdatesResponse(messages=[], conversations=[])

    # ── Autonomous Agent RPCs ────────────────────────────────────

    async def StartTask(self, request, context):
        """Start an autonomous agent task and stream events."""
        import json
        from agent.types import (
            AgentTask,
            GuardrailConfig as GCfg,
            RiskLevel,
            TaskStatus,
        )

        user_id = request.user_id
        workspace_id = request.workspace_id
        goal = request.goal

        # Build guardrail config from request (or defaults)
        config = GCfg()
        if request.guardrails:
            g = request.guardrails
            if g.max_iterations > 0:
                config.max_iterations = g.max_iterations
            if g.max_tool_calls > 0:
                config.max_tool_calls = g.max_tool_calls
            if g.max_tokens > 0:
                config.max_tokens = g.max_tokens
            if g.max_wall_time_seconds > 0:
                config.max_wall_time_seconds = g.max_wall_time_seconds
            if g.auto_approve_risk:
                config.auto_approve_risk = RiskLevel(g.auto_approve_risk)
            if g.blocked_patterns:
                config.blocked_patterns = list(g.blocked_patterns)
            if g.require_approval_tools:
                config.require_approval_tools = list(g.require_approval_tools)

        # Create the task
        task = AgentTask(
            user_id=user_id,
            workspace_id=workspace_id,
            goal=goal,
            conversation_id=request.conversation_id or None,
            config=config,
        )

        # Save to DB
        await _agent_persistence.save_task(task)
        logger.info(f"Agent task started: {task.id} — {goal}")

        # Store the running task handle
        _running_tasks[task.id] = task

        # Dynamically resolve provider from workspace config instead of
        # using the hardcoded "local" provider from the global _agent_loop.
        try:
            pool = await get_pool()
            ws_config = await ProviderConfig(pool).get_config(workspace_id)
            provider_name = ws_config.get("provider", "local")
            task_provider = get_provider(provider_name)
        except Exception as e:
            logger.warning(f"Failed to resolve workspace provider for task, using local: {e}")
            task_provider = get_provider("local")

        from agent.tools import build_tool_registry
        from agent.guardrails import Guardrails
        from agent.loop import AgentLoop
        from agent.evidence import EvidenceChain
        from agent.learner import TaskLearner
        from agent.memory import WorkingMemory
        from agent.reflection import ReflectionEngine

        task_model = ws_config.get("model", "") if ws_config else ""
        task_api_key = ws_config.get("api_key", "") if ws_config else ""

        task_tool_registry = build_tool_registry(hands_client=_hands_client, pool=pool)
        evidence_chain = EvidenceChain(task_id=task.id, pool=pool)

        task_working_memory = WorkingMemory(
            redis_client=None,
            vector_store=_vector_store,
        )
        task_learner = TaskLearner(
            provider=task_provider,
            model=task_model,
            working_memory=task_working_memory,
        )
        task_reflection = ReflectionEngine(
            llm_provider=task_provider,
            model=task_model,
        )

        task_loop = AgentLoop(
            provider=task_provider,
            tool_registry=task_tool_registry,
            guardrails=Guardrails(),
            persistence=_agent_persistence,
            model=task_model,
            api_key=task_api_key,
            memory_graph=_memory_graph,
            evidence_chain=evidence_chain,
            learner=task_learner,
            reflection_engine=task_reflection,
        )

        # Register this task as an active session
        if _session_manager:
            try:
                await _session_manager.register_session(
                    session_id=task.id,
                    task_id=task.id,
                    workspace_id=workspace_id,
                    user_id=user_id,
                    agent_type="task",
                    model=task_model,
                    goal=goal,
                )
            except Exception as e:
                logger.warning(f"Session registration failed: {e}")

        event_type_map = {
            "plan_created": brain_pb2.TaskEvent.EventType.PLAN_CREATED,
            "step_started": brain_pb2.TaskEvent.EventType.STEP_STARTED,
            "tool_called": brain_pb2.TaskEvent.EventType.TOOL_CALLED,
            "tool_result": brain_pb2.TaskEvent.EventType.TOOL_RESULT,
            "step_complete": brain_pb2.TaskEvent.EventType.STEP_COMPLETE,
            "approval_needed": brain_pb2.TaskEvent.EventType.APPROVAL_NEEDED,
            "thinking": brain_pb2.TaskEvent.EventType.THINKING,
            "task_complete": brain_pb2.TaskEvent.EventType.TASK_COMPLETE,
            "task_failed": brain_pb2.TaskEvent.EventType.TASK_FAILED,
            "task_paused": brain_pb2.TaskEvent.EventType.TASK_PAUSED,
        }

        # Run the task-specific agent loop and stream events
        try:
            async for event in task_loop.run(task):
                event_type_value = event.type.value if hasattr(event.type, "value") else str(event.type)
                yield brain_pb2.TaskEvent(
                    type=event_type_map.get(event_type_value, brain_pb2.TaskEvent.EventType.THINKING),
                    task_id=event.task_id,
                    step_id=event.step_id or "",
                    content=event.content,
                    tool_name=event.tool_name or "",
                    tool_args=event.tool_args or "",
                    tool_result=event.tool_result or "",
                    approval_id=event.approval_id or "",
                    progress={k: str(v) for k, v in (event.progress or {}).items()},
                )
        except Exception as e:
            logger.error(f"StartTask error for task {task.id}: {e}", exc_info=True)
            yield brain_pb2.TaskEvent(
                type=brain_pb2.TaskEvent.EventType.TASK_FAILED,
                task_id=task.id,
                content=str(e),
            )
        finally:
            _running_tasks.pop(task.id, None)
            if _session_manager:
                try:
                    await _session_manager.deregister_session(task.id)
                except Exception as e:
                    logger.warning(f"Session deregistration failed: {e}")

    async def StreamTaskEvents(self, request, context):
        """Reconnect to an already-running task's event stream."""
        task_id = request.task_id

        if task_id not in _running_tasks:
            context.abort(grpc.StatusCode.NOT_FOUND, f"Task {task_id} is not running")
            return

        # TODO: Implement event replay/fan-out via Redis pubsub
        context.abort(
            grpc.StatusCode.UNIMPLEMENTED,
            "Event stream reconnection coming in next iteration",
        )

    async def ApproveAction(self, request, context):
        """Approve or deny a pending agent action."""
        from agent.types import ApprovalStatus

        try:
            await _agent_persistence.resolve_approval(
                approval_id=request.approval_id,
                status=ApprovalStatus.APPROVED if request.approved else ApprovalStatus.DENIED,
                decided_by=request.user_id,
            )
            return brain_pb2.ApproveActionResponse(success=True, error="")
        except Exception as e:
            return brain_pb2.ApproveActionResponse(success=False, error=str(e))

    async def CancelTask(self, request, context):
        """Cancel a running agent task."""
        task_id = request.task_id

        if task_id in _running_tasks:
            task = _running_tasks[task_id]
            task.status = "cancelled"
            await _agent_persistence.update_task(task)
            _running_tasks.pop(task_id, None)
            return brain_pb2.CancelTaskResponse(success=True)

        # Try updating DB directly
        pool = await get_pool()
        await pool.execute(
            "UPDATE agent_tasks SET status = 'cancelled' WHERE id = $1",
            task_id,
        )
        return brain_pb2.CancelTaskResponse(success=True)

    async def ListTasks(self, request, context):
        """List agent tasks for a user/workspace."""
        pool = await get_pool()
        query = """
            SELECT id, goal, status, iterations, tool_calls_count,
                   result, error, created_at, completed_at
            FROM agent_tasks
            WHERE user_id = $1
        """
        params = [request.user_id]

        if request.workspace_id:
            query += " AND workspace_id = $2"
            params.append(request.workspace_id)

        if request.status:
            query += f" AND status = ${len(params) + 1}"
            params.append(request.status)

        query += " ORDER BY created_at DESC LIMIT 50"

        rows = await pool.fetch(query, *params)
        tasks = []
        for row in rows:
            tasks.append(brain_pb2.TaskSummary(
                id=str(row["id"]),
                goal=row["goal"],
                status=row["status"],
                iterations=row["iterations"],
                tool_calls=row["tool_calls_count"],
                result=row["result"] or "",
                error=row["error"] or "",
                created_at=row["created_at"].isoformat() if row["created_at"] else "",
                completed_at=row["completed_at"].isoformat() if row["completed_at"] else "",
            ))

        return brain_pb2.ListTasksResponse(tasks=tasks)

    # ── Workflows ────────────────────────────────────────────────

    async def LaunchWorkflow(self, request, context):
        """
        Launch a workflow by converting it into a StartTask call.
        Uses the in-memory WorkflowRegistry for template resolution.
        """
        workflow_id = request.workflow_id
        user_id = request.user_id
        workspace_id = request.workspace_id
        variables = dict(request.variables) if request.variables else {}
        conversation_id = request.conversation_id

        if not _workflow_registry:
            yield brain_pb2.TaskEvent(
                type=brain_pb2.TaskEvent.TASK_FAILED,
                content="Workflow registry not initialized",
            )
            return

        template = _workflow_registry.get(workflow_id)
        if not template:
            yield brain_pb2.TaskEvent(
                type=brain_pb2.TaskEvent.TASK_FAILED,
                content=f"Workflow '{workflow_id}' not found",
            )
            return

        # Substitute variables into the goal template
        goal = template.goal_template
        for key, value in variables.items():
            goal = goal.replace(f"{{{key}}}", value)

        # Create a StartTask request and delegate
        start_request = brain_pb2.StartTaskRequest(
            user_id=user_id,
            workspace_id=workspace_id,
            goal=goal,
            conversation_id=conversation_id,
        )

        async for event in self.StartTask(start_request, context):
            yield event

    async def ListWorkflows(self, request, context):
        """List available workflow templates."""
        if not _workflow_registry:
            return brain_pb2.ListWorkflowsResponse(workflows=[])

        category = request.category if request.category else None
        templates = _workflow_registry.list(category=category)

        items = []
        for t in templates:
            items.append(brain_pb2.WorkflowItem(
                id=t["id"],
                name=t["name"],
                description=t["description"],
                icon=t.get("icon", "📋"),
                category=t.get("category", ""),
                goal_template=t.get("goal_template", ""),
                tags=t.get("tags", []),
            ))

        return brain_pb2.ListWorkflowsResponse(workflows=items)

    async def GetCapabilities(self, request, context):
        """Return status of all agent subsystems for the UI."""
        caps = []

        # Intelligence subsystems
        caps.append(brain_pb2.CapabilityItem(
            name="Memory Graph",
            description="Semantic relationships between entities, concepts, and conversations",
            status="active" if _memory_graph else "disabled",
            category="intelligence",
            icon="🧠",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Persona Learning",
            description="Adapts communication style and preferences over time",
            status="active" if _persona_learner else "disabled",
            category="intelligence",
            icon="🎭",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Task Predictions",
            description="Proactive task suggestions based on patterns",
            status="active" if _task_predictor else "disabled",
            category="intelligence",
            icon="🔮",
        ))

        # Safety subsystems
        caps.append(brain_pb2.CapabilityItem(
            name="Slash Commands",
            description="/status, /help, /model, /think — instant session control",
            status="active" if _command_parser else "disabled",
            category="safety",
            icon="⚡",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Checkpoints",
            description="Auto-save task state before risky tool calls for rollback",
            status="active",
            category="safety",
            icon="💾",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Sandbox",
            description="Docker container isolation for untrusted code execution",
            status="active" if _sandbox_manager else "disabled",
            category="safety",
            icon="📦",
        ))

        # Automation subsystems
        caps.append(brain_pb2.CapabilityItem(
            name="Cron Scheduler",
            description="Scheduled recurring tasks with cron expressions",
            status="active" if _cron_scheduler else "disabled",
            category="automation",
            icon="⏰",
            stats={"jobs": str(len(_cron_scheduler._jobs)) if _cron_scheduler else "0"},
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Webhooks",
            description="HTTP webhook endpoints that trigger agent tasks",
            status="active" if _webhook_handler else "disabled",
            category="automation",
            icon="🔗",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Workflows",
            description="Pre-built task templates for common operations",
            status="active" if _workflow_registry else "disabled",
            category="automation",
            icon="📋",
            stats={"templates": str(len(_workflow_registry.list())) if _workflow_registry else "0"},
        ))

        # Tools subsystems
        caps.append(brain_pb2.CapabilityItem(
            name="Dynamic Skills",
            description="Custom user-created tools with sandboxed Python execution",
            status="active" if _skill_manager else "disabled",
            category="tools",
            icon="🛠️",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Metrics & Observability",
            description="Real-time token usage, cost tracking, and performance metrics",
            status="active" if _metrics_collector else "disabled",
            category="tools",
            icon="📊",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Agent Sessions",
            description="Cross-session messaging and agent discovery",
            status="active" if _session_manager else "disabled",
            category="tools",
            icon="💬",
        ))

        return brain_pb2.GetCapabilitiesResponse(capabilities=caps)

    # ── Provider Configuration ───────────────────────────────────



    async def GetMoltbookActivity(self, request, context):
        """Return recent Moltbook activity for the workspace."""
        pool = await get_pool()
        limit = request.limit or 20
        workspace_id = request.workspace_id

        try:
            rows = await pool.fetch(
                """SELECT id, action, title, content, submolt, post_id, url, created_at
                   FROM moltbook_activity
                   WHERE workspace_id = $1
                   ORDER BY created_at DESC
                   LIMIT $2""",
                workspace_id, min(limit, 50),
            )
        except Exception as e:
            # Table might not exist yet
            logger.warning(f"Moltbook activity query failed: {e}")
            rows = []

        activity = []
        for row in rows:
            activity.append(brain_pb2.MoltbookActivityItem(
                id=str(row['id']),
                action=row['action'] or "",
                title=row['title'] or "",
                content=(row['content'] or "")[:200],
                submolt=row['submolt'] or "",
                post_id=row['post_id'] or "",
                url=row['url'] or "",
                created_at=row['created_at'].isoformat() if row['created_at'] else "",
            ))
        return brain_pb2.GetMoltbookActivityResponse(activity=activity)

    # ── Automation ──────────────────────────────────────────────

    async def ParseCronJob(self, request, context):
        """Parse natural language into a cron expression."""
        try:
            from cron_parser import parse_nl_cron

            # Resolve provider & API key
            try:
                pool = await get_pool()
                ws_config = await ProviderConfig(pool).get_config(request.workspace_id)
                provider_name = ws_config.get("provider", "local")
                api_key = ws_config.get("api_key", "")
                model = ws_config.get("model", "")
                if api_key and api_key.startswith("provider_key:"):
                    r = await get_redis()
                    real_key = await r.get(api_key)
                    api_key = real_key.decode("utf-8") if real_key else ""
                provider = get_provider(provider_name)
            except Exception as e:
                logger.warning(f"Could not load provider config for cron parser: {e}")
                provider_name = "local"
                api_key = ""
                model = ""
                provider = get_provider("local")

            result = await parse_nl_cron(request.prompt, provider, model, api_key)
            return brain_pb2.ParseCronJobResponse(
                cron=result.get("cron", ""),
                human_schedule=result.get("human_schedule", ""),
                task=result.get("task", "")
            )
        except Exception as e:
            logger.error(f"ParseCronJob failed: {e}")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return brain_pb2.ParseCronJobResponse()

    async def ListProviderConfigs(self, request, context):
        rows = await list_provider_configs(request.workspace_id)
        configs = []
        for row in rows:
            configs.append(brain_pb2.ProviderConfig(
                workspace_id=str(row['workspace_id']),
                provider=row['provider'],
                model=row['model'] or "",
                temperature=row['temperature'],
                max_tokens=row['max_tokens'],
                system_prompt=row['system_prompt'] or "",
                rag_enabled=row['rag_enabled'],
                rag_top_k=row['rag_top_k'],
                rag_min_similarity=row['rag_min_similarity'],
                is_default=row['is_default'],
                api_key_encrypted="***" if row['api_key_encrypted'] else "",
                created_at=row['created_at'].isoformat(),
                updated_at=row['updated_at'].isoformat()
            ))
        return brain_pb2.ListProviderConfigsResponse(configs=configs)

    async def SetProviderConfig(self, request, context):
        config_dict = {
            'model': request.model,
            'temperature': request.temperature,
            'max_tokens': request.max_tokens,
            'system_prompt': request.system_prompt,
            'rag_enabled': request.rag_enabled,
            'rag_top_k': request.rag_top_k,
            'rag_min_similarity': request.rag_min_similarity,
            'is_default': request.is_default,
        }
        if request.api_key_encrypted:
            from encryption import encrypt
            config_dict['api_key_encrypted'] = encrypt(request.api_key_encrypted)
            
        row = await set_provider_config(request.workspace_id, request.provider, config_dict)
        
        return brain_pb2.SetProviderConfigResponse(
            config=brain_pb2.ProviderConfig(
                workspace_id=str(row['workspace_id']),
                provider=row['provider'],
                model=row['model'] or "",
                temperature=row['temperature'],
                max_tokens=row['max_tokens'],
                system_prompt=row['system_prompt'] or "",
                rag_enabled=row['rag_enabled'],
                rag_top_k=row['rag_top_k'],
                rag_min_similarity=row['rag_min_similarity'],
                is_default=row['is_default'],
                created_at=row['created_at'].isoformat(),
                updated_at=row['updated_at'].isoformat()
            )
        )

    async def DeleteProviderConfig(self, request, context):
        await delete_provider_config(request.workspace_id, request.provider)
        return brain_pb2.DeleteProviderConfigResponse(success=True)


# ── Server Bootstrap ─────────────────────────────────────────────────

async def serve():
    server = grpc_aio.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
        ],
    )

    # Register service
    # Note: In production, use compiled proto stubs via grpc_tools.protoc
    # For now, we use a generic servicer registration approach
    from grpc_reflection.v1alpha import reflection as grpc_reflection

    servicer = BrainServicer()

    brain_pb2_grpc.add_BrainServiceServicer_to_server(servicer, server)

    # Enable reflection
    service_names = (
        brain_pb2.DESCRIPTOR.services_by_name["BrainService"].full_name,
        grpc_reflection.SERVICE_NAME,
    )
    grpc_reflection.enable_server_reflection(service_names, server)

    bind_address = f"{GRPC_HOST}:{GRPC_PORT}"
    server.add_insecure_port(bind_address)

    logger.info(f"Brain gRPC server starting on {bind_address}")

    # Initialize database pool
    await get_pool()
    logger.info("Database pool initialized")

    # Initialize vector store + RAG pipelines
    vector_store = VectorStore()
    await vector_store.initialize()
    logger.info("Vector store initialized")

    global _retrieval, _embedding_pipeline, _vector_store
    _vector_store = vector_store
    _retrieval = RetrievalPipeline(vector_store)
    _embedding_pipeline = EmbeddingPipeline(vector_store)
    await _embedding_pipeline.start()
    logger.info("RAG pipelines initialized")

    # Initialize Hands gRPC client for sandboxed code execution
    global _hands_client
    hands_host = os.getenv("HANDS_GRPC_HOST", "hands")
    hands_port = os.getenv("HANDS_GRPC_PORT", "50052")
    try:
        hands_channel = grpc_aio.insecure_channel(f"{hands_host}:{hands_port}")
        # Import hands stubs if available
        try:
            hands_out_dir = os.path.join(os.path.dirname(__file__), "_generated")
            hands_proto_path = os.path.join(os.path.dirname(__file__), "../shared/proto")
            hands_proto = os.path.join(hands_proto_path, "hands.proto")
            if os.path.exists(hands_proto):
                protoc.main([
                    "grpc_tools.protoc",
                    f"-I{hands_proto_path}",
                    f"--python_out={hands_out_dir}",
                    f"--grpc_python_out={hands_out_dir}",
                    "hands.proto",
                ])
                import hands_pb2_grpc
                _hands_client = hands_pb2_grpc.HandsServiceStub(hands_channel)
                logger.info(f"Hands gRPC client connected to {hands_host}:{hands_port}")
            else:
                logger.warning("hands.proto not found — Hands client not initialized")
        except Exception as e:
            logger.warning(f"Hands gRPC client not available: {e}")
    except Exception as e:
        logger.warning(f"Could not connect to Hands service: {e}")

    # Initialize agent runtime
    from agent.tools import build_tool_registry
    from agent.guardrails import Guardrails
    from agent.loop import AgentLoop
    from agent.persistence import PostgresTaskPersistence
    from agent.memory_graph import MemoryGraph
    from agent.persona import PersonaLearner
    from agent.predictions import TaskPredictor
    from agent.automation import CronScheduler, WebhookHandler
    from agent.commands import CommandParser
    from agent.observability import MetricsCollector
    from agent.workflows import WorkflowRegistry
    from agent.skills import SkillManager
    from agent.sessions import SessionManager
    from agent.sandbox import SandboxManager
    from agent.checkpoints import CheckpointManager

    global _agent_loop, _agent_persistence, _tool_registry, _memory_graph
    global _cron_scheduler, _webhook_handler
    global _persona_learner, _task_predictor
    global _command_parser, _metrics_collector, _workflow_registry
    global _skill_manager, _session_manager, _sandbox_manager
    pool = await get_pool()
    _tool_registry = build_tool_registry(hands_client=_hands_client, pool=pool)
    guardrails = Guardrails()
    _agent_persistence = PostgresTaskPersistence(pool=pool)
    _agent_loop = AgentLoop(
        provider=get_provider("local"),
        tool_registry=_tool_registry,
        guardrails=guardrails,
        persistence=_agent_persistence,
    )
    logger.info(f"Agent runtime initialized ({len(_tool_registry._definitions)} tools)")

    # Initialize memory graph
    _memory_graph = MemoryGraph(pool=pool)
    logger.info("Memory graph initialized")

    # Initialize persona learner (adapts to user preferences over time)
    _persona_learner = PersonaLearner(pool=pool)
    logger.info("Persona learner initialized")

    # Initialize task predictor (proactive task suggestions)
    _task_predictor = TaskPredictor(
        pool=pool,
        memory_graph=_memory_graph,
        persona_learner=_persona_learner,
    )
    logger.info("Task predictor initialized")

    # Initialize command parser (slash commands like /status, /help)
    _command_parser = CommandParser()
    logger.info("Command parser initialized")

    # Initialize metrics collector (token & cost tracking)
    _metrics_collector = MetricsCollector()
    logger.info("Metrics collector initialized")

    # Initialize notification router
    from notifications import NotificationRouter
    _notification_router = NotificationRouter(pool, await get_redis())
    logger.info("Notification router initialized")

    # Initialize smart monitors
    from smart_monitors import SmartMonitors
    _smart_monitors = SmartMonitors(
        pool=pool,
        notification_router=_notification_router,
        metrics=_metrics_collector,
        agent_persistence=_agent_persistence
    )
    _smart_monitors.start()
    logger.info("Smart monitors initialized")

    # Initialize workflow registry (pre-built task templates)
    _workflow_registry = WorkflowRegistry()
    logger.info(f"Workflow registry initialized: {len(_workflow_registry.list())} templates")

    # Initialize skill manager (dynamic tool creation)
    _skill_manager = SkillManager(pool=pool, tool_registry=_tool_registry)
    logger.info("Skill manager initialized")

    # Initialize session manager (agent-to-agent messaging)
    _session_manager = SessionManager(pool=pool)
    logger.info("Session manager initialized")

    # Initialize sandbox manager (Docker container isolation)
    _sandbox_manager = SandboxManager()
    logger.info("Sandbox manager initialized")

    # Initialize and start automation (cron scheduler + webhook handler)
    async def launch_task_from_automation(workspace_id, user_id, goal, source="automation"):
        """Task launcher callback for cron/webhook automation."""
        from agent.types import AgentTask, GuardrailConfig as GCfg
        task = AgentTask(
            user_id=user_id,
            workspace_id=workspace_id,
            goal=goal,
            config=GCfg(),
        )
        await _agent_persistence.save_task(task)
        logger.info(f"Automation task started: {task.id} — {goal} (source: {source})")
        # Run in background
        asyncio.create_task(_run_automation_task(task))

    async def _run_automation_task(task):
        """Run an automation-triggered task in the background."""
        try:
            ws_config = await ProviderConfig(pool).get_config(task.workspace_id)
            provider_name = ws_config.get("provider", "local")
            task_provider = get_provider(provider_name)
            task_loop = AgentLoop(
                provider=task_provider,
                tool_registry=build_tool_registry(
                    hands_client=_hands_client,
                    vector_store=_vector_store,
                    pool=pool,
                ),
                guardrails=Guardrails(),
                persistence=_agent_persistence,
                memory_graph=_memory_graph,
            )
            # Set context for tools used in automated tasks
            import agent.tools.moltbook as _moltbook_mod
            _moltbook_mod._current_workspace_id = task.workspace_id
            import agent.tools.schedule as _schedule_mod
            _schedule_mod._cron_scheduler = _cron_scheduler
            _schedule_mod._current_workspace_id = task.workspace_id
            _schedule_mod._current_user_id = task.user_id
            async for event in task_loop.run(task):
                logger.debug(f"Automation task {task.id}: {event.type}")
        except Exception as e:
            logger.error(f"Automation task {task.id} failed: {e}")

    _cron_scheduler = CronScheduler(pool=pool, task_launcher=launch_task_from_automation)
    _webhook_handler = WebhookHandler(pool=pool, task_launcher=launch_task_from_automation)
    try:
        await _cron_scheduler.start()
        await _webhook_handler.load_endpoints()
        logger.info("Automation scheduler and webhook handler started")
    except Exception as e:
        logger.warning(f"Automation startup failed (non-fatal): {e}")

    await server.start()
    logger.info("Brain service ready")

    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down Brain service...")
        if _cron_scheduler:
            await _cron_scheduler.stop()
        await server.stop(5)
        if _pool:
            await _pool.close()


if __name__ == "__main__":
    asyncio.run(serve())
