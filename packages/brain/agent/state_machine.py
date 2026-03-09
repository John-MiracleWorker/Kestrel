"""
Task State Machine — enforces legal state transitions for agent tasks.

Prevents illegal jumps like PLANNING → COMPLETE (skipping execution)
or COMPLETE → EXECUTING (zombie tasks). Logs all transitions for
debugging and audit.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from agent.types import TaskStatus

logger = logging.getLogger("brain.agent.state_machine")


class IllegalTransitionError(Exception):
    """Raised when an illegal state transition is attempted."""
    def __init__(self, from_status: TaskStatus, to_status: TaskStatus):
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Illegal state transition: {from_status.value} → {to_status.value}"
        )


# ── Legal Transitions ────────────────────────────────────────────────

LEGAL_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PLANNING: {
        TaskStatus.EXECUTING,
        TaskStatus.WAITING_APPROVAL,  # Council/simulation rejection
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.EXECUTING: {
        TaskStatus.REFLECTING,
        TaskStatus.WAITING_APPROVAL,
        TaskStatus.COMPLETE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.PAUSED,
    },
    TaskStatus.REFLECTING: {
        TaskStatus.EXECUTING,
        TaskStatus.PLANNING,  # Replan
        TaskStatus.COMPLETE,  # Verification passed
        TaskStatus.FAILED,
    },
    TaskStatus.WAITING_APPROVAL: {
        TaskStatus.EXECUTING,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.OBSERVING: {
        TaskStatus.EXECUTING,
        TaskStatus.REFLECTING,
    },
    TaskStatus.PAUSED: {
        TaskStatus.EXECUTING,  # Resume
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETE: set(),     # Terminal — no transitions out
    TaskStatus.FAILED: set(),       # Terminal — no transitions out
    TaskStatus.CANCELLED: set(),    # Terminal — no transitions out
}


def validate_transition(current: TaskStatus, target: TaskStatus) -> bool:
    """Check if a state transition is legal."""
    allowed = LEGAL_TRANSITIONS.get(current, set())
    return target in allowed


class TaskStateMachine:
    """
    Tracks and enforces state transitions for agent tasks.

    Maintains a per-task state history for debugging and audit.
    Can be configured to either raise or warn on illegal transitions.
    """

    def __init__(self, strict: bool = False):
        """
        Args:
            strict: If True, raise IllegalTransitionError on violations.
                    If False (default), log a warning but allow the transition.
        """
        self._strict = strict
        self._last_status: dict[str, TaskStatus] = {}  # task_id → last known status
        self._transition_log: list[dict] = []

    def check_transition(
        self, task_id: str, current: TaskStatus, target: TaskStatus
    ) -> bool:
        """
        Validate and record a state transition.

        Returns True if the transition is legal.
        Raises IllegalTransitionError if strict mode and transition is illegal.
        """
        is_legal = validate_transition(current, target)

        self._transition_log.append({
            "task_id": task_id,
            "from": current.value,
            "to": target.value,
            "legal": is_legal,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

        if not is_legal:
            msg = f"Illegal state transition for task {task_id}: {current.value} → {target.value}"
            if self._strict:
                raise IllegalTransitionError(current, target)
            else:
                logger.warning(msg)

        self._last_status[task_id] = target
        return is_legal

    def get_history(self, task_id: str) -> list[dict]:
        """Get the transition history for a task."""
        return [
            entry for entry in self._transition_log
            if entry["task_id"] == task_id
        ]

    def get_last_status(self, task_id: str) -> Optional[TaskStatus]:
        """Get the last known status for a task."""
        return self._last_status.get(task_id)
