from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timezone
from typing import Any

import grpc

from core import runtime
from core.grpc_setup import brain_pb2
from db import get_pool
from .base import BaseServicerMixin

logger = logging.getLogger("brain.services.operator")

_TERMINAL_TASK_STATUSES = {"complete", "failed", "cancelled"}
_ACTIVE_QUEUE_STATUSES = {"queued", "running", "paused"}


def _iso(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _load_jsonb(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return dict(value) if value else {}


def _load_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []
    return list(value) if value else []


def _receipt_id_from_payload(payload: dict[str, Any]) -> str:
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    execution = metadata.get("execution")
    if not isinstance(execution, dict):
        return ""
    receipt = execution.get("receipt")
    if not isinstance(receipt, dict):
        return ""
    return str(receipt.get("receipt_id") or "")


def _progress_from_plan(plan: dict[str, Any]) -> tuple[str, str]:
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        return "", ""

    total = len(steps)
    complete_statuses = {"complete", "skipped"}
    done = sum(
        1
        for step in steps
        if isinstance(step, dict) and str(step.get("status", "")).lower() in complete_statuses
    )
    current = total if done >= total else done + 1
    return str(current), str(total)


def _build_recovery_hints(
    *,
    status: str,
    stale: bool,
    orphaned: bool,
    pending_approval_id: str,
    last_checkpoint_id: str,
) -> list[Any]:
    hints: list[Any] = []

    if pending_approval_id:
        hints.append(
            brain_pb2.RecoveryHint(
                code="approval_pending",
                title="Approval required",
                description="This task is waiting on an approval decision before it can continue.",
            )
        )
    if orphaned:
        hints.append(
            brain_pb2.RecoveryHint(
                code="orphaned_execution",
                title="Lease expired",
                description="The queue lease expired while the task was still marked running. Requeue or inspect worker health.",
            )
        )
    elif stale:
        hints.append(
            brain_pb2.RecoveryHint(
                code="stalled_queue",
                title="Task appears stalled",
                description="The task has active queue state with a stale lease or no recent progress.",
            )
        )
    if status == "failed":
        hints.append(
            brain_pb2.RecoveryHint(
                code="review_failure",
                title="Review failure trace",
                description="Inspect the recent timeline and execution trace summary before retrying this task.",
            )
        )
    if last_checkpoint_id:
        hints.append(
            brain_pb2.RecoveryHint(
                code="checkpoint_available",
                title="Checkpoint available",
                description="A recent checkpoint exists for this task and can be used for recovery or inspection.",
            )
        )
    return hints


