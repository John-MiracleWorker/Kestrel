"""Provider evidence normalization tests (Adaptive Flock plan, Task 10).

One provider-agnostic normalization path turns raw provider receipts into
``ProviderAttemptEvidence``: typed failure categories, request ID digests,
accepted/ambiguous request state, token usage when available, snapshotted
prices, known/unresolved cost, and bounded/redacted failure detail. Provider
self-reports are never trusted — classification derives only from typed
receipt fields.
"""

from __future__ import annotations

import hashlib

import pytest

from nested_memvid_agent.routing.learned_router import (
    LearnedRouterConfig,
    LearnedRouterState,
    build_route_examples,
)
from nested_memvid_agent.routing.qualification_evidence import (
    FAILURE_CATEGORIES,
    ProviderAttemptEvidence,
    normalize_provider_attempt,
)


def timeout_receipt() -> dict[str, object]:
    return {
        "subject_id": "decision-timeout",
        "run_id": "run-evidence",
        "task_id": "task-evidence",
        "attempt": 1,
        "target_id": "target_a",
        "provider": "mock-provider",
        "profile_id": "profile-a",
        "model": "model-a",
        "request_id": "req-raw-123",
        "status": "provider_error",
        "error_code": "timeout",
        "error": "provider request timed out after 30s",
        "latency_seconds": 30.0,
        "task_family": "coding",
        "risk": "low",
        "contract_digest": "c" * 64,
        "created_at": "2026-08-01T00:00:00Z",
    }


def tool_call_mismatch() -> dict[str, object]:
    return {
        **timeout_receipt(),
        "subject_id": "decision-tool",
        "error_code": "unsupported_tool",
        "error": "tool schema mismatch: missing_tool_arguments",
    }


def invalid_task_contract() -> dict[str, object]:
    return {
        **timeout_receipt(),
        "subject_id": "decision-contract",
        "error_code": "invalid_task_contract",
        "error": "request violates the compiled task contract",
    }


def test_provider_outage_does_not_become_task_quality_failure() -> None:
    evidence = normalize_provider_attempt(timeout_receipt())
    assert evidence.failure_category == "provider_outage"
    examples = build_route_examples([evidence.to_learning_payload()])
    state = LearnedRouterState.from_examples(examples, LearnedRouterConfig())
    assert state.target_scores["target_a"].validation_rate == 0.0
    assert state.target_scores["target_a"].effective_sample_size == 0.0


def test_capability_and_contract_failures_remain_distinct() -> None:
    assert normalize_provider_attempt(tool_call_mismatch()).failure_category == "capability_failure"
    assert (
        normalize_provider_attempt(invalid_task_contract()).failure_category == "contract_failure"
    )


def test_rate_limit_is_distinct_from_outage_and_not_task_quality() -> None:
    receipt = {
        **timeout_receipt(),
        "subject_id": "decision-rate-limit",
        "error_code": "rate_limit",
        "error": "HTTP 429 too many requests",
    }
    evidence = normalize_provider_attempt(receipt)
    assert evidence.failure_category == "provider_rate_limit"
    examples = build_route_examples([evidence.to_learning_payload()])
    state = LearnedRouterState.from_examples(examples, LearnedRouterConfig())
    assert state.target_scores["target_a"].validation_rate == 0.0
    assert state.target_scores["target_a"].effective_sample_size == 0.0


