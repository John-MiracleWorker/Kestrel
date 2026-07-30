from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Thread
from time import monotonic

from nested_memvid_agent.desktop_recovery import DesktopRecoveryService
from nested_memvid_agent.state_store import SCHEMA_VERSION, AgentStateStore


class _State:
    def __init__(
        self,
        *,
        pending_high_risk: int = 0,
        health: dict[str, object] | None = None,
    ) -> None:
        self.pending_high_risk = pending_high_risk
        self.health = health or {
            "ok": True,
            "integrity": "ok",
            "schema_version": SCHEMA_VERSION,
            "writable": True,
            "error_type": None,
        }
        self.calls: list[tuple[str, object]] = []

    def health_snapshot(self) -> dict[str, object]:
        self.calls.append(("health_snapshot", None))
        return dict(self.health)

    def count_pending_high_risk_approvals(self, *, limit: int) -> int:
        self.calls.append(("count_pending_high_risk_approvals", limit))
        assert limit == 1_000
        return min(limit, self.pending_high_risk)

    def list_approvals(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("recovery must not load raw approval rows")


class _Routing:
    def __init__(self, running: int = 0) -> None:
        self.running = running
        self.calls = 0

    def count_running_decisions(self, *, limit: int) -> int:
        self.calls += 1
        assert limit == 1_000
        return min(limit, self.running)

    def list_unsettled_decisions(self, *args: object, **kwargs: object) -> None:
        raise AssertionError("recovery must not load raw routing rows")


def _credential_readiness(
    state: str = "available",
) -> dict[str, object]:
    return {
        "schema": "kestrel.desktop_credential_readiness.v1",
        "state": state,
        "backend": "test-keyring" if state == "available" else None,
        "persistence": "persistent" if state == "available" else "none",
        "reason": "ready" if state == "available" else "backend_unverified",
        "remediation": "No recovery needed.",
    }


def _service(
    *,
    state: _State | None = None,
    routing: _Routing | None = None,
    credential_state: str = "available",
    memory_ready: bool = True,
) -> DesktopRecoveryService:
    return DesktopRecoveryService(
        state=state or _State(),
        routing=routing or _Routing(),
        credential_readiness=lambda: _credential_readiness(credential_state),
        memory_ready=lambda: memory_ready,
    )


def test_crash_recovery_never_replays_high_risk_or_ambiguous_attempts() -> None:
    state = _State(pending_high_risk=2)
    routing = _Routing(running=1)
    recovery_service = _service(state=state, routing=routing)

    report = recovery_service.inspect()

    assert report.can_auto_resume is False
    assert report.blockers == (
        "pending_high_risk_approval",
        "ambiguous_provider_attempt",
    )
    assert report.actions == (
        "inspect",
        "export_support_bundle",
        "retry_readiness",
    )
    assert report.pending_high_risk_approvals == 2
    assert report.ambiguous_provider_attempts == 1
    assert state.calls == [
        ("health_snapshot", None),
        ("count_pending_high_risk_approvals", 1_000),
    ]
    assert routing.calls == 1


def test_retry_readiness_is_a_read_only_reinspection() -> None:
    state = _State()
    routing = _Routing()
    service = _service(state=state, routing=routing)

    initial = service.inspect()
    retried = service.retry_readiness()

    assert retried == initial
    assert retried.can_auto_resume is True
    assert state.calls == [
        ("health_snapshot", None),
        ("count_pending_high_risk_approvals", 1_000),
        ("health_snapshot", None),
        ("count_pending_high_risk_approvals", 1_000),
    ]
    assert routing.calls == 2


def test_state_memory_and_credential_failures_use_stable_codes_only() -> None:
    state = _State(
        health={
            "ok": False,
            "integrity": "error",
            "schema_version": None,
            "writable": False,
            "error_type": "sentinel-secret-must-not-escape",
        }
    )
    report = _service(
        state=state,
        credential_state="unavailable",
        memory_ready=False,
    ).inspect()
    payload = report.to_public_payload()
    rendered = json.dumps(payload, sort_keys=True)

    assert report.can_auto_resume is False
    assert report.reasons == (
        "state_corrupt",
        "memvid_reopen_failed",
        "credential_backend_unavailable",
    )
    assert report.blockers == (
        "state_corrupt",
        "memvid_reopen_failed",
    )
    assert "sentinel-secret-must-not-escape" not in rendered
    assert set(payload) == {
        "schema",
        "can_auto_resume",
        "reasons",
        "blockers",
        "actions",
        "state",
        "memory",
        "approvals",
        "routing",
        "credential_storage",
    }


def test_incompatible_schema_is_not_reported_as_corruption() -> None:
    report = _service(
        state=_State(
            health={
                "ok": False,
                "integrity": "ok",
                "schema_version": SCHEMA_VERSION + 1,
                "writable": True,
                "error_type": None,
            }
        )
    ).inspect()

    assert report.reasons == ("state_incompatible",)
    assert report.blockers == ("state_incompatible",)
    assert report.can_auto_resume is False


def test_support_bundle_preview_is_bounded_metadata_without_paths_or_text() -> None:
    sentinel = "sk-proj-support-preview-secret"  # gitleaks:allow
    service = DesktopRecoveryService(
        state=_State(),
        routing=_Routing(),
        credential_readiness=lambda: {
            **_credential_readiness("unavailable"),
            "backend": f"/private/{sentinel}",
            "reason": sentinel,
            "remediation": sentinel,
        },
        memory_ready=lambda: True,
    )

    preview = service.support_bundle_preview()
    rendered = json.dumps(preview, sort_keys=True)

    assert preview["schema"] == ("kestrel.desktop.recovery-support-preview.v1")
    assert preview["redacted"] is True
    assert preview["entries"] == [
        "recovery.json",
        "state-summary.json",
        "routing-summary.json",
        "credential-storage.json",
    ]
    assert len(rendered.encode("utf-8")) <= 32 * 1024
    assert sentinel not in rendered
    assert "/private/" not in rendered


def test_counts_are_bounded_even_if_authority_returns_excess_rows() -> None:
    report = _service(
        state=_State(pending_high_risk=1_100),
        routing=_Routing(running=1_100),
    ).inspect()

    assert report.pending_high_risk_approvals == 1_000
    assert report.ambiguous_provider_attempts == 1_000


def test_pending_high_risk_count_is_bounded_and_never_decodes_arguments(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    rows = [
        (
            f"approval-{index}",
            f"run-{index}",
            f"call-{index}",
            "shell.run",
            "{invalid-and-intentionally-large:" + ("x" * 8_192),
            "critical" if index % 2 else "high",
            "pending",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
        )
        for index in range(1_025)
    ]
    with state._connect() as connection:
        connection.executemany(
            """
            INSERT INTO approval_requests (
                approval_id, run_id, tool_call_id, tool_name,
                arguments_json, risk, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    assert state.count_pending_high_risk_approvals(limit=1_000) == 1_000
    assert state.count_pending_high_risk_approvals(limit=7) == 7


def test_concurrent_inspection_fails_closed_without_starting_second_probe() -> None:
    entered = Event()
    release = Event()
    probe_count = 0

    def blocking_memory_probe() -> bool:
        nonlocal probe_count
        probe_count += 1
        entered.set()
        assert release.wait(timeout=2)
        return True

    service = DesktopRecoveryService(
        state=_State(),
        routing=_Routing(),
        credential_readiness=_credential_readiness,
        memory_ready=blocking_memory_probe,
    )
    completed: list[object] = []
    first = Thread(target=lambda: completed.append(service.inspect()))
    first.start()
    assert entered.wait(timeout=1)

    started = monotonic()
    second = service.inspect()
    elapsed = monotonic() - started
    release.set()
    first.join(timeout=1)

    assert elapsed < 0.25
    assert second.can_auto_resume is False
    assert second.blockers == ("recovery_inspection_unavailable",)
    assert probe_count == 1
    assert len(completed) == 1
