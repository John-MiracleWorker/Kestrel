"""Routing schema v4 qualification and grant ledger tests (Adaptive Flock Task 2).

Covers the additive v3 -> v4 migration receipt, run revision races, integer
micro-USD money columns, and the immutability guarantees for receipts,
activation grant base rows, and activation transitions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.routing.coordinator import DurableRoutingCoordinator
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile, RoutePolicy
from nested_memvid_agent.routing.qualification_digest import canonical_digest, canonical_json
from nested_memvid_agent.routing.qualification_ledger import QualificationLedger
from nested_memvid_agent.routing.qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    QualificationScope,
    QualificationThresholds,
)
from nested_memvid_agent.routing.qualification_records import (
    ActivationGrantDraft,
    QualificationAttemptDraft,
    QualificationCaseDraft,
    QualificationRevisionConflict,
    QualificationRunDraft,
)
from nested_memvid_agent.state_store import AgentStateStore, TaskNodeRecord

V4_TABLES = (
    "routing_qualification_runs",
    "routing_qualification_cases",
    "routing_qualification_attempts",
    "routing_qualification_events",
    "routing_qualification_receipts",
    "routing_activation_grants",
    "routing_activation_transitions",
)

V4_DECISION_COLUMNS = (
    "activation_grant_id",
    "activation_receipt_id",
    "activation_effective",
    "activation_reason",
)

V1_V3_EVIDENCE_TABLES = (
    "routing_provider_profiles",
    "routing_model_targets",
    "routing_policies",
    "routing_decisions",
    "routing_outcomes",
    "routing_shadow_evaluations",
    "routing_target_calibrations",
    "routing_lan_scans",
    "routing_lan_observations",
    "routing_lan_scan_events",
)


def _state_and_task(tmp_path: Path) -> tuple[AgentStateStore, TaskNodeRecord]:
    state = AgentStateStore(tmp_path / "state" / "agent.db")
    state.create_run(
        run_id="run-routing",
        message="Inspect the repository",
        session_id="session-routing",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    task = state.create_task_node(
        task_id="task-routing",
        run_id="run-routing",
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=("repo.search", "repo.map"),
        risk="low",
        acceptance_criteria=(),
    )
    return state, task


def _profile() -> ProviderProfile:
    return ProviderProfile(
        profile_id="local",
        display_name="Local model server",
        adapter="openai-compatible",
        base_url="http://127.0.0.1:1234/v1",
        secret_ref="secret://routing-local-key",
        locality="local",
    )


def _target() -> ModelTarget:
    return ModelTarget(
        target_id="local-scout",
        provider_profile_id="local",
        provider="openai-compatible",
        model="qwen-coder",
        locality="local",
        capability_tags=("repository_inspection", "scout", "worker"),
        role_affinities=("worker",),
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
    ledger.put_model_target(_target())
    ledger.put_policy(RoutePolicy())
    return ledger


def _insert_lan_draft_scan(state: AgentStateStore) -> None:
    now = "2026-08-02T00:00:00+00:00"
    limits_json = json.dumps(
        {"max_endpoints": 1}, sort_keys=True, separators=(",", ":")
    )
    limits_digest = "sha256:" + hashlib.sha256(limits_json.encode("utf-8")).hexdigest()
    with state._connect() as conn:
        conn.execute(
            """
            INSERT INTO routing_lan_scans (
                scan_id, status, revision, owner_principal, confirmed_interface_id,
                network, limits_json, limits_digest, preview_digest,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "lan_draft_migration_fixture",
                "draft",
                1,
                "owner@example.test",
                "en0",
                "192.168.10.1/32",
                limits_json,
                limits_digest,
                "sha256:" + "9" * 64,
                now,
                now,
            ),
        )


def _downgrade_to_v3(state: AgentStateStore) -> None:
    """Simulate a pre-v4 database without touching v1-v3 evidence rows."""

    with state._connect() as conn:
        existing_tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        for table in (
            "routing_qualification_events",
            "routing_qualification_receipts",
            "routing_qualification_attempts",
            "routing_qualification_cases",
            "routing_activation_transitions",
            "routing_activation_grants",
            "routing_qualification_runs",
        ):
            if table in existing_tables:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
        decision_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(routing_decisions)").fetchall()
        }
        for column in V4_DECISION_COLUMNS:
            if column in decision_columns:
                conn.execute(f"ALTER TABLE routing_decisions DROP COLUMN {column}")
        conn.execute("UPDATE routing_schema_version SET version = 3 WHERE id = 1")


