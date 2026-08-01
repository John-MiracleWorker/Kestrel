from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing import (
    AdaptiveFlockRoutingService,
    AgentTaskContract,
    ModelTarget,
    ProviderProfile,
    RouteCandidate,
    RouteDecision,
    RoutingUnavailableError,
    compile_task_contract,
    route_task,
)
from nested_memvid_agent.state_store import AgentStateStore


@dataclass(frozen=True)
class _Task:
    task_id: str = "task-security"
    run_id: str = "run-security"
    title: str = "Review authentication security"
    goal: str = "Identify vulnerabilities and trust-boundary flaws."
    profile: str = "worker"
    risk: str = "high"
    required_tools: tuple[str, ...] = ("repo.search",)
    acceptance_criteria: tuple[str, ...] = ("Security risks are evidence-linked.",)
    dependencies: tuple[str, ...] = ()
    plan: dict[str, Any] = field(default_factory=dict)


def test_planner_guidance_cannot_reclassify_protected_security_task() -> None:
    contract = compile_task_contract(
        _Task(),
        planner_guidance={
            "task_family": "documentation",
            "minimum_context_tokens": 8_000,
        },
    )

    assert contract.task_family == "security_review"
    assert contract.minimum_context_tokens >= 64_000
    assert "reasoning" in contract.required_capabilities


def test_service_rejects_target_profile_locality_mismatch() -> None:
    profile = ProviderProfile(
        profile_id="local",
        display_name="Local model server",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        locality="local",
    )
    target = ModelTarget(
        target_id="misdeclared-cloud-target",
        provider_profile_id="local",
        provider="openai-compatible",
        model="remote-model",
        locality="cloud",
        max_context_tokens=64_000,
        supports_tools=True,
        supports_json=True,
        supports_reasoning=True,
        quality_tier=3,
    )

    with pytest.raises(ValueError, match="does not match profile locality"):
        AdaptiveFlockRoutingService(
            profiles=[profile],
            targets=[target],
            mode="shadow",
        )


def _routing_contract() -> AgentTaskContract:
    return AgentTaskContract(
        task_id="task-lan-guard",
        run_id="run-lan-guard",
        role="worker",
        task_family="general",
        objective="Exercise the guarded LAN target.",
        complexity=0.2,
        ambiguity=0.1,
        risk="low",
    )


def _forged_lan_inventory() -> tuple[ProviderProfile, ModelTarget]:
    profile_id = "lan-provider-" + "1" * 64
    target_id = "lan-target-" + "2" * 64
    profile = ProviderProfile(
        profile_id=profile_id,
        display_name="forged LAN profile",
        adapter="lan-openai-compatible",
        base_url="http://192.168.50.2:1234/v1",
        enabled=True,
        locality="local",
        trust_class="operator_confirmed",
        metadata={"lan_discovery": {"managed": True}},
    )
    target = ModelTarget(
        target_id=target_id,
        provider_profile_id=profile_id,
        provider="lan-openai-compatible",
        model="alpha",
        enabled=True,
        locality="local",
        trust_class="operator_confirmed",
        capability_tags=("generation",),
        role_affinities=("worker",),
        health="healthy",
        predicted_success=0.9,
        metadata={"lan_discovery": {"managed": True, "reviewed": True}},
    )
    return profile, target


def _imported_managed_lan_target(tmp_path: Path) -> ModelTarget:
    import test_lan_discovery_service as lan_cases

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        _service,
        _imported,
        _provider_id,
        (target_id,),
    ) = lan_cases._import_first_positive(state)
    target = registry.get_model_target(target_id)
    assert target is not None
    return target.target


def _recomputed_managed_lan_candidate(
    target: ModelTarget,
    *,
    provider_profile_id: str | None = None,
    model_id: str | None = None,
) -> ModelTarget:
    import test_lan_discovery_service as lan_cases

    protected = copy.deepcopy(target.metadata["lan_discovery"])
    if provider_profile_id is not None:
        protected["provider_profile_id"] = provider_profile_id
    if model_id is not None:
        protected["model_id"] = model_id
    forged_target_id = lan_cases._target_id(
        protected["provider_profile_id"],
        protected["model_id"],
    )
    protected["material_binding_digest"] = lan_cases._review_material_digest(
        protected,
        trust_class=target.trust_class,
        privacy_acknowledgement_digest=protected["privacy_acknowledgement_digest"],
        intended_roles=target.role_affinities,
        task_family_affinities=target.task_family_affinities,
    )
    return replace(
        target,
        target_id=forged_target_id,
        provider_profile_id=protected["provider_profile_id"],
        model=protected["model_id"],
        enabled=True,
        health="healthy",
        metadata={"lan_discovery": protected},
    )


def test_router_and_direct_override_reject_invalid_managed_lan_binding() -> None:
    _profile, target = _forged_lan_inventory()

    with pytest.raises(RoutingUnavailableError) as ordinary:
        route_task(_routing_contract(), [target])
    assert "lan_binding_invalid" in ordinary.value.reason_codes

    with pytest.raises(RoutingUnavailableError) as direct:
        route_task(
            _routing_contract(),
            [target],
            direct_target_id=target.target_id,
        )
    assert "lan_binding_invalid" in direct.value.reason_codes


