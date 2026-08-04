"""Canonical Flock qualification value models (Adaptive Flock plan, Task 1)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from nested_memvid_agent.routing import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    PriceSnapshot,
    QualificationScope,
    QualificationThresholds,
    TargetSnapshot,
    canonical_digest,
    canonical_json,
)


def qualification_scope(
    *,
    capabilities: tuple[str, ...] = ("tools", "json"),
    target_ids: tuple[str, ...] = ("target_a", "target_b"),
    **overrides: object,
) -> QualificationScope:
    fields: dict[str, object] = {
        "project_id": "project_a",
        "task_family": "coding",
        "risk": "low",
        "capabilities": capabilities,
        "policy_id": "policy_balanced",
        "policy_revision": 7,
        "target_ids": target_ids,
        "target_inventory_digest": "1" * 64,
        "price_digest": "2" * 64,
        "learned_config_digest": "3" * 64,
        "project_authority_digest": "a" * 64,
    }
    fields.update(overrides)
    return QualificationScope(**fields)  # type: ignore[arg-type]


def test_money_uses_exact_micro_usd() -> None:
    assert MoneyMicros.from_usd_text("50.00").micros == 50_000_000
    assert MoneyMicros.from_usd_text("0.000001").micros == 1
    with pytest.raises(ValueError, match="at most six decimal places"):
        MoneyMicros.from_usd_text("0.0000001")


def test_scope_digest_is_order_independent_but_authority_sensitive() -> None:
    first = qualification_scope(
        capabilities=("tools", "json"),
        target_ids=("target_b", "target_a"),
    )
    second = qualification_scope(
        capabilities=("json", "tools"),
        target_ids=("target_a", "target_b"),
    )
    assert first.digest == second.digest
    assert replace(first, project_authority_digest="b" * 64).digest != first.digest


def test_money_rejects_bool_negative_and_fractional() -> None:
    with pytest.raises(ValueError, match="non-negative integer micro-USD"):
        MoneyMicros(-1)
    with pytest.raises(ValueError, match="non-negative integer micro-USD"):
        MoneyMicros(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="non-negative integer micro-USD"):
        MoneyMicros(1.5)  # type: ignore[arg-type]


def test_money_from_usd_text_parses_without_floating_point() -> None:
    assert MoneyMicros.from_usd_text("1").micros == 1_000_000
    assert MoneyMicros.from_usd_text("0.1").micros == 100_000
    assert MoneyMicros.from_usd_text("12.345678").micros == 12_345_678
    assert MoneyMicros.from_usd_text("0").micros == 0
    with pytest.raises(ValueError, match="invalid USD amount"):
        MoneyMicros.from_usd_text("abc")
    with pytest.raises(ValueError, match="invalid USD amount"):
        MoneyMicros.from_usd_text("-1.00")
    with pytest.raises(ValueError, match="invalid USD amount"):
        MoneyMicros.from_usd_text("")


def test_canonical_json_sorts_maps_and_semantic_sets_preserving_lists() -> None:
    payload = {"b": 1, "a": {"z", "y"}, "list": [2, 1]}
    assert canonical_json(payload) == '{"a":["y","z"],"b":1,"list":[2,1]}'
    assert canonical_digest(payload) == canonical_digest({"a": {"y", "z"}, "list": [2, 1], "b": 1})
    assert canonical_digest({"list": [1, 2]}) != canonical_digest({"list": [2, 1]})


def test_canonical_json_rejects_nan_and_infinity() -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"value": float("inf")})


def test_thresholds_match_required_defaults() -> None:
    thresholds = QualificationThresholds()
    assert thresholds.min_examples_per_scope == 5
    assert thresholds.min_examples_per_target == 3
    assert thresholds.confidence_threshold == 0.70
    assert thresholds.utility_margin == 0.08
    assert thresholds.cost_coverage_threshold == 0.80
    assert thresholds.decay_half_life_days == 30
    assert thresholds.max_guardrail_violations == 0
    assert thresholds.replay_runs == 20
    assert thresholds.replay_successes_required == 20


def test_price_snapshot_unknown_is_never_zero() -> None:
    with pytest.raises(ValueError, match="unknown price source cannot carry a price"):
        PriceSnapshot(
            target_id="target_a",
            source="unknown",
            captured_at="2026-07-29T00:00:00Z",
            input_per_million=MoneyMicros(0),
            output_per_million=MoneyMicros(0),
        )


def test_price_snapshot_explicit_non_billed_local_is_known_zero_with_provenance() -> None:
    zero = PriceSnapshot(
        target_id="local_qwen",
        source="operator_confirmed_non_billed_local",
        captured_at="2026-07-29T00:00:00Z",
        input_per_million=MoneyMicros(0),
        output_per_million=MoneyMicros(0),
        confirmed_by="owner",
        confirmed_at="2026-07-29T00:00:00Z",
    )
    assert zero.is_known_zero
    assert zero.is_trustworthy
    with pytest.raises(ValueError, match="non-billed local price must be zero"):
        PriceSnapshot(
            target_id="local_qwen",
            source="operator_confirmed_non_billed_local",
            captured_at="2026-07-29T00:00:00Z",
            input_per_million=MoneyMicros(1),
            output_per_million=MoneyMicros(0),
            confirmed_by="owner",
            confirmed_at="2026-07-29T00:00:00Z",
        )
    with pytest.raises(ValueError, match="non-billed local price requires owner/time provenance"):
        PriceSnapshot(
            target_id="local_qwen",
            source="operator_confirmed_non_billed_local",
            captured_at="2026-07-29T00:00:00Z",
            input_per_million=MoneyMicros(0),
            output_per_million=MoneyMicros(0),
        )


def test_price_snapshot_billed_sources_require_prices() -> None:
    with pytest.raises(ValueError, match="billed price source requires input and output prices"):
        PriceSnapshot(
            target_id="cloud_frontier",
            source="provider_published",
            captured_at="2026-07-29T00:00:00Z",
        )
    priced = PriceSnapshot(
        target_id="cloud_frontier",
        source="operator_verified",
        captured_at="2026-07-29T00:00:00Z",
        input_per_million=MoneyMicros(2_000_000),
        output_per_million=MoneyMicros(6_000_000),
    )
    assert not priced.is_known_zero
    assert priced.is_trustworthy
    assert (
        priced.digest
        == PriceSnapshot(
            target_id="cloud_frontier",
            source="operator_verified",
            captured_at="2026-07-29T00:00:00Z",
            input_per_million=MoneyMicros(2_000_000),
            output_per_million=MoneyMicros(6_000_000),
        ).digest
    )


def target_snapshot(**overrides: object) -> TargetSnapshot:
    fields: dict[str, object] = {
        "target_id": "target_a",
        "provider_profile_id": "profile_a",
        "adapter": "ollama",
        "model": "qwen3",
        "endpoint": "http://127.0.0.1:11434",
        "trust_class": "standard",
        "locality": "local",
        "enabled": True,
        "health": "healthy",
        "capabilities": ("tools", "json"),
        "privacy_class": "local_required",
        "max_context_tokens": 32_768,
        "price": PriceSnapshot(
            target_id="target_a",
            source="operator_confirmed_non_billed_local",
            captured_at="2026-07-29T00:00:00Z",
            input_per_million=MoneyMicros(0),
            output_per_million=MoneyMicros(0),
            confirmed_by="owner",
            confirmed_at="2026-07-29T00:00:00Z",
        ),
        "config_digest": "c" * 64,
    }
    fields.update(overrides)
    return TargetSnapshot(**fields)  # type: ignore[arg-type]


def test_target_snapshot_canonicalizes_capability_order() -> None:
    first = target_snapshot(capabilities=("tools", "json"))
    second = target_snapshot(capabilities=("json", "tools"))
    assert first == second
    assert first.digest == second.digest
    assert first.capabilities == ("json", "tools")
    assert replace(first, model="other-model").digest != first.digest


def corpus_item(item_id: str = "item_a", **overrides: object) -> CorpusItem:
    fields: dict[str, object] = {
        "item_id": item_id,
        "task_family": "coding",
        "risk": "low",
        "capabilities": ("tools", "json"),
        "task_contract_digest": "d" * 64,
        "acceptance_plan_digest": "e" * 64,
        "evidence_kind": "synthetic",
        "actionable": True,
    }
    fields.update(overrides)
    return CorpusItem(**fields)  # type: ignore[arg-type]


def test_corpus_manifest_digest_is_order_independent_over_items() -> None:
    item_a = corpus_item("item_a")
    item_b = corpus_item("item_b", evidence_kind="real_project")
    first = CorpusManifest(schema_version=1, items=(item_b, item_a))
    second = CorpusManifest(schema_version=1, items=(item_a, item_b))
    assert first.items == (item_a, item_b)
    assert first.digest == second.digest
    assert first.digest != CorpusManifest(schema_version=1, items=(item_a,)).digest


def test_scope_cross_project_or_risk_or_capability_change_changes_digest() -> None:
    baseline = qualification_scope()
    assert replace(baseline, project_id="project_b").digest != baseline.digest
    assert replace(baseline, risk="medium").digest != baseline.digest
    assert replace(baseline, capabilities=("tools", "json", "vision")).digest != baseline.digest
    assert baseline.capability_key == "json+tools"


def test_scope_rejects_invalid_risk_and_malformed_digests() -> None:
    with pytest.raises(ValueError, match="risk"):
        qualification_scope(risk="extreme")
    with pytest.raises(ValueError, match="sha256 hex digest"):
        qualification_scope(price_digest="not-a-digest")
    with pytest.raises(ValueError, match="at least two eligible targets"):
        qualification_scope(target_ids=("target_a",))
