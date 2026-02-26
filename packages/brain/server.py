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
from concurrent import futures

from grpc import aio as grpc_aio
from dotenv import load_dotenv

from provider_config import ProviderConfig
from db import get_pool, get_redis
from providers_registry import (
    get_provider, resolve_provider, get_available_providers,
)

load_dotenv()
logger = logging.getLogger("brain")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())

# ── Configuration (canonical definitions in core.config / core.prompts) ──
from core.config import GRPC_PORT, GRPC_HOST

# ── Agent Runtime Globals ─────────────────────────────────────────────
from memory.vector_store import VectorStore
from memory.retrieval import RetrievalPipeline
from memory.embeddings import EmbeddingPipeline

# ── gRPC Service Implementation ──────────────────────────────────────
# Proto loading is handled by core.grpc_setup (generates stubs + exports brain_pb2)
from core.grpc_setup import brain_pb2, brain_pb2_grpc, reflection


from services.auth_service import AuthServicerMixin
from services.chat_service import ChatServicerMixin
from services.conversation_service import ConversationServicerMixin
from services.agent_service import AgentServicerMixin
from services.workflow_service import WorkflowServicerMixin
from services.system_service import SystemServicerMixin
from services.provider_service import ProviderServicerMixin
from core import runtime

class BrainServicer(
    AuthServicerMixin,
    ChatServicerMixin,
    ConversationServicerMixin,
    AgentServicerMixin,
    WorkflowServicerMixin,
    SystemServicerMixin,
    ProviderServicerMixin,
    brain_pb2_grpc.BrainServiceServicer,
):
    pass
# ── Server Bootstrap ─────────────────────────────────────────────────