def test_router_rejects_recomputed_lan_profile_unbound_from_endpoint(
    tmp_path: Path,
) -> None:
    import test_lan_discovery_service as lan_cases

    target = _imported_managed_lan_target(tmp_path)
    candidate = _recomputed_managed_lan_candidate(
        target,
        provider_profile_id="lan-provider-" + "f" * 64,
    )

    with pytest.raises(RoutingUnavailableError) as raised:
        route_task(
            _routing_contract(),
            [candidate],
            clock=lambda: lan_cases.NOW,
        )
    assert raised.value.reason_codes == ("lan_binding_invalid",)


@pytest.mark.parametrize(
    "model_id",
    (
        "http://192.168.50.2:11434/api/generate",
        "localhost/model",
        "api-key=credential-like-model-material",
    ),
)
def test_router_rejects_recomputed_task4_invalid_lan_model_identity(
    tmp_path: Path,
    model_id: str,
) -> None:
    import test_lan_discovery_service as lan_cases

    target = _imported_managed_lan_target(tmp_path)
    candidate = _recomputed_managed_lan_candidate(target, model_id=model_id)

    with pytest.raises(RoutingUnavailableError) as raised:
        route_task(
            _routing_contract(),
            [candidate],
            clock=lambda: lan_cases.NOW,
        )
    assert raised.value.reason_codes == ("lan_binding_invalid",)


def test_router_rejects_whitespace_corrupt_protected_lan_scan_id(
    tmp_path: Path,
) -> None:
    import test_lan_discovery_service as lan_cases

    target = _imported_managed_lan_target(tmp_path)
    protected = copy.deepcopy(target.metadata["lan_discovery"])
    protected["scan_id"] = f" {protected['scan_id']}"
    candidate = replace(
        target,
        enabled=True,
        health="healthy",
        metadata={"lan_discovery": protected},
    )

    with pytest.raises(RoutingUnavailableError) as raised:
        route_task(
            _routing_contract(),
            [candidate],
            clock=lambda: lan_cases.NOW,
        )
    assert raised.value.reason_codes == ("lan_binding_invalid",)


def test_router_rejects_protected_lan_metadata_on_nonreserved_target() -> None:
    target = ModelTarget(
        target_id="ordinary-target",
        provider_profile_id="ordinary-profile",
        provider="mock",
        model="mock",
        enabled=True,
        health="healthy",
        metadata={"lan_discovery": {"managed": True}},
    )

    with pytest.raises(RoutingUnavailableError) as raised:
        route_task(_routing_contract(), [target])
    assert "lan_binding_invalid" in raised.value.reason_codes


@pytest.mark.parametrize(
    ("target_id", "profile_id"),
    [
        ("lan-provider-" + "3" * 64, "ordinary-profile"),
        ("ordinary-target", "lan-target-" + "4" * 64),
    ],
)
def test_router_rejects_either_reserved_lan_prefix_in_either_identity(
    target_id: str,
    profile_id: str,
) -> None:
    target = ModelTarget(
        target_id=target_id,
        provider_profile_id=profile_id,
        provider="mock",
        model="mock",
        enabled=True,
        health="healthy",
    )

    with pytest.raises(RoutingUnavailableError) as raised:
        route_task(_routing_contract(), [target], direct_target_id=target_id)
    assert raised.value.reason_codes == ("lan_binding_invalid",)


def test_apply_decision_has_independent_managed_lan_defense() -> None:
    profile, target = _forged_lan_inventory()
    service = AdaptiveFlockRoutingService(
        profiles=[profile],
        targets=[target],
        mode="constrained",
    )
    candidate = RouteCandidate(
        target=target,
        eligible=True,
        score=1.0,
        reason_codes=("forged",),
    )
    decision = RouteDecision(
        mode="constrained",
        policy_id="balanced",
        contract_digest=_routing_contract().digest,
        selected_target=target,
        selection_kind="forged-direct-call",
        score=1.0,
        reason_codes=("forged",),
        candidates=(candidate,),
        actionable=True,
    )

    with pytest.raises(RoutingUnavailableError) as raised:
        service.apply_decision(AgentConfig(provider="mock", model="mock"), decision)
    assert set(raised.value.reason_codes) & {
        "lan_binding_invalid",
        "lan_runtime_hardening_unavailable",
    }


