from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread

import pytest

import nested_memvid_agent.routing.lan_ledger as lan_ledger_module
from nested_memvid_agent.lan_discovery_models import (
    LanScanLimits,
    NetworkInterface,
    ResolvedLanEndpoint,
)
from nested_memvid_agent.lan_discovery_scope import PrivateScanScope
from nested_memvid_agent.lan_scanner import (
    LanFailureCategory,
    Reachability,
    _make_observation,
)
from nested_memvid_agent.routing.lan_ledger import LanDiscoveryLedger
from nested_memvid_agent.routing.lan_records import (
    ALLOWED_SCAN_TRANSITIONS,
    SCAN_STATES,
    LanObservationDraft,
    LanScanRevisionConflict,
    LanScanTransitionError,
)
from nested_memvid_agent.routing.lan_serialization import lan_observation_to_draft
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


def _preview_event() -> dict[str, object]:
    return {
        "schema": "kestrel.lan.scan-preview.v1",
        "owner_principal": "owner:test",
        "interface_id": INTERFACE_ID,
        "network": "192.168.10.0/30",
        "limits": _limits(),
        "active_host_count": 2,
        "passive_or_manual_only": False,
        "port_count": 4,
        "mdns_status": "available",
        "server_version": "kestrel-test",
        "contract_version": "kestrel.lan.preview-authorization.v1",
        "preview_digest": PREVIEW_DIGEST,
        "expires_at": "2099-08-01T12:00:30Z",
    }


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


