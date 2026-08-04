from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
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


def test_evaluate_target_eligibility_applies_the_same_lan_guards_as_route_task() -> None:
    from nested_memvid_agent.routing import RoutePolicy, evaluate_target_eligibility

    _profile, target = _forged_lan_inventory()
    contract = _routing_contract()
    evaluation = evaluate_target_eligibility(
        contract,
        target,
        RoutePolicy(),
        now=datetime.now(UTC),
    )
    assert not evaluation.eligible
    assert "lan_binding_invalid" in evaluation.reason_codes
    with pytest.raises(RoutingUnavailableError) as excinfo:
        route_task(contract, [target])
    assert tuple(sorted(excinfo.value.reason_codes)) == evaluation.reason_codes


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
        ("expiry_boundary", "lan_evidence_expired"),
        ("expired", "lan_evidence_expired"),
        ("unhardened", "lan_runtime_hardening_unavailable"),
        ("forced_enabled_unhardened", "lan_binding_invalid"),
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

    if state_kind in {"unreviewed", "expiry_boundary", "expired"}:
        candidate = replace(target.target, enabled=True, health="healthy")
        if state_kind == "expiry_boundary":
            route_clock += timedelta(seconds=300)
        elif state_kind == "expired":
            route_clock += timedelta(seconds=301)
    elif state_kind in {"unhardened", "forced_enabled_unhardened"}:
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
            enabled=state_kind == "forced_enabled_unhardened",
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


def _prior_lan_authority():
    import test_lan_openai_compatible_provider as lan_provider_cases

    return lan_provider_cases._authority()


def _ordinary_assignment_service() -> tuple[
    AdaptiveFlockRoutingService,
    ProviderProfile,
    ModelTarget,
]:
    profile = ProviderProfile(
        profile_id="ordinary-local",
        display_name="Ordinary local provider",
        adapter="mock",
        enabled=True,
        locality="local",
    )
    target = ModelTarget(
        target_id="ordinary-target",
        provider_profile_id=profile.profile_id,
        provider="mock",
        model="mock",
        enabled=True,
        locality="local",
        trust_class="standard",
        capability_tags=("generation",),
        role_affinities=("worker",),
        task_family_affinities=("general",),
        max_context_tokens=32768,
        quality_tier=5,
        health="healthy",
    )
    return (
        AdaptiveFlockRoutingService(
            profiles=[profile],
            targets=[target],
            mode="shadow",
        ),
        profile,
        target,
    )


def test_non_lan_apply_and_shadow_assignment_clear_prior_lan_authority() -> None:
    authority = _prior_lan_authority()
    service, _profile, target = _ordinary_assignment_service()
    base = AgentConfig(
        provider="lan-openai-compatible",
        model="alpha",
        base_url="http://192.168.50.8:1234/v1",
        lan_runtime_authority=authority,
    )
    decision = RouteDecision(
        mode="constrained",
        policy_id="balanced",
        contract_digest="digest",
        selected_target=target,
        selection_kind="static",
        score=1.0,
        reason_codes=("selected",),
        candidates=(
            RouteCandidate(
                target=target,
                eligible=True,
                score=1.0,
                reason_codes=("eligible",),
            ),
        ),
        actionable=True,
    )
    applied = service.apply_decision(base, decision)
    assert applied.provider == "mock"
    assert applied.lan_runtime_authority is None

    @dataclass(frozen=True)
    class PlainTask:
        task_id: str = "task-clear-shadow"
        run_id: str = "run-clear-shadow"
        title: str = "Write a short note"
        goal: str = "Write an ordinary local note."
        profile: str = "worker"
        risk: str = "low"
        required_tools: tuple[str, ...] = ()
        acceptance_criteria: tuple[str, ...] = ()
        dependencies: tuple[str, ...] = ()
        plan: dict[str, object] = field(default_factory=dict)

    shadow = service.assign(
        base,
        PlainTask(),
        default_privacy_class="local_required",
        local_required=True,
    )
    assert shadow.executes_selected_target is False
    assert shadow.config.lan_runtime_authority is None


