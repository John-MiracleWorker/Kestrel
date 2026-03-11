import asyncio
import json
from .base import BaseServicerMixin
from core.grpc_setup import brain_pb2
from core.config import logger
from core import runtime

from db import get_pool, get_redis
from providers_registry import get_provider, CloudProvider, list_provider_configs
from provider_config import ProviderConfig
from crud import ensure_conversation, save_message

from services.request_context import build_request_context
from services.task_factory import create_chat_task
from services.model_resolution import build_model_router, workspace_resolve_provider
from services.stream_coordinator import make_activity_callback, run_chat_stream
from services.post_response_hooks import run_post_response_hooks


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
                    configs = await list_provider_configs(request.workspace_id)
                    for c in configs:
                        if c["provider"] == request.provider:
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
            # ── 1. Resolve request context ─────────────────────────
            ctx = await build_request_context(request, workspace_id)

            # ── 2. Save user message before streaming ──────────────
            if conversation_id:
                await ensure_conversation(
                    conversation_id, workspace_id,
                    channel=ctx.channel_name,
                )
                if ctx.user_content:
                    await save_message(conversation_id, "user", ctx.user_content)

            # ── 3. Intercept /slash commands ───────────────────────
            from agent.types import TaskStatus
            if runtime.command_parser and runtime.command_parser.is_command(ctx.user_content):
                cmd_context = {
                    "model": ctx.model,
                    "total_tokens": 0,
                    "cost_usd": 0,
                    "task_status": "idle",
                    "session_type": "main",
                }
                cmd_result = runtime.command_parser.parse(ctx.user_content, cmd_context)
                if cmd_result and cmd_result.handled:
                    yield self._make_response(
                        chunk_type=0,
                        content_delta=cmd_result.response,
                    )
                    yield self._make_response(chunk_type=2)
                    if conversation_id and cmd_result.response:
                        await save_message(conversation_id, "user", ctx.user_content)
                        await save_message(conversation_id, "assistant", cmd_result.response)
                    return

            # ── 4. Build task and agent loop ────────────────────────
            chat_task = await create_chat_task(request, ctx, workspace_id)
            tool_registry = chat_task._tool_registry

            # Per-task evidence chain for auditable decision trail
            from agent.evidence import EvidenceChain
            evidence_chain = EvidenceChain(task_id=chat_task.id, pool=ctx.pool)

            # Per-task learner for post-task lesson extraction
            from agent.learner import TaskLearner
            from agent.core.memory import WorkingMemory
            task_working_memory = WorkingMemory(
                redis_client=None,
                vector_store=runtime.vector_store,
            )
            task_learner = TaskLearner(
                provider=ctx.provider,
                model=ctx.model,
                working_memory=task_working_memory,
            )

            # Output queue and activity callback
            output_queue = asyncio.Queue()
            _activity_callback = make_activity_callback(output_queue, self._make_response)

            # Load workspace-specific dynamic skills
            skill_manager = getattr(runtime, "skill_manager", None)
            bootstrapper = getattr(runtime, "subsystem_bootstrapper", None)
            if skill_manager is None and bootstrapper:
                skill_manager = await bootstrapper.ensure("skill_manager")
                if skill_manager is not None:
                    runtime.skill_manager = skill_manager
            if skill_manager:
                try:
                    skill_count = await skill_manager.load_workspace_skills(workspace_id)
                    if skill_count:
                        logger.info(f"Loaded {skill_count} custom skills for workspace")
                        await _activity_callback("skill_activated", {
                            "count": skill_count,
                            "message": f"{skill_count} workspace skills loaded",
                        })
                except Exception as e:
                    logger.warning(f"Failed to load workspace skills: {e}")

            # Checkpoint manager and model router
            from agent.checkpoints import CheckpointManager
            checkpoint_mgr = CheckpointManager(pool=ctx.pool)

            chat_model_router = build_model_router(
                ctx.provider_name, ctx.provider, ctx.provider_settings, ctx.model,
            )

            # Approval memory for pattern learning
            from agent.approval_memory import ApprovalMemory
            _approval_memory = ApprovalMemory(pool=ctx.pool)
            try:
                await _approval_memory.load_workspace_cache(workspace_id)
            except Exception as e:
                logger.warning("Failed to load approval memory cache: %s", e)

            _provider_resolver = workspace_resolve_provider(ctx.provider_settings)

            from agent.loop import AgentLoop
            from agent.core.reflection import ReflectionEngine
            from agent.guardrails import Guardrails
            from agent.simulation import OutcomeSimulator

            task_reflection = ReflectionEngine(
                llm_provider=ctx.provider,
                model=ctx.model,
                event_callback=_activity_callback,
            )
            task_simulator = None
            if bootstrapper:
                task_simulator = await bootstrapper.ensure("simulation")
            if task_simulator is None:
                task_simulator = OutcomeSimulator(
                    llm_provider=ctx.provider,
                    model=ctx.model,
                    event_callback=_activity_callback,
                )

            agent_loop = AgentLoop(
                provider=ctx.provider,
                tool_registry=tool_registry,
                guardrails=Guardrails(),
                persistence=runtime.agent_persistence,
                model=ctx.model,
                api_key=ctx.api_key,
                memory_graph=runtime.memory_graph,
                learner=task_learner,
                evidence_chain=evidence_chain,
                checkpoint_manager=checkpoint_mgr,
                event_callback=_activity_callback,
                provider_resolver=_provider_resolver,
                model_router=chat_model_router,
                approval_memory=_approval_memory,
                reflection_engine=task_reflection,
                simulator=task_simulator,
                persona_learner=runtime.persona_learner,
            )

            # Inject persona context into the system prompt if available
            if runtime.persona_learner:
                try:
                    prefs = await runtime.persona_learner.load_persona(request.user_id)
                    if prefs:
                        persona_block = runtime.persona_learner.format_for_prompt(prefs)
                        if persona_block and ctx.messages:
                            for msg in ctx.messages:
                                if not isinstance(msg, dict):
                                    continue
                                if msg.get("role") in ("system", 2):
                                    msg["content"] += "\n\n" + persona_block
                                    break
                except Exception as e:
                    logger.warning(f"Failed to inject persona context: {e}")

            # Inject installed MCP servers into the system prompt
            try:
                mcp_rows = await ctx.pool.fetch(
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
                    if ctx.messages:
                        for msg in ctx.messages:
                            if not isinstance(msg, dict):
                                continue
                            if msg.get("role") in ("system", 2):
                                msg["content"] += mcp_block
                                break
            except Exception as e:
                logger.warning(f"Failed to inject MCP server context: {e}")

            chat_task.messages = ctx.messages

            if chat_task.plan is not None:
                chat_task.status = TaskStatus.EXECUTING

            await runtime.agent_persistence.save_task(chat_task)

            # Attach activity callback to agent sub-modules
            if hasattr(agent_loop, '_council') and agent_loop._council:
                agent_loop._council._event_callback = _activity_callback
            if hasattr(agent_loop, '_coordinator') and agent_loop._coordinator:
                agent_loop._coordinator._event_callback = _activity_callback
            if hasattr(agent_loop, '_reflection') and agent_loop._reflection:
                agent_loop._reflection._event_callback = _activity_callback

            # ── 5. Stream agent loop output ────────────────────────
            full_response_parts: list[str] = []
            async for chunk in run_chat_stream(
                agent_loop, chat_task, output_queue, self._make_response,
                ctx.provider, ctx.model, ctx.api_key, full_response_parts,
                channel_name=ctx.channel_name,
            ):
                yield chunk

            # ── 6. Post-response hooks ─────────────────────────────
            full_response = "".join(full_response_parts)
            await run_post_response_hooks(
                conversation_id=conversation_id,
                workspace_id=workspace_id,
                user_content=ctx.user_content,
                full_response=full_response,
                ws_config=ctx.ws_config,
                provider=ctx.provider,
                model=ctx.model,
                api_key=ctx.api_key,
                user_id=request.user_id,
            )

            # Send DONE
            yield self._make_response(
                chunk_type=2,
                metadata={"provider": ctx.provider_name, "model": ctx.model},
            )

        except Exception as e:
            logger.error(f"StreamChat error: {e}", exc_info=True)
            yield self._make_response(
                chunk_type=3,
                error_message=str(e),
            )
