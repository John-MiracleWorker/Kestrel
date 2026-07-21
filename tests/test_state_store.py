from __future__ import annotations

import multiprocessing
import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import nested_memvid_agent.state_store as state_store_module
from nested_memvid_agent.state_store import (
    SCHEMA_VERSION,
    AgentStateStore,
    ApprovalConflictError,
    StateCapacityError,
)


def _initialize_state_store_in_child(
    path: str,
    barrier: Any,
    results: Any,
) -> None:
    try:
        barrier.wait(timeout=30)
        store = AgentStateStore(Path(path))
        with sqlite3.connect(path, timeout=5.0) as conn:
            integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
            version = int(
                conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0]
            )
            journal_mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0])
        results.put(
            {
                "ok": True,
                "version": version,
                "journal_mode": journal_mode,
                "health": store.health_snapshot(),
                "integrity": integrity,
            }
        )
    except BaseException as exc:
        results.put({"ok": False, "error_type": type(exc).__name__, "error": str(exc)})
        raise


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_state_store_creates_owner_only_directory_and_database(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private-state" / "agent.db"

    AgentStateStore(path)

    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    lock_path = path.with_name(f".{path.name}.kestrel-state-init.lock")
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not enforced on Windows")
def test_state_store_tightens_existing_database_and_wal_sidecars(
    tmp_path: Path,
) -> None:
    path = tmp_path / "existing-state" / "agent.db"
    path.parent.mkdir(mode=0o755)
    os.chmod(path.parent, 0o755)
    AgentStateStore(path)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    os.chmod(path, 0o644)

    keeper = sqlite3.connect(path)
    try:
        keeper.execute("CREATE TABLE IF NOT EXISTS permission_probe (id INTEGER)")
        keeper.execute("INSERT INTO permission_probe VALUES (1)")
        keeper.commit()
        sidecars = (Path(f"{path}-wal"), Path(f"{path}-shm"))
        assert all(sidecar.exists() for sidecar in sidecars)
        for sidecar in sidecars:
            os.chmod(sidecar, 0o644)

        AgentStateStore(path)

        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o755
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert all(stat.S_IMODE(sidecar.stat().st_mode) == 0o600 for sidecar in sidecars)
    finally:
        keeper.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink modes differ on Windows")
def test_state_store_rejects_symlinked_directory_without_chmod_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside-directory"
    target.mkdir(mode=0o755)
    os.chmod(target, 0o755)
    linked_directory = tmp_path / "linked-state"
    linked_directory.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="state directory must not be a symbolic link"):
        AgentStateStore(linked_directory / "agent.db")

    assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert not (target / "agent.db").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink modes differ on Windows")
def test_state_store_rejects_symlinked_database_without_chmod_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside.db"
    sqlite3.connect(target).close()
    os.chmod(target, 0o644)
    directory = tmp_path / "state"
    directory.mkdir()
    path = directory / "agent.db"
    path.symlink_to(target)

    with pytest.raises(ValueError, match="SQLite state files must not be symbolic links"):
        AgentStateStore(path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


@pytest.mark.skipif(os.name == "nt", reason="POSIX hard-link modes differ on Windows")
def test_state_store_rejects_hard_linked_database_without_chmod_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "outside-hard-link.db"
    sqlite3.connect(target).close()
    os.chmod(target, 0o644)
    directory = tmp_path / "hard-linked-state"
    directory.mkdir()
    path = directory / "agent.db"
    os.link(target, path)

    with pytest.raises(ValueError, match="SQLite state files must not be hard-linked"):
        AgentStateStore(path)

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_concurrent_fresh_database_initialization_is_serialized(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-state" / "state.db"
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: AgentStateStore(path), range(16)))

    assert {store.schema_version() for store in stores} == {SCHEMA_VERSION}
    assert all(store.health_snapshot()["ok"] is True for store in stores)
    if os.name != "nt":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_multiprocess_fresh_database_initialization_is_serialized(tmp_path: Path) -> None:
    path = tmp_path / "multiprocess-state" / "state.db"
    process_count = 16
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(process_count)
    results = context.Queue()
    processes = [
        context.Process(
            target=_initialize_state_store_in_child,
            args=(str(path), barrier, results),
        )
        for _ in range(process_count)
    ]

    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=45)
        alive = [process.pid for process in processes if process.is_alive()]
        assert not alive, f"state initialization workers did not exit: {alive}"
        assert [process.exitcode for process in processes] == [0] * process_count
        payloads = [results.get(timeout=5) for _ in range(process_count)]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        results.close()
        results.join_thread()

    assert all(payload["ok"] is True for payload in payloads)
    assert {payload["version"] for payload in payloads} == {SCHEMA_VERSION}
    assert {payload["journal_mode"] for payload in payloads} == {"wal"}
    assert {payload["integrity"] for payload in payloads} == {"ok"}
    assert all(payload["health"]["ok"] is True for payload in payloads)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0] == 19