def test_annotation_of_nonactionable_assignment_clears_prior_lan_authority() -> None:
    from nested_memvid_agent.routing.coordinator import _annotate_assignment
    from nested_memvid_agent.routing.service import RoutingAssignment

    authority = _prior_lan_authority()
    service, _profile, target = _ordinary_assignment_service()
    base = AgentConfig(lan_runtime_authority=authority)
    contract = _routing_contract()
    decision = RouteDecision(
        mode="shadow",
        policy_id="balanced",
        contract_digest=contract.digest,
        selected_target=target,
        selection_kind="shadow",
        score=1.0,
        reason_codes=("shadow_only",),
        candidates=(),
        actionable=False,
    )
    assignment = RoutingAssignment(
        contract=contract,
        decision=decision,
        config=base,
        executes_selected_target=False,
    )
    annotated = _annotate_assignment(
        service,
        base_config=base,
        assignment=assignment,
        selection_kind="transport_retry_same_target",
        reason_code="transport_retry_same_target",
    )
    assert annotated.executes_selected_target is False
    assert annotated.config.lan_runtime_authority is None


def test_persisted_and_reused_nonactionable_route_lease_clears_prior_lan_authority(
    tmp_path: Path,
) -> None:
    from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
    from nested_memvid_agent.routing.ledger import RoutingLedger
    from nested_memvid_agent.routing.models import RoutePolicy

    authority = _prior_lan_authority()
    _service, profile, target = _ordinary_assignment_service()
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    state.create_run(
        run_id="run-clear-reused-shadow",
        message="Write an ordinary local note.",
        session_id="session-clear-reused-shadow",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = state.create_task_node(
        task_id="task-clear-reused-shadow",
        run_id="run-clear-reused-shadow",
        title="Write a short note",
        goal="Write an ordinary local note.",
        profile="worker",
        approved=True,
        required_tools=(),
        risk="low",
        acceptance_criteria=(),
    )
    ledger = RoutingLedger(state)
    ledger.put_provider_profile(profile)
    ledger.put_model_target(target)
    ledger.put_policy(RoutePolicy())
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    base = AgentConfig(
        provider="lan-openai-compatible",
        model="alpha",
        base_url="http://192.168.50.8:1234/v1",
        lan_runtime_authority=authority,
    )

    first = coordinator.assign(base, task, subagent_id=None, attempt=1)
    reused = coordinator.assign(base, task, subagent_id=None, attempt=1)

    assert first.assignment.executes_selected_target is False
    assert first.assignment.config.lan_runtime_authority is None
    assert reused.reused is True
    assert reused.record.decision_id == first.record.decision_id
    assert reused.assignment.executes_selected_target is False
    assert reused.assignment.config.lan_runtime_authority is None
    assert len(ledger.list_decisions(run_id=task.run_id)) == 1


def test_managed_lan_apply_resolves_fresh_authority_and_clears_fallback(
    tmp_path: Path,
) -> None:
    import test_lan_runtime_transport as runtime_cases

    (
        _state,
        registry,
        _discovery,
        _scope,
        _observation,
        provider_id,
        target_id,
        inventory,
    ) = runtime_cases._enabled_lan_registry(tmp_path)
    profile_entry = registry.get_provider_profile(provider_id)
    target_entry = registry.get_model_target(target_id)
    assert profile_entry is not None and target_entry is not None
    resolved = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: runtime_cases.NOW,
        interface_inventory_resolver=lambda: inventory,
    )
    calls: list[str] = []

    def resolver(candidate_id: str):
        calls.append(candidate_id)
        return resolved

    service = AdaptiveFlockRoutingService(
        profiles=[profile_entry.profile],
        targets=[target_entry.target],
        mode="constrained",
        lan_runtime_authority_resolver=resolver,
        clock=lambda: runtime_cases.NOW,
    )
    decision = RouteDecision(
        mode="constrained",
        policy_id="balanced",
        contract_digest="digest",
        selected_target=target_entry.target,
        selection_kind="direct",
        score=1.0,
        reason_codes=("direct_target",),
        candidates=(),
        actionable=True,
    )
    base = AgentConfig(
        provider="mock",
        model="mock",
        fallback_provider="mock",
        fallback_model="mock",
    )
    applied = service.apply_decision(base, decision)
    assert calls == [target_id]
    assert applied.provider == "lan-openai-compatible"
    assert applied.model == "alpha"
    assert applied.base_url == "http://192.168.50.2:1234/v1"
    assert applied.api_key_env is None
    assert applied.fallback_provider is None
    assert applied.fallback_model is None
    assert applied.lan_runtime_authority is resolved


