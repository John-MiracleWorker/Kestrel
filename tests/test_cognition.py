from __future__ import annotations

from pathlib import Path

from nested_memvid_agent.cognition import FailureEpisode, LessonCard, RetryPolicy
from nested_memvid_agent.models import MemoryKind, MemoryLayer
from nested_memvid_agent.runtime_models import StrategyProposal, ToolCall, ToolExecution


def test_failure_episode_serializes_to_episodic_memory_record() -> None:
    execution = ToolExecution(
        call=ToolCall(name="test.run", arguments={"command": ["pytest", "-q"]}),
        success=False,
        content="AssertionError: expected fixed",
        error="test_failed",
    )

    episode = FailureEpisode.from_tool_failure(
        run_id="run_1",
        execution=execution,
        category="test_failure",
        diagnosis="Test failure playbook",
        attempted_strategy="Run the full suite.",
    )
    record = episode.to_memory_record()

    assert record.layer == MemoryLayer.EPISODIC
    assert record.kind == MemoryKind.FAILURE
    assert record.metadata["cognition_schema"] == "failure_episode.v1"
    assert record.metadata["frame_type"] == "failure_note"
    assert record.evidence


def test_lesson_card_serializes_to_procedural_memory_record() -> None:
    failure = FailureEpisode(
        failure_id="failure_1",
        run_id="run_1",
        task_id=None,
        tool_name="test.run",
        command="pytest -q",
        error_text="AssertionError: expected fixed",
        category="test_failure",
        diagnosis="Test failure playbook",
        attempted_strategy="Run the full suite.",
    )
    validation = ToolExecution(
        call=ToolCall(name="test.run", arguments={"command": ["pytest", "tests/test_one.py", "-q"]}, id="validate_1"),
        success=True,
        content="1 passed",
    )

    lesson = LessonCard.from_resolution(
        failure=failure,
        validation=validation,
        strategy=StrategyProposal(
            changed_strategy="Run the focused failing test before expanding validation.",
            why_different="Narrower target gives a cleaner signal.",
            expected_signal="The focused test passes.",
            fallback_if_fails="Inspect the assertion and fixture.",
        ),
    )
    record = lesson.to_memory_record()

    assert record.layer == MemoryLayer.PROCEDURAL
    assert record.kind == MemoryKind.PROCEDURE
    assert record.confidence >= 0.82
    assert record.metadata["cognition_schema"] == "lesson_card.v1"
    assert record.metadata["validation_status"] == "validated_once"


def test_retry_policy_blocks_same_action_without_strategy() -> None:
    previous = ToolExecution(
        call=ToolCall(name="test.run", arguments={"command": ["pytest", "-q"]}),
        success=False,
        content="failed",
        error="test_failed",
    )
    decision = RetryPolicy().assess_call(
        ToolCall(name="test.run", arguments={"command": ["pytest", "-q"]}),
        [previous],
    )

    assert decision.retry_allowed is False
    assert decision.strategy_diff is not None
    assert decision.strategy_diff.is_meaningfully_different is False


def test_retry_policy_allows_meaningful_changed_strategy() -> None:
    previous = ToolExecution(
        call=ToolCall(name="test.run", arguments={"command": ["pytest", "-q"]}),
        success=False,
        content="failed",
        error="test_failed",
    )
    decision = RetryPolicy().assess_call(
        ToolCall(
            name="test.run",
            arguments={"command": ["pytest", "-q"]},
            strategy=StrategyProposal(
                changed_strategy="Run only the failing test node to isolate the assertion before rerunning the suite.",
                why_different="The next action narrows the signal.",
                expected_signal="A focused failure or pass.",
                fallback_if_fails="Inspect the fixture setup.",
            ),
        ),
        [previous],
    )

    assert decision.retry_allowed is True
    assert decision.strategy_diff is not None
    assert decision.strategy_diff.is_meaningfully_different is True


def test_retry_policy_ignores_approval_failures_for_retry_blocking(tmp_path: Path) -> None:
    del tmp_path
    previous = ToolExecution(
        call=ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}),
        success=False,
        content="approval needed",
        error="approval_required",
    )

    decision = RetryPolicy().assess_call(
        ToolCall(name="shell.run", arguments={"command": ["echo", "hi"]}),
        [previous],
    )

    assert decision.retry_allowed is True
