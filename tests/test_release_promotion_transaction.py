"""S2 one-wire dispatch transport and durable send-boundary tests."""

from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import ssl
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import release_control_receipt as receipts  # noqa: E402
from scripts import release_promotion_transaction as subject  # noqa: E402

NONCE = bytes.fromhex("01" * 32)
SOURCE_SHA = "a" * 40
DISPATCH_TOKEN_FINGERPRINT = "sha256:" + "d" * 64


@pytest.fixture(autouse=True)
def _isolated_dispatch_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_root = tmp_path / "controller-dispatch-state"
    state_root.mkdir()
    monkeypatch.setattr(subject, "DISPATCH_STATE_ROOT", state_root)
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    public_key = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("ascii")
    )
    monkeypatch.setattr(
        receipts,
        "_fetch_owner_signing_keys_from_github",
        lambda principal: [{"id": 1, "key": public_key, "title": "release-control"}],
    )


def _canonical(value: object) -> bytes:
    return receipts.canonical_json_bytes(value)


def _sha256(raw: bytes) -> str:
    return receipts._sha256(raw)  # noqa: SLF001


def _trust_test_gh_binary(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        receipts,
        "PINNED_GH_BINARY_DIGESTS",
        {(sys.platform, receipts.platform.machine()): _sha256(path.read_bytes())},
    )


def _prepared_files(
    tmp_path: Path,
    *,
    prepared_at: datetime = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
) -> tuple[Path, Path, Path, Path]:
    journal, _, request = receipts.prepare_dispatch_records(
        repository={"full_name": "John-MiracleWorker/Kestrel", "id": 303},
        workflow={
            "id": 707,
            "path": ".github/workflows/release.yml",
            "state": "active",
            "default_branch_sha": SOURCE_SHA,
            "observation_digest": "sha256:" + "2" * 64,
        },
        target={
            "mode": "initiate",
            "short_ref": "main",
            "full_ref": "refs/heads/main",
            "head_sha": SOURCE_SHA,
            "workflow_ref": (
                "John-MiracleWorker/Kestrel/.github/workflows/release.yml@refs/heads/main"
            ),
            "workflow_sha": SOURCE_SHA,
        },
        actor={
            "login": "kestrel-release-dispatcher[bot]",
            "id": 808,
            "app_id": 909,
            "installation_id": 1001,
        },
        inputs={
            "candidate_run_id": "1000",
            "candidate_manifest_digest": "sha256:" + "4" * 64,
            "mode": "initiate",
        },
        _nonce_source=lambda count: NONCE,
        _clock=lambda: prepared_at,
        _monotonic=lambda: 100.0,
    )
    journal_path = tmp_path / "dispatch-transaction.json"
    request_path = tmp_path / "dispatch-request.json"
    result_path = tmp_path / "dispatch-response.json"
    boundary_path = tmp_path / "dispatch-transaction.send-boundary.json"
    journal_path.write_bytes(_canonical(journal))
    request_path.write_bytes(_canonical(request))
    return journal_path, request_path, result_path, boundary_path


def _response_body(run_id: int = 1101) -> bytes:
    return _canonical(
        {
            "workflow_run_id": run_id,
            "run_url": (
                f"https://api.github.com/repos/John-MiracleWorker/Kestrel/actions/runs/{run_id}"
            ),
            "html_url": (f"https://github.com/John-MiracleWorker/Kestrel/actions/runs/{run_id}"),
        }
    )


def _candidate_manifest() -> dict[str, object]:
    check_names = (
        "nine-row-exact-wheel",
        "oci-layout",
        "protected-main-ci",
        "release-payload",
        "release-rehearsal",
        "runtime-reliability-qualification",
    )
    artifacts = [
        {
            "path": "release/kestrel.whl",
            "media_type": "application/zip",
            "sha256": "sha256:" + "a" * 64,
            "size_bytes": 1,
        }
    ]
    return {
        "schema": "kestrel.release_candidate.v1",
        "version": "0.6.0",
        "tag": "v0.6.0",
        "source": {
            "repository": "John-MiracleWorker/Kestrel",
            "repository_id": 303,
            "commit_sha": SOURCE_SHA,
            "tree_sha": "b" * 40,
            "archive_sha256": "sha256:" + "c" * 64,
            "size_bytes": 1,
        },
        "candidate_run": {
            "workflow_id": 606,
            "workflow_ref": "refs/heads/main",
            "workflow_sha": SOURCE_SHA,
            "run_id": 1000,
            "run_attempt": 1,
        },
        "checks": [
            {
                "name": name,
                "status": "success",
                "subject_sha": SOURCE_SHA,
                "run_id": 1000 + index,
                "run_attempt": 1,
                "receipt_path": f"qualification/{name}.json",
                "receipt_sha256": "sha256:" + f"{index + 1:x}" * 64,
            }
            for index, name in enumerate(check_names)
        ],
        "attestation_subjects": [
            {
                "kind": "file",
                "name": "release/kestrel.whl",
                "digest": "sha256:" + "a" * 64,
            },
            {
                "kind": "oci_index",
                "name": "ghcr.io/john-miracleworker/kestrel",
                "digest": "sha256:" + "d" * 64,
            },
        ],
        "artifacts": artifacts,
        "artifact_set_digest": receipts._sha256(_canonical(artifacts)),  # noqa: SLF001
        "planned_surfaces": ["ghcr", "github_release", "github_tag", "pypi"],
        "evidence": {
            "source_bundle_digest": "sha256:" + "e" * 64,
            "canonicalization_vector_digest": (
                "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
            ),
        },
        "provenance": {
            "producer": "scripts/release_candidate_manifest.py",
            "provider": "github.com",
            "method": "candidate-run-finalization",
        },
        "confidence": 1,
        "validation_status": "validated",
    }


def _dispatch_preparation_observations(
    *,
    default_branch_sha: str = SOURCE_SHA,
    prior_nonces: list[str] | None = None,
) -> dict[str, bytes]:
    return {
        "repository": _canonical(
            {
                "id": 303,
                "full_name": "John-MiracleWorker/Kestrel",
                "default_branch": "main",
                "default_branch_sha": default_branch_sha,
            }
        ),
        "workflow": _canonical(
            {
                "id": 707,
                "path": ".github/workflows/release.yml",
                "state": "active",
            }
        ),
        "contents": b"name: Kestrel release transaction\n",
        "manifest": _canonical(_candidate_manifest()),
        "dispatcher": _canonical(
            {
                "schema": "kestrel.dispatcher_observation.v1",
                "repository": "John-MiracleWorker/Kestrel",
                "repository_id": 303,
                "bot_login": "kestrel-release-dispatcher[bot]",
                "bot_id": 808,
                "app_id": 909,
                "installation_id": 1001,
                "permissions": {"actions": "write", "metadata": "read"},
                "complete": True,
            }
        ),
        "prior": _canonical(
            {
                "schema": "kestrel.prior_dispatch_intents.v1",
                "transaction_nonces": [] if prior_nonces is None else prior_nonces,
                "complete": True,
            }
        ),
    }


def test_recovery_dispatch_allows_advanced_main_and_preserves_tagged_workflow() -> None:
    observations = _dispatch_preparation_observations(default_branch_sha="2" * 40)
    tagged_workflow = observations["contents"]
    advanced_workflow = b"name: advanced release transaction\n"

    journal, _intent, _request = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=advanced_workflow,
        candidate_workflow_contents=tagged_workflow,
        candidate_manifest=observations["manifest"],
        mode="recover_committed",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )

    assert journal["target"]["full_ref"] == "refs/tags/v0.6.0"  # type: ignore[index]
    assert journal["target"]["head_sha"] == SOURCE_SHA  # type: ignore[index]

    with pytest.raises(ValueError, match="ingress workflow bytes"):
        subject.prepare_dispatch_from_observations(
            repository_observation=observations["repository"],
            workflow_observation=observations["workflow"],
            default_branch_workflow_contents=advanced_workflow,
            candidate_workflow_contents=tagged_workflow,
            candidate_manifest=observations["manifest"],
            mode="initiate",
            dispatcher_observation=observations["dispatcher"],
            prior_intents_observation=observations["prior"],
        )


def _source_envelope(name: str, body: bytes) -> bytes:
    return _canonical(
        {
            "schema": "kestrel.source_observation.v1",
            "name": name,
            "provider": "github.com",
            "locator": f"GET /{name}",
            "authenticated_as": "John-MiracleWorker",
            "freshness_class": "current",
            "captured_at": "2026-08-13T20:00:00Z",
            "page_count": 1,
            "record_count": 1,
            "complete": True,
            "body_encoding": "base64",
            "body": base64.b64encode(body).decode("ascii"),
        }
    )


def _contract_source_envelope(
    *,
    receipt_schema: str,
    phase: str | None,
    mode: str | None,
    name: str,
    body: bytes,
) -> bytes:
    registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
    entry = next(
        item
        for item in registry["entries"]
        if item["receipt_schema"] == receipt_schema
        and item["phase"] == phase
        and item["mode"] == mode
        and item["name"] == name
    )
    identity = (
        _canonical({"login": "John-MiracleWorker"})
        if entry["authentication_mode"] in {"github-owner", "controller-owner"}
        else None
    )
    return _canonical(
        receipts.capture_source(
            registry=registry,
            receipt_schema=receipt_schema,
            phase=phase,
            mode=mode,
            name=name,
            raw_input=body,
            identity_observation=identity,
        )
    )


def _write_prepare_cli_inputs(
    tmp_path: Path,
    observations: dict[str, bytes],
) -> dict[str, Path]:
    source_names = {
        "repository": "repository-rest",
        "workflow": "workflow-rest",
        "default_contents": "default-branch-workflow-contents",
        "candidate_contents": "candidate-workflow-contents",
        "dispatcher": "dispatcher-observation",
        "prior": "prior-intents-observation",
    }
    bodies = {
        "repository": observations["repository"],
        "workflow": observations["workflow"],
        "default_contents": observations["contents"],
        "candidate_contents": observations["contents"],
        "dispatcher": observations["dispatcher"],
        "prior": observations["prior"],
    }
    paths: dict[str, Path] = {}
    for key, source_name in source_names.items():
        path = tmp_path / f"{key}.source.json"
        path.write_bytes(
            _contract_source_envelope(
                receipt_schema="kestrel.release_dispatch_intent.v2",
                phase="prepare",
                mode="initiate",
                name=source_name,
                body=bodies[key],
            )
        )
        paths[key] = path
    manifest = tmp_path / "manifest.json"
    manifest.write_bytes(observations["manifest"])
    paths["manifest"] = manifest
    return paths


def _signing_identity(tmp_path: Path) -> Path:
    identity = tmp_path / "controller-signing-key"
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    identity.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    identity.chmod(0o600)
    return identity


def _attacker_signing_identity(tmp_path: Path) -> Path:
    identity = tmp_path / "attacker-controller-signing-key"
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
    identity.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    identity.chmod(0o600)
    return identity


def _signed_recovery_capsule_verification(
    tmp_path: Path,
    *,
    transaction_authorization: bytes,
    candidate_manifest_digest: str,
    capsule_manifest_digest: str = "sha256:" + "f" * 64,
) -> bytes:
    signing_fingerprint = "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"
    claim = {
        "schema": "kestrel.recovery_capsule_verification_claim.v1",
        "capsule_manifest_digest": capsule_manifest_digest,
        "candidate_manifest_digest": candidate_manifest_digest,
        "transaction_authorization_digest": _sha256(transaction_authorization),
        "execution_closure_digest": "sha256:" + "1" * 64,
        "repository": {
            "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
            "id": 304,
            "private": True,
        },
        "release": {"id": 4101, "tag": "recovery-707-1", "immutable": True},
        "assets": [
            {
                "id": 5100,
                "name": "recovery-bootstrap.py",
                "size_bytes": 512,
                "sha256": "sha256:" + "0" * 64,
            },
            {
                "id": 5101,
                "name": "recovery-capsule-manifest.json",
                "size_bytes": 1024,
                "sha256": capsule_manifest_digest,
            },
            {
                "id": 5102,
                "name": "recovery-capsule.tar",
                "size_bytes": 2048,
                "sha256": "sha256:" + "2" * 64,
            },
        ],
        "owner_signing_keys_observation_digest": "sha256:" + "3" * 64,
        "signing_principal": receipts.SIGNING_PRINCIPAL,
        "signing_key_fingerprint": signing_fingerprint,
        "verified_at": "2026-08-13T20:00:00Z",
        "evidence": {
            "source_bundle_digest": "sha256:" + "4" * 64,
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/release_promotion_transaction.py",
            "provider": "github.com",
            "method": "immutable-recovery-capsule-verification",
        },
        "verified": True,
        "confidence": 1,
        "validation_status": "validated",
    }
    receipt = _canonical(claim)
    signature = receipts.sign_receipt_detached(
        receipt=receipt,
        identity_file=_signing_identity(tmp_path),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    return _canonical(
        {
            "schema": "kestrel.recovery_capsule_verification.v1",
            "verification": claim,
            "receipt_digest": _sha256(receipt),
            "signature_digest": _sha256(signature),
            "receipt_base64": base64.b64encode(receipt).decode("ascii"),
            "signature_base64": base64.b64encode(signature).decode("ascii"),
            "validation_status": "validated",
        }
    )


def _owner_signing_keys_observation(captured_at: str) -> dict[str, object]:
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    public_key = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("ascii")
    )
    registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
    entry = next(
        item
        for item in registry["entries"]
        if item["receipt_schema"] == receipts.SOURCE_OBSERVATION_SCHEMA
        and item["phase"] == "release-control"
        and item["mode"] is None
        and item["name"] == "owner-signing-keys-observation"
    )
    body = _canonical(
        {
            "pages": [
                {
                    "number": 1,
                    "request_url": entry["locator"],
                    "response_headers": [],
                    "body": [{"id": 1, "key": public_key, "title": "release-control"}],
                }
            ]
        }
    )
    captured = receipts.parse_timestamp(captured_at, label="test owner keys captured_at")
    return receipts.capture_source(
        registry=registry,
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        raw_input=body,
        identity_observation=_canonical({"login": "John-MiracleWorker"}),
        _clock=lambda: captured,
    )