def test_managed_lan_apply_allows_independently_valid_profile_latest_evidence(
    tmp_path: Path,
) -> None:
    import test_lan_runtime_transport as runtime_cases

    (
        _state,
        registry,
        _discovery,
        _scope,
        _observation,
        provider_id,
        target_id,
        inventory,
    ) = runtime_cases._enabled_lan_registry(tmp_path)
    profile_entry = registry.get_provider_profile(provider_id)
    target_entry = registry.get_model_target(target_id)
    assert profile_entry is not None and target_entry is not None
    resolved = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: runtime_cases.NOW,
        interface_inventory_resolver=lambda: inventory,
    )
    profile_metadata = copy.deepcopy(profile_entry.profile.metadata)
    profile_protected = profile_metadata["lan_discovery"]
    profile_protected.update(
        {
            "scan_id": "scan-profile-latest",
            "observation_digest": "sha256:" + "1" * 64,
            "terminal_receipt_digest": "sha256:" + "2" * 64,
            "catalog_digest": "sha256:" + "3" * 64,
            "capability_digest": "sha256:" + "4" * 64,
            "observed_at": "2026-08-01T12:00:01Z",
            "fresh_until": "2026-08-01T12:05:01Z",
        }
    )
    profile = replace(profile_entry.profile, metadata=profile_metadata)
    service = AdaptiveFlockRoutingService(
        profiles=[profile],
        targets=[target_entry.target],
        mode="constrained",
        lan_runtime_authority_resolver=lambda _target_id: resolved,
        clock=lambda: runtime_cases.NOW,
    )
    decision = RouteDecision(
        mode="constrained",
        policy_id="balanced",
        contract_digest="digest",
        selected_target=target_entry.target,
        selection_kind="direct",
        score=1.0,
        reason_codes=("direct_target",),
        candidates=(),
        actionable=True,
    )

    applied = service.apply_decision(AgentConfig(provider="mock", model="mock"), decision)

    assert applied.lan_runtime_authority is resolved


@pytest.mark.parametrize(
    "mismatch",
    (
        "missing_resolver",
        "provider_profile",
        "reviewed_target",
        "model",
        "endpoint_address",
        "endpoint_port",
        "endpoint_binding",
        "interface_index",
        "source_address",
        "os_interface_identity",
        "adapter",
        "hardening_version",
        "material",
        "review",
    ),
)
def test_managed_lan_apply_rejects_missing_or_mismatched_fresh_authority(
    tmp_path: Path,
    mismatch: str,
) -> None:
    import test_lan_runtime_transport as runtime_cases

    (
        _state,
        registry,
        _discovery,
        _scope,
        _observation,
        provider_id,
        target_id,
        inventory,
    ) = runtime_cases._enabled_lan_registry(tmp_path)
    profile_entry = registry.get_provider_profile(provider_id)
    target_entry = registry.get_model_target(target_id)
    assert profile_entry is not None and target_entry is not None
    resolved = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: runtime_cases.NOW,
        interface_inventory_resolver=lambda: inventory,
    )
    if mismatch == "provider_profile":
        forged = runtime_cases._unchecked_authority(
            resolved,
            provider_profile_id="lan-provider-" + "a" * 64,
        )
    elif mismatch == "reviewed_target":
        forged = runtime_cases._unchecked_authority(
            resolved,
            reviewed_target_id="lan-target-" + "b" * 64,
        )
    elif mismatch == "model":
        forged = runtime_cases._unchecked_authority(resolved, model_id="beta")
    elif mismatch == "endpoint_address":
        forged = runtime_cases._unchecked_authority(
            resolved,
            endpoint=runtime_cases._unchecked_endpoint(
                resolved.endpoint,
                address="192.168.50.3",
            ),
        )
    elif mismatch == "endpoint_port":
        forged = runtime_cases._unchecked_authority(
            resolved,
            endpoint=runtime_cases._unchecked_endpoint(resolved.endpoint, port=8000),
        )
    elif mismatch == "endpoint_binding":
        forged = runtime_cases._unchecked_authority(
            resolved,
            endpoint_binding_digest="sha256:" + "c" * 64,
        )
    elif mismatch == "interface_index":
        forged = runtime_cases._unchecked_authority(resolved, interface_index=8)
    elif mismatch == "source_address":
        forged = runtime_cases._unchecked_authority(
            resolved,
            source_address="192.168.50.4",
        )
    elif mismatch == "os_interface_identity":
        forged = runtime_cases._unchecked_authority(
            resolved,
            os_interface_identity="darwin:en8",
        )
    elif mismatch == "adapter":
        forged = runtime_cases._unchecked_authority(
            resolved,
            runtime_adapter="openai-compatible",
        )
    elif mismatch == "hardening_version":
        forged = runtime_cases._unchecked_authority(
            resolved,
            runtime_hardening_version="kestrel.lan.runtime.openai.v0",
        )
    elif mismatch == "material":
        forged = runtime_cases._unchecked_authority(
            resolved,
            reviewed_material_binding_digest="sha256:" + "d" * 64,
        )
    elif mismatch == "review":
        forged = runtime_cases._unchecked_authority(
            resolved,
            review_digest="sha256:" + "e" * 64,
        )
    else:
        forged = resolved

    calls: list[str] = []

    def resolver(candidate_id: str):
        calls.append(candidate_id)
        return forged

    service = AdaptiveFlockRoutingService(
        profiles=[profile_entry.profile],
        targets=[target_entry.target],
        mode="constrained",
        lan_runtime_authority_resolver=None if mismatch == "missing_resolver" else resolver,
        clock=lambda: runtime_cases.NOW,
    )
    decision = RouteDecision(
        mode="constrained",
        policy_id="balanced",
        contract_digest="digest",
        selected_target=target_entry.target,
        selection_kind="direct",
        score=1.0,
        reason_codes=("direct_target",),
        candidates=(),
        actionable=True,
    )
    base = AgentConfig(provider="mock", model="mock")

    with pytest.raises(RoutingUnavailableError) as raised:
        service.apply_decision(base, decision)

    assert raised.value.reason_codes
    assert all(code.startswith("lan_") for code in raised.value.reason_codes)
    assert calls == ([] if mismatch == "missing_resolver" else [target_id])
    assert base.provider == "mock"
    assert base.lan_runtime_authority is None


