"""Authenticated terminal qualification receipts (Adaptive Flock plan, Task 12).

Every terminal qualification run (``completed``, ``failed``, or
``cancelled``) finalizes exactly one immutable ``run_terminal`` receipt whose
payload binds the run identity, every authority digest, the cap and spend
history, the case/attempt/failure/guardrail summaries linked to raw evidence
IDs, all per-scope metrics and reasons, and all twenty replay projection
digests.  The payload digest and a Task 3 HMAC-SHA256 authentication envelope
are embedded before persistence.

Invariants:

- Only a ``completed`` receipt may contain qualified scopes; a ``failed`` or
  ``cancelled`` receipt carrying a qualified scope is rejected, and a
  qualified scope additionally requires a passed replay (one unique
  projection digest across the required passes).
- Signing happens before the ledger transaction opens: if authentication
  fails nothing is persisted and no unsigned qualifying receipt is invented.
- Verification is total and fail-closed: it recomputes the payload digest
  over the exact body and verifies the envelope cryptographically.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..control_plane_integrity import ControlPlaneIntegrity
from .qualification_digest import canonical_digest
from .qualification_evaluator import ScopeQualificationResult
from .qualification_records import TERMINAL_RUN_STATES, QualificationRun
from .qualification_replay import ReplayResult

__all__ = [
    "TERMINAL_RECEIPT_SCHEMA",
    "authenticate_terminal_receipt",
    "build_terminal_receipt",
    "verify_terminal_receipt",
]

TERMINAL_RECEIPT_SCHEMA = "kestrel.flock_qualification_terminal_receipt.v1"
TERMINAL_RECEIPT_TYPE = "run_terminal"


def build_terminal_receipt(
    *,
    status: str,
    run: QualificationRun | None = None,
    terminal_reason: str = "",
    scopes: Sequence[ScopeQualificationResult] = (),
    replay: ReplayResult | None = None,
    attempt_summaries: Sequence[Mapping[str, Any]] = (),
    effective_cap_revisions: Sequence[Mapping[str, Any]] = (),
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the unsigned terminal receipt payload for one terminal run.

    The payload digest and authentication envelope are attached separately by
    :func:`authenticate_terminal_receipt` so signing can fail before any
    persistence is attempted.
    """

    if status not in TERMINAL_RUN_STATES:
        raise ValueError(f"unsupported terminal status: {status}")
    scope_results = tuple(scopes)
    for scope in scope_results:
        if not isinstance(scope, ScopeQualificationResult):
            raise ValueError("scopes must be ScopeQualificationResult values")
    if replay is not None and not isinstance(replay, ReplayResult):
        raise ValueError("replay must be a ReplayResult value")
    qualified = any(scope.qualified for scope in scope_results)
    if qualified and status != "completed":
        raise ValueError(f"{status} receipt cannot contain qualified scopes")
    if qualified and (replay is None or not replay.passed):
        raise ValueError("qualified scopes require a passed replay")
    summaries = [dict(summary) for summary in attempt_summaries]
    failure_summary: dict[str, int] = {}
    for summary in summaries:
        category = summary.get("failure_category")
        if isinstance(category, str) and category:
            failure_summary[category] = failure_summary.get(category, 0) + 1
    payload: dict[str, Any] = {
        "schema": TERMINAL_RECEIPT_SCHEMA,
        "status": status,
        "terminal_reason": terminal_reason,
        "qualifying": bool(qualified and status == "completed"),
        "run": _run_section(run),
        "digests": _digest_section(run),
        "caps": _cap_section(run, effective_cap_revisions),
        "spend": _spend_section(run),
        "attempts_terminal": len(summaries),
        "attempts_succeeded": sum(
            1
            for summary in summaries
            if summary.get("status") == "completed" and summary.get("validation_passed") is True
        ),
        "failure_summary": failure_summary,
        "guardrail_violations": sum(
            1 for summary in summaries if summary.get("guardrail_state") == "violated"
        ),
        "attempts": summaries,
        "scopes": [scope.to_payload() for scope in scope_results],
        "replay": _replay_section(replay),
        "details": dict(details) if details is not None else {},
    }
    return payload


