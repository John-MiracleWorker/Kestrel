from services.operator_service import (
    OperatorServicerMixin,
    _build_recovery_hints,
    _progress_from_plan,
)


def test_progress_from_plan_uses_completed_steps():
    current, total = _progress_from_plan(
        {
            "steps": [
                {"status": "complete"},
                {"status": "skipped"},
                {"status": "pending"},
            ]
        }
    )

    assert current == "3"
    assert total == "3"


def test_recovery_hints_cover_pending_and_orphaned_states():
    hints = _build_recovery_hints(
        status="failed",
        stale=True,
        orphaned=True,
        pending_approval_id="approval-1",
        last_checkpoint_id="checkpoint-1",
    )
    codes = {hint.code for hint in hints}

    assert "approval_pending" in codes
    assert "orphaned_execution" in codes
    assert "review_failure" in codes
    assert "checkpoint_available" in codes


def test_execution_summary_uses_latest_execution_metadata():
    summary = OperatorServicerMixin._derive_execution_summary(
        [
            {
                "tool_name": "search",
                "created_at": "2026-03-11T10:00:00Z",
                "metadata": {},
            },
            {
                "tool_name": "code",
                "created_at": "2026-03-11T10:01:00Z",
                "metadata": {
                    "execution": {
                        "runtime_class": "docker_sandbox",
                        "risk_class": "workspace_write",
                        "fallback_used": True,
                        "fallback_from": "docker_sandbox",
                        "fallback_to": "native_host",
                    }
                },
            },
        ]
    )

    assert summary.runtime_class == "docker_sandbox"
    assert summary.risk_class == "workspace_write"
    assert summary.fallback_summary == "docker_sandbox -> native_host"
    assert list(summary.recent_tools) == ["search", "code"]
    assert summary.last_event_at == "2026-03-11T10:01:00Z"
