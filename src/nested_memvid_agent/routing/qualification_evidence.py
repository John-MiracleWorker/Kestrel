"""Normalized provider attempt evidence (Adaptive Flock plan, Task 10).

One provider-agnostic normalization path shared by ordinary Adaptive Flock
outcomes and qualification attempts. Raw provider receipts are converted into
``ProviderAttemptEvidence`` with:

- exact subject/provider/profile/model identity;
- a sha256 request ID digest (raw provider request IDs are never persisted);
- accepted/ambiguous request state (ambiguous transport outcomes keep cost
  unresolved, mirroring the budget ledger's reserve semantics);
- input/output/cached/reasoning tokens when available;
- snapshotted per-million token prices and known/unresolved cost;
- latency and a typed failure category;
- bounded, secret-redacted failure detail (raw provider errors are never
  persisted verbatim).

Provider self-reports are never trusted: failure classification derives only
from typed receipt fields (error codes/statuses), never from model-generated
text, and explicit categories outside the typed taxonomy are rejected.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from math import isfinite
from typing import Any

from ..security_boundary import redact_secrets

__all__ = [
    "COST_STATES",
    "FAILURE_CATEGORIES",
    "MAX_FAILURE_DETAIL_CHARS",
    "PROVIDER_SIDE_FAILURE_CATEGORIES",
    "REQUEST_STATES",
    "ProviderAttemptEvidence",
    "classify_failure_code",
    "normalize_provider_attempt",
]

FAILURE_CATEGORIES = frozenset(
    {
        "provider_outage",
        "provider_rate_limit",
        "capability_failure",
        "contract_failure",
        "task_quality_failure",
        "validation_failure",
        "guardrail_failure",
        "cancelled",
        "budget_rejected",
        "unknown",
    }
)

# Provider-side failures are never task-quality evidence: they are excluded
# from learned validation rates, effective sample sizes, and calibrations.
PROVIDER_SIDE_FAILURE_CATEGORIES = frozenset({"provider_outage", "provider_rate_limit"})

REQUEST_STATES = frozenset({"accepted", "ambiguous"})
COST_STATES = frozenset({"known", "unresolved"})

MAX_FAILURE_DETAIL_CHARS = 240

_SHA256_HEX = re.compile(r"[0-9a-f]{64}")

_CANCELLED_MARKERS = ("cancelled", "canceled")
_BUDGET_MARKERS = ("budget", "spend_cap", "cost_cap", "insufficient_funds")
_RATE_LIMIT_MARKERS = (
    "rate_limit",
    "ratelimit",
    "rate limit",
    "429",
    "too many requests",
    "quota",
    "throttl",
)
_OUTAGE_MARKERS = (
    "timeout",
    "timed out",
    "timed_out",
    "unavailable",
    "connection",
    "network",
    "overload",
    "transport",
    "endpoint_unreachable",
    "circuit_open",
    "dns",
    "refused",
    "reset",
    "500",
    "502",
    "503",
    "504",
)
_CONTRACT_MARKERS = ("contract",)
_VALIDATION_MARKERS = ("validation", "acceptance")
_GUARDRAIL_MARKERS = ("guardrail", "policy_violation", "privacy_block")
_QUALITY_MARKERS = ("task_quality", "quality_failure", "low_quality")
_CAPABILITY_MARKERS = (
    "unsupported",
    "capability",
    "context_length",
    "tool",
    "vision",
    "structured_output",
)

_SUCCESS_STATUSES = frozenset({"", "completed", "succeeded", "success", "ok"})


def classify_failure_code(code: str | None) -> str | None:
    """Map a typed provider failure code/status to the exact taxonomy.

    Returns ``None`` when no code is given, and ``"unknown"`` when a code is
    present but matches no typed marker. Classification never trusts
    free-form model self-reports; it matches typed marker tokens only.
    """
    if code is None:
        return None
    normalized = str(code).strip().lower()
    if not normalized:
        return None
    if any(marker in normalized for marker in _CANCELLED_MARKERS):
        return "cancelled"
    if any(marker in normalized for marker in _BUDGET_MARKERS):
        return "budget_rejected"
    if any(marker in normalized for marker in _RATE_LIMIT_MARKERS):
        return "provider_rate_limit"
    if any(marker in normalized for marker in _OUTAGE_MARKERS):
        return "provider_outage"
    if any(marker in normalized for marker in _CONTRACT_MARKERS):
        return "contract_failure"
    if any(marker in normalized for marker in _VALIDATION_MARKERS):
        return "validation_failure"
    if any(marker in normalized for marker in _GUARDRAIL_MARKERS):
        return "guardrail_failure"
    if any(marker in normalized for marker in _QUALITY_MARKERS):
        return "task_quality_failure"
    if any(marker in normalized for marker in _CAPABILITY_MARKERS):
        return "capability_failure"
    return "unknown"


@dataclass(frozen=True)
class ProviderAttemptEvidence:
    """Normalized, bounded evidence of one provider attempt."""

    subject_id: str
    run_id: str
    task_id: str
    attempt: int
    target_id: str
    provider: str
    profile_id: str
    model: str
    request_id_digest: str | None
    request_state: str
    validation_passed: bool
    execution_status: str
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    input_cost_per_million_usd: float | None
    output_cost_per_million_usd: float | None
    actual_cost_usd: float | None
    cost_state: str
    latency_seconds: float | None
    failure_category: str | None
    failure_detail: str | None
    task_family: str = ""
    risk: str = "low"
    contract_digest: str = ""
    project_id: str | None = None
    capability_key: str = "none"
    created_at: str = ""

    def __post_init__(self) -> None:
        for name in ("subject_id", "target_id", "provider", "model"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
        for name in ("run_id", "task_id", "profile_id"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 1:
            raise ValueError("attempt must be a positive integer")
        if self.request_id_digest is not None and (
            not isinstance(self.request_id_digest, str)
            or _SHA256_HEX.fullmatch(self.request_id_digest) is None
        ):
            raise ValueError("request_id_digest must be a lowercase sha256 hex digest")
        if self.request_state not in REQUEST_STATES:
            raise ValueError("request_state must be 'accepted' or 'ambiguous'")
        if self.cost_state not in COST_STATES:
            raise ValueError("cost_state must be 'known' or 'unresolved'")
        if (self.actual_cost_usd is None) != (self.cost_state == "unresolved"):
            raise ValueError("cost_state must be 'known' exactly when actual_cost_usd is set")
        for name in ("input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        for name in (
            "input_cost_per_million_usd",
            "output_cost_per_million_usd",
            "actual_cost_usd",
            "latency_seconds",
        ):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative number or None")
        if self.failure_category is not None and self.failure_category not in FAILURE_CATEGORIES:
            raise ValueError(
                f"failure_category must be one of {', '.join(sorted(FAILURE_CATEGORIES))}"
            )
        if self.failure_detail is not None and (
            not isinstance(self.failure_detail, str)
            or len(self.failure_detail) > MAX_FAILURE_DETAIL_CHARS
        ):
            raise ValueError(
                f"failure_detail must be bounded to {MAX_FAILURE_DETAIL_CHARS} characters"
            )

    @property
    def accepted(self) -> bool:
        """Whether the request disposition is definitive (not ambiguous)."""
        return self.request_state == "accepted"

    def to_learning_payload(self) -> dict[str, Any]:
        """Bounded payload consumable by ``build_route_examples``."""
        return {
            "decision_id": self.subject_id,
            "target_id": self.target_id,
            "validation_passed": self.validation_passed,
            "execution_status": self.execution_status,
            "failure_category": self.failure_category,
            "actual_cost_usd": self.actual_cost_usd,
            "latency_seconds": self.latency_seconds,
            "task_family": self.task_family,
            "risk": self.risk,
            "contract_digest": self.contract_digest,
            "project_id": self.project_id,
            "capability_key": self.capability_key,
            "created_at": self.created_at,
        }

    def to_payload(self) -> dict[str, Any]:
        return {
            "subject_id": self.subject_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "attempt": self.attempt,
            "target_id": self.target_id,
            "provider": self.provider,
            "profile_id": self.profile_id,
            "model": self.model,
            "request_id_digest": self.request_id_digest,
            "request_state": self.request_state,
            "validation_passed": self.validation_passed,
            "execution_status": self.execution_status,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "input_cost_per_million_usd": self.input_cost_per_million_usd,
            "output_cost_per_million_usd": self.output_cost_per_million_usd,
            "actual_cost_usd": self.actual_cost_usd,
            "cost_state": self.cost_state,
            "latency_seconds": self.latency_seconds,
            "failure_category": self.failure_category,
            "failure_detail": self.failure_detail,
            "task_family": self.task_family,
            "risk": self.risk,
            "contract_digest": self.contract_digest,
            "project_id": self.project_id,
            "capability_key": self.capability_key,
            "created_at": self.created_at,
        }


def normalize_provider_attempt(receipt: Any) -> ProviderAttemptEvidence:
    """Normalize one raw provider receipt into bounded attempt evidence.

    The receipt is a provider-agnostic mapping with optional keys:
    ``subject_id``/``decision_id``/``attempt_id``, ``run_id``, ``task_id`` (or
    ``case_id``), ``attempt``, ``target_id``, ``provider``, ``profile_id``,
    ``model``, ``request_id`` (digested, never persisted raw),
    ``request_state``, ``accepted``, ``zero_tokens_confirmed``, ``status`` (or
    ``execution_status``), ``validation_passed``, ``input_tokens`` (or
    ``prompt_tokens``), ``output_tokens`` (or ``completion_tokens``),
    ``cached_tokens``, ``reasoning_tokens``,
    ``input_cost_per_million_usd``/``output_cost_per_million_usd``,
    ``actual_cost_usd``, ``latency_seconds``, ``error_code`` (or
    ``provider_failure_code``/``code``), ``error`` (or ``error_message``),
    ``failure_category``, plus learning scope fields (``task_family``,
    ``risk``, ``contract_digest``, ``project_id``, ``capability_key``,
    ``created_at``).
    """
    if hasattr(receipt, "to_payload") and callable(receipt.to_payload):
        receipt = receipt.to_payload()
    if not isinstance(receipt, dict):
        raise ValueError("receipt must be a mapping of provider attempt fields")

    subject = receipt.get("subject_id") or receipt.get("decision_id") or receipt.get("attempt_id")
    status = str(receipt.get("status") or receipt.get("execution_status") or "")
    error_code = _optional_text(
        receipt.get("error_code") or receipt.get("provider_failure_code") or receipt.get("code")
    )
    error_text = _optional_text(receipt.get("error") or receipt.get("error_message"))

    explicit_category = receipt.get("failure_category")
    if explicit_category is not None:
        if explicit_category not in FAILURE_CATEGORIES:
            raise ValueError(
                "failure_category must be one of " + ", ".join(sorted(FAILURE_CATEGORIES))
            )
        failure_category: str | None = str(explicit_category)
    else:
        failure_category = _classify(status, error_code, error_text)

    input_tokens = _optional_non_negative_int(
        receipt.get("input_tokens", receipt.get("prompt_tokens"))
    )
    output_tokens = _optional_non_negative_int(
        receipt.get("output_tokens", receipt.get("completion_tokens"))
    )
    cached_tokens = _optional_non_negative_int(receipt.get("cached_tokens"))
    reasoning_tokens = _optional_non_negative_int(receipt.get("reasoning_tokens"))
    input_price = _optional_non_negative_float(receipt.get("input_cost_per_million_usd"))
    output_price = _optional_non_negative_float(receipt.get("output_cost_per_million_usd"))

    request_state = _resolve_request_state(
        receipt,
        failure_category=failure_category,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )

    actual_cost = _optional_non_negative_float(receipt.get("actual_cost_usd"))
    if actual_cost is None and (
        input_tokens is not None
        and output_tokens is not None
        and input_price is not None
        and output_price is not None
    ):
        actual_cost = round(
            (input_tokens * input_price + output_tokens * output_price) / 1_000_000.0,
            12,
        )
    if request_state == "ambiguous":
        # An ambiguous transport outcome keeps the reserve unresolved: cost
        # evidence is never trusted when acceptance is unproven.
        actual_cost = None
    cost_state = "known" if actual_cost is not None else "unresolved"

    request_id = _optional_text(receipt.get("request_id"))
    request_id_digest = (
        None if request_id is None else hashlib.sha256(request_id.encode("utf-8")).hexdigest()
    )

    failure_detail = None
    if error_text is not None:
        redacted = str(redact_secrets(error_text))
        failure_detail = redacted[:MAX_FAILURE_DETAIL_CHARS]

    attempt_raw = receipt.get("attempt", 1)
    attempt = (
        attempt_raw if isinstance(attempt_raw, int) and not isinstance(attempt_raw, bool) else 1
    )

    return ProviderAttemptEvidence(
        subject_id=str(subject or ""),
        run_id=str(receipt.get("run_id") or ""),
        task_id=str(receipt.get("task_id") or receipt.get("case_id") or ""),
        attempt=attempt,
        target_id=str(receipt.get("target_id") or ""),
        provider=str(receipt.get("provider") or ""),
        profile_id=str(receipt.get("profile_id") or receipt.get("selected_profile_id") or ""),
        model=str(receipt.get("model") or receipt.get("selected_model") or ""),
        request_id_digest=request_id_digest,
        request_state=request_state,
        validation_passed=bool(receipt.get("validation_passed", False)),
        execution_status=status,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        input_cost_per_million_usd=input_price,
        output_cost_per_million_usd=output_price,
        actual_cost_usd=actual_cost,
        cost_state=cost_state,
        latency_seconds=_optional_non_negative_float(receipt.get("latency_seconds")),
        failure_category=failure_category,
        failure_detail=failure_detail,
        task_family=str(receipt.get("task_family") or ""),
        risk=str(receipt.get("risk") or "low"),
        contract_digest=str(receipt.get("contract_digest") or ""),
        project_id=(None if receipt.get("project_id") is None else str(receipt.get("project_id"))),
        capability_key=str(receipt.get("capability_key") or "none"),
        created_at=str(receipt.get("created_at") or ""),
    )


def _classify(
    status: str,
    error_code: str | None,
    error_text: str | None,
) -> str | None:
    lowered_status = status.strip().lower()
    if lowered_status == "cancelled":
        return "cancelled"
    if lowered_status == "budget_rejected":
        return "budget_rejected"
    category = classify_failure_code(error_code)
    if category is not None:
        return category
    if error_code is not None or error_text is not None:
        return "unknown"
    if lowered_status in _SUCCESS_STATUSES:
        return None
    # A failed status without any typed code: classify from the status text
    # itself, else it is an unknown failure.
    return classify_failure_code(lowered_status) or "unknown"


def _resolve_request_state(
    receipt: dict[str, Any],
    *,
    failure_category: str | None,
    input_tokens: int | None,
    output_tokens: int | None,
) -> str:
    explicit = receipt.get("request_state")
    if explicit in REQUEST_STATES:
        return str(explicit)
    if receipt.get("accepted") is False:
        return "ambiguous"
    if failure_category in PROVIDER_SIDE_FAILURE_CATEGORIES:
        if receipt.get("zero_tokens_confirmed") is True:
            return "accepted"
        if input_tokens is not None or output_tokens is not None:
            return "accepted"
        return "ambiguous"
    return "accepted"


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _optional_non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _optional_non_negative_float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if isfinite(parsed) and parsed >= 0 else None
