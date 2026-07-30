from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from nested_memvid_agent.desktop_recovery import DesktopRecoveryService
from nested_memvid_agent.state_store import SCHEMA_VERSION


@dataclass(frozen=True)
class _Decision:
    status: str


class _State:
    def __init__(
        self,
        *,
        approvals: tuple[dict[str, object], ...] = (),
        health: dict[str, object] | None = None,
    ) -> None:
        self.approvals = approvals
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

    def list_approvals(
        self,
        status: str | None = None,
        *,
        expire: bool = True,
    ) -> list[dict[str, object]]:
        self.calls.append(("list_approvals", (status, expire)))
        assert status == "pending"
        assert expire is False
        return [dict(item) for item in self.approvals]


class _Routing:
    def __init__(self, decisions: tuple[_Decision, ...] = ()) -> None:
        self.decisions = decisions
        self.calls = 0

    def list_unsettled_decisions(self, *, limit: int) -> list[_Decision]:
        self.calls += 1
        assert limit == 1_000
        return list(self.decisions)


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
        credential_readiness=lambda: _credential_readiness(
            credential_state
        ),
        memory_ready=lambda: memory_ready,
    )


def test_crash_recovery_never_replays_high_risk_or_ambiguous_attempts() -> None:
    state = _State(
        approvals=(
            {"approval_id": "a-high", "status": "pending", "risk": "high"},
            {
                "approval_id": "a-critical",
                "status": "pending",
                "risk": "critical",
            },
            {"approval_id": "a-low", "status": "pending", "risk": "low"},
        )
    )
    routing = _Routing(
        (
            _Decision(status="running"),
            _Decision(status="selected"),
        )
    )
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
        ("list_approvals", ("pending", False)),
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
        ("list_approvals", ("pending", False)),
        ("health_snapshot", None),
        ("list_approvals", ("pending", False)),
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

    assert preview["schema"] == (
        "kestrel.desktop.recovery-support-preview.v1"
    )
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
    approvals: tuple[dict[str, Any], ...] = tuple(
        {
            "approval_id": f"a-{index}",
            "status": "pending",
            "risk": "high",
        }
        for index in range(1_100)
    )
    decisions = tuple(_Decision(status="running") for _ in range(1_100))

    report = _service(
        state=_State(approvals=approvals),
        routing=_Routing(decisions),
    ).inspect()

    assert report.pending_high_risk_approvals == 1_000
    assert report.ambiguous_provider_attempts == 1_000
