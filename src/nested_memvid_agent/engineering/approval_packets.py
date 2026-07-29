from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from ..security_boundary import redact_secrets, redact_text
from ..state_store import AgentStateStore, utc_now
from .schema import ensure_engineering_schema

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,191}\Z")
_TOOL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,159}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_RISKS = {"low", "medium", "high", "critical"}


@dataclass(frozen=True)
class ApprovalPacketCall:
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    risk: str
    capability_revision: int
    resource_digest: str
    reason: str
    resource_scope: str
    expected_side_effect: str
    rollback: str


@dataclass(frozen=True)
class ApprovalPacketCallRecord:
    packet_call_id: str
    packet_id: str
    run_id: str
    ordinal: int
    tool_call_id: str
    tool_name: str
    arguments: dict[str, Any]
    call_digest: str
    risk: str
    capability_revision: int
    resource_digest: str
    reason: str
    resource_scope: str
    expected_side_effect: str
    rollback: str
    status: str
    decision: dict[str, Any]
    created_at: str
    decided_at: str | None
    consumed_at: str | None

    def to_payload(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class ApprovalPacketRecord:
    packet_id: str
    run_id: str
    objective: str
    checkpoint: str
    packet_digest: str
    status: str
    actor: str
    decided_by: str | None
    created_at: str
    decided_at: str | None
    calls: tuple[ApprovalPacketCallRecord, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "packet_id": self.packet_id,
            "run_id": self.run_id,
            "objective": self.objective,
            "checkpoint": self.checkpoint,
            "packet_digest": self.packet_digest,
            "status": self.status,
            "actor": self.actor,
            "decided_by": self.decided_by,
            "created_at": self.created_at,
            "decided_at": self.decided_at,
            "calls": [item.to_payload() for item in self.calls],
            "authorization_record_count": len(self.calls),
        }


class ApprovalPacketService:
    """Group display only; preserve one digest-bound authorization per exact call."""

    def __init__(self, state: AgentStateStore) -> None:
        self.state = state
        ensure_engineering_schema(state)

    def create(
        self,
        *,
        packet_id: str,
        run_id: str,
        objective: str,
        calls: tuple[ApprovalPacketCall, ...],
        actor: str,
        checkpoint: str = "",
    ) -> ApprovalPacketRecord:
        packet_key = _id(packet_id, "packet_id")
        run_key = _id(run_id, "run_id")
        objective_text = _text(objective, "objective", 4000)
        actor_text = _text(actor, "actor", 160)
        checkpoint_text = _optional_text(checkpoint, "checkpoint", 1000)
        if not 1 <= len(calls) <= 32:
            raise ValueError("approval packet requires between one and 32 exact calls")
        normalized = tuple(_normalize_call(item) for item in calls)
        call_ids = [item["tool_call_id"] for item in normalized]
        if len(set(call_ids)) != len(call_ids):
            raise ValueError("approval packet tool call ids must be unique")
        packet_payload = {
            "schema": "kestrel.approval_packet.v1",
            "run_id": run_key,
            "objective": objective_text,
            "checkpoint": checkpoint_text,
            "calls": [
                {
                    "ordinal": index,
                    "tool_call_id": item["tool_call_id"],
                    "call_digest": item["call_digest"],
                }
                for index, item in enumerate(normalized)
            ],
        }
        packet_digest = _hash(packet_payload)
        run = self.state.get_run(run_key)
        if run.status in {"completed", "failed", "cancelled"}:
            raise ValueError("terminal runs cannot create approval packets")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM approval_packets WHERE packet_id = ?",
                (packet_key,),
            ).fetchone()
            if existing is not None:
                current = self.get(packet_key)
                if (
                    current.run_id,
                    current.objective,
                    current.checkpoint,
                    current.packet_digest,
                    current.actor,
                ) != (
                    run_key,
                    objective_text,
                    checkpoint_text,
                    packet_digest,
                    actor_text,
                ):
                    raise ValueError("approval_packet_identity_conflict")
                return current
            conn.execute(
                """
                INSERT INTO approval_packets (
                    packet_id, run_id, objective, checkpoint, packet_digest,
                    status, actor, decided_by, created_at, decided_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', ?, NULL, ?, NULL)
                """,
                (
                    packet_key,
                    run_key,
                    objective_text,
                    checkpoint_text,
                    packet_digest,
                    actor_text,
                    now,
                ),
            )
            for index, item in enumerate(normalized):
                packet_call_id = "packet_call_" + hashlib.sha256(
                    f"{packet_key}:{index}:{item['call_digest']}".encode()
                ).hexdigest()[:24]
                conn.execute(
                    """
                    INSERT INTO approval_packet_calls (
                        packet_call_id, packet_id, run_id, ordinal,
                        tool_call_id, tool_name, arguments_json, call_digest,
                        risk, capability_revision, resource_digest, reason,
                        resource_scope, expected_side_effect, rollback, status,
                        decision_json, created_at, decided_at, consumed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'pending', '{}', ?, NULL, NULL)
                    """,
                    (
                        packet_call_id,
                        packet_key,
                        run_key,
                        index,
                        item["tool_call_id"],
                        item["tool_name"],
                        _json(item["arguments"]),
                        item["call_digest"],
                        item["risk"],
                        item["capability_revision"],
                        item["resource_digest"],
                        item["reason"],
                        item["resource_scope"],
                        item["expected_side_effect"],
                        item["rollback"],
                        now,
                    ),
                )
        return self.get(packet_key)

    def decide(
        self,
        packet_id: str,
        *,
        expected_packet_digest: str,
        decisions: dict[str, bool],
        actor: str,
    ) -> ApprovalPacketRecord:
        packet_key = _id(packet_id, "packet_id")
        expected = _digest(expected_packet_digest, "expected_packet_digest")
        actor_text = _text(actor, "actor", 160)
        if not isinstance(decisions, dict) or not all(
            isinstance(key, str) and isinstance(value, bool)
            for key, value in decisions.items()
        ):
            raise ValueError("approval packet decisions must map call ids to booleans")
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            packet_row = conn.execute(
                "SELECT * FROM approval_packets WHERE packet_id = ?",
                (packet_key,),
            ).fetchone()
            if packet_row is None:
                raise KeyError(f"Unknown approval packet: {packet_key}")
            if str(packet_row["status"]) != "pending":
                raise ValueError("approval packet was already decided")
            if str(packet_row["packet_digest"]) != expected:
                raise ValueError("approval packet digest does not match the displayed packet")
            rows = conn.execute(
                """
                SELECT * FROM approval_packet_calls
                WHERE packet_id = ?
                ORDER BY ordinal ASC
                """,
                (packet_key,),
            ).fetchall()
            call_ids = {str(row["tool_call_id"]) for row in rows}
            if set(decisions) != call_ids:
                raise ValueError("every displayed exact call requires an individual decision")
            for row in rows:
                tool_call_id = str(row["tool_call_id"])
                approved = decisions[tool_call_id]
                conn.execute(
                    """
                    UPDATE approval_packet_calls
                    SET status = ?, decision_json = ?, decided_at = ?
                    WHERE packet_call_id = ? AND status = 'pending'
                    """,
                    (
                        "approved" if approved else "denied",
                        _json(
                            {
                                "approved": approved,
                                "principal": actor_text,
                                "call_digest": str(row["call_digest"]),
                            }
                        ),
                        now,
                        str(row["packet_call_id"]),
                    ),
                )
            conn.execute(
                """
                UPDATE approval_packets
                SET status = 'decided', decided_by = ?, decided_at = ?
                WHERE packet_id = ? AND status = 'pending'
                """,
                (actor_text, now, packet_key),
            )
        return self.get(packet_key)

    def consume_exact(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        risk: str,
        capability_revision: int,
        resource_digest: str,
    ) -> ApprovalPacketCallRecord | None:
        run_key = _id(run_id, "run_id")
        call_id = _id(tool_call_id, "tool_call_id")
        actual = _normalize_call(
            ApprovalPacketCall(
                tool_call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                risk=risk,
                capability_revision=capability_revision,
                resource_digest=resource_digest,
                reason="runtime revalidation",
                resource_scope="runtime revalidation",
                expected_side_effect="runtime revalidation",
                rollback="runtime revalidation",
            )
        )
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT c.*, p.status AS packet_status
                FROM approval_packet_calls AS c
                JOIN approval_packets AS p ON p.packet_id = c.packet_id
                WHERE c.run_id = ? AND c.tool_call_id = ?
                """,
                (run_key, call_id),
            ).fetchone()
            if row is None or str(row["status"]) != "approved":
                return None
            expected_identity = (
                str(row["tool_name"]),
                dict(_load(row["arguments_json"], {})),
                str(row["risk"]),
                int(row["capability_revision"]),
                str(row["resource_digest"]),
                str(row["call_digest"]),
            )
            actual_identity = (
                actual["tool_name"],
                actual["arguments"],
                actual["risk"],
                actual["capability_revision"],
                actual["resource_digest"],
                actual["call_digest"],
            )
            if str(row["packet_status"]) != "decided" or expected_identity != actual_identity:
                decision = {
                    "approved": False,
                    "reason": "exact_call_binding_changed",
                    "revalidated_at": now,
                }
                conn.execute(
                    """
                    UPDATE approval_packet_calls
                    SET status = 'invalidated', decision_json = ?, consumed_at = ?
                    WHERE packet_call_id = ? AND status = 'approved'
                    """,
                    (_json(decision), now, str(row["packet_call_id"])),
                )
                return None
            cursor = conn.execute(
                """
                UPDATE approval_packet_calls
                SET status = 'consumed', consumed_at = ?
                WHERE packet_call_id = ? AND status = 'approved'
                """,
                (now, str(row["packet_call_id"])),
            )
            if cursor.rowcount != 1:
                return None
            consumed = conn.execute(
                "SELECT * FROM approval_packet_calls WHERE packet_call_id = ?",
                (str(row["packet_call_id"]),),
            ).fetchone()
        if consumed is None:
            raise RuntimeError("approval_packet_consumption_lost")
        return _call_record(consumed)

    def invalidate_tools(self, tool_names: set[str], *, reason: str) -> int:
        names = sorted({_tool_name(item) for item in tool_names})
        if not names:
            return 0
        reason_text = _text(reason, "reason", 1000)
        placeholders = ",".join("?" for _ in names)
        now = utc_now()
        with self.state._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                f"""
                SELECT packet_call_id FROM approval_packet_calls
                WHERE tool_name IN ({placeholders})
                    AND status IN ('pending', 'approved')
                """,
                names,
            ).fetchall()
            ids = [str(row["packet_call_id"]) for row in rows]
            if not ids:
                return 0
            id_placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"""
                UPDATE approval_packet_calls
                SET status = 'invalidated', decision_json = ?, consumed_at = ?
                WHERE packet_call_id IN ({id_placeholders})
                    AND status IN ('pending', 'approved')
                """,
                (
                    _json({"approved": False, "reason": reason_text}),
                    now,
                    *ids,
                ),
            )
        return len(ids)

    def get(self, packet_id: str) -> ApprovalPacketRecord:
        with self.state._connect() as conn:
            row = conn.execute(
                "SELECT * FROM approval_packets WHERE packet_id = ?",
                (packet_id,),
            ).fetchone()
            calls = conn.execute(
                """
                SELECT * FROM approval_packet_calls
                WHERE packet_id = ?
                ORDER BY ordinal ASC
                """,
                (packet_id,),
            ).fetchall()
        if row is None:
            raise KeyError(f"Unknown approval packet: {packet_id}")
        return _packet_record(row, tuple(_call_record(item) for item in calls))

    def list(self, *, run_id: str) -> list[ApprovalPacketRecord]:
        with self.state._connect() as conn:
            rows = conn.execute(
                """
                SELECT packet_id FROM approval_packets
                WHERE run_id = ?
                ORDER BY created_at ASC, packet_id ASC
                """,
                (run_id,),
            ).fetchall()
        return [self.get(str(row["packet_id"])) for row in rows]


def _normalize_call(call: ApprovalPacketCall) -> dict[str, Any]:
    call_id = _id(call.tool_call_id, "tool_call_id")
    tool_name = _tool_name(call.tool_name)
    arguments = _arguments(call.arguments)
    risk = str(call.risk).strip().lower()
    if risk not in _RISKS:
        raise ValueError("approval packet risk is invalid")
    if (
        isinstance(call.capability_revision, bool)
        or not isinstance(call.capability_revision, int)
        or not 0 <= call.capability_revision <= 2**63 - 1
    ):
        raise ValueError("approval packet capability revision is invalid")
    resource_digest = _digest(call.resource_digest, "resource_digest")
    identity = {
        "tool_call_id": call_id,
        "tool_name": tool_name,
        "arguments": arguments,
        "risk": risk,
        "capability_revision": call.capability_revision,
        "resource_digest": resource_digest,
    }
    return {
        **identity,
        "call_digest": _hash(identity),
        "reason": _text(call.reason, "reason", 2000),
        "resource_scope": _text(call.resource_scope, "resource_scope", 2000),
        "expected_side_effect": _text(
            call.expected_side_effect,
            "expected_side_effect",
            2000,
        ),
        "rollback": _text(call.rollback, "rollback", 2000),
    }


def _arguments(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("approval packet arguments must be an object")
    encoded = _json(value)
    if len(encoded) > 128_000:
        raise ValueError("approval packet arguments exceed the 128 KiB bound")
    loaded = json.loads(encoded)
    if not isinstance(loaded, dict):
        raise ValueError("approval packet arguments must be an object")
    redacted = redact_secrets(loaded)
    if redacted != loaded:
        raise ValueError(
            "approval packet arguments contain sensitive material and cannot be persisted"
        )
    return loaded


def _packet_record(
    row: Any,
    calls: tuple[ApprovalPacketCallRecord, ...],
) -> ApprovalPacketRecord:
    return ApprovalPacketRecord(
        packet_id=str(row["packet_id"]),
        run_id=str(row["run_id"]),
        objective=str(row["objective"]),
        checkpoint=str(row["checkpoint"]),
        packet_digest=str(row["packet_digest"]),
        status=str(row["status"]),
        actor=str(row["actor"]),
        decided_by=_optional(row["decided_by"]),
        created_at=str(row["created_at"]),
        decided_at=_optional(row["decided_at"]),
        calls=calls,
    )


def _call_record(row: Any) -> ApprovalPacketCallRecord:
    return ApprovalPacketCallRecord(
        packet_call_id=str(row["packet_call_id"]),
        packet_id=str(row["packet_id"]),
        run_id=str(row["run_id"]),
        ordinal=int(row["ordinal"]),
        tool_call_id=str(row["tool_call_id"]),
        tool_name=str(row["tool_name"]),
        arguments=dict(_load(row["arguments_json"], {})),
        call_digest=str(row["call_digest"]),
        risk=str(row["risk"]),
        capability_revision=int(row["capability_revision"]),
        resource_digest=str(row["resource_digest"]),
        reason=str(row["reason"]),
        resource_scope=str(row["resource_scope"]),
        expected_side_effect=str(row["expected_side_effect"]),
        rollback=str(row["rollback"]),
        status=str(row["status"]),
        decision=dict(_load(row["decision_json"], {})),
        created_at=str(row["created_at"]),
        decided_at=_optional(row["decided_at"]),
        consumed_at=_optional(row["consumed_at"]),
    )


def _id(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _IDENTIFIER.fullmatch(text) is None:
        raise ValueError(f"{field} has an invalid identifier")
    return text


def _tool_name(value: Any) -> str:
    text = str(value or "").strip()
    if _TOOL_NAME.fullmatch(text) is None:
        raise ValueError("approval packet tool name is invalid")
    return text


def _digest(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if _SHA256.fullmatch(text) is None:
        raise ValueError(f"{field} must be a SHA-256 digest")
    return text


def _text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > limit:
        raise ValueError(f"{field} is required and bounded to {limit} characters")
    if any(
        ord(character) < 32 and character not in "\n\r\t"
        for character in text
    ):
        raise ValueError(f"{field} contains invalid control characters")
    if redact_text(text) != text:
        raise ValueError(f"{field} contains sensitive material")
    return text


def _optional_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        raise ValueError(f"{field} exceeds {limit} characters")
    if redact_text(text) != text:
        raise ValueError(f"{field} contains sensitive material")
    return text


def _json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _hash(value: Any) -> str:
    try:
        encoded = _json(value)
    except (RecursionError, TypeError, ValueError) as exc:
        raise ValueError("approval packet value must be finite JSON") from exc
    return hashlib.sha256(encoded.encode()).hexdigest()


def _load(value: Any, default: Any) -> Any:
    return default if value is None else json.loads(str(value))


def _optional(value: Any) -> str | None:
    return None if value is None else str(value)