def test_typed_failure_taxonomy_is_exact() -> None:
    assert FAILURE_CATEGORIES == frozenset(
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


def test_cost_is_known_only_from_usage_and_snapshotted_prices() -> None:
    receipt = {
        **timeout_receipt(),
        "subject_id": "decision-priced",
        "status": "completed",
        "error_code": None,
        "error": None,
        "validation_passed": True,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cached_tokens": 200,
        "reasoning_tokens": 100,
        "input_cost_per_million_usd": 2.0,
        "output_cost_per_million_usd": 8.0,
    }
    evidence = normalize_provider_attempt(receipt)
    assert evidence.failure_category is None
    assert evidence.request_state == "accepted"
    assert evidence.input_tokens == 1000
    assert evidence.output_tokens == 500
    assert evidence.cached_tokens == 200
    assert evidence.reasoning_tokens == 100
    assert evidence.input_cost_per_million_usd == 2.0
    assert evidence.output_cost_per_million_usd == 8.0
    assert evidence.actual_cost_usd == pytest.approx((1000 * 2.0 + 500 * 8.0) / 1_000_000.0)
    assert evidence.cost_state == "known"


def test_missing_prices_leave_cost_unresolved() -> None:
    receipt = {
        **timeout_receipt(),
        "subject_id": "decision-unpriced",
        "status": "completed",
        "error_code": None,
        "error": None,
        "validation_passed": True,
        "input_tokens": 1000,
        "output_tokens": 500,
    }
    evidence = normalize_provider_attempt(receipt)
    assert evidence.actual_cost_usd is None
    assert evidence.cost_state == "unresolved"


def test_request_id_is_digested_never_persisted_raw() -> None:
    evidence = normalize_provider_attempt(timeout_receipt())
    assert evidence.request_id_digest == hashlib.sha256(b"req-raw-123").hexdigest()
    assert "req-raw-123" not in str(evidence.to_payload())
    assert "req-raw-123" not in str(evidence.to_learning_payload())


def test_raw_provider_error_is_redacted_and_bounded() -> None:
    secret = "sk-live1234567890abcdef"
    receipt = {
        **timeout_receipt(),
        "error": f"upstream failure token={secret} " + ("x" * 1000),
    }
    evidence = normalize_provider_attempt(receipt)
    assert evidence.failure_detail is not None
    assert secret not in evidence.failure_detail
    assert secret not in str(evidence.to_payload())
    assert len(evidence.failure_detail) <= 240


def test_ambiguous_transport_outcome_keeps_cost_unresolved() -> None:
    receipt = {
        **timeout_receipt(),
        "subject_id": "decision-ambiguous",
        "error_code": "connection_reset",
        "error": "connection reset by peer",
        "latency_seconds": None,
        "actual_cost_usd": 0.004,
    }
    evidence = normalize_provider_attempt(receipt)
    assert evidence.failure_category == "provider_outage"
    assert evidence.request_state == "ambiguous"
    assert evidence.actual_cost_usd is None
    assert evidence.cost_state == "unresolved"


def test_confirmed_zero_token_transport_failure_is_definitive() -> None:
    receipt = {
        **timeout_receipt(),
        "subject_id": "decision-not-accepted",
        "error_code": "connection_reset",
        "error": "connection reset by peer",
        "input_tokens": 0,
        "output_tokens": 0,
        "zero_tokens_confirmed": True,
    }
    evidence = normalize_provider_attempt(receipt)
    assert evidence.request_state == "accepted"
    assert evidence.input_tokens == 0
    assert evidence.output_tokens == 0


def test_explicit_typed_category_is_preserved_and_untyped_rejected() -> None:
    receipt = {
        **timeout_receipt(),
        "error_code": None,
        "failure_category": "guardrail_failure",
    }
    assert normalize_provider_attempt(receipt).failure_category == "guardrail_failure"
    with pytest.raises(ValueError, match="failure_category"):
        normalize_provider_attempt({**receipt, "failure_category": "model_says_it_failed"})


def test_unrecognized_error_becomes_unknown_never_task_quality() -> None:
    receipt = {
        **timeout_receipt(),
        "error_code": "weird_upstream_blip",
    }
    evidence = normalize_provider_attempt(receipt)
    assert evidence.failure_category == "unknown"


def test_validated_success_counts_toward_task_quality() -> None:
    receipt = {
        **timeout_receipt(),
        "subject_id": "decision-success",
        "status": "completed",
        "error_code": None,
        "error": None,
        "validation_passed": True,
        "input_tokens": 10,
        "output_tokens": 5,
        "actual_cost_usd": 0.001,
        "latency_seconds": 1.5,
    }
    evidence = normalize_provider_attempt(receipt)
    assert evidence.failure_category is None
    examples = build_route_examples([evidence.to_learning_payload()])
    state = LearnedRouterState.from_examples(examples, LearnedRouterConfig())
    assert state.target_scores["target_a"].validation_rate == 1.0
    assert state.target_scores["target_a"].effective_sample_size == 1.0


def test_evidence_rejects_inconsistent_cost_state() -> None:
    base = normalize_provider_attempt(timeout_receipt())
    fields = {name: getattr(base, name) for name in base.__dataclass_fields__}
    fields["actual_cost_usd"] = None
    fields["cost_state"] = "known"
    with pytest.raises(ValueError, match="cost_state"):
        ProviderAttemptEvidence(**fields)
