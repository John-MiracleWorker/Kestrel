from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nested_memvid_agent.state_store import (
    SCHEMA_VERSION,
    AgentStateStore,
    ApprovalConflictError,
    StateCapacityError,
)


def test_concurrent_fresh_database_initialization_is_serialized(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with ThreadPoolExecutor(max_workers=8) as pool:
        stores = list(pool.map(lambda _: AgentStateStore(path), range(16)))

    assert {store.schema_version() for store in stores} == {SCHEMA_VERSION}
    assert all(store.health_snapshot()["ok"] is True for store in stores)


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
    assert {approval["approval_id"] for approval in approvals} == {
        approvals[0]["approval_id"]
    }
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
    assert state.acquire_run_lease(
        "run_lease",
        owner="worker-b",
        ttl_seconds=30,
        now=started_at + timedelta(seconds=1),
    ) is None
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


def test_matching_owner_is_fenced_after_lease_expiry_and_cannot_resurrect_it(tmp_path: Path) -> None:
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
    assert state.renew_run_lease(
        "run_expired",
        owner="worker-a",
        generation=leased.lease_generation,
        ttl_seconds=5,
        now=started_at + timedelta(seconds=6),
    ) is None

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
    leased = state.acquire_run_lease("run_heartbeat", owner="worker-a", ttl_seconds=30, now=started_at)
    assert leased is not None

    assert state.renew_run_lease(
        "run_heartbeat",
        owner="worker-b",
        generation=leased.lease_generation,
        ttl_seconds=30,
        now=started_at + timedelta(seconds=5),
    ) is None
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
