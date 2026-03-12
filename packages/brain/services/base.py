from core.grpc_setup import brain_pb2
from core.config import logger
from agent.task_events import (
    dumps_task_event_json,
    loads_task_event_json,
    persist_task_event_payload,
)

class BaseServicerMixin:
    """Provides common utilities for all BrainServicer mixins."""

    @staticmethod
    def _event_type_to_proto(event_type_value: str):
        return {
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
        }.get(event_type_value, brain_pb2.TaskEvent.EventType.THINKING)

    @staticmethod
    def _proto_event_type_name(event_type_value: int) -> str:
        return {
            brain_pb2.TaskEvent.EventType.PLAN_CREATED: "plan_created",
            brain_pb2.TaskEvent.EventType.STEP_STARTED: "step_started",
            brain_pb2.TaskEvent.EventType.TOOL_CALLED: "tool_called",
            brain_pb2.TaskEvent.EventType.TOOL_RESULT: "tool_result",
            brain_pb2.TaskEvent.EventType.STEP_COMPLETE: "step_complete",
            brain_pb2.TaskEvent.EventType.APPROVAL_NEEDED: "approval_needed",
            brain_pb2.TaskEvent.EventType.THINKING: "thinking",
            brain_pb2.TaskEvent.EventType.TASK_COMPLETE: "task_complete",
            brain_pb2.TaskEvent.EventType.TASK_FAILED: "task_failed",
            brain_pb2.TaskEvent.EventType.TASK_PAUSED: "task_paused",
        }.get(int(event_type_value), "thinking")

    async def _persist_task_event(
        self,
        task_event: "brain_pb2.TaskEvent",
        *,
        workspace_id: str = "",
        user_id: str = "",
    ) -> None:
        """Persist and publish task events for reconnectable streams."""
        try:
            event_type_name = self._proto_event_type_name(task_event.type)
            metadata = loads_task_event_json(getattr(task_event, "event_metadata_json", ""))
            metrics = loads_task_event_json(getattr(task_event, "metrics_json", ""))
            payload = {
                "type": event_type_name,
                "event_type": event_type_name,
                "task_id": task_event.task_id,
                "step_id": task_event.step_id,
                "content": task_event.content,
                "tool_name": task_event.tool_name,
                "tool_args": task_event.tool_args,
                "tool_result": task_event.tool_result,
                "approval_id": task_event.approval_id,
                "progress": dict(task_event.progress),
                "metadata": metadata,
                "metrics": metrics,
            }
            await persist_task_event_payload(
                payload,
                workspace_id=workspace_id,
                user_id=user_id,
            )
        except Exception as event_err:
            logger.warning(f"Failed to persist task event: {event_err}")

    @staticmethod
    def _task_event_from_json(payload: dict) -> "brain_pb2.TaskEvent":
        progress = payload.get("progress") or {}
        raw_metadata = payload.get("metadata")
        metadata = raw_metadata if isinstance(raw_metadata, dict) else loads_task_event_json(
            raw_metadata or payload.get("event_metadata_json", "")
        )
        raw_metrics = payload.get("metrics")
        metrics = raw_metrics if isinstance(raw_metrics, dict) else loads_task_event_json(
            raw_metrics or payload.get("metrics_json", "")
        )
        raw_type = payload.get("type", payload.get("event_type", "thinking"))
        try:
            event_type = int(raw_type)
        except (TypeError, ValueError):
            event_type = BaseServicerMixin._event_type_to_proto(str(raw_type))
        return brain_pb2.TaskEvent(
            type=event_type,
            task_id=str(payload.get("task_id", "")),
            step_id=str(payload.get("step_id", "")),
            content=str(payload.get("content", "")),
            tool_name=str(payload.get("tool_name", "")),
            tool_args=str(payload.get("tool_args", "")),
            tool_result=str(payload.get("tool_result", "")),
            approval_id=str(payload.get("approval_id", "")),
            progress={str(k): str(v) for k, v in progress.items()},
            event_metadata_json=dumps_task_event_json(metadata),
            metrics_json=dumps_task_event_json(metrics),
        )

    def _make_response(self, chunk_type: int, content_delta: str = "",
                       error_message: str = "", metadata: dict = None,
                       tool_call: dict = None):
        """Build a ChatResponse object."""
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
        
        if isinstance(resp, dict):
            logger.error("CRITICAL: ChatResponse is a dict! This will crash gRPC.")
        return resp