async def serve():
    server = grpc_aio.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
        ],
    )

    servicer = BrainServicer()
    brain_pb2_grpc.add_BrainServiceServicer_to_server(servicer, server)

    # Enable gRPC reflection (reflection imported via core.grpc_setup)
    service_names = (
        brain_pb2.DESCRIPTOR.services_by_name["BrainService"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)

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
    runtime.vector_store = vector_store
    runtime.retrieval = RetrievalPipeline(vector_store)
    runtime.embedding_pipeline = EmbeddingPipeline(vector_store)
    await runtime.embedding_pipeline.start()
    logger.info("RAG pipelines initialized")

    # Initialize Hands gRPC client for sandboxed code execution
    hands_host = os.getenv("HANDS_GRPC_HOST", "hands")
    hands_port = os.getenv("HANDS_GRPC_PORT", "50052")
    try:
        hands_channel = grpc_aio.insecure_channel(f"{hands_host}:{hands_port}")
        # Import hands stubs if available
        try:
            from grpc_tools import protoc as _protoc
            hands_out_dir = os.path.join(os.path.dirname(__file__), "_generated")
            hands_proto_path = os.path.join(os.path.dirname(__file__), "../shared/proto")
            hands_proto = os.path.join(hands_proto_path, "hands.proto")
            if os.path.exists(hands_proto):
                _protoc.main([
                    "grpc_tools.protoc",
                    f"-I{hands_proto_path}",
                    f"--python_out={hands_out_dir}",
                    f"--grpc_python_out={hands_out_dir}",
                    "hands.proto",
                ])
                import hands_pb2_grpc
                runtime.hands_client = hands_pb2_grpc.HandsServiceStub(hands_channel)
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
    from agent.core.memory_graph import MemoryGraph
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
    # God-tier feature imports
    from agent.model_router import ModelRouter, RoutingStrategy
    from agent.nl_automation import AutomationBuilder
    from agent.daemon import DaemonManager
    from agent.branching import BranchManager
    from agent.simulation import OutcomeSimulator
    from agent.proactive import ProactiveEngine
    from agent.ui_artifacts import UIArtifactManager
    pool = await get_pool()
    runtime.tool_registry = build_tool_registry(hands_client=runtime.hands_client, pool=pool)
    guardrails = Guardrails()
    runtime.agent_persistence = PostgresTaskPersistence(pool=pool)

    # Feature 1: Adaptive Model Router with provider-aware routing
    available = get_available_providers()
    logger.info(f"Available providers: {available}")
    _model_router = ModelRouter()
    logger.info(f"Model router initialized (strategy={_model_router._strategy.value})")

    # Pick the best default provider as the baseline
    _default_provider = resolve_provider("ollama")
    runtime.agent_loop = AgentLoop(
        provider=_default_provider,
        tool_registry=runtime.tool_registry,
        guardrails=guardrails,
        persistence=runtime.agent_persistence,
        model_router=_model_router,
        provider_resolver=resolve_provider,
    )
    logger.info(f"Agent runtime initialized ({len(runtime.tool_registry._definitions)} tools)")

    # Initialize memory graph
    runtime.memory_graph = MemoryGraph(pool=pool)
    logger.info("Memory graph initialized")

    # Initialize persona learner (adapts to user preferences over time)
    runtime.persona_learner = PersonaLearner(pool=pool)
    logger.info("Persona learner initialized")

    # Initialize task predictor (proactive task suggestions)
    runtime.task_predictor = TaskPredictor(
        pool=pool,
        memory_graph=runtime.memory_graph,
        persona_learner=runtime.persona_learner,
    )
    logger.info("Task predictor initialized")

    # Initialize command parser (slash commands like /status, /help)
    runtime.command_parser = CommandParser()
    logger.info("Command parser initialized")

    # Initialize metrics collector (token & cost tracking)
    runtime.metrics_collector = MetricsCollector()
    logger.info("Metrics collector initialized")

    # Initialize notification router
    from notifications import NotificationRouter
    runtime.notification_router = NotificationRouter(pool, await get_redis())
    logger.info("Notification router initialized")

    # Initialize smart monitors
    from smart_monitors import SmartMonitors
    _smart_monitors = SmartMonitors(
        pool=pool,
        notification_router=runtime.notification_router,
        metrics=runtime.metrics_collector,
        agent_persistence=runtime.agent_persistence
    )
    _smart_monitors.start()
    logger.info("Smart monitors initialized")

    # Initialize workflow registry (pre-built task templates)
    runtime.workflow_registry = WorkflowRegistry()
    logger.info(f"Workflow registry initialized: {len(runtime.workflow_registry.list())} templates")

    # Initialize skill manager (dynamic tool creation)
    runtime.skill_manager = SkillManager(pool=pool, tool_registry=runtime.tool_registry)
    logger.info("Skill manager initialized")

    # Initialize session manager (agent-to-agent messaging)
    runtime.session_manager = SessionManager(pool=pool)
    logger.info("Session manager initialized")

    # Initialize sandbox manager (Docker container isolation)
    runtime.sandbox_manager = SandboxManager()
    logger.info("Sandbox manager initialized")

    # ── God-Tier Features ────────────────────────────────────────────

    # Dynamic provider resolver — checks workspace config at call time
    def _resolve_default_provider():
        """Return the workspace's default cloud provider (sync-safe)."""
        # Try to find the default configured cloud provider
        for name in ("google", "openai", "anthropic"):
            try:
                p = get_provider(name)
                if p.is_ready():
                    return p
            except Exception:
                continue
        # Last resort: return Google even if key missing (will error at call time)
        return get_provider("google")

    # Feature 2: NL Automation Builder
    _automation_builder = AutomationBuilder(
        provider_resolver=_resolve_default_provider,
    )
    logger.info("NL automation builder initialized")

    # Feature 3: Daemon Manager
    _daemon_manager = DaemonManager(
        pool=pool,
        notification_router=runtime.notification_router,
        task_launcher=None,  # Set after cron scheduler init
    )
    logger.info("Daemon manager initialized")

    # Feature 4: Branch Manager (Time-Travel)
    _branch_manager = BranchManager(pool=pool)
    logger.info("Branch manager initialized")

    # Feature 5: Outcome Simulator (Pre-Flight)
    _outcome_simulator = OutcomeSimulator(
        provider_resolver=_resolve_default_provider,
    )
    logger.info("Outcome simulator initialized")

    # Feature 6: Proactive Interrupt Engine
    _proactive_engine = ProactiveEngine(
        notification_router=runtime.notification_router,
        task_launcher=None,  # Set after cron scheduler init
    )
    try:
        await _proactive_engine.start()
        logger.info("Proactive interrupt engine started")
    except Exception as e:
        logger.warning(f"Proactive engine start failed (non-fatal): {e}")

    # Feature 7: UI Artifact Manager
    _ui_artifact_manager = UIArtifactManager(pool=pool)
    logger.info("UI artifact manager initialized")

    # Register god-tier tools
    from agent.tools.build_automation import register_build_automation_tools
    from agent.tools.daemon_control import register_daemon_tools
    from agent.tools.time_travel import register_time_travel_tools
    from agent.tools.ui_builder import register_ui_builder_tools

    register_build_automation_tools(runtime.tool_registry)
    register_daemon_tools(runtime.tool_registry)
    register_time_travel_tools(runtime.tool_registry)
    register_ui_builder_tools(runtime.tool_registry)
    logger.info("God-tier tools registered")


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
        await runtime.agent_persistence.save_task(task)
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
                    hands_client=runtime.hands_client,
                    vector_store=runtime.vector_store,
                    pool=pool,
                ),
                guardrails=Guardrails(),
                persistence=runtime.agent_persistence,
                memory_graph=runtime.memory_graph,
                provider_resolver=resolve_provider,
            )
            # Set context for tools used in automated tasks
            import agent.tools.moltbook as _moltbook_mod
            _moltbook_mod._current_workspace_id = task.workspace_id
            _moltbook_mod._current_user_id = task.user_id
            import agent.tools.schedule as _schedule_mod
            _schedule_mod._cron_scheduler = runtime.cron_scheduler
            _schedule_mod._current_workspace_id = task.workspace_id
            _schedule_mod._current_user_id = task.user_id
            async for event in task_loop.run(task):
                logger.debug(f"Automation task {task.id}: {event.type}")
        except Exception as e:
            logger.error(f"Automation task {task.id} failed: {e}")

    runtime.cron_scheduler = CronScheduler(pool=pool, task_launcher=launch_task_from_automation)
    runtime.webhook_handler = WebhookHandler(pool=pool, task_launcher=launch_task_from_automation)
    try:
        await runtime.cron_scheduler.start()
        await runtime.webhook_handler.load_endpoints()
        logger.info("Automation scheduler and webhook handler started")
    except Exception as e:
        logger.warning(f"Automation startup failed (non-fatal): {e}")

    # ── Initialize dynamic model registry ──────────────────────────────
    # Discovers current models from Google/OpenAI/Anthropic APIs at startup
    # so the rest of the codebase never hardcodes model names.
    try:
        from core.model_registry import model_registry
        from agent.model_router import init_models as init_router_models
        from agent.failover import build_dynamic_chains

        # Pre-warm the registry for Google (primary provider)
        await model_registry.list_models("google")
        await init_router_models()
        await build_dynamic_chains()
        logger.info("Dynamic model registry initialized")
    except Exception as e:
        logger.warning(f"Model registry init failed (non-fatal, will use env defaults): {e}")

    # Bootstrap autonomous Moltbook cron jobs (canonical version from cron.py)
    try:
        from core.cron import bootstrap_moltbook_cron
        await bootstrap_moltbook_cron(pool)
    except Exception as e:
        logger.warning(f"Moltbook cron bootstrap failed (non-fatal): {e}")

    # ── Wire daemon manager and load persisted daemons ─────────────────
    try:
        from core.cron import launch_task_from_automation
        _daemon_manager._task_launcher = launch_task_from_automation
        await _daemon_manager.load_daemons()
        logger.info("Daemon manager wired and persisted daemons loaded")
    except Exception as e:
        logger.warning(f"Daemon manager wiring failed (non-fatal): {e}")

    await server.start()
    logger.info("Brain service ready")

    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down Brain service...")
        if runtime.cron_scheduler:
            await runtime.cron_scheduler.stop()
        # Stop all daemons gracefully
        try:
            for daemon in _daemon_manager._daemons.values():
                await daemon.stop()
        except Exception:
            pass
        await server.stop(5)
        pool = await get_pool()
        if pool:
            await pool.close()


if __name__ == "__main__":
    asyncio.run(serve())
