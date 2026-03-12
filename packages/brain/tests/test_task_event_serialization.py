from agent.task_events import task_event_payload_from_proto
from core.grpc_setup import brain_pb2
from services.base import BaseServicerMixin


def test_task_event_from_json_accepts_string_event_types():
    event = BaseServicerMixin._task_event_from_json(
        {
            "type": "task_complete",
            "task_id": "task-1",
            "content": "done",
            "progress": {"status": "complete"},
        }
    )

    assert event.type == brain_pb2.TaskEvent.EventType.TASK_COMPLETE
    assert event.task_id == "task-1"
    assert event.progress["status"] == "complete"


def test_task_event_round_trips_structured_metadata():
    event = BaseServicerMixin._task_event_from_json(
        {
            "type": "tool_result",
            "task_id": "task-2",
            "tool_name": "code_execute",
            "tool_result": "runtime returned",
            "metadata": {
                "execution": {
                    "runtime_class": "sandboxed_docker",
                    "risk_class": "medium",
                    "fallback_used": False,
                },
            },
            "metrics": {
                "elapsed_ms": 42,
            },
        }
    )

    assert event.type == brain_pb2.TaskEvent.EventType.TOOL_RESULT

    payload = task_event_payload_from_proto(event)
    assert payload["metadata"]["execution"]["runtime_class"] == "sandboxed_docker"
    assert payload["metrics"]["elapsed_ms"] == 42