def test_apply_decision_rejects_managed_lan_profile_behind_ordinary_target() -> None:
    profile = ProviderProfile(
        profile_id="ordinary-looking-profile",
        display_name="forged managed profile",
        adapter="mock",
        enabled=True,
        metadata={"nested": {"lan-discovery": {"managed": True}}},
    )
    target = ModelTarget(
        target_id="ordinary-looking-target",
        provider_profile_id=profile.profile_id,
        provider="mock",
        model="mock",
        enabled=True,
        health="healthy",
    )
    service = AdaptiveFlockRoutingService(
        profiles=[profile],
        targets=[target],
        mode="constrained",
    )
    candidate = RouteCandidate(
        target=target,
        eligible=True,
        score=1.0,
        reason_codes=("forged",),
    )
    decision = RouteDecision(
        mode="constrained",
        policy_id="balanced",
        contract_digest=_routing_contract().digest,
        selected_target=target,
        selection_kind="forged-direct-call",
        score=1.0,
        reason_codes=("forged",),
        candidates=(candidate,),
        actionable=True,
    )

    with pytest.raises(RoutingUnavailableError) as raised:
        service.apply_decision(AgentConfig(provider="mock", model="mock"), decision)
    assert raised.value.reason_codes == ("lan_binding_invalid",)


@pytest.mark.parametrize(
    "tamper",
    ("review_digest", "claim_container", "generation_failure"),
)
def test_router_recomputes_managed_lan_authority_before_routing(
    tmp_path: Path,
    tamper: str,
) -> None:
    import test_lan_discovery_service as lan_cases

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = lan_cases._import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    request, _privacy, _material = lan_cases._exact_review_request(
        owner=lan_cases.OWNER,
        profile_revision=profile.revision,
        target_revision=target.revision,
        target_id=target_id,
        protected=target.target.metadata["lan_discovery"],
    )
    reviewed = service.review_lan_target(
        request,
        authenticated_owner_principal=lan_cases.OWNER,
    )
    protected = copy.deepcopy(reviewed.target.target.metadata["lan_discovery"])
    if tamper == "review_digest":
        protected["review_digest"] = "sha256:" + "0" * 64
    elif tamper == "claim_container":
        protected["capability_claims"] = {
            "capabilities": protected["capability_claims"],
        }
    else:
        generation = protected["capability_claims"][0]
        generation.update(
            {
                "provenance": "observed",
                "status": "observed_failure",
                "supported": False,
            }
        )
    candidate = replace(
        reviewed.target.target,
        enabled=True,
        health="healthy",
        metadata={"lan_discovery": protected},
    )

    with pytest.raises(RoutingUnavailableError) as raised:
        route_task(
            _routing_contract(),
            [candidate],
            clock=lambda: lan_cases.NOW,
        )
    assert "lan_binding_invalid" in raised.value.reason_codes


@pytest.mark.parametrize(
    ("state_kind", "expected_reason"),
    (
        ("unreviewed", "lan_owner_review_required"),
        ("expired", "lan_evidence_expired"),
        ("unhardened", "lan_runtime_hardening_unavailable"),
        ("stale", "lan_binding_stale"),
    ),
)
def test_direct_target_override_never_bypasses_exact_managed_lan_state(
    tmp_path: Path,
    state_kind: str,
    expected_reason: str,
) -> None:
    import test_lan_discovery_service as lan_cases

    state = AgentStateStore(tmp_path / "state" / "agent.db")
    (
        _observation,
        _scan,
        registry,
        service,
        _imported,
        provider_id,
        (target_id,),
    ) = lan_cases._import_first_positive(state)
    profile = registry.get_provider_profile(provider_id)
    target = registry.get_model_target(target_id)
    assert profile is not None and target is not None
    route_clock = lan_cases.NOW

    if state_kind in {"unreviewed", "expired"}:
        candidate = replace(target.target, enabled=True, health="healthy")
        if state_kind == "expired":
            route_clock += timedelta(seconds=301)
    elif state_kind == "unhardened":
        review_request, _privacy, _material = lan_cases._exact_review_request(
            owner=lan_cases.OWNER,
            profile_revision=profile.revision,
            target_revision=target.revision,
            target_id=target_id,
            protected=target.target.metadata["lan_discovery"],
        )
        reviewed = service.review_lan_target(
            review_request,
            authenticated_owner_principal=lan_cases.OWNER,
        )
        candidate = replace(
            reviewed.target.target,
            enabled=True,
            health="healthy",
        )
    else:
        narrowed_scope = lan_cases._scope(network="192.168.50.0/30")
        drift = lan_cases._positive_observation(scope=narrowed_scope)
        _row, completed = lan_cases._persist_completed_scan(
            state,
            scan_id="scan-direct-override-stale",
            scope=narrowed_scope,
            observation=drift,
        )
        service.import_observation(
            lan_cases._import_request(
                drift,
                completed,
                profile_revision=profile.revision,
                target_revisions=((target_id, target.revision),),
            ),
            authenticated_owner_principal=lan_cases.OWNER,
        )
        stale = registry.get_model_target(target_id)
        assert stale is not None
        candidate = replace(stale.target, enabled=True, health="healthy")

    with pytest.raises(RoutingUnavailableError) as raised:
        route_task(
            _routing_contract(),
            [candidate],
            direct_target_id=candidate.target_id,
            clock=lambda: route_clock,
        )

    assert expected_reason in raised.value.reason_codes