def test_schema_migration_rolls_back_all_ddl_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "atomic-migration" / "state.db"
    real_apply_schema_v1 = state_store_module._apply_schema_v1

    def fail_after_schema_v1(conn: sqlite3.Connection) -> None:
        real_apply_schema_v1(conn)
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(state_store_module, "_apply_schema_v1", fail_after_schema_v1)

    with pytest.raises(RuntimeError, match="injected migration failure"):
        AgentStateStore(path)

    with sqlite3.connect(path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert "schema_version" not in tables
    assert "runs" not in tables

    monkeypatch.setattr(state_store_module, "_apply_schema_v1", real_apply_schema_v1)
    recovered = AgentStateStore(path)
    assert recovered.schema_version() == SCHEMA_VERSION
    assert recovered.health_snapshot()["integrity"] == "ok"


def test_current_schema_reopen_does_not_rewrite_schema_metadata(tmp_path: Path) -> None:
    path = tmp_path / "stable-schema" / "state.db"
    AgentStateStore(path)
    with sqlite3.connect(path) as conn:
        before = conn.execute("SELECT updated_at FROM schema_version WHERE id = 1").fetchone()[0]

    AgentStateStore(path)

    with sqlite3.connect(path) as conn:
        after = conn.execute("SELECT updated_at FROM schema_version WHERE id = 1").fetchone()[0]
    assert after == before


def test_regular_state_connections_do_not_reopen_sqlite_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "no-hot-path-sidecar-hardening" / "state.db"
    store = AgentStateStore(path)

    def fail_if_called(_: Path) -> None:
        raise AssertionError("SQLite sidecars must not be reopened on the connection hot path")

    monkeypatch.setattr(
        state_store_module,
        "_harden_private_sqlite_files",
        fail_if_called,
    )

    assert store.schema_version() == SCHEMA_VERSION
    assert store.health_snapshot()["integrity"] == "ok"


@pytest.mark.skipif(os.name == "nt", reason="POSIX link semantics differ on Windows")
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_state_store_rejects_aliased_initialization_lock(
    tmp_path: Path,
    link_kind: str,
) -> None:
    directory = tmp_path / "state"
    directory.mkdir()
    path = directory / "agent.db"
    lock_path = directory / ".agent.db.kestrel-state-init.lock"
    outside = tmp_path / "outside.lock"
    outside.write_text("do-not-touch", encoding="utf-8")
    os.chmod(outside, 0o644)
    if link_kind == "symlink":
        lock_path.symlink_to(outside)
    else:
        lock_path.hardlink_to(outside)

    with pytest.raises(ValueError, match="Sensitive artifacts must not be"):
        AgentStateStore(path)

    assert outside.read_text(encoding="utf-8") == "do-not-touch"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o644
    assert not path.exists()


def test_initialization_closes_every_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "state.db"
    real_connect = sqlite3.connect
    connections: list[sqlite3.Connection] = []

    def tracking_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # type: ignore[arg-type]
        connections.append(connection)
        return connection

    monkeypatch.setattr(
        "nested_memvid_agent.state_store.sqlite3.connect",
        tracking_connect,
    )

    AgentStateStore(path)

    assert len(connections) >= 2
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
            connection.execute("SELECT 1")


def test_state_store_rejects_unsupported_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE schema_version (id INTEGER PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schema_version (id, version, updated_at) VALUES (1, ?, 'future')",
            (SCHEMA_VERSION + 1,),
        )

    with pytest.raises(RuntimeError, match="newer than supported"):
        AgentStateStore(path)


def test_schema_17_migrates_existing_runs_to_primary_provenance_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v15.db"
    created_at = datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                session_id TEXT NOT NULL,
                workspace TEXT NOT NULL,
                provider TEXT NOT NULL DEFAULT 'mock',
                model TEXT NOT NULL,
                assistant_message TEXT NOT NULL DEFAULT '',
                context_chars INTEGER NOT NULL DEFAULT 0,
                tool_count INTEGER NOT NULL DEFAULT 0,
                stop_reason TEXT NOT NULL DEFAULT '',
                error TEXT,
                lease_owner TEXT,
                lease_generation INTEGER NOT NULL DEFAULT 0,
                lease_expires_at TEXT,
                heartbeat_at TEXT,
                interrupted_at TEXT,
                recovery_reason TEXT NOT NULL DEFAULT '',
                config_revision TEXT,
                config_snapshot_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO schema_version VALUES (1, 15, ?)", (created_at,))
        conn.execute(
            """
            INSERT INTO runs (
                run_id, status, message, session_id, workspace, provider, model,
                created_at, updated_at
            ) VALUES ('run_legacy', 'completed', 'legacy', 'session_legacy', ?,
                'mock', 'mock', ?, ?)
            """,
            (str(tmp_path), created_at, created_at),
        )

    state = AgentStateStore(path)
    run = state.get_run("run_legacy")

    assert state.schema_version() == SCHEMA_VERSION == 19
    assert run.turn_source is None
    assert run.turn_origin == "primary_user"
    assert run.transcript_scope == "primary"


def test_schema_17_to_18_adds_approval_execution_claims_and_preserves_cas(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v17-approvals.db"
    legacy = AgentStateStore(path)
    legacy.create_run(
        run_id="run_v17_approval",
        message="legacy approval",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    legacy.transition_run("run_v17_approval", "running")
    legacy.transition_run("run_v17_approval", "completed")
    legacy.create_approval(
        approval_id="approval_v17",
        run_id="run_v17_approval",
        tool_call_id="tool_v17",
        tool_name="test.side_effect",
        arguments={"value": "once"},
        risk="high",
    )
    legacy.decide_approval(
        "approval_v17",
        status="approved",
        decision={"approved": True},
    )
    claim_columns = (
        "execution_claim_owner",
        "execution_claim_id",
        "execution_claim_started_at",
        "execution_claim_expires_at",
        "execution_claim_task_id",
        "execution_claim_subagent_id",
    )
    with sqlite3.connect(path) as connection:
        for column in claim_columns:
            connection.execute(f"ALTER TABLE approval_requests DROP COLUMN {column}")
        connection.execute("UPDATE schema_version SET version = 17 WHERE id = 1")

    migrated = AgentStateStore(path)
    with sqlite3.connect(path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(approval_requests)").fetchall()
        }
    approval = migrated.get_approval("approval_v17", expire=False)

    assert migrated.schema_version() == SCHEMA_VERSION == 19
    assert set(claim_columns) <= columns
    assert approval["status"] == "approved"
    assert approval["result"] is None
    claimed, claim_applied = migrated.claim_approval_execution(
        "approval_v17",
        run_id="run_v17_approval",
        tool_call_id="tool_v17",
        owner="migration-owner",
        claim_id="migration-claim",
        ttl_seconds=30.0,
    )
    assert claim_applied is True
    finalized, result_applied = migrated.record_claimed_approval_result(
        "approval_v17",
        owner="migration-owner",
        claim_id="migration-claim",
        result={"success": True},
    )
    assert claimed["execution_claim_id"] == "migration-claim"
    assert result_applied is True
    assert finalized["result"] == {"success": True}


def test_approval_expiry_and_principal_are_enforced_atomically(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    expired_at = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    state.create_approval(
        approval_id="approval_expired",
        run_id="run_expired",
        tool_call_id="call_expired",
        tool_name="shell.run",
        arguments={"command": ["echo", "late"]},
        risk="high",
        expires_at=expired_at,
        principal="owner",
    )

    expired = state.get_approval("approval_expired")
    assert expired["status"] == "expired"
    replay = state.decide_approval(
        "approval_expired",
        status="approved",
        decision={"approved": True},
        principal="owner",
    )
    assert replay["status"] == "expired"

    future = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    state.create_approval(
        approval_id="approval_principal",
        run_id="run_principal",
        tool_call_id="call_principal",
        tool_name="shell.run",
        arguments={"command": ["echo", "owner"]},
        risk="high",
        expires_at=future,
        principal="owner",
    )
    with pytest.raises(ValueError, match="principal"):
        state.decide_approval_once(
            "approval_principal",
            status="approved",
            decision={"approved": True},
            principal="not-owner",
        )
    assert state.get_approval("approval_principal")["status"] == "pending"


def test_concurrent_exact_approval_requests_reuse_one_pending_record(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()

    def request(index: int) -> tuple[dict[str, object], bool]:
        return state.create_approval_once(
            approval_id=f"approval_{index}",
            run_id="run_single_pending",
            tool_call_id="call_single_pending",
            tool_name="shell.run",
            arguments={"command": ["echo", "once"]},
            risk="high",
            expires_at=expires_at,
            principal="owner",
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        requested = list(pool.map(request, range(16)))

    approvals = [approval for approval, _created in requested]
    assert sum(1 for _approval, created in requested if created) == 1
    assert {approval["approval_id"] for approval in approvals} == {approvals[0]["approval_id"]}
    assert len(state.list_approvals(status="pending")) == 1


def test_run_cannot_hold_two_different_pending_approvals(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    expires_at = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
    first = state.create_approval(
        approval_id="approval_first",
        run_id="run_single_pending",
        tool_call_id="call_first",
        tool_name="shell.run",
        arguments={"command": ["echo", "first"]},
        risk="high",
        expires_at=expires_at,
    )

    with pytest.raises(ApprovalConflictError) as raised:
        state.create_approval(
            approval_id="approval_second",
            run_id="run_single_pending",
            tool_call_id="call_second",
            tool_name="file.write",
            arguments={"path": "second.txt", "content": "second"},
            risk="high",
            expires_at=expires_at,
        )

    assert raised.value.approval["approval_id"] == first["approval_id"]
    assert [item["approval_id"] for item in state.list_approvals(status="pending")] == [
        first["approval_id"]
    ]


def test_schema_14_migration_expires_legacy_unbounded_pending_approvals(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-v13.db"
    created_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE schema_version (
                id INTEGER PRIMARY KEY,
                version INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE approval_requests (
                approval_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                tool_call_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                risk TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_json TEXT,
                result_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO schema_version VALUES (1, 13, ?)",
            (created_at,),
        )
        conn.execute(
            """
            INSERT INTO approval_requests VALUES (
                'approval_legacy', 'run_legacy', 'call_legacy', 'shell.run',
                '{}', 'high', 'pending', NULL, NULL, ?, ?
            )
            """,
            (created_at, created_at),
        )

    state = AgentStateStore(path)

    assert state.schema_version() == SCHEMA_VERSION
    approval = state.get_approval("approval_legacy")
    assert approval["principal"] == "owner"
    assert approval["expires_at"] == created_at
    assert approval["status"] == "expired"


def test_durable_run_admission_is_atomic_across_state_store_instances(tmp_path: Path) -> None:
    path = tmp_path / "state.db"

    def admit(index: int) -> str:
        try:
            AgentStateStore(path).create_run(
                run_id=f"run-{index}",
                message="bounded",
                session_id=f"session-{index}",
                workspace=str(tmp_path),
                provider="mock",
                model="mock-model",
                max_nonterminal_runs=1,
            )
        except StateCapacityError:
            return "rejected"
        return "admitted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(admit, range(2)))

    assert sorted(results) == ["admitted", "rejected"]


def test_state_store_rejects_unknown_update_columns(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_1",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )

    with pytest.raises(ValueError, match="Unknown runs column"):
        state.update_run("run_1", **{"status = 'completed' --": "completed"})


def test_state_store_rejects_unknown_subagent_update_columns(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_1",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    state.create_subagent_run(
        subagent_id="subagent_1",
        run_id="run_1",
        profile="research",
        goal="inspect the task",
    )

    with pytest.raises(ValueError, match="Unknown subagent_runs column"):
        state.update_subagent_run("subagent_1", **{"status = 'completed' --": "completed"})


def test_state_store_wal_allows_concurrent_reads_and_writes(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_1",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )

    def worker(index: int) -> None:
        for attempt in range(40):
            state.update_run("run_1", assistant_message=f"{index}:{attempt}")
            state.get_run("run_1")
            state.list_runs()

    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(worker, range(5)))

    assert state.get_run("run_1").assistant_message


def test_run_lease_fences_stale_owners_and_clears_on_completion(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_lease",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    started_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

    first = state.acquire_run_lease("run_lease", owner="worker-a", ttl_seconds=30, now=started_at)

    assert first is not None
    assert first.lease_owner == "worker-a"
    assert first.lease_generation == 1
    assert (
        state.acquire_run_lease(
            "run_lease",
            owner="worker-b",
            ttl_seconds=30,
            now=started_at + timedelta(seconds=1),
        )
        is None
    )
    running = state.transition_run(
        "run_lease",
        "running",
        lease_owner="worker-a",
        lease_generation=first.lease_generation,
        transition_at=started_at + timedelta(seconds=2),
    )
    assert running.status == "running"

    reclaimed = state.acquire_run_lease(
        "run_lease",
        owner="worker-b",
        ttl_seconds=30,
        now=started_at + timedelta(seconds=31),
    )

    assert reclaimed is not None
    assert reclaimed.lease_owner == "worker-b"
    assert reclaimed.lease_generation == 2
    stale_completion = state.transition_run(
        "run_lease",
        "completed",
        lease_owner="worker-a",
        lease_generation=first.lease_generation,
        transition_at=started_at + timedelta(seconds=32),
        stop_reason="stale_worker",
    )
    assert stale_completion.status == "running"
    assert stale_completion.lease_owner == "worker-b"

    completed = state.transition_run(
        "run_lease",
        "completed",
        lease_owner="worker-b",
        lease_generation=reclaimed.lease_generation,
        transition_at=started_at + timedelta(seconds=32),
        stop_reason="complete",
    )
    assert completed.status == "completed"
    assert completed.lease_owner is None
    assert completed.lease_expires_at is None


def test_startup_recovery_claim_can_fence_exact_fresh_dead_owner_snapshot(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_fresh_dead_owner",
        message="recover dead owner",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    leased = state.acquire_run_lease(
        "run_fresh_dead_owner",
        owner="manager_999999_dead",
        ttl_seconds=60.0,
    )
    assert leased is not None
    assert (
        state.claim_run_for_startup_recovery(
            leased.run_id,
            expected_status=leased.status,
            expected_lease_owner=leased.lease_owner,
            expected_lease_generation=leased.lease_generation,
            expected_lease_expires_at=leased.lease_expires_at,
            owner="manager_1_recovery",
            ttl_seconds=30.0,
        )
        is None
    )

    recovered = state.claim_run_for_startup_recovery(
        leased.run_id,
        expected_status=leased.status,
        expected_lease_owner=leased.lease_owner,
        expected_lease_generation=leased.lease_generation,
        expected_lease_expires_at=leased.lease_expires_at,
        owner="manager_1_recovery",
        ttl_seconds=30.0,
        allow_unexpired_observed_lease=True,
    )

    assert recovered is not None
    assert recovered.lease_owner == "manager_1_recovery"
    assert recovered.lease_generation == leased.lease_generation + 1


def test_matching_owner_is_fenced_after_lease_expiry_and_cannot_resurrect_it(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_expired",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    started_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    leased = state.acquire_run_lease("run_expired", owner="worker-a", ttl_seconds=5, now=started_at)
    assert leased is not None
    running = state.transition_run(
        "run_expired",
        "running",
        lease_owner="worker-a",
        lease_generation=leased.lease_generation,
        transition_at=started_at + timedelta(seconds=1),
    )
    assert running.status == "running"

    expired_completion = state.transition_run(
        "run_expired",
        "completed",
        lease_owner="worker-a",
        lease_generation=leased.lease_generation,
        transition_at=started_at + timedelta(seconds=6),
    )
    assert expired_completion.status == "running"
    assert (
        state.renew_run_lease(
            "run_expired",
            owner="worker-a",
            generation=leased.lease_generation,
            ttl_seconds=5,
            now=started_at + timedelta(seconds=6),
        )
        is None
    )

    replacement = state.acquire_run_lease(
        "run_expired",
        owner="worker-b",
        ttl_seconds=5,
        now=started_at + timedelta(seconds=6),
    )
    assert replacement is not None
    assert replacement.lease_generation == leased.lease_generation + 1


def test_run_lease_renewal_requires_the_current_owner_and_generation(tmp_path: Path) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    state.create_run(
        run_id="run_heartbeat",
        message="hello",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )
    started_at = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    leased = state.acquire_run_lease(
        "run_heartbeat", owner="worker-a", ttl_seconds=30, now=started_at
    )
    assert leased is not None

    assert (
        state.renew_run_lease(
            "run_heartbeat",
            owner="worker-b",
            generation=leased.lease_generation,
            ttl_seconds=30,
            now=started_at + timedelta(seconds=5),
        )
        is None
    )
    renewed = state.renew_run_lease(
        "run_heartbeat",
        owner="worker-a",
        generation=leased.lease_generation,
        ttl_seconds=30,
        now=started_at + timedelta(seconds=5),
    )

    assert renewed is not None
    assert renewed.heartbeat_at == (started_at + timedelta(seconds=5)).isoformat()
    assert renewed.lease_expires_at == (started_at + timedelta(seconds=35)).isoformat()


def test_run_lease_schema_migration_preserves_existing_runs(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    state = AgentStateStore(path)
    state.create_run(
        run_id="run_before_upgrade",
        message="preserve me",
        session_id="session",
        workspace=str(tmp_path),
        provider="mock",
        model="mock",
    )

    reopened = AgentStateStore(path)
    run = reopened.get_run("run_before_upgrade")

    assert reopened.schema_version() >= 12
    assert run.message == "preserve me"
    assert run.lease_owner is None
    assert run.lease_generation == 0