@pytest.mark.parametrize(
    "tamper",
    (
        "profile_base_url",
        "profile_secret",
        "profile_adapter",
        "profile_hardening_marker",
        "target_provider",
        "target_model",
    ),
)
def test_managed_lan_apply_revalidates_selected_inventory_before_authorizing(
    tmp_path: Path,
    tamper: str,
) -> None:
    import test_lan_runtime_transport as runtime_cases

    (
        _state,
        registry,
        _discovery,
        _scope,
        _observation,
        provider_id,
        target_id,
        inventory,
    ) = runtime_cases._enabled_lan_registry(tmp_path)
    profile_entry = registry.get_provider_profile(provider_id)
    target_entry = registry.get_model_target(target_id)
    assert profile_entry is not None and target_entry is not None
    resolved = registry.resolve_lan_runtime_authority(
        target_id,
        clock=lambda: runtime_cases.NOW,
        interface_inventory_resolver=lambda: inventory,
    )
    calls: list[str] = []

    def resolver(candidate_id: str):
        calls.append(candidate_id)
        return resolved

    service = AdaptiveFlockRoutingService(
        profiles=[profile_entry.profile],
        targets=[target_entry.target],
        mode="constrained",
        lan_runtime_authority_resolver=resolver,
        clock=lambda: runtime_cases.NOW,
    )
    selected_target = target_entry.target
    if tamper == "profile_base_url":
        forged_profile = replace(
            profile_entry.profile,
            base_url="http://192.168.50.3:1234/v1",
        )
    elif tamper == "profile_secret":
        forged_profile = replace(profile_entry.profile, secret_ref="secret://forged")
    elif tamper == "profile_adapter":
        forged_profile = replace(profile_entry.profile, adapter="openai-compatible")
    elif tamper == "profile_hardening_marker":
        forged_metadata = copy.deepcopy(profile_entry.profile.metadata)
        forged_metadata["lan_discovery"]["runtime_hardening"] = (
            "kestrel.lan.runtime.openai.v0"
        )
        forged_profile = replace(profile_entry.profile, metadata=forged_metadata)
    elif tamper == "target_provider":
        forged_profile = profile_entry.profile
        selected_target = replace(target_entry.target, provider="openai-compatible")
    elif tamper == "target_model":
        forged_profile = profile_entry.profile
        selected_target = replace(target_entry.target, model="beta")
    else:
        raise AssertionError(f"unhandled tamper: {tamper}")
    service.profiles[provider_id] = forged_profile
    decision = RouteDecision(
        mode="constrained",
        policy_id="balanced",
        contract_digest="digest",
        selected_target=selected_target,
        selection_kind="direct",
        score=1.0,
        reason_codes=("direct_target",),
        candidates=(),
        actionable=True,
    )

    with pytest.raises(RoutingUnavailableError) as raised:
        service.apply_decision(AgentConfig(provider="mock", model="mock"), decision)

    assert raised.value.reason_codes
    assert all(code.startswith("lan_") for code in raised.value.reason_codes)
    assert calls == []
