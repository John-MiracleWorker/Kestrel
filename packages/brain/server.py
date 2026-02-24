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
from pathlib import Path

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
    resolve_provider, get_available_providers,
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
- You do not have direct access to the user's camera or microphone.
- You CAN see and control the user's screen via the `computer_use` tool — use it for GUI tasks like clicking, typing, scrolling, and navigating desktop apps or browsers.
- Your knowledge has a training cutoff. For current events, use web tools.

## Host Filesystem — How to Explore Codebases
You have access to the user's actual filesystem via host_* tools. Follow this strategy:

1. **project_recall(name)** — ALWAYS try this first. Returns cached project context.
2. **host_tree(path)** — If no cache, get full directory tree + tech stack in ONE call.
3. **host_find(pattern)** or **host_search(query, path)** — Narrow target files first (search-first workflow).
4. **host_batch_read(paths)** — Read MULTIPLE files at once (up to 20). Use this instead of calling host_read one at a time.
5. **host_read(path)** — Use only for one-off targeted reads after find/search.

**For large tasks** (audits, reviews, migrations): use **delegate_parallel** to spawn multiple explorer sub-agents that analyze different parts of the codebase simultaneously.

**NEVER** call host_list or host_read repeatedly. Use host_tree + host_find/host_search + host_batch_read instead.
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



_TASK_EVENT_HISTORY_MAX = int(os.getenv("TASK_EVENT_HISTORY_MAX", "300"))
_TASK_EVENT_TTL_SECONDS = int(os.getenv("TASK_EVENT_TTL_SECONDS", "3600"))

_TOOL_CATALOG_PATH = Path(__file__).resolve().parents[1] / "shared" / "tool-catalog.json"


def load_tool_catalog() -> list[dict]:
    with _TOOL_CATALOG_PATH.open("r", encoding="utf-8") as catalog_file:
        return json.load(catalog_file)


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

    # Bootstrap autonomous Moltbook cron jobs for all workspaces
    async def _bootstrap_moltbook_cron() -> None:
        """
        Ensure every workspace has an autonomous Moltbook session cron job.
        Runs every 2 hours. Skips workspaces that already have the job.
        The job is a no-op if no Moltbook credentials are present.
        """
        _MOLTBOOK_JOB_NAME = "moltbook_autonomous_session"
        _MOLTBOOK_CRON = "0 */2 * * *"   # every 2 hours
        _MOLTBOOK_GOAL = (
            "Run your autonomous Moltbook session. "
            "First call moltbook_session to scan your subscribed submolts and get your "
            "engagement plan. Then use the moltbook tool to engage: upvote quality posts, "
            "leave on-topic comments that add genuine value, and optionally create one "
            "original post if you have something worth sharing. "
            "Stay in character as Kestrel throughout."
        )
        try:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT wm.workspace_id, wm.user_id
                    FROM workspace_members wm
                    WHERE wm.role = 'owner'
                    ORDER BY wm.workspace_id
                    """
                )

            existing_names = {
                (j.workspace_id, j.name)
                for j in runtime.cron_scheduler._jobs.values()
            }

            created = 0
            for row in rows:
                ws_id = str(row["workspace_id"])
                u_id = str(row["user_id"])
                if (ws_id, _MOLTBOOK_JOB_NAME) in existing_names:
                    continue
                await runtime.cron_scheduler.create_job(
                    workspace_id=ws_id,
                    user_id=u_id,
                    name=_MOLTBOOK_JOB_NAME,
                    description="Autonomous Moltbook participation — browse, engage, post",
                    cron_expression=_MOLTBOOK_CRON,
                    goal=_MOLTBOOK_GOAL,
                )
                created += 1

            if created:
                logger.info(f"Bootstrapped Moltbook autonomous cron job for {created} workspace(s)")
        except Exception as e:
            logger.warning(f"Moltbook cron bootstrap failed (non-fatal): {e}")

    try:
        await _bootstrap_moltbook_cron()
    except Exception as e:
        logger.warning(f"Moltbook bootstrap error (non-fatal): {e}")

    await server.start()
    logger.info("Brain service ready")

    try:
        await server.wait_for_termination()
    except KeyboardInterrupt:
        logger.info("Shutting down Brain service...")
        if runtime.cron_scheduler:
            await runtime.cron_scheduler.stop()
        await server.stop(5)
        if _pool:
            await _pool.close()


if __name__ == "__main__":
    asyncio.run(serve())
