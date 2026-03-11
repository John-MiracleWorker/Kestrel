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
