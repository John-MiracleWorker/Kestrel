"""S5 — zero-authority production shadow observation ledger (SHADOW-001..004).

Covers the default-on observational side channel that records, for every
eligible durable scheduler/subagent attempt, the *actual* authority + target
alongside the *shadow* recommendation and an honest verdict — without ever
altering execution.

* SHADOW-001 — observe without altering execution: compiled contract, >=2
  policy-admissible targets, explicit eligibility/exclusion reasons, and a
  byte-identical execution config / no alternate provider call.
* SHADOW-002 — additive migration + backward-compatible readers + replay-stable
  payload digests.
* SHADOW-003 — honest ``supported``/``contradicted``/``inconclusive`` verdict;
  mismatched unexecuted targets cannot claim counterfactual proof.
* SHADOW-004 — no policy memory / authority writes, and fault injection showing
  observer failure cannot change the base decision.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.routing.shadow_observation import (
    ActualAuthority,
    ShadowObservationDraft,
    ShadowObservationRecorder,
    ShadowRole,
    ShadowVerdict,
    build_shadow_observation_draft,
    compute_shadow_verdict,
    shadow_observation_payload_digest,
    stable_shadow_observation_id,
)
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord


def _state_and_task(tmp_path: Path) -> tuple[AgentStateStore, TaskNodeRecord]:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    state.create_run(
        run_id="run-shadow",
        message="Inspect the repository",
        session_id="session-shadow",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = state.create_task_node(
        task_id="task-shadow",
        run_id="run-shadow",
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=("repo.search", "repo.map"),
        risk="low",
        acceptance_criteria=(),
    )
    return state, task


def _profile(profile_id: str = "local") -> ProviderProfile:
    return ProviderProfile(
        profile_id=profile_id,
        display_name=f"Profile {profile_id}",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        secret_ref=f"secret://routing-{profile_id}-key",
        locality="local",
    )


def _target(
    target_id: str,
    *,
    profile_id: str = "local",
    model: str = "model-a",
    role_affinities: tuple[str, ...] = ("worker",),
) -> ModelTarget:
    return ModelTarget(
        target_id=target_id,
        provider_profile_id=profile_id,
        provider="openai-compatible",
        model=model,
        locality="local",
        capability_tags=("repository_inspection", "scout", "worker"),
        role_affinities=role_affinities,
        task_family_affinities=("repository_inspection",),
        max_context_tokens=64_000,
        supports_tools=True,
        supports_json=True,
        supports_reasoning=True,
        quality_tier=3,
        latency_tier=1,
        estimated_cost_usd=0.0,
        health="healthy",
    )


def _configured_ledger(state: AgentStateStore) -> RoutingLedger:
    ledger = RoutingLedger(state)
    ledger.put_provider_profile(_profile())
    ledger.put_model_target(_target("local-scout"))
    ledger.put_policy(RoutePolicy())
    return ledger


# ---------------------------------------------------------------------------
# SHADOW-003 — honest verdict
# ---------------------------------------------------------------------------


class TestComputeShadowVerdict:
    def test_no_shadow_recommendation_is_inconclusive(self):
        result = compute_shadow_verdict(
            shadow_target_id=None,
            actual_target_id="local-scout",
            shadow_executed=False,
            abstention_reason="sparse_evidence",
            favorable=None,
            actual_validation_passed=None,
        )
        assert result.verdict == ShadowVerdict.INCONCLUSIVE
        assert result.counterfactual_proven is False
        assert "sparse_evidence" == result.reason

    def test_shadow_agrees_with_successful_actual_is_supported(self):
        result = compute_shadow_verdict(
            shadow_target_id="local-scout",
            actual_target_id="local-scout",
            shadow_executed=False,
            abstention_reason=None,
            favorable=None,
            actual_validation_passed=True,
        )
        assert result.verdict == ShadowVerdict.SUPPORTED
        assert result.counterfactual_proven is True

    def test_shadow_agrees_with_failed_actual_is_contradicted(self):
        result = compute_shadow_verdict(
            shadow_target_id="local-scout",
            actual_target_id="local-scout",
            shadow_executed=False,
            abstention_reason=None,
            favorable=None,
            actual_validation_passed=False,
        )
        assert result.verdict == ShadowVerdict.CONTRADICTED
        assert result.counterfactual_proven is True

    def test_shadow_agrees_without_terminal_evidence_is_inconclusive(self):
        result = compute_shadow_verdict(
            shadow_target_id="local-scout",
            actual_target_id="local-scout",
            shadow_executed=False,
            abstention_reason=None,
            favorable=None,
            actual_validation_passed=None,
        )
        assert result.verdict == ShadowVerdict.INCONCLUSIVE

    def test_mismatched_unexecuted_target_cannot_claim_counterfactual_proof(self):
        # Adaptive Flock would pick a different target, but that target was
        # never executed: even with favorable prior evidence, the verdict is
        # only "supported by prior evidence" — counterfactual_proven is False.
        result = compute_shadow_verdict(
            shadow_target_id="cheap",
            actual_target_id="expensive",
            shadow_executed=False,
            abstention_reason=None,
            favorable=True,
            actual_validation_passed=None,
        )
        assert result.verdict == ShadowVerdict.SUPPORTED
        assert result.counterfactual_proven is False
        assert "target_unexecuted" in result.evidence_basis

    def test_mismatched_unfavorable_prior_evidence_is_contradicted(self):
        result = compute_shadow_verdict(
            shadow_target_id="cheap",
            actual_target_id="expensive",
            shadow_executed=False,
            abstention_reason=None,
            favorable=False,
            actual_validation_passed=None,
        )
        assert result.verdict == ShadowVerdict.CONTRADICTED
        assert result.counterfactual_proven is False

    def test_mismatched_insufficient_evidence_is_inconclusive(self):
        result = compute_shadow_verdict(
            shadow_target_id="cheap",
            actual_target_id="expensive",
            shadow_executed=False,
            abstention_reason=None,
            favorable=None,
            actual_validation_passed=None,
        )
        assert result.verdict == ShadowVerdict.INCONCLUSIVE
        assert result.counterfactual_proven is False

    def test_shadow_executed_and_passed_is_supported_with_proof(self):
        result = compute_shadow_verdict(
            shadow_target_id="cheap",
            actual_target_id="cheap",
            shadow_executed=True,
            abstention_reason=None,
            favorable=True,
            actual_validation_passed=True,
        )
        assert result.verdict == ShadowVerdict.SUPPORTED
        assert result.counterfactual_proven is True


class TestShadowFavorability:
    def test_positive_delta_and_confidence_is_favorable(self):
        from nested_memvid_agent.routing.shadow_observation import shadow_favorability

        assert (
            shadow_favorability(
                learned_target_id="cheap",
                utility_delta=0.2,
                confidence=0.9,
                abstention_reason=None,
            )
            is True
        )

    def test_low_confidence_is_not_favorable(self):
        from nested_memvid_agent.routing.shadow_observation import shadow_favorability

        assert (
            shadow_favorability(
                learned_target_id="cheap",
                utility_delta=0.2,
                confidence=0.4,
                abstention_reason=None,
            )
            is False
        )

    def test_no_recommendation_is_none(self):
        from nested_memvid_agent.routing.shadow_observation import shadow_favorability

        assert (
            shadow_favorability(
                learned_target_id=None,
                utility_delta=0.0,
                confidence=0.0,
                abstention_reason="sparse_evidence",
            )
            is None
        )


# ---------------------------------------------------------------------------
# SHADOW-002 — replay-stable digest, additive migration, backward-compat reads
# ---------------------------------------------------------------------------


class TestReplayStableDigest:
    def test_digest_is_stable_across_terminal_evidence_variation(
        self, tmp_path: Path
    ):
        from nested_memvid_agent.routing.router import route_task

        state, task = _state_and_task(tmp_path)
        ledger = _configured_ledger(state)
        from nested_memvid_agent.routing.contracts import compile_task_contract

        contract = compile_task_contract(task)
        targets = [entry.target for entry in ledger.list_model_targets()]
        decision = route_task(contract, targets, mode="shadow")

        def build(validated: bool | None) -> ShadowObservationDraft:
            return build_shadow_observation_draft(
                run_id=contract.run_id,
                task_id=contract.task_id,
                subagent_id=None,
                attempt=1,
                role=ShadowRole.EXECUTOR,
                actual_authority=ActualAuthority.DETERMINISTIC_STATIC,
                decision=decision,
                contract=contract,
                shadow_target_id=None,
                shadow_targets={},
                shadow_executed=False,
                static_target_id=decision.selected_target.target_id,
                qualification={"utility_delta": 0.0, "confidence": 0.0},
                reason_codes=("highest_admissible_score",),
                validation_passed=validated,
            )

        before = build(None)
        after = build(True)
        assert before.payload_digest == after.payload_digest
        assert shadow_observation_payload_digest(before) == before.payload_digest

    def test_observation_id_is_deterministic(self):
        first = stable_shadow_observation_id(
            run_id="r",
            task_id="t",
            subagent_id=None,
            attempt=1,
            role=ShadowRole.EXECUTOR,
            payload_digest="d" * 64,
        )
        second = stable_shadow_observation_id(
            run_id="r",
            task_id="t",
            subagent_id=None,
            attempt=1,
            role=ShadowRole.EXECUTOR,
            payload_digest="d" * 64,
        )
        assert first == second
        assert first.startswith("shadow_obs_")


class TestAdditiveMigrationAndReads:
    def test_schema_v5_and_round_trip(self, tmp_path: Path):
        state, task = _state_and_task(tmp_path)
        ledger = _configured_ledger(state)
        assert ledger.schema_version() == 5

        contract = None
        from nested_memvid_agent.routing.contracts import compile_task_contract
        from nested_memvid_agent.routing.router import route_task

        contract = compile_task_contract(task)
        decision = route_task(
            contract, [entry.target for entry in ledger.list_model_targets()], mode="shadow"
        )
        draft = build_shadow_observation_draft(
            run_id=contract.run_id,
            task_id=contract.task_id,
            subagent_id=None,
            attempt=1,
            role=ShadowRole.EXECUTOR,
            actual_authority=ActualAuthority.DETERMINISTIC_STATIC,
            decision=decision,
            contract=contract,
            shadow_target_id=None,
            shadow_targets={},
            shadow_executed=False,
            static_target_id=decision.selected_target.target_id,
            qualification={"utility_delta": 0.0, "confidence": 0.0},
            reason_codes=("highest_admissible_score",),
        )
        entry = ledger.record_shadow_observation(draft)
        assert entry.observation_id.startswith("shadow_obs_")

        fetched = ledger.get_shadow_observation(entry.observation_id)
        assert fetched is not None
        assert fetched.verdict == ShadowVerdict.INCONCLUSIVE.value
        assert fetched.actual_target_id == "local-scout"

        listed = ledger.list_shadow_observations(run_id=contract.run_id)
        assert len(listed) == 1

        # Idempotent re-record (replay) returns the same row, no duplicate.
        again = ledger.record_shadow_observation(draft)
        assert again.observation_id == entry.observation_id
        assert len(ledger.list_shadow_observations(run_id=contract.run_id)) == 1

    def test_v4_migrates_to_v5_without_rewriting_existing_routing_history(
        self, tmp_path: Path
    ):
        state, task = _state_and_task(tmp_path)
        ledger = _configured_ledger(state)
        coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
        durable = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)
        coordinator.mark_started(durable)
        coordinator.record_outcome(
            durable,
            execution_status="complete",
            validation_passed=True,
            validation_codes=("accepted",),
            latency_seconds=0.25,
            actual_cost_usd=0.01,
            outcome_labels=("validated_success",),
        )

        def history_digest() -> str:
            history: dict[str, list[list[object]]] = {}
            for table in (
                "routing_decisions",
                "routing_outcomes",
                "routing_shadow_evaluations",
                "routing_target_calibrations",
            ):
                with state._connect() as connection:
                    rows = connection.execute(
                        f"SELECT * FROM {table} ORDER BY rowid"
                    ).fetchall()
                history[table] = [list(row) for row in rows]
            encoded = json.dumps(
                history, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            return hashlib.sha256(encoded).hexdigest()

        before = history_digest()

        # Simulate a pre-v5 database by dropping the observation table and
        # rewinding the version marker, then re-migrate.
        with state._connect() as connection:
            connection.execute(
                "DROP TABLE IF EXISTS routing_shadow_observations"
            )
            connection.execute(
                "UPDATE routing_schema_version SET version = 4 WHERE id = 1"
            )

        migrated = RoutingLedger(state)
        assert migrated.schema_version() == 5
        assert history_digest() == before


# ---------------------------------------------------------------------------
# SHADOW-001 — observe without altering execution
# ---------------------------------------------------------------------------


class TestObserveWithoutAlteringExecution:
    def test_coordinator_records_observation_without_changing_config(
        self, tmp_path: Path
    ):
        state, task = _state_and_task(tmp_path)
        ledger = _configured_ledger(state)
        coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
        base = AgentConfig(provider="mock", model="orchestrator", workspace=tmp_path)

        durable = coordinator.assign(base, task, subagent_id=None, attempt=1)

        # The observation is a side channel: shadow mode executes nothing, the
        # config is byte-identical to the base config, and no alternate provider
        # was selected.
        assert durable.assignment.executes_selected_target is False
        assert durable.assignment.config is base

        observations = ledger.list_shadow_observations(run_id=task.run_id)
        assert len(observations) == 1
        observation = observations[0]
        assert observation.role == ShadowRole.EXECUTOR.value
        assert observation.actual_authority == ActualAuthority.DETERMINISTIC_STATIC.value
        assert observation.actual_target_id == "local-scout"
        # Shadow mode: the learned router abstained, so no differing target.
        assert observation.shadow_target_id is None
        assert observation.shadow_executed is False
        assert observation.verdict == ShadowVerdict.INCONCLUSIVE.value

    def test_observation_records_two_policy_admissible_targets_with_reasons(
        self, tmp_path: Path
    ):
        state, task = _state_and_task(tmp_path)
        ledger = RoutingLedger(state)
        ledger.put_provider_profile(_profile())
        ledger.put_model_target(_target("tgt-a", model="model-a"))
        ledger.put_model_target(_target("tgt-b", model="model-b"))
        ledger.put_policy(RoutePolicy())

        coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
        coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)

        observation = ledger.list_shadow_observations(run_id=task.run_id)[0]
        # At least two policy-admissible candidates are present, each with an
        # explicit eligibility reason code.
        eligible = [c for c in observation.candidates if c["eligible"]]
        assert len(eligible) >= 2
        assert all("eligible" in c["reason_codes"] for c in eligible)

    def test_recorded_observation_does_not_call_alternate_provider(
        self, tmp_path: Path
    ):
        # Shadow observation is computed from the already-resolved decision and
        # the learned-router shadow — it never builds or calls a provider.  The
        # strongest proxy available at the coordinator level is that shadow mode
        # leaves ``executes_selected_target`` False (no alternate execution) and
        # the observation's shadow provider/model are empty when the learned
        # router abstains.
        state, task = _state_and_task(tmp_path)
        ledger = _configured_ledger(state)
        coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
        durable = coordinator.assign(
            AgentConfig(), task, subagent_id=None, attempt=1
        )
        assert durable.assignment.executes_selected_target is False
        observation = ledger.list_shadow_observations(run_id=task.run_id)[0]
        assert observation.shadow_target_id is None
        assert observation.shadow_provider == ""
        assert observation.shadow_model == ""


# ---------------------------------------------------------------------------
# SHADOW-004 — zero authority: no policy memory, fault injection
# ---------------------------------------------------------------------------


class TestZeroAuthority:
    def test_recorder_swallows_ledger_failure(self, tmp_path: Path):
        state, task = _state_and_task(tmp_path)
        ledger = _configured_ledger(state)

        from nested_memvid_agent.routing.contracts import compile_task_contract
        from nested_memvid_agent.routing.router import route_task

        contract = compile_task_contract(task)
        decision = route_task(
            contract, [entry.target for entry in ledger.list_model_targets()], mode="shadow"
        )
        draft = build_shadow_observation_draft(
            run_id=contract.run_id,
            task_id=contract.task_id,
            subagent_id=None,
            attempt=1,
            role=ShadowRole.EXECUTOR,
            actual_authority=ActualAuthority.DETERMINISTIC_STATIC,
            decision=decision,
            contract=contract,
            shadow_target_id=None,
            shadow_targets={},
            shadow_executed=False,
            static_target_id=decision.selected_target.target_id,
            qualification={},
            reason_codes=(),
        )

        class FailingLedger:
            def record_shadow_observation(self, draft):
                raise RuntimeError("observer persistence failed")

            def resolve_shadow_observation(self, *args, **kwargs):
                raise RuntimeError("observer resolution failed")

        recorder = ShadowObservationRecorder(FailingLedger())
        assert recorder.record(draft) is None
        assert recorder.resolve("obs", validation_passed=True) is None

    def test_observer_failure_does_not_change_base_decision(self, tmp_path: Path):
        # The base decision (durable record) is fully written *before* the
        # observation side channel runs, so even a forced failure in the
        # recorder cannot change the returned assignment.
        state, task = _state_and_task(tmp_path)
        ledger = _configured_ledger(state)
        coordinator = DurableRoutingCoordinator(ledger, mode="shadow")

        original = ledger.record_shadow_observation

        def failing_record(draft, *args, **kwargs):
            raise RuntimeError("injected observer failure")

        ledger.record_shadow_observation = failing_record  # type: ignore[method-assign]

        try:
            durable = coordinator.assign(
                AgentConfig(), task, subagent_id=None, attempt=1
            )
        finally:
            ledger.record_shadow_observation = original  # type: ignore[method-assign]

        # The decision still exists and selected the deterministic target.
        assert durable.record.selected_target_id == "local-scout"
        assert ledger.get_decision(durable.record.decision_id) is not None

    def test_observation_writes_no_policy_memory_or_calibration(
        self, tmp_path: Path
    ):
        state, task = _state_and_task(tmp_path)
        ledger = _configured_ledger(state)
        coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
        coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)

        with state._connect() as connection:
            shadow_count = connection.execute(
                "SELECT COUNT(*) AS c FROM routing_shadow_observations"
            ).fetchone()["c"]
            calibration_count = connection.execute(
                "SELECT COUNT(*) AS c FROM routing_target_calibrations"
            ).fetchone()["c"]
            grant_count = connection.execute(
                "SELECT COUNT(*) AS c FROM routing_activation_grants"
            ).fetchone()["c"]
            receipt_count = connection.execute(
                "SELECT COUNT(*) AS c FROM routing_qualification_receipts"
            ).fetchone()["c"]

        # The observation writes its own append-only row, and nothing else.
        assert shadow_count == 1
        assert calibration_count == 0
        assert grant_count == 0
        assert receipt_count == 0