def authenticate_terminal_receipt(
    payload: Mapping[str, Any],
    *,
    integrity: ControlPlaneIntegrity,
) -> dict[str, Any]:
    """Attach the payload digest and Task 3 authentication envelope.

    The envelope binds the payload digest, the receipt type, the run, the
    terminal status, and the schema.  Signing never mutates the input.
    """

    if not isinstance(payload, Mapping):
        raise ValueError("terminal receipt payload must be a mapping")
    if "authentication" in payload or "payload_digest" in payload:
        raise ValueError("terminal receipt is already authenticated")
    body = dict(payload)
    payload_digest = canonical_digest(body)
    run_section = body.get("run")
    run_id = ""
    if isinstance(run_section, Mapping):
        run_id = str(run_section.get("run_id") or "")
    envelope = integrity.sign(
        {
            "payload_digest": payload_digest,
            "receipt_type": TERMINAL_RECEIPT_TYPE,
            "run_id": run_id,
            "status": str(body.get("status") or ""),
            "schema": str(body.get("schema") or ""),
        }
    )
    return {
        **body,
        "payload_digest": payload_digest,
        "authentication": dict(envelope),
    }


def verify_terminal_receipt(
    receipt: Mapping[str, Any],
    *,
    integrity: ControlPlaneIntegrity,
) -> bool:
    """Verify an authenticated terminal receipt; total and fail-closed."""

    if not isinstance(receipt, Mapping):
        return False
    envelope = receipt.get("authentication")
    payload_digest = receipt.get("payload_digest")
    if not isinstance(envelope, Mapping) or not isinstance(payload_digest, str):
        return False
    body = {
        key: value
        for key, value in receipt.items()
        if key not in ("authentication", "payload_digest")
    }
    try:
        recomputed = canonical_digest(body)
    except (TypeError, ValueError):
        return False
    if not hmac.compare_digest(recomputed, payload_digest):
        return False
    if not integrity.verify(envelope):
        return False
    projection = envelope.get("payload")
    if not isinstance(projection, Mapping):
        return False
    if str(projection.get("receipt_type")) != TERMINAL_RECEIPT_TYPE:
        return False
    if str(projection.get("status")) != str(body.get("status") or ""):
        return False
    return hmac.compare_digest(str(projection.get("payload_digest")), payload_digest)


def _run_section(run: QualificationRun | None) -> dict[str, Any]:
    if run is None:
        return {}
    scope_payload = json.loads(run.scope_json)
    return {
        "run_id": run.run_id,
        "owner_principal": run.owner_principal,
        "project_id": str(scope_payload.get("project_id") or ""),
        "revision": run.revision,
        "build": json.loads(run.build_json),
        "build_digest": run.build_digest,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def _digest_section(run: QualificationRun | None) -> dict[str, Any]:
    if run is None:
        return {}
    return {
        "scope": run.scope_digest,
        "corpus": run.corpus_digest,
        "target": run.target_digest,
        "price": run.price_digest,
        "policy": run.policy_digest,
        "learned": run.learned_digest,
        "project_authority": run.project_authority_digest,
        "thresholds": run.thresholds_digest,
    }


def _cap_section(
    run: QualificationRun | None,
    effective_cap_revisions: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    section: dict[str, Any] = {
        "effective_cap_revisions": [dict(entry) for entry in effective_cap_revisions],
    }
    if run is not None:
        section.update(
            {
                "max_spend_micros": run.max_spend.micros,
                "effective_stop_cap_micros": run.effective_stop_cap.micros,
                "attempt_ceiling_micros": run.attempt_ceiling.micros,
            }
        )
    return section


def _spend_section(run: QualificationRun | None) -> dict[str, Any]:
    if run is None:
        return {}
    return {
        "actual_spend_micros": run.actual_spend.micros,
        "unresolved_reserve_micros": run.unresolved_reserve.micros,
        "inflight_reserve_micros": run.inflight_reserve.micros,
    }


def _replay_section(replay: ReplayResult | None) -> dict[str, Any] | None:
    if replay is None:
        return None
    return {
        "repeats": replay.repeats,
        "completed_repeats": replay.completed_repeats,
        "successes_required": replay.successes_required,
        "unique_projection_digests": replay.unique_projection_digests,
        "projection_digests": list(replay.projection_digests),
        "passed": replay.passed,
        "reasons": list(replay.reasons),
    }
