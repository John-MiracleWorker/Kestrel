from provider_config import ProviderConfig
import grpc
from core.grpc_setup import brain_pb2
from .base import BaseServicerMixin
from core import runtime
from db import get_pool, get_redis
from providers_registry import get_provider
from core.config import logger

class WorkflowServicerMixin(BaseServicerMixin):
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

        if not runtime.workflow_registry:
            yield brain_pb2.TaskEvent(
                type=brain_pb2.TaskEvent.TASK_FAILED,
                content="Workflow registry not initialized",
            )
            return

        template = runtime.workflow_registry.get(workflow_id)
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
        if not runtime.workflow_registry:
            return brain_pb2.ListWorkflowsResponse(workflows=[])

        category = request.category if request.category else None
        templates = runtime.workflow_registry.list(category=category)

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

