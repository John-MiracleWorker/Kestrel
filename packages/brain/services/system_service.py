import grpc
from core.grpc_setup import brain_pb2
from .base import BaseServicerMixin
from core import runtime
from db import get_pool
from providers_registry import get_provider
from core.config import logger

class SystemServicerMixin(BaseServicerMixin):
    async def GetCapabilities(self, request, context):
        """Return status of all agent subsystems for the UI."""
        caps = []

        execution_runtime = getattr(runtime, "execution_runtime", None)
        feature_mode = getattr(runtime, "feature_mode", "core")
        enabled_bundles = getattr(runtime, "enabled_tool_bundles", [])
        catalog = runtime.tool_registry.catalog() if getattr(runtime, "tool_registry", None) else None
        kernel_registry = getattr(runtime, "kernel_node_registry", None)
        subsystem_bootstrapper = getattr(runtime, "subsystem_bootstrapper", None)
        subsystem_health = subsystem_bootstrapper.snapshot() if subsystem_bootstrapper else {}

        caps.append(brain_pb2.CapabilityItem(
            name="Kernel Preset",
            description="Active soft preset that seeds adaptive kernel thresholds and routing preferences.",
            status="active",
            category="runtime",
            icon="🧭",
            stats={"preset": str(feature_mode)},
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Execution Runtime",
            description="Active execution backend used by code and host tools.",
            status="active" if execution_runtime else "disabled",
            category="runtime",
            icon="⚙️",
            stats={
                key: str(value)
                for key, value in (execution_runtime.capabilities.as_dict() if execution_runtime else {}).items()
            },
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Capability Catalog",
            description="Live catalog generated from the active tool registry and subsystem availability.",
            status="active",
            category="runtime",
            icon="🧰",
            stats={
                "bundles": ", ".join(enabled_bundles) if enabled_bundles else "none",
                "tool_count": str(len(runtime.tool_registry.list_tools())) if runtime.tool_registry else "0",
                "catalog_path": getattr(catalog, "markdown_path", ""),
            },
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Subsystem Health",
            description="Lazy subsystem bootstrap status used by adaptive kernel policy and tool selection.",
            status="active",
            category="runtime",
            icon="âš¡",
            stats=subsystem_health or {"status": "none"},
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Kernel Playbook",
            description="Live adaptive-kernel node catalog describing orchestration nodes and activation signals.",
            status="active" if kernel_registry else "disabled",
            category="runtime",
            icon="kernel_playbook",
            stats={
                "node_count": str(len(kernel_registry.list_nodes())) if kernel_registry else "0",
                "playbook_path": getattr(kernel_registry, "markdown_path", ""),
            },
        ))

        # Intelligence subsystems
        caps.append(brain_pb2.CapabilityItem(
            name="Memory Graph",
            description="Semantic relationships between entities, concepts, and conversations",
            status="active" if runtime.memory_graph else "disabled",
            category="intelligence",
            icon="🧠",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Persona Learning",
            description="Adapts communication style and preferences over time",
            status="active" if runtime.persona_learner else "disabled",
            category="intelligence",
            icon="🎭",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Task Predictions",
            description="Proactive task suggestions based on patterns",
            status="active" if runtime.task_predictor else "disabled",
            category="intelligence",
            icon="🔮",
        ))

        # Safety subsystems
        caps.append(brain_pb2.CapabilityItem(
            name="Slash Commands",
            description="/status, /help, /model, /think — instant session control",
            status="active" if runtime.command_parser else "disabled",
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
            status="active" if runtime.sandbox_manager else "disabled",
            category="safety",
            icon="📦",
        ))

        # Automation subsystems
        caps.append(brain_pb2.CapabilityItem(
            name="Cron Scheduler",
            description="Scheduled recurring tasks with cron expressions",
            status="active" if runtime.cron_scheduler else "disabled",
            category="automation",
            icon="⏰",
            stats={"jobs": str(len(runtime.cron_scheduler._jobs)) if runtime.cron_scheduler else "0"},
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Webhooks",
            description="HTTP webhook endpoints that trigger agent tasks",
            status="active" if runtime.webhook_handler else "disabled",
            category="automation",
            icon="🔗",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Workflows",
            description="Pre-built task templates for common operations",
            status="active" if runtime.workflow_registry else "disabled",
            category="automation",
            icon="📋",
            stats={"templates": str(len(runtime.workflow_registry.list())) if runtime.workflow_registry else "0"},
        ))

        # Tools subsystems
        caps.append(brain_pb2.CapabilityItem(
            name="Dynamic Skills",
            description="Custom user-created tools with sandboxed Python execution",
            status="active" if runtime.skill_manager else "disabled",
            category="tools",
            icon="🛠️",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Metrics & Observability",
            description="Real-time token usage, cost tracking, and performance metrics",
            status="active" if runtime.metrics_collector else "disabled",
            category="tools",
            icon="📊",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Agent Sessions",
            description="Cross-session messaging and agent discovery",
            status="active" if runtime.session_manager else "disabled",
            category="tools",
            icon="💬",
        ))

        # Agent OS subsystems (Unified Control Plane)
        caps.append(brain_pb2.CapabilityItem(
            name="Proactive Heartbeat Engine",
            description="Background scheduler for proactive tasks and repository maintenance",
            status="active" if getattr(runtime, 'heartbeat_engine', None) else "disabled",
            category="automation",
            icon="⏱️",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Dual Memory Sync",
            description="Bi-directional vector graph and physical markdown memory persistence",
            status="active",
            category="intelligence",
            icon="📝",
        ))
        caps.append(brain_pb2.CapabilityItem(
            name="Headless Workflows",
            description="CI/CD integrated JSON-only background execution",
            status="active",
            category="automation",
            icon="🤖",
        ))

        return brain_pb2.GetCapabilitiesResponse(capabilities=caps)

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

    async def ListProcesses(self, request, context):
        """List scheduled jobs and recent agent tasks for a workspace."""
        workspace_id = request.workspace_id
        if not workspace_id:
            await context.abort(grpc.StatusCode.INVALID_ARGUMENT, "workspace_id is required")

        pool = await get_pool()
        processes = []

        if runtime.cron_scheduler:
            for job in await runtime.cron_scheduler.list_jobs(workspace_id):
                processes.append(brain_pb2.ProcessSummary(
                    id=str(job.get("id", "")),
                    name=str(job.get("name", "Scheduled task")),
                    type="scheduled",
                    status="running" if job.get("status") == "active" else str(job.get("status", "idle")),
                    cron=str(job.get("cron_expression", "")),
                    last_run=str(job.get("last_run") or ""),
                    next_run=str(job.get("next_run") or ""),
                    run_count=int(job.get("run_count") or 0),
                ))

        task_rows = await pool.fetch(
            """
            SELECT id, goal, status, result, error, created_at
            FROM agent_tasks
            WHERE workspace_id = $1
            ORDER BY created_at DESC
            LIMIT 25
            """,
            workspace_id,
        )

        for row in task_rows:
            status = str(row["status"])
            process_status = "completed" if status == "complete" else status
            processes.append(brain_pb2.ProcessSummary(
                id=str(row["id"]),
                name=str(row["goal"]),
                type="agent_task",
                status=process_status,
                last_run=row["created_at"].isoformat() if row["created_at"] else "",
                last_result=str(row["result"] or ""),
                error=str(row["error"] or ""),
            ))

        running = int(await pool.fetchval(
            """
            SELECT COUNT(*)
            FROM agent_tasks
            WHERE workspace_id = $1
              AND status IN ('planning', 'executing', 'observing', 'reflecting', 'waiting_approval')
            """,
            workspace_id,
        ) or 0)

        if runtime.cron_scheduler:
            running += sum(
                1
                for job in runtime.cron_scheduler._jobs.values()
                if job.workspace_id == workspace_id and job.status.value == "active"
            )

        return brain_pb2.ListProcessesResponse(processes=processes, running=running)


    async def HealthCheck(self, request, context):
        """Return health status."""
        status = {}
        for name in ["openai", "anthropic", "google", "ollama"]:
            try:
                status[name] = "ready" if get_provider(name).is_ready() else "not_ready"
            except Exception:
                status[name] = "not_ready"

        return brain_pb2.HealthCheckResponse(
            healthy=True,
            version="0.1.0",
            status=status,
        )
