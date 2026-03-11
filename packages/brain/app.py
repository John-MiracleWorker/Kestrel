"""Brain application composition root."""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent import futures
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from dotenv import load_dotenv
from grpc import aio as grpc_aio

from core import runtime
from core.config import GRPC_HOST, GRPC_PORT, validate_config
from core.feature_mode import (
    FeatureMode,
    enabled_bundles_for_mode,
    get_feature_mode,
    mode_supports_labs,
    mode_supports_ops,
)
from core.grpc_setup import brain_pb2, brain_pb2_grpc, reflection
from db import get_pool, get_redis
from memory.embeddings import EmbeddingPipeline
from memory.retrieval import RetrievalPipeline
from memory.vector_store import VectorStore
from providers_registry import get_available_providers, get_provider, resolve_provider
from services.agent_service import AgentServicerMixin
from services.auth_service import AuthServicerMixin
from services.chat_service import ChatServicerMixin
from services.conversation_service import ConversationServicerMixin
from services.provider_service import ProviderServicerMixin
from services.system_service import SystemServicerMixin
from services.workflow_service import WorkflowServicerMixin

load_dotenv()
logger = logging.getLogger("brain")
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper())
validate_config()


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


ShutdownHook = Callable[[], Awaitable[None] | None]


@dataclass
class InitializerResult:
    name: str
    shutdown_hooks: list[ShutdownHook] = field(default_factory=list)


def _record_initializer(name: str, status: str) -> None:
    runtime.startup_initializers.append(name)
    runtime.startup_readiness[name] = status


