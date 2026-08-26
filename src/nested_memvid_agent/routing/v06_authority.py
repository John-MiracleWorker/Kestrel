"""v0.6 learned-authority class guard (S11 / AUTH-002).

The only v0.6 learned-authority class is an exact, owner-activated, low-risk
summarizer scope.  This module defines that class and the guard predicates
used in two places:

* :class:`~nested_memvid_agent.routing.activation_service.ActivationService`
  rejects out-of-class scopes at owner activation time when the v0.6 class
  policy is enabled, and
* :class:`~nested_memvid_agent.routing.activation_evaluator.ActivationEvaluator`
  treats any resolved grant whose scope falls outside the class as never
  effective (a class restriction, not drift -- so it is not a suspension
  reason) when the v0.6 class policy is enabled.

The guard is opt-in (``v06_authority_class=True``) so the full Adaptive
Flock machinery built during the v0.5.x chain remains available to earlier
non-v0.6 flows; the v0.6 release wires the constrained class.  Enabling it
changes no capability boundary: an out-of-class grant is simply never
authoritative, and routing falls back to the deterministic static path with
a truthful ``v06_authority_class_restricted`` reason code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

#: Reason code emitted when a grant scope is outside the v0.6 class.
V06_AUTHORITY_CLASS_RESTRICTED = "v06_authority_class_restricted"

#: Risk levels permitted for the v0.6 learned-authority class.
V06_AUTHORITY_LOW_RISKS: frozenset[str] = frozenset({"low"})

#: Task families that are summarizer-class.  Mirrors the summarizer role
#: normalization used by the shadow-observation ledger so the class is
#: consistent across surfaces.
SUMMARIZER_TASK_FAMILIES: frozenset[str] = frozenset(
    {"summarizer", "summary", "summarization"}
)


def normalize_task_family(task_family: str | None) -> str:
    """Return the canonical lower-cased task family ('' for missing)."""
    if not isinstance(task_family, str):
        return ""
    return task_family.strip().lower()


def is_summarizer_family(task_family: str | None) -> bool:
    """True when the task family is a summarizer-class family."""
    return normalize_task_family(task_family) in SUMMARIZER_TASK_FAMILIES


def is_v06_authorized_scope(*, task_family: str | None, risk: str | None) -> bool:
    """True when the scope is inside the v0.6 learned-authority class.

    The class is intentionally narrow: ``risk == 'low'`` AND a summarizer
    task family.  Nothing else may ever carry v0.6 learned authority.
    """
    return (
        isinstance(risk, str)
        and risk.strip().lower() in V06_AUTHORITY_LOW_RISKS
        and is_summarizer_family(task_family)
    )


def scope_payload(scope_json: str) -> Mapping[str, object] | None:
    """Decode a grant scope payload defensively (None on any malformation)."""
    import json

    if not isinstance(scope_json, str):
        return None
    try:
        payload = json.loads(scope_json)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def scope_is_v06_authorized(scope_json: str) -> bool:
    """True when the JSON scope payload is inside the v0.6 class.

    A malformed or unreadable scope payload is never authorized (fail
    closed).
    """
    payload = scope_payload(scope_json)
    if payload is None:
        return False
    return is_v06_authorized_scope(
        task_family=str(payload.get("task_family") or ""),
        risk=str(payload.get("risk") or ""),
    )


def scope_class_digest(*, task_family: str | None, risk: str | None) -> str:
    """Deterministic digest of the authority-class decision.

    Binds the two class inputs so durable evidence can reference exactly
    which class was evaluated.  Not a security digest -- informational
    only.
    """
    canonical = "|".join(
        (
            normalize_task_family(task_family),
            str(risk or "").strip().lower(),
        )
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
