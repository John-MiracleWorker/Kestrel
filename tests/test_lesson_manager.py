from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.backends.in_memory import InMemoryBackend
from nested_memvid_agent.cognition import FailureEpisode, LessonManager
from nested_memvid_agent.layers import LayeredMemorySystem
from nested_memvid_agent.models import MemoryLayer, RetrievalQuery
from nested_memvid_agent.runtime_models import StrategyProposal, ToolCall, ToolExecution


def test_lesson_manager_deduplicates_similar_lessons_and_updates_counts(tmp_path: Path) -> None:
    memory = LayeredMemorySystem.from_backend_factory(tmp_path, InMemoryBackend)
    manager = LessonManager(memory)
    failure = FailureEpisode(
        failure_id="failure-1",
        run_id="run-1",
        task_id=None,
        tool_name="pytest",
        command="pytest -q",
        error_text="ImportError: missing PYTHONPATH for package tests",
        category="python-import",
        diagnosis="Python path was not configured",
        attempted_strategy="Run pytest directly",
    )
    validation = ToolExecution(
        call=ToolCall(name="test.run", arguments={"command": ["pytest", "-q"]}, id="validation-1"),
        success=True,
        content="exit_code=0",
    )

    first, first_record_id = manager.write_lesson_from_resolution(
        failure=failure,
        validation=validation,
        strategy=StrategyProposal(changed_strategy="Set PYTHONPATH before running pytest."),
    )
    second, second_record_id = manager.write_lesson_from_resolution(
        failure=FailureEpisode(
            **{**failure.to_payload(), "failure_id": "failure-2", "error_text": "ImportError: PYTHONPATH missing"}
        ),
        validation=ToolExecution(
            call=ToolCall(name="test.run", arguments={"command": ["pytest", "-q"]}, id="validation-2"),
            success=True,
            content="exit_code=0",
        ),
        strategy=StrategyProposal(changed_strategy="Set PYTHONPATH before pytest runs."),
    )

    hits = memory.retrieve(RetrievalQuery(query="PYTHONPATH pytest", layers=(MemoryLayer.PROCEDURAL,), k_per_layer=5))

    assert first_record_id == first.id
    assert second_record_id == first.id
    assert second.id == first.id
    assert second.success_count == 2
    assert second.failure_count == 2
    assert "validation-2" in second.evidence_refs
    assert len(hits) == 1
    assert hits[0].record.metadata["repeat_count"] == 4