def routing_and_lan_digest(state: AgentStateStore) -> str:
    """Digest every v1-v3 routing and LAN evidence row in insertion order.

    ``routing_decisions`` is projected onto its pre-v4 columns so the
    nullable activation columns added by the v4 migration cannot perturb
    the receipt.
    """

    history: dict[str, list[list[object]]] = {}
    for table in V1_V3_EVIDENCE_TABLES:
        with state._connect() as connection:
            if table == "routing_decisions":
                columns = [
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(routing_decisions)"
                    ).fetchall()
                    if str(row[1]) not in V4_DECISION_COLUMNS
                ]
                select_list = ", ".join(columns)
            else:
                select_list = "*"
            rows = connection.execute(
                f"SELECT {select_list} FROM {table} ORDER BY rowid"
            ).fetchall()
        history[table] = [list(row) for row in rows]
    encoded = json.dumps(
        history,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@pytest.fixture
def state(tmp_path: Path) -> AgentStateStore:
    return AgentStateStore(tmp_path / "state" / "agent.db")


@pytest.fixture
def qualification_ledger(state: AgentStateStore) -> QualificationLedger:
    return QualificationLedger(state)


@pytest.fixture
def v3_state(tmp_path: Path) -> AgentStateStore:
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
    _insert_lan_draft_scan(state)
    _downgrade_to_v3(state)
    return state


def run_scope() -> QualificationScope:
    return QualificationScope(
        project_id="project-alpha",
        task_family="repository_inspection",
        risk="low",
        capabilities=("repository_inspection",),
        policy_id="balanced",
        policy_revision=1,
        target_ids=("local-critic", "local-scout"),
        target_inventory_digest="1" * 64,
        price_digest="2" * 64,
        learned_config_digest="3" * 64,
        project_authority_digest="4" * 64,
    )


def run_corpus() -> CorpusManifest:
    return CorpusManifest(
        schema_version=1,
        items=(
            CorpusItem(
                item_id="corpus_item_1",
                task_family="repository_inspection",
                risk="low",
                capabilities=("repository_inspection",),
                task_contract_digest="a" * 64,
                acceptance_plan_digest="b" * 64,
                evidence_kind="synthetic",
            ),
        ),
    )


def run_draft(run_id: str = "qual_run_1") -> QualificationRunDraft:
    return QualificationRunDraft(
        run_id=run_id,
        owner_principal="owner@example.test",
        scope=run_scope(),
        corpus=run_corpus(),
        thresholds=QualificationThresholds(),
        target_snapshot={"targets": ["local-critic", "local-scout"]},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
        project_authority={"principal": "owner@example.test"},
        build={"version": "0.5.0", "git": "bd2c182"},
        max_spend=MoneyMicros.from_usd_text("50.00"),
        effective_stop_cap=MoneyMicros.from_usd_text("25.00"),
        attempt_ceiling=MoneyMicros.from_usd_text("5.00"),
    )


def case_draft(
    run_id: str = "qual_run_1", case_id: str = "qual_case_1"
) -> QualificationCaseDraft:
    return QualificationCaseDraft(
        case_id=case_id,
        run_id=run_id,
        item=run_corpus().items[0],
        repository_digest="c" * 64,
        privacy_eligible=True,
    )


def attempt_draft(
    case_id: str = "qual_case_1", attempt_id: str = "qual_attempt_1"
) -> QualificationAttemptDraft:
    return QualificationAttemptDraft(
        attempt_id=attempt_id,
        case_id=case_id,
        attempt_number=1,
        target_id="local-scout",
        target_digest="d" * 64,
        reservation=MoneyMicros.from_usd_text("1.00"),
    )


def _running_run(ledger: QualificationLedger, run_id: str = "qual_run_1"):
    run = ledger.create_run(run_draft(run_id))
    ready = ledger.mark_ready(run.run_id, expected_revision=run.revision)
    return ledger.mark_running(ready.run_id, expected_revision=ready.revision)


def _completed_run(ledger: QualificationLedger, run_id: str = "qual_run_1"):
    running = _running_run(ledger, run_id)
    return ledger.complete_run(
        running.run_id,
        expected_revision=running.revision,
        terminal_reason="all_cases_scored",
        actual_spend=MoneyMicros.from_usd_text("2.50"),
        terminal_receipt={"qualified": True, "target_id": "local-scout"},
    )


def _grant(ledger: QualificationLedger, run_id: str = "qual_run_1"):
    completed = _completed_run(ledger, run_id)
    receipts = ledger.list_receipts(completed.run_id)
    assert receipts
    return ledger.create_grant(
        ActivationGrantDraft(
            grant_id="grant_1",
            run_id=completed.run_id,
            target_id="local-scout",
            qualification_receipt_id=receipts[0].receipt_id,
            created_by="owner@example.test",
        )
    )


# --- Migration receipts -----------------------------------------------------


def test_routing_v3_migrates_to_v4_without_rewriting_existing_evidence(
    v3_state: AgentStateStore,
) -> None:
    before = routing_and_lan_digest(v3_state)
    ledger = RoutingLedger(v3_state)
    assert ledger.schema_version() == 5
    assert routing_and_lan_digest(v3_state) == before


def test_v4_migration_adds_qualification_tables_and_decision_columns(
    v3_state: AgentStateStore,
) -> None:
    RoutingLedger(v3_state)
    with v3_state._connect() as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        decision_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(routing_decisions)"
            ).fetchall()
        }
    assert set(V4_TABLES) <= tables
    assert set(V4_DECISION_COLUMNS) <= decision_columns


