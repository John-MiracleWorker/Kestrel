"""Flock qualification eligibility preview tests (Adaptive Flock plan, Task 6).

The preview snapshots every eligible target, price, policy, and authority for
the selected scopes using the exact same hard eligibility filters as ordinary
routing. It is read-only and fails closed on inventory drift before start.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.routing.qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    PriceSnapshot,
)
from nested_memvid_agent.routing.qualification_preview import (
    QualificationPreviewDraft,
    QualificationPreviewService,
    TargetInventory,
)

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

_AUTHORITY = {
    "schema": "kestrel.flock.project_authority.v1",
    "project_id": "project-a",
    "repository_path": "/repo/project-a",
    "privacy_class": "approved_cloud",
    "allowed_paths": ["src", "tests"],
    "capability_ceiling": ["tool:repo.map"],
    "revision": 3,
}


def _profile(
    profile_id: str,
    *,
    adapter: str = "openai-compatible",
    locality: str = "local",
    base_url: str = "http://127.0.0.1:1234/v1",
) -> ProviderProfile:
    return ProviderProfile(
        profile_id=profile_id,
        display_name=f"Profile {profile_id}",
        adapter=adapter,
        base_url=base_url,
        locality=locality,  # type: ignore[arg-type]
    )


def _target(
    target_id: str,
    profile_id: str,
    *,
    provider: str = "openai-compatible",
    model: str | None = None,
    locality: str = "local",
    health: str = "healthy",
    quality_tier: int = 3,
    input_cost: float | None = 0.0,
    output_cost: float | None = 0.0,
    supports_reasoning: bool = False,
) -> ModelTarget:
    return ModelTarget(
        target_id=target_id,
        provider_profile_id=profile_id,
        provider=provider,
        model=model or f"{target_id}-model",
        locality=locality,  # type: ignore[arg-type]
        health=health,  # type: ignore[arg-type]
        quality_tier=quality_tier,
        max_context_tokens=131_072,
        supports_tools=True,
        supports_reasoning=supports_reasoning,
        estimated_cost_usd=input_cost,
        input_cost_per_million_usd=input_cost,
        output_cost_per_million_usd=output_cost,
    )


def _inventory(*, with_mystery: bool = False) -> TargetInventory:
    profiles = (
        _profile("local"),
        _profile("lan", base_url="http://192.168.1.20:8080/v1"),
        _profile("cloud", adapter="openai", locality="cloud", base_url="https://api.example/v1"),
    )
    targets = [
        _target("local_qwen", "local", model="qwen-coder", supports_reasoning=True),
        _target("lan_deepseek", "lan", model="deepseek-v4", input_cost=None, output_cost=None),
        _target(
            "cloud_frontier",
            "cloud",
            provider="openai",
            model="frontier-1",
            locality="cloud",
            quality_tier=5,
            input_cost=15.0,
            output_cost=75.0,
        ),
        _target("stale_lan", "lan", model="deepseek-v3", health="unavailable"),
    ]
    if with_mystery:
        targets.append(
            _target(
                "cloud_mystery",
                "cloud",
                provider="openai",
                model="mystery-1",
                locality="cloud",
                input_cost=None,
                output_cost=None,
            )
        )
    return TargetInventory(profiles=profiles, targets=tuple(targets))


@dataclass
class _InventoryBox:
    inventory: TargetInventory


def _previewer(
    box: _InventoryBox | None = None,
) -> tuple[_InventoryBox, QualificationPreviewService]:
    box = box or _InventoryBox(inventory=_inventory())
    service = QualificationPreviewService(lambda: box.inventory, clock=lambda: NOW)
    return box, service


def _corpus(*families: str) -> CorpusManifest:
    items = []
    for index, family in enumerate(families):
        capabilities = ("reasoning",) if family == "deep_reasoning" else ("repo.map",)
        items.append(
            CorpusItem(
                item_id=f"item-{index}-a",
                task_family=family,
                risk="low",
                capabilities=capabilities,
                task_contract_digest="1" * 64,
                acceptance_plan_digest="2" * 64,
                evidence_kind="synthetic",
            )
        )
        items.append(
            CorpusItem(
                item_id=f"item-{index}-b",
                task_family=family,
                risk="low",
                capabilities=capabilities,
                task_contract_digest="3" * 64,
                acceptance_plan_digest="4" * 64,
                evidence_kind="real_project",
            )
        )
    return CorpusManifest(schema_version=1, items=tuple(items))


def _non_billed_local_price(target_id: str) -> PriceSnapshot:
    zero = MoneyMicros(0)
    return PriceSnapshot(
        target_id=target_id,
        source="operator_confirmed_non_billed_local",
        captured_at="2026-08-01T00:00:00+00:00",
        input_per_million=zero,
        output_per_million=zero,
        confirmed_by="owner@example",
        confirmed_at="2026-08-01T00:00:00+00:00",
    )


def draft(*families: str) -> QualificationPreviewDraft:
    return QualificationPreviewDraft(
        project_authority=dict(_AUTHORITY),
        task_families=families or ("repo_inspection",),
        policy=RoutePolicy(),
        policy_revision=1,
        corpus=_corpus(*(families or ("repo_inspection",))),
        prices={"lan_deepseek": _non_billed_local_price("lan_deepseek")},
    )


def draft_with_two_scopes() -> QualificationPreviewDraft:
    return draft("repo_inspection", "code_review")


def test_preview_includes_every_target_eligible_for_any_selected_scope() -> None:
    _box, previewer = _previewer()
    preview = previewer.preview(draft_with_two_scopes())
    assert preview.target_snapshot.target_ids == (
        "cloud_frontier",
        "lan_deepseek",
        "local_qwen",
    )
    assert preview.excluded_targets["stale_lan"] == ("target_health_unavailable",)
    assert len(preview.scopes) == 2
    assert all(len(scope.target_ids) == 3 for scope in preview.scopes)
    assert preview.matrix_size == 6
    assert preview.excluded_scopes == {}
    assert preview.start_blockers == {}
    # Explicit non-billed local zero pricing is accepted with provenance.
    lan = next(
        snapshot
        for snapshot in preview.target_snapshot.targets
        if snapshot.target_id == "lan_deepseek"
    )
    assert lan.price.is_known_zero
    assert lan.price.confirmed_by == "owner@example"
    # The preview is deterministic for identical inputs and clock.
    assert preview.digest == previewer.preview(draft_with_two_scopes()).digest


def test_preview_is_read_only_and_reports_reserved_cost_range() -> None:
    box, previewer = _previewer()
    before = box.inventory
    preview = previewer.preview(draft())
    assert box.inventory == before
    low, high = preview.estimated_reserved_cost_range
    assert low == MoneyMicros(0)
    assert high == MoneyMicros(90_000_000)
    assert preview.corpus_digest == draft().corpus.digest


def test_start_rejects_inventory_drift_after_preview() -> None:
    box, previewer = _previewer()
    preview = previewer.preview(draft())
    box.inventory = replace(
        box.inventory,
        targets=tuple(
            replace(target, model="qwen-coder-v2") if target.target_id == "local_qwen" else target
            for target in box.inventory.targets
        ),
    )
    with pytest.raises(ValueError, match="target_inventory_changed"):
        previewer.revalidate_for_start(preview)


def test_start_rejects_eligibility_drift_after_preview() -> None:
    box, previewer = _previewer()
    preview = previewer.preview(draft())
    box.inventory = replace(
        box.inventory,
        targets=tuple(
            replace(target, health="open") if target.target_id == "cloud_frontier" else target
            for target in box.inventory.targets
        ),
    )
    with pytest.raises(ValueError, match="target_inventory_changed|target_eligibility_changed"):
        previewer.revalidate_for_start(preview)


def test_revalidate_for_start_succeeds_when_inventory_is_unchanged() -> None:
    _box, previewer = _previewer()
    preview = previewer.preview(draft_with_two_scopes())
    fresh = previewer.revalidate_for_start(preview)
    assert fresh.target_inventory_digest == preview.target_inventory_digest
    assert [scope.target_ids for scope in fresh.scopes] == [
        scope.target_ids for scope in preview.scopes
    ]


def test_unknown_billed_price_is_a_start_blocker_for_that_target() -> None:
    box = _InventoryBox(inventory=_inventory(with_mystery=True))
    _box, previewer = _previewer(box)
    preview = previewer.preview(draft())
    assert preview.start_blockers["cloud_mystery"] == ("price_unknown",)
    assert "cloud_mystery" in preview.target_snapshot.target_ids
    with pytest.raises(ValueError, match="price_unknown"):
        previewer.revalidate_for_start(preview)


def test_scope_with_fewer_than_two_eligible_targets_is_excluded() -> None:
    _box, previewer = _previewer()
    preview = previewer.preview(draft("deep_reasoning"))
    assert preview.scopes == ()
    assert preview.excluded_scopes == {
        "deep_reasoning:low:reasoning": ("insufficient_eligible_targets",)
    }
    assert preview.target_snapshot.target_ids == ("local_qwen",)
    assert preview.excluded_targets["cloud_frontier"] == ("reasoning_unsupported",)


def test_stale_price_is_reported_as_a_freshness_warning() -> None:
    stale_price = PriceSnapshot(
        target_id="cloud_frontier",
        source="operator_verified",
        captured_at="2026-01-01T00:00:00+00:00",
        input_per_million=MoneyMicros.from_usd_text("15"),
        output_per_million=MoneyMicros.from_usd_text("75"),
    )
    stale_draft = replace(
        draft(),
        prices={
            "lan_deepseek": _non_billed_local_price("lan_deepseek"),
            "cloud_frontier": stale_price,
        },
    )
    _box, previewer = _previewer()
    preview = previewer.preview(stale_draft)
    assert preview.warnings["price_stale"] == ("cloud_frontier",)
    assert preview.start_blockers == {}