def _writer_inventory_arguments(
    tmp_path: Path,
    *,
    phase: str,
    captured_at: str = "2026-08-13T20:00:00Z",
    nonce_run_ids: list[int] | None = None,
) -> dict[str, bytes]:
    pre_send = phase == "pre_send"
    inventory = _canonical(
        {
            "schema": "kestrel.repository_writer_inventory.v1",
            "phase": phase,
            "repository": {
                "full_name": "John-MiracleWorker/Kestrel",
                "id": 303,
            },
            "owner": {"login": "John-MiracleWorker", "id": 606, "type": "User"},
            "repository_writers": [
                {
                    "login": "John-MiracleWorker",
                    "id": 606,
                    "type": "User",
                    "role_name": "admin",
                }
            ],
            "invitations": [],
            "write_deploy_keys": [],
            "installed_apps": (
                [
                    {
                        "app_id": 909,
                        "installation_id": 1001,
                        "bot_login": "kestrel-release-dispatcher[bot]",
                        "bot_id": 808,
                        "permissions": {"actions": "write", "metadata": "read"},
                    }
                ]
                if pre_send
                else []
            ),
            "actions_write_principals": (
                [
                    {
                        "kind": "GitHubApp",
                        "login": "kestrel-release-dispatcher[bot]",
                        "id": 808,
                        "app_id": 909,
                        "installation_id": 1001,
                    }
                ]
                if pre_send
                else []
            ),
            "mutation_capable_runs": [],
            "nonce_run_ids": [] if nonce_run_ids is None else nonce_run_ids,
            "captured_at": captured_at,
            "complete": True,
            "evidence": {
                "source_bundle_digest": "sha256:" + "1" * 64,
                "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
            },
            "provenance": {
                "producer": "kestrel-release-controller",
                "provider": "github.com",
                "method": "complete-writer-inventory",
            },
            "confidence": 1,
            "validation_status": "validated",
        }
    )
    signature = receipts.sign_receipt_detached(
        receipt=inventory,
        identity_file=_signing_identity(tmp_path),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    return {
        "writer_inventory": inventory,
        "writer_inventory_signature": signature,
        "owner_signing_keys_observation": _canonical(_owner_signing_keys_observation(captured_at)),
    }


def _reconciliation_candidate(
    journal: dict[str, object],
    *,
    matching_name_count: int = 1,
    name: str | None = None,
    observed_at: str = "2026-08-13T20:00:03Z",
) -> tuple[dict[str, object], dict[str, object]]:
    target = journal["target"]
    assert isinstance(target, dict)
    short_ref = target["short_ref"]
    full_ref = target["full_ref"]
    head_sha = target["head_sha"]
    workflow_sha = target["workflow_sha"]
    identity: dict[str, object] = {
        "schema": "kestrel.dispatch_identity.v1",
        "transaction_nonce": journal["transaction_nonce"],
        "dispatch_binding": journal["dispatch_binding"],
        "dispatch_inputs_digest": receipts._sha256(  # noqa: SLF001
            _canonical(journal["inputs"])
        ),
        "repository": "John-MiracleWorker/Kestrel",
        "repository_id": 303,
        "workflow": "Release",
        "workflow_ref": journal["target"]["workflow_ref"],  # type: ignore[index]
        "workflow_sha": workflow_sha,
        "event_name": "workflow_dispatch",
        "ref": full_ref,
        "sha": head_sha,
        "run_id": 1101,
        "run_attempt": 1,
        "actor": "kestrel-release-dispatcher[bot]",
        "actor_id": 808,
        "triggering_actor": "kestrel-release-dispatcher[bot]",
        "observed_at": observed_at,
        "evidence": {
            "source_bundle_digest": "sha256:" + "6" * 64,
            "canonicalization_vector_digest": (
                "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
            ),
        },
        "provenance": {
            "producer": "scripts/release_control_receipt.py",
            "provider": "github.com",
            "method": "github-context-allowlist",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    candidate = {
        "run_id": 1101,
        "list_observation_sha256": "sha256:" + "7" * 64,
        "get_run_observation_sha256": "sha256:" + "8" * 64,
        "run": {
            "workflow_id": 707,
            "repository_id": 303,
            "repository_full_name": "John-MiracleWorker/Kestrel",
            "path": f".github/workflows/release.yml@{short_ref}",
            "event": "workflow_dispatch",
            "display_title": journal["expected_display_title"],
            "head_branch": short_ref,
            "head_sha": head_sha,
            "run_attempt": 1,
            "actor_login": "kestrel-release-dispatcher[bot]",
            "actor_id": 808,
            "triggering_actor_login": "kestrel-release-dispatcher[bot]",
            "triggering_actor_id": 808,
            "status": "waiting",
            "conclusion": None,
        },
        "identity_artifact": {
            "artifact_id": 1201,
            "name": name or "kestrel-dispatch-identity-1101-1",
            "api_digest": "sha256:" + "9" * 64,
            "archive_sha256": "sha256:" + "9" * 64,
            "content_sha256": receipts._sha256(_canonical(identity)),  # noqa: SLF001
            "expired": False,
            "matching_name_count": matching_name_count,
        },
    }
    return candidate, identity


def _reconciliation_poll(ordinal: int) -> dict[str, object]:
    return {
        "ordinal": ordinal,
        "requested_at": f"2026-08-13T20:00:{3 + (ordinal - 1) * 5:02d}Z",
        "workflow_observation_sha256": "sha256:" + f"{ordinal:x}" * 64,
        "query": (
            "GET /repos/John-MiracleWorker/Kestrel/actions/workflows/707/"
            "runs?event=workflow_dispatch&per_page=100"
        ),
        "pages": [
            {
                "number": 1,
                "http_status": 200,
                "response_sha256": "sha256:" + f"{ordinal:x}" * 64,
                "next": None,
            }
        ],
        "complete": True,
        "result_count": 1,
        "nonce_run_ids": [1101],
        "binding_conflict_run_ids": [],
        "rejection_reasons": [],
    }


def _identity_archive(
    identity: dict[str, object], *, member: str = "dispatch-identity.json"
) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        info = zipfile.ZipInfo(member, date_time=(1980, 1, 1, 0, 0, 0))
        info.create_system = 3
        info.external_attr = 0o100600 << 16
        archive.writestr(info, _canonical(identity))
    return stream.getvalue()


def _raw_reconciliation_observations(
    journal: dict[str, object],
    candidate: dict[str, object],
    identity: dict[str, object],
) -> tuple[bytes, bytes]:
    run = candidate["run"]
    assert isinstance(run, dict)
    raw_run = {
        "id": candidate["run_id"],
        "workflow_id": run["workflow_id"],
        "repository": {
            "id": run["repository_id"],
            "full_name": run["repository_full_name"],
        },
        "path": run["path"],
        "event": run["event"],
        "display_title": run["display_title"],
        "head_branch": run["head_branch"],
        "head_sha": run["head_sha"],
        "run_attempt": run["run_attempt"],
        "actor": {"login": run["actor_login"], "id": run["actor_id"]},
        "triggering_actor": {
            "login": run["triggering_actor_login"],
            "id": run["triggering_actor_id"],
        },
        "status": run["status"],
        "conclusion": run["conclusion"],
    }
    base_query = (
        "GET /repos/John-MiracleWorker/Kestrel/actions/workflows/707/"
        "runs?event=workflow_dispatch&per_page=100"
    )
    polls = []
    get_run_body = _canonical(raw_run)
    for ordinal in range(1, 4):
        response = _canonical({"total_count": 1, "workflow_runs": [raw_run]})
        workflow_response = _canonical(
            {
                "id": 707,
                "path": ".github/workflows/release.yml",
                "state": "active",
            }
        )
        polls.append(
            {
                "requested_at": f"2026-08-13T20:00:{3 + (ordinal - 1) * 5:02d}Z",
                "workflow": {
                    "request_url": ("GET /repos/John-MiracleWorker/Kestrel/actions/workflows/707"),
                    "http_status": 200,
                    "response_headers": [["content-type", "application/json"]],
                    "response_body": base64.b64encode(workflow_response).decode("ascii"),
                },
                "pages": [
                    {
                        "number": 1,
                        "request_url": base_query,
                        "http_status": 200,
                        "response_headers": [["content-type", "application/json"]],
                        "response_body": base64.b64encode(response).decode("ascii"),
                    }
                ],
                "direct_runs": [
                    {
                        "run_id": candidate["run_id"],
                        "request_url": ("GET /repos/John-MiracleWorker/Kestrel/actions/runs/1101"),
                        "http_status": 200,
                        "response_headers": [["content-type", "application/json"]],
                        "response_body": base64.b64encode(get_run_body).decode("ascii"),
                    }
                ],
            }
        )
    workflow_runs = _canonical(
        {
            "schema": "kestrel.dispatch_workflow_runs_raw_observation.v1",
            "polls": polls,
            "complete": True,
        }
    )

    archive = _identity_archive(identity)
    archive_digest = receipts._sha256(archive)  # noqa: SLF001
    artifact = candidate["identity_artifact"]
    assert isinstance(artifact, dict)
    matching_count = artifact["matching_name_count"]
    assert isinstance(matching_count, int)
    artifacts = [
        {
            "id": 1201 + index,
            "name": artifact["name"],
            "expired": artifact["expired"],
            "digest": archive_digest,
        }
        for index in range(matching_count)
    ]
    artifact_list_body = _canonical({"total_count": matching_count, "artifacts": artifacts})
    identity_artifacts = _canonical(
        {
            "schema": "kestrel.dispatch_identity_artifact_raw_observation.v1",
            "polls": [
                {
                    "ordinal": ordinal,
                    "runs": [
                        {
                            "run_id": candidate["run_id"],
                            "pages": [
                                {
                                    "number": 1,
                                    "request_url": (
                                        "GET /repos/John-MiracleWorker/Kestrel/"
                                        "actions/runs/1101/artifacts?per_page=100"
                                    ),
                                    "http_status": 200,
                                    "response_headers": [["content-type", "application/json"]],
                                    "response_body": base64.b64encode(artifact_list_body).decode(
                                        "ascii"
                                    ),
                                }
                            ],
                            "downloads": [
                                {
                                    "artifact_id": 1201,
                                    "request_url": (
                                        "GET /repos/John-MiracleWorker/Kestrel/"
                                        "actions/artifacts/1201/zip"
                                    ),
                                    "http_status": 200,
                                    "response_headers": [["content-type", "application/zip"]],
                                    "response_body": base64.b64encode(archive).decode("ascii"),
                                }
                            ],
                        }
                    ],
                    "complete": True,
                }
                for ordinal in range(1, 4)
            ],
            "complete": True,
        }
    )
    return workflow_runs, identity_artifacts


def test_dispatch_reconciliation_accepts_escaped_controls_in_unused_github_fields() -> None:
    body = receipts.canonical_external_json_bytes(
        {
            "total_count": 0,
            "workflow_runs": [],
            "unused_commit_message": "subject\n\nbody",
        }
    )
    pages = [
        {
            "number": 1,
            "request_url": "GET /runs?per_page=100",
            "http_status": 200,
            "response_headers": [],
            "response_body": base64.b64encode(body).decode("ascii"),
        }
    ]

    items, parsed_pages, complete, reasons = subject._parse_paginated_items(  # noqa: SLF001
        pages,
        base_query="GET /runs?per_page=100",
        items_field="workflow_runs",
        label="workflow runs",
    )

    assert items == []
    assert len(parsed_pages) == 1
    assert complete is True
    assert reasons == []


def test_contract_source_reader_allows_base64_envelope_over_raw_body_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_body = b'{"full_name":"John-MiracleWorker/Kestrel","id":303}'
    registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
    envelope = receipts.capture_source(
        registry=registry,
        receipt_schema=receipts.DISPATCH_INTENT_SCHEMA,
        phase="prepare",
        mode="initiate",
        name="repository-rest",
        raw_input=raw_body,
        identity_observation=_canonical({"login": "John-MiracleWorker"}),
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    )
    envelope_path = tmp_path / "repository-source.json"
    envelope_path.write_bytes(_canonical(envelope))
    assert len(raw_body) < 128 < envelope_path.stat().st_size
    monkeypatch.setattr(receipts, "MAX_SOURCE_BODY_BYTES", 128)

    assert (
        subject._read_contract_source(  # noqa: SLF001
            envelope_path,
            label="repository source",
            receipt_schema=receipts.DISPATCH_INTENT_SCHEMA,
            phase="prepare",
            mode="initiate",
            name="repository-rest",
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 30, tzinfo=UTC),
        )
        == raw_body
    )


def test_join_reconciliation_derives_candidates_from_raw_server_bytes() -> None:
    observations = _dispatch_preparation_observations()
    journal, _, _ = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    candidate, identity = _reconciliation_candidate(journal)
    runs, artifacts = _raw_reconciliation_observations(journal, candidate, identity)

    polls, candidates = subject._join_reconciliation_observations(  # noqa: SLF001
        journal=journal,
        workflow_runs_observation=runs,
        identity_artifact_observations=artifacts,
    )

    assert [poll["nonce_run_ids"] for poll in polls] == [[1101], [1101], [1101]]
    expected_workflow = _canonical(
        {"id": 707, "path": ".github/workflows/release.yml", "state": "active"}
    )
    assert [poll["workflow_observation_sha256"] for poll in polls] == [
        _sha256(expected_workflow),
        _sha256(expected_workflow),
        _sha256(expected_workflow),
    ]
    assert candidates[0]["run"] == candidate["run"]
    assert candidates[0]["identity_artifact"]["identity"] == identity  # type: ignore[index]


def test_join_reconciliation_marks_inactive_per_poll_workflow_incomplete() -> None:
    observations = _dispatch_preparation_observations()
    journal, _, _ = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    candidate, identity = _reconciliation_candidate(journal)
    runs, artifacts = _raw_reconciliation_observations(journal, candidate, identity)
    value = json.loads(runs)
    workflow = value["polls"][1]["workflow"]
    body = json.loads(base64.b64decode(workflow["response_body"]))
    body["state"] = "disabled_manually"
    workflow["response_body"] = base64.b64encode(_canonical(body)).decode("ascii")

    polls, _ = subject._join_reconciliation_observations(  # noqa: SLF001
        journal=journal,
        workflow_runs_observation=_canonical(value),
        identity_artifact_observations=artifacts,
    )

    assert polls[1]["complete"] is False
    assert "workflow_inactive_or_mismatched" in polls[1]["rejection_reasons"]


def test_join_reconciliation_rejects_direct_run_drift_across_quiescence() -> None:
    observations = _dispatch_preparation_observations()
    journal, _, _ = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    candidate, identity = _reconciliation_candidate(journal)
    runs, artifacts = _raw_reconciliation_observations(journal, candidate, identity)
    value = json.loads(runs)
    direct = value["polls"][1]["direct_runs"][0]
    body = json.loads(base64.b64decode(direct["response_body"]))
    body["status"] = "queued"
    direct["response_body"] = base64.b64encode(_canonical(body)).decode("ascii")

    polls, _ = subject._join_reconciliation_observations(  # noqa: SLF001
        journal=journal,
        workflow_runs_observation=_canonical(value),
        identity_artifact_observations=artifacts,
    )

    assert [poll["complete"] for poll in polls] == [False, False, False]
    assert all("candidate_observation_changed" in poll["rejection_reasons"] for poll in polls)


def test_join_reconciliation_rejects_identity_drift_across_quiescence() -> None:
    observations = _dispatch_preparation_observations()
    journal, _, _ = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    candidate, identity = _reconciliation_candidate(journal)
    runs, artifacts = _raw_reconciliation_observations(journal, candidate, identity)
    value = json.loads(artifacts)
    value["polls"][1]["runs"][0]["pages"][0]["response_body"] = base64.b64encode(
        _canonical({"total_count": 0, "artifacts": []})
    ).decode("ascii")

    polls, _ = subject._join_reconciliation_observations(  # noqa: SLF001
        journal=journal,
        workflow_runs_observation=runs,
        identity_artifact_observations=_canonical(value),
    )

    assert polls[1]["complete"] is False
    assert "identity_artifact_observation_incomplete" in polls[1]["rejection_reasons"]


def test_join_reconciliation_rejects_legacy_caller_classification() -> None:
    observations = _dispatch_preparation_observations()
    journal, _, _ = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    candidate, identity = _reconciliation_candidate(journal)
    legacy_runs = _canonical(
        {
            "schema": "kestrel.dispatch_reconciliation_observation.v1",
            "polls": [_reconciliation_poll(index) for index in range(1, 4)],
            "candidates": [candidate],
            "complete": True,
        }
    )
    legacy_artifacts = _canonical(
        {
            "schema": "kestrel.dispatch_identity_artifact_observations.v1",
            "artifacts": [{"run_id": 1101, "identity": identity}],
            "complete": True,
        }
    )

    with pytest.raises(ValueError, match="raw|schema|server"):
        subject._join_reconciliation_observations(  # noqa: SLF001
            journal=journal,
            workflow_runs_observation=legacy_runs,
            identity_artifact_observations=legacy_artifacts,
        )


def test_reconciliation_checkpoint_rejects_restart_deadline_reset() -> None:
    observations = _dispatch_preparation_observations()
    journal, _, _ = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    containment = {
        "installation_id": 1001,
        "uninstalled_at": "2026-08-13T20:00:01Z",
        "installed_apps_snapshot_sha256": "sha256:" + "4" * 64,
        "pre_send_writer_inventory_digest": "sha256:" + "6" * 64,
        "post_containment_writer_inventory_digest": "sha256:" + "7" * 64,
        "token_probe": {
            "endpoint": "GET /installation/repositories",
            "http_status": 401,
            "observed_at": "2026-08-13T20:00:02Z",
            "response_sha256": "sha256:" + "5" * 64,
        },
        "validated": True,
    }
    first_poll = _reconciliation_poll(1)
    subject._persist_reconciliation_checkpoint(  # noqa: SLF001
        journal=journal,
        containment=containment,
        polls=[first_poll],
        candidates=[],
        terminal=None,
    )
    reset_poll = json.loads(_canonical(first_poll))
    reset_poll["requested_at"] = "2026-08-13T20:01:03Z"

    with pytest.raises(ValueError, match="checkpoint|history|deadline|reset"):
        subject._persist_reconciliation_checkpoint(  # noqa: SLF001
            journal=journal,
            containment=containment,
            polls=[reset_poll],
            candidates=[],
            terminal=None,
        )


def test_reconciliation_checkpoint_appends_normal_candidate_state_transitions() -> None:
    observations = _dispatch_preparation_observations()
    journal, _, _ = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    containment = {
        "installation_id": 1001,
        "uninstalled_at": "2026-08-13T20:00:01Z",
        "installed_apps_snapshot_sha256": "sha256:" + "4" * 64,
        "pre_send_writer_inventory_digest": "sha256:" + "6" * 64,
        "post_containment_writer_inventory_digest": "sha256:" + "7" * 64,
        "token_probe": {
            "endpoint": "GET /installation/repositories",
            "http_status": 401,
            "observed_at": "2026-08-13T20:00:02Z",
            "response_sha256": "sha256:" + "5" * 64,
        },
        "validated": True,
    }
    candidate, identity = _reconciliation_candidate(journal)
    candidate["identity_artifact"]["identity"] = identity  # type: ignore[index]
    subject._persist_reconciliation_checkpoint(  # noqa: SLF001
        journal=journal,
        containment=containment,
        polls=[_reconciliation_poll(1)],
        candidates=[candidate],
        terminal=None,
    )
    evolved = copy.deepcopy(candidate)
    evolved["run"]["status"] = "completed"  # type: ignore[index]
    evolved["run"]["conclusion"] = "success"  # type: ignore[index]

    subject._persist_reconciliation_checkpoint(  # noqa: SLF001
        journal=journal,
        containment=containment,
        polls=[_reconciliation_poll(1), _reconciliation_poll(2)],
        candidates=[evolved],
        terminal=None,
    )


def _run_reconcile_cli(
    tmp_path: Path,
    *,
    mode: str = "initiate",
    candidate_manifest: bytes | None = None,
    include_identity: bool = True,
    matching_name_count: int = 1,
    artifact_name: str | None = None,
    identity_observed_at: str = "2026-08-13T20:00:03Z",
    mislabel_runs_source: bool = False,
    mutate_journal: bool = False,
    omit_send_boundary: bool = False,
) -> tuple[int, dict[str, object] | None]:
    observations = _dispatch_preparation_observations()
    if candidate_manifest is not None:
        observations["manifest"] = candidate_manifest
    journal, intent, request = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode=mode,
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    candidate, identity = _reconciliation_candidate(
        journal,
        matching_name_count=matching_name_count,
        name=artifact_name,
        observed_at=identity_observed_at,
    )
    raw_runs, raw_identities = _raw_reconciliation_observations(
        journal,
        candidate,
        identity,
    )
    identity_observation = json.loads(raw_identities)
    if not include_identity:
        for poll in identity_observation["polls"]:
            poll["runs"] = []
    persisted_journal = json.loads(_canonical(journal))
    if mutate_journal:
        persisted_journal["monotonic_started_seconds"] = 101
        persisted_journal["monotonic_deadline_seconds"] = (
            101 + receipts.DISPATCH_RECONCILIATION_SECONDS
        )
    values = {
        "journal": persisted_journal,
        "intent": intent,
        "request": request,
        "containment": {
            "installation_id": 1001,
            "uninstalled_at": "2026-08-13T20:00:01Z",
            "installed_apps_snapshot_sha256": "sha256:" + "4" * 64,
            "pre_send_writer_inventory_digest": "sha256:" + "6" * 64,
            "post_containment_writer_inventory_digest": "sha256:" + "7" * 64,
            "token_probe": {
                "endpoint": "GET /installation/repositories",
                "http_status": 401,
                "observed_at": "2026-08-13T20:00:02Z",
                "response_sha256": "sha256:" + "5" * 64,
            },
            "validated": True,
        },
        "runs": json.loads(raw_runs),
        "identities": identity_observation,
    }
    paths: dict[str, Path] = {}
    for name, value in values.items():
        path = tmp_path / f"{name}.json"
        raw = _canonical(value)
        if name in {"runs", "identities"}:
            source_name = (
                "workflow-runs-observation" if name == "runs" else "identity-artifact-observations"
            )
            raw = _contract_source_envelope(
                receipt_schema="kestrel.release_dispatch_reconciliation.v1",
                phase="reconcile",
                mode=None,
                name=source_name,
                body=raw,
            )
            if name == "runs" and mislabel_runs_source:
                envelope = json.loads(raw)
                envelope["name"] = "identity-artifact-observations"
                raw = _canonical(envelope)
        path.write_bytes(raw)
        paths[name] = path
    final_runs = json.loads(raw_runs)
    final_poll = json.loads(_canonical(final_runs["polls"][-1]))
    final_poll["requested_at"] = identity_observed_at
    final_runs["polls"] = [final_poll]
    final_identity_observation = json.loads(_canonical(identity_observation))
    final_identity_poll = final_identity_observation["polls"][-1]
    final_identity_poll["ordinal"] = 1
    final_identity_observation["polls"] = [final_identity_poll]
    (tmp_path / "final-runs.json").write_bytes(
        _contract_source_envelope(
            receipt_schema="kestrel.release_dispatch_reconciliation.v1",
            phase="reconcile",
            mode=None,
            name="workflow-runs-observation",
            body=_canonical(final_runs),
        )
    )
    (tmp_path / "final-identities.json").write_bytes(
        _contract_source_envelope(
            receipt_schema="kestrel.release_dispatch_reconciliation.v1",
            phase="reconcile",
            mode=None,
            name="identity-artifact-observations",
            body=_canonical(final_identity_observation),
        )
    )
    signature_path = tmp_path / "intent.sig"
    signature_path.write_bytes(
        receipts.sign_receipt_detached(
            receipt=paths["intent"].read_bytes(),
            identity_file=_signing_identity(tmp_path),
            principal=receipts.SIGNING_PRINCIPAL,
            namespace=receipts.SIGNING_NAMESPACE,
        )
    )
    owner_keys_path = tmp_path / "intent-owner-keys.json"
    owner_keys_path.write_bytes(
        _canonical(
            _owner_signing_keys_observation(
                datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        )
    )
    if not omit_send_boundary:
        boundary = {
            "schema": "kestrel.dispatch_send_boundary.v1",
            "state": "sending",
            "transaction_nonce": journal["transaction_nonce"],
            "journal_digest": receipts._sha256(_canonical(journal)),  # noqa: SLF001
            "request_digest": journal["canonical_request_sha256"],
            "started_at": "2026-08-13T20:00:00Z",
            "token_fingerprint": DISPATCH_TOKEN_FINGERPRINT,
            "pre_send_writer_inventory_digest": "sha256:" + "6" * 64,
            "transport_policy": journal["transport_policy"],
            "validation_status": "validated",
        }
        boundary_raw = _canonical(boundary)
        receipts.write_once(subject._nonce_send_boundary_path(journal), boundary_raw)  # noqa: SLF001
        receipts.write_once(subject._send_boundary_path(paths["journal"]), boundary_raw)  # noqa: SLF001
    output = tmp_path / "reconciliation.json"
    result = subject.main(
        [
            "reconcile-dispatch",
            "--intent",
            str(paths["intent"]),
            "--journal",
            str(paths["journal"]),
            "--intent-signature",
            str(signature_path),
            "--owner-key-observation",
            str(owner_keys_path),
            "--request",
            str(paths["request"]),
            "--containment",
            str(paths["containment"]),
            "--workflow-runs-observation",
            str(paths["runs"]),
            "--identity-artifact-observations",
            str(paths["identities"]),
            "--output",
            str(output),
        ]
    )
    return result, json.loads(output.read_bytes()) if output.exists() else None


class RecordingTransport:
    def __init__(self, *, boundary: Path, result: subject.DispatchExchange) -> None:
        self.boundary = boundary
        self.result = result
        self.calls: list[tuple[str, dict[str, str], bytes, subject.OneWirePolicy]] = []

    def __call__(
        self,
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: subject.OneWirePolicy,
    ) -> subject.DispatchExchange:
        assert self.boundary.is_file()
        boundary = json.loads(self.boundary.read_bytes())
        assert boundary["state"] == "sending"
        self.calls.append((endpoint, headers, body, policy))
        return self.result


class FakeHTTPResponse:
    def __init__(self, *, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def getheaders(self) -> list[tuple[str, str]]:
        return [("Content-Type", "application/json"), ("X-Test", "one")]

    def read(self, amt: int | None = None) -> bytes:
        assert amt == subject.MAX_TRANSPORT_RESPONSE_BYTES + 1
        return self._body


class FakeHTTPSConnection:
    def __init__(
        self,
        *,
        response: FakeHTTPResponse,
        fail_at: str | None = None,
    ) -> None:
        self.response = response
        self.fail_at = fail_at
        self.requests: list[tuple[str, str, bool, bool]] = []
        self.headers: list[tuple[str, tuple[str, ...]]] = []
        self.sent: list[bytes] = []
        self.closed = False

    def connect(self) -> None:
        if self.fail_at == "connect":
            raise OSError("simulated connect failure")

    def putrequest(
        self,
        method: str,
        url: str,
        skip_host: bool = False,
        skip_accept_encoding: bool = False,
    ) -> None:
        self.requests.append((method, url, skip_host, skip_accept_encoding))

    def putheader(self, header: str, *values: str) -> None:
        self.headers.append((header, values))

    def endheaders(
        self, message_body: bytes | None = None, *, encode_chunked: bool = False
    ) -> None:
        assert message_body is None
        assert encode_chunked is False
        if self.fail_at == "endheaders":
            raise OSError("simulated header write failure")

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def getresponse(self) -> FakeHTTPResponse:
        return self.response

    def close(self) -> None:
        self.closed = True


def test_send_dispatch_persists_boundary_before_only_wire_transmission(
    tmp_path: Path,
) -> None:
    journal, request, result, boundary = _prepared_files(tmp_path)
    transport = RecordingTransport(
        boundary=boundary,
        result=subject.DispatchExchange(
            http_status=200,
            response_headers=b'{"content-type":"application/json"}',
            response_body=_response_body(),
            request_may_have_reached_peer=True,
        ),
    )

    writer_inventory = _writer_inventory_arguments(tmp_path, phase="pre_send")
    receipt = subject.send_dispatch_once(
        journal_path=journal,
        request_path=request,
        response_output=result,
        transport=transport,
        credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
        _monotonic=lambda: 101.0,
        **writer_inventory,
    )

    assert receipt["classification"] == "response_details_received"
    assert len(transport.calls) == 1
    endpoint, headers, body, policy = transport.calls[0]
    assert endpoint.endswith("/actions/workflows/707/dispatches")
    assert headers == {
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "X-GitHub-Api-Version": "2026-03-10",
    }
    assert body == request.read_bytes()
    assert policy == subject.OneWirePolicy(
        maximum_transmissions=1,
        redirects=False,
        retries=False,
        auth_replay=False,
        proxies=False,
        failover=False,
    )
    assert result.read_bytes() == _canonical(receipt)
    boundary_record = json.loads(boundary.read_bytes())
    assert boundary_record["token_fingerprint"] == DISPATCH_TOKEN_FINGERPRINT
    assert boundary_record["pre_send_writer_inventory_digest"] == receipts._sha256(  # noqa: SLF001
        writer_inventory["writer_inventory"]
    )
    assert "token" not in boundary_record

    with pytest.raises(ValueError, match="already attempted"):
        subject.send_dispatch_once(
            journal_path=journal,
            request_path=request,
            response_output=result,
            transport=transport,
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            _monotonic=lambda: 101.0,
            **writer_inventory,
        )
    assert len(transport.calls) == 1


def test_send_dispatch_rejects_an_expired_monotonic_journal_before_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal, request, result, boundary = _prepared_files(tmp_path)
    calls: list[bytes] = []

    def transport(
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: subject.OneWirePolicy,
    ) -> subject.DispatchExchange:
        del endpoint, headers, policy
        calls.append(body)
        return subject.DispatchExchange(
            http_status=None,
            response_headers=None,
            response_body=None,
            request_may_have_reached_peer=True,
        )

    monkeypatch.setattr(subject.time, "monotonic", lambda: 701.0)

    with pytest.raises(ValueError, match="deadline|expired"):
        subject.send_dispatch_once(
            journal_path=journal,
            request_path=request,
            response_output=result,
            transport=transport,
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            **_writer_inventory_arguments(tmp_path, phase="pre_send"),
        )

    assert calls == []
    assert not boundary.exists()


def test_send_dispatch_rejects_a_stale_journal_after_monotonic_reset(
    tmp_path: Path,
) -> None:
    journal, request, result, boundary = _prepared_files(tmp_path)
    calls: list[bytes] = []

    def transport(
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: subject.OneWirePolicy,
    ) -> subject.DispatchExchange:
        del endpoint, headers, policy
        calls.append(body)
        return subject.DispatchExchange(None, None, None, True)

    with pytest.raises(ValueError, match="deadline|expired|stale"):
        subject.send_dispatch_once(
            journal_path=journal,
            request_path=request,
            response_output=result,
            transport=transport,
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _clock=lambda: datetime(2026, 8, 13, 20, 10, 1, tzinfo=UTC),
            _monotonic=lambda: 101.0,
            **_writer_inventory_arguments(
                tmp_path,
                phase="pre_send",
                captured_at="2026-08-13T20:10:01Z",
            ),
        )

    assert calls == []
    assert not boundary.exists()


@pytest.mark.parametrize("mutation", ["extra-secret", "redirected-endpoint"])
def test_send_dispatch_rejects_mutated_journal_before_transport(
    tmp_path: Path, mutation: str
) -> None:
    """Catch send using caller-injected journal authority."""

    journal, request, result, boundary = _prepared_files(tmp_path)
    value = json.loads(journal.read_bytes())
    if mutation == "extra-secret":
        value["Authorization"] = "Bearer must-never-be-recorded"
    else:
        value["endpoint"] = (
            "https://api.github.com/repos/attacker/other/actions/workflows/999/dispatches"
        )
    journal.write_bytes(_canonical(value))
    calls = 0

    def transport(
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: subject.OneWirePolicy,
    ) -> subject.DispatchExchange:
        nonlocal calls
        calls += 1
        return subject.DispatchExchange(200, b"{}", _response_body(), True)

    with pytest.raises(ValueError, match="schema|field|endpoint|repository|workflow"):
        subject.send_dispatch_once(
            journal_path=journal,
            request_path=request,
            response_output=result,
            transport=transport,
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _monotonic=lambda: 101.0,
            **_writer_inventory_arguments(tmp_path, phase="pre_send"),
        )
    assert calls == 0
    assert not boundary.exists()
    assert not result.exists()


def test_send_dispatch_nonce_boundary_survives_copied_journal_paths(
    tmp_path: Path,
) -> None:
    """Catch path-local boundaries allowing the same nonce to be POSTed twice."""

    journal, request, result, _ = _prepared_files(tmp_path)
    calls = 0

    def transport(
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: subject.OneWirePolicy,
    ) -> subject.DispatchExchange:
        nonlocal calls
        calls += 1
        return subject.DispatchExchange(200, b"{}", _response_body(), True)

    subject.send_dispatch_once(
        journal_path=journal,
        request_path=request,
        response_output=result,
        transport=transport,
        credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
        _monotonic=lambda: 101.0,
        **_writer_inventory_arguments(tmp_path, phase="pre_send"),
    )
    copied_journal = tmp_path / "copied-transaction.json"
    copied_request = tmp_path / "copied-request.json"
    copied_result = tmp_path / "copied-response.json"
    copied_journal.write_bytes(journal.read_bytes())
    copied_request.write_bytes(request.read_bytes())

    with pytest.raises(ValueError, match="already attempted"):
        subject.send_dispatch_once(
            journal_path=copied_journal,
            request_path=copied_request,
            response_output=copied_result,
            transport=transport,
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            _monotonic=lambda: 101.0,
            **_writer_inventory_arguments(tmp_path, phase="pre_send"),
        )
    assert calls == 1
    assert not copied_result.exists()


def test_dispatch_response_records_the_actual_send_boundary_time(tmp_path: Path) -> None:
    """Catch response records substituting preparation time for send time."""

    journal, request, result, _ = _prepared_files(tmp_path)

    receipt = subject.send_dispatch_once(
        journal_path=journal,
        request_path=request,
        response_output=result,
        transport=lambda endpoint, headers, body, policy: subject.DispatchExchange(
            200, b"{}", _response_body(), True
        ),
        credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 7, tzinfo=UTC),
        _monotonic=lambda: 107.0,
        **_writer_inventory_arguments(tmp_path, phase="pre_send"),
    )

    assert receipt["send_started_at"] == "2026-08-13T20:00:07Z"


@pytest.mark.parametrize(
    ("may_have_reached", "expected"),
    [(False, "not_accepted"), (True, "outcome_unknown")],
)
def test_transport_failure_classification_never_retries(
    tmp_path: Path, may_have_reached: bool, expected: str
) -> None:
    journal, request, result, _ = _prepared_files(tmp_path)
    calls = 0

    def failing_transport(
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: subject.OneWirePolicy,
    ) -> subject.DispatchExchange:
        nonlocal calls
        calls += 1
        raise subject.DispatchTransportError(
            "simulated transport failure",
            request_may_have_reached_peer=may_have_reached,
        )

    receipt = subject.send_dispatch_once(
        journal_path=journal,
        request_path=request,
        response_output=result,
        transport=failing_transport,
        credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
        _monotonic=lambda: 101.0,
        **_writer_inventory_arguments(tmp_path, phase="pre_send"),
    )
    assert receipt["classification"] == expected
    assert calls == 1


def test_crash_after_possible_send_leaves_boundary_and_forbids_retry(
    tmp_path: Path,
) -> None:
    journal, request, result, boundary = _prepared_files(tmp_path)
    calls = 0

    def crashing_transport(
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: subject.OneWirePolicy,
    ) -> subject.DispatchExchange:
        nonlocal calls
        calls += 1
        assert boundary.is_file()
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        subject.send_dispatch_once(
            journal_path=journal,
            request_path=request,
            response_output=result,
            transport=crashing_transport,
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            _monotonic=lambda: 101.0,
            **_writer_inventory_arguments(tmp_path, phase="pre_send"),
        )
    assert calls == 1
    assert boundary.is_file()
    assert not result.exists()

    with pytest.raises(ValueError, match="already attempted"):
        subject.send_dispatch_once(
            journal_path=journal,
            request_path=request,
            response_output=result,
            transport=crashing_transport,
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            _monotonic=lambda: 101.0,
            **_writer_inventory_arguments(tmp_path, phase="pre_send"),
        )
    assert calls == 1


def test_containment_recovers_the_nonce_boundary_after_copy_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch a crash between the authoritative nonce boundary and its local copy."""

    journal_path, request, result, local_boundary = _prepared_files(tmp_path)
    journal = receipts._validate_dispatch_journal(  # noqa: SLF001
        json.loads(journal_path.read_bytes())
    )
    nonce_boundary = subject._nonce_send_boundary_path(journal)  # noqa: SLF001
    original_write_once = receipts.write_once

    def crash_before_local_copy(path: Path, raw: bytes) -> bool:
        if path == local_boundary:
            raise KeyboardInterrupt
        return original_write_once(path, raw)

    monkeypatch.setattr(receipts, "write_once", crash_before_local_copy)
    with pytest.raises(KeyboardInterrupt):
        subject.send_dispatch_once(
            journal_path=journal_path,
            request_path=request,
            response_output=result,
            transport=lambda endpoint, headers, body, policy: pytest.fail(
                "transport must not run before both boundary copies are durable"
            ),
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
            _monotonic=lambda: 101.0,
            **_writer_inventory_arguments(tmp_path, phase="pre_send"),
        )
    assert nonce_boundary.is_file()
    assert not local_boundary.exists()
    assert not result.exists()

    monkeypatch.setattr(receipts, "write_once", original_write_once)
    recovered = subject._load_or_recover_send_boundary(  # noqa: SLF001
        journal_path=journal_path,
        journal=journal,
    )

    assert local_boundary.is_file()
    assert local_boundary.read_bytes() == nonce_boundary.read_bytes()
    assert recovered == json.loads(nonce_boundary.read_bytes())


def test_request_bytes_must_match_fsynced_journal_before_boundary(
    tmp_path: Path,
) -> None:
    journal, request, result, boundary = _prepared_files(tmp_path)
    request.write_bytes(b'{"inputs":{},"ref":"main"}')
    called = False

    def transport(
        endpoint: str,
        headers: dict[str, str],
        body: bytes,
        policy: subject.OneWirePolicy,
    ) -> subject.DispatchExchange:
        nonlocal called
        called = True
        raise AssertionError("transport must not run")

    with pytest.raises(ValueError, match="request"):
        subject.send_dispatch_once(
            journal_path=journal,
            request_path=request,
            response_output=result,
            transport=transport,
            credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
            _monotonic=lambda: 101.0,
            **_writer_inventory_arguments(tmp_path, phase="pre_send"),
        )
    assert called is False
    assert not boundary.exists()


def test_prepared_record_write_recovers_an_exact_crash_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observations = _dispatch_preparation_observations()
    journal, intent, request = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    journal_path = tmp_path / "dispatch-transaction.json"
    intent_path = tmp_path / "dispatch-intent.json"
    request_path = tmp_path / "dispatch-request.json"
    original_write_once = receipts.write_once
    writes = 0

    def interrupted_write(path: Path, raw: bytes) -> bool:
        nonlocal writes
        writes += 1
        if writes == 3:
            raise OSError("simulated controller crash")
        return original_write_once(path, raw)

    monkeypatch.setattr(receipts, "write_once", interrupted_write)
    with pytest.raises(OSError, match="simulated"):
        subject._write_prepared_records(  # noqa: SLF001
            journal=journal,
            intent=intent,
            request=request,
            journal_output=journal_path,
            intent_output=intent_path,
            request_output=request_path,
        )
    monkeypatch.setattr(receipts, "write_once", original_write_once)

    subject._write_prepared_records(  # noqa: SLF001
        journal=journal,
        intent=intent,
        request=request,
        journal_output=journal_path,
        intent_output=intent_path,
        request_output=request_path,
    )

    assert journal_path.read_bytes() == _canonical(journal)
    assert intent_path.read_bytes() == _canonical(intent)
    assert request_path.read_bytes() == _canonical(request)


@pytest.mark.parametrize(
    ("mode", "default_branch_sha", "expected_ref"),
    [
        ("initiate", SOURCE_SHA, "main"),
        ("recover_committed", "f" * 40, "v0.6.0"),
    ],
)
def test_prepare_dispatch_derives_every_authority_field_from_observations(
    mode: str, default_branch_sha: str, expected_ref: str
) -> None:
    observations = _dispatch_preparation_observations(default_branch_sha=default_branch_sha)
    journal, intent, request = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode=mode,
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    assert journal["target"]["short_ref"] == expected_ref  # type: ignore[index]
    assert journal["target"]["head_sha"] == SOURCE_SHA  # type: ignore[index]
    assert journal["workflow"]["default_branch_sha"] == default_branch_sha  # type: ignore[index]
    assert journal["actor"]["installation_id"] == 1001  # type: ignore[index]
    assert intent["request_digest"] == receipts._sha256(_canonical(request))  # noqa: SLF001
    assert request == {
        "ref": expected_ref,
        "inputs": journal["inputs"],
    }


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("content-drift", "workflow bytes"),
        ("dispatcher-write-drift", "dispatcher"),
        ("initiate-main-drift", "current main"),
        ("nonce-reuse", "already used"),
    ],
)
def test_prepare_dispatch_rejects_observation_and_nonce_mutants(
    mutation: str, message: str
) -> None:
    observations = _dispatch_preparation_observations(
        default_branch_sha="f" * 40 if mutation == "initiate-main-drift" else SOURCE_SHA,
        prior_nonces=[NONCE.hex()] if mutation == "nonce-reuse" else None,
    )
    candidate_contents = observations["contents"]
    if mutation == "content-drift":
        candidate_contents += b"# drift\n"
    elif mutation == "dispatcher-write-drift":
        dispatcher = json.loads(observations["dispatcher"])
        dispatcher["permissions"]["contents"] = "write"
        observations["dispatcher"] = _canonical(dispatcher)

    with pytest.raises(ValueError, match=message):
        subject.prepare_dispatch_from_observations(
            repository_observation=observations["repository"],
            workflow_observation=observations["workflow"],
            default_branch_workflow_contents=observations["contents"],
            candidate_workflow_contents=candidate_contents,
            candidate_manifest=observations["manifest"],
            mode="initiate",
            dispatcher_observation=observations["dispatcher"],
            prior_intents_observation=observations["prior"],
            _nonce_source=lambda count: NONCE,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
            _monotonic=lambda: 100.0,
        )


def test_prepare_dispatch_cli_writes_three_create_once_records(
    tmp_path: Path,
) -> None:
    observations = _dispatch_preparation_observations()
    paths = _write_prepare_cli_inputs(tmp_path, observations)
    journal = tmp_path / "dispatch-transaction.json"
    intent = tmp_path / "dispatch-intent.json"
    request = tmp_path / "dispatch-request.json"

    assert (
        subject.main(
            [
                "prepare-dispatch",
                "--repository-observation",
                str(paths["repository"]),
                "--workflow-observation",
                str(paths["workflow"]),
                "--default-branch-workflow-contents",
                str(paths["default_contents"]),
                "--candidate-workflow-contents",
                str(paths["candidate_contents"]),
                "--candidate-manifest",
                str(paths["manifest"]),
                "--mode",
                "initiate",
                "--dispatcher-observation",
                str(paths["dispatcher"]),
                "--prior-intents-observation",
                str(paths["prior"]),
                "--journal-output",
                str(journal),
                "--intent-output",
                str(intent),
                "--request-output",
                str(request),
            ]
        )
        == 0
    )
    assert json.loads(journal.read_bytes())["schema"] == ("kestrel.release_dispatch_transaction.v1")
    assert json.loads(intent.read_bytes())["schema"] == ("kestrel.release_dispatch_intent.v2")
    assert set(json.loads(request.read_bytes())) == {"ref", "inputs"}

    assert (
        subject.main(
            [
                "prepare-dispatch",
                "--repository-observation",
                str(paths["repository"]),
                "--workflow-observation",
                str(paths["workflow"]),
                "--default-branch-workflow-contents",
                str(paths["default_contents"]),
                "--candidate-workflow-contents",
                str(paths["candidate_contents"]),
                "--candidate-manifest",
                str(paths["manifest"]),
                "--mode",
                "initiate",
                "--dispatcher-observation",
                str(paths["dispatcher"]),
                "--prior-intents-observation",
                str(paths["prior"]),
                "--journal-output",
                str(journal),
                "--intent-output",
                str(intent),
                "--request-output",
                str(request),
            ]
        )
        == 1
    )


def test_prepare_dispatch_cli_recovers_the_staged_nonce_after_partial_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _write_prepare_cli_inputs(tmp_path, _dispatch_preparation_observations())
    journal = tmp_path / "dispatch-transaction.json"
    intent = tmp_path / "dispatch-intent.json"
    request = tmp_path / "dispatch-request.json"
    command = [
        "prepare-dispatch",
        "--repository-observation",
        str(paths["repository"]),
        "--workflow-observation",
        str(paths["workflow"]),
        "--default-branch-workflow-contents",
        str(paths["default_contents"]),
        "--candidate-workflow-contents",
        str(paths["candidate_contents"]),
        "--candidate-manifest",
        str(paths["manifest"]),
        "--mode",
        "initiate",
        "--dispatcher-observation",
        str(paths["dispatcher"]),
        "--prior-intents-observation",
        str(paths["prior"]),
        "--journal-output",
        str(journal),
        "--intent-output",
        str(intent),
        "--request-output",
        str(request),
    ]
    original_write_once = receipts.write_once
    interrupted = False

    def crash_before_request(path: Path, raw: bytes) -> bool:
        nonlocal interrupted
        if path == request and not interrupted:
            interrupted = True
            raise OSError("simulated preparation crash")
        return original_write_once(path, raw)

    monkeypatch.setattr(receipts, "write_once", crash_before_request)
    assert subject.main(command) == 1
    assert journal.is_file()
    assert intent.is_file()
    assert not request.exists()
    nonce = json.loads(journal.read_bytes())["transaction_nonce"]

    monkeypatch.setattr(receipts, "write_once", original_write_once)
    assert subject.main(command) == 0
    assert request.is_file()
    assert json.loads(journal.read_bytes())["transaction_nonce"] == nonce
    assert subject.main(command) == 1


def test_prepare_dispatch_cli_rejects_a_mislabeled_source_envelope(
    tmp_path: Path,
) -> None:
    paths = _write_prepare_cli_inputs(tmp_path, _dispatch_preparation_observations())
    repository_envelope = json.loads(paths["repository"].read_bytes())
    repository_envelope["name"] = "workflow-rest"
    paths["repository"].write_bytes(_canonical(repository_envelope))

    assert (
        subject.main(
            [
                "prepare-dispatch",
                "--repository-observation",
                str(paths["repository"]),
                "--workflow-observation",
                str(paths["workflow"]),
                "--default-branch-workflow-contents",
                str(paths["default_contents"]),
                "--candidate-workflow-contents",
                str(paths["candidate_contents"]),
                "--candidate-manifest",
                str(paths["manifest"]),
                "--mode",
                "initiate",
                "--dispatcher-observation",
                str(paths["dispatcher"]),
                "--prior-intents-observation",
                str(paths["prior"]),
                "--journal-output",
                str(tmp_path / "journal.json"),
                "--intent-output",
                str(tmp_path / "intent.json"),
                "--request-output",
                str(tmp_path / "request.json"),
            ]
        )
        == 1
    )


def test_prepare_dispatch_cli_exposes_no_nonce_or_time_scalar() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "release_promotion_transaction.py"),
            "prepare-dispatch",
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    for forbidden in ("--nonce", "--transaction-nonce", "--now", "--clock", "--ref"):
        assert forbidden not in result.stdout


def test_reconcile_dispatch_cli_adopts_response_lost_exact_singleton(
    tmp_path: Path,
) -> None:
    result, reconciliation = _run_reconcile_cli(tmp_path)
    assert result == 0
    assert reconciliation is not None
    assert reconciliation["dispatch"]["classification"] == "outcome_unknown"  # type: ignore[index]
    assert reconciliation["outcome"]["state"] == "run_adopted"  # type: ignore[index]
    assert reconciliation["outcome"]["adopted_run_id"] == 1101  # type: ignore[index]
    assert reconciliation["tombstone"] is None


@pytest.mark.parametrize(
    (
        "include_identity",
        "matching_name_count",
        "artifact_name",
        "expected_state",
        "reason",
    ),
    [
        (False, 1, None, "reconciliation_unavailable", "observation_incomplete"),
        (
            True,
            2,
            None,
            "unsafe_orphan_or_tamper",
            "identity_artifact_name_cardinality_mismatch",
        ),
        (
            True,
            1,
            "../dispatch-identity.json",
            "unsafe_orphan_or_tamper",
            "identity_artifact_name_cardinality_mismatch",
        ),
    ],
)
def test_reconcile_dispatch_cli_tombstones_identity_artifact_failures(
    tmp_path: Path,
    include_identity: bool,
    matching_name_count: int,
    artifact_name: str | None,
    expected_state: str,
    reason: str,
) -> None:
    result, reconciliation = _run_reconcile_cli(
        tmp_path,
        include_identity=include_identity,
        matching_name_count=matching_name_count,
        artifact_name=artifact_name,
    )
    assert result == 0
    assert reconciliation is not None
    assert reconciliation["outcome"]["state"] == expected_state  # type: ignore[index]
    assert reconciliation["outcome"]["reason_code"] == reason  # type: ignore[index]
    assert reconciliation["tombstone"]["prohibition"] == (  # type: ignore[index]
        "never_issue_dispatch_admission"
    )


def test_reconcile_dispatch_cli_has_no_selection_heuristic_options() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "release_promotion_transaction.py"),
            "reconcile-dispatch",
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    for forbidden in (
        "--newest",
        "--run-number",
        "--watermark",
        "--actor",
        "--branch",
        "--status",
        "--head-sha",
    ):
        assert forbidden not in result.stdout


def test_reconcile_dispatch_cli_rejects_a_mislabeled_current_source(
    tmp_path: Path,
) -> None:
    result, reconciliation = _run_reconcile_cli(
        tmp_path,
        mislabel_runs_source=True,
    )

    assert result == 1
    assert reconciliation is None


def test_reconcile_dispatch_cli_rejects_signed_journal_digest_mismatch(
    tmp_path: Path,
) -> None:
    result, reconciliation = _run_reconcile_cli(tmp_path, mutate_journal=True)

    assert result == 1
    assert reconciliation is None


def test_reconcile_dispatch_cli_requires_the_durable_send_boundary(
    tmp_path: Path,
) -> None:
    result, reconciliation = _run_reconcile_cli(tmp_path, omit_send_boundary=True)

    assert result == 1
    assert reconciliation is None


def test_reconcile_dispatch_requires_registered_owner_intent_signer(
    tmp_path: Path,
) -> None:
    observations = _dispatch_preparation_observations()
    _, intent, _ = subject.prepare_dispatch_from_observations(
        repository_observation=observations["repository"],
        workflow_observation=observations["workflow"],
        default_branch_workflow_contents=observations["contents"],
        candidate_workflow_contents=observations["contents"],
        candidate_manifest=observations["manifest"],
        mode="initiate",
        dispatcher_observation=observations["dispatcher"],
        prior_intents_observation=observations["prior"],
        _nonce_source=lambda count: NONCE,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        _monotonic=lambda: 100.0,
    )
    intent_bytes = _canonical(intent)
    attacker_signature = receipts.sign_receipt_detached(
        receipt=intent_bytes,
        identity_file=_attacker_signing_identity(tmp_path),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )

    with pytest.raises(ValueError, match="fingerprint|owner|registered"):
        subject.verify_owner_signed_dispatch_intent(
            intent=intent_bytes,
            signature=attacker_signature,
            owner_signing_keys_observation=_canonical(
                _owner_signing_keys_observation("2026-08-13T20:00:00Z")
            ),
            _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
        )


def test_reconcile_dispatch_cli_requires_an_owner_key_observation() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "release_promotion_transaction.py"),
            "reconcile-dispatch",
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--owner-key-observation" in result.stdout


def test_publish_dispatch_admission_derives_fresh_owner_bound_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeTerminalReleaseAPI()
    monkeypatch.setattr(subject, "_terminal_release_api_from_environment", lambda: api)
    now = datetime.now(UTC).replace(microsecond=0)
    now_text = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    result, reconciliation = _run_reconcile_cli(
        tmp_path,
        identity_observed_at=now_text,
    )
    assert result == 0
    assert reconciliation is not None
    reconciliation_path = tmp_path / "adopted-reconciliation.json"
    containment_path = tmp_path / "adopted-containment.json"
    owner_keys_path = tmp_path / "owner-keys.json"
    writer_inventory_path = tmp_path / "pre-admission-writers.json"
    writer_signature_path = tmp_path / "pre-admission-writers.json.sig"
    admission_path = tmp_path / "dispatch-admission.json"
    identity_file = _signing_identity(tmp_path)
    reconciliation_path.write_bytes(_canonical(reconciliation))
    containment_path.write_bytes(_canonical(reconciliation["containment"]))
    owner_keys_path.write_bytes(_canonical(_owner_signing_keys_observation(now_text)))
    pre_admission_writer = _writer_inventory_arguments(
        tmp_path,
        phase="pre_admission",
        captured_at=now_text,
        nonce_run_ids=[1101],
    )
    writer_inventory_path.write_bytes(pre_admission_writer["writer_inventory"])
    writer_signature_path.write_bytes(pre_admission_writer["writer_inventory_signature"])

    assert (
        subject.main(
            [
                "publish-dispatch-admission",
                "--reconciliation",
                str(reconciliation_path),
                "--containment",
                str(containment_path),
                "--owner-key-observation",
                str(owner_keys_path),
                "--writer-inventory",
                str(writer_inventory_path),
                "--writer-inventory-signature",
                str(writer_signature_path),
                "--identity-file",
                str(identity_file),
                "--final-workflow-runs-observation",
                str(tmp_path / "final-runs.json"),
                "--final-identity-artifact-observations",
                str(tmp_path / "final-identities.json"),
                "--output",
                str(admission_path),
            ]
        )
        == 0
    )
    admission = json.loads(admission_path.read_bytes())
    assert admission["schema"] == "kestrel.dispatch_admission.v1"
    assert admission["adopted_run_id"] == 1101
    assert admission["reconciliation_digest"] == receipts._sha256(  # noqa: SLF001
        reconciliation_path.read_bytes()
    )
    assert admission["containment_digest"] == receipts._sha256(  # noqa: SLF001
        containment_path.read_bytes()
    )
    signature_path = admission_path.with_name(f"{admission_path.name}.sig")
    assert signature_path.is_file()
    publication_path = admission_path.with_name(
        f"{admission_path.stem}.terminal-publication-receipt.json"
    )
    assert json.loads(publication_path.read_bytes())["immutable"] is True
    assert api.publish_calls == 1
    assert receipts.verify_owner_detached_signature(
        receipt=admission_path.read_bytes(),
        signature=signature_path.read_bytes(),
        owner_signing_keys_observation=owner_keys_path.read_bytes(),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
        _clock=lambda: now,
    )


def test_publish_dispatch_tombstone_reconstructs_committed_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeTerminalReleaseAPI()
    monkeypatch.setattr(subject, "_terminal_release_api_from_environment", lambda: api)
    result, reconciliation = _run_reconcile_cli(tmp_path, include_identity=False)
    assert result == 0
    assert reconciliation is not None
    reconciliation_path = tmp_path / "failed-reconciliation.json"
    tombstone_path = tmp_path / "dispatch-tombstone.json"
    owner_keys_path = tmp_path / "tombstone-owner-keys.json"
    now = datetime.now(UTC).replace(microsecond=0)
    owner_keys_path.write_bytes(
        _canonical(_owner_signing_keys_observation(now.strftime("%Y-%m-%dT%H:%M:%SZ")))
    )
    reconciliation_path.write_bytes(_canonical(reconciliation))

    assert (
        subject.main(
            [
                "publish-dispatch-tombstone",
                "--reconciliation",
                str(reconciliation_path),
                "--reason",
                "observation_incomplete",
                "--identity-file",
                str(_signing_identity(tmp_path)),
                "--owner-key-observation",
                str(owner_keys_path),
                "--output",
                str(tombstone_path),
            ]
        )
        == 0
    )
    tombstone = json.loads(tombstone_path.read_bytes())
    assert tombstone["prohibition"] == "never_issue_dispatch_admission"
    assert tombstone["validation_status"] == "validated"
    assert tombstone_path.with_name(f"{tombstone_path.name}.sig").is_file()
    assert tombstone_path.with_name(f"{tombstone_path.stem}.reconciliation.json").is_file()
    publication_path = tombstone_path.with_name(
        f"{tombstone_path.stem}.terminal-publication-receipt.json"
    )
    assert json.loads(publication_path.read_bytes())["immutable"] is True
    assert api.publish_calls == 1


def test_terminal_publication_claim_forbids_admission_tombstone_conflict() -> None:
    admission_claim = subject.TerminalPublicationClaim(
        transaction_nonce=NONCE.hex(),
        kind="admission",
        record_digest="sha256:" + "a" * 64,
        ref_name=f"refs/tags/dispatch-terminal-claim-{NONCE.hex()}",
        tag_object_sha="b" * 40,
        target_commit_sha="c" * 40,
    )
    subject._claim_dispatch_terminal_publication(  # noqa: SLF001
        remote_claim=admission_claim,
    )

    with pytest.raises(ValueError, match="terminal|conflict|admission|tombstone"):
        subject._claim_dispatch_terminal_publication(  # noqa: SLF001
            remote_claim=subject.TerminalPublicationClaim(
                transaction_nonce=NONCE.hex(),
                kind="tombstone",
                record_digest="sha256:" + "d" * 64,
                ref_name=f"refs/tags/dispatch-terminal-claim-{NONCE.hex()}",
                tag_object_sha="e" * 40,
                target_commit_sha="f" * 40,
            ),
        )


def test_remote_terminal_claim_atomically_excludes_opposite_kind() -> None:
    api = FakeTerminalReleaseAPI()
    nonce = NONCE.hex()
    api.claim_terminal_kind(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        transaction_nonce=nonce,
        kind="admission",
        record_digest="sha256:" + "a" * 64,
    )

    with pytest.raises(ValueError, match="remote terminal.*conflict"):
        api.claim_terminal_kind(
            "John-MiracleWorker/Kestrel-Release-Recovery",
            transaction_nonce=nonce,
            kind="tombstone",
            record_digest="sha256:" + "b" * 64,
        )


def test_pending_admission_reuses_exact_preclaim_bytes_across_clock_ticks() -> None:
    raw, _ = _contract_vector("dispatch-admission")
    first = json.loads(raw)
    issued = datetime.fromisoformat(str(first["issued_at"]).replace("Z", "+00:00"))
    second = copy.deepcopy(first)
    second["issued_at"] = (issued + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    second["expires_at"] = (
        datetime.fromisoformat(str(first["expires_at"]).replace("Z", "+00:00"))
        + timedelta(seconds=1)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    first_bytes = subject._persist_or_load_terminal_record(  # noqa: SLF001
        kind="admission",
        record=first,
        _clock=lambda: issued + timedelta(seconds=1),
    )
    retried_bytes = subject._persist_or_load_terminal_record(  # noqa: SLF001
        kind="admission",
        record=second,
        _clock=lambda: issued + timedelta(seconds=2),
    )

    assert retried_bytes == first_bytes == raw


def _contract_vector(name: str) -> tuple[bytes, bytes | None]:
    bundle = json.loads(
        (
            ROOT
            / "tests"
            / "fixtures"
            / "release-control"
            / "v3"
            / "positive-contract-vectors.json"
        ).read_bytes()
    )
    matches = [item for item in bundle["vectors"] if item["name"] == name]
    assert len(matches) == 1
    item = matches[0]
    encoded = item["signature_base64"]
    return _canonical(item["record"]), (None if encoded is None else base64.b64decode(encoded))


def _signed_terminal_vector(name: str) -> tuple[bytes, bytes]:
    record, signature = _contract_vector(name)
    assert signature is not None
    return record, signature


def _complete_capsule_assets(transaction: bytes, tmp_path: Path) -> tuple[dict[str, bytes], bytes]:
    wheel = b"fixture wheel bytes"
    sandbox = b"fixture bubblewrap binary\n"
    runtime = b"fixture recovery runtime\n"
    runtime_asset = "recovery/runtime/libpython3.11.so.1.0"
    runtime_files = [
        {
            "asset_path": runtime_asset,
            "sandbox_path": (
                "/opt/hostedtoolcache/Python/3.11.14/x64/lib/libpython3.11.so.1.0"
            ),
            "sha256": _sha256(runtime),
            "size_bytes": len(runtime),
        }
    ]
    runtime_manifest = _canonical(
        {
            "schema": "kestrel.recovery_runtime.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": "3.11.14",
            "python_executable_sha256": receipts._RECOVERY_PYTHON_BINARY_DIGEST,  # noqa: SLF001
            "files": runtime_files,
        }
    )
    python_runtime_archive = b"fixture deterministic Python runtime archive"
    python_runtime_manifest = _canonical(
        {
            "schema": "kestrel.recovery_python_runtime.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": "3.11.14",
            "python_abi": "cp311",
            "python_executable_path": "bin/python3.11",
            "python_executable_sha256": receipts._RECOVERY_PYTHON_BINARY_DIGEST,  # noqa: SLF001
            "source_archive_url": receipts._RECOVERY_PYTHON_PACKAGE_URL,  # noqa: SLF001
            "source_archive_sha256": receipts._RECOVERY_PYTHON_PACKAGE_DIGEST,  # noqa: SLF001
            "runtime_archive_path": "recovery/python-runtime.tar.gz",
            "runtime_archive_sha256": _sha256(python_runtime_archive),
            "runtime_archive_size_bytes": len(python_runtime_archive),
            "runtime_tree_sha256": "sha256:" + "5" * 64,
            "runtime_file_count": 1,
            "runtime_total_size_bytes": 1,
        }
    )
    environment_manifest = _canonical(
        {
            "schema": "kestrel.recovery_environment.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": "3.11.14",
            "python_abi": "cp311",
            "environment_root": "/recovery-runtime/environment",
            "site_packages_path": (
                "/recovery-runtime/environment/lib/python3.11/site-packages"
            ),
            "site_packages_tree_sha256": "sha256:" + "6" * 64,
            "site_packages_file_count": 1,
            "site_packages_total_size_bytes": 1,
        }
    )
    closure_assets: dict[str, bytes] = {
        ".github/workflows/release.yml": b"name: Release\n",
        ".gitleaksignore": b"fixture\n",
        "evidence/normalized-source.json": b"{}",
        "recovery/bin/bwrap": sandbox,
        "recovery/environment-manifest.json": environment_manifest,
        "recovery/requirements.txt": b"# no third-party recovery dependencies\n",
        "recovery/python-runtime-manifest.json": python_runtime_manifest,
        "recovery/python-runtime.tar.gz": python_runtime_archive,
        "recovery/runtime-manifest.json": runtime_manifest,
        runtime_asset: runtime,
        "recovery/wheelhouse-manifest.json": _canonical(
            {
                "schema": "kestrel.recovery_wheelhouse.v1",
                "wheels": [
                    {
                        "filename": "fixture-1.0-py3-none-any.whl",
                        "sha256": _sha256(wheel),
                        "size_bytes": len(wheel),
                    }
                ],
            }
        ),
    }
    for name in receipts._RECOVERY_CAPSULE_SOURCE_ASSETS:  # noqa: SLF001
        closure_assets.setdefault(
            name,
            b"# recovery fixture\n" if name.endswith(".py") else b"{}",
        )
    for name in receipts._RECOVERY_CAPSULE_SCHEMA_ASSETS:  # noqa: SLF001
        closure_assets[name] = b"{}"
    closure = _canonical(
        {
            "schema": "kestrel.recovery_execution_closure.v1",
            "python_members": [
                {"path": name, "sha256": _sha256(raw)}
                for name, raw in sorted(closure_assets.items())
                if name.endswith(".py")
            ],
            "static_imports": [],
            "dynamic_imports": [],
            "shell_helpers": [],
            "data_resources": [
                {"path": name, "sha256": _sha256(raw)}
                for name, raw in sorted(closure_assets.items())
                if not name.endswith(".py")
            ],
            "external_executables": [
                {
                    "name": "python",
                    "path": "/recovery-runtime/environment/bin/python",
                    "sha256": receipts._RECOVERY_PYTHON_BINARY_DIGEST,  # noqa: SLF001
                    "version": "Python 3.11.14",
                },
                {
                    "name": "sandbox",
                    "path": "/capsule/recovery/bin/bwrap",
                    "sha256": _sha256(sandbox),
                    "version": "bubblewrap fixture 1.0",
                },
            ],
            "runtime_files": runtime_files,
            "python_runtime": {
                "implementation": "CPython",
                "version": "3.11.14",
                "abi": "cp311",
            },
            "dependency_lock": {
                "requirements_path": "recovery/requirements.txt",
                "requirements_sha256": _sha256(closure_assets["recovery/requirements.txt"]),
                "environment_manifest_sha256": _sha256(environment_manifest),
                "runtime_manifest_sha256": _sha256(runtime_manifest),
                "python_runtime_manifest_sha256": _sha256(python_runtime_manifest),
                "python_runtime_archive_sha256": _sha256(python_runtime_archive),
                "wheelhouse_manifest_sha256": _sha256(
                    closure_assets["recovery/wheelhouse-manifest.json"]
                ),
            },
            "sys_path": ["/capsule"],
            "io_roots": [{"path": "/capsule", "access": "read_write"}],
            "network_policy": {
                "default_deny": True,
                "allowed_endpoints": ["https://api.github.com"],
            },
            "evidence": {
                "source_bundle_digest": _sha256(b"fixture closure sources"),
                "canonicalization_vector_digest": (
                    "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
                ),
            },
            "provenance": {
                "producer": "scripts/recovery_launcher.py",
                "provider": "local",
                "method": "static-execution-closure",
            },
            "confidence": 1,
            "validation_status": "validated",
        }
    )
    transaction_value = json.loads(transaction)
    admission_raw, _ = _contract_vector("dispatch-admission")
    admission_value = json.loads(admission_raw)
    run = transaction_value["promotion_run"]
    candidate = transaction_value["candidate"]
    admission_value.update(
        {
            "transaction_nonce": run["transaction_nonce"],
            "adopted_run_id": run["run_id"],
            "run_attempt": run["run_attempt"],
            "repository_id": run["repository_id"],
            "workflow_id": run["workflow_id"],
            "workflow_path": run["workflow_path"],
            "expected_ref": run["ref"],
            "expected_head_sha": candidate["source_sha"],
        }
    )
    admission = _canonical(admission_value)
    admission_signature = receipts.sign_receipt_detached(
        receipt=admission,
        identity_file=_signing_identity(tmp_path),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    admission_verification = _canonical(
        {
            "receipt_digest": _sha256(admission),
            "signature_digest": _sha256(admission_signature),
            "verification_digest": _sha256(b"verified admission inputs"),
        }
    )
    recovery_authority, _ = _contract_vector("recovery-repository-authority")
    recovery_authority_signature = receipts.sign_receipt_detached(
        receipt=recovery_authority,
        identity_file=_signing_identity(tmp_path),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    dependency_receipt = {
        "schema": "kestrel.recovery_dependency_staging.v1",
        "inputs": {
            "bubblewrap_package_url": receipts._RECOVERY_BWRAP_PACKAGE_URL,  # noqa: SLF001
            "bubblewrap_package_sha256": receipts._RECOVERY_BWRAP_PACKAGE_DIGEST,  # noqa: SLF001
            "requirements_sha256": _sha256(closure_assets["recovery/requirements.txt"]),
            "python_package_url": receipts._RECOVERY_PYTHON_PACKAGE_URL,  # noqa: SLF001
            "python_package_sha256": receipts._RECOVERY_PYTHON_PACKAGE_DIGEST,  # noqa: SLF001
            "python_version": receipts._RECOVERY_PYTHON_VERSION,  # noqa: SLF001
            "python_abi": receipts._RECOVERY_PYTHON_ABI,  # noqa: SLF001
            "wheel_platform": receipts._RECOVERY_WHEEL_PLATFORM,  # noqa: SLF001
            "source_sha": transaction_value["candidate"]["source_sha"],
        },
        "outputs": {
            "bubblewrap_sha256": _sha256(sandbox),
            "bubblewrap_version": receipts._RECOVERY_BWRAP_VERSION,  # noqa: SLF001
            "wheelhouse_manifest_sha256": _sha256(
                closure_assets["recovery/wheelhouse-manifest.json"]
            ),
            "wheel_count": 1,
            "runtime_manifest_sha256": _sha256(runtime_manifest),
            "runtime_file_count": 1,
            "python_runtime_manifest_sha256": _sha256(python_runtime_manifest),
            "python_runtime_archive_sha256": _sha256(python_runtime_archive),
        },
        "provenance": receipts._RECOVERY_DEPENDENCY_STAGING_PROVENANCE,  # noqa: SLF001
        "confidence": 1,
        "validation_status": "validated",
    }
    dependency_receipt["receipt_digest"] = _sha256(_canonical(dependency_receipt))
    return (
        {
            **closure_assets,
            "candidate-archive.tar": b"candidate",
            "dispatch-admission-verification.json": admission_verification,
            "dispatch-admission.json": admission,
            "dispatch-admission.json.sig": admission_signature,
            "owner-signing-keys-observation.json": _canonical(
                _owner_signing_keys_observation("2026-08-13T20:00:00Z")
            ),
            "recovery-authority.json": recovery_authority,
            "recovery-authority.json.sig": recovery_authority_signature,
            "recovery/dependency-staging-receipt.json": _canonical(dependency_receipt),
            "recovery-execution-closure.json": closure,
            "recovery-repository-observation.json": b"{}",
            "release-authorization.json": transaction,
            "recovery/wheelhouse/fixture-1.0-py3-none-any.whl": wheel,
        },
        closure,
    )


def _recovery_capsule_verification_inputs(
    tmp_path: Path,
) -> tuple[Path, bytes, bytes, bytes, bytes, bytes, str, str]:
    transaction, _ = _contract_vector("server-authorization-initiate")
    transaction_value = json.loads(transaction)
    assets, closure = _complete_capsule_assets(transaction, tmp_path)
    inventory = [
        {"name": name, "sha256": _sha256(raw), "size_bytes": len(raw)}
        for name, raw in sorted(assets.items())
    ]
    manifest = receipts.build_recovery_capsule_manifest(
        candidate=transaction_value["candidate"],
        transaction_authorization=transaction,
        admission_authority_digest=_sha256(assets["dispatch-admission.json"]),
        source_workflows={
            name: assets[name]
            for name in (
                ".github/workflows/release-transaction.yml",
                ".github/workflows/release.yml",
            )
        },
        asset_bytes=assets,
        secret_scan={
            "image": receipts._GITLEAKS_IMAGE,  # noqa: SLF001
            "command": "dir --redact=100 --no-banner",
            "ignore_sha256": "sha256:" + "a" * 64,
            "inventory_sha256": _sha256(_canonical(inventory)),
            "redacted_report_sha256": "sha256:" + "c" * 64,
            "scanned_file_count": len(assets),
            "scanned_bytes": sum(len(raw) for raw in assets.values()),
            "unallowed_findings": 0,
        },
        recovery_repository={
            "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
            "id": 304,
            "authority_receipt_digest": _sha256(assets["recovery-authority.json"]),
            "authority_signature_digest": _sha256(assets["recovery-authority.json.sig"]),
        },
        promotion_run_id=707,
        source_records={"asset-inventory": _canonical(inventory)},
    )
    root = tmp_path / "capsule"
    for name, raw in assets.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    manifest_raw = _canonical(manifest)
    (root / "recovery-capsule-manifest.json").write_bytes(manifest_raw)
    archive = receipts.deterministic_recovery_capsule_archive(root)
    bootstrap = (root / "scripts" / "bootstrap_recovery.py").read_bytes()
    repository = _canonical(
        {
            "id": 304,
            "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
            "private": True,
        }
    )
    release = receipts.canonical_external_json_bytes(
        {
            "id": 4101,
            "tag_name": "recovery-707-1",
            "name": "Kestrel recovery capsule recovery-707-1",
            "body": (
                "Kestrel recovery capsule recovery-707-1\n\n"
                f"Kestrel-Recovery-Capsule: {_sha256(manifest_raw)}"
            ),
            "draft": False,
            "prerelease": False,
            "immutable": True,
        }
    )
    public_assets = _canonical(
        [
            {
                "id": 5100,
                "name": "recovery-bootstrap.py",
                "size": len(bootstrap),
                "digest": _sha256(bootstrap),
            },
            {
                "id": 5101,
                "name": "recovery-capsule-manifest.json",
                "size": len(manifest_raw),
                "digest": _sha256(manifest_raw),
            },
            {
                "id": 5102,
                "name": "recovery-capsule.tar",
                "size": len(archive),
                "digest": _sha256(archive),
            },
        ]
    )
    return (
        root,
        manifest_raw,
        repository,
        release,
        public_assets,
        closure,
        transaction_value["candidate"]["candidate_manifest_digest"],
        _sha256(transaction),
    )


def test_verify_recovery_capsule_binds_immutable_remote_and_local_state(
    tmp_path: Path,
) -> None:
    (
        root,
        manifest,
        repository,
        release,
        assets,
        closure,
        candidate_digest,
        transaction_digest,
    ) = _recovery_capsule_verification_inputs(tmp_path)

    verification = subject.verify_recovery_capsule(
        capsule_manifest=manifest,
        capsule_root=root,
        recovery_repository_observation=repository,
        recovery_release_observation=release,
        recovery_assets_observation=assets,
        execution_closure=closure,
        expected_candidate_digest=candidate_digest,
        expected_transaction_authorization_digest=transaction_digest,
    )

    assert verification["capsule_manifest_digest"] == _sha256(manifest)
    assert verification["execution_closure_digest"] == _sha256(closure)
    assert verification["verified"] is True


def test_verify_recovery_capsule_command_emits_owner_signed_authorization(
    tmp_path: Path,
) -> None:
    """Catch a verifier command that still emits a caller-forgeable success boolean."""

    (
        root,
        manifest,
        repository,
        release,
        assets,
        closure,
        candidate_digest,
        transaction_digest,
    ) = _recovery_capsule_verification_inputs(tmp_path)
    transaction_raw, _ = _contract_vector("server-authorization-initiate")
    assert _sha256(transaction_raw) == transaction_digest
    registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
    assets_entry = next(
        item
        for item in registry["entries"]
        if item["receipt_schema"] == receipts.SOURCE_OBSERVATION_SCHEMA
        and item["phase"] == "release-control"
        and item["mode"] is None
        and item["name"] == "recovery-assets-observation"
    )
    paginated_assets = _canonical(
        {
            "pages": [
                {
                    "number": 1,
                    "request_url": assets_entry["locator"],
                    "response_headers": [],
                    "body": json.loads(assets),
                }
            ]
        }
    )
    inputs = {
        "capsule_manifest": manifest,
        "recovery_repository_observation": _contract_source_envelope(
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="recovery-repository-observation",
            body=repository,
        ),
        "recovery_release_observation": _contract_source_envelope(
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="recovery-release-observation",
            body=release,
        ),
        "recovery_assets_observation": _contract_source_envelope(
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="recovery-assets-observation",
            body=paginated_assets,
        ),
        "execution_closure": closure,
        "owner_key_observation": _canonical(
            _owner_signing_keys_observation(
                datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        ),
    }
    paths: dict[str, Path] = {}
    for name, raw in inputs.items():
        path = tmp_path / f"{name}.json"
        path.write_bytes(raw)
        paths[name] = path
    output = tmp_path / "recovery-capsule-verification.json"
    args = subject.argparse.Namespace(
        capsule_manifest=str(paths["capsule_manifest"]),
        capsule_root=str(root),
        recovery_repository_observation=str(paths["recovery_repository_observation"]),
        recovery_release_observation=str(paths["recovery_release_observation"]),
        recovery_assets_observation=str(paths["recovery_assets_observation"]),
        execution_closure=str(paths["execution_closure"]),
        expected_candidate_digest=candidate_digest,
        expected_transaction_authorization_digest=transaction_digest,
        identity_file=str(_signing_identity(tmp_path)),
        owner_key_observation=str(paths["owner_key_observation"]),
        output=str(output),
    )

    assert subject._command_verify_recovery_capsule(args) == 0  # noqa: SLF001
    signed_verification = json.loads(output.read_bytes())
    claim = signed_verification["verification"]
    assert claim["evidence"]["source_bundle_digest"] == receipts.source_bundle_digest(
        {
            "capsule-manifest": manifest,
            "execution-closure": closure,
            "recovery-repository": paths["recovery_repository_observation"].read_bytes(),
            "recovery-release": paths["recovery_release_observation"].read_bytes(),
            "recovery-release-assets": paths["recovery_assets_observation"].read_bytes(),
        }
    )
    assert subject._authorization_capsule_digest(  # noqa: SLF001
        verification=signed_verification,
        candidate_manifest_digest=candidate_digest,
        transaction_authorization=transaction_raw,
    ) == _sha256(manifest)


def test_verify_recovery_capsule_command_rejects_raw_remote_json(tmp_path: Path) -> None:
    """Catch an owner signature that blesses caller-fabricated GitHub observations."""

    (
        root,
        manifest,
        repository,
        release,
        assets,
        closure,
        candidate_digest,
        transaction_digest,
    ) = _recovery_capsule_verification_inputs(tmp_path)
    raw_inputs = {
        "capsule_manifest": manifest,
        "recovery_repository_observation": repository,
        "recovery_release_observation": release,
        "recovery_assets_observation": assets,
        "execution_closure": closure,
        "owner_key_observation": _canonical(
            _owner_signing_keys_observation(
                datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
            )
        ),
    }
    paths: dict[str, Path] = {}
    for name, raw in raw_inputs.items():
        path = tmp_path / f"raw-{name}.json"
        path.write_bytes(raw)
        paths[name] = path
    args = subject.argparse.Namespace(
        capsule_manifest=str(paths["capsule_manifest"]),
        capsule_root=str(root),
        recovery_repository_observation=str(paths["recovery_repository_observation"]),
        recovery_release_observation=str(paths["recovery_release_observation"]),
        recovery_assets_observation=str(paths["recovery_assets_observation"]),
        execution_closure=str(paths["execution_closure"]),
        expected_candidate_digest=candidate_digest,
        expected_transaction_authorization_digest=transaction_digest,
        identity_file=str(_signing_identity(tmp_path)),
        owner_key_observation=str(paths["owner_key_observation"]),
        output=str(tmp_path / "raw-source-verification.json"),
    )

    with pytest.raises(ValueError, match="source observation|source contract"):
        subject._command_verify_recovery_capsule(args)  # noqa: SLF001


@pytest.mark.parametrize("mutation", ["candidate", "transaction", "repository", "release", "asset"])
def test_verify_recovery_capsule_rejects_cross_boundary_mutants(
    tmp_path: Path, mutation: str
) -> None:
    (
        root,
        manifest,
        repository,
        release,
        assets,
        closure,
        candidate_digest,
        transaction_digest,
    ) = _recovery_capsule_verification_inputs(tmp_path)
    if mutation == "candidate":
        candidate_digest = "sha256:" + "f" * 64
    elif mutation == "transaction":
        transaction_digest = "sha256:" + "0" * 64
    elif mutation == "repository":
        value = json.loads(repository)
        value["private"] = False
        repository = _canonical(value)
    elif mutation == "release":
        value = json.loads(release)
        value["immutable"] = False
        release = receipts.canonical_external_json_bytes(value)
    else:
        value = json.loads(assets)
        value[0]["digest"] = "sha256:" + "0" * 64
        assets = _canonical(value)

    with pytest.raises(ValueError, match="recovery capsule|capsule recovery"):
        subject.verify_recovery_capsule(
            capsule_manifest=manifest,
            capsule_root=root,
            recovery_repository_observation=repository,
            recovery_release_observation=release,
            recovery_assets_observation=assets,
            execution_closure=closure,
            expected_candidate_digest=candidate_digest,
            expected_transaction_authorization_digest=transaction_digest,
        )


def test_verify_recovery_capsule_cli_is_exposed() -> None:
    assert "verify-recovery-capsule" in subject._parser().format_help()  # noqa: SLF001


class FakeTerminalReleaseAPI:
    def __init__(
        self,
        releases: list[subject.TerminalRelease] | None = None,
        *,
        fail_after_upload_number: int | None = None,
    ) -> None:
        self.releases = [] if releases is None else list(releases)
        self.fail_after_upload_number = fail_after_upload_number
        self.create_calls = 0
        self.upload_calls: list[str] = []
        self.publish_calls = 0
        self.claim_calls: list[tuple[str, str]] = []
        self.claims: dict[str, subject.TerminalPublicationClaim] = {}

    def list_releases(self, repository: str) -> subject.TerminalReleaseListing:
        assert repository == "John-MiracleWorker/Kestrel-Release-Recovery"
        return subject.TerminalReleaseListing(tuple(self.releases), complete=True)

    def claim_terminal_kind(
        self,
        repository: str,
        *,
        transaction_nonce: str,
        kind: str,
        record_digest: str,
    ) -> subject.TerminalPublicationClaim:
        assert repository == "John-MiracleWorker/Kestrel-Release-Recovery"
        self.claim_calls.append((transaction_nonce, kind))
        requested = subject.TerminalPublicationClaim(
            transaction_nonce=transaction_nonce,
            kind=kind,
            record_digest=record_digest,
            ref_name=f"refs/tags/dispatch-terminal-claim-{transaction_nonce}",
            tag_object_sha="a" * 40,
            target_commit_sha="b" * 40,
        )
        existing = self.claims.get(transaction_nonce)
        if existing is not None and existing != requested:
            raise ValueError("remote terminal admission/tombstone claim conflict")
        self.claims[transaction_nonce] = requested
        return requested

    def create_draft(
        self,
        repository: str,
        *,
        tag_name: str,
        name: str,
        body: str,
    ) -> int:
        assert repository == "John-MiracleWorker/Kestrel-Release-Recovery"
        self.create_calls += 1
        self.releases.append(
            subject.TerminalRelease(
                release_id=4101,
                tag_name=tag_name,
                name=name,
                body=body,
                draft=True,
                prerelease=False,
                immutable=False,
                html_url=(
                    "https://github.com/John-MiracleWorker/"
                    f"Kestrel-Release-Recovery/releases/tag/{tag_name}"
                ),
                assets=(),
            )
        )
        return 4101

    def upload_asset(
        self,
        repository: str,
        *,
        release_id: int,
        name: str,
        media_type: str,
        content: bytes,
    ) -> None:
        assert repository == "John-MiracleWorker/Kestrel-Release-Recovery"
        self.upload_calls.append(name)
        release = next(item for item in self.releases if item.release_id == release_id)
        asset = subject.TerminalReleaseAsset(
            asset_id=5100 + len(self.upload_calls),
            name=name,
            size_bytes=len(content),
            digest=receipts._sha256(content),  # noqa: SLF001
            media_type=media_type,
        )
        self.releases[self.releases.index(release)] = subject.TerminalRelease(
            release_id=release.release_id,
            tag_name=release.tag_name,
            name=release.name,
            body=release.body,
            draft=release.draft,
            prerelease=release.prerelease,
            immutable=release.immutable,
            html_url=release.html_url,
            assets=tuple(sorted((*release.assets, asset), key=lambda item: item.name)),
        )
        if self.fail_after_upload_number == len(self.upload_calls):
            self.fail_after_upload_number = None
            raise RuntimeError("simulated response loss after asset upload")

    def publish_immutable(self, repository: str, *, release_id: int) -> None:
        assert repository == "John-MiracleWorker/Kestrel-Release-Recovery"
        self.publish_calls += 1
        release = next(item for item in self.releases if item.release_id == release_id)
        self.releases[self.releases.index(release)] = subject.TerminalRelease(
            release_id=release.release_id,
            tag_name=release.tag_name,
            name=release.name,
            body=release.body,
            draft=False,
            prerelease=False,
            immutable=True,
            html_url=release.html_url,
            assets=release.assets,
        )


def test_github_terminal_release_api_uses_pinned_single_call_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = tmp_path / "gh"
    gh.write_bytes(b"pinned gh")
    gh.chmod(0o700)
    token = b"github_pat_recovery_writer"
    release_body = "Kestrel dispatch admission abc"
    release_response = receipts.canonical_external_json_bytes(
        [
            [
                {
                    "id": 4101,
                    "tag_name": "dispatch-admission-abc",
                    "name": "dispatch-admission-abc",
                    "body": release_body,
                    "draft": True,
                    "prerelease": False,
                    "immutable": False,
                    "html_url": "https://github.example.invalid/release/4101",
                    "assets": [
                        {
                            "id": 5101,
                            "name": "record.json",
                            "size": 2,
                            "digest": _sha256(b"{}"),
                            "content_type": "application/json",
                        }
                    ],
                }
            ]
        ]
    )
    responses = iter(
        [
            release_response,
            b'{"id":4101}',
            b"{}",
            b"{}",
        ]
    )
    calls: list[tuple[list[str], bytes | None, dict[str, str]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(
            (
                command,
                kwargs.get("input") if type(kwargs.get("input")) is bytes else None,
                kwargs["env"],  # type: ignore[index]
            )
        )
        return subprocess.CompletedProcess(command, 0, next(responses), b"")

    monkeypatch.setattr(
        receipts.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=b"gh version 2.97.0 (2026-02-26)\n",
            stderr=b"",
        ),
    )
    _trust_test_gh_binary(gh, monkeypatch)

    api = subject.GitHubTerminalReleaseAPI(
        pinned_gh=gh,
        token=token,
        runner=runner,
    )

    listing = api.list_releases("John-MiracleWorker/Kestrel-Release-Recovery")
    api.create_draft(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        tag_name="dispatch-admission-abc",
        name="dispatch-admission-abc",
        body=release_body,
    )
    api.upload_asset(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        release_id=4101,
        name="record.json",
        media_type="application/json",
        content=b"{}",
    )
    api.publish_immutable("John-MiracleWorker/Kestrel-Release-Recovery", release_id=4101)

    assert listing.complete is True
    assert listing.releases[0].assets[0].digest == _sha256(b"{}")
    assert len(calls) == 4
    assert "--paginate" in calls[0][0] and "--slurp" in calls[0][0]
    assert calls[1][1] == receipts.canonical_external_json_bytes(
        {
            "tag_name": "dispatch-admission-abc",
            "name": "dispatch-admission-abc",
            "body": release_body,
            "draft": True,
            "prerelease": False,
            "generate_release_notes": False,
            "make_latest": "false",
        }
    )
    assert "https://uploads.github.com/" in calls[2][0][-1]
    assert calls[2][1] == b"{}"
    assert calls[3][1] == b'{"draft":false,"make_latest":"false"}'
    for command, _body, environment in calls:
        assert token.decode() not in " ".join(command)
        assert environment["GH_TOKEN"] == token.decode()


def test_github_terminal_claim_uses_one_shared_atomic_tag_ref(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = tmp_path / "gh"
    gh.write_bytes(b"pinned gh")
    gh.chmod(0o700)
    nonce = NONCE.hex()
    claim_tag = f"dispatch-terminal-claim-{nonce}"
    claim_ref = f"refs/tags/{claim_tag}"
    target_sha = "1" * 40
    tag_sha = "2" * 40
    record_digest = "sha256:" + "a" * 64
    claim_message = _canonical(
        {
            "schema": "kestrel.dispatch_terminal_remote_claim.v1",
            "transaction_nonce": nonce,
            "kind": "admission",
            "record_digest": record_digest,
        }
    ).decode("ascii")
    responses = iter(
        [
            _canonical(
                {
                    "ref": "refs/heads/main",
                    "object": {"type": "commit", "sha": target_sha},
                }
            ),
            _canonical({"sha": tag_sha}),
            _canonical({"ref": claim_ref}),
            _canonical({"ref": claim_ref, "object": {"type": "tag", "sha": tag_sha}}),
            _canonical(
                {
                    "tag": claim_tag,
                    "message": claim_message,
                    "object": {"type": "commit", "sha": target_sha},
                }
            ),
        ]
    )
    calls: list[list[str]] = []

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=next(responses), stderr=b"")

    monkeypatch.setattr(
        receipts.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=b"gh version 2.97.0 (2026-02-26)\n",
            stderr=b"",
        ),
    )
    _trust_test_gh_binary(gh, monkeypatch)
    api = subject.GitHubTerminalReleaseAPI(
        pinned_gh=gh,
        token=b"github_pat_recovery_writer",
        runner=runner,
    )

    claim = api.claim_terminal_kind(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        transaction_nonce=nonce,
        kind="admission",
        record_digest=record_digest,
    )

    assert claim == subject.TerminalPublicationClaim(
        transaction_nonce=nonce,
        kind="admission",
        record_digest=record_digest,
        ref_name=claim_ref,
        tag_object_sha=tag_sha,
        target_commit_sha=target_sha,
    )
    assert len(calls) == 5
    assert calls[2][-1].endswith("/git/refs")
    assert calls[3][-1].endswith(f"/git/ref/tags/{claim_tag}")


def test_github_terminal_claim_retry_accepts_exact_winner_after_main_advances(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = tmp_path / "gh"
    gh.write_bytes(b"pinned gh")
    gh.chmod(0o700)
    nonce = NONCE.hex()
    claim_tag = f"dispatch-terminal-claim-{nonce}"
    claim_ref = f"refs/tags/{claim_tag}"
    original_main = "1" * 40
    advanced_main = "2" * 40
    winner_tag_sha = "3" * 40
    retry_tag_sha = "4" * 40
    record_digest = "sha256:" + "a" * 64
    claim_message = _canonical(
        {
            "schema": "kestrel.dispatch_terminal_remote_claim.v1",
            "transaction_nonce": nonce,
            "kind": "admission",
            "record_digest": record_digest,
        }
    ).decode("ascii")
    responses = iter(
        [
            (
                0,
                _canonical(
                    {"ref": "refs/heads/main", "object": {"type": "commit", "sha": original_main}}
                ),
            ),
            (0, _canonical({"sha": winner_tag_sha})),
            (0, _canonical({"ref": claim_ref})),
            (0, _canonical({"ref": claim_ref, "object": {"type": "tag", "sha": winner_tag_sha}})),
            (
                0,
                _canonical(
                    {
                        "tag": claim_tag,
                        "message": claim_message,
                        "object": {"type": "commit", "sha": original_main},
                    }
                ),
            ),
            (
                0,
                _canonical(
                    {"ref": "refs/heads/main", "object": {"type": "commit", "sha": advanced_main}}
                ),
            ),
            (0, _canonical({"sha": retry_tag_sha})),
            (1, b'{"message":"Reference already exists"}'),
            (0, _canonical({"ref": claim_ref, "object": {"type": "tag", "sha": winner_tag_sha}})),
            (
                0,
                _canonical(
                    {
                        "tag": claim_tag,
                        "message": claim_message,
                        "object": {"type": "commit", "sha": original_main},
                    }
                ),
            ),
        ]
    )

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        returncode, stdout = next(responses)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=b"")

    monkeypatch.setattr(
        receipts.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, stdout=b"gh version 2.97.0 (2026-02-26)\n", stderr=b""
        ),
    )
    _trust_test_gh_binary(gh, monkeypatch)
    api = subject.GitHubTerminalReleaseAPI(
        pinned_gh=gh,
        token=b"github_pat_recovery_writer",
        runner=runner,
    )

    first = api.claim_terminal_kind(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        transaction_nonce=nonce,
        kind="admission",
        record_digest=record_digest,
    )
    retried = api.claim_terminal_kind(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        transaction_nonce=nonce,
        kind="admission",
        record_digest=record_digest,
    )

    assert retried == first
    assert retried.target_commit_sha == original_main


def test_github_terminal_claim_loser_rejects_opposite_remote_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = tmp_path / "gh"
    gh.write_bytes(b"pinned gh")
    gh.chmod(0o700)
    nonce = NONCE.hex()
    claim_tag = f"dispatch-terminal-claim-{nonce}"
    claim_ref = f"refs/tags/{claim_tag}"
    target_sha = "1" * 40
    winner_tag_sha = "2" * 40
    loser_tag_sha = "3" * 40
    admission_message = _canonical(
        {
            "schema": "kestrel.dispatch_terminal_remote_claim.v1",
            "transaction_nonce": nonce,
            "kind": "admission",
            "record_digest": "sha256:" + "a" * 64,
        }
    ).decode("ascii")
    responses = iter(
        [
            (
                0,
                _canonical(
                    {"ref": "refs/heads/main", "object": {"type": "commit", "sha": target_sha}}
                ),
            ),
            (0, _canonical({"sha": loser_tag_sha})),
            (1, b'{"message":"Reference already exists"}'),
            (0, _canonical({"ref": claim_ref, "object": {"type": "tag", "sha": winner_tag_sha}})),
            (
                0,
                _canonical(
                    {
                        "tag": claim_tag,
                        "message": admission_message,
                        "object": {"type": "commit", "sha": target_sha},
                    }
                ),
            ),
        ]
    )

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        returncode, stdout = next(responses)
        return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr=b"")

    monkeypatch.setattr(
        receipts.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            stdout=b"gh version 2.97.0 (2026-02-26)\n",
            stderr=b"",
        ),
    )
    _trust_test_gh_binary(gh, monkeypatch)
    api = subject.GitHubTerminalReleaseAPI(
        pinned_gh=gh,
        token=b"github_pat_recovery_writer",
        runner=runner,
    )

    with pytest.raises(ValueError, match="remote claim conflict"):
        api.claim_terminal_kind(
            "John-MiracleWorker/Kestrel-Release-Recovery",
            transaction_nonce=nonce,
            kind="tombstone",
            record_digest="sha256:" + "b" * 64,
        )


def test_terminal_release_publication_creates_exact_immutable_channel(
    tmp_path: Path,
) -> None:
    admission, signature = _signed_terminal_vector("dispatch-admission")
    api = FakeTerminalReleaseAPI()
    journal = tmp_path / "terminal-publication.json"
    nonce = json.loads(admission)["transaction_nonce"]
    claim = api.claim_terminal_kind(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        transaction_nonce=nonce,
        kind="admission",
        record_digest=_sha256(admission),
    )

    receipt = subject.publish_dispatch_terminal_release(
        kind="admission",
        record=admission,
        signature=signature,
        expected_signing_key_fingerprint=(
            "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"
        ),
        claim=claim,
        journal_path=journal,
        api=api,
    )

    assert receipt["tag_name"] == f"dispatch-admission-{nonce}"
    assert receipt["immutable"] is True
    assert receipt["asset_names"] == [
        "kestrel.dispatch_admission.v1.json",
        "kestrel.dispatch_admission.v1.json.sig",
    ]
    assert api.create_calls == 1
    assert api.publish_calls == 1
    assert api.upload_calls == receipt["asset_names"]
    assert journal.is_file()
    assert journal.with_name("terminal-publication.created.json").is_file()
    assert journal.with_name("terminal-publication.published.json").is_file()


def test_terminal_release_publication_rejects_opposite_tag_before_mutation(
    tmp_path: Path,
) -> None:
    admission, signature = _signed_terminal_vector("dispatch-admission")
    nonce = json.loads(admission)["transaction_nonce"]
    api = FakeTerminalReleaseAPI(
        [
            subject.TerminalRelease(
                release_id=4001,
                tag_name=f"dispatch-tombstone-{nonce}",
                name=f"dispatch-tombstone-{nonce}",
                body=f"Kestrel dispatch tombstone {nonce}",
                draft=False,
                prerelease=False,
                immutable=True,
                html_url="https://github.example.invalid/opposite",
                assets=(),
            )
        ]
    )
    claim = api.claim_terminal_kind(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        transaction_nonce=nonce,
        kind="admission",
        record_digest=_sha256(admission),
    )

    with pytest.raises(ValueError, match="opposite|tombstone|terminal"):
        subject.publish_dispatch_terminal_release(
            kind="admission",
            record=admission,
            signature=signature,
            expected_signing_key_fingerprint=(
                "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"
            ),
            claim=claim,
            journal_path=tmp_path / "terminal-publication.json",
            api=api,
        )

    assert api.create_calls == 0
    assert api.upload_calls == []
    assert api.publish_calls == 0


def test_terminal_release_publication_recovers_lost_upload_response_without_replay(
    tmp_path: Path,
) -> None:
    tombstone, signature = _signed_terminal_vector("dispatch-tombstone")
    api = FakeTerminalReleaseAPI(fail_after_upload_number=1)
    journal = tmp_path / "terminal-publication.json"
    nonce = json.loads(tombstone)["transaction_nonce"]
    claim = api.claim_terminal_kind(
        "John-MiracleWorker/Kestrel-Release-Recovery",
        transaction_nonce=nonce,
        kind="tombstone",
        record_digest=_sha256(tombstone),
    )
    arguments = {
        "kind": "tombstone",
        "record": tombstone,
        "signature": signature,
        "expected_signing_key_fingerprint": (
            "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"
        ),
        "claim": claim,
        "journal_path": journal,
        "api": api,
    }

    with pytest.raises(RuntimeError, match="response loss"):
        subject.publish_dispatch_terminal_release(**arguments)

    first_asset = "kestrel.dispatch_tombstone.v1.json"
    assert api.upload_calls == [first_asset]
    receipt = subject.publish_dispatch_terminal_release(**arguments)

    assert api.create_calls == 1
    assert api.upload_calls.count(first_asset) == 1
    assert api.upload_calls == [
        first_asset,
        "kestrel.dispatch_tombstone.v1.json.sig",
    ]
    assert api.publish_calls == 1
    assert receipt["immutable"] is True


@pytest.mark.parametrize("name", ["initiate.json", "recovery.json"])
def test_server_authorization_known_answers_pass_semantic_validation(
    name: str,
) -> None:
    raw = (
        ROOT / "tests" / "fixtures" / "release-control" / "v3" / "server-authorization" / name
    ).read_bytes()
    value = json.loads(raw)

    validated = subject.validate_server_authorization(
        value,
        expected_original_transaction_digest=(
            None
            if name == "initiate.json"
            else "sha256:c9caa303f4b4fa484020d2d8ad7a0f1e9b858339ca3d8de1faa87c832ea06af0"
        ),
        expected_owner_user_id=606,
    )

    assert _canonical(validated) == raw


@pytest.mark.parametrize(
    "mutation",
    [
        "kind-mode",
        "ref",
        "head",
        "approval-reviewer",
        "approval-reviewer-id",
        "approval-extra",
        "initiate-binding",
        "recovery-binding",
        "environment",
    ],
)
def test_server_authorization_semantic_mutants_fail_closed(mutation: str) -> None:
    recovery = mutation == "recovery-binding"
    name = "recovery.json" if recovery else "initiate.json"
    value = json.loads(
        (
            ROOT / "tests" / "fixtures" / "release-control" / "v3" / "server-authorization" / name
        ).read_bytes()
    )
    if mutation == "kind-mode":
        value["authorization_kind"] = "execution"
    elif mutation == "ref":
        value["promotion_run"]["ref"] = "refs/tags/v1.2.3"
    elif mutation == "head":
        value["promotion_run"]["head_sha"] = "c" * 40
    elif mutation == "approval-reviewer":
        value["approval_history"]["records"][0]["reviewer"]["login"] = "attacker"
    elif mutation == "approval-reviewer-id":
        value["approval_history"]["records"][0]["reviewer"]["id"] = 1
    elif mutation == "approval-extra":
        value["approval_history"]["records"].append(
            copy.deepcopy(value["approval_history"]["records"][0])
        )
    elif mutation == "initiate-binding":
        value["bindings"]["transaction_authorization_digest"] = "sha256:" + "f" * 64
    elif mutation == "recovery-binding":
        value["bindings"]["transaction_authorization_digest"] = "sha256:" + "e" * 64
    else:
        value["environment"]["name"] = "release-commit"

    with pytest.raises(ValueError, match="authorization|binding|run|approval|environment"):
        subject.validate_server_authorization(
            value,
            expected_original_transaction_digest=(
                "sha256:c9caa303f4b4fa484020d2d8ad7a0f1e9b858339ca3d8de1faa87c832ea06af0"
                if recovery
                else None
            ),
            expected_owner_user_id=606,
        )


@pytest.mark.parametrize(
    ("mode", "authorization_kind"),
    [("initiate", "transaction"), ("recover_committed", "execution")],
)
def test_build_server_authorization_derives_mode_specific_bindings(
    mode: str, authorization_kind: str
) -> None:
    fixture_root = ROOT / "tests" / "fixtures" / "release-control" / "v3"
    expected_raw = (
        fixture_root
        / "server-authorization"
        / ("initiate.json" if mode == "initiate" else "recovery.json")
    ).read_bytes()
    expected = json.loads(expected_raw)
    original = (
        None
        if mode == "initiate"
        else (fixture_root / "server-authorization" / "initiate.json").read_bytes()
    )

    authority = subject.build_server_authorization(
        candidate=expected["candidate"],
        promotion_run=expected["promotion_run"],
        environment=expected["environment"],
        approval_history=expected["approval_history"],
        admission_authority=expected["admission_authority"],
        repository_state=expected["repository_state"],
        mode=mode,
        transaction_authorization=original,
        recovery_capsule_manifest_digest=expected["bindings"]["recovery_capsule_manifest_digest"],
        commit_marker_digest=expected["bindings"]["commit_marker_digest"],
        source_records={"server-input": b"{}"},
        expected_owner_user_id=606,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
    )

    assert authority["authorization_kind"] == authorization_kind
    assert authority["bindings"] == expected["bindings"]
    assert (
        subject.validate_server_authorization(
            authority,
            expected_original_transaction_digest=(None if original is None else _sha256(original)),
            expected_owner_user_id=606,
        )
        == authority
    )


def test_build_recovery_authorization_rejects_nontransaction_original() -> None:
    fixture_root = ROOT / "tests" / "fixtures" / "release-control" / "v3"
    recovery = json.loads((fixture_root / "server-authorization" / "recovery.json").read_bytes())
    recovery_raw = _canonical(recovery)

    with pytest.raises(ValueError, match="original transaction authorization"):
        subject.build_server_authorization(
            candidate=recovery["candidate"],
            promotion_run=recovery["promotion_run"],
            environment=recovery["environment"],
            approval_history=recovery["approval_history"],
            admission_authority=recovery["admission_authority"],
            repository_state=recovery["repository_state"],
            mode="recover_committed",
            transaction_authorization=recovery_raw,
            recovery_capsule_manifest_digest=recovery["bindings"][
                "recovery_capsule_manifest_digest"
            ],
            commit_marker_digest=recovery["bindings"]["commit_marker_digest"],
            source_records={"server-input": b"{}"},
            expected_owner_user_id=606,
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC),
        )


def test_authorize_cli_is_exposed() -> None:
    assert "authorize" in subject._parser().format_help()  # noqa: SLF001


def test_authorization_rejects_caller_normalized_promotion_run() -> None:
    authorization_raw, _ = _contract_vector("server-authorization-initiate")
    authorization = json.loads(authorization_raw)
    identity_raw, _ = _contract_vector("dispatch-identity")
    identity = json.loads(identity_raw)

    with pytest.raises(ValueError, match="promotion REST observation"):
        subject._authorization_promotion_run(  # noqa: SLF001
            run_observation=authorization["promotion_run"],
            run_observation_raw=_canonical(authorization["promotion_run"]),
            identity=identity,
            identity_raw=identity_raw,
        )


def test_authorization_file_requires_contract_source_or_canonical_record(
    tmp_path: Path,
) -> None:
    api_path = tmp_path / "api.json"
    api_body = _canonical({"id": 1_155_799_292})
    api_path.write_bytes(
        _contract_source_envelope(
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="repository-observation",
            body=api_body,
        )
    )

    raw, body, value = subject._authorization_file(  # noqa: SLF001
        api_path,
        label="API observation",
        source_name="repository-observation",
    )

    assert raw != body
    assert body == api_body
    assert value == {"id": 1_155_799_292}

    record_path = tmp_path / "record.json"
    record_path.write_text('{\n  "schema": "kestrel.untrusted_record.v1"\n}\n', encoding="utf-8")
    with pytest.raises(ValueError, match="canonical"):
        subject._authorization_file(  # noqa: SLF001
            record_path, label="canonical record"
        )

    record_path.write_bytes(_canonical({"schema": "kestrel.local_record.v1"}))
    assert subject._authorization_file(  # noqa: SLF001
        record_path, label="canonical record"
    )[2] == {"schema": "kestrel.local_record.v1"}


def test_recovery_authorization_rejects_cross_transaction_capsule(tmp_path: Path) -> None:
    transaction_raw, _ = _contract_vector("server-authorization-initiate")
    transaction = json.loads(transaction_raw)
    verification = json.loads(
        _signed_recovery_capsule_verification(
            tmp_path,
            transaction_authorization=_canonical({}),
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
        )
    )

    with pytest.raises(ValueError, match="capsule verification binding"):
        subject._authorization_capsule_digest(  # noqa: SLF001
            verification=verification,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
            transaction_authorization=transaction_raw,
        )


def test_recovery_authorization_rejects_self_certified_capsule_verification() -> None:
    """Catch an unsigned caller-controlled `verified: true` authorization bypass."""

    transaction_raw, _ = _contract_vector("server-authorization-initiate")
    transaction = json.loads(transaction_raw)
    verification = {
        "schema": "kestrel.recovery_capsule_verification.v1",
        "capsule_manifest_digest": "sha256:" + "f" * 64,
        "candidate_manifest_digest": transaction["candidate"]["candidate_manifest_digest"],
        "transaction_authorization_digest": _sha256(transaction_raw),
        "verified": True,
        "validation_status": "validated",
    }

    with pytest.raises(ValueError, match="signed recovery capsule verification"):
        subject._authorization_capsule_digest(  # noqa: SLF001
            verification=verification,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
            transaction_authorization=transaction_raw,
        )


def test_recovery_authorization_accepts_current_owner_signed_verification(
    tmp_path: Path,
) -> None:
    """Catch verification code that checks fields but never authenticates the owner claim."""

    transaction_raw, _ = _contract_vector("server-authorization-initiate")
    transaction = json.loads(transaction_raw)
    verification = json.loads(
        _signed_recovery_capsule_verification(
            tmp_path,
            transaction_authorization=transaction_raw,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
        )
    )

    assert (
        subject._authorization_capsule_digest(  # noqa: SLF001
            verification=verification,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
            transaction_authorization=transaction_raw,
        )
        == "sha256:" + "f" * 64
    )


def test_recovery_authorization_rejects_tampered_signed_verification(
    tmp_path: Path,
) -> None:
    transaction_raw, _ = _contract_vector("server-authorization-initiate")
    transaction = json.loads(transaction_raw)
    verification = json.loads(
        _signed_recovery_capsule_verification(
            tmp_path,
            transaction_authorization=transaction_raw,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
        )
    )
    verification["verification"]["capsule_manifest_digest"] = "sha256:" + "e" * 64

    with pytest.raises(ValueError, match="manifest asset binding|receipt bytes mismatch"):
        subject._authorization_capsule_digest(  # noqa: SLF001
            verification=verification,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
            transaction_authorization=transaction_raw,
        )


def test_recovery_authorization_rejects_signed_open_nested_claim(tmp_path: Path) -> None:
    """Catch a valid owner signature around a claim whose nested contract is open."""

    transaction_raw, _ = _contract_vector("server-authorization-initiate")
    transaction = json.loads(transaction_raw)
    verification = json.loads(
        _signed_recovery_capsule_verification(
            tmp_path,
            transaction_authorization=transaction_raw,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
        )
    )
    verification["verification"]["repository"]["surplus"] = True
    receipt = _canonical(verification["verification"])
    signature = receipts.sign_receipt_detached(
        receipt=receipt,
        identity_file=_signing_identity(tmp_path),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    verification.update(
        {
            "receipt_digest": _sha256(receipt),
            "signature_digest": _sha256(signature),
            "receipt_base64": base64.b64encode(receipt).decode("ascii"),
            "signature_base64": base64.b64encode(signature).decode("ascii"),
        }
    )

    with pytest.raises(ValueError, match="verification repository fields mismatch"):
        subject._authorization_capsule_digest(  # noqa: SLF001
            verification=verification,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
            transaction_authorization=transaction_raw,
        )


def test_recovery_authorization_rejects_key_not_currently_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transaction_raw, _ = _contract_vector("server-authorization-initiate")
    transaction = json.loads(transaction_raw)
    verification = json.loads(
        _signed_recovery_capsule_verification(
            tmp_path,
            transaction_authorization=transaction_raw,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
        )
    )
    attacker = Ed25519PrivateKey.from_private_bytes(bytes(range(33, 65)))
    attacker_public_key = (
        attacker.public_key()
        .public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        )
        .decode("ascii")
    )
    monkeypatch.setattr(
        receipts,
        "_fetch_owner_signing_keys_from_github",
        lambda principal: [{"id": 2, "key": attacker_public_key, "title": "attacker"}],
    )

    with pytest.raises(ValueError, match="fingerprint|signature|owner"):
        subject._authorization_capsule_digest(  # noqa: SLF001
            verification=verification,
            candidate_manifest_digest=transaction["candidate"]["candidate_manifest_digest"],
            transaction_authorization=transaction_raw,
        )


def _environment_gate_observation(name: str, environment_id: int) -> dict[str, object]:
    return {
        "name": name,
        "id": environment_id,
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
        "protection_rules": [
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [
                    {
                        "type": "User",
                        "reviewer": {
                            "login": "John-MiracleWorker",
                            "id": 58918509,
                            "type": "User",
                        },
                    }
                ],
            }
        ],
    }


def _environment_policy_observation(environment_id: int) -> list[dict[str, object]]:
    return [
        {
            "total_count": 2,
            "branch_policies": [
                {"id": environment_id * 10 + 1, "name": "main"},
                {"id": environment_id * 10 + 2, "name": "v*"},
            ],
        }
    ]


def test_environment_gate_normalizes_exact_owner_and_two_policies() -> None:
    gate, policies = subject._environment_gate_from_observations(  # noqa: SLF001
        environment=_environment_gate_observation("release", 901),
        policies=_environment_policy_observation(901),
        policies_digest="sha256:" + "1" * 64,
        expected_name="release",
        expected_owner_login="John-MiracleWorker",
        expected_owner_user_id=58918509,
    )

    assert gate["id"] == 901
    assert policies == ((9011, "main"), (9012, "v*"))


@pytest.mark.parametrize(
    "mutation",
    ["environment-name", "reviewer-id", "deployment-policy", "extra-policy"],
)
def test_environment_gate_rejects_substituted_or_weaker_policy(
    mutation: str,
) -> None:
    environment = _environment_gate_observation("release", 901)
    policies = _environment_policy_observation(901)
    if mutation == "environment-name":
        environment["name"] = "release-commit"
    elif mutation == "reviewer-id":
        environment["protection_rules"][0]["reviewers"][0]["reviewer"]["id"] = 1  # type: ignore[index]
    elif mutation == "deployment-policy":
        environment["deployment_branch_policy"]["custom_branch_policies"] = False  # type: ignore[index]
    else:
        policies[0]["branch_policies"].append({"id": 9013, "name": "dev"})  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="environment|review|deployment|policy"):
        subject._environment_gate_from_observations(  # noqa: SLF001
            environment=environment,
            policies=policies,
            policies_digest="sha256:" + "1" * 64,
            expected_name="release",
            expected_owner_login="John-MiracleWorker",
            expected_owner_user_id=58918509,
        )


def test_operational_environment_policies_join_rest_ids_to_signed_types() -> None:
    raw, _ = _contract_vector("github-authority")
    authority = json.loads(raw)
    environment_ids = {
        item["environment_name"]: item["environment_id"]
        for item in authority["environment_policies"]
    }
    environments = {
        name: {"name": name, "id": environment_id}
        for name, environment_id in environment_ids.items()
    }
    observed = {
        name: tuple(
            sorted(
                (item["policy_id"], item["name"])
                for item in authority["environment_policies"]
                if item["environment_name"] == name
            )
        )
        for name in environments
    }

    subject._require_operational_environment_policy_join(  # noqa: SLF001
        github_authority=authority,
        environments=environments,
        observed_policies=observed,
    )

    substituted = {name: tuple(items) for name, items in observed.items()}
    substituted["release"] = (
        (substituted["release"][0][0] + 100, "main"),
        substituted["release"][1],
    )
    with pytest.raises(ValueError, match="policy authority mismatch"):
        subject._require_operational_environment_policy_join(  # noqa: SLF001
            github_authority=authority,
            environments=environments,
            observed_policies=substituted,
        )


def test_release_prerequisites_vector_passes_mode_policy() -> None:
    record, _ = _contract_vector("release-prerequisites")
    value = json.loads(record)

    assert subject.validate_release_prerequisites(value) == value


def test_hosted_smoke_prerequisites_preserve_exact_operational_blockers() -> None:
    record, _ = _contract_vector("release-prerequisites")
    value = json.loads(record)
    value["mode"] = "hosted-smoke"
    value["recovery_repository"]["authority_digest"] = None
    value["recovery_repository"]["immutable_releases"] = False
    value["operational_blockers"] = [
        "environment_policy_types_unverified",
        "github_authority_unprovisioned",
        "pypi_authority_unprovisioned",
        "recovery_authority_unprovisioned",
    ]
    value["validation_status"] = "validated_for_hosted_smoke"
    value["workflow_inventory"][0]["id"] = None
    value["workflow_inventory"][0]["state"] = "unverified"
    value["ingress_observation"]["ruleset_id"] = None
    value["ingress_observation"]["active"] = False
    value["ingress_observation"]["workflow_byte_equal"] = False

    assert subject.validate_release_prerequisites(value) == value


def test_operational_prerequisites_reject_unprovisioned_authority() -> None:
    record, _ = _contract_vector("release-prerequisites")
    value = json.loads(record)
    value["recovery_repository"]["authority_digest"] = None

    with pytest.raises(ValueError, match="operational.*authority"):
        subject.validate_release_prerequisites(value)


def test_release_prerequisites_reject_duplicate_environment_ids() -> None:
    record, _ = _contract_vector("release-prerequisites")
    value = json.loads(record)
    value["environments"][1]["id"] = value["environments"][0]["id"]

    with pytest.raises(ValueError, match="environment authority"):
        subject.validate_release_prerequisites(value)


def test_inspect_prerequisites_cli_is_exposed() -> None:
    assert "inspect-prerequisites" in subject._parser().format_help()  # noqa: SLF001


def test_repository_identity_joins_the_numeric_owner() -> None:
    repository = {
        "id": 1_155_799_292,
        "full_name": "John-MiracleWorker/Kestrel",
        "owner": {
            "login": "John-MiracleWorker",
            "id": 58918509,
            "type": "User",
        },
    }
    assert (
        subject._require_repository_identity(  # noqa: SLF001
            repository,
            expected_repository="John-MiracleWorker/Kestrel",
            expected_repository_id=1_155_799_292,
            expected_owner_login="John-MiracleWorker",
            expected_owner_user_id=58918509,
            label="test repository",
        )
        == 1_155_799_292
    )

    repository["owner"]["id"] = 1  # type: ignore[index]
    with pytest.raises(ValueError, match="repository identity"):
        subject._require_repository_identity(  # noqa: SLF001
            repository,
            expected_repository="John-MiracleWorker/Kestrel",
            expected_repository_id=1_155_799_292,
            expected_owner_login="John-MiracleWorker",
            expected_owner_user_id=58918509,
            label="test repository",
        )


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        (
            "linux",
            "x86_64",
            "sha256:a2c9b8497e1f85b1ad0dfcb78b5a622e098801b8e461e459e88e1ee12f018112",
        ),
        (
            "darwin",
            "arm64",
            "sha256:a58b8fd77b417a38f47a0b54d1370c59b0fcdb324ccc9ca002b0998f7c4c999e",
        ),
    ],
)
def test_workflow_tool_bootstrap_records_the_pinned_archive_digest(
    system: str, machine: str, expected: str
) -> None:
    assert (
        subject._workflow_tools_archive_digest(  # noqa: SLF001
            system=system, machine=machine
        )
        == expected
    )


def test_workflow_tool_bootstrap_rejects_unsupported_platform() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        subject._workflow_tools_archive_digest(  # noqa: SLF001
            system="linux", machine="aarch64"
        )


def test_pinned_github_cli_rejects_same_version_binary_with_wrong_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gh = tmp_path / "gh"
    fake_gh.write_bytes(b"#!/bin/sh\necho 'gh version 2.97.0 (2026-02-26)'\n")
    fake_gh.chmod(0o700)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b"gh version 2.97.0 (2026-02-26)\n",
            stderr=b"",
        )

    monkeypatch.setattr(receipts.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="digest|pin"):
        receipts._verify_pinned_gh(fake_gh)  # noqa: SLF001

    assert calls == []


def test_authorization_external_input_rejects_raw_caller_json(tmp_path: Path) -> None:
    raw_path = tmp_path / "repository.json"
    raw_path.write_bytes(
        _canonical(
            {
                "id": 1_155_799_292,
                "full_name": "John-MiracleWorker/Kestrel",
            }
        )
    )

    with pytest.raises(ValueError, match="source observation"):
        subject._authorization_file(  # noqa: SLF001
            raw_path,
            label="authorization repository observation",
            source_name="repository-observation",
        )


def test_hosted_smoke_prerequisites_accept_pretty_paged_api_observations(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    workflow_path = source_root / ".github" / "workflows" / "release.yml"
    workflow_path.parent.mkdir(parents=True)
    workflow_path.write_text("name: Release\n", encoding="utf-8")
    bootstrap_path = source_root / "scripts" / "bootstrap_workflow_tools.sh"
    bootstrap_path.parent.mkdir()
    bootstrap_path.write_text("#!/bin/sh\n", encoding="utf-8")

    contract_names = {
        "repository": "repository-observation",
        "collaborators": "repository-collaborators-observation",
        "invitations": "repository-invitations-observation",
        "deploy-keys": "deploy-keys-observation",
        "actions": "actions-workflow-permissions-observation",
        "owner-keys": "owner-signing-keys-observation",
        "main": "main-branch-observation",
        "immutable": "immutable-releases-observation",
        "rulesets": "rulesets-observation",
        "tag-ruleset": "tag-ruleset-detail-observation",
        "recovery": "recovery-repository-observation",
    }

    def write_observation(
        name: str,
        value: object,
        *,
        contract_name: str | None = None,
    ) -> Path:
        selected_name = contract_names.get(name, contract_name)
        assert selected_name is not None
        registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
        entry = next(
            item
            for item in registry["entries"]
            if item["receipt_schema"] == receipts.SOURCE_OBSERVATION_SCHEMA
            and item["phase"] == "release-control"
            and item["mode"] is None
            and item["name"] == selected_name
        )
        body_value = value
        if entry["body_mode"] == "paginated-json":
            body_value = {
                "pages": [
                    {
                        "number": 1,
                        "request_url": entry["locator"],
                        "response_headers": [],
                        "body": value,
                    }
                ]
            }
        path = tmp_path / f"{name}.json"
        path.write_bytes(
            _contract_source_envelope(
                receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
                phase="release-control",
                mode=None,
                name=selected_name,
                body=_canonical(body_value),
            )
        )
        return path

    owner_keys = _owner_signing_keys_observation("2026-08-13T20:00:00Z")
    observations = {
        "repository": write_observation(
            "repository",
            {
                "id": 1_155_799_292,
                "full_name": "John-MiracleWorker/Kestrel",
                "owner": {
                    "login": "John-MiracleWorker",
                    "id": 58918509,
                    "type": "User",
                },
            },
        ),
        "collaborators": write_observation(
            "collaborators",
            [
                {
                    "login": "John-MiracleWorker",
                    "id": 58918509,
                    "type": "User",
                    "role_name": "admin",
                }
            ],
        ),
        "invitations": write_observation("invitations", []),
        "deploy-keys": write_observation("deploy-keys", []),
        "actions": write_observation(
            "actions",
            {
                "default_workflow_permissions": "read",
                "can_approve_pull_request_reviews": False,
            },
        ),
        "owner-keys": write_observation(
            "owner-keys",
            json.loads(base64.b64decode(str(owner_keys["body"])))["pages"][0]["body"],
        ),
        "main": write_observation("main", {"commit": {"sha": SOURCE_SHA}}),
        "immutable": write_observation("immutable", {"enabled": True}),
        "rulesets": write_observation(
            "rulesets",
            [
                {
                    "id": 701,
                    "name": "kestrel-release-tags",
                    "enforcement": "active",
                }
            ],
        ),
        "tag-ruleset": write_observation(
            "tag-ruleset",
            {
                "id": 701,
                "name": "kestrel-release-tags",
                "target": "tag",
                "enforcement": "active",
                "bypass_actors": [],
            },
        ),
        "recovery": write_observation(
            "recovery",
            {
                "id": 1_155_800_001,
                "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
            },
        ),
    }
    arguments = [
        "inspect-prerequisites",
        "--mode",
        "hosted-smoke",
        "--repository-observation",
        str(observations["repository"]),
        "--repository-collaborators-observation",
        str(observations["collaborators"]),
        "--repository-invitations-observation",
        str(observations["invitations"]),
        "--deploy-keys-observation",
        str(observations["deploy-keys"]),
        "--actions-workflow-permissions-observation",
        str(observations["actions"]),
        "--owner-signing-keys-observation",
        str(observations["owner-keys"]),
        "--workflow-source-root",
        str(source_root),
        "--main-branch-observation",
        str(observations["main"]),
        "--immutable-releases-observation",
        str(observations["immutable"]),
        "--rulesets-observation",
        str(observations["rulesets"]),
        "--tag-ruleset-detail-observation",
        str(observations["tag-ruleset"]),
        "--recovery-repository-observation",
        str(observations["recovery"]),
        "--expected-repository",
        "John-MiracleWorker/Kestrel",
        "--expected-owner-login",
        "John-MiracleWorker",
        "--expected-owner-user-id",
        "58918509",
    ]
    for index, name in enumerate(
        ("release", "release-prepare", "release-commit", "pypi"), start=901
    ):
        environment_path = write_observation(
            f"{name}-environment",
            _environment_gate_observation(name, index),
            contract_name=f"environment-{name}-observation",
        )
        policies_path = write_observation(
            f"{name}-policies",
            _environment_policy_observation(index),
            contract_name=f"environment-{name}-policies-observation",
        )
        arguments.extend(
            [
                "--environment-observation",
                f"{name}={environment_path}",
                "--environment-policies-observation",
                f"{name}={policies_path}",
            ]
        )
    output = tmp_path / "release-prerequisites.json"
    arguments.extend(["--output", str(output)])

    assert subject.main(arguments) == 0
    record = json.loads(output.read_bytes())
    assert record["repository"]["id"] == 1_155_799_292
    assert record["validation_status"] == "validated_for_hosted_smoke"
    assert len(record["environments"]) == 4
    assert record["tool_bootstrap"] == [
        {
            "name": "gh",
            "version": "2.97.0",
            "sha256": subject._workflow_tools_archive_digest(),  # noqa: SLF001
        }
    ]


@pytest.mark.parametrize(
    ("vector_name", "schema"),
    [
        ("release-preparation-outcome", "kestrel.release_preparation_outcome.v2"),
        ("release-commit-outcome", "kestrel.release_commit_outcome.v2"),
        (
            "release-github-ghcr-verification",
            "kestrel.release_github_ghcr_verification.v2",
        ),
        ("release-pypi-outcome", "kestrel.release_pypi_outcome.v2"),
    ],
)
def test_build_release_stage_record_replays_validated_inputs(vector_name: str, schema: str) -> None:
    raw, _ = _contract_vector(vector_name)
    value = json.loads(raw)

    record = subject.build_release_stage_record(
        schema=schema,
        candidate=value["candidate"],
        transaction_authorization_digest=value["transaction_authorization_digest"],
        execution_authorization_digest=value["execution_authorization_digest"],
        recovery_capsule_digest=value["recovery_capsule_digest"],
        previous_record_digest=value["previous_record_digest"],
        observations_before=value.get("observations_before"),
        observations_after=value.get("observations_after"),
        attempted_operations=value.get("attempted_operations"),
        fresh_observations=value.get("fresh_observations"),
        verification_results=value.get("verification_results"),
        commit_authority_digest=value.get("commit_authority_digest"),
        completed=value["completed"],
        uncertain=value.get("uncertain"),
        pending=value.get("pending"),
        source_records={"stage-input": b"{}"},
    )

    assert record["schema"] == schema
    assert record["stage"] == value["stage"]
    assert subject.validate_release_stage_record(record) == record


def test_authority_consumer_rejects_digest_only_verification_claim() -> None:
    receipt, signature = _contract_vector("github-authority")
    assert signature is not None
    forged_verification = {
        "schema": "kestrel.github_release_authority_verification.v1",
        "authority_schema": receipts.GITHUB_AUTHORITY_SCHEMA,
        "authority": json.loads(receipt),
        "receipt_digest": _sha256(receipt),
        "signature_digest": _sha256(signature),
        "signing_key_fingerprint": (
            "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"
        ),
        "verified_at": "2026-08-13T20:01:00Z",
        "validation_status": "validated",
    }

    with pytest.raises(ValueError, match="fields|signature|proof|bytes"):
        subject._verified_authority_from_record(  # noqa: SLF001
            forged_verification,
            verification_schema="kestrel.github_release_authority_verification.v1",
            authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
            label="forged authority verification",
        )


def test_operational_boundary_joins_live_state_to_zero_app_signed_authority() -> None:
    raw, _signature = _contract_vector("github-authority")
    authority = json.loads(raw)
    workflow_bytes = b"name: exact release ingress\n"
    workflow_digest = _sha256(workflow_bytes)
    authority["workflow_ingress"]["default_branch_blob_sha256"] = workflow_digest
    authority["workflow_ingress"]["candidate_blob_sha256"] = workflow_digest
    snapshot = authority["source_snapshots"][0]
    installed_snapshot = {**snapshot, "name": "installed-apps-owner"}
    authority["source_snapshots"] = sorted(
        [snapshot, installed_snapshot], key=lambda item: item["name"]
    )
    fingerprint = "sha256:" + "f" * 64

    subject._require_operational_github_authority_join(  # noqa: SLF001
        github_authority=authority,
        github_verification={"signing_key_fingerprint": fingerprint},
        live_owner_signing_fingerprint=fingerprint,
        live_owner_keys_observation=_canonical(
            {"captured_at": "2026-08-13T20:01:00Z"}
        ),
        tag_ruleset=authority["tag_ruleset"],
        ingress_ruleset=authority["ingress_ruleset"],
        workflow={
            "id": 404,
            "path": ".github/workflows/release.yml",
            "state": "active",
        },
        default_workflow=workflow_bytes,
        candidate_workflow=workflow_bytes,
        main_sha=SOURCE_SHA,
        immutable_releases=True,
        transaction_mode="initiate",
        _clock=lambda: datetime(2026, 8, 13, 20, 1, tzinfo=UTC),
    )


def test_operational_boundary_rejects_controller_key_rotation() -> None:
    raw, _signature = _contract_vector("github-authority")
    authority = json.loads(raw)
    snapshot = authority["source_snapshots"][0]
    authority["source_snapshots"] = sorted(
        [snapshot, {**snapshot, "name": "installed-apps-owner"}],
        key=lambda item: item["name"],
    )

    with pytest.raises(ValueError, match="signing key"):
        subject._require_operational_github_authority_join(  # noqa: SLF001
            github_authority=authority,
            github_verification={"signing_key_fingerprint": "sha256:" + "f" * 64},
            live_owner_signing_fingerprint="sha256:" + "e" * 64,
            live_owner_keys_observation=_canonical(
                {"captured_at": "2026-08-13T20:01:00Z"}
            ),
            tag_ruleset=authority["tag_ruleset"],
            ingress_ruleset=authority["ingress_ruleset"],
            workflow={
                "id": 404,
                "path": ".github/workflows/release.yml",
                "state": "active",
            },
            default_workflow=b"irrelevant",
            candidate_workflow=b"irrelevant",
            main_sha=SOURCE_SHA,
            immutable_releases=True,
            transaction_mode="initiate",
            _clock=lambda: datetime(2026, 8, 13, 20, 1, tzinfo=UTC),
        )


@pytest.mark.parametrize("stage", [1, 2])
def test_build_release_stage_plan_has_only_fixed_operations(stage: int) -> None:
    vector_name = "release-preparation-outcome" if stage == 1 else "release-commit-outcome"
    raw, _ = _contract_vector(vector_name)
    value = json.loads(raw)
    operations = [item["operation"] for item in value["attempted_operations"]]
    observation = {
        "schema": "kestrel.release_stage_state.v1",
        "stage": stage,
        "operations": [{"operation": name, "state": "missing"} for name in operations],
        "complete": True,
    }

    plan = subject.build_release_stage_plan(
        stage=stage,
        candidate=value["candidate"],
        transaction_authorization_digest=value["transaction_authorization_digest"],
        execution_authorization_digest=value["execution_authorization_digest"],
        recovery_capsule_digest=value["recovery_capsule_digest"],
        previous_record_digest=value["previous_record_digest"],
        commit_authority_digest=value.get("commit_authority_digest"),
        state_observation=observation,
        state_observation_raw=_canonical(observation),
    )

    assert [item["operation"] for item in plan["operations"]] == operations
    assert {item["action"] for item in plan["operations"]} == {"create"}


def test_build_release_stage_plan_rejects_conflicting_remote_state() -> None:
    raw, _ = _contract_vector("release-preparation-outcome")
    value = json.loads(raw)
    observation = {
        "schema": "kestrel.release_stage_state.v1",
        "stage": 1,
        "operations": [
            {"operation": item["operation"], "state": "conflict"}
            for item in value["attempted_operations"]
        ],
        "complete": True,
    }

    with pytest.raises(ValueError, match="conflict"):
        subject.build_release_stage_plan(
            stage=1,
            candidate=value["candidate"],
            transaction_authorization_digest=value["transaction_authorization_digest"],
            execution_authorization_digest=None,
            recovery_capsule_digest=value["recovery_capsule_digest"],
            previous_record_digest=None,
            commit_authority_digest=None,
            state_observation=observation,
            state_observation_raw=_canonical(observation),
        )


def test_product_release_state_is_derived_from_complete_api_listing() -> None:
    manifest = _candidate_manifest()
    transaction = "sha256:" + "1" * 64
    capsule = "sha256:" + "2" * 64
    contract = subject._candidate_product_release_contract(  # noqa: SLF001
        manifest,
        transaction_authorization_digest=transaction,
        recovery_capsule_digest=capsule,
    )

    assert contract["create_request"] == {
        "tag_name": "v0.6.0",
        "target_commitish": SOURCE_SHA,
        "name": "Kestrel v0.6.0",
        "body": "\n".join(
            (
                "v0.6.0",
                "",
                f"Kestrel-Release-Candidate: {subject.candidates.candidate_manifest_digest(manifest)}",
                f"Kestrel-Artifact-Set: {manifest['artifact_set_digest']}",
                f"Kestrel-Source-SHA: {SOURCE_SHA}",
                f"Kestrel-Transaction-Authorization: {transaction}",
                f"Kestrel-Recovery-Capsule: {capsule}",
            )
        ),
        "draft": True,
        "prerelease": False,
        "generate_release_notes": False,
        "make_latest": "false",
    }
    serialized_create = receipts.canonical_external_json_bytes(contract["create_request"])
    assert json.loads(serialized_create)["make_latest"] == "false"
    legacy_boolean_create = {
        **contract["create_request"],  # type: ignore[dict-item]
        "make_latest": False,
    }
    assert receipts._sha256(serialized_create) != receipts._sha256(  # noqa: SLF001
        receipts.canonical_external_json_bytes(legacy_boolean_create)
    )
    assert subject._classify_product_release_listing(  # noqa: SLF001
        [[]], contract=contract
    ) == {
        "release": "missing",
        "assets": "missing",
        "release_id": None,
    }

    asset = contract["assets"][0]  # type: ignore[index]
    persisted = contract["persisted"]  # type: ignore[assignment]
    draft = {
        "id": 91,
        **persisted,
        "draft": True,
        "immutable": False,
        "assets": [
            {
                "id": 92,
                "name": asset["name"],
                "size": asset["size_bytes"],
                "digest": asset["sha256"],
                "content_type": asset["media_type"],
            }
        ],
    }
    assert subject._classify_product_release_listing(  # noqa: SLF001
        [[draft]], contract=contract
    ) == {
        "release": "draft_exact",
        "assets": "existing_exact",
        "release_id": 91,
    }


def test_product_release_publish_patch_serializes_string_enum_into_request_digest() -> None:
    patch = subject._product_release_publish_patch()  # noqa: SLF001
    raw = receipts.canonical_external_json_bytes(patch)

    assert raw == b'{"draft":false,"make_latest":"false"}'
    assert receipts._sha256(raw) != receipts._sha256(  # noqa: SLF001
        receipts.canonical_external_json_bytes(
            {"draft": False, "make_latest": False}
        )
    )


def test_missing_only_release_asset_request_excludes_preexisting_exact_asset() -> None:
    manifest = _candidate_manifest()
    contract = subject._candidate_product_release_contract(  # noqa: SLF001
        manifest,
        transaction_authorization_digest="sha256:" + "1" * 64,
        recovery_capsule_digest="sha256:" + "2" * 64,
    )
    first = contract["assets"][0]  # type: ignore[index]
    second = {
        "name": "second.txt",
        "path": "release/second.txt",
        "media_type": "text/plain",
        "sha256": "sha256:" + "e" * 64,
        "size_bytes": 2,
    }
    contract["assets"].append(second)  # type: ignore[union-attr]
    release = {
        "id": 91,
        **contract["persisted"],  # type: ignore[dict-item]
        "draft": True,
        "immutable": False,
        "assets": [
            {
                "id": 92,
                "name": first["name"],
                "size": first["size_bytes"],
                "digest": first["sha256"],
                "content_type": first["media_type"],
            }
        ],
    }

    assert subject._missing_product_release_assets(  # noqa: SLF001
        [[release]], contract=contract
    ) == [second]


def test_missing_draft_upload_request_binds_created_id_to_exact_create_request() -> None:
    manifest = _candidate_manifest()
    transaction_digest = "sha256:" + "1" * 64
    capsule_digest = "sha256:" + "2" * 64
    candidate = {"tag": manifest["tag"]}
    contract = subject._candidate_product_release_contract(  # noqa: SLF001
        manifest,
        transaction_authorization_digest=transaction_digest,
        recovery_capsule_digest=capsule_digest,
    )
    create_request = contract["create_request"]
    create_digest = subject.release_stage_operation_request_digest(
        candidate=candidate,
        operation="create_github_release_draft",
        request=create_request,  # type: ignore[arg-type]
        transaction_authorization_digest=transaction_digest,
        recovery_capsule_digest=capsule_digest,
    )
    missing_state = {"release": "missing", "assets": "missing", "release_id": None}
    request = subject._product_release_asset_upload_request(  # noqa: SLF001
        contract=contract,
        release_state=missing_state,
        missing_assets=contract["assets"],  # type: ignore[arg-type]
        create_operation_request_digest=create_digest,
    )

    assert request["release_locator"] == {
        "strategy": "created_response_and_exact_relist",
        "tag_name": "v0.6.0",
        "release_id": None,
        "create_operation_request_digest": create_digest,
    }
    resolved_state = {
        "release": "draft_exact",
        "assets": "missing",
        "release_id": 91,
    }
    assert (
        subject._resolve_product_release_asset_upload_target(  # noqa: SLF001
            request,
            release_state=resolved_state,
            created_release_id=91,
            create_operation_request_digest=create_digest,
        )
        == 91
    )

    with pytest.raises(ValueError, match="not authorized"):
        subject._resolve_product_release_asset_upload_target(  # noqa: SLF001
            request,
            release_state=resolved_state,
            created_release_id=92,
            create_operation_request_digest=create_digest,
        )


def test_existing_draft_upload_request_cannot_switch_release_ids() -> None:
    manifest = _candidate_manifest()
    contract = subject._candidate_product_release_contract(  # noqa: SLF001
        manifest,
        transaction_authorization_digest="sha256:" + "1" * 64,
        recovery_capsule_digest="sha256:" + "2" * 64,
    )
    state = {"release": "draft_exact", "assets": "missing", "release_id": 91}
    request = subject._product_release_asset_upload_request(  # noqa: SLF001
        contract=contract,
        release_state=state,
        missing_assets=contract["assets"],  # type: ignore[arg-type]
        create_operation_request_digest="sha256:" + "3" * 64,
    )

    assert request["release_locator"] == {
        "strategy": "preobserved_exact_release_id",
        "tag_name": "v0.6.0",
        "release_id": 91,
        "create_operation_request_digest": None,
    }
    assert (
        subject._resolve_product_release_asset_upload_target(  # noqa: SLF001
            request,
            release_state=state,
            created_release_id=None,
            create_operation_request_digest="sha256:" + "3" * 64,
        )
        == 91
    )

    changed = {**state, "release_id": 92}
    with pytest.raises(ValueError, match="changed before mutation"):
        subject._resolve_product_release_asset_upload_target(  # noqa: SLF001
            request,
            release_state=changed,
            created_release_id=None,
            create_operation_request_digest="sha256:" + "3" * 64,
        )


@pytest.mark.parametrize(
    "mutation",
    (
        "duplicate",
        "mutable",
        "immutable-partial-assets",
        "asset-digest",
        "asset-media-type",
        "extra-asset",
        "flat-pagination",
    ),
)
def test_product_release_state_rejects_ambiguous_or_divergent_remote_state(
    mutation: str,
) -> None:
    manifest = _candidate_manifest()
    contract = subject._candidate_product_release_contract(  # noqa: SLF001
        manifest,
        transaction_authorization_digest="sha256:" + "1" * 64,
        recovery_capsule_digest="sha256:" + "2" * 64,
    )
    asset = contract["assets"][0]  # type: ignore[index]
    release = {
        "id": 91,
        **contract["persisted"],  # type: ignore[dict-item]
        "draft": True,
        "immutable": False,
        "assets": [
            {
                "id": 92,
                "name": asset["name"],
                "size": asset["size_bytes"],
                "digest": asset["sha256"],
                "content_type": asset["media_type"],
            }
        ],
    }
    listing: object = [[release]]
    if mutation == "duplicate":
        listing = [[release], [{**release, "id": 93}]]
    elif mutation == "mutable":
        release["draft"] = False
    elif mutation == "immutable-partial-assets":
        release["draft"] = False
        release["immutable"] = True
        release["assets"] = []
    elif mutation == "asset-digest":
        release["assets"][0]["digest"] = "sha256:" + "f" * 64  # type: ignore[index]
    elif mutation == "asset-media-type":
        release["assets"][0]["content_type"] = "application/octet-stream"  # type: ignore[index]
    elif mutation == "extra-asset":
        release["assets"].append(  # type: ignore[union-attr]
            {
                "id": 94,
                "name": "unexpected.txt",
                "size": 1,
                "digest": "sha256:" + "f" * 64,
                "content_type": "text/plain",
            }
        )
    else:
        listing = [release]

    with pytest.raises(ValueError, match="Release|pagination|asset"):
        subject._classify_product_release_listing(  # noqa: SLF001
            listing, contract=contract
        )


def test_ghcr_package_versions_bind_complete_tag_inventory_to_digests() -> None:
    first = "sha256:" + "a" * 64
    second = "sha256:" + "b" * 64
    pages = [
        [
            {
                "id": 11,
                "name": first,
                "metadata": {
                    "package_type": "container",
                    "container": {"tags": ["v0.5.9", "stable"]},
                },
            }
        ],
        [
            {
                "id": 12,
                "name": second,
                "metadata": {
                    "package_type": "container",
                    "container": {"tags": []},
                },
            }
        ],
    ]

    assert subject._ghcr_tags_by_digest_from_package_versions(pages) == {  # noqa: SLF001
        first: ["stable", "v0.5.9"],
        second: [],
    }


def test_ghcr_package_presence_comes_from_the_complete_user_inventory() -> None:
    absent = [[]]
    present = [
        [
            {
                "id": 71,
                "name": "unrelated",
                "package_type": "container",
                "owner": {"login": "John-MiracleWorker", "id": 58918509},
            },
            {
                "id": 72,
                "name": "kestrel",
                "package_type": "container",
                "owner": {"login": "John-MiracleWorker", "id": 58918509},
            },
        ]
    ]

    assert not subject._ghcr_package_is_present(absent)  # noqa: SLF001
    assert subject._ghcr_package_is_present(present)  # noqa: SLF001

    duplicate = copy.deepcopy(present)
    duplicate[0].append(copy.deepcopy(duplicate[0][1]))
    with pytest.raises(ValueError, match="duplicated"):
        subject._ghcr_package_is_present(duplicate)  # noqa: SLF001

    wrong_owner = copy.deepcopy(present)
    wrong_owner[0][1]["owner"]["id"] = 7
    with pytest.raises(ValueError, match="owner"):
        subject._ghcr_package_is_present(wrong_owner)  # noqa: SLF001


def test_repository_attestation_inventory_proves_exact_absence_or_presence() -> None:
    assert subject._repository_attestation_inventory_count(  # noqa: SLF001
        [{"attestations": []}], expected_repository_id=303
    ) == 0
    inventory = [
        {
            "attestations": [
                {
                    "repository_id": 303,
                    "bundle_url": "https://example.invalid/attestation-1",
                    "initiator": "user",
                }
            ]
        }
    ]
    assert subject._repository_attestation_inventory_count(  # noqa: SLF001
        inventory, expected_repository_id=303
    ) == 1

    wrong_repository = copy.deepcopy(inventory)
    wrong_repository[0]["attestations"][0]["repository_id"] = 404
    with pytest.raises(ValueError, match="repository"):
        subject._repository_attestation_inventory_count(  # noqa: SLF001
            wrong_repository, expected_repository_id=303
        )

    duplicate = [
        {
            "attestations": [
                copy.deepcopy(inventory[0]["attestations"][0]),
                copy.deepcopy(inventory[0]["attestations"][0]),
            ]
        }
    ]
    with pytest.raises(ValueError, match="duplicated"):
        subject._repository_attestation_inventory_count(  # noqa: SLF001
            duplicate, expected_repository_id=303
        )


def test_attestation_verification_error_cannot_be_reclassified_as_absence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inventory = [
        {
            "attestations": [
                {
                    "repository_id": 303,
                    "bundle_url": "https://example.invalid/attestation-1",
                    "initiator": "user",
                }
            ]
        }
    ]
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(arguments)
        if arguments[1] == "api":
            return subprocess.CompletedProcess(arguments, 0, _canonical(inventory), b"")
        return subprocess.CompletedProcess(arguments, 1, b"", b"verification failed")

    monkeypatch.setattr(subject.subprocess, "run", fake_run)
    attestation_subject = _candidate_manifest()["attestation_subjects"][0]  # type: ignore[index]

    with pytest.raises(ValueError, match="verification failed"):
        subject._observe_promotion_attestation_subject(  # noqa: SLF001
            subject=attestation_subject,
            target=str(tmp_path / "release.whl"),
            pinned_gh=tmp_path / "gh",
            repository="John-MiracleWorker/Kestrel",
            expected_repository_id=303,
            common_arguments=("--format", "json"),
            token="token",
            label="commit attestation",
        )

    assert [call[1] for call in calls] == ["api", "attestation"]


def test_remote_digest_stream_is_bounded_and_incremental() -> None:
    payload = b"abcdefghij"
    assert subject._sha256_stream(io.BytesIO(payload), max_bytes=len(payload), chunk_bytes=3) == (  # noqa: SLF001
        "sha256:" + hashlib.sha256(payload).hexdigest(),
        len(payload),
    )

    with pytest.raises(ValueError, match="stream exceeds"):
        subject._sha256_stream(  # noqa: SLF001
            io.BytesIO(payload), max_bytes=len(payload) - 1, chunk_bytes=3
        )


@pytest.mark.parametrize(
    "mutation",
    ["flat", "empty-pages", "duplicate-id", "duplicate-digest", "duplicate-tag", "wrong-type"],
)
def test_ghcr_package_version_tag_inventory_fails_closed(mutation: str) -> None:
    digest = "sha256:" + "a" * 64
    version = {
        "id": 11,
        "name": digest,
        "metadata": {
            "package_type": "container",
            "container": {"tags": ["v0.6.0"]},
        },
    }
    pages: object = [[version]]
    if mutation == "flat":
        pages = [version]
    elif mutation == "empty-pages":
        pages = []
    elif mutation == "duplicate-id":
        pages = [[version, {**version, "name": "sha256:" + "b" * 64}]]
    elif mutation == "duplicate-digest":
        pages = [[version, {**version, "id": 12}]]
    elif mutation == "duplicate-tag":
        pages = [
            [
                version,
                {
                    **version,
                    "id": 12,
                    "name": "sha256:" + "b" * 64,
                },
            ]
        ]
    elif mutation == "wrong-type":
        pages = [[{**version, "metadata": {"package_type": "npm"}}]]

    with pytest.raises(ValueError, match="GHCR package version"):
        subject._ghcr_tags_by_digest_from_package_versions(pages)  # noqa: SLF001


def test_ghcr_state_is_derived_from_exact_digest_queries_without_tags() -> None:
    expected = ("sha256:" + "a" * 64, "sha256:" + "b" * 64)
    observation = {
        "repository": subject.candidates.OCI_REPOSITORY,
        "package_present": True,
        "objects": [
            {
                "digest": digest,
                "http_status": 200,
                "tags": [],
                "tag_inventory_complete": True,
                "observed_at": "2026-08-13T20:00:00Z",
            }
            for digest in expected
        ],
    }
    assert (
        subject._classify_ghcr_digest_observation(  # noqa: SLF001
            observation, expected_digests=expected
        )
        == "existing_exact"
    )

    observation["objects"][0]["http_status"] = 404
    assert (
        subject._classify_ghcr_digest_observation(  # noqa: SLF001
            observation, expected_digests=expected
        )
        == "missing"
    )
    assert subject._missing_ghcr_object_digests(  # noqa: SLF001
        observation, expected_digests=expected
    ) == [expected[0]]

    observation["objects"][0]["tags"] = ["v0.6.0"]
    with pytest.raises(ValueError, match="tag"):
        subject._classify_ghcr_digest_observation(  # noqa: SLF001
            observation, expected_digests=expected
        )


def test_ghcr_first_publication_requires_package_visibility_before_exact_presence() -> None:
    expected = ("sha256:" + "a" * 64,)
    observation = {
        "repository": subject.candidates.OCI_REPOSITORY,
        "package_present": False,
        "objects": [
            {
                "digest": expected[0],
                "http_status": 404,
                "tags": [],
                "tag_inventory_complete": True,
                "observed_at": "2026-08-13T20:00:00Z",
            }
        ],
    }
    assert (
        subject._classify_ghcr_digest_observation(  # noqa: SLF001
            observation, expected_digests=expected
        )
        == "missing"
    )

    observation["objects"][0]["http_status"] = 200
    with pytest.raises(ValueError, match="converg"):
        subject._classify_ghcr_digest_observation(  # noqa: SLF001
            observation, expected_digests=expected
        )

    observation["package_present"] = True
    observation["objects"][0]["tag_inventory_complete"] = False
    with pytest.raises(ValueError, match="converg"):
        subject._classify_ghcr_digest_observation(  # noqa: SLF001
            observation, expected_digests=expected
        )


def test_ghcr_first_publication_waits_for_package_and_digest_convergence() -> None:
    expected = ("sha256:" + "a" * 64,)
    split_view = {
        "repository": subject.candidates.OCI_REPOSITORY,
        "package_present": False,
        "objects": [
            {
                "digest": expected[0],
                "http_status": 200,
                "tags": [],
                "tag_inventory_complete": False,
                "observed_at": "2026-08-13T20:00:00Z",
            }
        ],
    }
    version_lag = copy.deepcopy(split_view)
    version_lag["package_present"] = True
    converged = copy.deepcopy(version_lag)
    converged["objects"][0]["tag_inventory_complete"] = True
    observations = iter((split_view, version_lag, converged))
    clocks = iter((100.0, 101.0, 102.0, 103.0, 104.0, 105.0))
    sleeps: list[float] = []

    result = subject.wait_for_ghcr_digest_convergence(
        observe=lambda: next(observations),
        expected_digests=expected,
        timeout_seconds=10.0,
        poll_interval_seconds=2.0,
        _monotonic=lambda: next(clocks),
        _sleep=sleeps.append,
    )

    assert result == converged
    assert result is not converged
    assert sleeps == [2.0, 2.0]


def test_ghcr_first_publication_convergence_has_one_fixed_monotonic_deadline() -> None:
    expected = ("sha256:" + "a" * 64,)
    split_view = {
        "repository": subject.candidates.OCI_REPOSITORY,
        "package_present": False,
        "objects": [
            {
                "digest": expected[0],
                "http_status": 200,
                "tags": [],
                "tag_inventory_complete": False,
                "observed_at": "2026-08-13T20:00:00Z",
            }
        ],
    }
    clocks = iter((100.0, 101.0, 110.0))
    sleeps: list[float] = []
    calls = 0

    def observe() -> object:
        nonlocal calls
        calls += 1
        return copy.deepcopy(split_view)

    with pytest.raises(ValueError, match="convergence deadline"):
        subject.wait_for_ghcr_digest_convergence(
            observe=observe,
            expected_digests=expected,
            timeout_seconds=10.0,
            poll_interval_seconds=2.0,
            _monotonic=lambda: next(clocks),
            _sleep=sleeps.append,
        )

    assert calls == 1
    assert sleeps == [2.0]


def test_ghcr_convergence_rejects_a_late_settled_observation() -> None:
    expected = ("sha256:" + "a" * 64,)
    exact = {
        "repository": subject.candidates.OCI_REPOSITORY,
        "package_present": True,
        "objects": [
            {
                "digest": expected[0],
                "http_status": 200,
                "tags": [],
                "tag_inventory_complete": True,
                "observed_at": "2026-08-13T20:00:00Z",
            }
        ],
    }
    clocks = iter((100.0, 111.0))

    with pytest.raises(ValueError, match="convergence deadline"):
        subject.wait_for_ghcr_digest_convergence(
            observe=lambda: exact,
            expected_digests=expected,
            timeout_seconds=10.0,
            poll_interval_seconds=2.0,
            _monotonic=lambda: next(clocks),
            _sleep=lambda _seconds: None,
        )


def test_ghcr_convergence_does_not_retry_conflicting_state() -> None:
    expected = ("sha256:" + "a" * 64,)
    conflict = {
        "repository": subject.candidates.OCI_REPOSITORY,
        "package_present": False,
        "objects": [
            {
                "digest": expected[0],
                "http_status": 200,
                "tags": ["unexpected"],
                "tag_inventory_complete": False,
                "observed_at": "2026-08-13T20:00:00Z",
            }
        ],
    }
    sleeps: list[float] = []

    with pytest.raises(ValueError, match="tag"):
        subject.wait_for_ghcr_digest_convergence(
            observe=lambda: conflict,
            expected_digests=expected,
            timeout_seconds=10.0,
            poll_interval_seconds=2.0,
            _monotonic=lambda: 100.0,
            _sleep=sleeps.append,
        )

    assert sleeps == []


def test_commit_tag_state_is_derived_from_ref_and_annotated_peel() -> None:
    raw, _ = _contract_vector("release-preparation-outcome")
    stage = json.loads(raw)
    candidate = stage["candidate"]
    transaction = stage["transaction_authorization_digest"]
    capsule = stage["recovery_capsule_digest"]
    missing = {"http_status": 404, "ref": None, "tag": None}
    assert (
        subject._classify_commit_tag_observation(  # noqa: SLF001
            missing,
            candidate=candidate,
            transaction_authorization_digest=transaction,
            recovery_capsule_digest=capsule,
        )
        == "missing"
    )

    tag_sha = "c" * 40
    existing = {
        "http_status": 200,
        "ref": {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "tag", "sha": tag_sha},
        },
        "tag": {
            "sha": tag_sha,
            "tag": "v1.2.3",
            "message": subject.build_annotated_tag_message(
                candidate=candidate,
                transaction_authorization_digest=transaction,
                recovery_capsule_digest=capsule,
            ),
            "object": {"type": "commit", "sha": candidate["source_sha"]},
        },
    }
    assert (
        subject._classify_commit_tag_observation(  # noqa: SLF001
            existing,
            candidate=candidate,
            transaction_authorization_digest=transaction,
            recovery_capsule_digest=capsule,
        )
        == "existing_exact"
    )

    existing["ref"]["object"]["type"] = "commit"
    with pytest.raises(ValueError, match="annotated tag"):
        subject._classify_commit_tag_observation(  # noqa: SLF001
            existing,
            candidate=candidate,
            transaction_authorization_digest=transaction,
            recovery_capsule_digest=capsule,
        )


def _promotion_predicate_fixture(
    manifest: dict[str, object],
    *,
    transaction_digest: str = "sha256:" + "1" * 64,
    capsule_digest: str = "sha256:" + "2" * 64,
) -> tuple[dict[str, object], str, str]:
    contract = subject._candidate_product_release_contract(  # noqa: SLF001
        manifest,
        transaction_authorization_digest=transaction_digest,
        recovery_capsule_digest=capsule_digest,
    )
    asset = contract["assets"][0]  # type: ignore[index]
    release = {
        "id": 91,
        **contract["persisted"],  # type: ignore[dict-item]
        "draft": False,
        "immutable": True,
        "assets": [
            {
                "id": 92,
                "name": asset["name"],
                "size": asset["size_bytes"],
                "digest": asset["sha256"],
                "content_type": asset["media_type"],
            }
        ],
    }
    expected_oci = subject._expected_oci_object_digests_from_manifest(manifest)  # noqa: SLF001
    ghcr = {
        "repository": subject.candidates.OCI_REPOSITORY,
        "package_present": True,
        "objects": [
            {
                "digest": digest,
                "http_status": 200,
                "tags": [],
                "tag_inventory_complete": True,
                "observed_at": "2026-08-13T20:00:00Z",
            }
            for digest in expected_oci
        ],
    }
    context = subject._release_promotion_predicate_context(  # noqa: SLF001
        manifest=manifest,
        transaction_authorization_digest=transaction_digest,
        release_listing=[[release]],
        release_contract=contract,
        ghcr_observation=ghcr,
        expected_oci_digests=expected_oci,
    )
    return context, transaction_digest, capsule_digest


def _promotion_authority_proof(
    tmp_path: Path,
    *,
    context: dict[str, object],
    capsule_digest: str,
    run_id: int = 808,
    execution_digest: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    vector, _signature = _contract_vector("github-authority")
    authority = json.loads(vector)
    candidate = copy.deepcopy(context["candidate"])
    mode = "initiate" if execution_digest is None else "recover_committed"
    authority["phase"] = "commit"
    authority["mode"] = mode
    authority["candidate"] = candidate
    authority["promotion_run"].update(
        {
            "repository_id": context["candidate_build_evidence"]["repository_id"],  # type: ignore[index]
            "run_id": run_id,
            "run_attempt": 1,
            "event": "workflow_dispatch",
            "ref": (
                "refs/heads/main"
                if execution_digest is None
                else f"refs/tags/{candidate['tag']}"
            ),
            "head_sha": candidate["source_sha"],
            "workflow_sha": candidate["source_sha"],
            "workflow_path": ".github/workflows/release.yml",
        }
    )
    authority["environment"] = {"name": "release-commit", "id": 903}
    authority["bindings"] = {
        "transaction_authorization_digest": context[
            "transaction_authorization_digest"
        ],
        "execution_authorization_digest": execution_digest,
        "recovery_capsule_manifest_digest": capsule_digest,
        "commit_marker_digest": None,
    }
    authority["workflow_ingress"]["capsule_blob_sha256"] = authority[
        "workflow_ingress"
    ]["candidate_blob_sha256"]
    receipt = _canonical(receipts.validate_github_authority(authority))
    signature = receipts.sign_receipt_detached(
        receipt=receipt,
        identity_file=_signing_identity(tmp_path),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    proof = receipts.verify_github_authority(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=_canonical(
            _owner_signing_keys_observation("2026-08-13T20:00:00Z")
        ),
        expected_run_id=run_id,
        expected_candidate_digest=candidate["candidate_manifest_digest"],
        expected_environment_id=903,
        _clock=lambda: datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC),
    )
    return proof, authority["promotion_run"]


def _promotion_verification(
    *,
    context: dict[str, object],
    promotion_run: dict[str, object],
    execution_digest: str | None,
    subjects: list[dict[str, object]],
) -> list[dict[str, object]]:
    predicate = subject.build_release_promotion_predicate(
        context=context,
        execution_authorization_digest=execution_digest,
        promotion_run=promotion_run,
    )
    source_sha = context["candidate"]["source_sha"]  # type: ignore[index]
    repository_id = context["candidate_build_evidence"]["repository_id"]  # type: ignore[index]
    ref = promotion_run["ref"]
    workflow_uri = (
        "https://github.com/John-MiracleWorker/Kestrel/"
        f".github/workflows/release.yml@{ref}"
    )
    certificate = {
        "issuer": "https://token.actions.githubusercontent.com",
        "runnerEnvironment": "github-hosted",
        "sourceRepositoryURI": "https://github.com/John-MiracleWorker/Kestrel",
        "sourceRepositoryDigest": source_sha,
        "sourceRepositoryRef": ref,
        "sourceRepositoryIdentifier": str(repository_id),
        "sourceRepositoryOwnerURI": "https://github.com/John-MiracleWorker",
        "sourceRepositoryOwnerIdentifier": "58918509",
        "sourceRepositoryVisibilityAtSigning": "public",
        "buildSignerURI": workflow_uri,
        "buildSignerDigest": source_sha,
        "buildConfigURI": workflow_uri,
        "buildConfigDigest": source_sha,
        "subjectAlternativeName": workflow_uri,
        "githubWorkflowName": "Release",
        "githubWorkflowRepository": "John-MiracleWorker/Kestrel",
        "githubWorkflowRef": ref,
        "githubWorkflowSHA": source_sha,
        "githubWorkflowTrigger": "workflow_dispatch",
        "buildTrigger": "workflow_dispatch",
        "runInvocationURI": (
            "https://github.com/John-MiracleWorker/Kestrel/actions/runs/"
            f"{promotion_run['run_id']}/attempts/1"
        ),
    }
    return [
        {
            "attestation": {},
            "verificationResult": {
                "signature": {"certificate": certificate},
                "verifiedTimestamps": [
                    {
                        "timestamp": "2026-08-13T16:01:00-04:00",
                        "type": "Tlog",
                        "uri": "https://rekor.sigstore.dev",
                    }
                ],
                "statement": {
                    "_type": "https://in-toto.io/Statement/v1",
                    "predicateType": subject._RELEASE_PROMOTION_PREDICATE_TYPE,  # noqa: SLF001
                    "subject": [
                        {
                            "name": item["name"],
                            "digest": {
                                "sha256": str(item["digest"]).removeprefix("sha256:")
                            },
                        }
                        for item in subjects
                    ],
                    "predicate": predicate,
                },
            },
        }
    ]


def _authorized_promotion_observation(
    tmp_path: Path,
    *,
    manifest: dict[str, object],
    execution_digest: str | None = None,
    run_id: int = 808,
) -> tuple[dict[str, object], dict[str, object], str]:
    context, _transaction_digest, capsule_digest = _promotion_predicate_fixture(manifest)
    proof, promotion_run = _promotion_authority_proof(
        tmp_path,
        context=context,
        capsule_digest=capsule_digest,
        run_id=run_id,
        execution_digest=execution_digest,
    )
    subjects = []
    for index, expected in enumerate(  # type: ignore[union-attr]
        manifest["attestation_subjects"]
    ):
        verification = _promotion_verification(
            context=context,
            promotion_run=promotion_run,
            execution_digest=execution_digest,
            subjects=[expected],
        )
        subjects.append(
            {
                **expected,
                "inventory": [
                    {
                        "attestations": [
                            {
                                "repository_id": 303,
                                "bundle_url": (
                                    "https://example.invalid/attestations/"
                                    f"promotion-{index}"
                                ),
                                "initiator": "user",
                            }
                        ]
                    }
                ],
                "verification": verification,
                "authority_verification": copy.deepcopy(proof),
            }
        )
    return {"subjects": subjects}, context, capsule_digest


def test_attestation_state_is_derived_from_predicate_certificate_and_authority(
    tmp_path: Path,
) -> None:
    manifest = _candidate_manifest()
    observation, context, capsule_digest = _authorized_promotion_observation(
        tmp_path, manifest=manifest
    )
    assert subject._classify_promotion_attestation_observation(  # noqa: SLF001
        observation,
        manifest=manifest,
        expected_context=context,
        recovery_capsule_digest=capsule_digest,
    ) == {"file": "existing_exact", "oci_index": "existing_exact"}

    observation["subjects"][0]["verification"] = None  # type: ignore[index]
    observation["subjects"][0]["authority_verification"] = None  # type: ignore[index]
    observation["subjects"][0]["inventory"] = [{"attestations": []}]  # type: ignore[index]
    assert (
        subject._classify_promotion_attestation_observation(  # noqa: SLF001
            observation,
            manifest=manifest,
            expected_context=context,
            recovery_capsule_digest=capsule_digest,
        )["file"]
        == "missing"
    )


def test_attestation_inventory_nonabsence_cannot_be_classified_as_missing(
    tmp_path: Path,
) -> None:
    manifest = _candidate_manifest()
    observation, context, capsule_digest = _authorized_promotion_observation(
        tmp_path, manifest=manifest
    )
    observation["subjects"][0]["verification"] = None  # type: ignore[index]
    observation["subjects"][0]["authority_verification"] = None  # type: ignore[index]

    with pytest.raises(ValueError, match="inventory.*verification|verification.*inventory"):
        subject._classify_promotion_attestation_observation(  # noqa: SLF001
            observation,
            manifest=manifest,
            expected_context=context,
            recovery_capsule_digest=capsule_digest,
        )


def test_attestation_recovery_compares_stable_surface_identity_not_observation_time(
    tmp_path: Path,
) -> None:
    manifest = _candidate_manifest()
    observation, signed_context, capsule_digest = _authorized_promotion_observation(
        tmp_path, manifest=manifest
    )
    current_context = copy.deepcopy(signed_context)
    current_context["published_surfaces"]["ghcr"]["objects"][0][  # type: ignore[index]
        "available_by_digest_at"
    ] = "2026-08-13T20:02:00Z"

    assert subject._classify_promotion_attestation_observation(  # noqa: SLF001
        observation,
        manifest=manifest,
        expected_context=current_context,
        recovery_capsule_digest=capsule_digest,
    ) == {"file": "existing_exact", "oci_index": "existing_exact"}


def test_attestation_request_identity_excludes_only_fresh_availability_time() -> None:
    manifest = _candidate_manifest()
    context, _transaction_digest, _capsule_digest = _promotion_predicate_fixture(
        manifest
    )
    promotion_run = {
        "repository_id": 303,
        "run_id": 808,
        "run_attempt": 1,
        "event": "workflow_dispatch",
        "ref": "refs/heads/main",
        "head_sha": SOURCE_SHA,
        "workflow_sha": SOURCE_SHA,
        "workflow_path": ".github/workflows/release.yml",
    }
    first = subject.build_release_promotion_predicate(
        context=context,
        execution_authorization_digest=None,
        promotion_run=promotion_run,
    )
    fresh_context = copy.deepcopy(context)
    fresh_context["published_surfaces"]["ghcr"]["objects"][0][  # type: ignore[index]
        "available_by_digest_at"
    ] = "2026-08-13T20:00:30Z"
    fresh = subject.build_release_promotion_predicate(
        context=fresh_context,
        execution_authorization_digest=None,
        promotion_run=promotion_run,
    )
    subjects = manifest["attestation_subjects"][:1]  # type: ignore[index]

    assert subject._promotion_attestation_request_identity(  # noqa: SLF001
        predicate=first, subjects=subjects
    ) == subject._promotion_attestation_request_identity(  # noqa: SLF001
        predicate=fresh, subjects=subjects
    )
    assert fresh["published_surfaces"]["ghcr"]["objects"][0][  # type: ignore[index]
        "available_by_digest_at"
    ] == "2026-08-13T20:00:30Z"


@pytest.mark.parametrize(
    "mutation",
    (
        "transaction-authorization",
        "execution-authorization",
        "run-id",
        "run-attempt",
        "run-ref",
        "run-sha",
        "build-receipt",
        "release-field",
        "ghcr-field",
    ),
)
def test_attestation_predicate_and_signer_binding_mutants_fail_closed(
    tmp_path: Path, mutation: str
) -> None:
    manifest = _candidate_manifest()
    observation, context, capsule_digest = _authorized_promotion_observation(
        tmp_path, manifest=manifest
    )
    statement = observation["subjects"][0]["verification"][0]["verificationResult"][  # type: ignore[index]
        "statement"
    ]
    predicate = statement["predicate"]
    if mutation == "transaction-authorization":
        predicate["transaction_authorization_digest"] = "sha256:" + "9" * 64
    elif mutation == "execution-authorization":
        predicate["execution_authorization_digest"] = "sha256:" + "8" * 64
    elif mutation == "run-id":
        predicate["promotion_run"]["run_id"] = 999
    elif mutation == "run-attempt":
        predicate["promotion_run"]["run_attempt"] = 2
    elif mutation == "run-ref":
        predicate["promotion_run"]["ref"] = "refs/tags/v0.6.0"
    elif mutation == "run-sha":
        predicate["promotion_run"]["workflow_sha"] = "c" * 40
    elif mutation == "build-receipt":
        predicate["candidate_build_evidence"]["checks"][0][
            "receipt_sha256"
        ] = "sha256:" + "7" * 64
    elif mutation == "release-field":
        predicate["published_surfaces"]["github_release"]["release_id"] = 999
    else:
        predicate["published_surfaces"]["ghcr"]["objects"][0][
            "available_by_digest"
        ] = False

    with pytest.raises(
        ValueError, match="attestation|predicate|authority|run|release-promotion"
    ):
        subject._classify_promotion_attestation_observation(  # noqa: SLF001
            observation,
            manifest=manifest,
            expected_context=context,
            recovery_capsule_digest=capsule_digest,
        )


def test_recovery_accepts_one_prior_authorized_bundle_from_a_different_attempt_one_run(
    tmp_path: Path,
) -> None:
    manifest = _candidate_manifest()
    old_execution = "sha256:" + "6" * 64
    observation, context, capsule_digest = _authorized_promotion_observation(
        tmp_path,
        manifest=manifest,
        execution_digest=old_execution,
        run_id=809,
    )

    assert subject._classify_promotion_attestation_observation(  # noqa: SLF001
        observation,
        manifest=manifest,
        expected_context=context,
        recovery_capsule_digest=capsule_digest,
    ) == {"file": "existing_exact", "oci_index": "existing_exact"}


def test_attestation_state_accepts_only_bounded_same_kind_subject_batches(
    tmp_path: Path,
) -> None:
    manifest = _candidate_manifest()
    original_file = copy.deepcopy(manifest["attestation_subjects"][0])
    second_file = {
        "kind": "file",
        "name": "release/second.whl",
        "digest": "sha256:" + "e" * 64,
    }
    third_file = {
        "kind": "file",
        "name": "release/third.whl",
        "digest": "sha256:" + "1" * 64,
    }
    oci = copy.deepcopy(manifest["attestation_subjects"][1])
    manifest["attestation_subjects"] = [original_file, second_file, third_file, oci]
    context, _transaction, capsule_digest = _promotion_predicate_fixture(manifest)
    proof, promotion_run = _promotion_authority_proof(
        tmp_path, context=context, capsule_digest=capsule_digest
    )
    file_batch = _promotion_verification(
        context=context,
        promotion_run=promotion_run,
        execution_digest=None,
        subjects=[original_file, second_file, third_file],
    )
    oci_verification = _promotion_verification(
        context=context,
        promotion_run=promotion_run,
        execution_digest=None,
        subjects=[oci],
    )
    observation = {
        "subjects": [
            {
                **item,
                "inventory": [
                    {
                        "attestations": [
                            {
                                "repository_id": 303,
                                "bundle_url": (
                                    "https://example.invalid/attestations/file-batch"
                                ),
                                "initiator": "user",
                            }
                        ]
                    }
                ],
                "verification": file_batch,
                "authority_verification": copy.deepcopy(proof),
            }
            for item in (original_file, second_file, third_file)
        ]
        + [
            {
                **oci,
                "inventory": [
                    {
                        "attestations": [
                            {
                                "repository_id": 303,
                                "bundle_url": (
                                    "https://example.invalid/attestations/oci-index"
                                ),
                                "initiator": "user",
                            }
                        ]
                    }
                ],
                "verification": oci_verification,
                "authority_verification": copy.deepcopy(proof),
            }
        ]
    }
    assert subject._classify_promotion_attestation_observation(  # noqa: SLF001
        observation,
        manifest=manifest,
        expected_context=context,
        recovery_capsule_digest=capsule_digest,
    ) == {"file": "existing_exact", "oci_index": "existing_exact"}

    foreign = copy.deepcopy(observation)
    foreign["subjects"][0]["verification"][0]["verificationResult"]["statement"][  # type: ignore[index]
        "subject"
    ].append({"name": "release/foreign.whl", "digest": {"sha256": "f" * 64}})
    with pytest.raises(ValueError, match="subject identity"):
        subject._classify_promotion_attestation_observation(  # noqa: SLF001
            foreign,
            manifest=manifest,
            expected_context=context,
            recovery_capsule_digest=capsule_digest,
        )


def _pypi_candidate_manifest() -> dict[str, object]:
    manifest = _candidate_manifest()
    artifacts = [
        {
            "path": "release/nested_memvid_agent-0.6.0-py3-none-any.whl",
            "media_type": "application/zip",
            "sha256": "sha256:" + "a" * 64,
            "size_bytes": 101,
        },
        {
            "path": "release/nested_memvid_agent-0.6.0.tar.gz",
            "media_type": "application/gzip",
            "sha256": "sha256:" + "b" * 64,
            "size_bytes": 102,
        },
    ]
    manifest["artifacts"] = artifacts
    manifest["artifact_set_digest"] = receipts._sha256(_canonical(artifacts))  # noqa: SLF001
    return manifest


def _pypi_project_observation(manifest: dict[str, object]) -> dict[str, object]:
    files = subject._candidate_pypi_files(manifest)  # noqa: SLF001
    return {
        "info": {"name": "nested-memvid-agent"},
        "last_serial": 17,
        "releases": {
            "0.6.0": [
                {
                    "filename": filename,
                    "digests": {"sha256": item["sha256"].removeprefix("sha256:")},
                    "size": item["size_bytes"],
                    "url": f"https://files.pythonhosted.org/packages/aa/{filename}",
                    "yanked": False,
                }
                for filename, item in files.items()
            ]
        },
    }


def test_pypi_project_state_is_derived_from_candidate_filenames_and_hashes() -> None:
    manifest = _pypi_candidate_manifest()
    files = subject._candidate_pypi_files(manifest)  # noqa: SLF001
    assert tuple(files) == (
        "nested_memvid_agent-0.6.0-py3-none-any.whl",
        "nested_memvid_agent-0.6.0.tar.gz",
    )
    project = _pypi_project_observation(manifest)
    state = subject._classify_pypi_project_observation(  # noqa: SLF001
        project, version="0.6.0", expected_files=files
    )
    assert state["present"] == list(files)
    assert state["missing"] == []

    project["releases"]["0.6.0"][0]["digests"]["sha256"] = "f" * 64  # type: ignore[index]
    with pytest.raises(ValueError, match="hash"):
        subject._classify_pypi_project_observation(  # noqa: SLF001
            project, version="0.6.0", expected_files=files
        )


def _publish_provenance(filename: str, digest: str) -> dict[str, object]:
    statement = _canonical(
        {
            "_type": "https://in-toto.io/Statement/v1",
            "subject": [
                {
                    "name": filename,
                    "digest": {"sha256": digest.removeprefix("sha256:")},
                }
            ],
            "predicateType": "https://docs.pypi.org/attestations/publish/v1",
            "predicate": None,
        }
    )
    return {
        "version": 1,
        "attestation_bundles": [
            {
                "publisher": {
                    "kind": "GitHub",
                    "repository": "John-MiracleWorker/Kestrel",
                    "workflow": "release.yml",
                    "environment": "pypi",
                    "claims": None,
                },
                "attestations": [
                    {
                        "version": 1,
                        "envelope": {
                            "signature": base64.b64encode(b"signature").decode(),
                            "statement": base64.b64encode(statement).decode(),
                        },
                        "verification_material": {
                            "certificate": "certificate",
                            "transparency_entries": [],
                        },
                    }
                ],
            }
        ],
    }


def test_pypi_integrity_provenance_requires_one_exact_publish_identity() -> None:
    filename = "nested_memvid_agent-0.6.0.tar.gz"
    digest = "sha256:" + "b" * 64
    provenance = _publish_provenance(filename, digest)
    assert subject._verify_pypi_integrity_provenance(  # noqa: SLF001
        provenance, filename=filename, expected_digest=digest
    ) == {
        "kind": "GitHub",
        "repository": "John-MiracleWorker/Kestrel",
        "workflow": "release.yml",
        "environment": "pypi",
    }

    provenance["attestation_bundles"][0]["publisher"]["workflow"] = "other.yml"  # type: ignore[index]
    with pytest.raises(ValueError, match="publisher"):
        subject._verify_pypi_integrity_provenance(  # noqa: SLF001
            provenance, filename=filename, expected_digest=digest
        )

    provenance = _publish_provenance(filename, digest)
    provenance["attestation_bundles"].append(  # type: ignore[union-attr]
        copy.deepcopy(provenance["attestation_bundles"][0])  # type: ignore[index]
    )
    with pytest.raises(ValueError, match="exactly one"):
        subject._verify_pypi_integrity_provenance(  # noqa: SLF001
            provenance, filename=filename, expected_digest=digest
        )


def test_pypi_provenance_evidence_joins_every_file_to_pinned_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files: dict[str, dict[str, object]] = {}
    for filename, raw in (
        ("nested_memvid_agent-0.6.0-py3-none-any.whl", b"wheel bytes"),
        ("nested_memvid_agent-0.6.0.tar.gz", b"sdist bytes"),
    ):
        path = tmp_path / "release" / filename
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(raw)
        files[filename] = {
            "path": f"release/{filename}",
            "sha256": _sha256(raw),
            "size_bytes": len(raw),
        }
    files = dict(sorted(files.items()))
    integrity_files = []
    verification_files = []
    verifier_provenance: list[bytes] = []
    for filename, item in files.items():
        provenance = _publish_provenance(filename, item["sha256"])
        provenance_raw = receipts.canonical_external_json_bytes(provenance) + b"\n"
        integrity_files.append(
            {
                "filename": filename,
                "provenance_response_base64": base64.b64encode(provenance_raw).decode(
                    "ascii"
                ),
            }
        )
        verification_files.append(
            {
                "filename": filename,
                "distribution_sha256": item["sha256"],
                "provenance_sha256": _sha256(provenance_raw),
                "exit_code": 0,
            }
        )
    integrity = {
        "schema": "pypi.integrity_observations.v1",
        "files": integrity_files,
    }
    verification = {
        "schema": "pypi.provenance_verifications.v1",
        "tool": {"name": "pypi-attestations", "version": "0.0.30"},
        "files": verification_files,
    }

    monkeypatch.setattr(
        subject,
        "_run_pypi_attestations_verifier",
        lambda **kwargs: verifier_provenance.append(kwargs["provenance"])
        or b"verified",
    )
    results = subject._validate_pypi_provenance_evidence(  # noqa: SLF001
        integrity_observations=integrity,
        provenance_verifications=verification,
        expected_files=files,
        persisted_filenames=list(files),
        distribution_root=tmp_path,
    )
    assert [item["filename"] for item in results] == list(files)
    assert verifier_provenance == [
        receipts.canonical_external_json_bytes(
            _publish_provenance(filename, files[filename]["sha256"])
        )
        + b"\n"
        for filename in files
    ]

    verification["tool"]["version"] = "0.0.29"
    with pytest.raises(ValueError, match="verifier version"):
        subject._validate_pypi_provenance_evidence(  # noqa: SLF001
            integrity_observations=integrity,
            provenance_verifications=verification,
            expected_files=files,
            persisted_filenames=list(files),
            distribution_root=tmp_path,
        )


def test_pypi_provenance_rejects_forged_success_without_running_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filename = "nested_memvid_agent-0.6.0.tar.gz"
    distribution = b"candidate distribution bytes"
    distribution_path = tmp_path / "release" / filename
    distribution_path.parent.mkdir()
    distribution_path.write_bytes(distribution)
    digest = _sha256(distribution)
    provenance = _publish_provenance(filename, digest)
    provenance_raw = receipts.canonical_external_json_bytes(provenance)
    integrity = {
        "schema": "pypi.integrity_observations.v1",
        "files": [
            {
                "filename": filename,
                "provenance_response_base64": base64.b64encode(
                    provenance_raw
                ).decode("ascii"),
            }
        ],
    }
    claimed = {
        "schema": "pypi.provenance_verifications.v1",
        "tool": {"name": "pypi-attestations", "version": "0.0.30"},
        "files": [
            {
                "filename": filename,
                "distribution_sha256": digest,
                "provenance_sha256": _sha256(provenance_raw),
                "exit_code": 0,
            }
        ],
    }

    def reject_forged_provenance(**_: object) -> bytes:
        raise RuntimeError("cryptographic verifier rejected forged provenance")

    monkeypatch.setattr(
        subject,
        "_run_pypi_attestations_verifier",
        reject_forged_provenance,
        raising=False,
    )

    with pytest.raises(RuntimeError, match="cryptographic verifier rejected"):
        subject._validate_pypi_provenance_evidence(  # noqa: SLF001
            integrity_observations=integrity,
            provenance_verifications=claimed,
            expected_files={
                filename: {
                    "path": f"release/{filename}",
                    "sha256": digest,
                    "size_bytes": len(distribution),
                }
            },
            persisted_filenames=[filename],
            distribution_root=tmp_path,
        )


def test_validated_stage_plan_rejects_unknown_action() -> None:
    raw, _ = _contract_vector("release-preparation-outcome")
    value = json.loads(raw)
    observation = {
        "schema": "kestrel.release_stage_state.v1",
        "stage": 1,
        "operations": [
            {"operation": item["operation"], "state": "missing"}
            for item in value["attempted_operations"]
        ],
        "complete": True,
    }
    plan = subject.build_release_stage_plan(
        stage=1,
        candidate=value["candidate"],
        transaction_authorization_digest=value["transaction_authorization_digest"],
        execution_authorization_digest=None,
        recovery_capsule_digest=value["recovery_capsule_digest"],
        previous_record_digest=None,
        commit_authority_digest=None,
        state_observation=observation,
        state_observation_raw=_canonical(observation),
    )
    plan["operations"][0]["action"] = "overwrite"  # type: ignore[index]

    with pytest.raises(ValueError, match="plan action"):
        subject._validated_stage_plan(plan, stage=1)  # noqa: SLF001


def test_record_stage_binds_execution_outcomes_to_plan_requests(
    tmp_path: Path,
) -> None:
    raw, _ = _contract_vector("release-preparation-outcome")
    value = json.loads(raw)
    state = {
        "schema": "kestrel.release_stage_state.v1",
        "stage": 1,
        "operations": [
            {"operation": item["operation"], "state": "missing"}
            for item in value["attempted_operations"]
        ],
        "complete": True,
    }
    plan = subject.build_release_stage_plan(
        stage=1,
        candidate=value["candidate"],
        transaction_authorization_digest=value["transaction_authorization_digest"],
        execution_authorization_digest=None,
        recovery_capsule_digest=value["recovery_capsule_digest"],
        previous_record_digest=None,
        commit_authority_digest=None,
        state_observation=state,
        state_observation_raw=_canonical(state),
    )
    pre = {
        "schema": "kestrel.release_stage_observations.v1",
        "stage": 1,
        "observations": value["observations_before"],
    }
    post = {
        "schema": "kestrel.release_stage_execution.v1",
        "stage": 1,
        "observations": value["observations_after"],
        "operation_outcomes": [
            {
                **outcome,
                "request_digest": planned["request_digest"],
            }
            for outcome, planned in zip(
                value["attempted_operations"], plan["operations"], strict=True
            )
        ],
        "completed": True,
        "uncertain": False,
        "pending": False,
    }
    plan_path = tmp_path / "plan.json"
    pre_path = tmp_path / "pre.json"
    post_path = tmp_path / "post.json"
    output = tmp_path / "outcome.json"
    plan_path.write_bytes(_canonical(plan))
    pre_path.write_bytes(_canonical(pre))
    post_path.write_bytes(_canonical(post))

    assert (
        subject.main(
            [
                "record-preparation",
                str(plan_path),
                "--pre-observations",
                str(pre_path),
                "--post-observations",
                str(post_path),
                "--output",
                str(output),
            ]
        )
        == 0
    )

    output.unlink()
    post["operation_outcomes"][0]["request_digest"] = "sha256:" + "9" * 64  # type: ignore[index]
    post_path.write_bytes(_canonical(post))
    assert (
        subject.main(
            [
                "record-preparation",
                str(plan_path),
                "--pre-observations",
                str(pre_path),
                "--post-observations",
                str(post_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()

    post["operation_outcomes"][0]["request_digest"] = plan["operations"][0][  # type: ignore[index]
        "request_digest"
    ]
    post["pending"] = True
    post_path.write_bytes(_canonical(post))
    assert (
        subject.main(
            [
                "record-preparation",
                str(plan_path),
                "--pre-observations",
                str(pre_path),
                "--post-observations",
                str(post_path),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert not output.exists()


def test_release_stage_clis_are_exposed() -> None:
    help_text = subject._parser().format_help()  # noqa: SLF001
    for command in (
        "tag-message",
        "plan-preparation",
        "record-preparation",
        "plan-commit",
        "record-commit",
        "verify-github-ghcr",
        "record-pypi",
        "reconcile",
    ):
        assert command in help_text


def test_annotated_tag_message_is_the_exact_transaction_commit_marker() -> None:
    raw, _ = _contract_vector("release-preparation-outcome")
    stage = json.loads(raw)
    candidate = stage["candidate"]

    message = subject.build_annotated_tag_message(
        candidate=candidate,
        transaction_authorization_digest=stage["transaction_authorization_digest"],
        recovery_capsule_digest=stage["recovery_capsule_digest"],
    )

    assert message == "\n".join(
        (
            "Kestrel release v1.2.3",
            "",
            "Kestrel-Release-Candidate: sha256:" + "0" * 64,
            "Kestrel-Artifact-Set: sha256:" + "1" * 64,
            "Kestrel-Transaction-Authorization: sha256:" + "1" * 64,
            "Kestrel-Recovery-Capsule: sha256:" + "2" * 64,
        )
    )
    assert "Execution" not in message


@pytest.mark.parametrize(
    "mutation",
    [None, "lightweight", "target", "message", "mutable-release"],
)
def test_recovery_commit_marker_requires_annotated_peel_and_committed_release(
    mutation: str | None,
) -> None:
    raw, _ = _contract_vector("release-preparation-outcome")
    stage = json.loads(raw)
    candidate = stage["candidate"]
    transaction = stage["transaction_authorization_digest"]
    capsule = stage["recovery_capsule_digest"]
    tag_sha = "c" * 40
    observation = {
        "ref": {
            "ref": "refs/tags/v1.2.3",
            "object": {"type": "tag", "sha": tag_sha},
        },
        "tag": {
            "sha": tag_sha,
            "tag": "v1.2.3",
            "message": subject.build_annotated_tag_message(
                candidate=candidate,
                transaction_authorization_digest=transaction,
                recovery_capsule_digest=capsule,
            ),
            "object": {"type": "commit", "sha": candidate["source_sha"]},
        },
        "release": {
            "tag_name": "v1.2.3",
            "draft": True,
            "prerelease": False,
            "immutable": False,
        },
    }
    if mutation == "lightweight":
        observation["ref"]["object"]["type"] = "commit"  # type: ignore[index]
    elif mutation == "target":
        observation["tag"]["object"]["sha"] = "d" * 40  # type: ignore[index]
    elif mutation == "message":
        observation["tag"]["message"] = "substituted"  # type: ignore[index]
    elif mutation == "mutable-release":
        observation["release"]["draft"] = False  # type: ignore[index]

    if mutation is None:
        subject._require_committed_recovery_marker(  # noqa: SLF001
            observation=observation,
            candidate=candidate,
            transaction_authorization_digest=transaction,
            recovery_capsule_digest=capsule,
        )
    else:
        with pytest.raises(ValueError, match="commit marker|peel|Release"):
            subject._require_committed_recovery_marker(  # noqa: SLF001
                observation=observation,
                candidate=candidate,
                transaction_authorization_digest=transaction,
                recovery_capsule_digest=capsule,
            )


def test_recovery_authorization_commit_marker_is_a_fresh_registered_source() -> None:
    source_name = subject._AUTHORIZATION_EXTERNAL_SOURCE_NAMES[  # noqa: SLF001
        "commit_marker_observation"
    ]
    registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
    matches = [
        entry
        for entry in registry["entries"]
        if entry["receipt_schema"] == receipts.SOURCE_OBSERVATION_SCHEMA
        and entry["phase"] == "release-control"
        and entry["mode"] is None
        and entry["name"] == source_name
    ]

    assert len(matches) == 1
    assert matches[0]["authentication_mode"] == "github-owner"
    assert matches[0]["freshness_class"] == "current"


def test_github_surface_verification_is_controller_owned_and_uses_pinned_gh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    candidate = _candidate_manifest()
    bundle = tmp_path / "bundle"
    release_file = bundle / "release" / "kestrel.whl"
    release_file.parent.mkdir(parents=True)
    release_file.write_bytes(b"x")
    release_digest = _sha256(b"x")
    candidate["artifacts"][0]["sha256"] = release_digest  # type: ignore[index]
    candidate["attestation_subjects"][0]["digest"] = release_digest  # type: ignore[index]
    candidate["artifact_set_digest"] = receipts._sha256(  # noqa: SLF001
        _canonical(candidate["artifacts"])
    )
    context, _transaction_digest, capsule_digest = _promotion_predicate_fixture(
        candidate
    )
    authority_proof, promotion_run = _promotion_authority_proof(
        tmp_path,
        context=context,
        capsule_digest=capsule_digest,
    )
    pinned_gh = tmp_path / "gh"
    pinned_gh.write_bytes(b"pinned gh")
    pinned_gh.chmod(0o700)
    calls: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if command[1:] == ["--version"]:
            stdout = b"gh version 2.97.0 (2026-02-26)\n"
        elif command[1:3] == ["attestation", "verify"]:
            target = command[3]
            if target.startswith("oci://"):
                expected = candidate["attestation_subjects"][1]  # type: ignore[index]
            else:
                expected = candidate["attestation_subjects"][0]  # type: ignore[index]
            stdout = _canonical(
                _promotion_verification(
                    context=context,
                    promotion_run=promotion_run,
                    execution_digest=None,
                    subjects=[expected],
                )
            )
        else:
            stdout = b'{"verified":true}'
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(receipts.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "verification-token")
    _trust_test_gh_binary(pinned_gh, monkeypatch)

    results, evidence = subject._run_github_surface_verifications(  # noqa: SLF001
        candidate=candidate,
        bundle_root=bundle,
        pinned_gh=pinned_gh,
        expected_context=context,
        recovery_capsule_digest=capsule_digest,
        authority_verifications={(808, "2026-08-13T20:01:00Z"): authority_proof},
    )

    repository = "John-MiracleWorker/Kestrel"
    common = [
        "--repo",
        repository,
        "--signer-workflow",
        f"{repository}/.github/workflows/release.yml",
        "--signer-digest",
        SOURCE_SHA,
        "--source-digest",
        SOURCE_SHA,
        "--predicate-type",
        "https://kestrel.dev/attestations/release-promotion/v1",
        "--deny-self-hosted-runners",
        "--format",
        "json",
    ]
    assert calls == [
        [str(pinned_gh), "--version"],
        [
            str(pinned_gh),
            "release",
            "verify",
            "v0.6.0",
            "--repo",
            repository,
            "--format",
            "json",
        ],
        [
            str(pinned_gh),
            "release",
            "verify-asset",
            "v0.6.0",
            str(release_file.resolve()),
            "--repo",
            repository,
            "--format",
            "json",
        ],
        [str(pinned_gh), "attestation", "verify", str(release_file.resolve()), *common],
        [
            str(pinned_gh),
            "attestation",
            "verify",
            "oci://ghcr.io/john-miracleworker/kestrel@sha256:" + "d" * 64,
            *common,
        ],
    ]
    assert [item["result"] for item in results] == ["passed"] * 4
    assert set(evidence) == {
        "github-release-verification",
        "github-release-asset-001",
        "repository-attestation-001",
        "repository-attestation-002",
    }
    assert all(b"verification-token" not in raw for raw in evidence.values())


def test_github_surface_verification_rejects_mutable_oci_subject(
    tmp_path: Path,
) -> None:
    candidate = _candidate_manifest()
    candidate["attestation_subjects"][1]["name"] = (  # type: ignore[index]
        "ghcr.io/john-miracleworker/kestrel:0.6.0"
    )
    pinned_gh = tmp_path / "gh"
    pinned_gh.write_bytes(b"pinned gh")
    pinned_gh.chmod(0o700)

    with pytest.raises(ValueError, match="candidate OCI repository"):
        subject._run_github_surface_verifications(  # noqa: SLF001
            candidate=candidate,
            bundle_root=tmp_path / "bundle",
            pinned_gh=pinned_gh,
            expected_context={},
            recovery_capsule_digest="sha256:" + "f" * 64,
            authority_verifications={},
        )


@pytest.mark.parametrize(
    "vector_name",
    [
        "release-preparation-outcome",
        "release-commit-outcome",
        "release-github-ghcr-verification",
        "release-pypi-outcome",
    ],
)
def test_release_stage_vectors_pass_semantic_validation(vector_name: str) -> None:
    record, _ = _contract_vector(vector_name)

    validated = subject.validate_release_stage_record(json.loads(record))

    assert _canonical(validated) == record


@pytest.mark.parametrize(
    ("vector_name", "mutation"),
    [
        ("release-preparation-outcome", "missing-operation"),
        ("release-preparation-outcome", "unknown-operation"),
        ("release-commit-outcome", "missing-previous"),
        ("release-commit-outcome", "missing-authority"),
        ("release-github-ghcr-verification", "failed-check-complete"),
        ("release-pypi-outcome", "uncertain-complete"),
    ],
)
def test_release_stage_semantic_mutants_fail_closed(vector_name: str, mutation: str) -> None:
    record, _ = _contract_vector(vector_name)
    value = json.loads(record)
    if mutation == "missing-operation":
        value["attempted_operations"].pop()
    elif mutation == "unknown-operation":
        value["attempted_operations"][0]["operation"] = "delete_release"
    elif mutation == "missing-previous":
        value["previous_record_digest"] = None
    elif mutation == "missing-authority":
        value["commit_authority_digest"] = "sha256:" + "0" * 63
    elif mutation == "failed-check-complete":
        value["verification_results"][0]["result"] = "failed"
    else:
        value["uncertain"] = True

    with pytest.raises(
        ValueError, match="stage|operation|previous|authority|verification|uncertain"
    ):
        subject.validate_release_stage_record(value)


def test_completed_stage_consumer_rejects_cross_transaction_substitution() -> None:
    record, _ = _contract_vector("release-commit-outcome")
    value = json.loads(record)
    candidate = value["candidate"]

    subject._require_completed_stage_binding(  # noqa: SLF001
        value,
        candidate=candidate,
        transaction_authorization_digest=value["transaction_authorization_digest"],
        execution_authorization_digest=value["execution_authorization_digest"],
        recovery_capsule_digest=value["recovery_capsule_digest"],
        label="commit outcome",
    )
    value["candidate"] = {**candidate, "tag": "v9.9.9", "version": "9.9.9"}

    with pytest.raises(ValueError, match="commit outcome.*binding"):
        subject._require_completed_stage_binding(  # noqa: SLF001
            value,
            candidate=candidate,
            transaction_authorization_digest=value["transaction_authorization_digest"],
            execution_authorization_digest=value["execution_authorization_digest"],
            recovery_capsule_digest=value["recovery_capsule_digest"],
            label="commit outcome",
        )


def test_pypi_authority_consumer_binds_candidate_and_prior_verification() -> None:
    raw, _ = _contract_vector("pypi-authority")
    authority = json.loads(raw)
    bindings = authority["bindings"]

    subject._require_pypi_authority_binding(  # noqa: SLF001
        authority,
        candidate=authority["candidate"],
        transaction_authorization_digest=bindings["transaction_authorization_digest"],
        execution_authorization_digest=bindings["execution_authorization_digest"],
        recovery_capsule_digest=bindings["recovery_capsule_manifest_digest"],
        github_ghcr_verification_digest=bindings["github_ghcr_verification_digest"],
    )

    with pytest.raises(ValueError, match="PyPI authority.*binding"):
        subject._require_pypi_authority_binding(  # noqa: SLF001
            authority,
            candidate={**authority["candidate"], "version": "9.9.9"},
            transaction_authorization_digest=bindings["transaction_authorization_digest"],
            execution_authorization_digest=bindings["execution_authorization_digest"],
            recovery_capsule_digest=bindings["recovery_capsule_manifest_digest"],
            github_ghcr_verification_digest=bindings["github_ghcr_verification_digest"],
        )


def test_github_authority_consumer_binds_phase_and_transaction() -> None:
    raw, _ = _contract_vector("github-authority")
    authority = json.loads(raw)
    bindings = authority["bindings"]

    subject._require_github_authority_binding(  # noqa: SLF001
        authority,
        candidate=authority["candidate"],
        phase="admission",
        transaction_authorization_digest=bindings["transaction_authorization_digest"],
        execution_authorization_digest=bindings["execution_authorization_digest"],
        recovery_capsule_digest=bindings["recovery_capsule_manifest_digest"],
        commit_marker_digest=bindings["commit_marker_digest"],
    )

    with pytest.raises(ValueError, match="GitHub authority.*binding"):
        subject._require_github_authority_binding(  # noqa: SLF001
            authority,
            candidate=authority["candidate"],
            phase="commit",
            transaction_authorization_digest=bindings["transaction_authorization_digest"],
            execution_authorization_digest=bindings["execution_authorization_digest"],
            recovery_capsule_digest=bindings["recovery_capsule_manifest_digest"],
            commit_marker_digest=bindings["commit_marker_digest"],
        )


def test_server_authorization_consumes_the_frozen_admission_authority() -> None:
    raw, _ = _contract_vector("github-authority")
    authority = json.loads(raw)
    bindings = authority["bindings"]

    subject._require_authorization_admission_authority(  # noqa: SLF001
        authority,
        candidate=authority["candidate"],
        transaction_authorization_digest=bindings["transaction_authorization_digest"],
        recovery_capsule_digest=bindings["recovery_capsule_manifest_digest"],
        commit_marker_digest=bindings["commit_marker_digest"],
    )

    authority["phase"] = "commit"
    with pytest.raises(ValueError, match="admission authority.*binding"):
        subject._require_authorization_admission_authority(  # noqa: SLF001
            authority,
            candidate=authority["candidate"],
            transaction_authorization_digest=bindings[
                "transaction_authorization_digest"
            ],
            recovery_capsule_digest=bindings["recovery_capsule_manifest_digest"],
            commit_marker_digest=bindings["commit_marker_digest"],
        )


def _genuine_github_authority_verification(
    *, verified_at: datetime = datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC)
) -> tuple[dict[str, object], dict[str, object]]:
    raw, signature = _contract_vector("github-authority")
    assert signature is not None
    authority = json.loads(raw)
    candidate = receipts._object(authority["candidate"], label="test authority candidate")  # noqa: SLF001
    run = receipts._object(authority["promotion_run"], label="test authority run")  # noqa: SLF001
    environment = receipts._object(  # noqa: SLF001
        authority["environment"], label="test authority environment"
    )
    verification = receipts.verify_github_authority(
        receipt=raw,
        signature=signature,
        owner_signing_keys_observation=_canonical(
            _owner_signing_keys_observation("2026-08-13T20:00:00Z")
        ),
        expected_run_id=run["run_id"],
        expected_candidate_digest=candidate["candidate_manifest_digest"],
        expected_environment_id=environment["id"],
        _clock=lambda: verified_at,
    )
    return verification, authority


def test_authority_verification_record_binds_embedded_signed_receipt() -> None:
    verification, authority = _genuine_github_authority_verification()
    verified_at = datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC)

    assert (
        subject._verified_authority_from_record(  # noqa: SLF001
            verification,
            verification_schema="kestrel.github_release_authority_verification.v1",
            authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
            label="GitHub authority verification",
            _clock=lambda: verified_at,
        )
        == authority
    )

    verification["authority"] = {
        **authority,
        "candidate": {**authority["candidate"], "version": "9.9.9"},
    }
    with pytest.raises(ValueError, match="receipt|authority"):
        subject._verified_authority_from_record(  # noqa: SLF001
            verification,
            verification_schema=("kestrel.github_release_authority_verification.v1"),
            authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
            label="GitHub authority verification",
            _clock=lambda: verified_at,
        )


def test_authority_verification_and_consumption_use_exclusive_expiry() -> None:
    observed = datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC)
    verification, authority = _genuine_github_authority_verification(verified_at=observed)
    subject._verified_authority_from_record(  # noqa: SLF001
        verification,
        verification_schema="kestrel.github_release_authority_verification.v1",
        authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
        label="GitHub authority verification",
        _clock=lambda: observed,
    )
    subject._require_current_authority(  # noqa: SLF001
        authority,
        label="GitHub authority",
        _clock=lambda: datetime.fromisoformat(authority["observed_at"].replace("Z", "+00:00")),
    )

    verification["verified_at"] = authority["expires_at"]
    expires = datetime.fromisoformat(authority["expires_at"].replace("Z", "+00:00"))
    # At the exclusive expiry boundary the embedded live source is already stale,
    # so verification must fail closed before the signed lifetime is consumed.
    with pytest.raises(ValueError, match="stale|not currently fresh"):
        subject._verified_authority_from_record(  # noqa: SLF001
            verification,
            verification_schema=("kestrel.github_release_authority_verification.v1"),
            authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
            label="GitHub authority verification",
            _clock=lambda: expires,
        )
    with pytest.raises(ValueError, match="stale|not currently fresh"):
        subject._require_current_authority(  # noqa: SLF001
            authority,
            label="GitHub authority",
            _clock=lambda: datetime.fromisoformat(authority["expires_at"].replace("Z", "+00:00")),
        )


def test_final_freshness_budget_uses_one_monotonic_deadline(tmp_path: Path) -> None:
    marker = tmp_path / "final-freshness-budget.json"
    second = 1_000_000_000

    subject.begin_final_reconciliation_freshness_budget(
        marker, _clock=lambda: 100 * second
    )

    assert subject.remaining_final_reconciliation_observation_seconds(
        marker, _clock=lambda: 110 * second
    ) == 65
    subject.require_final_reconciliation_freshness_budget(
        marker, _clock=lambda: 189 * second
    )
    with pytest.raises(ValueError, match="freshness budget is exhausted"):
        subject.require_final_reconciliation_freshness_budget(
            marker, _clock=lambda: 190 * second
        )


def test_historical_authority_proof_remains_verifiable_after_expiry() -> None:
    verified_at = datetime(2026, 8, 13, 20, 1, 0, tzinfo=UTC)
    verification, authority = _genuine_github_authority_verification(
        verified_at=verified_at
    )
    after_expiry = datetime.fromisoformat(authority["expires_at"].replace("Z", "+00:00"))

    with pytest.raises(ValueError, match="stale|not currently fresh"):
        subject._verified_authority_from_record(  # noqa: SLF001
            verification,
            verification_schema="kestrel.github_release_authority_verification.v1",
            authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
            label="GitHub authority verification",
            _clock=lambda: after_expiry,
        )

    assert (
        subject._verified_authority_from_record(  # noqa: SLF001
            verification,
            verification_schema="kestrel.github_release_authority_verification.v1",
            authority_schema=receipts.GITHUB_AUTHORITY_SCHEMA,
            label="historical GitHub authority verification",
            require_current=False,
            _clock=lambda: after_expiry,
        )
        == authority
    )


def test_pypi_stage_requires_exact_four_owner_approvals() -> None:
    history = {
        "records": [
            {
                "environment": {"name": name, "id": 901 + index},
                "reviewer": {
                    "login": "John-MiracleWorker",
                    "id": 58918509,
                    "type": "User",
                },
                "state": "approved",
                "observed_record_digest": "sha256:" + f"{index + 1:x}" * 64,
            }
            for index, name in enumerate(("release", "release-prepare", "release-commit", "pypi"))
        ],
        "complete_response_digest": "sha256:" + "f" * 64,
    }

    subject._require_cumulative_owner_approvals(  # noqa: SLF001
        history,
        expected_environments=(
            "release",
            "release-prepare",
            "release-commit",
            "pypi",
        ),
    )
    history["records"].pop()

    with pytest.raises(ValueError, match="approval.*cardinality"):
        subject._require_cumulative_owner_approvals(  # noqa: SLF001
            history,
            expected_environments=(
                "release",
                "release-prepare",
                "release-commit",
                "pypi",
            ),
        )


def test_release_dispatch_binding_rejects_cross_transaction_substitution() -> None:
    identity_raw, _ = _contract_vector("dispatch-identity")
    intent_raw, _ = _contract_vector("dispatch-intent")
    reconciliation_raw, _ = _contract_vector("dispatch-reconciliation-adopted")
    identity = json.loads(identity_raw)
    intent = json.loads(intent_raw)
    reconciliation = json.loads(reconciliation_raw)
    run = {
        "repository_id": identity["repository_id"],
        "workflow_id": intent["workflow"]["id"],
        "workflow_path": intent["workflow"]["path"],
        "run_id": identity["run_id"],
        "run_attempt": identity["run_attempt"],
        "event": identity["event_name"],
        "ref": identity["ref"],
        "head_sha": identity["sha"],
        "workflow_sha": identity["workflow_sha"],
        "actor": {"login": identity["actor"], "id": identity["actor_id"]},
        "triggering_actor": {
            "login": identity["triggering_actor"],
            "id": identity["actor_id"],
        },
        "transaction_nonce": identity["transaction_nonce"],
        "rest_observation_digest": "sha256:" + "7" * 64,
        "context_observation_digest": "sha256:" + "8" * 64,
    }

    subject._require_release_dispatch_binding(  # noqa: SLF001
        run=run,
        identity=identity,
        intent=intent,
        dispatch_reconciliation=reconciliation,
    )

    substituted_run = {**run, "run_id": run["run_id"] + 1}
    with pytest.raises(ValueError, match="dispatch authority binding"):
        subject._require_release_dispatch_binding(  # noqa: SLF001
            run=substituted_run,
            identity=identity,
            intent=intent,
            dispatch_reconciliation=reconciliation,
        )

    substituted_reconciliation = copy.deepcopy(reconciliation)
    substituted_reconciliation["transaction"]["request_sha256"] = "sha256:" + "9" * 64
    with pytest.raises(ValueError, match="dispatch authority binding"):
        subject._require_release_dispatch_binding(  # noqa: SLF001
            run=run,
            identity=identity,
            intent=intent,
            dispatch_reconciliation=substituted_reconciliation,
        )


def _write_bound_release_stage_chain(
    root: Path,
) -> tuple[Path, dict[str, object], str, str]:
    stage_root = root / "stage-records"
    stage_root.mkdir(parents=True)
    vector_names = (
        (
            "release-preparation-outcome.json",
            "release-preparation-outcome",
        ),
        ("release-commit-outcome.json", "release-commit-outcome"),
        (
            "release-github-ghcr-verification.json",
            "release-github-ghcr-verification",
        ),
        ("release-pypi-outcome.json", "release-pypi-outcome"),
    )
    preparation_raw, _ = _contract_vector("release-preparation-outcome")
    preparation = json.loads(preparation_raw)
    candidate = preparation["candidate"]
    transaction_digest = preparation["transaction_authorization_digest"]
    capsule_digest = preparation["recovery_capsule_digest"]
    previous_raw: bytes | None = None
    for filename, vector_name in vector_names:
        raw, _ = _contract_vector(vector_name)
        stage = json.loads(raw)
        stage["candidate"] = copy.deepcopy(candidate)
        stage["transaction_authorization_digest"] = transaction_digest
        stage["execution_authorization_digest"] = None
        stage["recovery_capsule_digest"] = capsule_digest
        stage["previous_record_digest"] = None if previous_raw is None else _sha256(previous_raw)
        previous_raw = _canonical(stage)
        (stage_root / filename).write_bytes(previous_raw)
    return stage_root, candidate, transaction_digest, capsule_digest


def _write_release_stage_prefix(
    root: Path,
    *,
    candidate: dict[str, object],
    transaction_digest: str,
    execution_digest: str | None,
    capsule_digest: str,
    stage_count: int,
) -> Path:
    stage_root = root / "stage-records"
    stage_root.mkdir(parents=True)
    vector_names = (
        ("release-preparation-outcome.json", "release-preparation-outcome"),
        ("release-commit-outcome.json", "release-commit-outcome"),
        (
            "release-github-ghcr-verification.json",
            "release-github-ghcr-verification",
        ),
        ("release-pypi-outcome.json", "release-pypi-outcome"),
    )
    previous_raw: bytes | None = None
    for filename, vector_name in vector_names[:stage_count]:
        raw, _ = _contract_vector(vector_name)
        stage = json.loads(raw)
        stage["candidate"] = copy.deepcopy(candidate)
        stage["transaction_authorization_digest"] = transaction_digest
        stage["execution_authorization_digest"] = execution_digest
        stage["recovery_capsule_digest"] = capsule_digest
        stage["previous_record_digest"] = None if previous_raw is None else _sha256(previous_raw)
        previous_raw = _canonical(stage)
        (stage_root / filename).write_bytes(previous_raw)
    return stage_root


def _final_lock_proof() -> dict[str, object]:
    workflow_digest = _sha256((ROOT / ".github/workflows/release.yml").read_bytes())
    return {
        "main_lock": {
            "name": "kestrel-release-transaction-main-lock",
            "target": "branch",
            "enforcement": "active",
            "source_type": "Repository",
            "source": "John-MiracleWorker/Kestrel",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {
                    "type": "update",
                    "parameters": {"update_allows_fetch_and_merge": False},
                },
            ],
        },
        "workflow": {
            "id": 707,
            "path": ".github/workflows/release.yml",
            "state": "active",
            "default_branch": "main",
        },
        "default_branch_workflow_sha256": workflow_digest,
        "capsule_workflow_sha256": workflow_digest,
    }


def _server_authorization_fixture(
    *,
    candidate: dict[str, object],
    promotion_run: dict[str, object],
    mode: str,
    transaction_authorization: bytes | None,
    capsule_digest: str | None,
) -> bytes:
    return _canonical(
        subject.build_server_authorization(
            candidate=candidate,
            promotion_run=promotion_run,
            environment={
                "id": 505,
                "name": "release",
                "policies_digest": "sha256:" + "4" * 64,
            },
            approval_history={
                "records": [
                    {
                        "environment": {"id": 505, "name": "release"},
                        "reviewer": {
                            "id": 58918509,
                            "login": "John-MiracleWorker",
                            "type": "User",
                        },
                        "state": "approved",
                        "observed_record_digest": "sha256:" + "5" * 64,
                    }
                ],
                "complete_response_digest": "sha256:" + "6" * 64,
            },
            admission_authority={
                "receipt_digest": "sha256:" + "7" * 64,
                "signature_digest": "sha256:" + "8" * 64,
                "verification_digest": "sha256:" + "9" * 64,
            },
            repository_state={
                "repository_writers_observation_digest": "sha256:" + "a" * 64,
                "actions_authority_digest": "sha256:" + "b" * 64,
                "immutable_releases_observation_digest": "sha256:" + "c" * 64,
                "tag_ruleset_observation_digest": "sha256:" + "d" * 64,
                "ingress_observation_digest": "sha256:" + "e" * 64,
            },
            mode=mode,
            transaction_authorization=transaction_authorization,
            recovery_capsule_manifest_digest=(None if mode == "initiate" else capsule_digest),
            commit_marker_digest=(None if mode == "initiate" else "sha256:" + "f" * 64),
            source_records={"test-fixture": b"{}"},
            _clock=lambda: datetime(2026, 8, 13, 20, 0, 14, tzinfo=UTC),
        )
    )


def _final_candidate_manifest() -> dict[str, object]:
    manifest = _candidate_manifest()
    oci_blob = "c" * 64
    artifacts = [
        {
            "path": "release/nested_memvid_agent-0.6.0-py3-none-any.whl",
            "media_type": "application/zip",
            "sha256": "sha256:" + "a" * 64,
            "size_bytes": 101,
        },
        {
            "path": "release/nested_memvid_agent-0.6.0.tar.gz",
            "media_type": "application/gzip",
            "sha256": "sha256:" + "b" * 64,
            "size_bytes": 102,
        },
        {
            "path": f"containers/oci-layout/blobs/sha256/{oci_blob}",
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "sha256": f"sha256:{oci_blob}",
            "size_bytes": 103,
        },
    ]
    manifest["artifacts"] = artifacts
    manifest["artifact_set_digest"] = receipts._sha256(_canonical(artifacts))  # noqa: SLF001
    manifest["attestation_subjects"] = [
        {
            "kind": "file",
            "name": artifact["path"],
            "digest": artifact["sha256"],
        }
        for artifact in artifacts[:2]
    ] + [
        {
            "kind": "oci_index",
            "name": subject.candidates.OCI_REPOSITORY,
            "digest": "sha256:" + "d" * 64,
        }
    ]
    return manifest


def _final_source_envelope(name: str, body: object | bytes) -> dict[str, object]:
    registry = json.loads((ROOT / "release-control-source-registry.json").read_bytes())
    entry = next(
        item
        for item in registry["entries"]
        if item["receipt_schema"] == receipts.SOURCE_OBSERVATION_SCHEMA
        and item["phase"] == "release-control"
        and item["mode"] is None
        and item["name"] == name
    )
    if entry["body_mode"] == "paginated-json":
        assert isinstance(body, list)
        raw_body = receipts.canonical_external_json_bytes(
            {
                "pages": [
                    {
                        "number": 1,
                        "request_url": entry["locator"],
                        "response_headers": [],
                        "body": body,
                    }
                ]
            }
        )
    elif isinstance(body, bytes):
        raw_body = body
    else:
        raw_body = receipts.canonical_external_json_bytes(body)
    return json.loads(
        _contract_source_envelope(
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name=name,
            body=raw_body,
        )
    )


def _final_remote_source_envelopes(
    tmp_path: Path,
    *,
    manifest: dict[str, object],
    candidate: dict[str, object],
    transaction_digest: str,
    execution_digest: str | None,
    capsule_digest: str,
    complete: bool,
) -> list[dict[str, object]]:
    release_contract = subject._candidate_product_release_contract(  # noqa: SLF001
        manifest,
        transaction_authorization_digest=transaction_digest,
        recovery_capsule_digest=capsule_digest,
    )
    release_assets = [
        {
            "id": index + 100,
            "name": item["name"],
            "size": item["size_bytes"],
            "digest": item["sha256"],
            "content_type": item["media_type"],
        }
        for index, item in enumerate(release_contract["assets"])
    ]
    release_listing = (
        [
            {
                "id": 91,
                **release_contract["persisted"],
                "draft": False,
                "immutable": True,
                "assets": release_assets,
            }
        ]
        if complete
        else []
    )
    ghcr = {
        "repository": subject.candidates.OCI_REPOSITORY,
        "package_present": True,
        "objects": [
            {
                "digest": digest,
                "http_status": 200 if complete else 404,
                "tags": [],
                "tag_inventory_complete": True,
                "observed_at": "2026-08-13T20:00:00Z",
            }
            for digest in subject._expected_oci_object_digests_from_manifest(  # noqa: SLF001
                manifest
            )
        ],
    }
    if complete:
        tag_object_sha = "e" * 40
        tag_observation: dict[str, object] = {
            "http_status": 200,
            "ref": {
                "ref": f"refs/tags/{candidate['tag']}",
                "object": {"type": "tag", "sha": tag_object_sha},
            },
            "tag": {
                "sha": tag_object_sha,
                "tag": candidate["tag"],
                "message": subject.build_annotated_tag_message(
                    candidate=candidate,
                    transaction_authorization_digest=transaction_digest,
                    recovery_capsule_digest=capsule_digest,
                ),
                "object": {"type": "commit", "sha": candidate["source_sha"]},
            },
        }
    else:
        tag_observation = {"http_status": 404, "ref": None, "tag": None}
    predicate_context: dict[str, object] | None = None
    authority_verification: dict[str, object] | None = None
    attestation_run: dict[str, object] | None = None
    if complete:
        predicate_context = subject._release_promotion_predicate_context(  # noqa: SLF001
            manifest=manifest,
            transaction_authorization_digest=transaction_digest,
            release_listing=[release_listing],
            release_contract=release_contract,
            ghcr_observation=ghcr,
            expected_oci_digests=subject._expected_oci_object_digests_from_manifest(  # noqa: SLF001
                manifest
            ),
        )
        authority_verification, attestation_run = _promotion_authority_proof(
            tmp_path,
            context=predicate_context,
            capsule_digest=capsule_digest,
            execution_digest=execution_digest,
        )
    attestation_subjects = []
    for index, item in enumerate(manifest["attestation_subjects"]):
        verification = None
        if complete:
            assert predicate_context is not None
            assert attestation_run is not None
            verification = _promotion_verification(
                context=predicate_context,
                promotion_run=attestation_run,
                execution_digest=execution_digest,
                subjects=[item],
            )
        attestation_subjects.append(
            {
                **item,
                "inventory": [
                    {
                        "attestations": (
                            [
                                {
                                    "repository_id": 303,
                                    "bundle_url": (
                                        "https://example.invalid/attestations/"
                                        f"final-promotion-{index}"
                                    ),
                                    "initiator": "user",
                                }
                            ]
                            if complete
                            else []
                        )
                    }
                ],
                "verification": verification,
                "authority_verification": authority_verification,
            }
        )
    expected_pypi = subject._candidate_pypi_files(manifest)  # noqa: SLF001
    if complete:
        pypi_project = _pypi_project_observation(manifest)
        integrity_files = []
        verification_files = []
        for filename, item in expected_pypi.items():
            provenance = _publish_provenance(filename, item["sha256"])
            provenance_raw = receipts.canonical_external_json_bytes(provenance)
            integrity_files.append(
                {
                    "filename": filename,
                    "provenance_response_base64": base64.b64encode(
                        provenance_raw
                    ).decode("ascii"),
                }
            )
            verification_files.append(
                {
                    "filename": filename,
                    "distribution_sha256": item["sha256"],
                    "provenance_sha256": _sha256(provenance_raw),
                    "exit_code": 0,
                }
            )
    else:
        pypi_project = {
            "info": {"name": "nested-memvid-agent"},
            "last_serial": 17,
            "releases": {manifest["version"]: []},
        }
        integrity_files = []
        verification_files = []
    sources = {
        "final-attestation-observation": {"subjects": attestation_subjects},
        "final-ghcr-observation": ghcr,
        "final-pypi-integrity-observations": {
            "schema": "pypi.integrity_observations.v1",
            "files": integrity_files,
        },
        "final-pypi-project-observation": pypi_project,
        "final-pypi-provenance-verifications": {
            "schema": "pypi.provenance_verifications.v1",
            "tool": {"name": "pypi-attestations", "version": "0.0.30"},
            "files": verification_files,
        },
        "final-release-list-observation": release_listing,
        "tag-observation": tag_observation,
    }
    return [_final_source_envelope(name, sources[name]) for name in sorted(sources)]


def _final_reconciliation_cli_fixture(
    tmp_path: Path,
    *,
    mode: str,
    stage_count: int | None,
    remote_complete: bool | None = None,
) -> tuple[list[str], Path, Path]:
    manifest_raw = _canonical(_final_candidate_manifest())
    dispatch_root = tmp_path / "dispatch"
    dispatch_root.mkdir()
    result, reconciliation = _run_reconcile_cli(
        dispatch_root,
        mode=mode,
        candidate_manifest=manifest_raw,
    )
    assert result == 0
    assert reconciliation is not None

    journal = json.loads((dispatch_root / "journal.json").read_bytes())
    _candidate, identity = _reconciliation_candidate(journal)
    run_observation = {
        "schema": "kestrel.promotion_run_observation.v1",
        "repository_id": identity["repository_id"],
        "workflow_id": journal["workflow"]["id"],
        "workflow_path": journal["workflow"]["path"],
        "run_id": identity["run_id"],
        "run_attempt": identity["run_attempt"],
        "event": identity["event_name"],
        "ref": identity["ref"],
        "head_sha": identity["sha"],
        "workflow_sha": identity["workflow_sha"],
        "actor": {"login": identity["actor"], "id": identity["actor_id"]},
        "triggering_actor": {
            "login": identity["triggering_actor"],
            "id": identity["actor_id"],
        },
        "captured_at": identity["observed_at"],
        "complete": True,
    }
    run_path = tmp_path / "promotion-run.json"
    identity_path = tmp_path / "dispatch-identity.json"
    fresh_path = tmp_path / "fresh-final-state.json"
    output_path = tmp_path / "release-reconciliation.json"
    run_path.write_bytes(
        _contract_source_envelope(
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="promotion-run-observation",
            body=_canonical(run_observation),
        )
    )
    identity_path.write_bytes(_canonical(identity))

    candidate_inputs = stage_count is not None
    actual_remote_complete = stage_count == 4 if remote_complete is None else remote_complete
    release_complete = stage_count == 4 and actual_remote_complete
    failure_code = None if release_complete else "reconciliation_input_missing"
    lock = _final_lock_proof()
    workflow_raw = (ROOT / ".github/workflows/release.yml").read_bytes()
    fresh_sources = [
        _final_source_envelope("default-branch-workflow-contents", workflow_raw),
        _final_source_envelope("ingress-ruleset-detail-observation", lock["main_lock"]),
        _final_source_envelope("workflow-observation", lock["workflow"]),
    ]

    arguments = [
        "reconcile",
        "--run-observation",
        str(run_path),
        "--dispatch-identity",
        str(identity_path),
        "--dispatch-intent",
        str(dispatch_root / "intent.json"),
        "--dispatch-reconciliation",
        str(dispatch_root / "reconciliation.json"),
        "--fresh-observations",
        str(fresh_path),
        "--failure-code",
        "none" if failure_code is None else failure_code,
        "--output",
        str(output_path),
    ]
    if candidate_inputs:
        manifest_path = tmp_path / "candidate-manifest.json"
        manifest_path.write_bytes(manifest_raw)
        manifest = json.loads(manifest_raw)
        candidate, _repository_id = receipts._candidate_from_manifest(  # noqa: SLF001
            manifest_raw
        )
        promotion_run = subject._authorization_promotion_run(  # noqa: SLF001
            run_observation=run_observation,
            run_observation_raw=run_path.read_bytes(),
            identity=identity,
            identity_raw=identity_path.read_bytes(),
        )
        capsule_digest = "sha256:" + "f" * 64
        if mode == "initiate":
            transaction_raw = _server_authorization_fixture(
                candidate=candidate,
                promotion_run=promotion_run,
                mode="initiate",
                transaction_authorization=None,
                capsule_digest=None,
            )
            execution_raw = None
        else:
            original_run = copy.deepcopy(promotion_run)
            original_run["run_id"] = 1100
            original_run["ref"] = "refs/heads/main"
            original_run["transaction_nonce"] = "02" * 32
            transaction_raw = _server_authorization_fixture(
                candidate=candidate,
                promotion_run=original_run,
                mode="initiate",
                transaction_authorization=None,
                capsule_digest=None,
            )
            execution_raw = _server_authorization_fixture(
                candidate=candidate,
                promotion_run=promotion_run,
                mode="recover_committed",
                transaction_authorization=transaction_raw,
                capsule_digest=capsule_digest,
            )
        transaction_path = tmp_path / "transaction-authorization.json"
        transaction_path.write_bytes(transaction_raw)
        execution_path = tmp_path / "execution-authorization.json"
        if execution_raw is not None:
            execution_path.write_bytes(execution_raw)
        verification_path = tmp_path / "recovery-capsule-verification.json"
        verification_path.write_bytes(
            _signed_recovery_capsule_verification(
                tmp_path,
                transaction_authorization=transaction_raw,
                candidate_manifest_digest=candidate["candidate_manifest_digest"],
                capsule_manifest_digest=capsule_digest,
            )
        )
        stage_root = _write_release_stage_prefix(
            tmp_path,
            candidate=candidate,
            transaction_digest=_sha256(transaction_raw),
            execution_digest=(None if execution_raw is None else _sha256(execution_raw)),
            capsule_digest=capsule_digest,
            stage_count=stage_count,
        )
        arguments.extend(
            [
                "--manifest",
                str(manifest_path),
                "--transaction-authorization",
                str(transaction_path),
                "--recovery-capsule-verification",
                str(verification_path),
                "--stage-records",
                str(stage_root),
            ]
        )
        if execution_raw is not None:
            arguments.extend(["--execution-authorization", str(execution_path)])
        fresh_sources.extend(
            _final_remote_source_envelopes(
                tmp_path,
                manifest=manifest,
                candidate=candidate,
                transaction_digest=_sha256(transaction_raw),
                execution_digest=(
                    None if execution_raw is None else _sha256(execution_raw)
                ),
                capsule_digest=capsule_digest,
                complete=actual_remote_complete,
            )
        )
    fresh = {
        "schema": "kestrel.release_final_observations.v2",
        "sources": sorted(fresh_sources, key=lambda item: str(item["name"])),
    }
    fresh_path.write_bytes(_canonical(fresh))
    return arguments, fresh_path, output_path


def _attach_current_final_authority(
    tmp_path: Path,
    *,
    arguments: list[str],
    fresh_path: Path,
    authority_binding_digest: str | None = None,
    tamper_boundary_source: bool = False,
) -> tuple[Path, Path]:
    """Create the signed final authority and exact fresh boundary for one CLI fixture."""

    def argument_path(name: str) -> Path:
        return Path(arguments[arguments.index(name) + 1])

    manifest_raw = argument_path("--manifest").read_bytes()
    candidate, _repository_id = receipts._candidate_from_manifest(  # noqa: SLF001
        manifest_raw
    )
    transaction_raw = argument_path("--transaction-authorization").read_bytes()
    transaction_digest = _sha256(transaction_raw)
    execution_digest = (
        None
        if "--execution-authorization" not in arguments
        else _sha256(argument_path("--execution-authorization").read_bytes())
    )
    capsule = json.loads(argument_path("--recovery-capsule-verification").read_bytes())
    capsule_digest = capsule["verification"]["capsule_manifest_digest"]

    now = datetime.now(UTC).replace(microsecond=0)
    observed_at = now.isoformat().replace("+00:00", "Z")
    expires_at = (now + timedelta(seconds=receipts.RECEIPT_LIFETIME_SECONDS)).isoformat().replace(
        "+00:00", "Z"
    )
    workflow_raw = (ROOT / ".github/workflows/release.yml").read_bytes()
    workflow_digest = _sha256(workflow_raw)
    intent = json.loads(argument_path("--dispatch-intent").read_bytes())
    run_source = json.loads(argument_path("--run-observation").read_bytes())
    run_body = json.loads(receipts.source_observation_body(_canonical(run_source)))
    identity = json.loads(argument_path("--dispatch-identity").read_bytes())
    promotion_run = subject._authorization_promotion_run(  # noqa: SLF001
        run_observation=run_body,
        run_observation_raw=_canonical(run_source),
        identity=identity,
        identity_raw=_canonical(identity),
    )

    raw, _signature = _contract_vector("github-authority")
    authority = json.loads(raw)
    authority.update(
        {
            "phase": "commit",
            "mode": intent["inputs"]["mode"],
            "candidate": candidate,
            "promotion_run": promotion_run,
            "environment": {"id": 903, "name": "release-commit"},
            "bindings": {
                "transaction_authorization_digest": (
                    authority_binding_digest or transaction_digest
                ),
                "execution_authorization_digest": execution_digest,
                "recovery_capsule_manifest_digest": capsule_digest,
                "commit_marker_digest": None,
            },
            "installed_apps": [],
            "observed_at": observed_at,
            "expires_at": expires_at,
            "maintenance_window_acknowledgement": {
                "acknowledged_by_login": "John-MiracleWorker",
                "acknowledged_by_id": 58918509,
                "begins_at": observed_at,
                "expires_at": expires_at,
                "statement": authority["maintenance_window_acknowledgement"][
                    "statement"
                ],
            },
            "owner": {
                "login": "John-MiracleWorker",
                "id": 58918509,
                "node_id": "MDQ6VXNlcjU4OTE4NTA5",
                "type": "User",
            },
            "repository": {
                "full_name": "John-MiracleWorker/Kestrel",
                "id": 303,
                "owner_login": "John-MiracleWorker",
                "owner_id": 58918509,
            },
            "source_snapshots": [
                {
                    "name": "installed-apps-owner",
                    "provider": "github.com",
                    "locator": "GET /user/installations?per_page=100",
                    "authenticated_as": "John-MiracleWorker",
                    "freshness_class": "current",
                    "captured_at": observed_at,
                    "page_count": 1,
                    "record_count": 0,
                    "sha256": "sha256:" + "1" * 64,
                    "size_bytes": 2,
                    "complete": True,
                }
            ],
            "ghcr_package": {
                "state": "present",
                "scope": "user",
                "owner_login": "John-MiracleWorker",
                "name": "kestrel",
                "linked_repository": {
                    "full_name": "John-MiracleWorker/Kestrel",
                    "id": 303,
                },
                "inheritance_mode": "granular",
                "direct_roles": [],
                "team_roles": [],
                "actions_access": [],
                "upload_delete_principals": [
                    {
                        "kind": "repository",
                        "id": 303,
                        "name": "John-MiracleWorker/Kestrel",
                        "capability": "upload",
                    },
                    {
                        "kind": "user",
                        "id": 58918509,
                        "name": "John-MiracleWorker",
                        "capability": "upload_and_delete",
                    },
                ],
            },
        }
    )
    authority["tag_ruleset"].update(
        {
            "id": 810,
            "updated_at": observed_at,
            "observation_digest": "sha256:" + "2" * 64,
        }
    )
    authority["ingress_ruleset"].update(
        {
            "id": 811,
            "updated_at": observed_at,
            "observation_digest": "sha256:" + "3" * 64,
        }
    )
    authority["workflow_ingress"].update(
        {
            "workflow_id": 707,
            "default_branch_blob_sha256": workflow_digest,
            "candidate_blob_sha256": workflow_digest,
            "capsule_blob_sha256": workflow_digest,
        }
    )
    receipts.validate_github_authority(authority)
    authority_raw = _canonical(authority)
    authority_signature = receipts.sign_receipt_detached(
        receipt=authority_raw,
        identity_file=_signing_identity(tmp_path),
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    owner_keys_raw = _canonical(_owner_signing_keys_observation(observed_at))
    verification = receipts.verify_github_authority(
        receipt=authority_raw,
        signature=authority_signature,
        owner_signing_keys_observation=owner_keys_raw,
        expected_run_id=promotion_run["run_id"],
        expected_candidate_digest=candidate["candidate_manifest_digest"],
        expected_environment_id=903,
        _clock=lambda: now,
    )
    verification_path = tmp_path / "final-github-authority-verification.json"
    verification_path.write_bytes(_canonical(verification))

    boundary_root = tmp_path / "final-boundary"
    boundary_root.mkdir()

    def write_source(name: str, body: object | bytes) -> None:
        boundary_root.joinpath(f"{name}.json").write_bytes(
            _canonical(_final_source_envelope(name, body))
        )

    boundary_root.joinpath("owner-signing-keys-observation.json").write_bytes(
        owner_keys_raw
    )
    write_source("tag-ruleset-detail-observation", authority["tag_ruleset"])
    ingress = copy.deepcopy(authority["ingress_ruleset"])
    if tamper_boundary_source:
        ingress["updated_at"] = (now + timedelta(seconds=1)).isoformat().replace(
            "+00:00", "Z"
        )
    write_source("ingress-ruleset-detail-observation", ingress)
    workflow = {
        "id": 707,
        "path": ".github/workflows/release.yml",
        "state": "active",
        "default_branch": "main",
    }
    write_source("workflow-observation", workflow)
    write_source("default-branch-workflow-contents", workflow_raw)
    write_source("candidate-workflow-contents", workflow_raw)
    write_source(
        "main-branch-observation",
        {"name": "main", "commit": {"sha": candidate["source_sha"]}},
    )
    write_source("immutable-releases-observation", {"enabled": True})
    for environment_name, environment_id in (
        ("release", 901),
        ("release-prepare", 902),
        ("release-commit", 903),
        ("pypi", 904),
    ):
        write_source(
            f"environment-{environment_name}-observation",
            _environment_gate_observation(environment_name, environment_id),
        )
        write_source(
            f"environment-{environment_name}-policies-observation",
            _environment_policy_observation(environment_id),
        )

    fresh = json.loads(fresh_path.read_bytes())
    replacement_sources = {
        "default-branch-workflow-contents": workflow_raw,
        "ingress-ruleset-detail-observation": authority["ingress_ruleset"],
        "workflow-observation": workflow,
    }
    fresh["sources"] = [
        _final_source_envelope(name, replacement_sources[name])
        if (name := str(source["name"])) in replacement_sources
        else source
        for source in fresh["sources"]
    ]
    fresh_path.write_bytes(_canonical(fresh))

    arguments.extend(
        [
            "--final-github-authority-verification",
            str(verification_path),
            "--final-boundary-root",
            str(boundary_root),
        ]
    )
    return verification_path, boundary_root


def test_reconciliation_stage_chain_binds_authority_and_previous_bytes(
    tmp_path: Path,
) -> None:
    stage_root, candidate, transaction_digest, capsule_digest = _write_bound_release_stage_chain(
        tmp_path
    )
    source_records: dict[str, bytes] = {}

    chain = subject._reconciliation_stage_chain(  # noqa: SLF001
        stage_root=stage_root,
        candidate=candidate,
        transaction_authorization_digest=transaction_digest,
        execution_authorization_digest=None,
        recovery_capsule_digest=capsule_digest,
        require_complete=True,
        source_records=source_records,
    )

    assert tuple((item["filename"], item["schema"]) for item in chain) == (
        subject._RELEASE_STAGE_CHAIN  # noqa: SLF001
    )
    final_path = stage_root / "release-pypi-outcome.json"
    substituted = json.loads(final_path.read_bytes())
    substituted["transaction_authorization_digest"] = "sha256:" + "9" * 64
    final_path.write_bytes(_canonical(substituted))
    with pytest.raises(ValueError, match="authority or completion"):
        subject._reconciliation_stage_chain(  # noqa: SLF001
            stage_root=stage_root,
            candidate=candidate,
            transaction_authorization_digest=transaction_digest,
            execution_authorization_digest=None,
            recovery_capsule_digest=capsule_digest,
            require_complete=True,
            source_records={},
        )


def test_reconciliation_stage_chain_rejects_missing_predecessor(
    tmp_path: Path,
) -> None:
    stage_root, candidate, transaction_digest, capsule_digest = _write_bound_release_stage_chain(
        tmp_path
    )
    (stage_root / "release-preparation-outcome.json").unlink()

    with pytest.raises(ValueError, match="chronological prefix"):
        subject._reconciliation_stage_chain(  # noqa: SLF001
            stage_root=stage_root,
            candidate=candidate,
            transaction_authorization_digest=transaction_digest,
            execution_authorization_digest=None,
            recovery_capsule_digest=capsule_digest,
            require_complete=False,
            source_records={},
        )


def test_final_reconciliation_vector_proves_complete_lock_release() -> None:
    record, _ = _contract_vector("release-reconciliation")

    validated = subject.validate_release_reconciliation(json.loads(record))

    assert validated["lock_release_permitted"] is True
    assert len(validated["stage_chain"]) == 4


def test_final_reconciliation_summary_is_derived_from_stage_outcomes() -> None:
    complete = [(True, False, False)] * 4
    assert subject._derive_final_release_summary(  # noqa: SLF001
        stage_statuses=complete,
        full_chain=True,
        remote_complete=True,
        failure_code=None,
        next_action="none",
    ) == (True, False, False)
    assert (
        subject._derive_final_release_summary(  # noqa: SLF001
            stage_statuses=[*complete[:2], (False, True, False)],
            full_chain=False,
            remote_complete=False,
            failure_code="dispatch_response_unknown",
            next_action="reconcile",
        )
        == (False, True, False)
    )
    assert subject._derive_final_release_summary(  # noqa: SLF001
        stage_statuses=[(True, False, False), (False, False, True)],
        full_chain=False,
        remote_complete=False,
        failure_code=None,
        next_action="recover",
    ) == (False, False, True)


def test_final_lock_proof_requires_active_exact_ingress() -> None:
    workflow_digest = "sha256:" + "a" * 64
    proof = {
        "main_lock": {
            "name": "kestrel-release-transaction-main-lock",
            "target": "branch",
            "enforcement": "active",
            "source_type": "Repository",
            "source": "John-MiracleWorker/Kestrel",
            "bypass_actors": [],
            "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
            "rules": [
                {"type": "deletion"},
                {
                    "type": "update",
                    "parameters": {"update_allows_fetch_and_merge": False},
                },
            ],
        },
        "workflow": {
            "id": 404,
            "path": ".github/workflows/release.yml",
            "state": "active",
            "default_branch": "main",
        },
        "default_branch_workflow_sha256": workflow_digest,
        "capsule_workflow_sha256": workflow_digest,
    }
    subject._validate_final_lock_proof(  # noqa: SLF001
        proof, expected_workflow_digest=workflow_digest
    )
    proof["main_lock"]["bypass_actors"] = [{"actor_id": 1}]  # type: ignore[index]
    with pytest.raises(ValueError, match="lock|bypass"):
        subject._validate_final_lock_proof(  # noqa: SLF001
            proof, expected_workflow_digest=workflow_digest
        )


def test_final_lock_sources_allow_advanced_main_only_during_tag_recovery() -> None:
    proof = _final_lock_proof()
    tagged_workflow = b"name: tagged release transaction\n"
    advanced_workflow = b"name: advanced release transaction\n"

    subject._validate_final_lock_sources(  # noqa: SLF001
        main_lock=proof["main_lock"],
        workflow=proof["workflow"],
        default_branch_workflow=advanced_workflow,
        expected_workflow=tagged_workflow,
        transaction_mode="recover_committed",
    )

    with pytest.raises(ValueError, match="ingress byte source"):
        subject._validate_final_lock_sources(  # noqa: SLF001
            main_lock=proof["main_lock"],
            workflow=proof["workflow"],
            default_branch_workflow=advanced_workflow,
            expected_workflow=tagged_workflow,
            transaction_mode="initiate",
        )


def test_failed_pypi_publisher_stays_uncertain_when_files_become_visible() -> None:
    filenames = ("nested_memvid_agent-1.2.3-py3-none-any.whl", "nested_memvid_agent-1.2.3.tar.gz")

    assert subject._pypi_publication_outcome(  # noqa: SLF001
        pre_missing=filenames,
        post_missing=(),
        publisher_outcome="failure",
    ) == "unknown"


def test_pypi_recovery_treats_preexisting_exact_files_as_noop() -> None:
    assert subject._pypi_publication_outcome(  # noqa: SLF001
        pre_missing=(),
        post_missing=(),
        publisher_outcome="skipped",
    ) == "existing_exact"


def test_final_reconcile_without_candidate_records_truthful_nulls(
    tmp_path: Path,
) -> None:
    arguments, _fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode="initiate",
        stage_count=None,
    )

    assert subject.main(arguments) == 0
    record = json.loads(output.read_bytes())
    assert record["candidate"] is None
    assert record["transaction_authorization_digest"] is None
    assert record["execution_authorization_digest"] is None
    assert record["recovery_capsule_digest"] is None
    assert record["stage_chain"] == []
    assert record["completed"] is False
    assert record["pending"] is True
    assert record["lock_release_permitted"] is False


@pytest.mark.parametrize("stage_count", [0, 1, 2, 3])
def test_final_reconcile_derives_each_partial_stage_as_pending(
    tmp_path: Path, stage_count: int
) -> None:
    arguments, _fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode="initiate",
        stage_count=stage_count,
    )

    assert subject.main(arguments) == 0
    record = json.loads(output.read_bytes())
    assert len(record["stage_chain"]) == stage_count
    assert record["completed"] is False
    assert record["uncertain"] is False
    assert record["pending"] is True
    assert record["lock_release_permitted"] is False


@pytest.mark.parametrize("mode", ["initiate", "recover_committed"])
def test_final_reconcile_full_remote_success_stays_pending_without_final_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    provenance_calls: list[dict[str, object]] = []

    def validate_provenance(**kwargs: object) -> list[dict[str, object]]:
        provenance_calls.append(kwargs)
        return []

    monkeypatch.setattr(subject, "_validate_pypi_provenance_evidence", validate_provenance)
    arguments, _fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode=mode,
        stage_count=4,
    )

    assert subject.main(arguments) == 0
    record = json.loads(output.read_bytes())
    assert len(record["stage_chain"]) == 4
    assert record["completed"] is False
    assert record["uncertain"] is False
    assert record["pending"] is True
    assert record["lock_release_permitted"] is False
    assert record["next_action"] == "reconcile"
    assert (record["execution_authorization_digest"] is not None) is (mode == "recover_committed")
    assert len(provenance_calls) == 1


def test_final_reconcile_current_bound_authority_releases_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(subject, "_validate_pypi_provenance_evidence", lambda **_: [])
    arguments, fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode="initiate",
        stage_count=4,
    )
    _attach_current_final_authority(
        tmp_path,
        arguments=arguments,
        fresh_path=fresh_path,
    )

    assert subject.main(arguments) == 0
    record = json.loads(output.read_bytes())
    assert record["completed"] is True
    assert record["pending"] is False
    assert record["uncertain"] is False
    assert record["lock_release_permitted"] is True
    assert record["next_action"] == "none"


@pytest.mark.parametrize("tamper", ["authority-binding", "boundary-source"])
def test_final_reconcile_authority_or_boundary_tamper_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    monkeypatch.setattr(subject, "_validate_pypi_provenance_evidence", lambda **_: [])
    arguments, fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode="initiate",
        stage_count=4,
    )
    _attach_current_final_authority(
        tmp_path,
        arguments=arguments,
        fresh_path=fresh_path,
        authority_binding_digest=(
            "sha256:" + "9" * 64 if tamper == "authority-binding" else None
        ),
        tamper_boundary_source=tamper == "boundary-source",
    )

    assert subject.main(arguments) == 1
    assert not output.exists()


def test_final_reconcile_observation_failure_still_writes_locked_receipt(
    tmp_path: Path,
) -> None:
    arguments, _fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode="initiate",
        stage_count=None,
    )
    arguments[arguments.index("--failure-code") + 1] = "final_observation_failed"

    assert subject.main(arguments) == 0
    record = json.loads(output.read_bytes())
    assert record["failure_code"] == "final_observation_failed"
    assert record["completed"] is False
    assert record["pending"] is True
    assert record["lock_release_permitted"] is False


def test_final_reconcile_full_stage_chain_stays_locked_when_remote_evidence_is_incomplete(
    tmp_path: Path,
) -> None:
    arguments, _fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode="initiate",
        stage_count=4,
        remote_complete=False,
    )

    assert subject.main(arguments) == 0
    record = json.loads(output.read_bytes())
    assert len(record["stage_chain"]) == 4
    assert record["completed"] is False
    assert record["uncertain"] is False
    assert record["pending"] is True
    assert record["lock_release_permitted"] is False


def test_final_reconcile_rejects_remote_success_without_every_stage_producer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subject,
        "_validate_pypi_provenance_evidence",
        lambda **_: [],
    )
    arguments, _fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode="initiate",
        stage_count=2,
        remote_complete=True,
    )

    assert subject.main(arguments) == 1
    assert not output.exists()


def test_final_reconcile_rejects_caller_claimed_completion(
    tmp_path: Path,
) -> None:
    arguments, fresh_path, output = _final_reconciliation_cli_fixture(
        tmp_path,
        mode="initiate",
        stage_count=2,
    )
    fresh = json.loads(fresh_path.read_bytes())
    fresh.update(
        {
            "completed": True,
            "pending": False,
            "failure_code": None,
            "next_action": "none",
            "lock_proof": _final_lock_proof(),
        }
    )
    fresh_path.write_bytes(_canonical(fresh))
    failure_index = arguments.index("--failure-code") + 1
    arguments[failure_index] = "none"

    assert subject.main(arguments) == 1
    assert not output.exists()


def test_completed_recovery_reconciliation_requires_execution_authority() -> None:
    record, _ = _contract_vector("release-reconciliation")
    value = json.loads(record)
    value["dispatch_inputs"]["mode"] = "recover_committed"
    value["run"]["ref"] = f"refs/tags/{value['candidate']['tag']}"
    value["execution_authorization_digest"] = None
    value["lock_release_permitted"] = False

    with pytest.raises(ValueError, match="recovery.*execution authorization"):
        subject.validate_release_reconciliation(value)


@pytest.mark.parametrize(
    "mutation",
    ["missing-stage", "pending", "uncertain", "failed", "wrong-schema", "lock-early"],
)
def test_final_reconciliation_lock_release_mutants_fail_closed(mutation: str) -> None:
    record, _ = _contract_vector("release-reconciliation")
    value = json.loads(record)
    if mutation == "missing-stage":
        value["stage_chain"].pop()
    elif mutation == "pending":
        value["pending"] = True
    elif mutation == "uncertain":
        value["uncertain"] = True
    elif mutation == "failed":
        value["completed"] = False
    elif mutation == "wrong-schema":
        value["stage_chain"][0]["schema"] = "kestrel.wrong.v1"
    else:
        value["candidate"] = None

    with pytest.raises(ValueError, match="reconciliation|lock|stage|complete|candidate"):
        subject.validate_release_reconciliation(value)


def test_dispatch_terminal_publications_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    result, reconciliation = _run_reconcile_cli(tmp_path, include_identity=False)
    assert result == 0
    assert reconciliation is not None
    reconciliation_path = tmp_path / "failed-reconciliation.json"
    containment_path = tmp_path / "failed-containment.json"
    owner_keys_path = tmp_path / "owner-keys.json"
    admission_path = tmp_path / "forbidden-admission.json"
    tombstone_path = tmp_path / "wrong-reason-tombstone.json"
    reconciliation_path.write_bytes(_canonical(reconciliation))
    containment_path.write_bytes(_canonical(reconciliation["containment"]))
    now_text = datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner_keys_path.write_bytes(_canonical(_owner_signing_keys_observation(now_text)))

    assert (
        subject.main(
            [
                "publish-dispatch-admission",
                "--reconciliation",
                str(reconciliation_path),
                "--containment",
                str(containment_path),
                "--owner-key-observation",
                str(owner_keys_path),
                "--output",
                str(admission_path),
            ]
        )
        == 1
    )
    assert not admission_path.exists()
    assert (
        subject.main(
            [
                "publish-dispatch-tombstone",
                "--reconciliation",
                str(reconciliation_path),
                "--reason",
                "dispatch_ambiguous",
                "--output",
                str(tombstone_path),
            ]
        )
        == 1
    )
    assert not tombstone_path.exists()


def test_pinned_github_transport_sends_body_once_and_keeps_token_internal() -> None:
    token = b"github_pat_exact_dispatch_secret"
    connection = FakeHTTPSConnection(response=FakeHTTPResponse(status=200, body=_response_body()))
    factory_calls: list[tuple[str, float]] = []

    def factory(host: str, context: ssl.SSLContext, timeout: float) -> subject.HTTPSConnectionLike:
        assert context.check_hostname is True
        factory_calls.append((host, timeout))
        return connection

    transport = subject.PinnedGitHubTransport(
        token=token,
        connection_factory=factory,
    )
    body = b'{"inputs":{},"ref":"main"}'
    exchange = transport(
        "https://api.github.com/repos/John-MiracleWorker/Kestrel/actions/workflows/707/dispatches",
        {
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2026-03-10",
        },
        body,
        subject.OneWirePolicy(),
    )

    assert factory_calls == [("api.github.com", 30.0)]
    assert connection.requests == [
        (
            "POST",
            "/repos/John-MiracleWorker/Kestrel/actions/workflows/707/dispatches",
            False,
            True,
        )
    ]
    assert connection.sent == [body]
    assert connection.closed is True
    assert ("Authorization", (f"Bearer {token.decode()}",)) in connection.headers
    assert exchange.request_may_have_reached_peer is True
    assert token not in (exchange.response_headers or b"")
    assert token not in (exchange.response_body or b"")


@pytest.mark.parametrize(
    "target",
    [
        "GET /repos/John-MiracleWorker/Kestrel-evil",
        "GET /repos/John-MiracleWorker/KestrelReleaseRecovery",
    ],
)
def test_prerequisite_reader_rejects_repository_prefix_confusion(target: str) -> None:
    with pytest.raises(ValueError, match="pinned origin"):
        subject.DirectGitHubReadAPI._validate_request_target(target)  # noqa: SLF001


def test_prerequisite_reader_follows_one_pinned_asset_redirect_without_auth() -> None:
    class Response:
        def __init__(
            self, *, status: int, body: bytes, headers: list[tuple[str, str]]
        ) -> None:
            self.status = status
            self.body = body
            self.headers = headers

        def getheaders(self) -> list[tuple[str, str]]:
            return self.headers

        def read(self, amount: int | None = None) -> bytes:
            assert amount == receipts.MAX_SOURCE_BODY_BYTES + 1
            return self.body

    class Connection:
        def __init__(self, response: Response) -> None:
            self.response = response
            self.requests: list[tuple[str, str]] = []
            self.headers: list[tuple[str, str]] = []
            self.closed = False

        def connect(self) -> None:
            return None

        def putrequest(
            self,
            method: str,
            target: str,
            skip_host: bool = False,
            skip_accept_encoding: bool = False,
        ) -> None:
            assert skip_host is False
            assert skip_accept_encoding is True
            self.requests.append((method, target))

        def putheader(self, name: str, value: str) -> None:
            self.headers.append((name, value))

        def endheaders(self) -> None:
            return None

        def getresponse(self) -> Response:
            return self.response

        def close(self) -> None:
            self.closed = True

    location = (
        "https://release-assets.githubusercontent.com/"
        "github-production-release-asset/1/example?sp=r&sig=exact"
    )
    api_connection = Connection(
        Response(status=302, body=b"", headers=[("Location", location)])
    )
    asset_connection = Connection(Response(status=200, body=b"authority", headers=[]))
    calls: list[str] = []

    def factory(host: str, context: ssl.SSLContext, timeout: float) -> Connection:
        assert context.check_hostname is True
        assert timeout == 30.0
        calls.append(host)
        return api_connection if host == "api.github.com" else asset_connection

    api = subject.DirectGitHubReadAPI(
        token=b"qualified-reader-token",
        connection_factory=factory,
    )

    exchange = api(
        "GET /repos/John-MiracleWorker/Kestrel-Release-Recovery/"
        "releases/assets/101",
        accept="application/octet-stream",
    )

    assert exchange.http_status == 200
    assert exchange.response_body == b"authority"
    assert calls == ["api.github.com", "release-assets.githubusercontent.com"]
    assert any(name == "Authorization" for name, _value in api_connection.headers)
    assert all(name != "Authorization" for name, _value in asset_connection.headers)
    assert asset_connection.requests == [
        (
            "GET",
            "/github-production-release-asset/1/example?sp=r&sig=exact",
        )
    ]
    assert api_connection.closed is True
    assert asset_connection.closed is True


def test_ghcr_reader_follows_one_pinned_blob_redirect_without_auth() -> None:
    body = b"exact GHCR object bytes"
    digest = _sha256(body)

    class Response:
        def __init__(
            self, *, status: int, body: bytes, headers: list[tuple[str, str]]
        ) -> None:
            self.status = status
            self._body = io.BytesIO(body)
            self._headers = headers

        def getheaders(self) -> list[tuple[str, str]]:
            return self._headers

        def read(self, amount: int | None = None) -> bytes:
            return self._body.read() if amount is None else self._body.read(amount)

    class Connection:
        def __init__(self, response: Response) -> None:
            self.response = response
            self.requests: list[tuple[str, str]] = []
            self.headers: list[tuple[str, str]] = []
            self.closed = False

        def connect(self) -> None:
            return None

        def putrequest(
            self,
            method: str,
            target: str,
            skip_host: bool = False,
            skip_accept_encoding: bool = False,
        ) -> None:
            assert skip_host is False
            assert skip_accept_encoding is True
            self.requests.append((method, target))

        def putheader(self, name: str, value: str) -> None:
            self.headers.append((name, value))

        def endheaders(self) -> None:
            return None

        def getresponse(self) -> Response:
            return self.response

        def close(self) -> None:
            self.closed = True

    location = (
        "https://pkg-containers.githubusercontent.com/ghcrblobs01/blobs/"
        f"{digest}?se=exact&sig=exact&sp=r&spr=https&sr=b&sv=2025"
    )
    registry_connection = Connection(
        Response(
            status=307,
            body=b"",
            headers=[("Location", location)],
        )
    )
    storage_connection = Connection(Response(status=200, body=body, headers=[]))
    calls: list[str] = []

    def factory(host: str, context: ssl.SSLContext, timeout: float) -> Connection:
        assert context.check_hostname is True
        assert timeout == 30.0
        calls.append(host)
        return registry_connection if host == "ghcr.io" else storage_connection

    api = subject.DirectOCIReadAPI(
        token=b"exact-registry-bearer",
        connection_factory=factory,
    )

    observation = api.read_digest(kind="blobs", digest=digest, max_bytes=1024)

    assert observation == subject.OCIReadObservation(
        http_status=200,
        observed_digest=digest,
        size_bytes=len(body),
    )
    assert calls == ["ghcr.io", "pkg-containers.githubusercontent.com"]
    assert any(name == "Authorization" for name, _value in registry_connection.headers)
    assert all(name != "Authorization" for name, _value in storage_connection.headers)
    assert registry_connection.closed is True
    assert storage_connection.closed is True


def test_ghcr_reader_rejects_unpinned_redirect_before_replaying_auth() -> None:
    digest = "sha256:" + "a" * 64

    with pytest.raises(ValueError, match="pinned storage origin"):
        subject.DirectOCIReadAPI._redirect_target(  # noqa: SLF001
            (
                "https://attacker.example/ghcrblobs14/blobs/"
                f"{digest}?se=x&sig=bad&sp=r&spr=https&sr=b&sv=2025"
            ),
            digest=digest,
        )


def test_ghcr_writer_sends_auth_and_body_only_to_the_pinned_registry() -> None:
    blob = b"exact candidate blob"
    blob_digest = _sha256(blob)
    manifest = b'{"mediaType":"application/vnd.oci.image.manifest.v1+json"}'
    manifest_digest = _sha256(manifest)

    class Response:
        def __init__(self, status: int, headers: list[tuple[str, str]]) -> None:
            self.status = status
            self._headers = headers

        def getheaders(self) -> list[tuple[str, str]]:
            return self._headers

        def read(self, amount: int | None = None) -> bytes:
            assert amount == subject.MAX_TRANSPORT_RESPONSE_BYTES + 1
            return b""

    class Connection:
        def __init__(self, response: Response) -> None:
            self.response = response
            self.requests: list[tuple[str, str]] = []
            self.headers: list[tuple[str, str]] = []
            self.sent: list[bytes] = []
            self.closed = False

        def connect(self) -> None:
            return None

        def putrequest(
            self,
            method: str,
            target: str,
            skip_host: bool = False,
            skip_accept_encoding: bool = False,
        ) -> None:
            assert skip_host is False
            assert skip_accept_encoding is True
            self.requests.append((method, target))

        def putheader(self, name: str, value: str) -> None:
            self.headers.append((name, value))

        def endheaders(
            self, message_body: bytes | None = None, *, encode_chunked: bool = False
        ) -> None:
            assert message_body is None
            assert encode_chunked is False

        def send(self, data: bytes) -> None:
            self.sent.append(data)

        def getresponse(self) -> Response:
            return self.response

        def close(self) -> None:
            self.closed = True

    connections = iter(
        (
            Connection(
                Response(
                    202,
                    [
                        (
                            "Location",
                            "https://ghcr.io/v2/john-miracleworker/kestrel/"
                            "blobs/uploads/exact-upload?_state=exact",
                        )
                    ],
                )
            ),
            Connection(
                Response(201, [("Docker-Content-Digest", blob_digest)])
            ),
            Connection(
                Response(201, [("Docker-Content-Digest", manifest_digest)])
            ),
        )
    )
    created: list[tuple[str, Connection]] = []

    def factory(host: str, context: ssl.SSLContext, timeout: float) -> Connection:
        assert host == "ghcr.io"
        assert context.check_hostname is True
        assert timeout == 30.0
        connection = next(connections)
        created.append((host, connection))
        return connection

    writer = subject.DirectOCIWriteAPI(
        token=b"exact-registry-bearer",
        connection_factory=factory,
    )

    writer.upload_blob(digest=blob_digest, content=blob)
    writer.put_manifest(
        digest=manifest_digest,
        media_type="application/vnd.oci.image.manifest.v1+json",
        content=manifest,
    )

    assert [host for host, _connection in created] == ["ghcr.io"] * 3
    assert created[0][1].requests == [
        ("POST", "/v2/john-miracleworker/kestrel/blobs/uploads/")
    ]
    assert created[1][1].requests[0][0] == "PUT"
    assert "digest=sha256%3A" in created[1][1].requests[0][1]
    assert created[1][1].sent == [blob]
    assert created[2][1].sent == [manifest]
    assert all(
        any(name == "Authorization" for name, _value in connection.headers)
        for _host, connection in created
    )


def test_ghcr_writer_rejects_an_unpinned_upload_location_before_auth_replay() -> None:
    digest = "sha256:" + "a" * 64

    with pytest.raises(ValueError, match="pinned registry origin"):
        subject.DirectOCIWriteAPI._upload_target(  # noqa: SLF001
            "https://attacker.example/v2/john-miracleworker/kestrel/"
            "blobs/uploads/exact?_state=bad",
            digest=digest,
        )


def test_ghcr_push_token_uses_the_bound_workflow_actor_principal() -> None:
    credential = b"github-token-at-least-twenty-bytes"
    connection = FakeHTTPSConnection(
        response=FakeHTTPResponse(
            status=200,
            body=_canonical({"token": "registry-bearer-token"}),
        )
    )

    token = subject.fetch_ghcr_push_token(
        credential,
        principal="kestrel-release-dispatcher[bot]",
        connection_factory=lambda host, context, timeout: connection,
    )

    assert token == "registry-bearer-token"
    assert connection.requests == [
        (
            "GET",
            "/token?service=ghcr.io&scope=repository:"
            "john-miracleworker/kestrel:pull,push",
            False,
            True,
        )
    ]
    authorization = next(
        values for name, values in connection.headers if name == "Authorization"
    )[0]
    assert base64.b64decode(authorization.removeprefix("Basic ")) == (
        b"kestrel-release-dispatcher[bot]:" + credential
    )


def test_prerequisite_pagination_consumes_the_complete_pinned_link_chain() -> None:
    calls: list[str] = []
    next_url = (
        "https://api.github.com/users/John-MiracleWorker/"
        "ssh_signing_keys?page=2&per_page=100"
    )

    def api(request_target: str, *, accept: str) -> subject.GitHubReadExchange:
        assert accept == "application/vnd.github+json"
        calls.append(request_target)
        if len(calls) == 1:
            return subject.GitHubReadExchange(
                http_status=200,
                response_headers=(("link", f'<{next_url}>; rel="next"'),),
                response_body=_canonical([{"id": 1}]),
            )
        return subject.GitHubReadExchange(
            http_status=200,
            response_headers=(),
            response_body=_canonical([{"id": 2}]),
        )

    raw, items = subject._boundary_paginated_source(  # noqa: SLF001
        api,
        request_target="GET /users/John-MiracleWorker/ssh_signing_keys?per_page=100",
        locator="GET /users/John-MiracleWorker/ssh_signing_keys?per_page=100",
        label="owner signing keys",
    )

    assert calls == [
        "GET /users/John-MiracleWorker/ssh_signing_keys?per_page=100",
        "GET /users/John-MiracleWorker/ssh_signing_keys?page=2&per_page=100",
    ]
    assert items == [{"id": 1}, {"id": 2}]
    pages = json.loads(raw)["pages"]
    assert [page["request_url"] for page in pages] == [
        "GET /users/John-MiracleWorker/ssh_signing_keys?per_page=100",
        next_url,
    ]


def test_boundary_authority_fetch_polls_then_downloads_exact_immutable_assets(
    tmp_path: Path,
) -> None:
    expected_assets = {
        "approval-history-observation.json": b"approval",
        "owner-signing-keys-observation.json": b"keys",
        **{
            name: name.encode("ascii")
            for name in subject._BOUNDARY_CREDENTIAL_ASSETS  # noqa: SLF001
        },
    }
    assets = [
        {
            "id": 100 + index,
            "name": name,
            "size": len(body),
        }
        for index, (name, body) in enumerate(sorted(expected_assets.items()))
    ]
    release = _canonical(
        {
            "tag_name": "release-preparation-authority-707-1",
            "name": "release-preparation-authority-707-1",
            "body": "Kestrel prepare authority for promotion run 707",
            "draft": False,
            "prerelease": False,
            "immutable": True,
            "assets": assets,
        }
    )
    calls: list[tuple[str, str]] = []
    release_polls = 0

    def api(request_target: str, *, accept: str) -> subject.GitHubReadExchange:
        nonlocal release_polls
        calls.append((request_target, accept))
        if request_target == "GET /user":
            return subject.GitHubReadExchange(
                http_status=200,
                response_headers=(),
                response_body=_canonical(
                    {"login": "John-MiracleWorker", "id": 58918509, "type": "User"}
                ),
            )
        if request_target == (
            "GET /repos/John-MiracleWorker/Kestrel-Release-Recovery"
        ):
            return subject.GitHubReadExchange(
                http_status=200,
                response_headers=(),
                response_body=_canonical(
                    {
                        "id": 808,
                        "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
                        "private": True,
                        "visibility": "private",
                        "archived": False,
                        "disabled": False,
                        "owner": {
                            "login": "John-MiracleWorker",
                            "id": 58918509,
                            "type": "User",
                        },
                    }
                ),
            )
        if "/releases/tags/" in request_target:
            release_polls += 1
            return subject.GitHubReadExchange(
                http_status=404 if release_polls == 1 else 200,
                response_headers=(),
                response_body=b"{}" if release_polls == 1 else release,
            )
        asset_id = int(request_target.rsplit("/", 1)[1])
        asset = next(item for item in assets if item["id"] == asset_id)
        return subject.GitHubReadExchange(
            http_status=200,
            response_headers=(),
            response_body=expected_assets[asset["name"]],
        )

    sleeps: list[float] = []
    ticks = iter((0.0, 0.0, 5.0))
    output = tmp_path / "authority"
    written = subject.fetch_github_boundary_authority(
        boundary="prepare",
        run_id=707,
        output_dir=output,
        api=api,
        timeout_seconds=30.0,
        poll_interval_seconds=5.0,
        _monotonic=lambda: next(ticks),
        _sleep=sleeps.append,
    )

    assert written == tuple(sorted(expected_assets))
    assert sleeps == [5.0]
    assert {path.name: path.read_bytes() for path in output.iterdir()} == expected_assets
    assert calls[1] == calls[2]
    assert all(accept == "application/octet-stream" for _, accept in calls[3:])


def _write_boundary_authority_asset_root(root: Path, *, boundary: str) -> None:
    root.mkdir()
    _tag_prefix, authority_stem, extra_assets = subject._BOUNDARY_AUTHORITY_RELEASES[  # noqa: SLF001
        boundary
    ]
    names = {
        "owner-signing-keys-observation.json",
        *extra_assets,
    }
    if authority_stem is not None:
        names.update({f"{authority_stem}.json", f"{authority_stem}.json.sig"})
    for name in names:
        body = b"signature" if name.endswith(".sig") else b"{}"
        (root / name).write_bytes(body)


def test_boundary_authority_publisher_creates_one_exact_immutable_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_root = tmp_path / "assets"
    _write_boundary_authority_asset_root(asset_root, boundary="final")
    api = FakeTerminalReleaseAPI()
    verification_calls: list[str] = []

    def verify_runtime_credential(**kwargs: object) -> dict[str, object]:
        token = kwargs["token_bytes"]
        assert type(token) is bytes
        verification_calls.append(token.decode("ascii"))
        return {"validation_status": "validated"}

    monkeypatch.setattr(receipts, "verify_runtime_credential", verify_runtime_credential)
    journal = tmp_path / "boundary-publication.json"
    reader_calls: list[str] = []

    def recovery_reader(
        request_target: str, *, accept: str
    ) -> subject.GitHubReadExchange:
        reader_calls.append(request_target)
        if request_target == "GET /user":
            return subject.GitHubReadExchange(
                http_status=200,
                response_headers=(),
                response_body=_canonical(
                    {"login": "John-MiracleWorker", "id": 58918509, "type": "User"}
                ),
            )
        if request_target == (
            "GET /repos/John-MiracleWorker/Kestrel-Release-Recovery"
        ):
            return subject.GitHubReadExchange(
                http_status=200,
                response_headers=(),
                response_body=_canonical(
                    {
                        "id": 808,
                        "full_name": "John-MiracleWorker/Kestrel-Release-Recovery",
                        "private": True,
                        "visibility": "private",
                        "archived": False,
                        "disabled": False,
                        "owner": {
                            "login": "John-MiracleWorker",
                            "id": 58918509,
                            "type": "User",
                        },
                    }
                ),
            )
        if "/releases/tags/" in request_target:
            if not api.releases:
                return subject.GitHubReadExchange(
                    http_status=404,
                    response_headers=(),
                    response_body=b"{}",
                )
            release = api.releases[0]
            return subject.GitHubReadExchange(
                http_status=200,
                response_headers=(),
                response_body=_canonical(
                    {
                        "tag_name": release.tag_name,
                        "name": release.name,
                        "body": release.body,
                        "draft": release.draft,
                        "prerelease": release.prerelease,
                        "immutable": release.immutable,
                        "assets": [
                            {
                                "id": asset.asset_id,
                                "name": asset.name,
                                "size": asset.size_bytes,
                            }
                            for asset in release.assets
                        ],
                    }
                ),
            )
        asset_id = int(request_target.rsplit("/", 1)[1])
        asset = next(
            asset for asset in api.releases[0].assets if asset.asset_id == asset_id
        )
        return subject.GitHubReadExchange(
            http_status=200,
            response_headers=(),
            response_body=(asset_root / asset.name).read_bytes(),
        )

    receipt = subject.publish_github_boundary_authority(
        boundary="final",
        run_id=707,
        candidate_manifest_digest="sha256:" + "a" * 64,
        environment_id=505,
        asset_root=asset_root,
        journal_path=journal,
        credential_tokens={
            "recovery-reader": b"recovery-reader-token",
            "release-guard": b"release-guard-token",
        },
        api=api,
        recovery_reader_api=recovery_reader,
    )

    assert verification_calls == [
        "recovery-reader-token",
        "release-guard-token",
    ]
    assert receipt["tag_name"] == "release-final-authority-707-1"
    assert receipt["immutable"] is True
    assert api.create_calls == 1
    assert api.publish_calls == 1
    assert api.upload_calls == sorted(path.name for path in asset_root.iterdir())
    assert journal.is_file()
    assert reader_calls[0] == "GET /user"
    assert len(reader_calls) == len(tuple(asset_root.iterdir())) + 5

    replay = subject.publish_github_boundary_authority(
        boundary="final",
        run_id=707,
        candidate_manifest_digest="sha256:" + "a" * 64,
        environment_id=505,
        asset_root=asset_root,
        journal_path=journal,
        credential_tokens={
            "recovery-reader": b"recovery-reader-token",
            "release-guard": b"release-guard-token",
        },
        api=api,
        recovery_reader_api=recovery_reader,
    )

    assert replay == receipt
    assert api.create_calls == 1
    assert api.publish_calls == 1
    assert api.upload_calls == sorted(path.name for path in asset_root.iterdir())


def test_boundary_authority_publisher_rejects_incomplete_assets_before_mutation(
    tmp_path: Path,
) -> None:
    asset_root = tmp_path / "assets"
    _write_boundary_authority_asset_root(asset_root, boundary="final")
    (asset_root / "release-guard-endpoint-probes.json").unlink()
    api = FakeTerminalReleaseAPI()

    with pytest.raises(ValueError, match="asset inventory"):
        subject.publish_github_boundary_authority(
            boundary="final",
            run_id=707,
            candidate_manifest_digest="sha256:" + "a" * 64,
            environment_id=505,
            asset_root=asset_root,
            journal_path=tmp_path / "boundary-publication.json",
            credential_tokens={
                "recovery-reader": b"recovery-reader-token",
                "release-guard": b"release-guard-token",
            },
            api=api,
            recovery_reader_api=lambda request_target, *, accept: subject.GitHubReadExchange(
                http_status=500,
                response_headers=(),
                response_body=b"{}",
            ),
        )

    assert api.create_calls == 0
    assert api.upload_calls == []
    assert api.publish_calls == 0


def test_pypi_boundary_publisher_verifies_pypi_authority_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_root = tmp_path / "assets"
    _write_boundary_authority_asset_root(asset_root, boundary="pypi")
    api = FakeTerminalReleaseAPI()
    monkeypatch.setattr(
        receipts,
        "verify_github_authority",
        lambda **_: {"validation_status": "validated"},
    )
    monkeypatch.setattr(
        receipts,
        "verify_runtime_credential",
        lambda **_: {"validation_status": "validated"},
    )
    monkeypatch.setattr(
        receipts,
        "verify_pypi_authority",
        lambda **_: (_ for _ in ()).throw(ValueError("untrusted PyPI authority")),
    )

    with pytest.raises(ValueError, match="untrusted PyPI authority"):
        subject.publish_github_boundary_authority(
            boundary="pypi",
            run_id=707,
            candidate_manifest_digest="sha256:" + "a" * 64,
            environment_id=505,
            asset_root=asset_root,
            journal_path=tmp_path / "boundary-publication.json",
            credential_tokens={
                "recovery-reader": b"recovery-reader-token",
                "release-guard": b"release-guard-token",
            },
            api=api,
            recovery_reader_api=lambda request_target, *, accept: subject.GitHubReadExchange(
                http_status=500,
                response_headers=(),
                response_body=b"{}",
            ),
        )

    assert api.create_calls == 0
    assert api.upload_calls == []
    assert api.publish_calls == 0


def test_boundary_authority_publisher_requires_independent_reader_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_root = tmp_path / "assets"
    _write_boundary_authority_asset_root(asset_root, boundary="final")
    api = FakeTerminalReleaseAPI()
    monkeypatch.setattr(
        receipts,
        "verify_github_authority",
        lambda **_: {"validation_status": "validated"},
    )
    monkeypatch.setattr(
        receipts,
        "verify_runtime_credential",
        lambda **_: {"validation_status": "validated"},
    )

    with pytest.raises(ValueError, match="recovery reader identity"):
        subject.publish_github_boundary_authority(
            boundary="final",
            run_id=707,
            candidate_manifest_digest="sha256:" + "a" * 64,
            environment_id=505,
            asset_root=asset_root,
            journal_path=tmp_path / "boundary-publication.json",
            credential_tokens={
                "recovery-reader": b"recovery-reader-token",
                "release-guard": b"release-guard-token",
            },
            api=api,
            recovery_reader_api=lambda request_target, *, accept: (
                subject.GitHubReadExchange(
                    http_status=403,
                    response_headers=(),
                    response_body=b"{}",
                )
            ),
        )

    assert api.create_calls == 0
    assert api.publish_calls == 0


def test_boundary_authority_publisher_preflights_reader_repository_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_root = tmp_path / "assets"
    _write_boundary_authority_asset_root(asset_root, boundary="final")
    api = FakeTerminalReleaseAPI()
    monkeypatch.setattr(
        receipts,
        "verify_github_authority",
        lambda **_: {"validation_status": "validated"},
    )
    monkeypatch.setattr(
        receipts,
        "verify_runtime_credential",
        lambda **_: {"validation_status": "validated"},
    )

    def reader(request_target: str, *, accept: str) -> subject.GitHubReadExchange:
        if request_target == "GET /user":
            return subject.GitHubReadExchange(
                http_status=200,
                response_headers=(),
                response_body=_canonical(
                    {"login": "John-MiracleWorker", "id": 58918509, "type": "User"}
                ),
            )
        return subject.GitHubReadExchange(
            http_status=404,
            response_headers=(),
            response_body=b"{}",
        )

    with pytest.raises(ValueError, match="repository access"):
        subject.publish_github_boundary_authority(
            boundary="final",
            run_id=707,
            candidate_manifest_digest="sha256:" + "a" * 64,
            environment_id=505,
            asset_root=asset_root,
            journal_path=tmp_path / "boundary-publication.json",
            credential_tokens={
                "recovery-reader": b"recovery-reader-token",
                "release-guard": b"release-guard-token",
            },
            api=api,
            recovery_reader_api=reader,
        )

    assert api.create_calls == 0
    assert api.publish_calls == 0


def test_boundary_authority_publisher_is_exposed_as_a_controller_command() -> None:
    parser = subject._parser()  # noqa: SLF001

    arguments = parser.parse_args(
        [
            "publish-github-boundary-authority",
            "--boundary",
            "commit",
            "--run-id",
            "707",
            "--candidate-manifest-digest",
            "sha256:" + "a" * 64,
            "--environment-id",
            "505",
            "--asset-root",
            "authority-assets",
            "--journal",
            "authority-publication.json",
            "--output",
            "authority-publication-receipt.json",
        ]
    )

    assert arguments.handler is subject._command_publish_github_boundary_authority  # noqa: SLF001


@pytest.mark.parametrize(
    ("fail_at", "may_have_reached"),
    [("connect", False), ("endheaders", True)],
)
def test_pinned_transport_reports_exact_possible_write_boundary(
    fail_at: str, may_have_reached: bool
) -> None:
    connection = FakeHTTPSConnection(
        response=FakeHTTPResponse(status=500, body=b"{}"),
        fail_at=fail_at,
    )
    transport = subject.PinnedGitHubTransport(
        token=b"exact-token",
        connection_factory=lambda host, context, timeout: connection,
    )
    with pytest.raises(subject.DispatchTransportError) as failure:
        transport(
            "https://api.github.com/repos/John-MiracleWorker/Kestrel/"
            "actions/workflows/707/dispatches",
            {
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            b"{}",
            subject.OneWirePolicy(),
        )
    assert failure.value.request_may_have_reached_peer is may_have_reached
    assert "exact-token" not in str(failure.value)
    assert connection.sent == []
    assert connection.closed is True


def test_redirect_response_is_not_followed_and_enters_reconciliation(
    tmp_path: Path,
) -> None:
    journal, request, result, _ = _prepared_files(tmp_path)
    token = b"exact-token-never-recorded"
    connection = FakeHTTPSConnection(
        response=FakeHTTPResponse(status=307, body=b'{"location":"elsewhere"}')
    )
    transport = subject.PinnedGitHubTransport(
        token=token,
        connection_factory=lambda host, context, timeout: connection,
    )

    record = subject.send_dispatch_once(
        journal_path=journal,
        request_path=request,
        response_output=result,
        transport=transport,
        credential_fingerprint=transport.token_fingerprint,
        _clock=lambda: datetime(2026, 8, 13, 20, 0, 1, tzinfo=UTC),
        _monotonic=lambda: 101.0,
        **_writer_inventory_arguments(tmp_path, phase="pre_send"),
    )
    assert record["classification"] == "outcome_unknown"
    assert len(connection.requests) == 1
    assert len(connection.sent) == 1
    assert token not in result.read_bytes()
    assert token not in journal.read_bytes()
    assert token not in request.read_bytes()


@pytest.mark.parametrize(
    "token",
    [b"", b"secret\n", b"secret with spaces", b"x" * 4097],
)
def test_pinned_transport_rejects_unsafe_token_bytes(token: bytes) -> None:
    with pytest.raises(ValueError, match="credential"):
        subject.PinnedGitHubTransport(token=token)


def test_send_dispatch_cli_has_no_credential_argument() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "release_promotion_transaction.py"),
            "send-dispatch",
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "--token" not in result.stdout
    assert "--secret" not in result.stdout
    assert "--authorization" not in result.stdout


def test_promotion_cli_exposes_the_exact_transaction_command_set() -> None:
    parser = subject._parser()
    command_action = next(action for action in parser._actions if action.dest == "command")
    assert command_action.choices is not None
    assert set(command_action.choices) == {
        "authorize",
        "capture-prerequisite-boundary",
        "contain-dispatch",
        "create-dispatch-intent",
        "fetch-github-boundary-authority",
        "inspect-prerequisites",
        "plan-commit",
        "plan-preparation",
        "prepare-dispatch",
        "publish-github-boundary-authority",
        "publish-dispatch-admission",
        "publish-dispatch-tombstone",
        "reconcile",
        "reconcile-dispatch",
        "record-commit",
        "record-preparation",
        "record-pypi",
        "send-dispatch",
        "tag-message",
        "verify-github-ghcr",
        "verify-github-boundary-binding",
        "verify-recovery-capsule",
    }


def test_contain_dispatch_cli_requires_composite_uninstall_proof(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    prepared_at = now - timedelta(seconds=4)
    send_at = now - timedelta(seconds=3)
    uninstall_at = now - timedelta(seconds=2)
    probe_at = now - timedelta(seconds=1)

    def stamp(value: datetime) -> str:
        return value.strftime("%Y-%m-%dT%H:%M:%SZ")

    journal, request, response, boundary = _prepared_files(tmp_path, prepared_at=prepared_at)
    bundle_path = tmp_path / "uninstall-bundle.json"
    probe_path = tmp_path / "token-probe.json"
    output_path = tmp_path / "containment.json"
    bundle = {
        "schema": "kestrel.dispatcher_uninstall_bundle.v1",
        "installed_apps_snapshot": {
            "schema": "kestrel.installed_apps_snapshot.v1",
            "apps": [],
            "captured_at": stamp(now),
            "complete": True,
        },
        "uninstall": {
            "schema": "kestrel.dispatcher_uninstall_observation.v1",
            "app_id": 909,
            "installation_id": 1001,
            "uninstalled_at": stamp(uninstall_at),
            "complete": True,
        },
    }
    probe = {
        "schema": "kestrel.dispatcher_token_probe.v1",
        "endpoint": "GET /installation/repositories",
        "http_status": 401,
        "observed_at": stamp(probe_at),
        "response_sha256": "sha256:" + "5" * 64,
        "token_fingerprint": DISPATCH_TOKEN_FINGERPRINT,
        "complete": True,
    }
    bundle_path.write_bytes(_canonical(bundle))
    probe_path.write_bytes(_canonical(probe))
    post_writer = _writer_inventory_arguments(
        tmp_path,
        phase="post_containment",
        captured_at=stamp(now),
        nonce_run_ids=[1101],
    )
    writer_inventory_path = tmp_path / "post-containment-writers.json"
    writer_signature_path = tmp_path / "post-containment-writers.json.sig"
    writer_owner_keys_path = tmp_path / "post-containment-owner-keys.json"
    writer_inventory_path.write_bytes(post_writer["writer_inventory"])
    writer_signature_path.write_bytes(post_writer["writer_inventory_signature"])
    writer_owner_keys_path.write_bytes(post_writer["owner_signing_keys_observation"])

    command = [
        "contain-dispatch",
        "--journal",
        str(journal),
        "--response",
        str(response),
        "--uninstall-observation",
        str(bundle_path),
        "--token-probe-observation",
        str(probe_path),
        "--writer-inventory",
        str(writer_inventory_path),
        "--writer-inventory-signature",
        str(writer_signature_path),
        "--owner-key-observation",
        str(writer_owner_keys_path),
        "--output",
        str(output_path),
    ]

    assert subject.main(command) == 1
    assert not output_path.exists()

    transport = RecordingTransport(
        boundary=boundary,
        result=subject.DispatchExchange(
            http_status=None,
            response_headers=None,
            response_body=None,
            request_may_have_reached_peer=True,
        ),
    )
    subject.send_dispatch_once(
        journal_path=journal,
        request_path=request,
        response_output=response,
        transport=transport,
        credential_fingerprint=DISPATCH_TOKEN_FINGERPRINT,
        _clock=lambda: send_at,
        _monotonic=lambda: 101.0,
        **_writer_inventory_arguments(
            tmp_path,
            phase="pre_send",
            captured_at=stamp(prepared_at),
        ),
    )

    assert subject.main(command) == 0
    containment = json.loads(output_path.read_bytes())
    assert containment["validated"] is True
    assert containment["installation_id"] == 1001
