from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .models import MemoryRecord
from .state_store import AgentStateStore

POLICY_PROMOTION_TOOL = "memory.policy_promote"
POLICY_APPROVAL_SCHEMA = "kestrel.policy_approval.v1"


def public_tool_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in arguments.items()
        if not str(key).startswith("_")
    }


def policy_arguments_digest(arguments: dict[str, Any]) -> str:
    encoded = json.dumps(
        public_tool_arguments(arguments),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def policy_approval_metadata(
    receipt: dict[str, Any],
    *,
    call_id: str,
    arguments: dict[str, Any],
    run_id: str | None,
) -> dict[str, str] | None:
    """Validate a durable exact-call receipt before recording its attestation."""

    decision = receipt.get("decision")
    public_arguments = public_tool_arguments(arguments)
    principal = str(receipt.get("principal") or "").strip()
    if (
        receipt.get("status") != "approved"
        or receipt.get("tool_name") != POLICY_PROMOTION_TOOL
        or receipt.get("tool_call_id") != call_id
        or str(receipt.get("run_id") or "") != str(run_id or "")
        or receipt.get("arguments") != public_arguments
        or principal != "owner"
        or not isinstance(decision, dict)
        or decision.get("approved") is not True
        or decision.get("arguments") != public_arguments
        or decision.get("principal") != principal
    ):
        return None
    approval_id = str(receipt.get("approval_id") or "").strip()
    if not approval_id:
        return None
    return {
        "schema": POLICY_APPROVAL_SCHEMA,
        "approval_id": approval_id,
        "run_id": str(run_id or ""),
        "tool_name": POLICY_PROMOTION_TOOL,
        "tool_call_id": call_id,
        "principal": principal,
        "arguments_sha256": policy_arguments_digest(public_arguments),
    }


def durable_policy_approval_authenticates(
    record: MemoryRecord,
    *,
    state_path: Path,
) -> bool:
    """Cross-check policy memory against the durable approval and its result."""

    provenance = record.metadata.get("approval_provenance")
    if not isinstance(provenance, dict) or provenance.get("schema") != POLICY_APPROVAL_SCHEMA:
        return False
    approval_id = str(provenance.get("approval_id") or "")
    if not approval_id:
        return False
    try:
        approval = AgentStateStore(state_path).get_approval(approval_id, expire=False)
    except Exception:  # noqa: BLE001 - authentication fails closed on any state error
        return False
    decision = approval.get("decision")
    result = approval.get("result")
    arguments = approval.get("arguments")
    principal = str(approval.get("principal") or "")
    if (
        approval.get("status") != "approved"
        or approval.get("tool_name") != POLICY_PROMOTION_TOOL
        or approval.get("tool_call_id") != provenance.get("tool_call_id")
        or approval.get("run_id") != provenance.get("run_id")
        or principal != "owner"
        or principal != provenance.get("principal")
        or not isinstance(arguments, dict)
        or policy_arguments_digest(arguments) != provenance.get("arguments_sha256")
        or not isinstance(decision, dict)
        or decision.get("approved") is not True
        or decision.get("arguments") != arguments
        or decision.get("principal") != principal
        or not isinstance(result, dict)
        or result.get("success") is not True
        or result.get("tool") != POLICY_PROMOTION_TOOL
        or result.get("tool_call_id") != provenance.get("tool_call_id")
    ):
        return False
    result_data = result.get("data")
    if (
        not isinstance(result_data, dict)
        or result_data.get("record_id") != record.id
        or result_data.get("record_content_hash") != record.content_hash
    ):
        return False
    return (
        arguments.get("title") == record.title
        and arguments.get("content") == record.content
    )
