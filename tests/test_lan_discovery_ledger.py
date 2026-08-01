from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from nested_memvid_agent.lan_discovery_models import LanScanLimits
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.routing.lan_records import (
    ALLOWED_SCAN_TRANSITIONS,
    SCAN_STATES,
    LanObservationDraft,
    LanScanRevisionConflict,
    LanScanTransitionError,
)
from nested_memvid_agent.state_store import AgentStateStore

INTERFACE_ID = "sha256:" + "1" * 64
PREVIEW_DIGEST = "sha256:" + "2" * 64


@pytest.fixture
def state(tmp_path: Path) -> AgentStateStore:
    return AgentStateStore(tmp_path / "state" / "agent.db")


@pytest.fixture
def lan_ledger(state: AgentStateStore) -> LanDiscoveryLedger:
    return LanDiscoveryLedger(state)


def _limits() -> dict[str, object]:
    return asdict(LanScanLimits())


def _create_scan(
    ledger: LanDiscoveryLedger,
    *,
    scan_id: str = "lan_1",
):
    return ledger.create_scan(
        scan_id=scan_id,
        owner_principal="owner:test",
        confirmed_interface_id=INTERFACE_ID,
        network="192.168.10.0/30",
        limits=_limits(),
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )


def _observation(
    *,
    endpoint_id: str = "sha256:" + "3" * 64,
    address: str = "192.168.10.1",
    public_payload: dict[str, object] | None = None,
) -> LanObservationDraft:
    return LanObservationDraft(
        endpoint_id=endpoint_id,
        source="active",
        interface_id=INTERFACE_ID,
        address=address,
        port=11434,
        api_shape="ollama",
        tls_enabled=False,
        certificate_sha256=None,
        catalog_digest="sha256:" + "4" * 64,
        capability_digest="sha256:" + "5" * 64,
        public_payload=public_payload or {"model_count": 1, "service": "ollama"},
        freshness_timestamp="2026-08-01T12:00:00Z",
        error_category=None,
    )


def _running_scan(ledger: LanDiscoveryLedger, *, scan_id: str = "lan_1"):
    draft = _create_scan(ledger, scan_id=scan_id)
    return ledger.transition_scan(
        scan_id,
        "running",
        expected_revision=draft.revision,
    )