# --- Run lifecycle ----------------------------------------------------------


def test_qualification_run_revision_race_has_one_winner(
    qualification_ledger: QualificationLedger,
) -> None:
    run = qualification_ledger.create_run(run_draft())
    first = qualification_ledger.mark_ready(run.run_id, expected_revision=1)
    assert first.revision == 2
    with pytest.raises(QualificationRevisionConflict) as raised:
        qualification_ledger.mark_ready(run.run_id, expected_revision=1)
    assert raised.value.current_revision == 2


def test_run_persists_canonical_json_and_digest_columns(
    qualification_ledger: QualificationLedger, state: AgentStateStore
) -> None:
    draft = run_draft()
    run = qualification_ledger.create_run(draft)

    assert run.status == "draft"
    assert run.revision == 1
    with state._connect() as connection:
        row = connection.execute(
            "SELECT * FROM routing_qualification_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row is not None
    for name in (
        "scope",
        "corpus",
        "target",
        "price",
        "policy",
        "learned",
        "project_authority",
        "build",
    ):
        json_text = str(row[f"{name}_json"])
        digest = str(row[f"{name}_digest"])
        assert digest == canonical_digest(json.loads(json_text))
        assert json_text == canonical_json(json.loads(json_text))
    assert str(row["scope_digest"]) == draft.scope.digest


def test_run_money_columns_are_integer_micros_and_stop_cap_is_mutable(
    qualification_ledger: QualificationLedger, state: AgentStateStore
) -> None:
    run = qualification_ledger.create_run(run_draft())
    with state._connect() as connection:
        row = connection.execute(
            "SELECT * FROM routing_qualification_runs WHERE run_id = ?",
            (run.run_id,),
        ).fetchone()
    assert row is not None
    for column in (
        "max_spend_micros",
        "effective_stop_cap_micros",
        "actual_spend_micros",
        "unresolved_reserve_micros",
        "inflight_reserve_micros",
        "attempt_ceiling_micros",
    ):
        assert type(row[column]) is int
    assert row["max_spend_micros"] == 50_000_000
    assert row["effective_stop_cap_micros"] == 25_000_000
    assert row["actual_spend_micros"] == 0

    updated = qualification_ledger.update_effective_stop_cap(
        run.run_id,
        expected_revision=run.revision,
        new_cap=MoneyMicros.from_usd_text("30.00"),
    )
    assert updated.effective_stop_cap.micros == 30_000_000
    assert updated.max_spend.micros == 50_000_000
    assert updated.revision == run.revision + 1

    with pytest.raises(ValueError, match="stop cap"):
        qualification_ledger.update_effective_stop_cap(
            run.run_id,
            expected_revision=updated.revision,
            new_cap=MoneyMicros.from_usd_text("60.00"),
        )
    with pytest.raises(ValueError, match="stop cap"):
        qualification_ledger.create_run(
            replace(
                run_draft("qual_run_overspent"),
                effective_stop_cap=MoneyMicros.from_usd_text("60.00"),
            )
        )


def test_run_and_attempt_reject_invalid_state_transitions(
    qualification_ledger: QualificationLedger,
) -> None:
    run = qualification_ledger.create_run(run_draft())
    with pytest.raises(ValueError, match="draft"):
        qualification_ledger.mark_running(run.run_id, expected_revision=run.revision)
    ready = qualification_ledger.mark_ready(run.run_id, expected_revision=run.revision)
    with pytest.raises(ValueError, match="draft"):
        qualification_ledger.mark_ready(ready.run_id, expected_revision=ready.revision)


# --- Immutability guards ----------------------------------------------------


def test_receipt_rows_cannot_be_updated_or_deleted(
    qualification_ledger: QualificationLedger, state: AgentStateStore
) -> None:
    run = qualification_ledger.create_run(run_draft())
    receipt = qualification_ledger.append_receipt(
        run.run_id, "scope_review", {"ok": True}
    )

    with pytest.raises(sqlite3.IntegrityError, match="qualification_receipt_immutable"):
        with state._connect() as connection:
            connection.execute(
                "UPDATE routing_qualification_receipts SET payload_json = '{}' "
                "WHERE receipt_id = ?",
                (receipt.receipt_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="qualification_receipt_immutable"):
        with state._connect() as connection:
            connection.execute(
                "DELETE FROM routing_qualification_receipts WHERE receipt_id = ?",
                (receipt.receipt_id,),
            )


def test_grant_base_rows_cannot_be_updated_or_deleted(
    qualification_ledger: QualificationLedger, state: AgentStateStore
) -> None:
    grant = _grant(qualification_ledger)

    with pytest.raises(sqlite3.IntegrityError, match="activation_grant_immutable"):
        with state._connect() as connection:
            connection.execute(
                "UPDATE routing_activation_grants SET target_id = 'other' "
                "WHERE grant_id = ?",
                (grant.grant_id,),
            )
    with pytest.raises(sqlite3.IntegrityError, match="activation_grant_immutable"):
        with state._connect() as connection:
            connection.execute(
                "DELETE FROM routing_activation_grants WHERE grant_id = ?",
                (grant.grant_id,),
            )


def test_activation_transitions_cannot_be_rewritten(
    qualification_ledger: QualificationLedger, state: AgentStateStore
) -> None:
    grant = _grant(qualification_ledger)
    transition = qualification_ledger.append_transition(
        grant.grant_id, "activated", "initial_activation"
    )

    with pytest.raises(
        sqlite3.IntegrityError, match="activation_transition_immutable"
    ):
        with state._connect() as connection:
            connection.execute(
                "UPDATE routing_activation_transitions SET reason = 'rewritten' "
                "WHERE transition_id = ?",
                (transition.transition_id,),
            )
    with pytest.raises(
        sqlite3.IntegrityError, match="activation_transition_immutable"
    ):
        with state._connect() as connection:
            connection.execute(
                "DELETE FROM routing_activation_transitions WHERE transition_id = ?",
                (transition.transition_id,),
            )


def test_activation_transition_chain_is_ordered_and_revocation_is_terminal(
    qualification_ledger: QualificationLedger,
) -> None:
    grant = _grant(qualification_ledger)

    with pytest.raises(ValueError, match="activated"):
        qualification_ledger.append_transition(
            grant.grant_id, "suspended", "suspend_before_activation"
        )

    activated = qualification_ledger.append_transition(
        grant.grant_id, "activated", "initial_activation"
    )
    assert activated.sequence == 1
    suspended = qualification_ledger.append_transition(
        grant.grant_id, "suspended", "operator_pause"
    )
    assert suspended.sequence == 2
    resumed = qualification_ledger.append_transition(
        grant.grant_id, "resumed", "operator_resume"
    )
    assert resumed.sequence == 3
    revoked = qualification_ledger.append_transition(
        grant.grant_id, "revoked", "operator_revoke"
    )
    assert revoked.sequence == 4
    with pytest.raises(ValueError, match="revoked"):
        qualification_ledger.append_transition(
            grant.grant_id, "activated", "reactivate_after_revoke"
        )
    assert [
        transition.transition_type
        for transition in qualification_ledger.list_transitions(grant.grant_id)
    ] == ["activated", "suspended", "resumed", "revoked"]


# --- Terminal evidence guards -----------------------------------------------


def test_terminal_run_rejects_evidence_append_and_second_terminalization(
    qualification_ledger: QualificationLedger, state: AgentStateStore
) -> None:
    completed = _completed_run(qualification_ledger)
    assert completed.status == "completed"

    with pytest.raises(ValueError, match="terminal"):
        qualification_ledger.complete_run(
            completed.run_id,
            expected_revision=completed.revision,
            terminal_reason="second_terminalization",
            actual_spend=MoneyMicros(0),
            terminal_receipt={"qualified": False},
        )
    with pytest.raises(ValueError, match="terminal"):
        qualification_ledger.append_event(completed.run_id, "late_event", {})
    with pytest.raises(ValueError, match="terminal"):
        qualification_ledger.append_receipt(completed.run_id, "late", {})
    with pytest.raises(ValueError, match="terminal"):
        qualification_ledger.create_case(case_draft(completed.run_id))

    with pytest.raises(
        sqlite3.IntegrityError, match="terminal_qualification_run_immutable"
    ):
        with state._connect() as connection:
            connection.execute(
                "UPDATE routing_qualification_runs SET status = 'running' "
                "WHERE run_id = ?",
                (completed.run_id,),
            )
    with pytest.raises(
        sqlite3.IntegrityError, match="terminal_qualification_run_immutable"
    ):
        with state._connect() as connection:
            connection.execute(
                """
                INSERT INTO routing_qualification_events (
                    run_id, sequence, event_type, payload_json, created_at
                ) VALUES (?, 99, 'late_event', '{}', '2026-08-02T00:00:00+00:00')
                """,
                (completed.run_id,),
            )


def test_terminalize_appends_receipt_and_event_within_one_transaction(
    qualification_ledger: QualificationLedger,
) -> None:
    completed = _completed_run(qualification_ledger)
    receipts = qualification_ledger.list_receipts(completed.run_id)
    assert [receipt.receipt_type for receipt in receipts] == ["run_terminal"]
    assert receipts[0].payload == {"qualified": True, "target_id": "local-scout"}
    assert receipts[0].payload_digest == canonical_digest(receipts[0].payload)
    events = qualification_ledger.list_events(completed.run_id)
    assert events[-1].event_type == "run_completed"
    assert completed.actual_spend.micros == 2_500_000


# --- Cases and attempts -----------------------------------------------------


def test_case_rejects_scope_mismatch_and_duplicate_item(
    qualification_ledger: QualificationLedger,
) -> None:
    run = _running_run(qualification_ledger)
    mismatched = QualificationCaseDraft(
        case_id="qual_case_mismatch",
        run_id=run.run_id,
        item=CorpusItem(
            item_id="corpus_item_2",
            task_family="code_modification",
            risk="high",
            capabilities=("repository_inspection",),
            task_contract_digest="e" * 64,
            acceptance_plan_digest="f" * 64,
            evidence_kind="synthetic",
        ),
        repository_digest="c" * 64,
        privacy_eligible=True,
    )
    with pytest.raises(ValueError, match="scope"):
        qualification_ledger.create_case(mismatched)

    created = qualification_ledger.create_case(case_draft(run.run_id))
    assert created.scope_digest == run.scope_digest
    with pytest.raises(sqlite3.IntegrityError):
        qualification_ledger.create_case(
            case_draft(run.run_id, case_id="qual_case_duplicate_item")
        )


def test_attempt_lifecycle_terminalizes_exactly_once(
    qualification_ledger: QualificationLedger, state: AgentStateStore
) -> None:
    run = _running_run(qualification_ledger)
    case = qualification_ledger.create_case(case_draft(run.run_id))
    attempt = qualification_ledger.create_attempt(attempt_draft(case.case_id))
    assert attempt.status == "pending"

    reserved = qualification_ledger.reserve_attempt(attempt.attempt_id)
    assert reserved.status == "reserved"
    started = qualification_ledger.mark_attempt_running(attempt.attempt_id)
    assert started.status == "running"
    done = qualification_ledger.complete_attempt(
        attempt.attempt_id,
        usage={"input_tokens": 10, "output_tokens": 5},
        actual_cost=MoneyMicros.from_usd_text("0.75"),
        validation_passed=True,
        validation_codes=("accepted",),
        evidence_refs=("mv2://layer/entry-1",),
    )
    assert done.status == "completed"
    assert done.actual_cost is not None and done.actual_cost.micros == 750_000
    assert done.validation_passed is True

    with pytest.raises(ValueError, match="terminal"):
        qualification_ledger.complete_attempt(
            attempt.attempt_id,
            usage={},
            actual_cost=MoneyMicros(0),
            validation_passed=False,
            validation_codes=(),
            evidence_refs=(),
        )
    with pytest.raises(
        sqlite3.IntegrityError, match="terminal_qualification_attempt_immutable"
    ):
        with state._connect() as connection:
            connection.execute(
                "UPDATE routing_qualification_attempts SET status = 'running' "
                "WHERE attempt_id = ?",
                (attempt.attempt_id,),
            )


def test_attempt_binds_case_target_scope_and_routing_decision(
    qualification_ledger: QualificationLedger, state: AgentStateStore
) -> None:
    run = _running_run(qualification_ledger)
    case = qualification_ledger.create_case(case_draft(run.run_id))

    out_of_scope = QualificationAttemptDraft(
        attempt_id="qual_attempt_out_of_scope",
        case_id=case.case_id,
        attempt_number=1,
        target_id="cloud-premium",
        target_digest="d" * 64,
        reservation=MoneyMicros(0),
    )
    with pytest.raises(ValueError, match="scope"):
        qualification_ledger.create_attempt(out_of_scope)

    with pytest.raises(ValueError, match="ceiling"):
        qualification_ledger.create_attempt(
            QualificationAttemptDraft(
                attempt_id="qual_attempt_over_ceiling",
                case_id=case.case_id,
                attempt_number=1,
                target_id="local-scout",
                target_digest="d" * 64,
                reservation=MoneyMicros.from_usd_text("6.00"),
            )
        )

    state.create_run(
        run_id="run-routing",
        message="Inspect the repository",
        session_id="session-routing",
        workspace="/workspace",
        provider="mock",
        model="mock",
    )
    task = state.create_task_node(
        task_id="task-routing",
        run_id="run-routing",
        title="Inspect repository context",
        goal="Gather relevant repository context without changing files.",
        profile="worker",
        approved=True,
        required_tools=("repo.search", "repo.map"),
        risk="low",
        acceptance_criteria=(),
    )
    ledger = _configured_ledger(state)
    coordinator = DurableRoutingCoordinator(ledger, mode="shadow")
    durable = coordinator.assign(AgentConfig(), task, subagent_id=None, attempt=1)

    bound = qualification_ledger.create_attempt(
        QualificationAttemptDraft(
            attempt_id="qual_attempt_bound",
            case_id=case.case_id,
            attempt_number=1,
            target_id="local-scout",
            target_digest="d" * 64,
            reservation=MoneyMicros.from_usd_text("1.00"),
            routing_decision_id=durable.record.decision_id,
            routing_lease_id="lease-1",
        )
    )
    assert bound.routing_decision_id == durable.record.decision_id

    with pytest.raises(sqlite3.IntegrityError):
        qualification_ledger.create_attempt(
            QualificationAttemptDraft(
                attempt_id="qual_attempt_missing_decision",
                case_id=case.case_id,
                attempt_number=2,
                target_id="local-scout",
                target_digest="d" * 64,
                reservation=MoneyMicros(0),
                routing_decision_id="decision-does-not-exist",
            )
        )
