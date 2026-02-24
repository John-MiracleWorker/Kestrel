import json
from core.grpc_setup import brain_pb2
from core.config import logger, TASK_EVENT_HISTORY_MAX, TASK_EVENT_TTL_SECONDS
from db import get_redis

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

    async def _persist_task_event(self, task_event: "brain_pb2.TaskEvent") -> None:
        """Persist and publish task events for reconnectable streams."""
        try:
            redis_client = await get_redis()
            event_json = json.dumps({
                "type": int(task_event.type),
                "task_id": task_event.task_id,
                "step_id": task_event.step_id,
                "content": task_event.content,
                "tool_name": task_event.tool_name,
                "tool_args": task_event.tool_args,
                "tool_result": task_event.tool_result,
                "approval_id": task_event.approval_id,
                "progress": dict(task_event.progress),
            }, default=str)
            key = f"kestrel:task_events:{task_event.task_id}"
            channel = f"kestrel:task_events:{task_event.task_id}:channel"
            await redis_client.rpush(key, event_json)
            await redis_client.ltrim(key, -TASK_EVENT_HISTORY_MAX, -1)
            await redis_client.expire(key, TASK_EVENT_TTL_SECONDS)
            await redis_client.publish(channel, event_json)
        except Exception as event_err:
            logger.warning(f"Failed to persist task event: {event_err}")

    @staticmethod
    def _task_event_from_json(payload: dict) -> "brain_pb2.TaskEvent":
        progress = payload.get("progress") or {}
        return brain_pb2.TaskEvent(
            type=int(payload.get("type", brain_pb2.TaskEvent.EventType.THINKING)),
            task_id=str(payload.get("task_id", "")),
            step_id=str(payload.get("step_id", "")),
            content=str(payload.get("content", "")),
            tool_name=str(payload.get("tool_name", "")),
            tool_args=str(payload.get("tool_args", "")),
            tool_result=str(payload.get("tool_result", "")),
            approval_id=str(payload.get("approval_id", "")),
            progress={str(k): str(v) for k, v in progress.items()},
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
