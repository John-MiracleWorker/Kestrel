"""Adaptive Flock Task 3: owner-only control-plane receipt authentication.

Covers generation/storage/ownership of ``<state-directory>/.routing-integrity.key``,
HMAC-SHA256 receipt envelopes, wrong-key/wrong-owner rejection, ledger receipt
authentication, and the desktop recovery ``routing_integrity_key_missing_or_mismatched``
signal.  Key material must never appear in envelopes, logs, or API payloads.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from nested_memvid_agent.control_plane_integrity import (
    AUTHENTICATED_PAYLOAD_ALGORITHM,
    ROUTING_INTEGRITY_KEY_NAME,
    AuthenticatedPayload,
    ControlPlaneIntegrity,
    RoutingIntegrityError,
    routing_integrity_key_state,
)
from nested_memvid_agent.desktop_recovery import DesktopRecoveryService
from nested_memvid_agent.routing.qualification_ledger import QualificationLedger
from nested_memvid_agent.routing.qualification_models import (
    CorpusItem,
    CorpusManifest,
    MoneyMicros,
    QualificationScope,
    QualificationThresholds,
)
from nested_memvid_agent.routing.qualification_records import QualificationRunDraft
from nested_memvid_agent.state_store import SCHEMA_VERSION, AgentStateStore

POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")


# -- key generation, storage, ownership --------------------------------------


def test_signed_receipt_survives_restart_and_rejects_tampering(tmp_path: Path) -> None:
    signer = ControlPlaneIntegrity(tmp_path)
    envelope = signer.sign({"receipt_id": "receipt_1", "qualified": True})
    assert ControlPlaneIntegrity(tmp_path).verify(envelope)
    envelope["payload"]["qualified"] = False
    assert not ControlPlaneIntegrity(tmp_path).verify(envelope)


@POSIX_ONLY
def test_key_is_generated_atomically_once_and_stays_owner_only(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    signer = ControlPlaneIntegrity(state_dir)
    key_path = state_dir / ROUTING_INTEGRITY_KEY_NAME
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700
    first_bytes = key_path.read_bytes()
    assert base64.b64decode(first_bytes.strip(), validate=True)

    restarted = ControlPlaneIntegrity(state_dir)
    assert restarted.key_id == signer.key_id
    assert key_path.read_bytes() == first_bytes


def test_envelope_carries_algorithm_key_id_digest_and_tag_but_never_key(
    tmp_path: Path,
) -> None:
    signer = ControlPlaneIntegrity(tmp_path / "state")
    envelope = signer.sign({"receipt_id": "receipt_1", "qualified": True})

    assert isinstance(envelope, AuthenticatedPayload)
    assert envelope["algorithm"] == AUTHENTICATED_PAYLOAD_ALGORITHM == "hmac-sha256"
    assert envelope["key_id"] == signer.key_id
    assert len(str(envelope["payload_digest"])) == 64
    assert len(str(envelope["tag"])) == 64

    raw_key = base64.b64decode(
        (tmp_path / "state" / ROUTING_INTEGRITY_KEY_NAME).read_text(encoding="utf-8").strip(),
        validate=True,
    )
    serialized = json.dumps(envelope)
    assert raw_key.hex() not in serialized
    assert base64.b64encode(raw_key).decode("ascii") not in serialized


# -- wrong key / wrong owner / unsafe storage rejection -----------------------


def test_wrong_key_cannot_verify_envelope(tmp_path: Path) -> None:
    envelope = ControlPlaneIntegrity(tmp_path / "owner-a").sign(
        {"receipt_id": "receipt_1", "qualified": True}
    )
    assert not ControlPlaneIntegrity(tmp_path / "owner-b").verify(envelope)


def test_malformed_envelopes_are_rejected_without_raising(tmp_path: Path) -> None:
    signer = ControlPlaneIntegrity(tmp_path)
    envelope = signer.sign({"receipt_id": "receipt_1"})

    assert not signer.verify({})
    assert not signer.verify({"algorithm": "hmac-sha256"})
    assert not signer.verify("not-a-mapping")

    wrong_algorithm = dict(envelope, algorithm="hmac-sha1")
    assert not signer.verify(wrong_algorithm)

    wrong_key_id = dict(envelope, key_id="0" * 16)
    assert not signer.verify(wrong_key_id)

    tampered_tag = dict(envelope, tag="0" * 64)
    assert not signer.verify(tampered_tag)

    tampered_digest = dict(envelope, payload_digest="0" * 64)
    assert not signer.verify(tampered_digest)

    missing_payload = {key: value for key, value in envelope.items() if key != "payload"}
    assert not signer.verify(missing_payload)


@POSIX_ONLY
def test_symlink_key_is_refused(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    ControlPlaneIntegrity(state_dir)
    key_path = state_dir / ROUTING_INTEGRITY_KEY_NAME
    real_key = key_path.read_bytes()
    key_path.unlink()
    decoy = tmp_path / "decoy.key"
    decoy.write_bytes(real_key)
    key_path.symlink_to(decoy)

    with pytest.raises(RoutingIntegrityError, match="symbolic link"):
        ControlPlaneIntegrity(state_dir)


@POSIX_ONLY
def test_group_or_world_readable_key_is_refused(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    ControlPlaneIntegrity(state_dir)
    key_path = state_dir / ROUTING_INTEGRITY_KEY_NAME

    os.chmod(key_path, 0o640)
    with pytest.raises(PermissionError, match="group/world"):
        ControlPlaneIntegrity(state_dir)

    os.chmod(key_path, 0o604)
    with pytest.raises(PermissionError, match="group/world"):
        ControlPlaneIntegrity(state_dir)


@POSIX_ONLY
def test_wrong_owner_key_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    ControlPlaneIntegrity(state_dir)
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(state_dir).st_uid + 1)

    with pytest.raises(PermissionError, match="owned by the current user"):
        ControlPlaneIntegrity(state_dir)


@POSIX_ONLY
def test_malformed_base64_key_is_refused(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    ControlPlaneIntegrity(state_dir)
    key_path = state_dir / ROUTING_INTEGRITY_KEY_NAME
    key_path.write_text("!!!not-base64!!!", encoding="utf-8")
    os.chmod(key_path, 0o600)

    with pytest.raises(RoutingIntegrityError, match="base64"):
        ControlPlaneIntegrity(state_dir)


@POSIX_ONLY
def test_wrong_length_key_is_refused(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    ControlPlaneIntegrity(state_dir)
    key_path = state_dir / ROUTING_INTEGRITY_KEY_NAME
    key_path.write_text(base64.b64encode(b"too-short-key-1234").decode("ascii"), encoding="utf-8")
    os.chmod(key_path, 0o600)

    with pytest.raises(RoutingIntegrityError, match="size"):
        ControlPlaneIntegrity(state_dir)


@POSIX_ONLY
def test_ambiguous_temp_and_final_key_state_is_refused(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    ControlPlaneIntegrity(state_dir)
    temp_path = state_dir / f"{ROUTING_INTEGRITY_KEY_NAME}.tmp"
    temp_path.write_text(
        base64.b64encode(b"ambiguous-temp-key-material!!!").decode("ascii"),
        encoding="utf-8",
    )
    os.chmod(temp_path, 0o600)

    with pytest.raises(RoutingIntegrityError, match="Ambiguous"):
        ControlPlaneIntegrity(state_dir)


@POSIX_ONLY
def test_unpublished_temp_key_is_discarded_and_key_still_generates(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    temp_path = state_dir / f"{ROUTING_INTEGRITY_KEY_NAME}.tmp"
    temp_path.write_text(
        base64.b64encode(b"unpublished-temp-key-material!").decode("ascii"),
        encoding="utf-8",
    )
    os.chmod(temp_path, 0o600)

    signer = ControlPlaneIntegrity(state_dir)

    assert not temp_path.exists()
    assert signer.verify(signer.sign({"receipt_id": "receipt_1"}))


def test_load_existing_never_generates_key_material(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    with pytest.raises(RoutingIntegrityError, match="missing"):
        ControlPlaneIntegrity(state_dir, create_if_missing=False)
    assert not (state_dir / ROUTING_INTEGRITY_KEY_NAME).exists()


# -- read-only recovery inspection --------------------------------------------


def test_inspection_reports_missing_or_mismatched_without_generating_key(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    key_path = state_dir / ROUTING_INTEGRITY_KEY_NAME

    assert routing_integrity_key_state(state_dir, receipts_present=False) == "ok"
    assert routing_integrity_key_state(state_dir, receipts_present=True) == "missing_or_mismatched"
    assert not key_path.exists()

    ControlPlaneIntegrity(state_dir)
    assert routing_integrity_key_state(state_dir, receipts_present=True) == "ok"

    key_path.write_text("!!!corrupted!!!", encoding="utf-8")
    assert routing_integrity_key_state(state_dir, receipts_present=False) == "missing_or_mismatched"


# -- qualification ledger receipt authentication ------------------------------


def _run_draft(run_id: str = "qual_run_1") -> QualificationRunDraft:
    return QualificationRunDraft(
        run_id=run_id,
        owner_principal="owner@example.test",
        scope=QualificationScope(
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
        ),
        corpus=CorpusManifest(
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
        ),
        thresholds=QualificationThresholds(),
        target_snapshot={"targets": ["local-critic", "local-scout"]},
        price_snapshot={"source": "operator_verified"},
        policy_payload={"policy_id": "balanced", "revision": 1},
        learned_payload={"state": "disabled"},
        project_authority={"principal": "owner@example.test"},
        build={"version": "0.5.0", "git": "20f0565"},
        max_spend=MoneyMicros.from_usd_text("50.00"),
        effective_stop_cap=MoneyMicros.from_usd_text("25.00"),
        attempt_ceiling=MoneyMicros.from_usd_text("5.00"),
    )


def _ledger_with_receipt(
    state_dir: Path,
    payload: dict[str, object] | None = None,
) -> tuple[AgentStateStore, QualificationLedger, str]:
    state = AgentStateStore(state_dir / "agent.db")
    ledger = QualificationLedger(state)
    run = ledger.create_run(_run_draft())
    receipt = ledger.append_receipt(
        run.run_id,
        "case_result",
        payload or {"qualified": True, "score": 0.91},
    )
    return state, ledger, receipt.receipt_id


def test_ledger_receipt_envelope_survives_restart_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    state, ledger, receipt_id = _ledger_with_receipt(tmp_path / "state")
    envelope = ledger.receipt_envelope(receipt_id)

    assert envelope["algorithm"] == "hmac-sha256"
    assert envelope["payload"]["receipt_id"] == receipt_id
    assert ledger.verify_receipt_envelope(envelope)

    restarted = QualificationLedger(state)
    assert restarted.verify_receipt_envelope(envelope)

    envelope["payload"]["payload"]["qualified"] = False
    assert not restarted.verify_receipt_envelope(envelope)


def test_ledger_receipt_envelope_rejected_under_wrong_owner_key(tmp_path: Path) -> None:
    _, ledger, receipt_id = _ledger_with_receipt(tmp_path / "state-a")
    envelope = ledger.receipt_envelope(receipt_id)

    other_state = AgentStateStore(tmp_path / "state-b" / "agent.db")
    other_ledger = QualificationLedger(other_state)
    run = other_ledger.create_run(_run_draft())
    other_ledger.append_receipt(run.run_id, "case_result", {"qualified": False})
    assert not other_ledger.verify_receipt_envelope(envelope)


def test_ledger_receipt_envelope_binds_the_exact_stored_row(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    _, ledger, receipt_id = _ledger_with_receipt(state_dir)
    envelope = ledger.receipt_envelope(receipt_id)

    forged = dict(envelope)
    forged["payload"] = dict(envelope["payload"], receipt_id="rcpt_unknown")
    assert not ledger.verify_receipt_envelope(forged)

    # A validly signed projection that does not match the stored immutable row
    # must still be rejected: authentication is bound to ledger content.
    signer = ControlPlaneIntegrity(state_dir)
    phantom = signer.sign(
        {
            "receipt_id": receipt_id,
            "run_id": "qual_run_1",
            "attempt_id": None,
            "receipt_type": "case_result",
            "payload_digest": "0" * 64,
            "created_at": envelope["payload"]["created_at"],
            "payload": {"qualified": True, "score": 0.91},
        }
    )
    assert signer.verify(phantom)
    assert not ledger.verify_receipt_envelope(phantom)


def test_key_loss_after_signing_is_reported_never_silently_regenerated(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state, ledger, receipt_id = _ledger_with_receipt(state_dir)
    envelope = ledger.receipt_envelope(receipt_id)
    key_path = state_dir / ROUTING_INTEGRITY_KEY_NAME
    assert key_path.exists()
    key_path.unlink()

    # Desktop recovery inspection reports the mismatch without minting a key.
    assert routing_integrity_key_state(state_dir, receipts_present=True) == "missing_or_mismatched"
    assert not key_path.exists()
    # Read-only verification fails closed and never regenerates key material.
    assert not QualificationLedger(state).verify_receipt_envelope(envelope)
    assert not key_path.exists()
    with pytest.raises(RoutingIntegrityError, match="missing"):
        ControlPlaneIntegrity(state_dir, create_if_missing=False)
    assert not key_path.exists()


# -- desktop recovery reporting ------------------------------------------------


class _RecoveryState:
    def health_snapshot(self) -> dict[str, object]:
        return {
            "ok": True,
            "integrity": "ok",
            "schema_version": SCHEMA_VERSION,
            "writable": True,
            "error_type": None,
        }

    def count_pending_high_risk_approvals(self, *, limit: int) -> int:
        return 0


class _RecoveryRouting:
    def count_running_decisions(self, *, limit: int) -> int:
        return 0


def _recovery_service(
    routing_integrity: object = None,
) -> DesktopRecoveryService:
    kwargs: dict[str, object] = {}
    if routing_integrity is not None:
        kwargs["routing_integrity"] = routing_integrity
    return DesktopRecoveryService(
        state=_RecoveryState(),
        routing=_RecoveryRouting(),
        credential_readiness=lambda: {"state": "available"},
        memory_ready=lambda: True,
        **kwargs,
    )


def test_desktop_recovery_reports_routing_integrity_key_missing_or_mismatched() -> None:
    report = _recovery_service(lambda: "missing_or_mismatched").inspect()

    assert "routing_integrity_key_missing_or_mismatched" in report.reasons
    assert "routing_integrity_key_missing_or_mismatched" in report.blockers
    assert report.can_auto_resume is False


def test_desktop_recovery_routing_integrity_ok_is_silent() -> None:
    report = _recovery_service(lambda: "ok").inspect()

    assert "routing_integrity_key_missing_or_mismatched" not in report.reasons
    assert report.can_auto_resume is True


def test_desktop_recovery_routing_integrity_probe_failure_fails_closed() -> None:
    def _exploding_probe() -> str:
        raise RuntimeError("state directory unavailable")

    report = _recovery_service(_exploding_probe).inspect()

    assert "routing_integrity_key_missing_or_mismatched" in report.reasons
    assert report.can_auto_resume is False