def _task4_observation(
    scope: PrivateScanScope,
    address: str,
    port: int,
) -> LanObservationDraft:
    endpoint = ResolvedLanEndpoint.from_scope(scope, address, port)
    observation = _make_observation(
        endpoint,
        reachability=Reachability.UNREACHABLE,
        failure_category=LanFailureCategory.TCP_REFUSED,
    )
    return lan_observation_to_draft(
        observation,
        scope=scope,
        freshness_timestamp="2026-08-01T12:00:00Z",
        source="active",
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


def _lan_state_bytes(state: AgentStateStore) -> tuple[bytes, bytes, bytes]:
    return tuple(
        _lan_table_snapshot(state, table)[1]
        for table in (
            "routing_lan_scans",
            "routing_lan_observations",
            "routing_lan_scan_events",
        )
    )  # type: ignore[return-value]


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


def test_claim_scan_start_is_one_transaction_with_event_before_running_state(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    draft = _create_scan(lan_ledger, scan_id="lan_atomic_start")
    with state._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER test_require_start_event_before_running
            BEFORE UPDATE OF status ON routing_lan_scans
            WHEN NEW.scan_id = 'lan_atomic_start' AND NEW.status = 'running'
             AND NOT EXISTS (
                SELECT 1 FROM routing_lan_scan_events
                WHERE scan_id = NEW.scan_id AND event_type = 'scan_started'
             )
            BEGIN
                SELECT RAISE(ABORT, 'start_event_missing');
            END
            """
        )

    running = lan_ledger.claim_scan_start(
        draft.scan_id,
        owner_principal="owner:test",
        expected_revision=draft.revision,
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event=_preview_event(),
    )

    assert running.status == "running"
    assert running.revision == draft.revision + 1
    assert [event.event_type for event in lan_ledger.list_events(draft.scan_id)] == ["scan_started"]
    assert lan_ledger.list_events(draft.scan_id)[0].payload == json.loads(
        json.dumps(_preview_event())
    )


@pytest.mark.parametrize("cross_during_precommit", [False, True])
def test_claim_scan_start_rejects_expiry_equality_and_transaction_crossing_without_writes(
    state: AgentStateStore,
    cross_during_precommit: bool,
) -> None:
    expires_at = datetime(2026, 8, 1, 12, 0, 30, tzinfo=UTC)
    now = [expires_at - timedelta(microseconds=1)]

    def precommit(operation: str) -> None:
        if operation == "claim_scan_start" and cross_during_precommit:
            now[0] = expires_at + timedelta(microseconds=1)

    ledger = LanDiscoveryLedger(
        state,
        utc_clock=lambda: now[0],
        precommit_hook=precommit,
    )
    draft = _create_scan(ledger, scan_id="lan_expiring_claim")
    preview_event = {
        **_preview_event(),
        "expires_at": expires_at.isoformat().replace("+00:00", "Z"),
    }
    if not cross_during_precommit:
        now[0] = expires_at
    before = _lan_state_bytes(state)

    with pytest.raises(ValueError, match="^LAN preview authorization expired$"):
        ledger.claim_scan_start(
            draft.scan_id,
            owner_principal="owner:test",
            expected_revision=draft.revision,
            preview_digest=PREVIEW_DIGEST,
            authorized_preview_digest=PREVIEW_DIGEST,
            preview_event=preview_event,
        )

    assert _lan_state_bytes(state) == before
    assert ledger.get_scan(draft.scan_id) == draft
    assert ledger.list_events(draft.scan_id) == []


def test_claim_scan_start_rejects_foreign_stale_and_second_active_without_writes(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    first = _create_scan(lan_ledger, scan_id="lan_first")
    second = _create_scan(lan_ledger, scan_id="lan_second")
    running = lan_ledger.claim_scan_start(
        first.scan_id,
        owner_principal="owner:test",
        expected_revision=first.revision,
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event=_preview_event(),
    )

    for kwargs in (
        {"owner_principal": "owner:foreign", "expected_revision": second.revision},
        {"owner_principal": "owner:test", "expected_revision": 0},
    ):
        with pytest.raises((ValueError, LanScanRevisionConflict)):
            lan_ledger.claim_scan_start(
                second.scan_id,
                preview_digest=PREVIEW_DIGEST,
                authorized_preview_digest=PREVIEW_DIGEST,
                preview_event=_preview_event(),
                **kwargs,
            )
    with pytest.raises(RuntimeError, match="active"):
        lan_ledger.claim_scan_start(
            second.scan_id,
            owner_principal="owner:test",
            expected_revision=second.revision,
            preview_digest=PREVIEW_DIGEST,
            authorized_preview_digest=PREVIEW_DIGEST,
            preview_event=_preview_event(),
        )

    assert lan_ledger.get_scan(first.scan_id) == running
    assert lan_ledger.get_scan(second.scan_id) == second
    assert lan_ledger.list_events(second.scan_id) == []


def test_request_cancel_commits_event_and_state_before_token_layer_can_signal(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger, scan_id="lan_atomic_cancel")
    with state._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER test_require_cancel_event_before_cancelling
            BEFORE UPDATE OF status ON routing_lan_scans
            WHEN NEW.scan_id = 'lan_atomic_cancel' AND NEW.status = 'cancelling'
             AND NOT EXISTS (
                SELECT 1 FROM routing_lan_scan_events
                WHERE scan_id = NEW.scan_id AND event_type = 'scan_cancel_requested'
             )
            BEGIN
                SELECT RAISE(ABORT, 'cancel_event_missing');
            END
            """
        )

    cancelling = lan_ledger.request_scan_cancel(
        running.scan_id,
        owner_principal="owner:test",
        expected_revision=running.revision,
        cancel_reason="owner_cancelled",
    )

    assert cancelling.status == "cancelling"
    assert cancelling.cancel_reason == "owner_cancelled"
    assert [event.event_type for event in lan_ledger.list_events(running.scan_id)] == [
        "scan_cancel_requested"
    ]


def test_draft_cancel_is_atomic_terminal_and_stale_or_foreign_cancel_is_zero_write(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    draft = _create_scan(lan_ledger, scan_id="lan_draft_cancel")
    before_events = lan_ledger.list_events(draft.scan_id)
    with pytest.raises(LanScanRevisionConflict):
        lan_ledger.request_scan_cancel(
            draft.scan_id,
            owner_principal="owner:test",
            expected_revision=0,
            cancel_reason="owner_cancelled",
        )
    with pytest.raises(ValueError, match="owner"):
        lan_ledger.request_scan_cancel(
            draft.scan_id,
            owner_principal="owner:foreign",
            expected_revision=draft.revision,
            cancel_reason="owner_cancelled",
        )
    assert lan_ledger.get_scan(draft.scan_id) == draft
    assert lan_ledger.list_events(draft.scan_id) == before_events

    cancelled = lan_ledger.request_scan_cancel(
        draft.scan_id,
        owner_principal="owner:test",
        expected_revision=draft.revision,
        cancel_reason="owner_cancelled",
    )
    assert cancelled.status == "cancelled"
    assert cancelled.terminal_receipt is not None
    assert cancelled.terminal_receipt["evidence_complete"] is True
    assert cancelled.terminal_receipt["unknown_inflight_count"] == 0
    assert [event.event_type for event in lan_ledger.list_events(draft.scan_id)] == [
        "scan_cancelled"
    ]


def test_start_vs_draft_cancel_has_exactly_one_revision_winner(
    state: AgentStateStore,
) -> None:
    first = LanDiscoveryLedger(state)
    second = LanDiscoveryLedger(state)
    draft = _create_scan(first, scan_id="lan_start_cancel_race")
    barrier = Barrier(2)
    outcomes: list[str] = []

    def claim() -> None:
        barrier.wait()
        try:
            first.claim_scan_start(
                draft.scan_id,
                owner_principal="owner:test",
                expected_revision=draft.revision,
                preview_digest=PREVIEW_DIGEST,
                authorized_preview_digest=PREVIEW_DIGEST,
                preview_event=_preview_event(),
            )
        except (LanScanRevisionConflict, LanScanTransitionError):
            outcomes.append("claim_lost")
        else:
            outcomes.append("claim_won")

    def cancel() -> None:
        barrier.wait()
        try:
            second.request_scan_cancel(
                draft.scan_id,
                owner_principal="owner:test",
                expected_revision=draft.revision,
                cancel_reason="owner_cancelled",
            )
        except (LanScanRevisionConflict, LanScanTransitionError):
            outcomes.append("cancel_lost")
        else:
            outcomes.append("cancel_won")

    threads = [Thread(target=claim), Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert len(outcomes) == 2
    assert len([outcome for outcome in outcomes if outcome.endswith("won")]) == 1
    current = first.get_scan(draft.scan_id)
    assert current is not None
    events = first.list_events(draft.scan_id)
    assert current.revision == draft.revision + 1
    assert first.list_observations(draft.scan_id) == []
    if current.status == "running":
        assert outcomes.count("claim_won") == 1
        assert outcomes.count("cancel_lost") == 1
        assert current.terminal_receipt is None
        assert [event.event_type for event in events] == ["scan_started"]
    else:
        assert current.status == "cancelled"
        assert outcomes.count("cancel_won") == 1
        assert outcomes.count("claim_lost") == 1
        assert current.terminal_receipt is not None
        assert [event.event_type for event in events] == ["scan_cancelled"]


def test_completion_vs_cancel_has_one_terminal_authority_winner(
    state: AgentStateStore,
) -> None:
    worker_ledger = LanDiscoveryLedger(state)
    cancel_ledger = LanDiscoveryLedger(state)
    running = _running_scan(worker_ledger, scan_id="lan_completion_cancel_race")
    barrier = Barrier(2)
    outcomes: list[str] = []

    def complete() -> None:
        barrier.wait()
        try:
            worker_ledger.commit_scan_terminal(
                running.scan_id,
                owner_principal="owner:test",
                expected_revision=running.revision,
                status="completed",
                terminal_reason="scan_complete",
                cancel_reason=None,
                observations=(),
                mdns_status="available",
                planned_count=0,
                admitted_count=0,
                completed_count=0,
                error_category_counts={},
                timeout_count=0,
                evidence_complete=True,
                unknown_inflight_count=0,
            )
        except LanScanRevisionConflict:
            outcomes.append("completion_lost")
        else:
            outcomes.append("completion_won")

    def cancel() -> None:
        barrier.wait()
        try:
            cancel_ledger.request_scan_cancel(
                running.scan_id,
                owner_principal="owner:test",
                expected_revision=running.revision,
                cancel_reason="owner_cancelled",
            )
        except LanScanRevisionConflict:
            outcomes.append("cancel_lost")
        else:
            outcomes.append("cancel_won")

    threads = [Thread(target=complete), Thread(target=cancel)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert len(outcomes) == 2
    assert len([outcome for outcome in outcomes if outcome.endswith("won")]) == 1
    current = worker_ledger.get_scan(running.scan_id)
    assert current is not None
    assert current.revision == running.revision + 1
    pre_terminal_events = worker_ledger.list_events(running.scan_id)
    if current.status == "cancelling":
        assert outcomes.count("cancel_won") == 1
        assert outcomes.count("completion_lost") == 1
        assert current.terminal_receipt is None
        assert [event.event_type for event in pre_terminal_events] == ["scan_cancel_requested"]
        current = worker_ledger.commit_scan_terminal(
            current.scan_id,
            owner_principal="owner:test",
            expected_revision=current.revision,
            status="cancelled",
            terminal_reason="owner_cancelled",
            cancel_reason="owner_cancelled",
            observations=(),
            mdns_status="available",
            planned_count=0,
            admitted_count=0,
            completed_count=0,
            error_category_counts={},
            timeout_count=0,
            evidence_complete=True,
            unknown_inflight_count=0,
        )
        assert current.revision == running.revision + 2
        assert [event.event_type for event in worker_ledger.list_events(running.scan_id)] == [
            "scan_cancel_requested",
            "scan_cancelled",
        ]
    else:
        assert current.status == "completed"
        assert outcomes.count("completion_won") == 1
        assert outcomes.count("cancel_lost") == 1
        assert [event.event_type for event in pre_terminal_events] == ["scan_completed"]
    assert current.status in {"completed", "cancelled"}
    assert current.terminal_receipt is not None
    assert worker_ledger.list_observations(running.scan_id) == []


def test_terminal_observations_event_receipt_and_status_roll_back_at_crash_hook(
    state: AgentStateStore,
) -> None:
    def crash(operation: str) -> None:
        if operation == "commit_scan_terminal":
            raise RuntimeError("injected terminal crash")

    ledger = LanDiscoveryLedger(state, precommit_hook=crash)
    running = _running_scan(ledger, scan_id="lan_terminal_rollback")

    with pytest.raises(RuntimeError, match="injected terminal crash"):
        ledger.commit_scan_terminal(
            running.scan_id,
            owner_principal="owner:test",
            expected_revision=running.revision,
            status="completed",
            terminal_reason="scan_complete",
            cancel_reason=None,
            observations=(_observation(),),
            mdns_status="available",
            planned_count=1,
            admitted_count=1,
            completed_count=1,
            error_category_counts={},
            timeout_count=0,
            evidence_complete=True,
            unknown_inflight_count=0,
        )

    assert ledger.get_scan(running.scan_id) == running
    assert ledger.list_observations(running.scan_id) == []
    assert ledger.list_events(running.scan_id) == []


@pytest.mark.parametrize(
    ("case", "hook_operation"),
    (
        ("start", "claim_scan_start"),
        ("running_cancel", "request_scan_cancel"),
        ("draft_cancel", "request_scan_cancel"),
        ("progress", "record_scan_progress"),
        ("terminal", "commit_scan_terminal"),
        ("interrupt", "interrupt_active_scans"),
    ),
)
def test_every_specialized_lifecycle_crash_hook_is_byte_for_byte_zero_write(
    state: AgentStateStore,
    case: str,
    hook_operation: str,
) -> None:
    seed = LanDiscoveryLedger(state)
    if case in {"start", "draft_cancel"}:
        current = _create_scan(seed, scan_id=f"lan_crash_{case}")
    else:
        current = _running_scan(seed, scan_id=f"lan_crash_{case}")
    before = _lan_state_bytes(state)

    def crash(operation: str) -> None:
        assert operation == hook_operation
        raise RuntimeError(f"injected {case} crash")

    ledger = LanDiscoveryLedger(state, precommit_hook=crash)
    with pytest.raises(RuntimeError, match=f"injected {case} crash"):
        if case == "start":
            ledger.claim_scan_start(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=current.revision,
                preview_digest=PREVIEW_DIGEST,
                authorized_preview_digest=PREVIEW_DIGEST,
                preview_event=_preview_event(),
            )
        elif case in {"running_cancel", "draft_cancel"}:
            ledger.request_scan_cancel(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=current.revision,
                cancel_reason="owner_cancelled",
            )
        elif case == "progress":
            ledger.record_scan_progress(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=current.revision,
                planned_count=4,
                admitted_count=1,
                completed_count=1,
                persisted_observation_count=1,
                error_category_counts={},
                timeout_count=0,
                mdns_status="available",
                observations=(_observation(),),
            )
        elif case == "terminal":
            ledger.commit_scan_terminal(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=current.revision,
                status="completed",
                terminal_reason="scan_complete",
                cancel_reason=None,
                observations=(_observation(),),
                mdns_status="available",
                planned_count=1,
                admitted_count=1,
                completed_count=1,
                error_category_counts={},
                timeout_count=0,
                evidence_complete=True,
                unknown_inflight_count=0,
            )
        else:
            ledger.interrupt_active_scans(owner_principal="owner:test")

    assert _lan_state_bytes(state) == before


def test_terminal_events_exist_before_completed_or_cancelled_status_update(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger, scan_id="lan_event_before_completed")
    failing = _running_scan(lan_ledger, scan_id="lan_event_before_failed")
    draft = _create_scan(lan_ledger, scan_id="lan_event_before_cancelled")
    with state._connect() as connection:
        connection.executescript(
            """
            CREATE TRIGGER test_completed_event_precedes_terminal_status
            BEFORE UPDATE OF status ON routing_lan_scans
            WHEN NEW.scan_id = 'lan_event_before_completed' AND NEW.status = 'completed'
             AND NOT EXISTS (
                SELECT 1 FROM routing_lan_scan_events
                WHERE scan_id = NEW.scan_id AND event_type = 'scan_completed'
             )
            BEGIN
                SELECT RAISE(ABORT, 'completed_event_missing');
            END;
            CREATE TRIGGER test_cancelled_event_precedes_terminal_status
            BEFORE UPDATE OF status ON routing_lan_scans
            WHEN NEW.scan_id = 'lan_event_before_cancelled' AND NEW.status = 'cancelled'
             AND NOT EXISTS (
                SELECT 1 FROM routing_lan_scan_events
                WHERE scan_id = NEW.scan_id AND event_type = 'scan_cancelled'
             )
            BEGIN
                SELECT RAISE(ABORT, 'cancelled_event_missing');
            END;
            CREATE TRIGGER test_failed_event_precedes_terminal_status
            BEFORE UPDATE OF status ON routing_lan_scans
            WHEN NEW.scan_id = 'lan_event_before_failed' AND NEW.status = 'failed'
             AND NOT EXISTS (
                SELECT 1 FROM routing_lan_scan_events
                WHERE scan_id = NEW.scan_id AND event_type = 'scan_failed'
             )
            BEGIN
                SELECT RAISE(ABORT, 'failed_event_missing');
            END;
            """
        )

    completed = lan_ledger.commit_scan_terminal(
        running.scan_id,
        owner_principal="owner:test",
        expected_revision=running.revision,
        status="completed",
        terminal_reason="scan_complete",
        cancel_reason=None,
        observations=(),
        mdns_status="available",
        planned_count=0,
        admitted_count=0,
        completed_count=0,
        error_category_counts={},
        timeout_count=0,
        evidence_complete=True,
        unknown_inflight_count=0,
    )
    cancelled = lan_ledger.request_scan_cancel(
        draft.scan_id,
        owner_principal="owner:test",
        expected_revision=draft.revision,
        cancel_reason="owner_cancelled",
    )
    failed = lan_ledger.commit_scan_terminal(
        failing.scan_id,
        owner_principal="owner:test",
        expected_revision=failing.revision,
        status="failed",
        terminal_reason="worker_error",
        cancel_reason=None,
        observations=(),
        mdns_status="unavailable",
        planned_count=0,
        admitted_count=0,
        completed_count=0,
        error_category_counts={},
        timeout_count=0,
        evidence_complete=True,
        unknown_inflight_count=0,
    )

    assert completed.status == "completed"
    assert cancelled.status == "cancelled"
    assert failed.status == "failed"


def test_progress_is_monotonic_idempotent_and_persists_exact_observation_membership(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    scope = PrivateScanScope.from_request(
        NetworkInterface.from_addresses(
            os_identity="darwin:en-progress",
            display_name="Progress fixture",
            addresses=("192.168.73.1/30",),
        ),
        "192.168.73.0/30",
    )
    draft = lan_ledger.create_scan(
        scan_id="lan_progress_exact",
        owner_principal="owner:test",
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits=_limits(),
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = lan_ledger.claim_scan_start(
        draft.scan_id,
        owner_principal="owner:test",
        expected_revision=draft.revision,
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event={
            **_preview_event(),
            "interface_id": scope.interface.interface_id,
            "network": scope.network,
        },
    )
    first_observation = _task4_observation(scope, "192.168.73.1", 8000)
    second_observation = _task4_observation(scope, "192.168.73.2", 8080)

    admitted = lan_ledger.record_scan_progress(
        running.scan_id,
        owner_principal="owner:test",
        expected_revision=running.revision,
        planned_count=4,
        admitted_count=1,
        completed_count=0,
        persisted_observation_count=0,
        error_category_counts={},
        timeout_count=0,
        mdns_status="available",
        observations=(),
    )
    completed_one = lan_ledger.record_scan_progress(
        admitted.scan_id,
        owner_principal="owner:test",
        expected_revision=admitted.revision,
        planned_count=4,
        admitted_count=1,
        completed_count=1,
        persisted_observation_count=1,
        error_category_counts={"tcp_refused": 1},
        timeout_count=0,
        mdns_status="available",
        observations=(first_observation,),
    )
    assert admitted.revision == running.revision + 1
    assert completed_one.revision == admitted.revision + 1
    durable_observations = lan_ledger.list_observations(running.scan_id)
    assert len(durable_observations) == 1
    assert durable_observations[0].endpoint_id == first_observation.endpoint_id
    after_progress = _lan_state_bytes(state)

    duplicate = lan_ledger.record_scan_progress(
        completed_one.scan_id,
        owner_principal="owner:test",
        expected_revision=completed_one.revision,
        planned_count=4,
        admitted_count=1,
        completed_count=1,
        persisted_observation_count=1,
        error_category_counts={"tcp_refused": 1},
        timeout_count=0,
        mdns_status="available",
        observations=(first_observation,),
    )
    assert duplicate == completed_one
    assert _lan_state_bytes(state) == after_progress

    for invalid in (
        {
            "planned_count": 4,
            "admitted_count": 0,
            "completed_count": 0,
            "persisted_observation_count": 1,
            "error_category_counts": {"tcp_refused": 1},
            "timeout_count": 0,
            "mdns_status": "available",
        },
        {
            "planned_count": 4,
            "admitted_count": 1,
            "completed_count": 1,
            "persisted_observation_count": 1,
            "error_category_counts": {"not_closed": 1},
            "timeout_count": 0,
            "mdns_status": "available",
        },
        {
            "planned_count": 4,
            "admitted_count": 1,
            "completed_count": 1,
            "persisted_observation_count": 1,
            "error_category_counts": {"tcp_refused": 1},
            "timeout_count": 0,
            "mdns_status": "lookalike",
        },
    ):
        with pytest.raises(ValueError):
            lan_ledger.record_scan_progress(
                completed_one.scan_id,
                owner_principal="owner:test",
                expected_revision=completed_one.revision,
                observations=(),
                **invalid,
            )
        assert _lan_state_bytes(state) == after_progress

    terminal = lan_ledger.commit_scan_terminal(
        completed_one.scan_id,
        owner_principal="owner:test",
        expected_revision=completed_one.revision,
        status="completed",
        terminal_reason="scan_complete",
        cancel_reason=None,
        observations=(first_observation, second_observation),
        mdns_status="available",
        planned_count=4,
        admitted_count=2,
        completed_count=2,
        error_category_counts={"tcp_refused": 2},
        timeout_count=0,
        evidence_complete=True,
        unknown_inflight_count=0,
    )
    assert terminal.terminal_receipt is not None
    assert terminal.candidate_count == 2
    assert terminal.error_count == 2
    assert terminal.terminal_receipt["persisted_observation_count"] == 2
    assert terminal.terminal_receipt["error_category_counts"] == {"tcp_refused": 2}
    assert len(lan_ledger.list_observations(running.scan_id)) == 2
    assert [event.event_type for event in lan_ledger.list_events(running.scan_id)] == [
        "scan_started",
        "scan_progress",
        "scan_progress",
        "scan_completed",
    ]


def test_specialized_progress_rejects_planned_work_growth_without_writes(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger, scan_id="lan_fixed_plan")
    progressed = lan_ledger.record_scan_progress(
        running.scan_id,
        owner_principal="owner:test",
        expected_revision=running.revision,
        planned_count=1,
        admitted_count=0,
        completed_count=0,
        persisted_observation_count=0,
        error_category_counts={},
        timeout_count=0,
        mdns_status="available",
    )
    before = _lan_state_bytes(state)

    with pytest.raises(ValueError, match="planned count"):
        lan_ledger.record_scan_progress(
            running.scan_id,
            owner_principal="owner:test",
            expected_revision=progressed.revision,
            planned_count=2,
            admitted_count=0,
            completed_count=0,
            persisted_observation_count=0,
            error_category_counts={},
            timeout_count=0,
            mdns_status="available",
        )

    assert _lan_state_bytes(state) == before


def test_terminal_error_counts_must_equal_durable_observation_categories(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger, scan_id="lan_exact_error_counts")
    before = _lan_state_bytes(state)

    with pytest.raises(ValueError, match="durable observations"):
        lan_ledger.commit_scan_terminal(
            running.scan_id,
            owner_principal="owner:test",
            expected_revision=running.revision,
            status="completed",
            terminal_reason="scan_complete",
            cancel_reason=None,
            observations=(_observation(),),
            mdns_status="available",
            planned_count=1,
            admitted_count=1,
            completed_count=1,
            error_category_counts={"tcp_refused": 1},
            timeout_count=0,
            evidence_complete=True,
            unknown_inflight_count=0,
        )

    assert _lan_state_bytes(state) == before


def test_ordinary_terminal_requires_one_durable_observation_per_completion(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    running = _running_scan(lan_ledger, scan_id="lan_complete_evidence")
    before = _lan_state_bytes(state)

    with pytest.raises(ValueError, match="complete evidence"):
        lan_ledger.commit_scan_terminal(
            running.scan_id,
            owner_principal="owner:test",
            expected_revision=running.revision,
            status="completed",
            terminal_reason="scan_complete",
            cancel_reason=None,
            observations=(),
            mdns_status="available",
            planned_count=1,
            admitted_count=1,
            completed_count=1,
            error_category_counts={},
            timeout_count=0,
            evidence_complete=True,
            unknown_inflight_count=0,
        )

    assert _lan_state_bytes(state) == before


@pytest.mark.parametrize("operation", ("start", "cancel", "progress", "terminal"))
def test_specialized_lifecycle_rejects_boolean_revision_without_writes(
    state: AgentStateStore,
    operation: str,
) -> None:
    ledger = LanDiscoveryLedger(state)
    current = (
        _create_scan(ledger, scan_id=f"lan_bool_{operation}")
        if operation in {"start", "cancel"}
        else _running_scan(ledger, scan_id=f"lan_bool_{operation}")
    )
    before = _lan_state_bytes(state)

    with pytest.raises(LanScanRevisionConflict):
        if operation == "start":
            ledger.claim_scan_start(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=True,  # type: ignore[arg-type]
                preview_digest=PREVIEW_DIGEST,
                authorized_preview_digest=PREVIEW_DIGEST,
                preview_event=_preview_event(),
            )
        elif operation == "cancel":
            ledger.request_scan_cancel(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=True,  # type: ignore[arg-type]
                cancel_reason="owner_cancelled",
            )
        elif operation == "progress":
            ledger.record_scan_progress(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=True,  # type: ignore[arg-type]
                planned_count=0,
                admitted_count=0,
                completed_count=0,
                persisted_observation_count=0,
                error_category_counts={},
                timeout_count=0,
                mdns_status="available",
                observations=(),
            )
        else:
            ledger.commit_scan_terminal(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=True,  # type: ignore[arg-type]
                status="completed",
                terminal_reason="scan_complete",
                cancel_reason=None,
                observations=(),
                mdns_status="available",
                planned_count=0,
                admitted_count=0,
                completed_count=0,
                error_category_counts={},
                timeout_count=0,
                evidence_complete=True,
                unknown_inflight_count=0,
            )

    assert _lan_state_bytes(state) == before


def test_shared_revision_validator_rejects_bool_when_current_revision_is_one() -> None:
    from nested_memvid_agent.routing.lan_serialization import validate_exact_revision

    assert validate_exact_revision(1, actual_revision=1, scan_id="lan_revision_one") == 1
    with pytest.raises(LanScanRevisionConflict):
        validate_exact_revision(True, actual_revision=1, scan_id="lan_revision_one")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("mdns_status", "unknown"),
        ("error_category_counts", {"not_closed": 1}),
        ("status", "Completed"),
        ("terminal_reason", "raw exception text"),
    ),
)
def test_terminal_projection_rejects_open_enum_lookalikes_atomically(
    state: AgentStateStore,
    field: str,
    value: object,
) -> None:
    ledger = LanDiscoveryLedger(state)
    running = _running_scan(ledger, scan_id=f"lan_closed_{field}")
    before = _lan_state_bytes(state)
    kwargs: dict[str, object] = {
        "status": "completed",
        "terminal_reason": "scan_complete",
        "cancel_reason": None,
        "observations": (),
        "mdns_status": "available",
        "planned_count": 0,
        "admitted_count": 0,
        "completed_count": 0,
        "error_category_counts": {},
        "timeout_count": 0,
        "evidence_complete": True,
        "unknown_inflight_count": 0,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        ledger.commit_scan_terminal(
            running.scan_id,
            owner_principal="owner:test",
            expected_revision=running.revision,
            **kwargs,  # type: ignore[arg-type]
        )
    assert _lan_state_bytes(state) == before


@pytest.mark.parametrize(
    "operation",
    ("start", "cancel", "progress", "terminal", "recovery"),
)
def test_specialized_lifecycle_rejects_padded_owner_without_writes(
    state: AgentStateStore,
    operation: str,
) -> None:
    ledger = LanDiscoveryLedger(state)
    current = (
        _create_scan(ledger, scan_id=f"lan_owner_{operation}")
        if operation == "start"
        else _running_scan(ledger, scan_id=f"lan_owner_{operation}")
    )
    before = _lan_state_bytes(state)

    with pytest.raises(ValueError, match="^LAN owner principal must be exact$"):
        if operation == "start":
            ledger.claim_scan_start(
                current.scan_id,
                owner_principal=" owner:test ",
                expected_revision=current.revision,
                preview_digest=PREVIEW_DIGEST,
                authorized_preview_digest=PREVIEW_DIGEST,
                preview_event=_preview_event(),
            )
        elif operation == "cancel":
            ledger.request_scan_cancel(
                current.scan_id,
                owner_principal=" owner:test ",
                expected_revision=current.revision,
                cancel_reason="owner_cancelled",
            )
        elif operation == "progress":
            ledger.record_scan_progress(
                current.scan_id,
                owner_principal=" owner:test ",
                expected_revision=current.revision,
                planned_count=0,
                admitted_count=0,
                completed_count=0,
                persisted_observation_count=0,
                error_category_counts={},
                timeout_count=0,
                mdns_status="available",
                observations=(),
            )
        elif operation == "terminal":
            ledger.commit_scan_terminal(
                current.scan_id,
                owner_principal=" owner:test ",
                expected_revision=current.revision,
                status="completed",
                terminal_reason="scan_complete",
                cancel_reason=None,
                observations=(),
                mdns_status="available",
                planned_count=0,
                admitted_count=0,
                completed_count=0,
                error_category_counts={},
                timeout_count=0,
                evidence_complete=True,
                unknown_inflight_count=0,
            )
        else:
            ledger.interrupt_active_scans(owner_principal=" owner:test ")

    assert _lan_state_bytes(state) == before


def test_specialized_authority_and_legal_source_status_matrix_is_zero_write(
    state: AgentStateStore,
) -> None:
    ledger = LanDiscoveryLedger(state)

    mismatch = _create_scan(ledger, scan_id="lan_digest_mismatch")
    before = _lan_state_bytes(state)
    with pytest.raises(ValueError, match="preview"):
        ledger.claim_scan_start(
            mismatch.scan_id,
            owner_principal="owner:test",
            expected_revision=mismatch.revision,
            preview_digest=PREVIEW_DIGEST,
            authorized_preview_digest="sha256:" + "9" * 64,
            preview_event=_preview_event(),
        )
    assert _lan_state_bytes(state) == before
    before = _lan_state_bytes(state)
    with pytest.raises(ValueError, match="preview"):
        ledger.claim_scan_start(
            mismatch.scan_id,
            owner_principal="owner:test",
            expected_revision=mismatch.revision,
            preview_digest="sha256:" + "9" * 64,
            authorized_preview_digest="sha256:" + "9" * 64,
            preview_event={
                **_preview_event(),
                "preview_digest": "sha256:" + "9" * 64,
            },
        )
    assert _lan_state_bytes(state) == before

    running = _running_scan(ledger, scan_id="lan_illegal_running")
    progress_kwargs = {
        "planned_count": 0,
        "admitted_count": 0,
        "completed_count": 0,
        "persisted_observation_count": 0,
        "error_category_counts": {},
        "timeout_count": 0,
        "mdns_status": "available",
        "observations": (),
    }
    terminal_kwargs = {
        "terminal_reason": "scan_complete",
        "cancel_reason": None,
        "observations": (),
        "mdns_status": "available",
        "planned_count": 0,
        "admitted_count": 0,
        "completed_count": 0,
        "error_category_counts": {},
        "timeout_count": 0,
        "evidence_complete": True,
        "unknown_inflight_count": 0,
    }
    for operation in ("foreign_progress", "foreign_terminal", "running_cancelled", "interrupted"):
        before = _lan_state_bytes(state)
        with pytest.raises((ValueError, LanScanTransitionError)):
            if operation == "foreign_progress":
                ledger.record_scan_progress(
                    running.scan_id,
                    owner_principal="owner:foreign",
                    expected_revision=running.revision,
                    **progress_kwargs,  # type: ignore[arg-type]
                )
            else:
                selected_terminal_kwargs = dict(terminal_kwargs)
                if operation == "interrupted":
                    selected_terminal_kwargs.update(
                        terminal_reason="startup_interrupted",
                        evidence_complete=False,
                        unknown_inflight_count=None,
                    )
                ledger.commit_scan_terminal(
                    running.scan_id,
                    owner_principal=(
                        "owner:foreign" if operation == "foreign_terminal" else "owner:test"
                    ),
                    expected_revision=running.revision,
                    status=(
                        "cancelled"
                        if operation == "running_cancelled"
                        else ("interrupted" if operation == "interrupted" else "completed")
                    ),
                    **selected_terminal_kwargs,  # type: ignore[arg-type]
                )
        assert _lan_state_bytes(state) == before

    cancelling_draft = _create_scan(ledger, scan_id="lan_illegal_cancelling")
    cancelling = ledger.transition_scan(
        cancelling_draft.scan_id,
        "running",
        expected_revision=cancelling_draft.revision,
    )
    cancelling = ledger.transition_scan(
        cancelling.scan_id,
        "cancelling",
        expected_revision=cancelling.revision,
        cancel_reason="owner_cancelled",
    )
    for operation in ("progress", "completed", "failed", "interrupted"):
        before = _lan_state_bytes(state)
        with pytest.raises((ValueError, LanScanTransitionError)):
            if operation == "progress":
                ledger.record_scan_progress(
                    cancelling.scan_id,
                    owner_principal="owner:test",
                    expected_revision=cancelling.revision,
                    **progress_kwargs,  # type: ignore[arg-type]
                )
            else:
                selected_terminal_kwargs = {
                    key: value for key, value in terminal_kwargs.items() if key != "terminal_reason"
                }
                if operation == "interrupted":
                    selected_terminal_kwargs.update(
                        evidence_complete=False,
                        unknown_inflight_count=None,
                    )
                ledger.commit_scan_terminal(
                    cancelling.scan_id,
                    owner_principal="owner:test",
                    expected_revision=cancelling.revision,
                    status=operation,
                    terminal_reason=(
                        "worker_error"
                        if operation == "failed"
                        else (
                            "startup_interrupted" if operation == "interrupted" else "scan_complete"
                        )
                    ),
                    **selected_terminal_kwargs,  # type: ignore[arg-type]
                )
        assert _lan_state_bytes(state) == before


@pytest.mark.parametrize(
    ("case", "value"),
    (
        ("cancel_reason", 1),
        ("cancel_reason", "OWNER_CANCELLED"),
        ("cancel_reason", None),
        ("evidence_complete", 1),
        ("evidence_complete", "true"),
        ("evidence_complete", False),
        ("unknown_inflight_count", True),
        ("unknown_inflight_count", "0"),
        ("unknown_inflight_count", None),
        ("unknown_inflight_count", -1),
    ),
)
def test_cancel_and_terminal_exact_types_and_closed_values_are_atomic(
    state: AgentStateStore,
    case: str,
    value: object,
) -> None:
    ledger = LanDiscoveryLedger(state)
    current = (
        _create_scan(ledger, scan_id=f"lan_exact_{case}")
        if case == "cancel_reason"
        else _running_scan(ledger, scan_id=f"lan_exact_{case}")
    )
    before = _lan_state_bytes(state)
    with pytest.raises((TypeError, ValueError)):
        if case == "cancel_reason":
            ledger.request_scan_cancel(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=current.revision,
                cancel_reason=value,  # type: ignore[arg-type]
            )
        else:
            kwargs: dict[str, object] = {
                "status": "completed",
                "terminal_reason": "scan_complete",
                "cancel_reason": None,
                "observations": (),
                "mdns_status": "available",
                "planned_count": 0,
                "admitted_count": 0,
                "completed_count": 0,
                "error_category_counts": {},
                "timeout_count": 0,
                "evidence_complete": True,
                "unknown_inflight_count": 0,
            }
            kwargs[case] = value
            ledger.commit_scan_terminal(
                current.scan_id,
                owner_principal="owner:test",
                expected_revision=current.revision,
                **kwargs,  # type: ignore[arg-type]
            )
    assert _lan_state_bytes(state) == before


def test_conflicting_same_endpoint_replay_is_rejected_in_progress_and_terminal(
    state: AgentStateStore,
) -> None:
    ledger = LanDiscoveryLedger(state)
    scope = PrivateScanScope.from_request(
        NetworkInterface.from_addresses(
            os_identity="darwin:en-replay",
            display_name="Replay fixture",
            addresses=("192.168.75.1/30",),
        ),
        "192.168.75.0/30",
    )
    endpoint = ResolvedLanEndpoint.from_scope(scope, "192.168.75.2", 11434)
    first = lan_observation_to_draft(
        _make_observation(
            endpoint,
            reachability=Reachability.UNREACHABLE,
            failure_category=LanFailureCategory.TCP_REFUSED,
        ),
        scope=scope,
        freshness_timestamp="2026-08-01T12:00:00Z",
    )
    conflict = lan_observation_to_draft(
        _make_observation(
            endpoint,
            reachability=Reachability.UNREACHABLE,
            failure_category=LanFailureCategory.TCP_TIMEOUT,
        ),
        scope=scope,
        freshness_timestamp="2026-08-01T12:00:00Z",
    )
    assert first.endpoint_id == conflict.endpoint_id
    assert (
        first.public_payload["observation_digest"] != conflict.public_payload["observation_digest"]
    )
    draft = ledger.create_scan(
        scan_id="lan_replay",
        owner_principal="owner:test",
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits=_limits(),
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = ledger.claim_scan_start(
        draft.scan_id,
        owner_principal="owner:test",
        expected_revision=draft.revision,
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event={
            **_preview_event(),
            "interface_id": scope.interface.interface_id,
            "network": scope.network,
        },
    )
    progressed = ledger.record_scan_progress(
        running.scan_id,
        owner_principal="owner:test",
        expected_revision=running.revision,
        planned_count=1,
        admitted_count=1,
        completed_count=1,
        persisted_observation_count=1,
        error_category_counts={"tcp_refused": 1},
        timeout_count=0,
        mdns_status="available",
        observations=(first,),
    )

    for operation in ("progress", "terminal"):
        before = _lan_state_bytes(state)
        with pytest.raises(ValueError, match="endpoint"):
            if operation == "progress":
                ledger.record_scan_progress(
                    progressed.scan_id,
                    owner_principal="owner:test",
                    expected_revision=progressed.revision,
                    planned_count=1,
                    admitted_count=1,
                    completed_count=1,
                    persisted_observation_count=1,
                    error_category_counts={"tcp_refused": 1},
                    timeout_count=0,
                    mdns_status="available",
                    observations=(conflict,),
                )
            else:
                ledger.commit_scan_terminal(
                    progressed.scan_id,
                    owner_principal="owner:test",
                    expected_revision=progressed.revision,
                    status="completed",
                    terminal_reason="scan_complete",
                    cancel_reason=None,
                    observations=(conflict,),
                    mdns_status="available",
                    planned_count=1,
                    admitted_count=1,
                    completed_count=1,
                    error_category_counts={"tcp_refused": 1},
                    timeout_count=0,
                    evidence_complete=True,
                    unknown_inflight_count=0,
                )
        assert _lan_state_bytes(state) == before


def test_v2_terminal_receipt_has_exact_schema_bindings_counts_and_injected_times(
    state: AgentStateStore,
) -> None:
    current_time = [datetime(2026, 8, 1, 12, 0, tzinfo=UTC)]
    ledger = LanDiscoveryLedger(state, utc_clock=lambda: current_time[0])
    scope = PrivateScanScope.from_request(
        NetworkInterface.from_addresses(
            os_identity="darwin:en-receipt",
            display_name="Receipt fixture",
            addresses=("192.168.76.1/30",),
        ),
        "192.168.76.0/30",
    )
    observation = _task4_observation(scope, "192.168.76.2", 11434)
    draft = ledger.create_scan(
        scan_id="lan_exact_receipt",
        owner_principal="owner:test",
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits=_limits(),
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    current_time[0] = datetime(2026, 8, 1, 12, 1, tzinfo=UTC)
    preview_event = {
        **_preview_event(),
        "interface_id": scope.interface.interface_id,
        "network": scope.network,
    }
    running = ledger.claim_scan_start(
        draft.scan_id,
        owner_principal="owner:test",
        expected_revision=draft.revision,
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event=preview_event,
    )
    current_time[0] = datetime(2026, 8, 1, 12, 2, tzinfo=UTC)
    terminal = ledger.commit_scan_terminal(
        running.scan_id,
        owner_principal="owner:test",
        expected_revision=running.revision,
        status="completed",
        terminal_reason="scan_complete",
        cancel_reason=None,
        observations=(observation,),
        mdns_status="available",
        planned_count=1,
        admitted_count=1,
        completed_count=1,
        error_category_counts={"tcp_refused": 1},
        timeout_count=0,
        evidence_complete=True,
        unknown_inflight_count=0,
    )

    canonical_limits = json.loads(json.dumps(_limits(), sort_keys=True, separators=(",", ":")))
    limits_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                canonical_limits,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )
    membership_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {
                    "schema": "kestrel.lan.observation-membership.v1",
                    "count": 1,
                    "members": [
                        {
                            "endpoint_id": observation.endpoint_id,
                            "observation_digest": observation.public_payload["observation_digest"],
                        }
                    ],
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )
    expected = {
        "schema": "kestrel.lan.scan-receipt.v2",
        "version": 2,
        "scan_id": draft.scan_id,
        "status": "completed",
        "owner_principal": "owner:test",
        "confirmed_interface_id": scope.interface.interface_id,
        "network": scope.network,
        "limits": canonical_limits,
        "limits_digest": limits_digest,
        "preview_digest": PREVIEW_DIGEST,
        "started_at": "2026-08-01T12:01:00+00:00",
        "finished_at": "2026-08-01T12:02:00+00:00",
        "cancel_reason": None,
        "terminal_reason": "scan_complete",
        "mdns_status": "available",
        "planned_count": 1,
        "admitted_count": 1,
        "completed_count": 1,
        "persisted_observation_count": 1,
        "error_count": 1,
        "timeout_count": 0,
        "error_category_counts": {"tcp_refused": 1},
        "observation_count": 1,
        "observation_membership_digest": membership_digest,
        "evidence_complete": True,
        "unknown_inflight_count": 0,
    }
    assert terminal.terminal_receipt == expected
    assert set(terminal.terminal_receipt) == set(expected)
    assert terminal.candidate_count == 1
    assert terminal.error_count == 1
    assert terminal.timeout_count == 0
    assert terminal.terminal_receipt_digest == (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                expected,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize("operation", ("progress", "terminal"))
def test_shared_deadline_expiry_during_persistence_rolls_back_every_write(
    state: AgentStateStore,
    operation: str,
) -> None:
    ledger = LanDiscoveryLedger(state)
    running = _running_scan(ledger, scan_id=f"lan_deadline_{operation}")
    samples = iter((100.0, 102.0))
    calls: list[float] = []

    def clock() -> float:
        value = next(samples)
        calls.append(value)
        return value

    before = _lan_state_bytes(state)
    with pytest.raises(TimeoutError, match="deadline"):
        if operation == "progress":
            ledger.record_scan_progress(
                running.scan_id,
                owner_principal="owner:test",
                expected_revision=running.revision,
                planned_count=1,
                admitted_count=1,
                completed_count=1,
                persisted_observation_count=1,
                error_category_counts={},
                timeout_count=0,
                mdns_status="available",
                observations=(_observation(),),
                absolute_deadline=101.0,
                monotonic_clock=clock,
            )
        else:
            ledger.commit_scan_terminal(
                running.scan_id,
                owner_principal="owner:test",
                expected_revision=running.revision,
                status="completed",
                terminal_reason="scan_complete",
                cancel_reason=None,
                observations=(_observation(),),
                mdns_status="available",
                planned_count=1,
                admitted_count=1,
                completed_count=1,
                error_category_counts={},
                timeout_count=0,
                evidence_complete=True,
                unknown_inflight_count=0,
                absolute_deadline=101.0,
                monotonic_clock=clock,
            )
    assert calls == [100.0, 102.0]
    assert _lan_state_bytes(state) == before


@pytest.mark.parametrize("operation", ("progress", "terminal"))
def test_shared_deadline_crossing_in_precommit_hook_rolls_back_every_write(
    state: AgentStateStore,
    operation: str,
) -> None:
    class BoundaryClock:
        def __init__(self) -> None:
            self.value = 100.0
            self.calls: list[float] = []

        def __call__(self) -> float:
            self.calls.append(self.value)
            return self.value

    clock = BoundaryClock()
    crossed = False
    target_operation = "record_scan_progress" if operation == "progress" else "commit_scan_terminal"

    def cross_deadline(selected_operation: str) -> None:
        nonlocal crossed
        if selected_operation == target_operation and not crossed:
            crossed = True
            clock.value = 102.0

    ledger = LanDiscoveryLedger(state, precommit_hook=cross_deadline)
    running = _running_scan(ledger, scan_id=f"lan_precommit_deadline_{operation}")
    before = _lan_state_bytes(state)

    with pytest.raises(TimeoutError, match="deadline"):
        if operation == "progress":
            ledger.record_scan_progress(
                running.scan_id,
                owner_principal="owner:test",
                expected_revision=running.revision,
                planned_count=1,
                admitted_count=1,
                completed_count=1,
                persisted_observation_count=1,
                error_category_counts={},
                timeout_count=0,
                mdns_status="available",
                observations=(_observation(),),
                absolute_deadline=101.0,
                monotonic_clock=clock,
            )
        else:
            ledger.commit_scan_terminal(
                running.scan_id,
                owner_principal="owner:test",
                expected_revision=running.revision,
                status="completed",
                terminal_reason="scan_complete",
                cancel_reason=None,
                observations=(_observation(),),
                mdns_status="available",
                planned_count=1,
                admitted_count=1,
                completed_count=1,
                error_category_counts={},
                timeout_count=0,
                evidence_complete=True,
                unknown_inflight_count=0,
                absolute_deadline=101.0,
                monotonic_clock=clock,
            )

    assert crossed is True
    assert clock.calls == [100.0, 100.0, 102.0]
    assert _lan_state_bytes(state) == before


def test_terminal_receipt_uses_bounded_aggregate_membership_for_1024_observations(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    scope = PrivateScanScope.from_request(
        NetworkInterface.from_addresses(
            os_identity="darwin:en1024",
            display_name="IPv6 aggregate fixture",
            addresses=("fd00::1/120",),
        ),
        "fd00::/120",
    )
    draft = lan_ledger.create_scan(
        scan_id="lan_aggregate_1024",
        owner_principal="owner:test",
        confirmed_interface_id=scope.interface.interface_id,
        network=scope.network,
        limits=_limits(),
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    running = lan_ledger.claim_scan_start(
        draft.scan_id,
        owner_principal="owner:test",
        expected_revision=draft.revision,
        preview_digest=PREVIEW_DIGEST,
        authorized_preview_digest=PREVIEW_DIGEST,
        preview_event={
            **_preview_event(),
            "interface_id": scope.interface.interface_id,
            "network": scope.network,
            "active_host_count": 0,
            "passive_or_manual_only": True,
        },
    )
    observations = tuple(
        _task4_observation(scope, f"fd00::{address:x}", port)
        for address in range(256)
        for port in (1234, 8000, 8080, 11434)
    )

    before = _lan_state_bytes(lan_ledger.state)
    with pytest.raises(ValueError, match="fixed ceiling"):
        lan_ledger.commit_scan_terminal(
            running.scan_id,
            owner_principal="owner:test",
            expected_revision=running.revision,
            status="completed",
            terminal_reason="scan_complete",
            cancel_reason=None,
            observations=(*observations, observations[0]),
            mdns_status="available",
            planned_count=1025,
            admitted_count=1025,
            completed_count=1025,
            error_category_counts={"tcp_refused": 1025},
            timeout_count=0,
            evidence_complete=True,
            unknown_inflight_count=0,
        )
    assert _lan_state_bytes(lan_ledger.state) == before

    terminal = lan_ledger.commit_scan_terminal(
        running.scan_id,
        owner_principal="owner:test",
        expected_revision=running.revision,
        status="completed",
        terminal_reason="scan_complete",
        cancel_reason=None,
        observations=observations,
        mdns_status="available",
        planned_count=1024,
        admitted_count=1024,
        completed_count=1024,
        error_category_counts={"tcp_refused": 1024},
        timeout_count=0,
        evidence_complete=True,
        unknown_inflight_count=0,
    )

    assert terminal.terminal_receipt is not None
    receipt_json = json.dumps(terminal.terminal_receipt, sort_keys=True, separators=(",", ":"))
    assert len(receipt_json.encode()) < 16_384
    assert terminal.terminal_receipt["observation_count"] == 1024
    assert terminal.terminal_receipt["persisted_observation_count"] == 1024
    assert terminal.candidate_count == 1024
    assert terminal.terminal_receipt["observation_membership_digest"].startswith("sha256:")
    assert "observations" not in terminal.terminal_receipt
    ordered_members = sorted(
        (
            {
                "endpoint_id": observation.endpoint_id,
                "observation_digest": observation.public_payload["observation_digest"],
            }
            for observation in observations
        ),
        key=lambda item: item["endpoint_id"],
    )
    membership_preimage = {
        "schema": "kestrel.lan.observation-membership.v1",
        "count": 1024,
        "members": ordered_members,
    }
    expected_membership = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                membership_preimage,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )
    assert terminal.terminal_receipt["observation_membership_digest"] == expected_membership


def test_membership_digest_changes_when_one_durable_member_changes(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    from nested_memvid_agent.routing.lan_serialization import (
        observation_membership_digest,
    )

    scope = PrivateScanScope.from_request(
        NetworkInterface.from_addresses(
            os_identity="darwin:en-membership",
            display_name="Membership fixture",
            addresses=("192.168.74.1/30",),
        ),
        "192.168.74.0/30",
    )
    members = (
        _task4_observation(scope, "192.168.74.1", 8000),
        _task4_observation(scope, "192.168.74.2", 8080),
    )
    ordered_members = sorted(
        (
            {
                "endpoint_id": member.endpoint_id,
                "observation_digest": member.public_payload["observation_digest"],
            }
            for member in members
        ),
        key=lambda item: item["endpoint_id"],
    )
    expected = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                {
                    "schema": "kestrel.lan.observation-membership.v1",
                    "count": 2,
                    "members": ordered_members,
                },
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode()
        ).hexdigest()
    )
    assert observation_membership_digest(members) == expected
    assert observation_membership_digest(tuple(reversed(members))) == expected

    terminals = []
    for scan_id, observations in (
        ("lan_membership_a", members),
        ("lan_membership_b", tuple(reversed(members))),
    ):
        draft = lan_ledger.create_scan(
            scan_id=scan_id,
            owner_principal="owner:test",
            confirmed_interface_id=scope.interface.interface_id,
            network=scope.network,
            limits=_limits(),
            preview_digest=PREVIEW_DIGEST,
            expected_revision=0,
        )
        running = lan_ledger.claim_scan_start(
            draft.scan_id,
            owner_principal="owner:test",
            expected_revision=draft.revision,
            preview_digest=PREVIEW_DIGEST,
            authorized_preview_digest=PREVIEW_DIGEST,
            preview_event={
                **_preview_event(),
                "interface_id": scope.interface.interface_id,
                "network": scope.network,
            },
        )
        terminal = lan_ledger.commit_scan_terminal(
            running.scan_id,
            owner_principal="owner:test",
            expected_revision=running.revision,
            status="completed",
            terminal_reason="scan_complete",
            cancel_reason=None,
            observations=observations,
            mdns_status="available",
            planned_count=2,
            admitted_count=2,
            completed_count=2,
            error_category_counts={"tcp_refused": 2},
            timeout_count=0,
            evidence_complete=True,
            unknown_inflight_count=0,
        )
        assert terminal.terminal_receipt is not None
        terminals.append(terminal)

    assert {
        terminal.terminal_receipt["observation_membership_digest"]
        for terminal in terminals
        if terminal.terminal_receipt is not None
    } == {expected}

    changed_members = (members[0], _task4_observation(scope, "192.168.74.2", 11434))
    assert observation_membership_digest(changed_members) != expected


def test_interrupted_recovery_is_owner_filtered_and_never_invents_inflight_counts(
    lan_ledger: LanDiscoveryLedger,
    state: AgentStateStore,
) -> None:
    owned_running = _running_scan(lan_ledger, scan_id="lan_owned_running")
    owned_cancelling_draft = _create_scan(lan_ledger, scan_id="lan_owned_cancelling")
    owned_cancelling = lan_ledger.transition_scan(
        owned_cancelling_draft.scan_id,
        "running",
        expected_revision=owned_cancelling_draft.revision,
    )
    owned_cancelling = lan_ledger.transition_scan(
        owned_cancelling.scan_id,
        "cancelling",
        expected_revision=owned_cancelling.revision,
        cancel_reason="owner_cancelled",
    )
    owned_draft = _create_scan(lan_ledger, scan_id="lan_owned_draft")
    foreign = lan_ledger.create_scan(
        scan_id="lan_foreign_running",
        owner_principal="owner:foreign",
        confirmed_interface_id=INTERFACE_ID,
        network="192.168.10.0/30",
        limits=_limits(),
        preview_digest=PREVIEW_DIGEST,
        expected_revision=0,
    )
    foreign = lan_ledger.transition_scan(
        foreign.scan_id,
        "running",
        expected_revision=foreign.revision,
    )
    with state._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER test_interrupt_event_precedes_terminal_status
            BEFORE UPDATE OF status ON routing_lan_scans
            WHEN NEW.scan_id IN ('lan_owned_running', 'lan_owned_cancelling')
             AND NEW.status = 'interrupted'
             AND NOT EXISTS (
                SELECT 1 FROM routing_lan_scan_events
                WHERE scan_id = NEW.scan_id AND event_type = 'scan_interrupted'
             )
            BEGIN
                SELECT RAISE(ABORT, 'interrupt_event_missing');
            END
            """
        )

    interrupted = lan_ledger.interrupt_active_scans(owner_principal="owner:test")

    assert [item.scan_id for item in interrupted] == [
        owned_cancelling.scan_id,
        owned_running.scan_id,
    ]
    for item in interrupted:
        assert item.status == "interrupted"
        assert item.terminal_receipt is not None
        assert item.terminal_receipt["evidence_complete"] is False
        assert item.terminal_receipt["unknown_inflight_count"] is None
    assert lan_ledger.get_scan(owned_draft.scan_id) == owned_draft
    assert lan_ledger.get_scan(foreign.scan_id) == foreign
    assert Counter(
        event.event_type for item in interrupted for event in lan_ledger.list_events(item.scan_id)
    ) == Counter({"scan_interrupted": 2})


def test_interrupted_recovery_projects_durable_progress_as_lower_bounds(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    running = _running_scan(lan_ledger, scan_id="lan_progress_recovery")
    observations = (
        replace(
            _observation(
                endpoint_id="sha256:" + "8" * 64,
                address="192.168.10.1",
            ),
            error_category="tcp_refused",
        ),
        _observation(
            endpoint_id="sha256:" + "9" * 64,
            address="192.168.10.2",
        ),
    )
    progressed = lan_ledger.record_scan_progress(
        running.scan_id,
        owner_principal="owner:test",
        expected_revision=running.revision,
        planned_count=8,
        admitted_count=3,
        completed_count=2,
        persisted_observation_count=2,
        error_category_counts={"tcp_refused": 1},
        timeout_count=0,
        mdns_status="available",
        observations=observations,
    )

    interrupted = lan_ledger.interrupt_active_scans(owner_principal="owner:test")

    assert len(interrupted) == 1
    receipt = interrupted[0].terminal_receipt
    assert receipt is not None
    assert interrupted[0].revision == progressed.revision + 1
    assert receipt["planned_count"] == 8
    assert receipt["admitted_count"] == 3
    assert receipt["completed_count"] == 2
    assert receipt["persisted_observation_count"] == 2
    assert receipt["error_category_counts"] == {"tcp_refused": 1}
    assert receipt["evidence_complete"] is False
    assert receipt["unknown_inflight_count"] is None


def test_interrupted_recovery_without_progress_derives_known_durable_error_counts(
    lan_ledger: LanDiscoveryLedger,
) -> None:
    running = _running_scan(lan_ledger, scan_id="lan_legacy_error_recovery")
    first = lan_ledger.append_observation(
        running.scan_id,
        replace(
            _observation(
                endpoint_id="sha256:" + "a" * 64,
                address="192.168.10.1",
            ),
            error_category="tcp_refused",
        ),
        expected_revision=running.revision,
    )
    current = lan_ledger.get_scan(first.scan_id)
    assert current is not None
    lan_ledger.append_observation(
        current.scan_id,
        replace(
            _observation(
                endpoint_id="sha256:" + "b" * 64,
                address="192.168.10.2",
            ),
            error_category="http_timeout",
        ),
        expected_revision=current.revision,
    )

    interrupted = lan_ledger.interrupt_active_scans(owner_principal="owner:test")

    assert len(interrupted) == 1
    terminal = interrupted[0]
    receipt = terminal.terminal_receipt
    assert receipt is not None
    assert terminal.error_count == 2
    assert terminal.timeout_count == 1
    assert receipt["planned_count"] == 2
    assert receipt["admitted_count"] == 2
    assert receipt["completed_count"] == 2
    assert receipt["persisted_observation_count"] == 2
    assert receipt["error_category_counts"] == {
        "http_timeout": 1,
        "tcp_refused": 1,
    }
    assert receipt["timeout_count"] == 1
    assert receipt["evidence_complete"] is False
    assert receipt["unknown_inflight_count"] is None


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