def test_create_scan_persists_canonical_limits_digest_and_enforces_foreign_keys(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    scan = _create_scan(lan_ledger)
    expected_json = json.dumps(
        _limits(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    expected_limits = json.loads(expected_json)
    expected_digest = "sha256:" + hashlib.sha256(expected_json.encode("utf-8")).hexdigest()

    assert scan.status == "draft"
    assert scan.revision == 1
    assert scan.limits == expected_limits
    assert scan.limits_digest == expected_digest
    with state._connect() as connection:
        stored = connection.execute(
            "SELECT limits_json, limits_digest FROM routing_lan_scans WHERE scan_id = ?",
            (scan.scan_id,),
        ).fetchone()
        assert stored is not None
        assert stored["limits_json"] == expected_json
        assert stored["limits_digest"] == expected_digest
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO routing_lan_scan_events
                    (scan_id, sequence, event_type, payload_json, created_at)
                VALUES ('missing', 1, 'invalid', '{}', '2026-08-01T12:00:00Z')
                """
            )


def test_observation_insert_uses_revision_cas_and_unique_endpoint_identity(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    running = _running_scan(lan_ledger)
    stored = lan_ledger.append_observation(
        running.scan_id,
        _observation(),
        expected_revision=running.revision,
    )

    assert stored.scan_id == running.scan_id
    assert lan_ledger.get_scan(running.scan_id).revision == running.revision + 1  # type: ignore[union-attr]
    with pytest.raises(LanScanRevisionConflict) as stale:
        lan_ledger.append_observation(
            running.scan_id,
            _observation(
                endpoint_id="sha256:" + "6" * 64,
                address="192.168.10.2",
            ),
            expected_revision=running.revision,
        )
    assert stale.value.current_revision == running.revision + 1

    current = lan_ledger.get_scan(running.scan_id)
    assert current is not None
    with pytest.raises(ValueError, match="lan_observation_exists"):
        lan_ledger.append_observation(
            running.scan_id,
            _observation(),
            expected_revision=current.revision,
        )
    assert lan_ledger.get_scan(running.scan_id) == current


def test_scan_transition_graph_rejects_stale_and_illegal_transitions(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    assert SCAN_STATES == frozenset(
        {"draft", "running", "cancelling", "cancelled", "completed", "failed", "interrupted"}
    )
    assert ALLOWED_SCAN_TRANSITIONS == {
        "draft": frozenset({"running", "cancelled"}),
        "running": frozenset({"cancelling", "completed", "failed", "interrupted"}),
        "cancelling": frozenset({"cancelled", "failed", "interrupted"}),
        "cancelled": frozenset(),
        "completed": frozenset(),
        "failed": frozenset(),
        "interrupted": frozenset(),
    }
    draft = _create_scan(lan_ledger)
    running = lan_ledger.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )

    with pytest.raises(LanScanRevisionConflict):
        lan_ledger.transition_scan(
            draft.scan_id,
            "cancelling",
            expected_revision=draft.revision,
        )
    with pytest.raises(LanScanTransitionError, match="running_to_draft"):
        lan_ledger.transition_scan(
            draft.scan_id,
            "draft",
            expected_revision=running.revision,
        )
    assert lan_ledger.get_scan(draft.scan_id) == running


@pytest.mark.parametrize(
    ("path", "terminal_status"),
    [
        (("running",), "completed"),
        (("running",), "failed"),
        (("running",), "interrupted"),
        (("running", "cancelling"), "cancelled"),
        ((), "cancelled"),
    ],
)
def test_every_allowed_terminal_path_writes_one_authenticated_receipt(
    lan_ledger: LanDiscoveryLedger,
    path: tuple[str, ...],
    terminal_status: str,
) -> None:
    current = _create_scan(lan_ledger)
    for status in path:
        current = lan_ledger.transition_scan(
            current.scan_id,
            status,
            expected_revision=current.revision,
        )
    terminal = lan_ledger.transition_scan(
        current.scan_id,
        terminal_status,
        expected_revision=current.revision,
        cancel_reason=("owner_cancelled" if terminal_status == "cancelled" else None),
        terminal_reason=("finished" if terminal_status != "cancelled" else None),
        candidate_count=0,
        error_count=0,
        timeout_count=0,
    )

    assert terminal.terminal_receipt is not None
    assert terminal.terminal_receipt["status"] == terminal_status
    assert terminal.terminal_receipt_digest is not None


def test_terminal_receipt_digest_is_hand_derived_and_terminal_scan_is_immutable(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger)
    lan_ledger.append_observation(
        running.scan_id,
        _observation(),
        expected_revision=running.revision,
    )
    observed = lan_ledger.get_scan(running.scan_id)
    assert observed is not None
    terminal = lan_ledger.transition_scan(
        running.scan_id,
        "completed",
        expected_revision=observed.revision,
        terminal_reason="scan_complete",
        candidate_count=1,
        error_count=0,
        timeout_count=0,
    )
    assert terminal.terminal_receipt is not None
    receipt_json = json.dumps(
        terminal.terminal_receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    expected_digest = "sha256:" + hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()

    assert terminal.terminal_receipt_digest == expected_digest
    with state._connect() as connection:
        row = connection.execute(
            """
            SELECT terminal_receipt_json, terminal_receipt_digest
            FROM routing_lan_scans WHERE scan_id = ?
            """,
            (terminal.scan_id,),
        ).fetchone()
    assert row is not None
    assert row["terminal_receipt_json"] == receipt_json
    assert row["terminal_receipt_digest"] == expected_digest

    with pytest.raises(LanScanRevisionConflict):
        lan_ledger.append_observation(terminal.scan_id, _observation())
    with pytest.raises(LanScanRevisionConflict):
        lan_ledger.append_event(
            terminal.scan_id,
            "late_event",
            {},
            expected_revision=terminal.revision,
        )
    with pytest.raises(LanScanRevisionConflict):
        lan_ledger.transition_scan(
            terminal.scan_id,
            "failed",
            expected_revision=terminal.revision,
            terminal_reason="rewrite_attempt",
            candidate_count=0,
            error_count=1,
            timeout_count=0,
        )
    assert lan_ledger.get_scan(terminal.scan_id) == terminal


def test_event_sequences_are_monotonic_per_scan_and_revision_checked(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    first_scan = _running_scan(lan_ledger, scan_id="lan_a")
    second_scan = _running_scan(lan_ledger, scan_id="lan_b")
    first = lan_ledger.append_event(
        first_scan.scan_id,
        "probe_started",
        {"port": 11434},
        expected_revision=first_scan.revision,
    )
    first_current = lan_ledger.get_scan(first_scan.scan_id)
    assert first_current is not None
    second = lan_ledger.append_event(
        first_scan.scan_id,
        "probe_finished",
        {"ok": True},
        expected_revision=first_current.revision,
    )
    other = lan_ledger.append_event(
        second_scan.scan_id,
        "probe_started",
        {},
        expected_revision=second_scan.revision,
    )

    assert (first.sequence, second.sequence, other.sequence) == (1, 2, 1)
    assert [event.sequence for event in lan_ledger.list_events("lan_a")] == [1, 2]
    assert [event.sequence for event in lan_ledger.list_events("lan_a", after_sequence=1)] == [2]
    with pytest.raises(LanScanRevisionConflict):
        lan_ledger.append_event(
            first_scan.scan_id,
            "stale",
            {},
            expected_revision=first_current.revision,
        )


def test_public_payloads_are_bounded_canonical_and_secret_fields_are_redacted(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    running = _running_scan(lan_ledger)
    stored = lan_ledger.append_observation(
        running.scan_id,
        _observation(
            public_payload={
                "service": "ollama",
                "authorization": "Bearer must-not-persist",
                "nested": {"api_key": "must-not-persist"},
            }
        ),
        expected_revision=running.revision,
    )

    assert stored.public_payload == {
        "authorization": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]"},
        "service": "ollama",
    }
    serialized = json.dumps(
        stored.public_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert "must-not-persist" not in serialized

    current = lan_ledger.get_scan(running.scan_id)
    assert current is not None
    with pytest.raises(ValueError, match="bounded public payload"):
        lan_ledger.append_observation(
            running.scan_id,
            replace(
                _observation(
                    endpoint_id="sha256:" + "7" * 64,
                    address="192.168.10.2",
                ),
                public_payload={"body": "x" * 20_000},
            ),
            expected_revision=current.revision,
        )
    assert lan_ledger.get_scan(running.scan_id) == current


def test_invalid_terminal_counts_roll_back_receipt_and_status_atomically(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger)
    with pytest.raises(ValueError, match="candidate_count"):
        lan_ledger.transition_scan(
            running.scan_id,
            "completed",
            expected_revision=running.revision,
            terminal_reason="scan_complete",
            candidate_count=-1,
            error_count=0,
            timeout_count=0,
        )

    assert lan_ledger.get_scan(running.scan_id) == running
    with state._connect() as connection:
        row = connection.execute(
            """
            SELECT status, terminal_receipt_json, terminal_receipt_digest
            FROM routing_lan_scans WHERE scan_id = ?
            """,
            (running.scan_id,),
        ).fetchone()
    assert row is not None
    assert tuple(row) == ("running", None, None)


def test_schema_v3_application_is_idempotent_and_polling_indexes_exist(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    created = _create_scan(lan_ledger)
    reopened = LanDiscoveryLedger(state)

    assert reopened.get_scan(created.scan_id) == created
    with state._connect() as connection:
        indexes = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            ).fetchall()
        }
    assert {
        "idx_routing_lan_scans_status_updated",
        "idx_routing_lan_scan_events_poll",
    } <= indexes