def _bind_server() -> grpc_aio.Server:
    server = grpc_aio.server(
        futures.ThreadPoolExecutor(max_workers=10),
        options=[
            ("grpc.max_send_message_length", 50 * 1024 * 1024),
            ("grpc.max_receive_message_length", 50 * 1024 * 1024),
        ],
    )

    servicer = BrainServicer()
    brain_pb2_grpc.add_BrainServiceServicer_to_server(servicer, server)

    service_names = (
        brain_pb2.DESCRIPTOR.services_by_name["BrainService"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(service_names, server)
    server.add_insecure_port(f"{GRPC_HOST}:{GRPC_PORT}")
    return server


def initializer_plan_for_mode(feature_mode: FeatureMode) -> tuple[str, ...]:
    plan = [
        "init_db",
        "init_memory",
        "init_hands_client",
        "init_agent_core",
        "init_provider_discovery",
    ]
    if mode_supports_ops(feature_mode):
        plan.append("init_ops")
    if mode_supports_labs(feature_mode):
        plan.append("init_labs")
    plan.append("init_mcp_auto_connect")
    return tuple(plan)


async def _init_db() -> InitializerResult:
    await get_pool()
    logger.info("Database pool initialized")
    _record_initializer("init_db", "ready")
    return InitializerResult(name="init_db")


async def _init_memory() -> InitializerResult:
    vector_store = VectorStore()
    await vector_store.initialize()
    runtime.vector_store = vector_store
    runtime.retrieval = RetrievalPipeline(vector_store)
    runtime.embedding_pipeline = EmbeddingPipeline(vector_store)
    await runtime.embedding_pipeline.start()
    logger.info("RAG pipelines initialized")
    _record_initializer("init_memory", "ready")
    return InitializerResult(
        name="init_memory",
        shutdown_hooks=[runtime.embedding_pipeline.stop],
    )


async def _init_hands_client() -> InitializerResult:
    hands_host = os.getenv("HANDS_GRPC_HOST", "hands")
    hands_port = os.getenv("HANDS_GRPC_PORT", "50052")
    try:
        hands_channel = grpc_aio.insecure_channel(f"{hands_host}:{hands_port}")
        import hands_pb2_grpc

        runtime.hands_client = hands_pb2_grpc.HandsServiceStub(hands_channel)
        logger.info("Hands gRPC client connected to %s:%s", hands_host, hands_port)
        _record_initializer("init_hands_client", "ready")
    except Exception as exc:
        runtime.hands_client = None
        logger.warning("Hands gRPC client not available: %s", exc)
        _record_initializer("init_hands_client", f"degraded:{exc}")
    return InitializerResult(name="init_hands_client")


async def _init_agent_core(feature_mode: FeatureMode) -> InitializerResult:
    from agent.checkpoints import CheckpointManager
    from agent.commands import CommandParser
    from agent.core.memory_graph import MemoryGraph
    from agent.guardrails import Guardrails
    from agent.loop import AgentLoop
    from agent.model_router import ModelRouter
    from agent.observability import MetricsCollector
    from agent.persistence import PostgresTaskPersistence
    from agent.policy_engine import PolicyEngine
    from agent.persona import PersonaLearner
    from agent.runtime import build_runtime_policy
    from agent.sandbox import SandboxManager
    from agent.task_queue import (
        JobRunner,
        OpportunityEngine,
        TaskDispatcher,
        TaskEnqueuer,
        WorkspaceAgentStore,
    )
    from agent.tools import build_tool_registry

    pool = await get_pool()
    shutdown_hooks: list[ShutdownHook] = []
    runtime.execution_runtime = build_runtime_policy(hands_client=runtime.hands_client)
    runtime.feature_mode = feature_mode.value
    runtime.enabled_tool_bundles = list(enabled_bundles_for_mode(feature_mode))
    runtime.tool_registry = build_tool_registry(
        hands_client=runtime.hands_client,
        pool=pool,
        runtime_policy=runtime.execution_runtime,
        enabled_bundles=runtime.enabled_tool_bundles,
        feature_mode=feature_mode.value,
    )
    runtime.agent_persistence = PostgresTaskPersistence(pool=pool)
    runtime.memory_graph = MemoryGraph(pool=pool)
    runtime.persona_learner = PersonaLearner(pool=pool)
    runtime.command_parser = CommandParser()
    runtime.metrics_collector = MetricsCollector()
    runtime.sandbox_manager = SandboxManager()
    runtime.checkpoint_manager = CheckpointManager(pool=pool)
    runtime.workspace_agent_store = WorkspaceAgentStore(pool=pool)
    runtime.task_enqueuer = TaskEnqueuer(pool=pool, workspace_agent_store=runtime.workspace_agent_store)
    runtime.policy_engine = PolicyEngine()
    runtime.opportunity_engine = OpportunityEngine(
        pool=pool,
        workspace_agent_store=runtime.workspace_agent_store,
        task_enqueuer=runtime.task_enqueuer,
    )
    runtime.job_runner = JobRunner(runtime_ctx=runtime, pool=pool)
    runtime.task_dispatcher = TaskDispatcher(
        enqueuer=runtime.task_enqueuer,
        runner=runtime.job_runner,
        concurrency=int(os.getenv("TASK_DISPATCHER_CONCURRENCY", "2")),
        poll_interval_seconds=float(os.getenv("TASK_DISPATCHER_POLL_SECONDS", "1.0")),
        lease_seconds=int(os.getenv("TASK_DISPATCHER_LEASE_SECONDS", "300")),
    )
    await runtime.task_dispatcher.start()

    async def _stop_dispatcher() -> None:
        if runtime.task_dispatcher:
            await runtime.task_dispatcher.stop()

    shutdown_hooks.append(_stop_dispatcher)

    available = get_available_providers()
    logger.info("Available providers: %s", available)
    model_router = ModelRouter()
    default_provider = resolve_provider("ollama")
    runtime.agent_loop = AgentLoop(
        provider=default_provider,
        tool_registry=runtime.tool_registry,
        guardrails=Guardrails(),
        persistence=runtime.agent_persistence,
        model_router=model_router,
        provider_resolver=resolve_provider,
        persona_learner=runtime.persona_learner,
    )
    logger.info(
        "Agent core initialized (feature_mode=%s, tools=%s)",
        feature_mode.value,
        len(runtime.tool_registry._definitions),
    )
    _record_initializer("init_agent_core", "ready")
    return InitializerResult(name="init_agent_core", shutdown_hooks=shutdown_hooks)


async def _init_ops() -> InitializerResult:
    from agent.automation import CronScheduler, WebhookHandler
    from agent.observability import MetricsCollector
    from agent.workflows import WorkflowRegistry
    from core.cron import launch_task_from_automation
    from notifications import NotificationRouter
    from smart_monitors import SmartMonitors

    pool = await get_pool()
    shutdown_hooks: list[ShutdownHook] = []

    runtime.notification_router = NotificationRouter(pool, await get_redis())
    runtime.workflow_registry = WorkflowRegistry()

    smart_monitors = SmartMonitors(
        pool=pool,
        notification_router=runtime.notification_router,
        metrics=runtime.metrics_collector or MetricsCollector(),
        agent_persistence=runtime.agent_persistence,
    )
    smart_monitors.start()
    runtime.smart_monitors = smart_monitors

    runtime.cron_scheduler = CronScheduler(pool=pool, task_launcher=launch_task_from_automation)
    runtime.webhook_handler = WebhookHandler(pool=pool, task_launcher=launch_task_from_automation)
    try:
        await runtime.cron_scheduler.start()
        await runtime.webhook_handler.load_endpoints()
    except Exception as exc:
        logger.warning("Automation startup failed (non-fatal): %s", exc)

    async def _stop_ops() -> None:
        if runtime.cron_scheduler:
            await runtime.cron_scheduler.stop()

    shutdown_hooks.append(_stop_ops)
    logger.info("Ops services initialized")
    _record_initializer("init_ops", "ready")
    return InitializerResult(name="init_ops", shutdown_hooks=shutdown_hooks)


def _resolve_default_cloud_provider():
    for name in ("google", "openai", "anthropic"):
        try:
            provider = get_provider(name)
            if provider.is_ready():
                return provider
        except Exception:
            continue
    return get_provider("google")


async def _init_labs() -> InitializerResult:
    from agent.branching import BranchManager
    from agent.daemon import DaemonManager
    from agent.nl_automation import AutomationBuilder
    from agent.predictions import TaskPredictor
    from agent.proactive import HeartbeatEngine, ProactiveEngine
    from agent.sessions import SessionManager
    from agent.skills import SkillManager
    from agent.simulation import OutcomeSimulator
    from agent.ui_artifacts import UIArtifactManager
    from core.cron import (
        bootstrap_ai_news_cron,
        bootstrap_gmail_cron,
        bootstrap_moltbook_cron,
        launch_task_from_automation,
    )

    pool = await get_pool()
    shutdown_hooks: list[ShutdownHook] = []

    runtime.task_predictor = TaskPredictor(
        pool=pool,
        memory_graph=runtime.memory_graph,
        persona_learner=runtime.persona_learner,
    )
    runtime.skill_manager = SkillManager(pool=pool, tool_registry=runtime.tool_registry)
    runtime.session_manager = SessionManager(pool=pool)
    runtime.outcome_simulator = OutcomeSimulator(provider_resolver=_resolve_default_cloud_provider)
    runtime.branch_manager = BranchManager(pool=pool)
    runtime.ui_artifact_manager = UIArtifactManager(pool=pool)

    automation_builder = AutomationBuilder(provider_resolver=_resolve_default_cloud_provider)
    daemon_manager = DaemonManager(
        pool=pool,
        notification_router=runtime.notification_router,
        task_launcher=None,
    )
    runtime.automation_builder = automation_builder
    runtime.daemon_manager = daemon_manager
    runtime.proactive_engine = ProactiveEngine(
        notification_router=runtime.notification_router,
        task_launcher=None,
        llm_provider=_resolve_default_cloud_provider(),
        model=os.getenv("PROACTIVE_MODEL", "gemini-2.0-flash"),
    )

    try:
        await runtime.proactive_engine.start()
        shutdown_hooks.append(runtime.proactive_engine.stop)
    except Exception as exc:
        logger.warning("Proactive engine start failed (non-fatal): %s", exc)

    from agent.core.fs_watcher import KestrelFSWatcher

    runtime.fs_watcher = KestrelFSWatcher(engine=runtime.proactive_engine)
    try:
        runtime.fs_watcher.start()
        shutdown_hooks.append(runtime.fs_watcher.stop)
    except Exception as exc:
        logger.warning("FS watcher start failed (non-fatal): %s", exc)

    if getattr(runtime, "smart_monitors", None):
        try:
            runtime.smart_monitors.set_proactive_engine(runtime.proactive_engine)
        except Exception:
            pass

    runtime.heartbeat_engine = HeartbeatEngine(
        pool=pool,
        task_launcher=launch_task_from_automation,
        interval_seconds=3600,
        opportunity_engine=runtime.opportunity_engine,
        session_manager=runtime.session_manager,
    )
    try:
        await runtime.heartbeat_engine.start()
        shutdown_hooks.append(runtime.heartbeat_engine.stop)
    except Exception as exc:
        logger.warning("Heartbeat engine start failed (non-fatal): %s", exc)

    try:
        daemon_manager._task_launcher = launch_task_from_automation
        await daemon_manager.load_daemons()
    except Exception as exc:
        logger.warning("Daemon manager wiring failed (non-fatal): %s", exc)

    try:
        runtime.proactive_engine._task_launcher = launch_task_from_automation
    except Exception:
        pass

    try:
        await bootstrap_moltbook_cron(pool)
        await bootstrap_gmail_cron(pool)
        await bootstrap_ai_news_cron(pool)
    except Exception as exc:
        logger.warning("Labs cron bootstrap failed (non-fatal): %s", exc)

    logger.info("Labs services initialized")
    _record_initializer("init_labs", "ready")
    return InitializerResult(name="init_labs", shutdown_hooks=shutdown_hooks)


async def _init_provider_discovery() -> InitializerResult:
    try:
        from agent.failover import build_dynamic_chains
        from agent.model_router import init_models as init_router_models
        from core.model_registry import model_registry
        from providers.lmstudio import LMStudioProvider
        from providers.ollama import OllamaProvider

        OllamaProvider.start_discovery()
        LMStudioProvider.start_discovery()
        await model_registry.list_models("google")
        await init_router_models()
        await build_dynamic_chains()
        _record_initializer("init_provider_discovery", "ready")
    except Exception as exc:
        logger.warning("Model registry init failed (non-fatal): %s", exc)
        _record_initializer("init_provider_discovery", f"degraded:{exc}")
    return InitializerResult(name="init_provider_discovery")


async def _init_mcp_auto_connect(feature_mode: FeatureMode) -> InitializerResult:
    if not mode_supports_ops(feature_mode):
        _record_initializer("init_mcp_auto_connect", "skipped")
        return InitializerResult(name="init_mcp_auto_connect")

    try:
        from agent.tools.mcp_client import get_mcp_pool

        pool = await get_pool()
        mcp_pool = get_mcp_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT name, server_url, config FROM installed_tools WHERE enabled = true"
            )
        connected = 0
        for row in rows:
            command = row["server_url"]
            if not command:
                continue
            env = {}
            if row["config"]:
                config = row["config"] if isinstance(row["config"], dict) else __import__("json").loads(row["config"])
                env = config.get("env", {})
            try:
                result = await mcp_pool.connect(row["name"], command, env)
                if "error" not in result:
                    connected += 1
            except Exception:
                continue
        if rows:
            logger.info("MCP auto-connect: %s/%s servers connected", connected, len(rows))
        mcp_pool.start_health_monitor(interval_seconds=60)
        _record_initializer("init_mcp_auto_connect", "ready")
    except Exception as exc:
        logger.warning("MCP auto-connect failed (non-fatal): %s", exc)
        _record_initializer("init_mcp_auto_connect", f"degraded:{exc}")
    return InitializerResult(name="init_mcp_auto_connect")


async def serve() -> None:
    runtime.startup_initializers.clear()
    runtime.startup_readiness.clear()

    feature_mode = get_feature_mode()
    server = _bind_server()
    bind_address = f"{GRPC_HOST}:{GRPC_PORT}"
    logger.info("Brain gRPC server starting on %s (feature_mode=%s)", bind_address, feature_mode.value)

    shutdown_hooks: list[ShutdownHook] = []
    await _init_db()
    shutdown_hooks.extend((await _init_memory()).shutdown_hooks)
    await _init_hands_client()
    shutdown_hooks.extend((await _init_agent_core(feature_mode)).shutdown_hooks)
    await _init_provider_discovery()

    if mode_supports_ops(feature_mode):
        shutdown_hooks.extend((await _init_ops()).shutdown_hooks)
    else:
        _record_initializer("init_ops", "skipped")

    if mode_supports_labs(feature_mode):
        shutdown_hooks.extend((await _init_labs()).shutdown_hooks)
    else:
        _record_initializer("init_labs", "skipped")

    await _init_mcp_auto_connect(feature_mode)

    await server.start()
    logger.info("Brain service ready")

    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down Brain service...")
    finally:
        for hook in reversed(shutdown_hooks):
            try:
                result = hook()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.warning("Shutdown hook failed: %s", exc)
        pool = await get_pool()
        await server.stop(5)
        if pool:
            await pool.close()
