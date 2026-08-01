from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest

import nested_memvid_agent.routing.lan_ledger as lan_ledger_module
from nested_memvid_agent.lan_discovery_models import LanScanLimits
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.routing.lan_records import (
    ALLOWED_SCAN_TRANSITIONS,
    SCAN_STATES,
    LanObservationDraft,
    LanScanRevisionConflict,
    LanScanTransitionError,
)
from nested_memvid_agent.routing.ledger import RoutingLedger
from nested_memvid_agent.routing.models import ModelTarget, ProviderProfile
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


def _lan_table_snapshot(
    state: AgentStateStore,
    table: str,
) -> tuple[tuple[tuple[object, ...], ...], bytes]:
    query = {
        "routing_lan_scans": "SELECT rowid, * FROM routing_lan_scans ORDER BY rowid",
        "routing_lan_observations": (
            "SELECT rowid, * FROM routing_lan_observations ORDER BY rowid"
        ),
        "routing_lan_scan_events": ("SELECT rowid, * FROM routing_lan_scan_events ORDER BY rowid"),
    }[table]
    with state._connect() as connection:
        rows = connection.execute(query).fetchall()
    values = tuple(tuple(row) for row in rows)
    canonical_bytes = json.dumps(
        values,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return values, canonical_bytes


def _install_pre_hardening_nullable_v3_schema(state: AgentStateStore) -> None:
    statements = (
        """
        CREATE TABLE routing_schema_version (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            version INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """,
        """
        INSERT INTO routing_schema_version (id, version, updated_at)
        VALUES (1, 3, '2026-08-01T00:00:00Z')
        """,
        """
        CREATE TABLE routing_lan_scans (
            scan_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK (
                status IN (
                    'draft', 'running', 'cancelling', 'cancelled',
                    'completed', 'failed', 'interrupted'
                )
            ),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            owner_principal TEXT NOT NULL,
            confirmed_interface_id TEXT NOT NULL,
            network TEXT NOT NULL,
            limits_json TEXT NOT NULL,
            limits_digest TEXT NOT NULL,
            preview_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            cancel_reason TEXT,
            terminal_reason TEXT,
            candidate_count INTEGER CHECK (
                candidate_count IS NULL OR candidate_count >= 0
            ),
            error_count INTEGER CHECK (error_count IS NULL OR error_count >= 0),
            timeout_count INTEGER CHECK (timeout_count IS NULL OR timeout_count >= 0),
            terminal_receipt_json TEXT,
            terminal_receipt_digest TEXT,
            CHECK (
                (terminal_receipt_json IS NULL AND terminal_receipt_digest IS NULL)
                OR
                (terminal_receipt_json IS NOT NULL
                 AND terminal_receipt_digest IS NOT NULL)
            )
        )
        """,
        """
        CREATE TABLE routing_lan_observations (
            scan_id TEXT NOT NULL,
            endpoint_id TEXT NOT NULL,
            source TEXT NOT NULL CHECK (source IN ('mdns', 'active', 'manual')),
            interface_id TEXT NOT NULL,
            address TEXT NOT NULL,
            port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
            api_shape TEXT,
            tls_enabled INTEGER NOT NULL CHECK (tls_enabled IN (0, 1)),
            certificate_sha256 TEXT,
            catalog_digest TEXT,
            capability_digest TEXT,
            public_payload_json TEXT NOT NULL,
            freshness_timestamp TEXT NOT NULL,
            error_category TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scan_id, endpoint_id),
            UNIQUE (scan_id, endpoint_id),
            FOREIGN KEY (scan_id)
                REFERENCES routing_lan_scans(scan_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TABLE routing_lan_scan_events (
            scan_id TEXT NOT NULL,
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (scan_id, sequence),
            FOREIGN KEY (scan_id)
                REFERENCES routing_lan_scans(scan_id) ON DELETE RESTRICT
        )
        """,
        """
        CREATE TRIGGER trg_routing_lan_scan_id_update_immutable
        BEFORE UPDATE OF scan_id ON routing_lan_scans
        WHEN NEW.scan_id <> OLD.scan_id
        BEGIN
            SELECT RAISE(ABORT, 'lan_scan_identity_immutable');
        END
        """,
        """
        CREATE TRIGGER trg_routing_lan_pristine_draft_insert_required
        BEFORE INSERT ON routing_lan_scans
        WHEN NEW.status <> 'draft'
          OR NEW.revision <> 1
          OR NEW.created_at <> NEW.updated_at
          OR NEW.started_at IS NOT NULL
          OR NEW.finished_at IS NOT NULL
          OR NEW.cancel_reason IS NOT NULL
          OR NEW.terminal_reason IS NOT NULL
          OR NEW.candidate_count IS NOT NULL
          OR NEW.error_count IS NOT NULL
          OR NEW.timeout_count IS NOT NULL
          OR NEW.terminal_receipt_json IS NOT NULL
          OR NEW.terminal_receipt_digest IS NOT NULL
        BEGIN
            SELECT RAISE(ABORT, 'lan_scan_insert_requires_pristine_draft');
        END
        """,
    )
    with state._connect() as connection:
        for statement in statements:
            connection.execute(statement)


def test_create_scan_persists_canonical_limits_digest_and_enforces_foreign_keys(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    scan = _create_scan(lan_ledger)
    expected_json = json.dumps(_limits(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
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


@pytest.mark.parametrize(
    ("field", "lookalike"),
    [
        ("max_active_hosts", 256.0),
        ("known_model_service_ports", (1234.0, 8000, 8080, 11434)),
    ],
)
def test_scan_limits_reject_json_number_lookalikes_with_wrong_exact_types(
    lan_ledger: LanDiscoveryLedger,
    field: str,
    lookalike: object,
) -> None:
    limits = _limits()
    limits[field] = lookalike

    with pytest.raises(ValueError, match="fixed bounded limits"):
        lan_ledger.create_scan(
            scan_id="lan_float_limits",
            owner_principal="owner:test",
            confirmed_interface_id=INTERFACE_ID,
            network="192.168.10.0/30",
            limits=limits,
            preview_digest=PREVIEW_DIGEST,
            expected_revision=0,
        )

    assert lan_ledger.get_scan("lan_float_limits") is None


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


def test_public_evidence_schema_preserves_safe_canonical_bounded_metadata(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    running = _running_scan(lan_ledger)
    stored = lan_ledger.append_observation(
        running.scan_id,
        _observation(
            public_payload={
                "service": "ollama",
                "model_count": 1,
                "metadata": {
                    "display_name": "Studio model service",
                    "vendor": "Ollama",
                },
            }
        ),
        expected_revision=running.revision,
    )

    assert stored.public_payload == {
        "metadata": {
            "display_name": "Studio model service",
            "vendor": "Ollama",
        },
        "model_count": 1,
        "service": "ollama",
    }
    serialized = json.dumps(
        stored.public_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    assert serialized == (
        '{"metadata":{"display_name":"Studio model service","vendor":"Ollama"},'
        '"model_count":1,"service":"ollama"}'
    )

    current = lan_ledger.get_scan(running.scan_id)
    assert current is not None
    with pytest.raises(ValueError, match="metadata.description"):
        lan_ledger.append_observation(
            running.scan_id,
            replace(
                _observation(
                    endpoint_id="sha256:" + "7" * 64,
                    address="192.168.10.2",
                ),
                public_payload={
                    "service": "ollama",
                    "metadata": {"description": "x" * 20_000},
                },
            ),
            expected_revision=current.revision,
        )
    assert lan_ledger.get_scan(running.scan_id) == current


@pytest.mark.parametrize(
    "unsafe_payload",
    [
        {"body": '{"models":[{"name":"raw-provider-response"}]}'},
        {"set-cookie": "session=must-not-persist"},
        {"proxy-authorization": "Basic must-not-persist"},
        {"metadata": {"api_key": "must-not-persist"}},
        {b"authorization": "Bearer must-not-persist"},
    ],
)
def test_observation_public_evidence_rejects_raw_bodies_credentials_and_non_string_keys(
    lan_ledger: LanDiscoveryLedger,
    unsafe_payload: dict[object, object],
) -> None:
    running = _running_scan(lan_ledger)

    with pytest.raises(ValueError, match="public evidence|string keys"):
        lan_ledger.append_observation(
            running.scan_id,
            replace(_observation(), public_payload=unsafe_payload),  # type: ignore[arg-type]
            expected_revision=running.revision,
        )

    assert lan_ledger.list_observations(running.scan_id) == []
    assert lan_ledger.get_scan(running.scan_id) == running


def test_event_public_evidence_rejects_raw_provider_material(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    running = _running_scan(lan_ledger)

    with pytest.raises(ValueError, match="event public evidence"):
        lan_ledger.append_event(
            running.scan_id,
            "probe_finished",
            {"body": "raw-provider-response"},
            expected_revision=running.revision,
        )

    assert lan_ledger.list_events(running.scan_id) == []
    assert lan_ledger.get_scan(running.scan_id) == running


def test_sqlite_terminal_guards_reject_direct_scan_observation_and_event_rewrites(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger)
    with pytest.raises(sqlite3.IntegrityError, match="terminal_fields_require_terminal_state"):
        with state._connect() as connection:
            connection.execute(
                """
                INSERT INTO routing_lan_scans (
                    scan_id, status, revision, owner_principal,
                    confirmed_interface_id, network, limits_json, limits_digest,
                    preview_digest, created_at, updated_at, terminal_receipt_json,
                    terminal_receipt_digest
                )
                SELECT 'lan_invalid_fields', 'draft', 1, owner_principal,
                       confirmed_interface_id, network, limits_json, limits_digest,
                       preview_digest, created_at, updated_at, '{}', ?
                FROM routing_lan_scans WHERE scan_id = ?
                """,
                ("sha256:" + "9" * 64, running.scan_id),
            )
    with pytest.raises(sqlite3.IntegrityError, match="terminal_fields_require_terminal_state"):
        with state._connect() as connection:
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET terminal_receipt_json = '{}', terminal_receipt_digest = ?
                WHERE scan_id = ?
                """,
                ("sha256:" + "9" * 64, running.scan_id),
            )

    lan_ledger.append_observation(
        running.scan_id,
        _observation(),
        expected_revision=running.revision,
    )
    observed = lan_ledger.get_scan(running.scan_id)
    assert observed is not None
    lan_ledger.append_event(
        running.scan_id,
        "probe_finished",
        {"ok": True, "port": 11434},
        expected_revision=observed.revision,
    )
    with_event = lan_ledger.get_scan(running.scan_id)
    assert with_event is not None
    terminal = lan_ledger.transition_scan(
        running.scan_id,
        "completed",
        expected_revision=with_event.revision,
        terminal_reason="scan_complete",
        candidate_count=1,
        error_count=0,
        timeout_count=0,
    )
    source = _running_scan(lan_ledger, scan_id="lan_source")
    lan_ledger.append_observation(
        source.scan_id,
        _observation(
            endpoint_id="sha256:" + "8" * 64,
            address="192.168.10.2",
        ),
        expected_revision=source.revision,
    )
    source_observed = lan_ledger.get_scan(source.scan_id)
    assert source_observed is not None
    lan_ledger.append_event(
        source.scan_id,
        "probe_started",
        {"port": 11434},
        expected_revision=source_observed.revision,
    )
    source_event = lan_ledger.get_scan(source.scan_id)
    assert source_event is not None
    lan_ledger.append_event(
        source.scan_id,
        "probe_finished",
        {"ok": True, "port": 11434},
        expected_revision=source_event.revision,
    )

    direct_mutations = (
        (
            "UPDATE routing_lan_scans SET terminal_reason = 'rewritten' WHERE scan_id = ?",
            (terminal.scan_id,),
        ),
        ("DELETE FROM routing_lan_scans WHERE scan_id = ?", (terminal.scan_id,)),
        (
            """
            INSERT INTO routing_lan_observations (
                scan_id, endpoint_id, source, interface_id, address, port,
                api_shape, tls_enabled, certificate_sha256, catalog_digest,
                capability_digest, public_payload_json, freshness_timestamp,
                error_category, created_at
            ) VALUES (?, ?, 'active', ?, '192.168.10.2', 11434, 'ollama', 0,
                      NULL, NULL, NULL, '{}', '2026-08-01T12:01:00Z', NULL,
                      '2026-08-01T12:01:00Z')
            """,
            (terminal.scan_id, "sha256:" + "8" * 64, INTERFACE_ID),
        ),
        (
            "UPDATE routing_lan_observations SET api_shape = 'rewritten' WHERE scan_id = ?",
            (terminal.scan_id,),
        ),
        (
            "DELETE FROM routing_lan_observations WHERE scan_id = ?",
            (terminal.scan_id,),
        ),
        (
            "UPDATE routing_lan_observations SET scan_id = ? WHERE scan_id = ?",
            (terminal.scan_id, source.scan_id),
        ),
        (
            """
            INSERT INTO routing_lan_scan_events
                (scan_id, sequence, event_type, payload_json, created_at)
            VALUES (?, 2, 'late', '{}', '2026-08-01T12:01:00Z')
            """,
            (terminal.scan_id,),
        ),
        (
            "UPDATE routing_lan_scan_events SET event_type = 'rewritten' WHERE scan_id = ?",
            (terminal.scan_id,),
        ),
        (
            "DELETE FROM routing_lan_scan_events WHERE scan_id = ?",
            (terminal.scan_id,),
        ),
        (
            """
            UPDATE routing_lan_scan_events SET scan_id = ?
            WHERE scan_id = ? AND sequence = 2
            """,
            (terminal.scan_id, source.scan_id),
        ),
    )
    for statement, parameters in direct_mutations:
        with pytest.raises(sqlite3.IntegrityError, match="terminal_lan_scan_immutable"):
            with state._connect() as connection:
                connection.execute(statement, parameters)

    assert lan_ledger.get_scan(terminal.scan_id) == terminal
    assert len(lan_ledger.list_observations(terminal.scan_id)) == 1
    assert len(lan_ledger.list_events(terminal.scan_id)) == 1


def test_insert_or_replace_cannot_overwrite_zero_child_terminal_scan(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    draft = _create_scan(lan_ledger, scan_id="lan_replace_terminal")
    running = lan_ledger.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )
    terminal = lan_ledger.transition_scan(
        running.scan_id,
        "completed",
        expected_revision=running.revision,
        terminal_reason="zero_child_complete",
        candidate_count=0,
        error_count=0,
        timeout_count=0,
    )
    assert lan_ledger.list_observations(terminal.scan_id) == []
    assert lan_ledger.list_events(terminal.scan_id) == []
    with state._connect() as connection:
        before = connection.execute(
            "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
            (terminal.scan_id,),
        ).fetchone()
    assert before is not None
    before_values = tuple(before)
    before_bytes = json.dumps(
        before_values,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    with pytest.raises(sqlite3.IntegrityError, match="terminal_lan_scan_immutable"):
        with state._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO routing_lan_scans (
                    scan_id, status, revision, owner_principal,
                    confirmed_interface_id, network, limits_json, limits_digest,
                    preview_digest, created_at, updated_at
                )
                SELECT scan_id, 'draft', 1, owner_principal,
                       confirmed_interface_id, network, limits_json, limits_digest,
                       preview_digest, '2026-08-02T00:00:00Z',
                       '2026-08-02T00:00:00Z'
                FROM routing_lan_scans WHERE scan_id = ?
                """,
                (terminal.scan_id,),
            )

    with state._connect() as connection:
        after = connection.execute(
            "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
            (terminal.scan_id,),
        ).fetchone()
    assert after is not None
    after_values = tuple(after)
    after_bytes = json.dumps(
        after_values,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert after_values == before_values
    assert after_bytes == before_bytes
    assert lan_ledger.get_scan(terminal.scan_id) == terminal


def test_update_or_replace_cannot_rename_draft_over_zero_child_terminal_scan(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    terminal_draft = _create_scan(
        lan_ledger,
        scan_id="lan_update_replace_terminal",
    )
    running = lan_ledger.transition_scan(
        terminal_draft.scan_id,
        "running",
        expected_revision=terminal_draft.revision,
    )
    terminal = lan_ledger.transition_scan(
        running.scan_id,
        "completed",
        expected_revision=running.revision,
        terminal_reason="zero_child_complete",
        candidate_count=0,
        error_count=0,
        timeout_count=0,
    )
    draft = _create_scan(lan_ledger, scan_id="lan_update_replace_draft")
    for scan_id in (terminal.scan_id, draft.scan_id):
        assert lan_ledger.list_observations(scan_id) == []
        assert lan_ledger.list_events(scan_id) == []

    with state._connect() as connection:
        before_rows = connection.execute(
            "SELECT * FROM routing_lan_scans WHERE scan_id IN (?, ?) ORDER BY scan_id",
            (terminal.scan_id, draft.scan_id),
        ).fetchall()
    before_values = tuple(tuple(row) for row in before_rows)
    assert len(before_values) == 2
    before_bytes = json.dumps(
        before_values,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")

    with pytest.raises(sqlite3.IntegrityError, match="lan_scan_identity_immutable"):
        with state._connect() as connection:
            connection.execute(
                """
                UPDATE OR REPLACE routing_lan_scans
                SET scan_id = ?
                WHERE scan_id = ?
                """,
                (terminal.scan_id, draft.scan_id),
            )

    with state._connect() as connection:
        after_rows = connection.execute(
            "SELECT * FROM routing_lan_scans WHERE scan_id IN (?, ?) ORDER BY scan_id",
            (terminal.scan_id, draft.scan_id),
        ).fetchall()
    after_values = tuple(tuple(row) for row in after_rows)
    after_bytes = json.dumps(
        after_values,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert after_values == before_values
    assert after_bytes == before_bytes
    assert lan_ledger.get_scan(terminal.scan_id) == terminal
    assert lan_ledger.get_scan(draft.scan_id) == draft


def test_scan_identity_rejects_valid_id_to_null_update(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    draft = _create_scan(lan_ledger, scan_id="lan_valid_to_null")
    before_values, before_bytes = _lan_table_snapshot(state, "routing_lan_scans")

    with pytest.raises(sqlite3.IntegrityError, match="lan_scan_identity_immutable"):
        with state._connect() as connection:
            connection.execute(
                "UPDATE routing_lan_scans SET scan_id = NULL WHERE scan_id = ?",
                (draft.scan_id,),
            )

    after_values, after_bytes = _lan_table_snapshot(state, "routing_lan_scans")
    assert after_values == before_values
    assert after_bytes == before_bytes
    assert lan_ledger.get_scan(draft.scan_id) == draft


def test_pristine_scan_insert_rejects_null_identity(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    source = _create_scan(lan_ledger, scan_id="lan_null_insert_source")
    before_values, before_bytes = _lan_table_snapshot(state, "routing_lan_scans")

    with pytest.raises(
        sqlite3.IntegrityError,
        match="lan_scan_insert_requires_pristine_draft",
    ):
        with state._connect() as connection:
            connection.execute(
                """
                INSERT INTO routing_lan_scans (
                    scan_id, status, revision, owner_principal,
                    confirmed_interface_id, network, limits_json, limits_digest,
                    preview_digest, created_at, updated_at, started_at, finished_at,
                    cancel_reason, terminal_reason, candidate_count, error_count,
                    timeout_count, terminal_receipt_json, terminal_receipt_digest
                )
                SELECT NULL, status, revision, owner_principal,
                       confirmed_interface_id, network, limits_json, limits_digest,
                       preview_digest, created_at, updated_at, started_at, finished_at,
                       cancel_reason, terminal_reason, candidate_count, error_count,
                       timeout_count, terminal_receipt_json, terminal_receipt_digest
                FROM routing_lan_scans WHERE scan_id = ?
                """,
                (source.scan_id,),
            )

    after_values, after_bytes = _lan_table_snapshot(state, "routing_lan_scans")
    assert after_values == before_values
    assert after_bytes == before_bytes
    with state._connect() as connection:
        null_count = connection.execute(
            "SELECT COUNT(*) FROM routing_lan_scans WHERE scan_id IS NULL"
        ).fetchone()
    assert null_count is not None
    assert null_count[0] == 0


def test_existing_v3_null_scan_identity_guards_upgrade_without_trigger_replacement(
    tmp_path: Path,
) -> None:
    legacy_state = AgentStateStore(tmp_path / "legacy-v3" / "agent.db")
    _install_pre_hardening_nullable_v3_schema(legacy_state)
    limits_json = json.dumps(
        _limits(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    limits_digest = "sha256:" + hashlib.sha256(limits_json.encode("utf-8")).hexdigest()

    with legacy_state._connect() as connection:
        columns = {
            str(row[1]): int(row[3])
            for row in connection.execute("PRAGMA table_info(routing_lan_scans)").fetchall()
        }
        assert columns["scan_id"] == 0
        connection.execute(
            """
            INSERT INTO routing_lan_scans (
                scan_id, status, revision, owner_principal,
                confirmed_interface_id, network, limits_json, limits_digest,
                preview_digest, created_at, updated_at
            ) VALUES (
                NULL, 'draft', 1, 'owner:test', ?, '192.168.10.0/30', ?, ?, ?,
                '2026-08-01T00:00:00Z', '2026-08-01T00:00:00Z'
            )
            """,
            (INTERFACE_ID, limits_json, limits_digest, PREVIEW_DIGEST),
        )

    before_values, before_bytes = _lan_table_snapshot(
        legacy_state,
        "routing_lan_scans",
    )
    assert len(before_values) == 1
    LanDiscoveryLedger(legacy_state)
    with legacy_state._connect() as connection:
        restored_guards = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'trigger'
                  AND name IN (
                      'trg_routing_lan_pristine_draft_insert_required',
                      'trg_routing_lan_scan_id_update_null_safe_immutable',
                      'trg_routing_lan_scan_id_insert_not_null'
                  )
                """
            ).fetchall()
        }
    assert "trg_routing_lan_pristine_draft_insert_required" in restored_guards

    with pytest.raises(sqlite3.IntegrityError, match="lan_scan_identity_immutable"):
        with legacy_state._connect() as connection:
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET scan_id = 'lan_legacy_null_reassigned'
                WHERE scan_id IS NULL
                """
            )

    after_values, after_bytes = _lan_table_snapshot(
        legacy_state,
        "routing_lan_scans",
    )
    assert after_values == before_values
    assert after_bytes == before_bytes

    with pytest.raises(sqlite3.IntegrityError, match="lan_scan_identity_required"):
        with legacy_state._connect() as connection:
            connection.execute(
                """
                INSERT INTO routing_lan_scans (
                    scan_id, status, revision, owner_principal,
                    confirmed_interface_id, network, limits_json, limits_digest,
                    preview_digest, created_at, updated_at
                ) VALUES (
                    NULL, 'draft', 1, 'owner:second', ?, '192.168.10.0/30', ?, ?, ?,
                    '2026-08-01T00:00:01Z', '2026-08-01T00:00:01Z'
                )
                """,
                (INTERFACE_ID, limits_json, limits_digest, PREVIEW_DIGEST),
            )

    final_values, final_bytes = _lan_table_snapshot(
        legacy_state,
        "routing_lan_scans",
    )
    assert final_values == before_values
    assert final_bytes == before_bytes
    assert "trg_routing_lan_scan_id_update_null_safe_immutable" in restored_guards
    assert "trg_routing_lan_scan_id_insert_not_null" in restored_guards


def test_fresh_v3_scan_identity_is_explicitly_not_null(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    _create_scan(lan_ledger, scan_id="lan_fresh_not_null")
    with state._connect() as connection:
        columns = {
            str(row[1]): int(row[3])
            for row in connection.execute("PRAGMA table_info(routing_lan_scans)").fetchall()
        }

    assert columns["scan_id"] == 1


@pytest.mark.parametrize("operation", ["insert", "update"])
def test_rowid_replace_cannot_delete_zero_child_terminal_scan(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
    operation: str,
) -> None:
    terminal_draft = _create_scan(
        lan_ledger,
        scan_id=f"lan_rowid_{operation}_terminal",
    )
    running = lan_ledger.transition_scan(
        terminal_draft.scan_id,
        "running",
        expected_revision=terminal_draft.revision,
    )
    terminal = lan_ledger.transition_scan(
        running.scan_id,
        "completed",
        expected_revision=running.revision,
        terminal_reason="zero_child_complete",
        candidate_count=0,
        error_count=0,
        timeout_count=0,
    )
    draft = _create_scan(
        lan_ledger,
        scan_id=f"lan_rowid_{operation}_draft",
    )
    for scan_id in (terminal.scan_id, draft.scan_id):
        assert lan_ledger.list_observations(scan_id) == []
        assert lan_ledger.list_events(scan_id) == []

    with state._connect() as connection:
        terminal_rowid = connection.execute(
            "SELECT rowid FROM routing_lan_scans WHERE scan_id = ?",
            (terminal.scan_id,),
        ).fetchone()
    assert terminal_rowid is not None
    before_values, before_bytes = _lan_table_snapshot(state, "routing_lan_scans")

    with pytest.raises(sqlite3.IntegrityError, match="terminal_lan_scan_immutable"):
        with state._connect() as connection:
            if operation == "insert":
                connection.execute(
                    """
                    INSERT OR REPLACE INTO routing_lan_scans (
                        rowid, scan_id, status, revision, owner_principal,
                        confirmed_interface_id, network, limits_json, limits_digest,
                        preview_digest, created_at, updated_at, started_at, finished_at,
                        cancel_reason, terminal_reason, candidate_count, error_count,
                        timeout_count, terminal_receipt_json, terminal_receipt_digest
                    )
                    SELECT ?, 'lan_rowid_insert_replacement', status, revision,
                           owner_principal, confirmed_interface_id, network, limits_json,
                           limits_digest, preview_digest, created_at, updated_at,
                           started_at, finished_at, cancel_reason, terminal_reason,
                           candidate_count, error_count, timeout_count,
                           terminal_receipt_json, terminal_receipt_digest
                    FROM routing_lan_scans WHERE scan_id = ?
                    """,
                    (terminal_rowid[0], draft.scan_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE OR REPLACE routing_lan_scans
                    SET rowid = ?
                    WHERE scan_id = ?
                    """,
                    (terminal_rowid[0], draft.scan_id),
                )

    after_values, after_bytes = _lan_table_snapshot(state, "routing_lan_scans")
    assert after_values == before_values
    assert after_bytes == before_bytes
    assert lan_ledger.get_scan(terminal.scan_id) == terminal
    assert lan_ledger.get_scan(draft.scan_id) == draft
    assert lan_ledger.get_scan("lan_rowid_insert_replacement") is None


@pytest.mark.parametrize(
    ("table", "operation"),
    [
        pytest.param("routing_lan_observations", "insert", id="observation-insert"),
        pytest.param("routing_lan_observations", "update", id="observation-update"),
        pytest.param("routing_lan_scan_events", "insert", id="event-insert"),
        pytest.param("routing_lan_scan_events", "update", id="event-update"),
    ],
)
def test_rowid_replace_cannot_delete_terminal_child_evidence(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
    table: str,
    operation: str,
) -> None:
    kind = "observation" if table == "routing_lan_observations" else "event"
    terminal_running = _running_scan(
        lan_ledger,
        scan_id=f"lan_rowid_{kind}_{operation}_terminal",
    )
    if kind == "observation":
        lan_ledger.append_observation(
            terminal_running.scan_id,
            _observation(),
            expected_revision=terminal_running.revision,
        )
        terminal_current = lan_ledger.get_scan(terminal_running.scan_id)
        terminal_candidate_count = 1
    else:
        lan_ledger.append_event(
            terminal_running.scan_id,
            "probe_finished",
            {"ok": True},
            expected_revision=terminal_running.revision,
        )
        terminal_current = lan_ledger.get_scan(terminal_running.scan_id)
        terminal_candidate_count = 0
    assert terminal_current is not None
    terminal = lan_ledger.transition_scan(
        terminal_current.scan_id,
        "completed",
        expected_revision=terminal_current.revision,
        terminal_reason="child_evidence_complete",
        candidate_count=terminal_candidate_count,
        error_count=0,
        timeout_count=0,
    )
    source = _running_scan(
        lan_ledger,
        scan_id=f"lan_rowid_{kind}_{operation}_source",
    )
    if operation == "update" and kind == "observation":
        lan_ledger.append_observation(
            source.scan_id,
            _observation(
                endpoint_id="sha256:" + "6" * 64,
                address="192.168.10.2",
            ),
            expected_revision=source.revision,
        )
        source = lan_ledger.get_scan(source.scan_id)
    elif operation == "update":
        lan_ledger.append_event(
            source.scan_id,
            "probe_started",
            {"port": 11434},
            expected_revision=source.revision,
        )
        source = lan_ledger.get_scan(source.scan_id)
    assert source is not None

    if kind == "observation":
        assert len(lan_ledger.list_observations(terminal.scan_id)) == 1
        target_key: tuple[object, ...] = (
            terminal.scan_id,
            "sha256:" + "3" * 64,
        )
        target_query = (
            "SELECT rowid FROM routing_lan_observations WHERE scan_id = ? AND endpoint_id = ?"
        )
    else:
        assert len(lan_ledger.list_events(terminal.scan_id)) == 1
        target_key = (terminal.scan_id, 1)
        target_query = (
            "SELECT rowid FROM routing_lan_scan_events WHERE scan_id = ? AND sequence = ?"
        )
    with state._connect() as connection:
        terminal_rowid = connection.execute(target_query, target_key).fetchone()
    assert terminal_rowid is not None
    before_values, before_bytes = _lan_table_snapshot(state, table)

    with pytest.raises(sqlite3.IntegrityError, match="terminal_lan_scan_immutable"):
        with state._connect() as connection:
            if kind == "observation" and operation == "insert":
                connection.execute(
                    """
                    INSERT OR REPLACE INTO routing_lan_observations (
                        rowid, scan_id, endpoint_id, source, interface_id, address,
                        port, api_shape, tls_enabled, certificate_sha256,
                        catalog_digest, capability_digest, public_payload_json,
                        freshness_timestamp, error_category, created_at
                    )
                    SELECT ?, ?, ?, source, interface_id, address, port, api_shape,
                           tls_enabled, certificate_sha256, catalog_digest,
                           capability_digest, public_payload_json,
                           freshness_timestamp, error_category, created_at
                    FROM routing_lan_observations
                    WHERE scan_id = ? AND endpoint_id = ?
                    """,
                    (
                        terminal_rowid[0],
                        source.scan_id,
                        "sha256:" + "7" * 64,
                        *target_key,
                    ),
                )
            elif kind == "observation":
                connection.execute(
                    """
                    UPDATE OR REPLACE routing_lan_observations
                    SET rowid = ?
                    WHERE scan_id = ? AND endpoint_id = ?
                    """,
                    (
                        terminal_rowid[0],
                        source.scan_id,
                        "sha256:" + "6" * 64,
                    ),
                )
            elif operation == "insert":
                connection.execute(
                    """
                    INSERT OR REPLACE INTO routing_lan_scan_events (
                        rowid, scan_id, sequence, event_type, payload_json, created_at
                    )
                    SELECT ?, ?, 1, event_type, payload_json, created_at
                    FROM routing_lan_scan_events
                    WHERE scan_id = ? AND sequence = ?
                    """,
                    (terminal_rowid[0], source.scan_id, *target_key),
                )
            else:
                connection.execute(
                    """
                    UPDATE OR REPLACE routing_lan_scan_events
                    SET rowid = ?
                    WHERE scan_id = ? AND sequence = 1
                    """,
                    (terminal_rowid[0], source.scan_id),
                )

    after_values, after_bytes = _lan_table_snapshot(state, table)
    assert after_values == before_values
    assert after_bytes == before_bytes
    assert lan_ledger.get_scan(terminal.scan_id) == terminal
    assert lan_ledger.get_scan(source.scan_id) == source


@pytest.mark.parametrize(
    ("status", "revision", "started_at"),
    [
        pytest.param("completed", 1, None, id="terminal_without_receipt"),
        pytest.param("running", 1, None, id="non_draft_status"),
        pytest.param("draft", 2, None, id="non_initial_revision"),
        pytest.param("draft", 1, "2026-08-02T00:00:01Z", id="started_draft"),
    ],
)
def test_direct_scan_insert_requires_pristine_revision_one_draft(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
    status: str,
    revision: int,
    started_at: str | None,
) -> None:
    source = _create_scan(lan_ledger, scan_id="lan_insert_source")

    with pytest.raises(sqlite3.IntegrityError, match="lan_scan_insert_requires_pristine_draft"):
        with state._connect() as connection:
            connection.execute(
                """
                INSERT INTO routing_lan_scans (
                    scan_id, status, revision, owner_principal,
                    confirmed_interface_id, network, limits_json, limits_digest,
                    preview_digest, created_at, updated_at, started_at
                )
                SELECT 'lan_malformed_insert', ?, ?, owner_principal,
                       confirmed_interface_id, network, limits_json, limits_digest,
                       preview_digest, created_at, updated_at, ?
                FROM routing_lan_scans WHERE scan_id = ?
                """,
                (status, revision, started_at, source.scan_id),
            )

    assert lan_ledger.get_scan("lan_malformed_insert") is None


def test_direct_terminal_transition_requires_complete_receipt_shape(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    draft = _create_scan(lan_ledger, scan_id="lan_incomplete_terminal")
    with state._connect() as connection:
        before = connection.execute(
            "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
            (draft.scan_id,),
        ).fetchone()
    assert before is not None

    with pytest.raises(sqlite3.IntegrityError, match="terminal_lan_scan_evidence_incomplete"):
        with state._connect() as connection:
            connection.execute(
                """
                UPDATE routing_lan_scans
                SET status = 'completed', revision = revision + 1
                WHERE scan_id = ?
                """,
                (draft.scan_id,),
            )

    with state._connect() as connection:
        after = connection.execute(
            "SELECT * FROM routing_lan_scans WHERE scan_id = ?",
            (draft.scan_id,),
        ).fetchone()
    assert after is not None
    assert tuple(after) == tuple(before)
    assert lan_ledger.get_scan(draft.scan_id) == draft


def test_observation_insert_rolls_back_when_revision_update_fails_in_sqlite(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger)
    with state._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER test_reject_lan_revision_update
            BEFORE UPDATE OF revision ON routing_lan_scans
            WHEN OLD.scan_id = 'lan_1'
            BEGIN
                SELECT RAISE(ABORT, 'test_revision_update_rejected');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="test_revision_update_rejected"):
        lan_ledger.append_observation(
            running.scan_id,
            _observation(),
            expected_revision=running.revision,
        )

    assert lan_ledger.list_observations(running.scan_id) == []
    assert lan_ledger.get_scan(running.scan_id) == running


def _independent_terminal_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> str:
    owner = "owner:test"
    interface_id = INTERFACE_ID
    network = "192.168.10.0/30"
    preview_digest = PREVIEW_DIGEST
    terminal_status = "completed"
    terminal_reason = "scan_complete"
    candidate_count = 1
    error_count = 0
    timeout_count = 0
    observation = _observation()
    timestamps = [
        "2026-08-01T12:00:00Z",
        "2026-08-01T12:00:01Z",
        "2026-08-01T12:00:02Z",
        "2026-08-01T12:00:03Z",
    ]

    if variant == "owner":
        owner = "owner:other"
    elif variant == "interface":
        interface_id = "sha256:" + "a" * 64
        observation = replace(observation, interface_id=interface_id)
    elif variant == "network":
        network = "192.168.20.0/30"
        observation = replace(observation, address="192.168.20.1")
    elif variant == "preview":
        preview_digest = "sha256:" + "b" * 64
    elif variant == "started_at":
        timestamps[1] = "2026-08-01T12:00:00Z"
    elif variant == "observation_created_at":
        timestamps[2] = "2026-08-01T12:00:01Z"
    elif variant == "finished_at":
        timestamps[3] = "2026-08-01T12:00:04Z"
    elif variant == "status":
        terminal_status = "failed"
    elif variant == "terminal_reason":
        terminal_reason = "probe_failure"
    elif variant == "candidate_count":
        candidate_count = 2
    elif variant == "error_count":
        error_count = 1
    elif variant == "timeout_count":
        error_count = 1
        timeout_count = 1
    elif variant == "endpoint_id":
        observation = replace(observation, endpoint_id="sha256:" + "c" * 64)
    elif variant == "source":
        observation = replace(observation, source="mdns")
    elif variant == "address":
        observation = replace(observation, address="192.168.10.2")
    elif variant == "port":
        observation = replace(observation, port=1234)
    elif variant == "api_shape":
        observation = replace(observation, api_shape="openai-compatible")
    elif variant == "tls_evidence":
        observation = replace(
            observation,
            tls_enabled=True,
            certificate_sha256="sha256:" + "d" * 64,
        )
    elif variant == "catalog_digest":
        observation = replace(observation, catalog_digest="sha256:" + "e" * 64)
    elif variant == "capability_digest":
        observation = replace(observation, capability_digest="sha256:" + "f" * 64)
    elif variant == "public_payload":
        observation = replace(
            observation,
            public_payload={
                "service": "ollama",
                "model_count": 1,
                "metadata": {"display_name": "Changed service"},
            },
        )
    elif variant == "freshness":
        observation = replace(observation, freshness_timestamp="2026-08-01T11:59:59Z")
    elif variant == "error_category":
        observation = replace(observation, error_category="connection_refused")

    clock = iter(timestamps)
    monkeypatch.setattr(lan_ledger_module, "utc_now", clock.__next__)
    state = AgentStateStore(tmp_path / variant / "agent.db")
    ledger = LanDiscoveryLedger(state)
    draft = ledger.create_scan(
        scan_id="lan_digest",
        owner_principal=owner,
        confirmed_interface_id=interface_id,
        network=network,
        limits=_limits(),
        preview_digest=preview_digest,
        expected_revision=0,
    )
    running = ledger.transition_scan(
        draft.scan_id,
        "running",
        expected_revision=draft.revision,
    )
    ledger.append_observation(
        running.scan_id,
        observation,
        expected_revision=running.revision,
    )
    observed = ledger.get_scan(running.scan_id)
    assert observed is not None
    terminal = ledger.transition_scan(
        running.scan_id,
        terminal_status,
        expected_revision=observed.revision,
        terminal_reason=terminal_reason,
        candidate_count=candidate_count,
        error_count=error_count,
        timeout_count=timeout_count,
    )
    assert terminal.terminal_receipt_digest is not None
    return terminal.terminal_receipt_digest


def test_server_derived_receipt_digest_changes_for_each_load_bearing_evidence_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    variants = (
        "owner",
        "interface",
        "network",
        "preview",
        "started_at",
        "observation_created_at",
        "finished_at",
        "status",
        "terminal_reason",
        "candidate_count",
        "error_count",
        "timeout_count",
        "endpoint_id",
        "source",
        "address",
        "port",
        "api_shape",
        "tls_evidence",
        "catalog_digest",
        "capability_digest",
        "public_payload",
        "freshness",
        "error_category",
    )
    baseline = _independent_terminal_digest(tmp_path, monkeypatch, "baseline")
    variant_digests = {
        variant: _independent_terminal_digest(tmp_path, monkeypatch, variant)
        for variant in variants
    }

    assert all(digest != baseline for digest in variant_digests.values())
    assert len(set(variant_digests.values())) == len(variants)


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
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
    assert {
        "idx_routing_lan_scans_status_updated",
        "idx_routing_lan_scan_events_poll",
    } <= indexes
    expected_guards = {
        "trg_routing_lan_scan_id_insert_not_null",
        "trg_routing_lan_scan_id_update_immutable",
        "trg_routing_lan_scan_id_update_null_safe_immutable",
        "trg_routing_lan_pristine_draft_insert_required",
        "trg_routing_lan_terminal_scan_id_replace_immutable",
        "trg_routing_lan_terminal_transition_complete",
        "trg_routing_lan_terminal_fields_require_terminal",
        "trg_routing_lan_terminal_fields_insert_require_terminal",
        "trg_routing_lan_terminal_scan_update_immutable",
        "trg_routing_lan_terminal_scan_delete_immutable",
        "trg_routing_lan_terminal_observation_insert_immutable",
        "trg_routing_lan_terminal_observation_update_immutable",
        "trg_routing_lan_terminal_observation_delete_immutable",
        "trg_routing_lan_terminal_event_insert_immutable",
        "trg_routing_lan_terminal_event_update_immutable",
        "trg_routing_lan_terminal_event_delete_immutable",
    }
    assert expected_guards <= triggers

    with state._connect() as connection:
        connection.execute("DROP TRIGGER trg_routing_lan_terminal_observation_insert_immutable")
        connection.execute("DROP TRIGGER trg_routing_lan_scan_id_update_immutable")
        connection.execute("DROP TRIGGER trg_routing_lan_scan_id_update_null_safe_immutable")
        connection.execute("DROP TRIGGER trg_routing_lan_scan_id_insert_not_null")
    LanDiscoveryLedger(state)
    with state._connect() as connection:
        restored = {
            str(row[0])
            for row in connection.execute(
                """
            SELECT name FROM sqlite_master
            WHERE type = 'trigger'
              AND name IN (
                  'trg_routing_lan_terminal_observation_insert_immutable',
                  'trg_routing_lan_scan_id_update_immutable',
                  'trg_routing_lan_scan_id_update_null_safe_immutable',
                  'trg_routing_lan_scan_id_insert_not_null'
              )
            """
            ).fetchall()
        }
    assert restored == {
        "trg_routing_lan_terminal_observation_insert_immutable",
        "trg_routing_lan_scan_id_update_immutable",
        "trg_routing_lan_scan_id_update_null_safe_immutable",
        "trg_routing_lan_scan_id_insert_not_null",
    }


def test_generic_registry_fences_reserved_lan_ids_and_nested_metadata(
    state: AgentStateStore,
) -> None:
    registry = RoutingLedger(state)

    with pytest.raises(ValueError, match="lan.*reserved"):
        registry.put_provider_profile(
            ProviderProfile(
                profile_id="lan-provider-" + "1" * 64,
                display_name="forged LAN provider",
                adapter="lan-openai-compatible",
                locality="local",
            ),
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="lan.*reserved"):
        registry.put_provider_profile(
            ProviderProfile(
                profile_id="lan-target-" + "1" * 64,
                display_name="cross-prefix forged LAN provider",
                adapter="mock",
            ),
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="lan_discovery"):
        registry.put_provider_profile(
            ProviderProfile(
                profile_id="ordinary-provider",
                display_name="ordinary",
                adapter="mock",
                metadata={"nested": {"lan_discovery": {"managed": True}}},
            ),
            expected_revision=0,
        )

    ordinary = registry.put_provider_profile(
        ProviderProfile(
            profile_id="ordinary-provider",
            display_name="ordinary",
            adapter="mock",
            metadata={"purpose": "regression control"},
        ),
        expected_revision=0,
    )
    assert ordinary.revision == 1
    with pytest.raises(ValueError, match="lan.*reserved"):
        registry.put_model_target(
            ModelTarget(
                target_id="lan-target-" + "2" * 64,
                provider_profile_id="ordinary-provider",
                provider="mock",
                model="forged",
            ),
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="lan.*reserved"):
        registry.put_model_target(
            ModelTarget(
                target_id="lan-provider-" + "2" * 64,
                provider_profile_id="ordinary-provider",
                provider="mock",
                model="cross-prefix-forged",
            ),
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="lan.*reserved"):
        registry.put_model_target(
            ModelTarget(
                target_id="ordinary-cross-provider",
                provider_profile_id="lan-target-" + "3" * 64,
                provider="mock",
                model="cross-prefix-provider",
            ),
            expected_revision=0,
        )
    with pytest.raises(ValueError, match="lan.*reserved"):
        registry.apply_provider_inventory(
            ordinary.profile,
            expected_profile_revision=ordinary.revision,
            target_updates=(
                (
                    ModelTarget(
                        target_id="lan-provider-" + "4" * 64,
                        provider_profile_id=ordinary.profile.profile_id,
                        provider=ordinary.profile.adapter,
                        model="inventory-cross-prefix",
                    ),
                    0,
                ),
            ),
        )
    assert registry.get_provider_profile(ordinary.profile.profile_id) == ordinary
    assert registry.list_model_targets() == []


@pytest.mark.parametrize("expected_revision", [False, True, -1, 0.0, "0"])
def test_generic_registry_revisions_require_exact_nonnegative_ints(
    state: AgentStateStore,
    expected_revision: object,
) -> None:
    registry = RoutingLedger(state)

    with pytest.raises(ValueError, match="revision"):
        registry.put_provider_profile(
            ProviderProfile(
                profile_id="ordinary-strict-revision",
                display_name="ordinary",
                adapter="mock",
            ),
            expected_revision=expected_revision,  # type: ignore[arg-type]
        )
    assert registry.get_provider_profile("ordinary-strict-revision") is None


def test_inventory_rejects_boolean_target_revision_before_mutation(
    state: AgentStateStore,
) -> None:
    registry = RoutingLedger(state)
    profile = ProviderProfile(
        profile_id="ordinary-inventory",
        display_name="ordinary",
        adapter="mock",
    )
    registry.put_provider_profile(profile, expected_revision=0)
    target = ModelTarget(
        target_id="ordinary-target",
        provider_profile_id=profile.profile_id,
        provider=profile.adapter,
        model="mock-model",
    )

    with pytest.raises(ValueError, match="revision"):
        registry.apply_provider_inventory(
            profile,
            expected_profile_revision=1,
            target_updates=((target, False),),
        )

    assert registry.get_provider_profile(profile.profile_id).revision == 1  # type: ignore[union-attr]
    assert registry.get_model_target(target.target_id) is None


def test_generic_put_and_inventory_cannot_mutate_existing_lan_managed_profile(
    state: AgentStateStore,
) -> None:
    registry = RoutingLedger(state)
    profile_id = "lan-provider-" + "a" * 64
    metadata = {
        "lan_discovery": {
            "schema": "kestrel.lan.provider-profile.v1",
            "managed": True,
        }
    }
    with state._connect() as connection:
        connection.execute(
            """
            INSERT INTO routing_provider_profiles (
                profile_id, display_name, adapter, base_url, secret_ref, enabled,
                locality, trust_class, max_concurrency, metadata_json, revision,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 0, 'local', 'unconfirmed', 1, ?, 1, ?, ?)
            """,
            (
                profile_id,
                "LAN managed",
                "lan-openai-compatible",
                "http://192.168.50.2:1234/v1",
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                "2026-08-01T12:00:00Z",
                "2026-08-01T12:00:00Z",
            ),
        )
    before = registry.get_provider_profile(profile_id)
    assert before is not None
    proposed = ProviderProfile(
        profile_id=profile_id,
        display_name="generic mutation",
        adapter="lan-openai-compatible",
        base_url="http://192.168.50.2:1234/v1",
        enabled=False,
        locality="local",
        trust_class="unconfirmed",
        metadata=metadata,
    )

    with pytest.raises(ValueError, match="lan.*managed"):
        registry.put_provider_profile(proposed, expected_revision=before.revision)
    with pytest.raises(ValueError, match="lan.*managed"):
        registry.apply_provider_inventory(
            proposed,
            expected_profile_revision=before.revision,
            target_updates=(),
        )

    assert registry.get_provider_profile(profile_id) == before


def _insert_existing_managed_target_under_ordinary_profile(
    state: AgentStateStore,
) -> tuple[RoutingLedger, ProviderProfile, ModelTarget]:
    registry = RoutingLedger(state)
    profile = ProviderProfile(
        profile_id="ordinary-target-container",
        display_name="ordinary container",
        adapter="mock",
    )
    registry.put_provider_profile(profile, expected_revision=0)
    target = ModelTarget(
        target_id="lan-target-" + "c" * 64,
        provider_profile_id=profile.profile_id,
        provider=profile.adapter,
        model="managed-model",
        enabled=False,
        metadata={
            "lan_discovery": {
                "schema": "kestrel.lan.model-target-binding.v1",
                "managed": True,
            }
        },
    )
    with state._connect() as connection:
        connection.execute(
            """
            INSERT INTO routing_model_targets (
                target_id, provider_profile_id, provider, model, enabled, locality,
                trust_class, capability_tags_json, role_affinities_json,
                task_family_affinities_json, max_context_tokens, supports_tools,
                supports_json, supports_vision, supports_reasoning, supports_streaming,
                quality_tier, latency_tier, operator_priority, estimated_cost_usd,
                input_cost_per_million_usd, output_cost_per_million_usd, health,
                recent_failure_rate, predicted_success, metadata_json, revision,
                created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, 0, 'cloud', 'unconfirmed', '[]', '[]', '[]', NULL,
                0, 0, 0, 0, 0, 1, 3, 0, NULL, NULL, NULL, 'unknown', 0.0,
                NULL, ?, 1, ?, ?
            )
            """,
            (
                target.target_id,
                target.provider_profile_id,
                target.provider,
                target.model,
                json.dumps(target.metadata, sort_keys=True, separators=(",", ":")),
                "2026-08-01T12:00:00Z",
                "2026-08-01T12:00:00Z",
            ),
        )
    return registry, profile, target


def test_generic_target_mutations_fence_existing_managed_target_symmetrically(
    state: AgentStateStore,
) -> None:
    registry, profile, target = _insert_existing_managed_target_under_ordinary_profile(state)
    before_profile = registry.get_provider_profile(profile.profile_id)
    before_target = registry.get_model_target(target.target_id)
    assert before_profile is not None and before_target is not None
    proposed = replace(target, enabled=True, trust_class="operator_confirmed")

    with pytest.raises(ValueError, match="lan.*managed"):
        registry.put_model_target(proposed, expected_revision=before_target.revision)
    with pytest.raises(ValueError, match="lan.*managed"):
        registry.apply_provider_inventory(
            profile,
            expected_profile_revision=before_profile.revision,
            target_updates=((proposed, before_target.revision),),
        )

    assert registry.get_provider_profile(profile.profile_id) == before_profile
    assert registry.get_model_target(target.target_id) == before_target

    ordinary = ModelTarget(
        target_id="ordinary-control-target",
        provider_profile_id=profile.profile_id,
        provider=profile.adapter,
        model="ordinary-model",
    )
    created = registry.put_model_target(ordinary, expected_revision=0)
    updated = registry.put_model_target(
        replace(ordinary, enabled=False),
        expected_revision=created.revision,
    )
    assert updated.revision == 2


def test_nested_lan_metadata_is_reserved_for_targets_too(state: AgentStateStore) -> None:
    registry = RoutingLedger(state)
    profile = registry.put_provider_profile(
        ProviderProfile(
            profile_id="nested-target-profile",
            display_name="ordinary",
            adapter="mock",
        ),
        expected_revision=0,
    )
    forged = ModelTarget(
        target_id="ordinary-looking-target",
        provider_profile_id=profile.profile.profile_id,
        provider=profile.profile.adapter,
        model="forged",
        metadata={"outer": {"lan_discovery": {"managed": True}}},
    )

    with pytest.raises(ValueError, match="lan_discovery"):
        registry.put_model_target(forged, expected_revision=0)
    assert registry.get_model_target(forged.target_id) is None

    with pytest.raises(ValueError, match="lan_discovery"):
        registry.apply_provider_inventory(
            profile.profile,
            expected_profile_revision=profile.revision,
            target_updates=((forged, 0),),
        )
    assert registry.get_provider_profile(profile.profile.profile_id) == profile
    assert registry.get_model_target(forged.target_id) is None


@pytest.mark.parametrize("expected_revision", [False, True, -1, 0.0, "0"])
def test_direct_target_revision_is_an_exact_nonnegative_int(
    state: AgentStateStore,
    expected_revision: object,
) -> None:
    registry = RoutingLedger(state)
    profile = registry.put_provider_profile(
        ProviderProfile(
            profile_id="strict-target-profile",
            display_name="ordinary",
            adapter="mock",
        ),
        expected_revision=0,
    )
    target = ModelTarget(
        target_id="strict-target",
        provider_profile_id=profile.profile.profile_id,
        provider=profile.profile.adapter,
        model="strict-model",
    )

    with pytest.raises(ValueError, match="revision"):
        registry.put_model_target(
            target,
            expected_revision=expected_revision,  # type: ignore[arg-type]
        )
    assert registry.get_model_target(target.target_id) is None


@pytest.mark.parametrize(
    ("revision_field", "expected_revision"),
    [(field, value) for field in ("profile", "target") for value in (False, True, -1, 0.0, "0")],
)
def test_every_inventory_revision_is_an_exact_nonnegative_int(
    state: AgentStateStore,
    revision_field: str,
    expected_revision: object,
) -> None:
    registry = RoutingLedger(state)
    profile = ProviderProfile(
        profile_id="strict-inventory-profile",
        display_name="ordinary",
        adapter="mock",
    )
    persisted = registry.put_provider_profile(profile, expected_revision=0)
    target = ModelTarget(
        target_id="strict-inventory-target",
        provider_profile_id=profile.profile_id,
        provider=profile.adapter,
        model="strict-model",
    )
    profile_revision: object = persisted.revision
    target_revision: object = 0
    if revision_field == "profile":
        profile_revision = expected_revision
    else:
        target_revision = expected_revision

    with pytest.raises(ValueError, match="revision"):
        registry.apply_provider_inventory(
            profile,
            expected_profile_revision=profile_revision,  # type: ignore[arg-type]
            target_updates=((target, target_revision),),  # type: ignore[arg-type]
        )
    assert registry.get_provider_profile(profile.profile_id) == persisted
    assert registry.get_model_target(target.target_id) is None
