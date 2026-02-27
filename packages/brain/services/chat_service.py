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
                # Dynamically query installed Ollama models
                try:
                    from providers.ollama import OllamaProvider
                    ollama = OllamaProvider()
                    if ollama.is_ready():
                        ollama_models = await ollama.list_models()
                        pb_models = [
                            brain_pb2.Model(
                                id=m["id"],
                                name=m.get("name", m["id"]),
                                context_window=m.get("context_window", ""),
                            )
                            for m in ollama_models
                        ]
                        return brain_pb2.ListModelsResponse(models=pb_models)
                except Exception as e:
                    logger.warning(f"Failed to query Ollama models: {e}")
                # Fallback if Ollama unavailable
                return brain_pb2.ListModelsResponse(models=[])
            
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
            # ── 1 & 2. Load context, history, and RAG ───────────────
            pool = await get_pool()
            r = await get_redis()
            ws_config = await ProviderConfig(pool).get_config(workspace_id)
            provider_name = request.provider or ws_config["provider"]
            model = request.model or ws_config["model"]
            
            # Resolve API Key from Redis if it's a reference
            api_key = ws_config.get("api_key", "")
            if api_key and api_key.startswith("provider_key:"):
                try:
                    real_key = await r.get(api_key)
                    api_key = real_key.decode("utf-8") if real_key else ""
                except Exception:
                    api_key = ""
            
            provider = get_provider(provider_name)
            
            from services.context_builder import build_chat_context
            messages = await build_chat_context(
                request, workspace_id, pool, r, runtime, provider_name, model, ws_config, api_key
            )
            
            # ── 3. Save user message before streaming ───────────────
            if conversation_id:
                # Ensure conversation row exists (external channels like
                # Telegram generate deterministic IDs without creating rows).
                # Channel is passed in gRPC parameters map by registry.ts
                params = dict(request.parameters) if hasattr(request, 'parameters') else {}
                channel_name = params.get('channel', '') or 'web'
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
                    yield self._make_response(
                        chunk_type=0,  # CONTENT_DELTA
                        content_delta=cmd_result.response,
                    )
                    yield self._make_response(
                        chunk_type=2,  # DONE
                    )
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

            # Set workspace context for MCP tools
            import agent.tools.mcp as _mcp_mod
            _mcp_mod._current_workspace_id = workspace_id

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
            from agent.core.memory import WorkingMemory
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

                if activity_type in (
                    "delegation_started", "delegation_progress",
                    "delegation_complete", "parallel_delegation_started",
                    "parallel_delegation_complete",
                    "council_started", "council_opinion",
                    "council_debate", "council_verdict",
                ):
                    # Send as structured metadata for the AgentDebatePanel
                    await output_queue.put(self._make_response(
                        chunk_type=0,
                        metadata={
                            "agent_status": "delegation",
                            "delegation_type": activity_type,
                            "delegation": json.dumps(data),
                        },
                    ))
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

                # Direct response chunks from activity callbacks (_make_response returns ChatResponse)
                if isinstance(item, brain_pb2.ChatResponse):
                    yield item
                    continue

                # Tuple from the agent loop background task
                if isinstance(item, tuple):
                    from services.tool_parser import parse_agent_event
                    async for response_chunk in parse_agent_event(
                        item, full_response_parts, tool_results_gathered, 
                        provider, model, api_key, self._make_response
                    ):
                        yield response_chunk

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
                        from agent.core.memory_graph import extract_entities_llm
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
