#!/usr/bin/env python3
"""Build and publish one exact authority-bound release recovery capsule."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import platform
import shutil
import subprocess  # nosec B404
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import (  # noqa: E402
    bootstrap_recovery,
    recovery_launcher,
)
from scripts import (  # noqa: E402
    bootstrap_recovery_capsule_controller as controller_bootstrap,
)
from scripts import release_candidate_manifest as candidates  # noqa: E402
from scripts import release_control_receipt as receipts  # noqa: E402
from scripts import release_promotion_transaction as transaction  # noqa: E402

REPOSITORY = "John-MiracleWorker/Kestrel"
RECOVERY_REPOSITORY = "John-MiracleWorker/Kestrel-Release-Recovery"
PREPARE_AUTHORITY_ASSETS = frozenset(
    {
        "approval-history-observation.json",
        "recovery-capsule-publication.json",
        "recovery-capsule-verification.json",
    }
)


class ActionsArtifactAPI(Protocol):
    """Bounded observation and exact-ID download surface for Actions artifacts."""

    def get_workflow_run(self, run_id: int) -> bytes: ...

    def list_run_artifacts(self, run_id: int) -> bytes: ...

    def get_artifact(self, artifact_id: int) -> bytes: ...

    def download_artifact(self, artifact_id: int, destination: Path) -> None: ...


CommandRunner = Callable[..., subprocess.CompletedProcess[bytes]]


class GitHubActionsArtifactAPI:
    """Pinned-GitHub-CLI transport with exact-ID, noninteractive artifact reads."""

    def __init__(
        self,
        *,
        pinned_gh: Path,
        token: bytes,
        runner: CommandRunner = subprocess.run,
    ) -> None:
        if (
            not pinned_gh.is_absolute()
            or not pinned_gh.is_file()
            or pinned_gh.is_symlink()
            or not os.access(pinned_gh, os.X_OK)
        ):
            raise receipts.ReleaseControlError("recovery controller GitHub CLI path is invalid")
        if (
            type(token) is not bytes
            or not token
            or len(token) > transaction.MAX_DISPATCH_TOKEN_BYTES
            or any(byte < 0x21 or byte > 0x7E for byte in token)
        ):
            raise receipts.ReleaseControlError(
                "recovery controller Actions credential bytes are invalid"
            )
        receipts._verify_pinned_gh(pinned_gh)  # noqa: SLF001
        self._gh = pinned_gh
        self._token = token.decode("ascii")
        self._runner = runner

    def _command(self, endpoint: str, *, paginate: bool = False) -> list[str]:
        command = [
            str(self._gh),
            "api",
            "--method",
            "GET",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            f"X-GitHub-Api-Version: {receipts.DISPATCH_API_VERSION}",
        ]
        if paginate:
            command.extend(("--paginate", "--slurp"))
        command.append(endpoint)
        return command

    def _environment(self) -> dict[str, str]:
        return {
            "GH_TOKEN": self._token,
            "GH_PROMPT_DISABLED": "1",
            "NO_COLOR": "1",
        }

    def _metadata(self, endpoint: str, *, paginate: bool = False) -> bytes:
        completed = self._runner(  # noqa: S603  # nosec B603
            self._command(endpoint, paginate=paginate),
            capture_output=True,
            check=False,
            timeout=30,
            env=self._environment(),
        )
        if completed.returncode != 0:
            raise receipts.ReleaseControlError(
                "recovery controller Actions metadata request failed"
            )
        if len(completed.stdout) > candidates.MAX_ACTIONS_OBSERVATION_BYTES:
            raise receipts.ReleaseControlError(
                "recovery controller Actions metadata response is too large"
            )
        return completed.stdout

    def get_workflow_run(self, run_id: int) -> bytes:
        checked = receipts._safe_integer(  # noqa: SLF001
            run_id, label="recovery controller workflow run ID", positive=True
        )
        return self._metadata(f"/repos/{REPOSITORY}/actions/runs/{checked}")

    def list_run_artifacts(self, run_id: int) -> bytes:
        checked = receipts._safe_integer(  # noqa: SLF001
            run_id, label="recovery controller workflow run ID", positive=True
        )
        return self._metadata(
            f"/repos/{REPOSITORY}/actions/runs/{checked}/artifacts?per_page=100",
            paginate=True,
        )

    def get_artifact(self, artifact_id: int) -> bytes:
        checked = receipts._safe_integer(  # noqa: SLF001
            artifact_id, label="recovery controller artifact ID", positive=True
        )
        return self._metadata(f"/repos/{REPOSITORY}/actions/artifacts/{checked}")

    def download_artifact(self, artifact_id: int, destination: Path) -> None:
        checked = receipts._safe_integer(  # noqa: SLF001
            artifact_id, label="recovery controller artifact ID", positive=True
        )
        if destination.exists() or destination.is_symlink():
            raise receipts.ReleaseControlError(
                "recovery controller artifact archive path already exists"
            )
        if not destination.parent.is_dir() or destination.parent.is_symlink():
            raise receipts.ReleaseControlError(
                "recovery controller artifact archive parent is invalid"
            )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(destination, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                completed = self._runner(  # noqa: S603  # nosec B603
                    self._command(f"/repos/{REPOSITORY}/actions/artifacts/{checked}/zip"),
                    stdout=output,
                    stderr=subprocess.PIPE,
                    check=False,
                    timeout=300,
                    env=self._environment(),
                )
                output.flush()
                os.fsync(output.fileno())
            if completed.returncode != 0:
                raise receipts.ReleaseControlError("recovery controller artifact download failed")
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)


@dataclass(frozen=True)
class ActionsArtifactSpec:
    """Owner-supplied server identity for one exact workflow artifact."""

    name: str
    workflow_path: str
    run_id: int
    artifact_id: int
    api_digest: str
    source_sha: str
    require_completed_success: bool


@dataclass(frozen=True)
class AcquiredActionsArtifact:
    """Locally extracted artifact plus its replayable transport receipt."""

    root: Path
    archive_path: Path
    evidence_root: Path
    receipt: receipts.JSONObject


@dataclass(frozen=True)
class FreshRecoverySources:
    """Current private-repository and owner-key evidence from the reader boundary."""

    repository_id: int
    repository_body: bytes
    repository_observation: bytes
    owner_signing_keys_observation: bytes


@dataclass(frozen=True)
class RecoveryAuthorityMaterial:
    """Decoded and freshly revalidated recovery-repository authority bytes."""

    authority: receipts.JSONObject
    receipt: bytes
    signature: bytes


@dataclass(frozen=True)
class RecoveryAuthorityGeneration:
    """Write-once full sole-writer authority captured from renewable input slots."""

    generation_id: str
    root: Path
    authority: RecoveryAuthorityMaterial
    input_digests: Mapping[str, str]


@dataclass(frozen=True)
class RecoveryMutationGrantMaterial:
    """Owner-signed, exact-stage authority derived from one full generation."""

    grant: receipts.JSONObject
    receipt: bytes
    signature: bytes
    root: Path
    issuance_owner_signing_keys_observation: bytes
    issuance_reader_runtime_verification: bytes


@dataclass(frozen=True)
class PreparedProductionCapsule:
    """Write-once result of the slow, non-authoritative capsule preparation."""

    root: Path
    candidate_archive: Path
    environment_manifest_raw: bytes
    environment_manifest: receipts.JSONObject
    sys_path: tuple[str, ...]
    runtime: dict[str, str]
    python_sha256: str
    base_library_root: Path
    receipt: bytes


@dataclass(frozen=True)
class CapsuleAuthorityBinding:
    """Immutable historical authority and evidence embedded in one capsule."""

    root: Path
    authority_generation_id: str
    authority: RecoveryAuthorityMaterial
    fresh_sources: FreshRecoverySources
    owner_signing_keys_observation: bytes
    reader_runtime_verification: receipts.JSONObject


@dataclass(frozen=True)
class AuthorizationMaterial:
    """Validated initiate authorization and candidate bundle inputs."""

    root: Path
    candidate_root: Path
    candidate: receipts.JSONObject
    candidate_manifest: bytes
    transaction_authorization: bytes
    transaction: receipts.JSONObject
    approval_history_observation: bytes


@dataclass(frozen=True)
class RecoveryReaderMaterial:
    """Owner-signed scope and its exact historical runtime proof."""

    scope_authority: bytes
    scope_signature: bytes
    owner_signing_keys_observation: bytes
    identity_probe: bytes
    endpoint_probes: bytes
    runtime_verification: bytes


@dataclass(frozen=True)
class RemoteCapsuleSources:
    """Fresh post-publication bodies plus replayable source observations."""

    repository_body: bytes
    release_body: bytes
    assets_body: bytes
    source_records: Mapping[str, bytes]


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _require_distinct_credentials(owner_token: bytes, reader_token: bytes) -> None:
    if (
        type(owner_token) is not bytes
        or type(reader_token) is not bytes
        or not owner_token
        or not reader_token
        or hmac.compare_digest(owner_token, reader_token)
    ):
        raise receipts.ReleaseControlError(
            "owner and recovery-reader credentials must be nonempty distinct bytes"
        )


def load_recovery_reader_material(root: Path) -> RecoveryReaderMaterial:
    """Load the exact recovery-reader proof set carried by authorization."""

    expected = frozenset(
        {
            "recovery-reader-scope-authority.json",
            "recovery-reader-scope-authority.json.sig",
            "owner-signing-keys-observation.json",
            "owner-signing-keys-raw.json",
            "identity-probe.json",
            "endpoint-probes.json",
            "runtime-verification.json",
        }
    )
    if not root.is_dir() or root.is_symlink():
        raise receipts.ReleaseControlError(
            "recovery reader authorization inventory root is invalid"
        )
    entries = tuple(root.iterdir())
    if (
        {entry.name for entry in entries} != expected
        or len(entries) != len(expected)
        or any(not entry.is_file() or entry.is_symlink() for entry in entries)
    ):
        raise receipts.ReleaseControlError("recovery reader authorization inventory is not exact")

    def read(name: str, *, signature: bool = False) -> bytes:
        return receipts._read_regular(  # noqa: SLF001
            root / name,
            label=f"recovery reader authorization {name}",
            max_bytes=(1024 * 1024 if signature else receipts.MAX_SOURCE_ENVELOPE_BYTES),
        )

    return RecoveryReaderMaterial(
        scope_authority=read("recovery-reader-scope-authority.json"),
        scope_signature=read("recovery-reader-scope-authority.json.sig", signature=True),
        owner_signing_keys_observation=read("owner-signing-keys-observation.json"),
        identity_probe=read("identity-probe.json"),
        endpoint_probes=read("endpoint-probes.json"),
        runtime_verification=read("runtime-verification.json"),
    )


def _load_initial_reader_proof(
    *,
    source_root: Path,
    root: Path,
    reader_material: RecoveryReaderMaterial,
) -> tuple[bytes, receipts.JSONObject]:
    expected = {
        "owner-signing-keys-observation.json",
        "runtime-verification.json",
    }
    entries = tuple(root.iterdir()) if root.is_dir() else ()
    if (
        root.is_symlink()
        or {entry.name for entry in entries} != expected
        or len(entries) != len(expected)
        or any(not entry.is_file() or entry.is_symlink() for entry in entries)
    ):
        raise receipts.ReleaseControlError("initial recovery reader proof inventory is not exact")
    owner_keys = receipts._read_regular(  # noqa: SLF001
        root / "owner-signing-keys-observation.json",
        label="initial recovery reader owner keys",
        max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
    )
    runtime_raw = receipts._read_regular(  # noqa: SLF001
        root / "runtime-verification.json",
        label="initial recovery reader runtime verification",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    runtime = _canonical_object(
        runtime_raw,
        label="initial recovery reader runtime verification",
    )
    receipts._validate_schema(  # noqa: SLF001
        receipts.RUNTIME_CREDENTIAL_SCHEMA,
        runtime,
        label="initial recovery reader runtime verification",
    )
    verified_at = receipts.parse_timestamp(
        runtime.get("verified_at"),
        label="initial recovery reader verified_at",
    )
    receipts.source_observation_body_for_contract(
        owner_keys,
        registry=_source_registry(source_root),
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        _clock=lambda: verified_at,
    )
    if (
        runtime.get("purpose") != "recovery_reader"
        or runtime.get("scope_authority_digest") != _sha256(reader_material.scope_authority)
        or runtime.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("initial recovery reader scope runtime binding mismatch")
    return owner_keys, runtime


def _write_exclusive(path: Path, raw: bytes) -> None:
    if receipts.write_once(path, raw):
        return
    try:
        observed = receipts._read_regular(  # noqa: SLF001
            path,
            label="recovery controller replay output",
            max_bytes=max(1, len(raw)),
        )
    except (OSError, ValueError) as exc:
        raise receipts.ReleaseControlError("recovery controller replay output conflicts") from exc
    if observed != raw:
        raise receipts.ReleaseControlError("recovery controller replay output conflicts")


def _directory_file_identity(root: Path, *, label: str) -> receipts.JSONObject:
    if not root.is_dir() or root.is_symlink():
        raise receipts.ReleaseControlError(f"{label} root is invalid")
    directories: list[receipts.JSONValue] = []
    files: list[receipts.JSONValue] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise receipts.ReleaseControlError(f"{label} contains a symlink")
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            directories.append(relative)
            continue
        if not path.is_file():
            raise receipts.ReleaseControlError(f"{label} contains a special file")
        raw = receipts._read_regular(  # noqa: SLF001
            path,
            label=f"{label} {relative}",
            max_bytes=2_147_483_648,
        )
        files.append(
            {
                "path": relative,
                "sha256": _sha256(raw),
                "size_bytes": len(raw),
            }
        )
    return {"directories": directories, "files": files}


def _load_acquired_actions_artifact(
    *,
    specification: ActionsArtifactSpec,
    output_root: Path,
) -> AcquiredActionsArtifact:
    entries = tuple(output_root.iterdir()) if output_root.is_dir() else ()
    if (
        output_root.is_symlink()
        or {entry.name for entry in entries} != {"artifact.zip", "contents", "evidence"}
        or len(entries) != 3
    ):
        raise receipts.ReleaseControlError(
            "recovery controller resumed artifact inventory is not exact"
        )
    archive = output_root / "artifact.zip"
    contents = output_root / "contents"
    evidence = output_root / "evidence"
    evidence_entries = tuple(evidence.iterdir()) if evidence.is_dir() else ()
    expected_evidence = {
        "workflow-run.json",
        "artifact-pages.json",
        "artifact-metadata.json",
        "actions-artifact-observation.json",
    }
    if (
        not archive.is_file()
        or archive.is_symlink()
        or not contents.is_dir()
        or contents.is_symlink()
        or evidence.is_symlink()
        or {entry.name for entry in evidence_entries} != expected_evidence
        or len(evidence_entries) != len(expected_evidence)
        or any(not entry.is_file() or entry.is_symlink() for entry in evidence_entries)
    ):
        raise receipts.ReleaseControlError(
            "recovery controller resumed artifact inventory is invalid"
        )
    run_raw = receipts._read_regular(  # noqa: SLF001
        evidence / "workflow-run.json",
        label="resumed artifact workflow run",
        max_bytes=candidates.MAX_ACTIONS_OBSERVATION_BYTES,
    )
    artifacts_raw = receipts._read_regular(  # noqa: SLF001
        evidence / "artifact-pages.json",
        label="resumed artifact pages",
        max_bytes=candidates.MAX_ACTIONS_OBSERVATION_BYTES,
    )
    direct_raw = receipts._read_regular(  # noqa: SLF001
        evidence / "artifact-metadata.json",
        label="resumed direct artifact metadata",
        max_bytes=candidates.MAX_ACTIONS_OBSERVATION_BYTES,
    )
    receipt = cast(
        receipts.JSONObject,
        candidates.verify_actions_artifact(
            artifacts_raw,
            run_raw,
            expected_name=specification.name,
            expected_run_id=specification.run_id,
            expected_run_attempt=1,
            expected_source_sha=specification.source_sha,
            retention_days=30,
            expected_workflow_path=specification.workflow_path,
            require_completed_success=specification.require_completed_success,
            expected_artifact_id=specification.artifact_id,
            expected_api_digest=specification.api_digest,
            direct_artifact_observation=direct_raw,
        ),
    )
    receipt_raw = receipts._read_regular(  # noqa: SLF001
        evidence / "actions-artifact-observation.json",
        label="resumed artifact receipt",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    if receipt_raw != receipts.canonical_json_bytes(receipt):
        raise receipts.ReleaseControlError("recovery controller resumed artifact receipt conflicts")
    receipt_artifact = receipts._object(  # noqa: SLF001
        receipt.get("artifact"), label="resumed recovery controller artifact receipt"
    )
    if (
        archive.stat().st_size != receipt_artifact.get("size_bytes")
        or _path_sha256(archive) != specification.api_digest
    ):
        raise receipts.ReleaseControlError(
            "recovery controller resumed artifact archive digest mismatch"
        )
    comparison_root = Path(
        tempfile.mkdtemp(prefix=f".{output_root.name}.replay-", dir=output_root.parent)
    )
    try:
        replay_contents = comparison_root / "contents"
        candidates.extract_actions_artifact(
            archive,
            expected_digest=specification.api_digest,
            output=replay_contents,
        )
        if _directory_file_identity(
            replay_contents, label="replayed artifact contents"
        ) != _directory_file_identity(contents, label="resumed artifact contents"):
            raise receipts.ReleaseControlError(
                "recovery controller resumed artifact extraction conflicts"
            )
    finally:
        shutil.rmtree(comparison_root)
    return AcquiredActionsArtifact(
        root=contents,
        archive_path=archive,
        evidence_root=evidence,
        receipt=receipt,
    )


def acquire_actions_artifact(
    *,
    api: ActionsArtifactAPI,
    specification: ActionsArtifactSpec,
    output_root: Path,
) -> AcquiredActionsArtifact:
    """Observe, download by server ID, digest-check, and atomically extract an artifact."""

    if output_root.exists() or output_root.is_symlink():
        return _load_acquired_actions_artifact(
            specification=specification,
            output_root=output_root,
        )
    parent = output_root.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError("recovery controller artifact output parent is invalid")
    staging = Path(tempfile.mkdtemp(prefix=f".{output_root.name}.staging-", dir=parent))
    staging.chmod(0o700)
    try:
        run_raw = api.get_workflow_run(specification.run_id)
        artifacts_raw = api.list_run_artifacts(specification.run_id)
        artifact_raw = api.get_artifact(specification.artifact_id)
        receipt = cast(
            receipts.JSONObject,
            candidates.verify_actions_artifact(
                artifacts_raw,
                run_raw,
                expected_name=specification.name,
                expected_run_id=specification.run_id,
                expected_run_attempt=1,
                expected_source_sha=specification.source_sha,
                retention_days=30,
                expected_workflow_path=specification.workflow_path,
                require_completed_success=specification.require_completed_success,
                expected_artifact_id=specification.artifact_id,
                expected_api_digest=specification.api_digest,
                direct_artifact_observation=artifact_raw,
            ),
        )
        evidence_root = staging / "evidence"
        evidence_root.mkdir(mode=0o700)
        for name, raw in (
            ("workflow-run.json", run_raw),
            ("artifact-pages.json", artifacts_raw),
            ("artifact-metadata.json", artifact_raw),
            (
                "actions-artifact-observation.json",
                receipts.canonical_json_bytes(receipt),
            ),
        ):
            _write_exclusive(evidence_root / name, raw)

        archive_path = staging / "artifact.zip"
        api.download_artifact(specification.artifact_id, archive_path)
        receipt_artifact = receipts._object(  # noqa: SLF001
            receipt.get("artifact"), label="recovery controller artifact receipt"
        )
        if (
            not archive_path.is_file()
            or archive_path.is_symlink()
            or archive_path.stat().st_size != receipt_artifact.get("size_bytes")
            or _path_sha256(archive_path) != specification.api_digest
        ):
            raise ValueError("recovery controller artifact archive digest mismatch")
        contents = staging / "contents"
        candidates.extract_actions_artifact(
            archive_path,
            expected_digest=specification.api_digest,
            output=contents,
        )
        os.replace(staging, output_root)
        staging = output_root
        return AcquiredActionsArtifact(
            root=output_root / "contents",
            archive_path=output_root / "artifact.zip",
            evidence_root=output_root / "evidence",
            receipt=receipt,
        )
    except Exception:
        if staging.exists() and staging != output_root:
            shutil.rmtree(staging)
        raise


def _source_registry(source_root: Path) -> receipts.JSONObject:
    return receipts._object(  # noqa: SLF001
        receipts._load_canonical_file(  # noqa: SLF001
            source_root / "release-control-source-registry.json",
            label="recovery controller source registry",
            max_bytes=4 * 1024 * 1024,
        ),
        label="recovery controller source registry",
    )


def _source_entry(registry: Mapping[str, object], *, name: str) -> receipts.JSONObject:
    entries = [
        entry
        for entry in receipts._validate_registry(registry)  # noqa: SLF001
        if entry.get("receipt_schema") == receipts.SOURCE_OBSERVATION_SCHEMA
        and entry.get("phase") == "release-control"
        and entry.get("mode") is None
        and entry.get("name") == name
    ]
    if len(entries) != 1:
        raise receipts.ReleaseControlError(
            f"recovery controller source registry lacks one {name} entry"
        )
    return entries[0]


def capture_fresh_owner_signing_keys(
    *,
    source_root: Path,
    api: transaction.GitHubReadAPI,
    identity_observation: bytes | None = None,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> bytes:
    """Capture the current owner key registry through the owner credential."""

    registry = _source_registry(source_root)
    identity = (
        transaction._boundary_identity(  # noqa: SLF001
            api, label="recovery controller owner"
        )
        if identity_observation is None
        else identity_observation
    )
    owner_entry = _source_entry(registry, name="owner-signing-keys-observation")
    owner_input, _items = transaction._boundary_paginated_source(  # noqa: SLF001
        api,
        request_target=("GET /users/John-MiracleWorker/ssh_signing_keys?per_page=100"),
        locator=cast(str, owner_entry["locator"]),
        label="recovery controller owner signing keys",
    )
    owner_observation = receipts.canonical_json_bytes(
        receipts.capture_source(
            registry=registry,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="owner-signing-keys-observation",
            raw_input=owner_input,
            identity_observation=identity,
            _clock=_clock,
        )
    )
    receipts.source_observation_body_for_contract(
        owner_observation,
        registry=registry,
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        _clock=_clock,
    )
    return owner_observation


def _load_fresh_recovery_sources(
    *,
    source_root: Path,
    output_root: Path,
    replay_at_capture: bool = False,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FreshRecoverySources:
    expected = {
        "owner-signing-keys-observation.json",
        "recovery-repository-observation.json",
        "recovery-repository.json",
    }
    entries = tuple(output_root.iterdir()) if output_root.is_dir() else ()
    if (
        output_root.is_symlink()
        or {entry.name for entry in entries} != expected
        or len(entries) != len(expected)
        or any(not entry.is_file() or entry.is_symlink() for entry in entries)
    ):
        raise receipts.ReleaseControlError("fresh recovery source resume inventory is not exact")
    owner_observation = receipts._read_regular(  # noqa: SLF001
        output_root / "owner-signing-keys-observation.json",
        label="resumed owner signing keys observation",
        max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
    )
    repository_observation = receipts._read_regular(  # noqa: SLF001
        output_root / "recovery-repository-observation.json",
        label="resumed recovery repository observation",
        max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
    )
    repository_body = receipts._read_regular(  # noqa: SLF001
        output_root / "recovery-repository.json",
        label="resumed recovery repository",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    registry = _source_registry(source_root)
    for name, raw in (
        ("owner-signing-keys-observation", owner_observation),
        ("recovery-repository-observation", repository_observation),
    ):
        replay_clock = _clock
        if replay_at_capture:
            observation = _canonical_object(raw, label=f"resumed {name}")
            replay_time = receipts.parse_timestamp(
                observation.get("captured_at"), label=f"resumed {name} captured_at"
            )

            def replay_clock_at_capture(
                replay_time: datetime = replay_time,
            ) -> datetime:
                return replay_time

            replay_clock = replay_clock_at_capture
        receipts.source_observation_body_for_contract(
            raw,
            registry=registry,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name=name,
            _clock=replay_clock,
        )
    repository_value = receipts.parse_external_json_bytes(
        repository_body,
        label="resumed recovery repository",
    )
    if receipts.canonical_external_json_bytes(repository_value) != repository_body:
        raise receipts.ReleaseControlError("resumed recovery repository is not canonical")
    return FreshRecoverySources(
        repository_id=_require_recovery_repository(repository_value),
        repository_body=repository_body,
        repository_observation=repository_observation,
        owner_signing_keys_observation=owner_observation,
    )


def capture_fresh_recovery_sources(
    *,
    source_root: Path,
    output_root: Path,
    api: transaction.GitHubReadAPI,
    owner_signing_keys_observation: bytes | None = None,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FreshRecoverySources:
    """Capture owner keys and private recovery-repository state through one reader."""

    if output_root.exists() or output_root.is_symlink():
        return _load_fresh_recovery_sources(
            source_root=source_root,
            output_root=output_root,
            _clock=_clock,
        )
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_root.name}.staging-",
            dir=output_root.parent,
        )
    )
    staging.chmod(0o700)
    registry = _source_registry(source_root)
    try:
        identity = transaction._boundary_identity(  # noqa: SLF001
            api, label="recovery controller reader"
        )
        owner_observation = (
            capture_fresh_owner_signing_keys(
                source_root=source_root,
                api=api,
                identity_observation=identity,
                _clock=_clock,
            )
            if owner_signing_keys_observation is None
            else owner_signing_keys_observation
        )
        receipts.source_observation_body_for_contract(
            owner_observation,
            registry=registry,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="owner-signing-keys-observation",
            _clock=_clock,
        )

        repository_exchange = api(
            f"GET /repos/{RECOVERY_REPOSITORY}",
            accept="application/vnd.github+json",
        )
        if repository_exchange.http_status != 200:
            raise receipts.ReleaseControlError("fresh recovery repository observation failed")
        repository_value = receipts.parse_external_json_bytes(
            repository_exchange.response_body,
            label="fresh recovery repository",
        )
        repository_id = _require_recovery_repository(repository_value)
        repository_body = receipts.canonical_external_json_bytes(repository_value)
        repository_observation = receipts.canonical_json_bytes(
            receipts.capture_source(
                registry=registry,
                receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
                phase="release-control",
                mode=None,
                name="recovery-repository-observation",
                raw_input=repository_exchange.response_body,
                identity_observation=identity,
                _clock=_clock,
            )
        )
        receipts.source_observation_body_for_contract(
            repository_observation,
            registry=registry,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name="recovery-repository-observation",
            _clock=_clock,
        )
        for name, raw in (
            ("owner-signing-keys-observation.json", owner_observation),
            ("recovery-repository-observation.json", repository_observation),
            ("recovery-repository.json", repository_body),
        ):
            _write_exclusive(staging / name, raw)
        os.replace(staging, output_root)
        staging = output_root
        return FreshRecoverySources(
            repository_id=repository_id,
            repository_body=repository_body,
            repository_observation=repository_observation,
            owner_signing_keys_observation=owner_observation,
        )
    except Exception:
        if staging.exists() and staging != output_root:
            shutil.rmtree(staging)
        raise


def _load_remote_capsule_sources(
    *,
    source_root: Path,
    recovery_tag: str,
    expected_release_id: int,
    expected_repository_id: int,
    output_root: Path,
) -> RemoteCapsuleSources:
    expected = {
        "recovery-repository-observation.json",
        "recovery-release-observation.json",
        "recovery-assets-observation.json",
    }
    entries = tuple(output_root.iterdir()) if output_root.is_dir() else ()
    if (
        output_root.is_symlink()
        or {entry.name for entry in entries} != expected
        or len(entries) != len(expected)
        or any(not entry.is_file() or entry.is_symlink() for entry in entries)
    ):
        raise receipts.ReleaseControlError("remote recovery capsule resume inventory is not exact")
    registry = _source_registry(source_root)
    observations: dict[str, bytes] = {}
    bodies: dict[str, bytes] = {}
    for name in (
        "recovery-repository-observation",
        "recovery-release-observation",
        "recovery-assets-observation",
    ):
        raw = receipts._read_regular(  # noqa: SLF001
            output_root / f"{name}.json",
            label=f"resumed {name}",
            max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
        )
        observation = _canonical_object(raw, label=f"resumed {name}")
        captured_at = receipts.parse_timestamp(
            observation.get("captured_at"), label=f"resumed {name} captured_at"
        )

        def replay_clock(captured_at: datetime = captured_at) -> datetime:
            return captured_at

        body = receipts.source_observation_body_for_contract(
            raw,
            registry=registry,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name=name,
            _clock=replay_clock,
        )
        observations[name] = raw
        bodies[name] = body
    repository_value = receipts.parse_external_json_bytes(
        bodies["recovery-repository-observation"],
        label="resumed recovery repository",
    )
    if _require_recovery_repository(repository_value) != expected_repository_id:
        raise receipts.ReleaseControlError("resumed recovery repository ID changed")
    release_value = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(
            bodies["recovery-release-observation"],
            label="resumed recovery Release",
        ),
        label="resumed recovery Release",
    )
    if (
        release_value.get("id") != expected_release_id
        or release_value.get("tag_name") != recovery_tag
        or release_value.get("draft") is not False
        or release_value.get("immutable") is not True
    ):
        raise receipts.ReleaseControlError("resumed recovery Release identity changed")
    assets_observation = _canonical_object(
        observations["recovery-assets-observation"],
        label="resumed recovery assets observation",
    )
    parsed_assets = receipts.parse_external_json_bytes(
        bodies["recovery-assets-observation"],
        label="resumed recovery assets pages",
    )
    pages = receipts._paginated_bodies(  # noqa: SLF001
        parsed_assets,
        locator=assets_observation.get("locator"),
    )
    asset_items = [item for page in pages for item in receipts._array(page, label="asset page")]  # noqa: SLF001
    return RemoteCapsuleSources(
        repository_body=receipts.canonical_external_json_bytes(repository_value),
        release_body=receipts.canonical_external_json_bytes(release_value),
        assets_body=receipts.canonical_external_json_bytes(asset_items),
        source_records={
            "recovery-repository": observations["recovery-repository-observation"],
            "recovery-release": observations["recovery-release-observation"],
            "recovery-release-assets": observations["recovery-assets-observation"],
        },
    )


def capture_remote_capsule_sources(
    *,
    source_root: Path,
    recovery_tag: str,
    expected_release_id: int,
    expected_repository_id: int,
    output_root: Path,
    api: transaction.GitHubReadAPI,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RemoteCapsuleSources:
    """Capture the immutable recovery Release and assets after publication."""

    if output_root.exists() or output_root.is_symlink():
        return _load_remote_capsule_sources(
            source_root=source_root,
            recovery_tag=recovery_tag,
            expected_release_id=expected_release_id,
            expected_repository_id=expected_repository_id,
            output_root=output_root,
        )
    staging_prefix = f".{output_root.name}.staging-"
    for interrupted in output_root.parent.iterdir():
        if not interrupted.name.startswith(staging_prefix):
            continue
        if interrupted.is_symlink() or not interrupted.is_dir():
            raise receipts.ReleaseControlError("interrupted remote source staging path is unsafe")
        shutil.rmtree(interrupted)
    staging = Path(
        tempfile.mkdtemp(
            prefix=staging_prefix,
            dir=output_root.parent,
        )
    )
    staging.chmod(0o700)
    registry = _source_registry(source_root)
    identity = transaction._boundary_identity(  # noqa: SLF001
        api, label="recovery controller post-publication reader"
    )
    repository_exchange = api(
        f"GET /repos/{RECOVERY_REPOSITORY}",
        accept="application/vnd.github+json",
    )
    if repository_exchange.http_status != 200:
        raise receipts.ReleaseControlError(
            "post-publication recovery repository observation failed"
        )
    repository_value = receipts.parse_external_json_bytes(
        repository_exchange.response_body,
        label="post-publication recovery repository",
    )
    repository_id = _require_recovery_repository(repository_value)
    if repository_id != expected_repository_id:
        raise receipts.ReleaseControlError("post-publication recovery repository ID changed")
    release_exchange = api(
        f"GET /repos/{RECOVERY_REPOSITORY}/releases/tags/{quote(recovery_tag, safe='')}",
        accept="application/vnd.github+json",
    )
    if release_exchange.http_status != 200:
        raise receipts.ReleaseControlError("post-publication recovery Release observation failed")
    release_value = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(
            release_exchange.response_body,
            label="post-publication recovery Release",
        ),
        label="post-publication recovery Release",
    )
    release_id = receipts._safe_integer(  # noqa: SLF001
        release_value.get("id"),
        label="post-publication recovery Release ID",
        positive=True,
    )
    if release_id != expected_release_id or release_value.get("tag_name") != recovery_tag:
        raise receipts.ReleaseControlError("post-publication recovery Release identity changed")
    assets_entry = _source_entry(registry, name="recovery-assets-observation")
    assets_input, asset_items = transaction._boundary_paginated_source(  # noqa: SLF001
        api,
        request_target=(
            f"GET /repos/{RECOVERY_REPOSITORY}/releases/{release_id}/assets?per_page=100"
        ),
        locator=cast(str, assets_entry["locator"]),
        label="post-publication recovery assets",
    )
    source_inputs = {
        "recovery-repository-observation": repository_exchange.response_body,
        "recovery-release-observation": release_exchange.response_body,
        "recovery-assets-observation": assets_input,
    }
    observations: dict[str, bytes] = {}
    for name, raw_input in source_inputs.items():
        observation = receipts.canonical_json_bytes(
            receipts.capture_source(
                registry=registry,
                receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
                phase="release-control",
                mode=None,
                name=name,
                raw_input=raw_input,
                identity_observation=identity,
                _clock=_clock,
            )
        )
        observations[name] = observation
        receipts.source_observation_body_for_contract(
            observation,
            registry=registry,
            receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
            phase="release-control",
            mode=None,
            name=name,
            _clock=_clock,
        )
        _write_exclusive(staging / f"{name}.json", observation)
    result = RemoteCapsuleSources(
        repository_body=receipts.canonical_external_json_bytes(repository_value),
        release_body=receipts.canonical_external_json_bytes(release_value),
        assets_body=receipts.canonical_external_json_bytes(asset_items),
        source_records={
            "recovery-repository": observations["recovery-repository-observation"],
            "recovery-release": observations["recovery-release-observation"],
            "recovery-release-assets": observations["recovery-assets-observation"],
        },
    )
    os.replace(staging, output_root)
    return result


def _validate_recovery_authority_record(
    *,
    raw: bytes,
    fresh_owner_signing_keys_observation: bytes,
    expected_repository_id: int,
    source_registry: Mapping[str, object],
    authorization_time: datetime,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecoveryAuthorityMaterial:
    record = _canonical_object(raw, label="recovery authority verification record")
    receipts._require_exact_fields(  # noqa: SLF001
        record,
        frozenset(
            {
                "schema",
                "authority_schema",
                "authority",
                "receipt_digest",
                "signature_digest",
                "receipt_base64",
                "signature_base64",
                "owner_signing_keys_observation_base64",
                "signing_key_fingerprint",
                "verified_at",
                "validation_status",
            }
        ),
        label="recovery authority verification record",
    )
    if (
        record.get("schema") != "kestrel.recovery_repository_authority_verification.v1"
        or record.get("authority_schema") != receipts.RECOVERY_AUTHORITY_SCHEMA
        or record.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("recovery authority verification record is invalid")
    receipt = _decode_canonical_base64(
        record.get("receipt_base64"), label="recovery authority receipt"
    )
    signature = _decode_canonical_base64(
        record.get("signature_base64"), label="recovery authority signature"
    )
    embedded_owner_keys = _decode_canonical_base64(
        record.get("owner_signing_keys_observation_base64"),
        label="recovery authority embedded owner keys",
    )
    authority = receipts.validate_recovery_repository_authority(
        _canonical_object(receipt, label="recovery authority receipt")
    )
    if (
        record.get("authority") != authority
        or record.get("receipt_digest") != _sha256(receipt)
        or record.get("signature_digest") != _sha256(signature)
    ):
        raise receipts.ReleaseControlError("recovery authority verification byte binding mismatch")
    verified_at = receipts.parse_timestamp(
        record.get("verified_at"), label="recovery authority verified_at"
    )
    _embedded_key, embedded_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        embedded_owner_keys,
        expected_fingerprint=cast(str, record.get("signing_key_fingerprint")),
    )
    receipts.verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=embedded_fingerprint,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    receipts.source_observation_body_for_contract(
        fresh_owner_signing_keys_observation,
        registry=source_registry,
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        _clock=lambda: verified_at,
    )
    _fresh_key, fresh_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        fresh_owner_signing_keys_observation,
        expected_fingerprint=embedded_fingerprint,
    )
    receipts.verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=fresh_fingerprint,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    if authorization_time.tzinfo is None or authorization_time.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError(
            "recovery authority authorization time must be aware UTC"
        )
    authorization_time = authorization_time.astimezone(UTC).replace(microsecond=0)
    observed_at = receipts.parse_timestamp(
        authority.get("observed_at"), label="recovery authority observed_at"
    )
    expires_at = receipts.parse_timestamp(
        authority.get("expires_at"), label="recovery authority expires_at"
    )
    authority_repository = receipts._object(  # noqa: SLF001
        authority.get("repository"), label="recovery authority repository"
    )
    if (
        verified_at < observed_at
        or verified_at >= expires_at
        or verified_at > authorization_time
        or authorization_time < observed_at
        or authorization_time >= expires_at
        or authority_repository.get("full_name") != RECOVERY_REPOSITORY
        or authority_repository.get("id") != expected_repository_id
    ):
        raise receipts.ReleaseControlError(
            "recovery authority authorization time or repository binding mismatch"
        )
    return RecoveryAuthorityMaterial(authority=authority, receipt=receipt, signature=signature)


def create_current_recovery_authority(
    *,
    owner_authority_snapshot: bytes,
    repository_observation: bytes,
    immutable_releases_observation: bytes,
    controller_context: bytes,
    identity_file: Path,
    fresh_owner_signing_keys_observation: bytes,
    expected_repository_id: int,
    source_registry: Mapping[str, object],
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecoveryAuthorityMaterial:
    """Create and sign a fresh fail-closed mutation gate for the recovery repo."""

    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError("current recovery authority clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    checked_repository_id = receipts._safe_integer(  # noqa: SLF001
        expected_repository_id,
        label="current recovery authority repository ID",
        positive=True,
    )
    receipts.source_observation_body_for_contract(
        fresh_owner_signing_keys_observation,
        registry=source_registry,
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        _clock=lambda: now,
    )
    authority = receipts.create_recovery_repository_authority(
        owner_authority_snapshot=owner_authority_snapshot,
        repository_observation=repository_observation,
        immutable_releases_observation=immutable_releases_observation,
        controller_context=controller_context,
        _clock=lambda: now,
    )
    repository = receipts._object(  # noqa: SLF001
        authority.get("repository"), label="current recovery authority repository"
    )
    observed_at = receipts.parse_timestamp(
        authority.get("observed_at"),
        label="current recovery authority observed_at",
    )
    expires_at = receipts.parse_timestamp(
        authority.get("expires_at"),
        label="current recovery authority expires_at",
    )
    if (
        repository.get("full_name") != RECOVERY_REPOSITORY
        or repository.get("id") != checked_repository_id
        or now < observed_at
        or now >= expires_at
    ):
        raise receipts.ReleaseControlError(
            "current recovery authority time or repository binding mismatch"
        )
    receipt = receipts.canonical_json_bytes(authority)
    signature = receipts.sign_receipt_detached(
        receipt=receipt,
        identity_file=identity_file,
        principal=receipts.SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    _public_key, registered_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        fresh_owner_signing_keys_observation,
        expected_fingerprint=None,
    )
    receipts.verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=registered_fingerprint,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    return RecoveryAuthorityMaterial(
        authority=authority,
        receipt=receipt,
        signature=signature,
    )


_RECOVERY_AUTHORITY_SLOT_NAMES = {
    "recovery-owner-dashboard": "current_recovery_owner_authority_snapshot",
    "recovery-repository-rest": "current_recovery_repository_observation",
    "recovery-immutable-releases-rest": ("current_recovery_immutable_releases_observation"),
    "controller-context": "current_recovery_controller_context",
}


def _verify_recovery_authority_bytes(
    *,
    receipt: bytes,
    signature: bytes,
    owner_signing_keys_observation: bytes,
    _clock: Callable[[], datetime],
) -> receipts.JSONObject:
    authority = receipts.validate_recovery_repository_authority(
        _canonical_object(receipt, label="recovery authority receipt")
    )
    _public_key, fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        owner_signing_keys_observation,
        expected_fingerprint=None,
    )
    receipts.verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=fingerprint,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError(
            "recovery authority verification clock must be aware UTC"
        )
    now = now.astimezone(UTC).replace(microsecond=0)
    observed_at = receipts.parse_timestamp(
        authority.get("observed_at"), label="recovery authority observed_at"
    )
    expires_at = receipts.parse_timestamp(
        authority.get("expires_at"), label="recovery authority expires_at"
    )
    if now < observed_at:
        raise receipts.ReleaseControlError("recovery authority is not yet valid")
    if now >= expires_at:
        raise receipts.ReleaseControlError("recovery authority is expired")
    return authority


def _read_stable_authority_slot(path: Path, *, label: str) -> bytes:
    """Read one replaceable slot without admitting an in-place partial rewrite."""

    if (
        not path.is_absolute()
        or not path.is_file()
        or path.is_symlink()
        or path.resolve(strict=True) != path
    ):
        raise receipts.ReleaseControlError(f"{label} slot is not a real regular file")
    before = path.stat()
    raw = receipts._read_regular(  # noqa: SLF001
        path,
        label=label,
        max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
    )
    after = path.stat()
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if identity_before != identity_after or _path_sha256(path) != _sha256(raw):
        raise receipts.ReleaseControlError(f"{label} slot changed while it was read")
    return raw


def _load_recovery_authority_generation(
    *,
    root: Path,
    generation_id: str,
    current_owner_signing_keys_observation: bytes,
    replay_at_observed: bool = False,
    _clock: Callable[[], datetime],
) -> RecoveryAuthorityGeneration:
    expected_inputs = set(_RECOVERY_AUTHORITY_SLOT_NAMES)
    inputs_root = root / "inputs"
    expected_root = {"authority.json", "authority.json.sig", "generation.json", "inputs"}
    if (
        root.is_symlink()
        or not root.is_dir()
        or {entry.name for entry in root.iterdir()} != expected_root
        or inputs_root.is_symlink()
        or not inputs_root.is_dir()
        or {entry.name for entry in inputs_root.iterdir()}
        != {f"{name}.json" for name in expected_inputs}
    ):
        raise receipts.ReleaseControlError("recovery authority generation inventory is invalid")
    metadata_raw = receipts._read_regular(  # noqa: SLF001
        root / "generation.json",
        label="recovery authority generation metadata",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    metadata = _canonical_object(metadata_raw, label="recovery authority generation metadata")
    input_digests = metadata.get("input_digests")
    if (
        set(metadata)
        != {
            "schema",
            "generation_id",
            "input_digests",
            "authority_receipt_digest",
            "authority_signature_digest",
            "validation_status",
        }
        or metadata.get("schema") != "kestrel.recovery_authority_generation.v1"
        or metadata.get("generation_id") != generation_id
        or root.name != generation_id.removeprefix("sha256:")
        or type(input_digests) is not dict
        or set(input_digests) != expected_inputs
        or metadata.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("recovery authority generation metadata conflicts")
    if generation_id != _sha256(receipts.canonical_json_bytes(input_digests)):
        raise receipts.ReleaseControlError(
            "recovery authority generation content identity conflicts"
        )
    for name, expected_digest in input_digests.items():
        receipts._digest(  # noqa: SLF001
            expected_digest, label=f"recovery authority generation {name} digest"
        )
        path = inputs_root / f"{name}.json"
        if not path.is_file() or path.is_symlink() or _path_sha256(path) != expected_digest:
            raise receipts.ReleaseControlError("recovery authority generation input bytes conflict")
    receipt = receipts._read_regular(  # noqa: SLF001
        root / "authority.json",
        label="recovery authority generation receipt",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    signature = receipts._read_regular(  # noqa: SLF001
        root / "authority.json.sig",
        label="recovery authority generation signature",
        max_bytes=1024 * 1024,
    )
    if metadata.get("authority_receipt_digest") != _sha256(receipt) or metadata.get(
        "authority_signature_digest"
    ) != _sha256(signature):
        raise receipts.ReleaseControlError("recovery authority generation signed bytes conflict")
    verification_clock = _clock
    if replay_at_observed:
        decoded = _canonical_object(receipt, label="historical recovery authority generation")
        observed_at = receipts.parse_timestamp(
            decoded.get("observed_at"),
            label="historical recovery authority generation observed_at",
        )

        def verification_clock() -> datetime:
            return observed_at

    authority = _verify_recovery_authority_bytes(
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=(current_owner_signing_keys_observation),
        _clock=verification_clock,
    )
    return RecoveryAuthorityGeneration(
        generation_id=generation_id,
        root=root,
        authority=RecoveryAuthorityMaterial(
            authority=authority,
            receipt=receipt,
            signature=signature,
        ),
        input_digests=cast(Mapping[str, str], input_digests),
    )


def _load_or_create_recovery_authority_generation(
    *,
    request: RecoveryControllerRequest,
    expected_identity_digest: str,
    current_owner_signing_keys_observation: bytes,
    expected_repository_id: int,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecoveryAuthorityGeneration:
    """Snapshot and validate one full renewable sole-writer authority generation."""

    _require_signing_identity_binding(request, expected_identity_digest)
    generations_root = request.work_root / "recovery-authority-generations"
    if generations_root.exists() or generations_root.is_symlink():
        if not generations_root.is_dir() or generations_root.is_symlink():
            raise receipts.ReleaseControlError("recovery authority generations root is invalid")
    else:
        generations_root.mkdir(mode=0o700)
    failed_root = generations_root / "failed"
    if failed_root.exists() or failed_root.is_symlink():
        if not failed_root.is_dir() or failed_root.is_symlink():
            raise receipts.ReleaseControlError(
                "failed recovery authority generations root is invalid"
            )
    else:
        failed_root.mkdir(mode=0o700)

    staging = Path(tempfile.mkdtemp(prefix=".authority-generation-", dir=generations_root))
    staging.chmod(0o700)
    inputs_root = staging / "inputs"
    inputs_root.mkdir(mode=0o700)
    copied: dict[str, bytes] = {}
    try:
        for name, attribute in sorted(_RECOVERY_AUTHORITY_SLOT_NAMES.items()):
            raw = _read_stable_authority_slot(
                cast(Path, getattr(request, attribute)),
                label=f"current recovery authority {name}",
            )
            copied[name] = raw
            _write_exclusive(inputs_root / f"{name}.json", raw)
        input_digests = {name: _sha256(raw) for name, raw in sorted(copied.items())}
        generation_id = _sha256(receipts.canonical_json_bytes(input_digests))
        final_root = generations_root / generation_id.removeprefix("sha256:")
        if final_root.exists() or final_root.is_symlink():
            shutil.rmtree(staging)
            return _load_recovery_authority_generation(
                root=final_root,
                generation_id=generation_id,
                current_owner_signing_keys_observation=(current_owner_signing_keys_observation),
                _clock=_clock,
            )
        authority = create_current_recovery_authority(
            owner_authority_snapshot=copied["recovery-owner-dashboard"],
            repository_observation=copied["recovery-repository-rest"],
            immutable_releases_observation=copied["recovery-immutable-releases-rest"],
            controller_context=copied["controller-context"],
            identity_file=request.identity_file,
            fresh_owner_signing_keys_observation=(current_owner_signing_keys_observation),
            expected_repository_id=expected_repository_id,
            source_registry=_source_registry(request.source_root),
            _clock=_clock,
        )
        _write_exclusive(staging / "authority.json", authority.receipt)
        _write_exclusive(staging / "authority.json.sig", authority.signature)
        metadata = {
            "schema": "kestrel.recovery_authority_generation.v1",
            "generation_id": generation_id,
            "input_digests": input_digests,
            "authority_receipt_digest": _sha256(authority.receipt),
            "authority_signature_digest": _sha256(authority.signature),
            "validation_status": "validated",
        }
        _write_exclusive(staging / "generation.json", receipts.canonical_json_bytes(metadata))
        os.replace(staging, final_root)
        staging = final_root
        return RecoveryAuthorityGeneration(
            generation_id=generation_id,
            root=final_root,
            authority=authority,
            input_digests=input_digests,
        )
    except BaseException as exc:
        if staging.exists() and staging.parent == generations_root:
            generation_id = _sha256(
                receipts.canonical_json_bytes(
                    {name: _sha256(raw) for name, raw in sorted(copied.items())}
                )
            )
            failure = {
                "schema": "kestrel.recovery_authority_generation_failure.v1",
                "generation_id": generation_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "validation_status": "failed_closed",
            }
            try:
                _write_exclusive(
                    staging / "failure.json",
                    receipts.canonical_json_bytes(failure),
                )
                final_failed = failed_root / generation_id.removeprefix("sha256:")
                if final_failed.exists() or final_failed.is_symlink():
                    shutil.rmtree(staging)
                else:
                    os.replace(staging, final_failed)
            except BaseException:
                # Preserve the original validation failure; leave staging in place
                # if even the append-only failure journal cannot be finalized.
                pass
        raise


MUTATION_GRANT_LIFETIME_SECONDS = 5 * 60


def _canonical_stage_scope(value: Mapping[str, object]) -> receipts.JSONObject:
    scope = _canonical_object(
        receipts.canonical_json_bytes(value), label="recovery mutation stage scope"
    )
    stage = scope.get("stage")
    operations = scope.get("allowed_operations")
    assets = scope.get("assets")
    release = scope.get("release")
    expected_asset_names = {
        "capsule_publish": {
            "recovery-bootstrap.py",
            "recovery-capsule-manifest.json",
            "recovery-capsule.tar",
        },
        "prepare_publish": set(PREPARE_AUTHORITY_ASSETS),
    }
    expected_names = expected_asset_names.get(cast(str, stage), set())
    expected_operations = {
        "create_draft_release",
        "publish_immutable_release",
        *(f"upload:{name}" for name in expected_names),
    }
    if (
        set(scope) != {"stage", "release", "assets", "allowed_operations"}
        or stage not in {"capsule_publish", "prepare_publish"}
        or type(operations) is not list
        or operations != sorted(expected_operations)
        or type(assets) is not list
        or type(release) is not dict
        or set(release) != {"repository", "repository_id", "tag", "name", "body_sha256"}
    ):
        raise receipts.ReleaseControlError("recovery mutation stage scope is invalid")
    release_object = cast(dict[str, object], release)
    if (
        receipts._validate_string(  # noqa: SLF001
            release_object.get("repository"),
            label="recovery mutation stage repository",
        )
        != RECOVERY_REPOSITORY
        or receipts._safe_integer(  # noqa: SLF001
            release_object.get("repository_id"),
            label="recovery mutation stage repository id",
            positive=True,
        )
        <= 0
        or not receipts._validate_string(  # noqa: SLF001
            release_object.get("tag"), label="recovery mutation stage tag"
        )
        or not receipts._validate_string(  # noqa: SLF001
            release_object.get("name"), label="recovery mutation stage name"
        )
        or receipts._digest(  # noqa: SLF001
            release_object.get("body_sha256"),
            label="recovery mutation stage body digest",
        )
        != release_object.get("body_sha256")
    ):
        raise receipts.ReleaseControlError("recovery mutation stage release binding is invalid")
    normalized_assets: list[receipts.JSONObject] = []
    for item in cast(list[object], assets):
        asset = receipts._object(  # noqa: SLF001
            item, label="recovery mutation stage asset"
        )
        if set(asset) != {"name", "size_bytes", "sha256"}:
            raise receipts.ReleaseControlError("recovery mutation stage asset shape is invalid")
        name = receipts._validate_string(  # noqa: SLF001
            asset.get("name"), label="recovery mutation stage asset name"
        )
        size = receipts._safe_integer(  # noqa: SLF001
            asset.get("size_bytes"),
            label="recovery mutation stage asset size",
            positive=True,
        )
        digest = receipts._digest(  # noqa: SLF001
            asset.get("sha256"), label="recovery mutation stage asset digest"
        )
        normalized_assets.append({"name": name, "size_bytes": size, "sha256": digest})
    if [item["name"] for item in normalized_assets] != sorted(expected_names) or {
        cast(str, item["name"]) for item in normalized_assets
    } != expected_names:
        raise receipts.ReleaseControlError("recovery mutation stage asset inventory is invalid")
    return scope


def _stage_asset_scope(assets: Mapping[str, bytes]) -> list[receipts.JSONObject]:
    return [
        {"name": name, "size_bytes": len(raw), "sha256": _sha256(raw)}
        for name, raw in sorted(assets.items())
    ]


def _capsule_publish_stage_scope(
    *,
    capsule_root: Path,
    manifest_raw: bytes,
    tag: str,
    repository_id: int,
) -> receipts.JSONObject:
    checked_repository_id = receipts._safe_integer(  # noqa: SLF001
        repository_id,
        label="recovery capsule mutation scope repository ID",
        positive=True,
    )
    assets = {
        "recovery-bootstrap.py": receipts._read_regular(  # noqa: SLF001
            capsule_root / "scripts/bootstrap_recovery.py",
            label="recovery capsule mutation scope bootstrap",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        ),
        "recovery-capsule-manifest.json": manifest_raw,
        "recovery-capsule.tar": receipts.deterministic_recovery_capsule_archive(capsule_root),
    }
    release_name = f"Kestrel recovery capsule {tag}"
    release_body = (
        f"Kestrel recovery capsule {tag}\n\nKestrel-Recovery-Capsule: {_sha256(manifest_raw)}"
    )
    return _canonical_stage_scope(
        {
            "stage": "capsule_publish",
            "release": {
                "repository": RECOVERY_REPOSITORY,
                "repository_id": checked_repository_id,
                "tag": tag,
                "name": release_name,
                "body_sha256": _sha256(release_body.encode("utf-8")),
            },
            "assets": _stage_asset_scope(assets),
            "allowed_operations": sorted(
                {
                    "create_draft_release",
                    "publish_immutable_release",
                    *(f"upload:{name}" for name in assets),
                }
            ),
        }
    )


def _prepare_publish_stage_scope(
    *,
    asset_root: Path,
    promotion_run_id: int,
    repository_id: int,
) -> receipts.JSONObject:
    run_id = receipts._safe_integer(  # noqa: SLF001
        promotion_run_id,
        label="prepare mutation scope promotion run ID",
        positive=True,
    )
    checked_repository_id = receipts._safe_integer(  # noqa: SLF001
        repository_id,
        label="prepare mutation scope repository ID",
        positive=True,
    )
    assets = _prepare_authority_asset_bytes(asset_root)
    tag = f"release-prepare-authority-{run_id}-1"
    body = f"Kestrel recovery capsule authority for promotion run {run_id}"
    return _canonical_stage_scope(
        {
            "stage": "prepare_publish",
            "release": {
                "repository": RECOVERY_REPOSITORY,
                "repository_id": checked_repository_id,
                "tag": tag,
                "name": tag,
                "body_sha256": _sha256(body.encode("utf-8")),
            },
            "assets": _stage_asset_scope(assets),
            "allowed_operations": sorted(
                {
                    "create_draft_release",
                    "publish_immutable_release",
                    *(f"upload:{name}" for name in assets),
                }
            ),
        }
    )


def _grant_request_journal_digest(request: RecoveryControllerRequest) -> str:
    journal = request.work_root / "controller-request.json"
    if not journal.is_file() or journal.is_symlink():
        raise receipts.ReleaseControlError("recovery mutation grant request journal is unavailable")
    return _path_sha256(journal)


def _require_stage_scope_request_binding(
    scope: Mapping[str, object], request: RecoveryControllerRequest
) -> None:
    release = receipts._object(  # noqa: SLF001
        scope.get("release"), label="recovery mutation request-bound release"
    )
    assets = receipts._array(  # noqa: SLF001
        scope.get("assets"), label="recovery mutation request-bound assets"
    )
    stage = scope.get("stage")
    if stage == "capsule_publish":
        tag = f"recovery-{request.promotion_run_id}-1"
        name = f"Kestrel recovery capsule {tag}"
        manifest_asset = next(
            (
                receipts._object(  # noqa: SLF001
                    item, label="recovery mutation capsule manifest asset"
                )
                for item in assets
                if isinstance(item, dict) and item.get("name") == "recovery-capsule-manifest.json"
            ),
            None,
        )
        if manifest_asset is None:
            raise receipts.ReleaseControlError(
                "recovery mutation capsule manifest binding is missing"
            )
        body = (
            f"Kestrel recovery capsule {tag}\n\n"
            f"Kestrel-Recovery-Capsule: {manifest_asset.get('sha256')}"
        )
    elif stage == "prepare_publish":
        tag = f"release-prepare-authority-{request.promotion_run_id}-1"
        name = tag
        body = f"Kestrel recovery capsule authority for promotion run {request.promotion_run_id}"
    else:
        raise receipts.ReleaseControlError("recovery mutation request-bound stage is invalid")
    if release != {
        "repository": RECOVERY_REPOSITORY,
        "repository_id": request.recovery_repository_id,
        "tag": tag,
        "name": name,
        "body_sha256": _sha256(body.encode("utf-8")),
    }:
        raise receipts.ReleaseControlError(
            "recovery mutation stage scope differs from the controller request"
        )


def _validate_stage_mutation_grant(
    *,
    material: RecoveryMutationGrantMaterial,
    request: RecoveryControllerRequest,
    expected_identity_digest: str,
    generation: RecoveryAuthorityGeneration,
    stage_scope: Mapping[str, object],
    transaction_authorization: bytes,
    reader_material: RecoveryReaderMaterial,
    bound_reader_runtime_verification: Mapping[str, object],
    recovery_reader_token: bytes,
    current_owner_signing_keys_observation: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecoveryMutationGrantMaterial:
    """Validate one durable exact-stage grant without treating it as capsule evidence."""

    _require_signing_identity_binding(request, expected_identity_digest)
    scope = _canonical_stage_scope(stage_scope)
    _require_stage_scope_request_binding(scope, request)
    grant = _canonical_object(material.receipt, label="recovery mutation stage grant")
    if grant != material.grant:
        raise receipts.ReleaseControlError("recovery mutation grant decoded bytes conflict")
    receipts._require_exact_fields(  # noqa: SLF001
        grant,
        frozenset(
            {
                "schema",
                "grant_id",
                "authority_generation_id",
                "request_journal_digest",
                "source_sha",
                "candidate_manifest_digest",
                "promotion_run_id",
                "recovery_repository",
                "transaction_authorization_digest",
                "stage_scope",
                "owner",
                "reader",
                "current_recovery_authority",
                "issuance_evidence",
                "maintenance_acknowledgement_digest",
                "issued_at",
                "expires_at",
                "provenance",
                "confidence",
                "validation_status",
            }
        ),
        label="recovery mutation stage grant",
    )
    projection = dict(grant)
    claimed_grant_id = projection.pop("grant_id", None)
    expected_grant_id = _sha256(receipts.canonical_json_bytes(projection))
    owner = receipts._object(  # noqa: SLF001
        grant.get("owner"), label="recovery mutation grant owner"
    )
    reader = receipts._object(  # noqa: SLF001
        grant.get("reader"), label="recovery mutation grant reader"
    )
    authority_binding = receipts._object(  # noqa: SLF001
        grant.get("current_recovery_authority"),
        label="recovery mutation grant authority",
    )
    repository = receipts._object(  # noqa: SLF001
        grant.get("recovery_repository"),
        label="recovery mutation grant repository",
    )
    issuance_evidence = receipts._object(  # noqa: SLF001
        grant.get("issuance_evidence"),
        label="recovery mutation grant issuance evidence",
    )
    scope_authority = _canonical_object(
        reader_material.scope_authority,
        label="recovery mutation grant reader scope",
    )
    issuance_runtime = _canonical_object(
        material.issuance_reader_runtime_verification,
        label="recovery mutation grant issuance reader proof",
    )
    expected_inventory = {
        "grant.json",
        "grant.json.sig",
        "grant-metadata.json",
        "issuance-owner-signing-keys-observation.json",
        "issuance-reader-runtime-verification.json",
    }
    if (
        material.root.is_symlink()
        or not material.root.is_dir()
        or {entry.name for entry in material.root.iterdir()} != expected_inventory
        or receipts._read_regular(  # noqa: SLF001
            material.root / "grant.json",
            label="recovery mutation grant stored receipt",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        != material.receipt
        or receipts._read_regular(  # noqa: SLF001
            material.root / "grant.json.sig",
            label="recovery mutation grant stored signature",
            max_bytes=1024 * 1024,
        )
        != material.signature
        or receipts._read_regular(  # noqa: SLF001
            material.root / "issuance-owner-signing-keys-observation.json",
            label="recovery mutation grant stored issuance owner keys",
            max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
        )
        != material.issuance_owner_signing_keys_observation
        or receipts._read_regular(  # noqa: SLF001
            material.root / "issuance-reader-runtime-verification.json",
            label="recovery mutation grant stored issuance reader proof",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        != material.issuance_reader_runtime_verification
    ):
        raise receipts.ReleaseControlError("recovery mutation grant stored inventory conflicts")
    metadata_raw = receipts._read_regular(  # noqa: SLF001
        material.root / "grant-metadata.json",
        label="recovery mutation grant storage metadata",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    metadata = _canonical_object(metadata_raw, label="recovery mutation grant storage metadata")
    acknowledgement = receipts._object(  # noqa: SLF001
        generation.authority.authority.get("maintenance_window_acknowledgement"),
        label="recovery mutation grant acknowledgement",
    )
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError("recovery mutation grant clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    issued_at = receipts.parse_timestamp(
        grant.get("issued_at"), label="recovery mutation grant issued_at"
    )
    expires_at = receipts.parse_timestamp(
        grant.get("expires_at"), label="recovery mutation grant expires_at"
    )
    acknowledgement_expiry = receipts.parse_timestamp(
        acknowledgement.get("expires_at"),
        label="recovery mutation grant acknowledgement expiry",
    )
    credential_expiry = receipts.parse_timestamp(
        receipts._object(  # noqa: SLF001
            generation.authority.authority.get("credentials")[0],  # type: ignore[index]
            label="recovery mutation grant authority credential",
        ).get("expires_at"),
        label="recovery mutation grant authority credential expiry",
    )
    reader_expiry = receipts.parse_timestamp(
        scope_authority.get("expires_at"),
        label="recovery mutation grant reader scope expiry",
    )
    authority_expiry = receipts.parse_timestamp(
        generation.authority.authority.get("expires_at"),
        label="recovery mutation grant authority expiry",
    )
    receipts.source_observation_body_for_contract(
        material.issuance_owner_signing_keys_observation,
        registry=_source_registry(request.source_root),
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        _clock=lambda: issued_at,
    )
    receipts.source_observation_body_for_contract(
        current_owner_signing_keys_observation,
        registry=_source_registry(request.source_root),
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        _clock=lambda: now,
    )
    _issuance_key, issuance_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        material.issuance_owner_signing_keys_observation,
        expected_fingerprint=cast(str, owner.get("signing_key_fingerprint")),
    )
    _public_key, current_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        current_owner_signing_keys_observation,
        expected_fingerprint=issuance_fingerprint,
    )
    receipts.verify_detached_signature(
        receipt=material.receipt,
        signature=material.signature,
        expected_fingerprint=issuance_fingerprint,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    authority_observed_at = receipts.parse_timestamp(
        generation.authority.authority.get("observed_at"),
        label="recovery mutation grant authority observed_at",
    )
    replayed_authority = _verify_recovery_authority_bytes(
        receipt=generation.authority.receipt,
        signature=generation.authority.signature,
        owner_signing_keys_observation=(material.issuance_owner_signing_keys_observation),
        _clock=lambda: authority_observed_at,
    )
    _require_current_reader_authority_binding(
        recovery_authority=generation.authority.authority,
        reader_material=reader_material,
        current_runtime_verification=issuance_runtime,
        _clock=lambda: issued_at,
    )
    _require_current_reader_authority_binding(
        recovery_authority=generation.authority.authority,
        reader_material=reader_material,
        current_runtime_verification=bound_reader_runtime_verification,
        _clock=lambda: now,
    )
    if (
        grant.get("schema") != "kestrel.recovery_mutation_grant.v1"
        or claimed_grant_id != expected_grant_id
        or material.root.name != claimed_grant_id.removeprefix("sha256:")
        or material.root.parent.name != generation.generation_id.removeprefix("sha256:")
        or grant.get("authority_generation_id") != generation.generation_id
        or grant.get("request_journal_digest") != _grant_request_journal_digest(request)
        or grant.get("source_sha") != request.source_sha
        or grant.get("candidate_manifest_digest") != request.candidate_manifest_digest
        or grant.get("promotion_run_id") != request.promotion_run_id
        or repository != {"full_name": RECOVERY_REPOSITORY, "id": request.recovery_repository_id}
        or grant.get("transaction_authorization_digest") != _sha256(transaction_authorization)
        or grant.get("stage_scope") != scope
        or owner
        != {
            "principal": receipts.SIGNING_PRINCIPAL,
            "signing_key_fingerprint": current_fingerprint,
            "identity_file_digest": expected_identity_digest,
        }
        or reader.get("credential_id") != scope_authority.get("credential_id")
        or reader.get("scope_authority_digest") != _sha256(reader_material.scope_authority)
        or reader.get("token_fingerprint") != _sha256(recovery_reader_token)
        or reader.get("historical_runtime_verification_digest")
        != _sha256(reader_material.runtime_verification)
        or reader.get("issuance_runtime_verification_digest")
        != _sha256(material.issuance_reader_runtime_verification)
        or authority_binding
        != {
            "receipt_digest": _sha256(generation.authority.receipt),
            "signature_digest": _sha256(generation.authority.signature),
        }
        or issuance_evidence
        != {
            "owner_signing_keys_observation_digest": _sha256(
                material.issuance_owner_signing_keys_observation
            ),
            "reader_runtime_verification_digest": _sha256(
                material.issuance_reader_runtime_verification
            ),
        }
        or grant.get("maintenance_acknowledgement_digest")
        != _sha256(receipts.canonical_json_bytes(acknowledgement))
        or replayed_authority != generation.authority.authority
        or issued_at > now
        or issued_at < authority_observed_at
        or now >= expires_at
        or expires_at > issued_at + timedelta(seconds=MUTATION_GRANT_LIFETIME_SECONDS)
        or expires_at > acknowledgement_expiry
        or expires_at > credential_expiry
        or expires_at > reader_expiry
        or expires_at > authority_expiry
        or current_fingerprint != issuance_fingerprint
        or metadata
        != {
            "schema": "kestrel.recovery_mutation_grant_storage.v1",
            "grant_id": claimed_grant_id,
            "authority_generation_id": generation.generation_id,
            "receipt_digest": _sha256(material.receipt),
            "signature_digest": _sha256(material.signature),
            "issuance_owner_signing_keys_observation_digest": _sha256(
                material.issuance_owner_signing_keys_observation
            ),
            "issuance_reader_runtime_verification_digest": _sha256(
                material.issuance_reader_runtime_verification
            ),
            "validation_status": "validated",
        }
        or grant.get("provenance")
        != {
            "producer": "scripts/recovery_capsule_controller.py",
            "provider": "owner-controller",
            "method": "full-authority-exact-stage-mutation-grant",
        }
        or grant.get("confidence") != 1
        or grant.get("validation_status") != "validated"
    ):
        if now >= expires_at:
            raise receipts.ReleaseControlError("recovery mutation stage grant is expired")
        raise receipts.ReleaseControlError(
            "recovery mutation stage grant scope or binding conflicts"
        )
    return material


def _load_or_create_stage_mutation_grant(
    *,
    request: RecoveryControllerRequest,
    expected_identity_digest: str,
    generation: RecoveryAuthorityGeneration,
    stage_scope: Mapping[str, object],
    transaction_authorization: bytes,
    reader_material: RecoveryReaderMaterial,
    bound_reader_runtime_verification: Mapping[str, object],
    recovery_reader_token: bytes,
    current_owner_signing_keys_observation: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecoveryMutationGrantMaterial:
    """Create or replay a write-once grant for one exact remote mutation stage."""

    _require_signing_identity_binding(request, expected_identity_digest)
    scope = _canonical_stage_scope(stage_scope)
    _require_stage_scope_request_binding(scope, request)
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError("recovery mutation grant clock must be aware UTC")
    now = now.astimezone(UTC).replace(microsecond=0)
    checked_authority = _verify_recovery_authority_bytes(
        receipt=generation.authority.receipt,
        signature=generation.authority.signature,
        owner_signing_keys_observation=(current_owner_signing_keys_observation),
        _clock=lambda: now,
    )
    if checked_authority != generation.authority.authority:
        raise receipts.ReleaseControlError("recovery mutation grant authority replay conflicts")
    _require_current_reader_authority_binding(
        recovery_authority=generation.authority.authority,
        reader_material=reader_material,
        current_runtime_verification=bound_reader_runtime_verification,
        _clock=lambda: now,
    )
    scope_authority = _canonical_object(
        reader_material.scope_authority,
        label="recovery mutation grant reader scope",
    )
    if scope_authority.get("token_fingerprint") != _sha256(recovery_reader_token):
        raise receipts.ReleaseControlError("recovery mutation grant reader token conflicts")
    credentials = receipts._array(  # noqa: SLF001
        generation.authority.authority.get("credentials"),
        label="recovery mutation grant authority credentials",
    )
    if len(credentials) != 1:
        raise receipts.ReleaseControlError(
            "recovery mutation grant authority credential cardinality mismatch"
        )
    credential = receipts._object(  # noqa: SLF001
        credentials[0], label="recovery mutation grant authority credential"
    )
    if credential.get("id") != scope_authority.get("credential_id"):
        raise receipts.ReleaseControlError(
            "recovery mutation grant reader credential binding mismatch"
        )
    acknowledgement = receipts._object(  # noqa: SLF001
        generation.authority.authority.get("maintenance_window_acknowledgement"),
        label="recovery mutation grant acknowledgement",
    )
    expiry = min(
        now + timedelta(seconds=MUTATION_GRANT_LIFETIME_SECONDS),
        receipts.parse_timestamp(
            generation.authority.authority.get("expires_at"),
            label="recovery mutation grant authority expiry",
        ),
        receipts.parse_timestamp(
            acknowledgement.get("expires_at"),
            label="recovery mutation grant acknowledgement expiry",
        ),
        receipts.parse_timestamp(
            credential.get("expires_at"),
            label="recovery mutation grant authority credential expiry",
        ),
        receipts.parse_timestamp(
            scope_authority.get("expires_at"),
            label="recovery mutation grant reader scope expiry",
        ),
    )
    if expiry <= now:
        raise receipts.ReleaseControlError(
            "recovery mutation grant has no owner-acknowledged current window"
        )
    _public_key, owner_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        current_owner_signing_keys_observation,
        expected_fingerprint=None,
    )
    runtime_raw = receipts.canonical_json_bytes(
        cast(receipts.JSONObject, bound_reader_runtime_verification)
    )
    grant: receipts.JSONObject = {
        "schema": "kestrel.recovery_mutation_grant.v1",
        "authority_generation_id": generation.generation_id,
        "request_journal_digest": _grant_request_journal_digest(request),
        "source_sha": request.source_sha,
        "candidate_manifest_digest": request.candidate_manifest_digest,
        "promotion_run_id": request.promotion_run_id,
        "recovery_repository": {
            "full_name": RECOVERY_REPOSITORY,
            "id": request.recovery_repository_id,
        },
        "transaction_authorization_digest": _sha256(transaction_authorization),
        "stage_scope": scope,
        "owner": {
            "principal": receipts.SIGNING_PRINCIPAL,
            "signing_key_fingerprint": owner_fingerprint,
            "identity_file_digest": expected_identity_digest,
        },
        "reader": {
            "credential_id": scope_authority.get("credential_id"),
            "scope_authority_digest": _sha256(reader_material.scope_authority),
            "token_fingerprint": _sha256(recovery_reader_token),
            "historical_runtime_verification_digest": _sha256(reader_material.runtime_verification),
            "issuance_runtime_verification_digest": _sha256(runtime_raw),
        },
        "current_recovery_authority": {
            "receipt_digest": _sha256(generation.authority.receipt),
            "signature_digest": _sha256(generation.authority.signature),
        },
        "issuance_evidence": {
            "owner_signing_keys_observation_digest": _sha256(
                current_owner_signing_keys_observation
            ),
            "reader_runtime_verification_digest": _sha256(runtime_raw),
        },
        "maintenance_acknowledgement_digest": _sha256(
            receipts.canonical_json_bytes(acknowledgement)
        ),
        "issued_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provenance": {
            "producer": "scripts/recovery_capsule_controller.py",
            "provider": "owner-controller",
            "method": "full-authority-exact-stage-mutation-grant",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    grant_id = _sha256(receipts.canonical_json_bytes(grant))
    grant = {"grant_id": grant_id, **grant}
    grants_root = (
        request.work_root
        / "recovery-mutation-grants"
        / generation.generation_id.removeprefix("sha256:")
    )
    grants_parent = grants_root.parent
    if grants_parent.exists() or grants_parent.is_symlink():
        if not grants_parent.is_dir() or grants_parent.is_symlink():
            raise receipts.ReleaseControlError("recovery mutation grants root is invalid")
    else:
        grants_parent.mkdir(mode=0o700)
    if grants_root.exists() or grants_root.is_symlink():
        if not grants_root.is_dir() or grants_root.is_symlink():
            raise receipts.ReleaseControlError("recovery mutation grant generation root is invalid")
    else:
        grants_root.mkdir(mode=0o700)
    grant_root = grants_root / grant_id.removeprefix("sha256:")
    if grant_root.exists() or grant_root.is_symlink():
        if (
            not grant_root.is_dir()
            or grant_root.is_symlink()
            or {entry.name for entry in grant_root.iterdir()}
            != {
                "grant.json",
                "grant.json.sig",
                "grant-metadata.json",
                "issuance-owner-signing-keys-observation.json",
                "issuance-reader-runtime-verification.json",
            }
        ):
            raise receipts.ReleaseControlError("recovery mutation grant inventory is invalid")
        receipt = receipts._read_regular(  # noqa: SLF001
            grant_root / "grant.json",
            label="resumed recovery mutation grant",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        signature = receipts._read_regular(  # noqa: SLF001
            grant_root / "grant.json.sig",
            label="resumed recovery mutation grant signature",
            max_bytes=1024 * 1024,
        )
        issuance_owner_keys = receipts._read_regular(  # noqa: SLF001
            grant_root / "issuance-owner-signing-keys-observation.json",
            label="resumed recovery mutation grant issuance owner keys",
            max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
        )
        issuance_runtime = receipts._read_regular(  # noqa: SLF001
            grant_root / "issuance-reader-runtime-verification.json",
            label="resumed recovery mutation grant issuance reader proof",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
    else:
        receipt = receipts.canonical_json_bytes(grant)
        signature = receipts.sign_receipt_detached(
            receipt=receipt,
            identity_file=request.identity_file,
            principal=receipts.SIGNING_PRINCIPAL,
            namespace=receipts.SIGNING_NAMESPACE,
        )
        receipts.verify_detached_signature(
            receipt=receipt,
            signature=signature,
            expected_fingerprint=owner_fingerprint,
            namespace=receipts.SIGNING_NAMESPACE,
        )
        issuance_owner_keys = current_owner_signing_keys_observation
        issuance_runtime = runtime_raw
        staging = Path(tempfile.mkdtemp(prefix=".mutation-grant-", dir=grants_root))
        staging.chmod(0o700)
        try:
            _write_exclusive(staging / "grant.json", receipt)
            _write_exclusive(staging / "grant.json.sig", signature)
            _write_exclusive(
                staging / "issuance-owner-signing-keys-observation.json",
                issuance_owner_keys,
            )
            _write_exclusive(
                staging / "issuance-reader-runtime-verification.json",
                issuance_runtime,
            )
            metadata = {
                "schema": "kestrel.recovery_mutation_grant_storage.v1",
                "grant_id": grant_id,
                "authority_generation_id": generation.generation_id,
                "receipt_digest": _sha256(receipt),
                "signature_digest": _sha256(signature),
                "issuance_owner_signing_keys_observation_digest": _sha256(issuance_owner_keys),
                "issuance_reader_runtime_verification_digest": _sha256(issuance_runtime),
                "validation_status": "validated",
            }
            _write_exclusive(
                staging / "grant-metadata.json",
                receipts.canonical_json_bytes(metadata),
            )
            os.replace(staging, grant_root)
            staging = grant_root
        except BaseException:
            if staging.exists() and staging != grant_root:
                # The uncommitted hidden directory carries no authority and can
                # be discarded; only the atomic final directory is replayable.
                shutil.rmtree(staging)
            raise
    material = RecoveryMutationGrantMaterial(
        grant=grant,
        receipt=receipt,
        signature=signature,
        root=grant_root,
        issuance_owner_signing_keys_observation=issuance_owner_keys,
        issuance_reader_runtime_verification=issuance_runtime,
    )
    return _validate_stage_mutation_grant(
        material=material,
        request=request,
        expected_identity_digest=expected_identity_digest,
        generation=generation,
        stage_scope=scope,
        transaction_authorization=transaction_authorization,
        reader_material=reader_material,
        bound_reader_runtime_verification=bound_reader_runtime_verification,
        recovery_reader_token=recovery_reader_token,
        current_owner_signing_keys_observation=(current_owner_signing_keys_observation),
        _clock=lambda: now,
    )


def _load_current_recovery_authority(
    *,
    work_root: Path,
    owner_signing_keys_observation: bytes,
    replay_at_observed: bool = False,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecoveryAuthorityMaterial:
    receipt_path = work_root / "current-recovery-authority.json"
    signature_path = work_root / "current-recovery-authority.json.sig"
    if not receipt_path.is_file() or receipt_path.is_symlink():
        raise receipts.ReleaseControlError(
            "current recovery authority receipt is missing or invalid"
        )
    if not signature_path.is_file() or signature_path.is_symlink():
        raise receipts.ReleaseControlError(
            "current recovery authority signature is missing or invalid"
        )
    receipt = receipts._read_regular(  # noqa: SLF001
        receipt_path,
        label="current recovery authority resume receipt",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    signature = receipts._read_regular(  # noqa: SLF001
        signature_path,
        label="current recovery authority resume signature",
        max_bytes=1024 * 1024,
    )
    verification_clock = _clock
    if replay_at_observed:
        decoded = _canonical_object(receipt, label="historical capsule recovery authority")
        observed_at = receipts.parse_timestamp(
            decoded.get("observed_at"),
            label="historical capsule recovery authority observed_at",
        )

        def verification_clock() -> datetime:
            return observed_at

    authority, _verified_at, _fingerprint = receipts._verify_signed_authority(  # noqa: SLF001
        receipt=receipt,
        signature=signature,
        owner_signing_keys_observation=owner_signing_keys_observation,
        schema=receipts.RECOVERY_AUTHORITY_SCHEMA,
        validator=receipts.validate_recovery_repository_authority,
        _clock=verification_clock,
    )
    return RecoveryAuthorityMaterial(
        authority=authority,
        receipt=receipt,
        signature=signature,
    )


def _load_capsule_authority_binding(
    *,
    request: RecoveryControllerRequest,
    reader_material: RecoveryReaderMaterial,
) -> CapsuleAuthorityBinding:
    root = request.work_root / "capsule-authority-binding"
    expected_inventory = {
        "binding.json",
        "current-recovery-authority.json",
        "current-recovery-authority.json.sig",
        "fresh-sources",
        "reader-credential",
    }
    if (
        root.is_symlink()
        or not root.is_dir()
        or {entry.name for entry in root.iterdir()} != expected_inventory
    ):
        raise receipts.ReleaseControlError("capsule authority binding inventory is invalid")
    owner_keys, reader_verification = _load_initial_reader_proof(
        source_root=request.source_root,
        root=root / "reader-credential",
        reader_material=reader_material,
    )
    verified_at = receipts.parse_timestamp(
        reader_verification.get("verified_at"),
        label="capsule authority reader verified_at",
    )
    authority = _load_current_recovery_authority(
        work_root=root,
        owner_signing_keys_observation=owner_keys,
        replay_at_observed=True,
    )
    fresh_sources = _load_fresh_recovery_sources(
        source_root=request.source_root,
        output_root=root / "fresh-sources",
        replay_at_capture=True,
        _clock=lambda: verified_at,
    )
    _require_current_reader_authority_binding(
        recovery_authority=authority.authority,
        reader_material=reader_material,
        current_runtime_verification=reader_verification,
        _clock=lambda: verified_at,
    )
    metadata_raw = receipts._read_regular(  # noqa: SLF001
        root / "binding.json",
        label="capsule authority binding metadata",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    metadata = _canonical_object(metadata_raw, label="capsule authority binding metadata")
    generation_id = receipts._digest(  # noqa: SLF001
        metadata.get("authority_generation_id"),
        label="capsule authority generation ID",
    )
    generation = _load_recovery_authority_generation(
        root=(
            request.work_root
            / "recovery-authority-generations"
            / generation_id.removeprefix("sha256:")
        ),
        generation_id=generation_id,
        current_owner_signing_keys_observation=owner_keys,
        replay_at_observed=True,
        _clock=lambda: verified_at,
    )
    fresh_identity = _directory_file_identity(
        root / "fresh-sources", label="capsule authority fresh sources"
    )
    if (
        metadata
        != {
            "schema": "kestrel.recovery_capsule_authority_binding.v1",
            "authority_generation_id": generation_id,
            "authority_receipt_digest": _sha256(authority.receipt),
            "authority_signature_digest": _sha256(authority.signature),
            "owner_signing_keys_observation_digest": _sha256(owner_keys),
            "reader_runtime_verification_digest": _sha256(
                receipts.canonical_json_bytes(reader_verification)
            ),
            "fresh_sources_identity_digest": _sha256(receipts.canonical_json_bytes(fresh_identity)),
            "validation_status": "validated",
        }
        or generation.authority.receipt != authority.receipt
        or generation.authority.signature != authority.signature
        or generation.authority.authority != authority.authority
        or fresh_sources.repository_id != request.recovery_repository_id
    ):
        raise receipts.ReleaseControlError("capsule authority binding metadata conflicts")
    return CapsuleAuthorityBinding(
        root=root,
        authority_generation_id=generation_id,
        authority=authority,
        fresh_sources=fresh_sources,
        owner_signing_keys_observation=owner_keys,
        reader_runtime_verification=reader_verification,
    )


def _load_or_create_capsule_authority_binding(
    *,
    request: RecoveryControllerRequest,
    expected_identity_digest: str,
    reader_material: RecoveryReaderMaterial,
    transaction_authorization: Mapping[str, object],
    expected_repository_id: int,
    owner_read_api: transaction.GitHubReadAPI,
    recovery_reader_api: transaction.GitHubReadAPI,
    recovery_reader_token: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> CapsuleAuthorityBinding:
    """Atomically freeze the first fresh generation as capsule-only evidence."""

    root = request.work_root / "capsule-authority-binding"
    if root.exists() or root.is_symlink():
        return _load_capsule_authority_binding(
            request=request,
            reader_material=reader_material,
        )
    _require_signing_identity_binding(request, expected_identity_digest)
    owner_keys = capture_fresh_owner_signing_keys(
        source_root=request.source_root,
        api=owner_read_api,
        _clock=_clock,
    )
    reader_verification = verify_recovery_reader_credential(
        material=reader_material,
        token_bytes=recovery_reader_token,
        transaction_authorization=transaction_authorization,
        expected_repository_id=expected_repository_id,
        api=recovery_reader_api,
        current_owner_signing_keys_observation=owner_keys,
        _clock=_clock,
    )
    generation = _load_or_create_recovery_authority_generation(
        request=request,
        expected_identity_digest=expected_identity_digest,
        current_owner_signing_keys_observation=owner_keys,
        expected_repository_id=expected_repository_id,
        _clock=_clock,
    )
    _require_current_reader_authority_binding(
        recovery_authority=generation.authority.authority,
        reader_material=reader_material,
        current_runtime_verification=reader_verification,
        _clock=_clock,
    )
    staging = Path(tempfile.mkdtemp(prefix=".capsule-authority-binding-", dir=request.work_root))
    staging.chmod(0o700)
    try:
        fresh_sources = capture_fresh_recovery_sources(
            source_root=request.source_root,
            output_root=staging / "fresh-sources",
            api=recovery_reader_api,
            owner_signing_keys_observation=owner_keys,
            _clock=_clock,
        )
        if fresh_sources.repository_id != expected_repository_id:
            raise receipts.ReleaseControlError(
                "capsule authority repository ID differs from owner input"
            )
        reader_root = staging / "reader-credential"
        reader_root.mkdir(mode=0o700)
        _write_exclusive(reader_root / "owner-signing-keys-observation.json", owner_keys)
        reader_raw = receipts.canonical_json_bytes(reader_verification)
        _write_exclusive(reader_root / "runtime-verification.json", reader_raw)
        _write_exclusive(
            staging / "current-recovery-authority.json",
            generation.authority.receipt,
        )
        _write_exclusive(
            staging / "current-recovery-authority.json.sig",
            generation.authority.signature,
        )
        fresh_identity = _directory_file_identity(
            staging / "fresh-sources", label="capsule authority fresh sources"
        )
        metadata = {
            "schema": "kestrel.recovery_capsule_authority_binding.v1",
            "authority_generation_id": generation.generation_id,
            "authority_receipt_digest": _sha256(generation.authority.receipt),
            "authority_signature_digest": _sha256(generation.authority.signature),
            "owner_signing_keys_observation_digest": _sha256(owner_keys),
            "reader_runtime_verification_digest": _sha256(reader_raw),
            "fresh_sources_identity_digest": _sha256(receipts.canonical_json_bytes(fresh_identity)),
            "validation_status": "validated",
        }
        _write_exclusive(staging / "binding.json", receipts.canonical_json_bytes(metadata))
        os.replace(staging, root)
        staging = root
    except BaseException:
        if staging.exists() and staging != root:
            shutil.rmtree(staging)
        raise
    return _load_capsule_authority_binding(
        request=request,
        reader_material=reader_material,
    )


def _require_current_reader_authority_binding(
    *,
    recovery_authority: Mapping[str, object],
    reader_material: RecoveryReaderMaterial,
    current_runtime_verification: Mapping[str, object],
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> None:
    """Join the dashboard credential inventory to the credential proven live."""

    credentials = receipts._array(  # noqa: SLF001
        recovery_authority.get("credentials"),
        label="current recovery authority credentials",
    )
    if len(credentials) != 1:
        raise receipts.ReleaseControlError(
            "current recovery authority reader credential cardinality mismatch"
        )
    credential = receipts._object(  # noqa: SLF001
        credentials[0], label="current recovery authority reader credential"
    )
    scope = _canonical_object(
        reader_material.scope_authority,
        label="current recovery reader scope authority",
    )
    runtime = receipts._object(  # noqa: SLF001
        current_runtime_verification,
        label="current recovery reader runtime verification",
    )
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError(
            "current recovery reader binding clock must be aware UTC"
        )
    now = now.astimezone(UTC).replace(microsecond=0)
    credential_expiry = receipts.parse_timestamp(
        credential.get("expires_at"),
        label="current recovery authority reader expiry",
    )
    scope_expiry = receipts.parse_timestamp(
        scope.get("expires_at"), label="current recovery reader scope expiry"
    )
    if (
        credential.get("kind") != "pat"
        or credential.get("purpose") != "recovery_reader"
        or credential.get("active") is not True
        or credential.get("capabilities") != ["repository_read"]
        or credential.get("id") != scope.get("credential_id")
        or scope.get("purpose") != "recovery_reader"
        or runtime.get("schema") != receipts.RUNTIME_CREDENTIAL_SCHEMA
        or runtime.get("credential_id") != scope.get("credential_id")
        or runtime.get("purpose") != "recovery_reader"
        or runtime.get("token_fingerprint") != scope.get("token_fingerprint")
        or runtime.get("scope_authority_digest") != _sha256(reader_material.scope_authority)
        or runtime.get("validation_status") != "validated"
        or credential.get("scope_authority_digest") != _sha256(reader_material.scope_authority)
        or credential.get("runtime_verification_digest")
        != _sha256(reader_material.runtime_verification)
        or credential_expiry != scope_expiry
        or now >= credential_expiry
    ):
        raise receipts.ReleaseControlError(
            "current recovery authority reader runtime verification binding mismatch"
        )


def _require_signing_identity_binding(
    request: RecoveryControllerRequest,
    expected_digest: str,
) -> None:
    """Require the original private signing identity bytes at the exact path."""

    identity = request.identity_file
    checked_digest = receipts._digest(  # noqa: SLF001
        expected_digest,
        label="recovery controller signing identity digest",
    )
    if (
        not identity.is_absolute()
        or not identity.is_file()
        or identity.is_symlink()
        or identity.resolve(strict=True) != identity
        or identity.stat().st_mode & 0o077
        or _path_sha256(identity) != checked_digest
    ):
        raise receipts.ReleaseControlError("recovery controller signing identity bytes changed")


def _authorize_current_stage_mutation(
    *,
    request: RecoveryControllerRequest,
    expected_identity_digest: str,
    stage_scope: Mapping[str, object],
    reader_material: RecoveryReaderMaterial,
    transaction_authorization: bytes,
    transaction_authorization_record: Mapping[str, object],
    expected_repository_id: int,
    owner_read_api: transaction.GitHubReadAPI,
    recovery_reader_api: transaction.GitHubReadAPI,
    recovery_reader_token: bytes,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RecoveryMutationGrantMaterial:
    """Issue and validate a fresh exact-stage grant before one remote mutation."""

    _require_signing_identity_binding(request, expected_identity_digest)
    current_owner_keys = capture_fresh_owner_signing_keys(
        source_root=request.source_root,
        api=owner_read_api,
        _clock=_clock,
    )
    current_reader_verification = verify_recovery_reader_credential(
        material=reader_material,
        token_bytes=recovery_reader_token,
        transaction_authorization=transaction_authorization_record,
        expected_repository_id=expected_repository_id,
        api=recovery_reader_api,
        current_owner_signing_keys_observation=current_owner_keys,
        _clock=_clock,
    )
    generation = _load_or_create_recovery_authority_generation(
        request=request,
        expected_identity_digest=expected_identity_digest,
        current_owner_signing_keys_observation=current_owner_keys,
        expected_repository_id=expected_repository_id,
        _clock=_clock,
    )
    _require_current_reader_authority_binding(
        recovery_authority=generation.authority.authority,
        reader_material=reader_material,
        current_runtime_verification=current_reader_verification,
        _clock=_clock,
    )
    _require_clean_source_identity(request)
    return _load_or_create_stage_mutation_grant(
        request=request,
        expected_identity_digest=expected_identity_digest,
        generation=generation,
        stage_scope=stage_scope,
        transaction_authorization=transaction_authorization,
        reader_material=reader_material,
        bound_reader_runtime_verification=current_reader_verification,
        recovery_reader_token=recovery_reader_token,
        current_owner_signing_keys_observation=current_owner_keys,
        _clock=_clock,
    )


def verify_recovery_reader_credential(
    *,
    material: RecoveryReaderMaterial,
    token_bytes: bytes,
    transaction_authorization: Mapping[str, object],
    expected_repository_id: int,
    api: transaction.GitHubReadAPI,
    current_owner_signing_keys_observation: bytes | None = None,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> receipts.JSONObject:
    """Replay the signed scope, then prove its exact read-only surface now."""

    runtime = _canonical_object(
        material.runtime_verification,
        label="recovery reader historical runtime verification",
    )
    receipts._validate_schema(  # noqa: SLF001
        receipts.RUNTIME_CREDENTIAL_SCHEMA,
        runtime,
        label="recovery reader historical runtime verification",
    )
    historical_time = receipts.parse_timestamp(
        runtime.get("verified_at"),
        label="recovery reader historical verified_at",
    )
    authorized_at = receipts.parse_timestamp(
        transaction_authorization.get("authorized_at"),
        label="recovery reader transaction authorized_at",
    )
    if historical_time > authorized_at:
        raise receipts.ReleaseControlError(
            "recovery reader verification follows transaction authorization"
        )
    replay = receipts.verify_runtime_credential(
        scope_authority=material.scope_authority,
        scope_authority_signature=material.scope_signature,
        owner_signing_keys_observation=material.owner_signing_keys_observation,
        identity_probe=material.identity_probe,
        endpoint_probe_observations=material.endpoint_probes,
        token_bytes=token_bytes,
        _clock=lambda: historical_time,
    )
    if receipts.canonical_json_bytes(replay) != material.runtime_verification:
        raise receipts.ReleaseControlError(
            "recovery reader historical runtime verification does not replay"
        )
    scope = _canonical_object(
        material.scope_authority,
        label="recovery reader scope authority",
    )
    receipts._validate_recovery_reader_scope(  # noqa: SLF001
        scope,
        now=authorized_at,
    )
    repositories = receipts._array(  # noqa: SLF001
        scope.get("repositories"), label="recovery reader repositories"
    )
    if len(repositories) != 1:
        raise receipts.ReleaseControlError(
            "recovery reader repository authority is not an exact singleton"
        )
    repository_scope = receipts._object(  # noqa: SLF001
        repositories[0], label="recovery reader repository authority"
    )
    checked_repository_id = receipts._safe_integer(  # noqa: SLF001
        expected_repository_id,
        label="recovery reader expected repository ID",
        positive=True,
    )
    if repository_scope != {
        "full_name": RECOVERY_REPOSITORY,
        "id": checked_repository_id,
    }:
        raise receipts.ReleaseControlError("recovery reader repository scope identity mismatch")
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError(
            "recovery reader current verification clock must be aware UTC"
        )
    now = now.astimezone(UTC).replace(microsecond=0)
    if now < authorized_at:
        raise receipts.ReleaseControlError(
            "recovery reader current verification predates authorization"
        )
    receipts._validate_recovery_reader_scope(scope, now=now)  # noqa: SLF001
    promotion_run = receipts._object(  # noqa: SLF001
        transaction_authorization.get("promotion_run"),
        label="recovery reader transaction promotion run",
    )
    nonce = receipts._nonce(  # noqa: SLF001
        promotion_run.get("transaction_nonce"),
        label="recovery reader transaction nonce",
    )
    admission_tag = f"dispatch-admission-{nonce}"

    repository_exchange = api(
        f"GET /repos/{RECOVERY_REPOSITORY}",
        accept="application/vnd.github+json",
    )
    if repository_exchange.http_status != 200:
        raise receipts.ReleaseControlError("recovery reader repository probe failed")
    repository = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(
            repository_exchange.response_body,
            label="recovery reader repository probe",
        ),
        label="recovery reader repository probe",
    )
    if (
        repository.get("id") != checked_repository_id
        or repository.get("full_name") != RECOVERY_REPOSITORY
        or repository.get("private") is not True
    ):
        raise receipts.ReleaseControlError("recovery reader repository probe identity mismatch")

    release_exchange = api(
        f"GET /repos/{RECOVERY_REPOSITORY}/releases/tags/{admission_tag}",
        accept="application/vnd.github+json",
    )
    if release_exchange.http_status != 200:
        raise receipts.ReleaseControlError("recovery reader admission Release probe failed")
    release = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(
            release_exchange.response_body,
            label="recovery reader admission Release probe",
        ),
        label="recovery reader admission Release probe",
    )
    assets = [
        receipts._object(item, label="recovery reader admission asset")  # noqa: SLF001
        for item in receipts._array(  # noqa: SLF001
            release.get("assets"), label="recovery reader admission assets"
        )
    ]
    if (
        release.get("tag_name") != admission_tag
        or release.get("draft") is not False
        or release.get("immutable") is not True
        or len(assets) != 2
        or {asset.get("name") for asset in assets}
        != {"dispatch-admission.json", "dispatch-admission.json.sig"}
    ):
        raise receipts.ReleaseControlError(
            "recovery reader admission Release probe is not exact and immutable"
        )
    admission_assets = [asset for asset in assets if asset.get("name") == "dispatch-admission.json"]
    if len(admission_assets) != 1:
        raise receipts.ReleaseControlError("recovery reader admission asset probe is ambiguous")
    admission_asset = admission_assets[0]
    asset_id = receipts._safe_integer(  # noqa: SLF001
        admission_asset.get("id"),
        label="recovery reader admission asset ID",
        positive=True,
    )
    asset_size = receipts._safe_integer(  # noqa: SLF001
        admission_asset.get("size"),
        label="recovery reader admission asset size",
        positive=True,
    )
    asset_exchange = api(
        f"GET /repos/{RECOVERY_REPOSITORY}/releases/assets/{asset_id}",
        accept="application/octet-stream",
    )
    if asset_exchange.http_status != 200 or len(asset_exchange.response_body) != asset_size:
        raise receipts.ReleaseControlError("recovery reader admission asset probe failed")

    identity_exchange = api("GET /user", accept="application/vnd.github+json")
    if identity_exchange.http_status != 200:
        raise receipts.ReleaseControlError("recovery reader identity probe failed")
    identity_value = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(
            identity_exchange.response_body,
            label="recovery reader identity probe",
        ),
        label="recovery reader identity probe",
    )
    identity_probe = receipts.canonical_json_bytes(
        {
            "login": identity_value.get("login"),
            "id": identity_value.get("id"),
            "type": identity_value.get("type"),
        }
    )
    forbidden_exchange = api(
        "GET /repos/John-MiracleWorker/Kestrel/actions/permissions",
        accept="application/vnd.github+json",
    )
    probe_results: list[receipts.JSONObject] = [
        {
            "endpoint": "GET /repos/{repository}",
            "http_status": repository_exchange.http_status,
            "response_digest": _sha256(repository_exchange.response_body),
        },
        {
            "endpoint": "GET /repos/{repository}/releases/tags/{tag}",
            "http_status": release_exchange.http_status,
            "response_digest": _sha256(release_exchange.response_body),
        },
        {
            "endpoint": "GET /repos/{repository}/releases/assets/{asset_id}",
            "http_status": asset_exchange.http_status,
            "response_digest": _sha256(asset_exchange.response_body),
        },
        {
            "endpoint": "GET /user",
            "http_status": identity_exchange.http_status,
            "response_digest": _sha256(identity_exchange.response_body),
        },
        {
            "endpoint": "GET /repos/John-MiracleWorker/Kestrel/actions/permissions",
            "http_status": forbidden_exchange.http_status,
            "response_digest": _sha256(forbidden_exchange.response_body),
        },
    ]
    probe_results.sort(key=lambda item: cast(str, item["endpoint"]))
    fresh_probes = receipts.canonical_json_bytes(
        {
            "schema": "kestrel.credential_endpoint_probes.v1",
            "credential_id": scope.get("credential_id"),
            "results": cast(list[receipts.JSONValue], probe_results),
            "captured_at": receipts._format_timestamp(  # noqa: SLF001
                now, label="recovery reader fresh probe time"
            ),
            "complete": True,
        }
    )
    return receipts.verify_runtime_credential(
        scope_authority=material.scope_authority,
        scope_authority_signature=material.scope_signature,
        owner_signing_keys_observation=(
            current_owner_signing_keys_observation
            if current_owner_signing_keys_observation is not None
            else material.owner_signing_keys_observation
        ),
        identity_probe=identity_probe,
        endpoint_probe_observations=fresh_probes,
        token_bytes=token_bytes,
        _clock=lambda: now,
    )


def validate_authorization_artifact(
    *,
    root: Path,
    source_root: Path,
    source_sha: str,
    promotion_run_id: int,
    candidate_manifest_digest: str,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AuthorizationMaterial:
    """Validate the exact initiate authorization product before capsule creation."""

    if not root.is_dir() or root.is_symlink():
        raise receipts.ReleaseControlError("release authorization artifact root is invalid")
    entries = tuple(root.iterdir())
    expected_top_level = {
        "authority-evidence",
        "candidate",
        "release-authorization.json",
        "transaction-identity",
    }
    if {entry.name for entry in entries} != expected_top_level:
        raise receipts.ReleaseControlError(
            "release authorization artifact top-level inventory is not exact"
        )
    candidate_root = root / "candidate"
    manifest_path = candidate_root / "candidate-manifest.json"
    manifest_raw = receipts._read_regular(  # noqa: SLF001
        manifest_path,
        label="controller candidate manifest",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    candidate, repository_id = receipts._candidate_from_manifest(manifest_raw)  # noqa: SLF001
    try:
        verified = candidates.verify_candidate_bundle(
            _canonical_object(manifest_raw, label="controller candidate manifest"),
            bundle_root=candidate_root,
            source_root=source_root,
        )
    except ValueError as exc:
        raise receipts.ReleaseControlError(
            f"controller candidate bundle verification failed: {exc}"
        ) from exc
    for field in (
        "candidate_manifest_digest",
        "artifact_set_digest",
        "source_sha",
        "source_tree",
        "tag",
        "version",
    ):
        if verified.get(field) != candidate.get(field):
            raise receipts.ReleaseControlError("controller candidate bundle identity mismatch")
    checked_source_sha = receipts._git_sha(  # noqa: SLF001
        source_sha, label="controller source SHA"
    )
    checked_candidate_digest = receipts._digest(  # noqa: SLF001
        candidate_manifest_digest, label="controller candidate manifest digest"
    )
    if (
        candidate.get("source_sha") != checked_source_sha
        or candidate.get("candidate_manifest_digest") != checked_candidate_digest
        or repository_id <= 0
    ):
        raise receipts.ReleaseControlError("controller candidate request binding mismatch")

    transaction_raw = receipts._read_regular(  # noqa: SLF001
        root / "release-authorization.json",
        label="controller transaction authorization",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    transaction_value = transaction.validate_server_authorization(
        _canonical_object(transaction_raw, label="controller transaction authorization"),
        expected_original_transaction_digest=None,
    )
    promotion_run = receipts._object(  # noqa: SLF001
        transaction_value.get("promotion_run"),
        label="controller authorization promotion run",
    )
    if (
        transaction_value.get("authorization_kind") != "transaction"
        or transaction_value.get("mode") != "initiate"
        or transaction_value.get("candidate") != candidate
        or promotion_run.get("run_id") != promotion_run_id
        or promotion_run.get("repository_id") != repository_id
        or promotion_run.get("head_sha") != checked_source_sha
    ):
        raise receipts.ReleaseControlError("controller transaction authorization identity mismatch")
    authorized_at = receipts.parse_timestamp(
        transaction_value.get("authorized_at"),
        label="controller transaction authorized_at",
    )
    controller_now = _clock()
    if controller_now.tzinfo is None or controller_now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError(
            "recovery controller authorization clock must be aware UTC"
        )
    if controller_now.astimezone(UTC).replace(microsecond=0) < authorized_at:
        raise receipts.ReleaseControlError("controller transaction authorization is in the future")

    approval_path = root / "authority-evidence" / "approval-history-observation.json"
    approval_raw = receipts._read_regular(  # noqa: SLF001
        approval_path,
        label="controller approval history observation",
        max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
    )
    approval_body = receipts.source_observation_body_for_contract(
        approval_raw,
        registry=_source_registry(source_root),
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="approval-history-observation",
        _clock=lambda: authorized_at,
    )
    approval = _canonical_object(approval_body, label="controller approval history observation")
    transaction._require_cumulative_owner_approvals(  # noqa: SLF001
        approval, expected_environments=("release",)
    )
    if transaction_value.get("approval_history") != approval:
        raise receipts.ReleaseControlError(
            "controller approval history differs from transaction authorization"
        )
    required = (
        root / "transaction-identity" / "dispatch-admission.json",
        root / "transaction-identity" / "dispatch-admission.json.sig",
        root / "transaction-identity" / "dispatch-admission-verification.json",
        root / "authority-evidence" / "github-admission-authority-verification.json",
        root / "authority-evidence" / "recovery-authority-verification.json",
    )
    if any(not path.is_file() or path.is_symlink() for path in required):
        raise receipts.ReleaseControlError("controller authorization evidence is incomplete")
    return AuthorizationMaterial(
        root=root,
        candidate_root=candidate_root,
        candidate=candidate,
        candidate_manifest=manifest_raw,
        transaction_authorization=transaction_raw,
        transaction=transaction_value,
        approval_history_observation=approval_raw,
    )


def _probe_final_python(
    *, destination: Path, dependency_root: Path
) -> tuple[list[str], dict[str, str], str, Path, dict[str, Any]]:
    """Build the reference environment at the exact later extraction path."""

    runtime_root = destination.parent / "recovery-runtime"
    base_root = runtime_root / "base"
    environment_root = runtime_root / "environment"
    probe_capsule = runtime_root / "probe-capsule"
    requirements = dependency_root / "recovery" / "requirements.txt"
    wheelhouse = dependency_root / "recovery" / "wheelhouse"
    python_manifest = json.loads(
        (dependency_root / "recovery" / "python-runtime-manifest.json").read_bytes()
    )
    if runtime_root.exists() or runtime_root.is_symlink():
        raise ValueError("recovery controller reference runtime path must be absent")
    runtime_parent_existed = runtime_root.parent.exists()
    shutil.copytree(
        dependency_root / "recovery" / "runtime",
        probe_capsule / "recovery" / "runtime",
    )
    for path in (probe_capsule / "recovery" / "runtime").iterdir():
        path.chmod(0o500 if path.name.startswith("ld-linux-") else 0o400)
    base_python = bootstrap_recovery._extract_python_runtime(  # noqa: SLF001
        archive=dependency_root / "recovery" / "python-runtime.tar.gz",
        destination=base_root,
        manifest=python_manifest,
    )
    bootstrap_recovery._run_bootstrap_command(  # noqa: SLF001
        bootstrap_recovery._private_loader_command(  # noqa: SLF001
            capsule_root=probe_capsule,
            executable=base_python,
            arguments=(
                "-I",
                "-S",
                "-B",
                "-m",
                "venv",
                "--copies",
                str(environment_root),
            ),
            additional_library_roots=(base_root / "lib",),
        ),
        environment=bootstrap_recovery._bootstrap_environment(),  # noqa: SLF001
    )
    venv_python = environment_root / "bin" / "python"
    try:
        bootstrap_recovery._run_bootstrap_command(  # noqa: SLF001
            bootstrap_recovery._private_loader_command(  # noqa: SLF001
                capsule_root=probe_capsule,
                executable=venv_python,
                arguments=(
                    "-I",
                    "-B",
                    "-m",
                    "pip",
                    "--isolated",
                    "install",
                    "--no-compile",
                    "--no-index",
                    "--find-links",
                    str(wheelhouse),
                    "--require-hashes",
                    "--only-binary=:all:",
                    "-r",
                    str(requirements),
                ),
                additional_library_roots=(base_root / "lib",),
            ),
            environment=bootstrap_recovery._bootstrap_environment(),  # noqa: SLF001
        )
        bootstrap_recovery._run_bootstrap_command(  # noqa: SLF001
            bootstrap_recovery._private_loader_command(  # noqa: SLF001
                capsule_root=probe_capsule,
                executable=venv_python,
                arguments=("-I", "-B", "-m", "pip", "--isolated", "check"),
                additional_library_roots=(base_root / "lib",),
            ),
            environment=bootstrap_recovery._bootstrap_environment(),  # noqa: SLF001
        )
        probe = (
            "import json,platform,sys;"
            "print(json.dumps({'implementation':platform.python_implementation(),"
            "'version':platform.python_version(),"
            "'abi':f'cp{sys.version_info.major}{sys.version_info.minor}',"
            "'sys_path':sys.path},sort_keys=True,separators=(',',':')))"
        )
        command = bootstrap_recovery._private_loader_command(  # noqa: SLF001
            capsule_root=probe_capsule,
            executable=venv_python,
            arguments=("-I", "-B", "-c", probe),
            additional_library_roots=(base_root / "lib",),
        )
        completed = subprocess.run(  # noqa: S603  # nosec B603
            command,
            check=False,
            capture_output=True,
            env=bootstrap_recovery._bootstrap_environment(),  # noqa: SLF001
            timeout=30,
        )
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            raise ValueError("recovery controller could not inspect private Python")
        observed = json.loads(completed.stdout)
        if type(observed) is not dict or set(observed) != {
            "implementation",
            "version",
            "abi",
            "sys_path",
        }:
            raise ValueError("recovery controller private Python probe is invalid")
        raw_path = observed.pop("sys_path")
        if type(raw_path) is not list or any(type(item) is not str for item in raw_path):
            raise ValueError("recovery controller private Python path is invalid")
        runtime = {name: str(observed[name]) for name in ("implementation", "version", "abi")}
        effective_path = recovery_launcher.effective_recovery_sys_path(
            capsule_root=destination,
            interpreter_sys_path=raw_path,
        )
        count, total, tree_digest = recovery_launcher._installed_environment_tree_identity(  # noqa: SLF001
            environment_root
        )
        environment_manifest = {
            "schema": "kestrel.recovery_environment.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": runtime["version"],
            "python_abi": runtime["abi"],
            "environment_root": str(environment_root),
            "site_packages_path": str(environment_root / "lib" / "python3.11" / "site-packages"),
            "site_packages_tree_sha256": tree_digest,
            "site_packages_file_count": count,
            "site_packages_total_size_bytes": total,
        }
        receipts._validate_schema(  # noqa: SLF001
            "kestrel.recovery_environment.v1",
            environment_manifest,
            label="recovery installed environment manifest",
        )
        return (
            effective_path,
            runtime,
            _path_sha256(venv_python),
            base_root / "lib",
            environment_manifest,
        )
    finally:
        shutil.rmtree(runtime_root)
        if not runtime_parent_existed and runtime_root.parent.is_dir():
            if not any(runtime_root.parent.iterdir()):
                runtime_root.parent.rmdir()


def _evidence_members(evidence_root: Path | None) -> dict[str, bytes]:
    if evidence_root is None:
        return {
            "evidence/recovery-smoke.json": receipts.canonical_json_bytes(
                {
                    "schema": "kestrel.recovery_smoke_evidence.v1",
                    "complete": True,
                }
            )
        }
    if not evidence_root.is_dir() or evidence_root.is_symlink():
        raise ValueError("recovery controller evidence root is invalid")
    members: dict[str, bytes] = {}
    for path in sorted(evidence_root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise ValueError("recovery controller evidence contains a symlink")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("recovery controller evidence contains a special file")
        relative = path.relative_to(evidence_root).as_posix()
        name = f"evidence/{relative}"
        receipts._validate_capsule_asset_name(name)  # noqa: SLF001
        members[name] = receipts._read_regular(  # noqa: SLF001
            path,
            label=f"recovery controller evidence {relative}",
            max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
        )
    if not members:
        raise ValueError("recovery controller evidence root is empty")
    return members


def build_recovery_execution_closure(
    *,
    source_root: Path,
    dependency_root: Path,
    destination: Path,
    candidate_archive: Path,
    environment_manifest_output: Path,
    evidence_root: Path | None = None,
    target_source_root: Path | None = None,
    prepared_environment: PreparedProductionCapsule | None = None,
) -> dict[str, Any]:
    """Build the static closure from real assets for one exact target path."""

    target_source = source_root if target_source_root is None else target_source_root
    if not target_source.is_absolute():
        raise ValueError("recovery controller target source root must be absolute")
    member_bytes: dict[str, bytes] = {}
    for name in sorted(receipts._RECOVERY_CAPSULE_SOURCE_ASSETS):  # noqa: SLF001
        member_bytes[name] = (source_root / name).read_bytes()
    for name in sorted(receipts._RECOVERY_CAPSULE_SCHEMA_ASSETS):  # noqa: SLF001
        member_bytes[name] = (source_root / name).read_bytes()
    member_bytes.update(_evidence_members(evidence_root))
    member_bytes["candidate-archive.tar"] = receipts._read_regular(  # noqa: SLF001
        candidate_archive,
        label="recovery controller candidate archive",
        max_bytes=2_147_483_648,
    )
    for name in (
        "recovery/bin/bwrap",
        "recovery/python-runtime-manifest.json",
        "recovery/python-runtime.tar.gz",
        "recovery/requirements.txt",
        "recovery/runtime-manifest.json",
        "recovery/wheelhouse-manifest.json",
    ):
        member_bytes[name] = (dependency_root / name).read_bytes()
    runtime_manifest = json.loads(member_bytes["recovery/runtime-manifest.json"])
    runtime_files = runtime_manifest.get("files")
    if type(runtime_files) is not list or not runtime_files:
        raise ValueError("recovery controller runtime manifest is incomplete")
    for raw_item in runtime_files:
        if type(raw_item) is not dict or type(raw_item.get("asset_path")) is not str:
            raise ValueError("recovery controller runtime file is invalid")
        asset_path = raw_item["asset_path"]
        member_bytes[asset_path] = (dependency_root / asset_path).read_bytes()

    python_names = sorted(name for name in member_bytes if name.endswith(".py"))
    shell_names = sorted(name for name in member_bytes if name.endswith(".sh"))
    data_names = sorted(set(member_bytes) - set(python_names) - set(shell_names))
    modules = {
        recovery_launcher._module_name(name): name  # noqa: SLF001
        for name in python_names
    }
    static_edges: set[tuple[str, str, str]] = set()
    dynamic_edges: set[tuple[str, str, str]] = set()
    for name in python_names:
        raw = member_bytes[name]
        static_edges.update(
            recovery_launcher._local_static_imports(  # noqa: SLF001
                member_path=name,
                source=raw,
                modules=modules,
            )
        )
        for importer, module in recovery_launcher._literal_dynamic_imports(  # noqa: SLF001
            member_path=name,
            source=raw,
        ):
            target = modules.get(module)
            if target is None:
                raise ValueError("recovery controller dynamic import leaves the closure")
            dynamic_edges.add((importer, module, target))

    requirements = dependency_root / "recovery" / "requirements.txt"
    wheelhouse_manifest = dependency_root / "recovery" / "wheelhouse-manifest.json"
    if prepared_environment is None:
        (
            sys_path,
            runtime,
            python_digest,
            base_library_root,
            environment_manifest,
        ) = _probe_final_python(
            destination=destination,
            dependency_root=dependency_root,
        )
        environment_manifest_raw = receipts.canonical_json_bytes(environment_manifest)
    else:
        sys_path = list(prepared_environment.sys_path)
        runtime = dict(prepared_environment.runtime)
        python_digest = prepared_environment.python_sha256
        base_library_root = prepared_environment.base_library_root
        environment_manifest = dict(prepared_environment.environment_manifest)
        environment_manifest_raw = prepared_environment.environment_manifest_raw
    _write_exclusive(environment_manifest_output, environment_manifest_raw)
    member_bytes["recovery/environment-manifest.json"] = environment_manifest_raw
    data_names = sorted(set(member_bytes) - set(python_names) - set(shell_names))
    python_identity = (
        python_digest,
        f"Python {runtime['version']}",
        runtime["implementation"],
        runtime["version"],
        runtime["abi"],
    )
    trusted_python = bootstrap_recovery.TRUSTED_RECOVERY_PYTHON_IDENTITIES.get(
        (sys.platform, platform.machine()), frozenset()
    )
    if python_identity not in trusted_python:
        raise ValueError("recovery controller Python identity is not frozen")

    staging_receipt = json.loads(
        (dependency_root / "recovery" / "dependency-staging-receipt.json").read_bytes()
    )
    sandbox_digest = staging_receipt["outputs"]["bubblewrap_sha256"]
    sandbox_version = staging_receipt["outputs"]["bubblewrap_version"]
    trusted_sandbox = bootstrap_recovery.TRUSTED_OS_SANDBOX_IDENTITIES.get(
        (sys.platform, platform.machine()), frozenset()
    )
    if (sandbox_digest, sandbox_version) not in trusted_sandbox:
        raise ValueError("recovery controller sandbox identity is not frozen")

    io_roots = {path: "read" for path in sys_path}
    io_roots[str(destination.parent)] = "read_write"
    io_roots[str(destination)] = "read"
    io_roots[str(base_library_root)] = "read"
    io_roots[str(target_source)] = "read"

    return {
        "schema": "kestrel.recovery_execution_closure.v1",
        "python_members": [
            {"path": name, "sha256": _sha256(member_bytes[name])} for name in python_names
        ],
        "static_imports": [
            {
                "importer": importer,
                "module": module,
                "member_path": target,
                "member_sha256": _sha256(member_bytes[target]),
            }
            for importer, module, target in sorted(static_edges)
        ],
        "dynamic_imports": [
            {
                "importer": importer,
                "module": module,
                "member_path": target,
                "member_sha256": _sha256(member_bytes[target]),
            }
            for importer, module, target in sorted(dynamic_edges)
        ],
        "shell_helpers": [
            {"path": name, "sha256": _sha256(member_bytes[name])} for name in shell_names
        ],
        "data_resources": [
            {"path": name, "sha256": _sha256(member_bytes[name])} for name in data_names
        ],
        "external_executables": [
            {
                "name": "python",
                "path": str(
                    destination.parent / "recovery-runtime" / "environment" / "bin" / "python"
                ),
                "sha256": python_digest,
                "version": f"Python {runtime['version']}",
            },
            {
                "name": "sandbox",
                "path": str(destination / "recovery" / "bin" / "bwrap"),
                "sha256": sandbox_digest,
                "version": sandbox_version,
            },
        ],
        "runtime_files": runtime_files,
        "python_runtime": runtime,
        "dependency_lock": {
            "requirements_path": "recovery/requirements.txt",
            "requirements_sha256": _path_sha256(requirements),
            "environment_manifest_sha256": _sha256(environment_manifest_raw),
            "wheelhouse_manifest_sha256": _path_sha256(wheelhouse_manifest),
            "runtime_manifest_sha256": _path_sha256(
                dependency_root / "recovery" / "runtime-manifest.json"
            ),
            "python_runtime_manifest_sha256": _path_sha256(
                dependency_root / "recovery" / "python-runtime-manifest.json"
            ),
            "python_runtime_archive_sha256": _path_sha256(
                dependency_root / "recovery" / "python-runtime.tar.gz"
            ),
        },
        "sys_path": sys_path,
        "io_roots": [{"path": path, "access": io_roots[path]} for path in sorted(io_roots)],
        "network_policy": {"default_deny": True, "allowed_endpoints": []},
        "evidence": {
            "source_bundle_digest": receipts.source_bundle_digest(member_bytes),
            "canonicalization_vector_digest": receipts.CANONICALIZATION_VECTOR_DIGEST,
        },
        "provenance": {
            "producer": "scripts/recovery_launcher.py",
            "provider": "local",
            "method": "static-execution-closure",
        },
        "confidence": 1,
        "validation_status": "validated",
    }


def _canonical_object(raw: bytes, *, label: str) -> receipts.JSONObject:
    return receipts._object(  # noqa: SLF001
        receipts.strict_canonical_json(raw, label=label), label=label
    )


def _decode_canonical_base64(value: object, *, label: str) -> bytes:
    encoded = receipts._validate_string(value, label=label)  # noqa: SLF001
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise receipts.ReleaseControlError(f"{label} is invalid base64") from exc
    if base64.b64encode(raw).decode("ascii") != encoded:
        raise receipts.ReleaseControlError(f"{label} is not canonical base64")
    return raw


def _validate_prepare_capsule_assets(
    *,
    promotion_run_id: int,
    assets: Mapping[str, bytes],
    candidate_manifest_digest: str,
    transaction_authorization: bytes,
    owner_signing_keys_observation: bytes,
    source_registry: Mapping[str, object],
    _clock: Callable[[], datetime],
) -> dict[str, receipts.JSONValue]:
    transaction_value = transaction.validate_server_authorization(
        _canonical_object(
            transaction_authorization,
            label="prepare transaction authorization",
        ),
        expected_original_transaction_digest=None,
    )
    transaction_run = receipts._object(  # noqa: SLF001
        transaction_value.get("promotion_run"),
        label="prepare transaction promotion run",
    )
    transaction_candidate = receipts._object(  # noqa: SLF001
        transaction_value.get("candidate"),
        label="prepare transaction candidate",
    )
    authorized_at = receipts.parse_timestamp(
        transaction_value.get("authorized_at"),
        label="prepare transaction authorized_at",
    )
    now = _clock()
    if now.tzinfo is None or now.utcoffset() != timedelta(0):
        raise receipts.ReleaseControlError("prepare capsule authority clock must be aware UTC")
    if (
        transaction_value.get("authorization_kind") != "transaction"
        or transaction_value.get("mode") != "initiate"
        or transaction_run.get("run_id") != promotion_run_id
        or transaction_candidate.get("candidate_manifest_digest")
        != receipts._digest(  # noqa: SLF001
            candidate_manifest_digest,
            label="prepare capsule candidate manifest digest",
        )
        or now.astimezone(UTC).replace(microsecond=0) < authorized_at
    ):
        raise receipts.ReleaseControlError("prepare transaction authorization identity mismatch")
    approval_raw = assets["approval-history-observation.json"]
    approval_body = receipts.source_observation_body_for_contract(
        approval_raw,
        registry=source_registry,
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="approval-history-observation",
        _clock=lambda: authorized_at,
    )
    approval = _canonical_object(approval_body, label="prepare capsule authority approval history")
    transaction._require_cumulative_owner_approvals(  # noqa: SLF001
        approval, expected_environments=("release",)
    )
    if transaction_value.get("approval_history") != approval:
        raise receipts.ReleaseControlError(
            "prepare approval history differs from transaction authorization"
        )

    publication_raw = assets["recovery-capsule-publication.json"]
    publication = _canonical_object(publication_raw, label="prepare capsule publication receipt")
    receipts._require_exact_fields(  # noqa: SLF001
        publication,
        frozenset(
            {
                "schema",
                "repository",
                "repository_id",
                "tag",
                "release_id",
                "manifest_digest",
                "archive_digest",
                "immutable",
                "validation_status",
            }
        ),
        label="prepare capsule publication receipt",
    )
    if (
        publication.get("schema") != "kestrel.recovery_capsule_publication.v1"
        or publication.get("repository") != RECOVERY_REPOSITORY
        or publication.get("immutable") is not True
        or publication.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("prepare capsule publication receipt is invalid")
    repository_id = receipts._safe_integer(  # noqa: SLF001
        publication.get("repository_id"),
        label="prepare capsule publication repository ID",
        positive=True,
    )
    release_id = receipts._safe_integer(  # noqa: SLF001
        publication.get("release_id"),
        label="prepare capsule publication Release ID",
        positive=True,
    )
    manifest_digest = receipts._digest(  # noqa: SLF001
        publication.get("manifest_digest"),
        label="prepare capsule publication manifest digest",
    )
    archive_digest = receipts._digest(  # noqa: SLF001
        publication.get("archive_digest"),
        label="prepare capsule publication archive digest",
    )
    recovery_tag = f"recovery-{promotion_run_id}-1"
    if publication.get("tag") != recovery_tag:
        raise receipts.ReleaseControlError(
            "prepare capsule publication promotion run binding mismatch"
        )

    signed_raw = assets["recovery-capsule-verification.json"]
    signed = _canonical_object(signed_raw, label="prepare signed recovery capsule verification")
    receipts._require_exact_fields(  # noqa: SLF001
        signed,
        frozenset(
            {
                "schema",
                "verification",
                "receipt_digest",
                "signature_digest",
                "receipt_base64",
                "signature_base64",
                "validation_status",
            }
        ),
        label="prepare signed recovery capsule verification",
    )
    if (
        signed.get("schema") != "kestrel.recovery_capsule_verification.v1"
        or signed.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError(
            "prepare signed recovery capsule verification is invalid"
        )
    claim = transaction._validate_recovery_capsule_verification_claim(  # noqa: SLF001
        receipts._object(  # noqa: SLF001
            signed.get("verification"),
            label="prepare recovery capsule verification claim",
        )
    )
    receipt = _decode_canonical_base64(
        signed.get("receipt_base64"),
        label="prepare recovery capsule verification receipt",
    )
    signature = _decode_canonical_base64(
        signed.get("signature_base64"),
        label="prepare recovery capsule verification signature",
    )
    if (
        _canonical_object(receipt, label="prepare recovery capsule verification receipt") != claim
        or signed.get("receipt_digest") != _sha256(receipt)
        or signed.get("signature_digest") != _sha256(signature)
    ):
        raise receipts.ReleaseControlError(
            "prepare recovery capsule verification byte binding mismatch"
        )
    verified_at = receipts.parse_timestamp(
        claim.get("verified_at"),
        label="prepare recovery capsule verification time",
    )
    if verified_at > now.astimezone(UTC).replace(microsecond=0):
        raise receipts.ReleaseControlError(
            "prepare recovery capsule verification is from the future"
        )
    receipts.source_observation_body_for_contract(
        owner_signing_keys_observation,
        registry=source_registry,
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        _clock=lambda: verified_at,
    )
    _public_key, observed_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        owner_signing_keys_observation,
        expected_fingerprint=cast(str, claim.get("signing_key_fingerprint")),
    )
    receipts.verify_detached_signature(
        receipt=receipt,
        signature=signature,
        expected_fingerprint=observed_fingerprint,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    if (
        claim.get("signing_principal") != receipts.SIGNING_PRINCIPAL
        or claim.get("signing_key_fingerprint")
        != receipts.signature_public_key_fingerprint(signature)
        or claim.get("owner_signing_keys_observation_digest")
        != _sha256(owner_signing_keys_observation)
        or claim.get("candidate_manifest_digest")
        != receipts._digest(  # noqa: SLF001
            candidate_manifest_digest,
            label="prepare capsule candidate manifest digest",
        )
        or claim.get("transaction_authorization_digest") != _sha256(transaction_authorization)
    ):
        raise receipts.ReleaseControlError(
            "prepare recovery capsule owner or transaction binding mismatch"
        )
    claim_repository = receipts._object(  # noqa: SLF001
        claim.get("repository"), label="prepare recovery capsule repository"
    )
    claim_release = receipts._object(  # noqa: SLF001
        claim.get("release"), label="prepare recovery capsule Release"
    )
    claim_assets = {
        receipts._validate_string(  # noqa: SLF001
            item.get("name"), label="prepare recovery capsule asset name"
        ): item
        for item in (
            receipts._object(raw, label="prepare recovery capsule asset")  # noqa: SLF001
            for raw in receipts._array(  # noqa: SLF001
                claim.get("assets"), label="prepare recovery capsule assets"
            )
        )
    }
    if (
        claim_repository != {"full_name": RECOVERY_REPOSITORY, "id": repository_id, "private": True}
        or claim_release != {"id": release_id, "tag": recovery_tag, "immutable": True}
        or claim.get("capsule_manifest_digest") != manifest_digest
        or claim_assets["recovery-capsule-manifest.json"].get("sha256") != manifest_digest
        or claim_assets["recovery-capsule.tar"].get("sha256") != archive_digest
    ):
        raise receipts.ReleaseControlError("prepare recovery capsule publication binding mismatch")
    return {
        "approval_history": _sha256(approval_raw),
        "capsule_publication": _sha256(publication_raw),
        "capsule_verification": _sha256(signed_raw),
    }


def _prepare_authority_asset_bytes(asset_root: Path) -> dict[str, bytes]:
    if not asset_root.is_dir() or asset_root.is_symlink():
        raise receipts.ReleaseControlError("prepare capsule authority asset root is invalid")
    entries = tuple(asset_root.iterdir())
    if (
        {entry.name for entry in entries} != PREPARE_AUTHORITY_ASSETS
        or len(entries) != len(PREPARE_AUTHORITY_ASSETS)
        or any(not entry.is_file() or entry.is_symlink() for entry in entries)
    ):
        raise receipts.ReleaseControlError("prepare capsule authority asset inventory is not exact")
    assets: dict[str, bytes] = {}
    for name in sorted(PREPARE_AUTHORITY_ASSETS):
        raw = receipts._read_regular(  # noqa: SLF001
            asset_root / name,
            label=f"prepare capsule authority asset {name}",
            max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
        )
        receipts.strict_canonical_json(raw, label=f"prepare capsule authority asset {name}")
        assets[name] = raw
    return assets


def _require_recovery_repository(value: object) -> int:
    repository = receipts._object(value, label="recovery repository")  # noqa: SLF001
    owner = receipts._object(  # noqa: SLF001
        repository.get("owner"), label="recovery repository owner"
    )
    repository_id = receipts._safe_integer(  # noqa: SLF001
        repository.get("id"), label="recovery repository ID", positive=True
    )
    if (
        repository.get("full_name") != RECOVERY_REPOSITORY
        or repository.get("private") is not True
        or repository.get("visibility") != "private"
        or repository.get("archived") is not False
        or repository.get("disabled") is not False
        or owner.get("login") != receipts.SIGNING_PRINCIPAL
        or owner.get("id") != 58918509
        or owner.get("type") != "User"
    ):
        raise receipts.ReleaseControlError("recovery repository identity conflicts")
    return repository_id


def publish_prepare_capsule_authority(
    *,
    promotion_run_id: int,
    asset_root: Path,
    journal_path: Path,
    candidate_manifest_digest: str,
    transaction_authorization: bytes,
    owner_signing_keys_observation: bytes,
    api: transaction.TerminalReleaseAPI,
    recovery_reader_api: transaction.GitHubReadAPI,
    mutation_guard: Callable[[], None] | None = None,
    source_root: Path = ROOT,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> receipts.JSONObject:
    """Crash-resume the exact three-asset capsule handoff to release-prepare."""

    run_id = receipts._safe_integer(  # noqa: SLF001
        promotion_run_id,
        label="prepare capsule authority promotion run ID",
        positive=True,
    )
    assets = _prepare_authority_asset_bytes(asset_root)
    validation_digests = _validate_prepare_capsule_assets(
        promotion_run_id=run_id,
        assets=assets,
        candidate_manifest_digest=candidate_manifest_digest,
        transaction_authorization=transaction_authorization,
        owner_signing_keys_observation=owner_signing_keys_observation,
        source_registry=_source_registry(source_root),
        _clock=_clock,
    )
    repository_exchange = recovery_reader_api(
        f"GET /repos/{RECOVERY_REPOSITORY}", accept="application/vnd.github+json"
    )
    if repository_exchange.http_status != 200:
        raise receipts.ReleaseControlError(
            "prepare capsule authority recovery repository preflight failed"
        )
    repository_id = _require_recovery_repository(
        receipts.parse_external_json_bytes(
            repository_exchange.response_body,
            label="prepare capsule authority recovery repository",
        )
    )

    tag_name = f"release-prepare-authority-{run_id}-1"
    release_name = tag_name
    release_body = f"Kestrel recovery capsule authority for promotion run {run_id}"
    reader_preflight = recovery_reader_api(
        f"GET /repos/{RECOVERY_REPOSITORY}/releases/tags/{quote(tag_name, safe='')}",
        accept="application/vnd.github+json",
    )
    if reader_preflight.http_status not in {200, 404}:
        raise receipts.ReleaseControlError("prepare capsule authority Release preflight failed")
    if reader_preflight.http_status == 200:
        preexisting = receipts._object(  # noqa: SLF001
            receipts.parse_external_json_bytes(
                reader_preflight.response_body,
                label="preexisting prepare capsule authority Release",
            ),
            label="preexisting prepare capsule authority Release",
        )
        if preexisting.get("tag_name") != tag_name:
            raise receipts.ReleaseControlError(
                "preexisting prepare capsule authority Release tag conflicts"
            )

    expected_assets = {name: (raw, "application/json") for name, raw in sorted(assets.items())}
    journal: receipts.JSONObject = {
        "schema": "kestrel.release_prepare_capsule_authority_journal.v1",
        "repository": RECOVERY_REPOSITORY,
        "repository_id": repository_id,
        "promotion_run_id": run_id,
        "candidate_manifest_digest": receipts._digest(  # noqa: SLF001
            candidate_manifest_digest,
            label="prepare capsule authority candidate manifest digest",
        ),
        "tag_name": tag_name,
        "name": release_name,
        "body": release_body,
        "assets": [
            {
                "name": name,
                "sha256": _sha256(raw),
                "size_bytes": len(raw),
                "media_type": "application/json",
            }
            for name, raw in sorted(assets.items())
        ],
        "validation_digests": validation_digests,
        "validation_status": "validated",
    }
    return _finish_prepare_authority_publication(
        journal=journal,
        journal_path=journal_path,
        api=api,
        expected_assets=expected_assets,
        recovery_reader_api=recovery_reader_api,
        tag_name=tag_name,
        release_name=release_name,
        release_body=release_body,
        repository_id=repository_id,
        run_id=run_id,
        candidate_manifest_digest=candidate_manifest_digest,
        validation_digests=validation_digests,
        mutation_guard=mutation_guard,
    )


@dataclass(frozen=True)
class RecoveryControllerRequest:
    """Complete owner-approved input contract for one production capsule."""

    source_root: Path
    source_sha: str
    candidate_manifest_digest: str
    promotion_run_id: int
    authorization_artifact_id: int
    authorization_artifact_digest: str
    staging_run_id: int
    staging_artifact_id: int
    staging_artifact_digest: str
    target_workspace_root: Path
    recovery_repository_id: int
    identity_file: Path
    current_recovery_owner_authority_snapshot: Path
    current_recovery_repository_observation: Path
    current_recovery_immutable_releases_observation: Path
    current_recovery_controller_context: Path
    work_root: Path
    output: Path


@dataclass(frozen=True)
class ValidatedControllerScalars:
    """Canonical scalar identities validated before filesystem or transport use."""

    source_sha: str
    candidate_manifest_digest: str
    promotion_run_id: int
    authorization_artifact_id: int
    authorization_artifact_digest: str
    staging_run_id: int
    staging_artifact_id: int
    staging_artifact_digest: str
    recovery_repository_id: int


CapsulePublisher = Callable[[Path, str, int, Path, Callable[[], None]], None]
VerificationSigner = Callable[..., receipts.JSONObject]


def _validate_controller_request_scalars(
    request: RecoveryControllerRequest,
) -> ValidatedControllerScalars:
    return ValidatedControllerScalars(
        source_sha=receipts._git_sha(  # noqa: SLF001
            request.source_sha, label="recovery controller source SHA"
        ),
        candidate_manifest_digest=receipts._digest(  # noqa: SLF001
            request.candidate_manifest_digest,
            label="recovery controller candidate manifest digest",
        ),
        promotion_run_id=receipts._safe_integer(  # noqa: SLF001
            request.promotion_run_id,
            label="recovery controller promotion run ID",
            positive=True,
        ),
        authorization_artifact_id=receipts._safe_integer(  # noqa: SLF001
            request.authorization_artifact_id,
            label="recovery controller authorization artifact ID",
            positive=True,
        ),
        authorization_artifact_digest=receipts._digest(  # noqa: SLF001
            request.authorization_artifact_digest,
            label="recovery controller authorization artifact digest",
        ),
        staging_run_id=receipts._safe_integer(  # noqa: SLF001
            request.staging_run_id,
            label="recovery controller staging run ID",
            positive=True,
        ),
        staging_artifact_id=receipts._safe_integer(  # noqa: SLF001
            request.staging_artifact_id,
            label="recovery controller staging artifact ID",
            positive=True,
        ),
        staging_artifact_digest=receipts._digest(  # noqa: SLF001
            request.staging_artifact_digest,
            label="recovery controller staging artifact digest",
        ),
        recovery_repository_id=receipts._safe_integer(  # noqa: SLF001
            request.recovery_repository_id,
            label="recovery controller recovery repository ID",
            positive=True,
        ),
    )


def _controller_request_journal(
    request: RecoveryControllerRequest,
    scalars: ValidatedControllerScalars,
) -> receipts.JSONObject:
    bootstrap_raw = os.environ.get("KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT")
    if bootstrap_raw is None or not bootstrap_raw:
        raise receipts.ReleaseControlError("recovery controller bootstrap receipt is unavailable")
    bootstrap_receipt = Path(bootstrap_raw)
    if (
        not bootstrap_receipt.is_absolute()
        or not bootstrap_receipt.is_file()
        or bootstrap_receipt.is_symlink()
        or bootstrap_receipt.resolve(strict=True) != bootstrap_receipt
    ):
        raise receipts.ReleaseControlError("recovery controller bootstrap receipt path is invalid")
    authority_inputs = {
        "owner_authority_snapshot": request.current_recovery_owner_authority_snapshot,
        "repository_observation": request.current_recovery_repository_observation,
        "immutable_releases_observation": (request.current_recovery_immutable_releases_observation),
        "controller_context": request.current_recovery_controller_context,
    }
    return {
        "schema": "kestrel.recovery_capsule_controller_request.v2",
        "source": {
            "root": str(request.source_root),
            "sha": scalars.source_sha,
        },
        "candidate_manifest_digest": scalars.candidate_manifest_digest,
        "promotion_run_id": scalars.promotion_run_id,
        "authorization_artifact": {
            "id": scalars.authorization_artifact_id,
            "api_digest": scalars.authorization_artifact_digest,
        },
        "staging_artifact": {
            "run_id": scalars.staging_run_id,
            "id": scalars.staging_artifact_id,
            "api_digest": scalars.staging_artifact_digest,
        },
        "recovery_repository_id": scalars.recovery_repository_id,
        "paths": {
            "target_workspace_root": str(request.target_workspace_root),
            "work_root": str(request.work_root),
            "output": str(request.output),
        },
        "signing_identity": {
            "path": str(request.identity_file),
            "sha256": _path_sha256(request.identity_file),
        },
        "authority_inputs": {
            name: {
                "path": str(path),
                "renewable_generation_slot": True,
            }
            for name, path in sorted(authority_inputs.items())
        },
        "bootstrap": {
            "receipt_path": str(bootstrap_receipt),
            "receipt_sha256": _path_sha256(bootstrap_receipt),
        },
        "validation_status": "validated",
    }


def _open_controller_workspace(
    request: RecoveryControllerRequest,
    scalars: ValidatedControllerScalars,
) -> bool:
    """Atomically create the request journal, or validate one exact resumption."""

    expected = receipts.canonical_json_bytes(_controller_request_journal(request, scalars))
    work_root = request.work_root
    journal_name = "controller-request.json"
    if not work_root.exists() and not work_root.is_symlink():
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{work_root.name}.request-",
                dir=work_root.parent,
            )
        )
        staging.chmod(0o700)
        installed = False
        try:
            _write_exclusive(staging / journal_name, expected)
            try:
                receipts._rename_noreplace(staging, work_root)  # noqa: SLF001
            except FileExistsError:
                pass
            else:
                receipts._fsync_directory(work_root.parent)  # noqa: SLF001
                installed = True
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        if installed:
            if not work_root.is_dir() or work_root.is_symlink():
                raise receipts.ReleaseControlError("recovery controller work root creation raced")
            return False
    if (
        not work_root.is_absolute()
        or not work_root.is_dir()
        or work_root.is_symlink()
        or work_root.resolve(strict=True) != work_root
    ):
        raise receipts.ReleaseControlError("recovery controller resume work root is invalid")
    journal_path = work_root / journal_name
    try:
        observed = receipts._read_regular(  # noqa: SLF001
            journal_path,
            label="recovery controller request journal",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
    except (OSError, ValueError) as exc:
        raise receipts.ReleaseControlError(
            "recovery controller request journal is missing or invalid"
        ) from exc
    if observed != expected:
        raise receipts.ReleaseControlError("recovery controller request journal conflicts")
    return True


def _load_completed_controller_result(
    request: RecoveryControllerRequest,
    scalars: ValidatedControllerScalars,
) -> receipts.JSONObject | None:
    if not request.output.exists() and not request.output.is_symlink():
        return None
    raw = receipts._read_regular(  # noqa: SLF001
        request.output,
        label="completed recovery controller output",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    result = _canonical_object(raw, label="completed recovery controller output")
    receipts._require_exact_fields(  # noqa: SLF001
        result,
        frozenset(
            {
                "schema",
                "source_sha",
                "candidate_manifest_digest",
                "promotion_run_id",
                "target_workspace_root",
                "authorization_artifact",
                "dependency_artifact",
                "recovery_repository",
                "capsule",
                "prepare_authority",
                "evidence",
                "provenance",
                "confidence",
                "validation_status",
            }
        ),
        label="completed recovery controller output",
    )
    authorization_artifact = receipts._object(  # noqa: SLF001
        result.get("authorization_artifact"),
        label="completed controller authorization artifact",
    )
    dependency_artifact = receipts._object(  # noqa: SLF001
        result.get("dependency_artifact"),
        label="completed controller dependency artifact",
    )
    repository = receipts._object(  # noqa: SLF001
        result.get("recovery_repository"),
        label="completed controller recovery repository",
    )
    capsule = receipts._object(  # noqa: SLF001
        result.get("capsule"), label="completed controller capsule"
    )
    prepare = receipts._object(  # noqa: SLF001
        result.get("prepare_authority"),
        label="completed controller prepare authority",
    )
    evidence = receipts._object(  # noqa: SLF001
        result.get("evidence"), label="completed controller evidence"
    )
    provenance = receipts._object(  # noqa: SLF001
        result.get("provenance"), label="completed controller provenance"
    )
    digest_values = (
        capsule.get("manifest_digest"),
        capsule.get("publication_receipt_digest"),
        capsule.get("verification_digest"),
        prepare.get("publication_digest"),
        evidence.get("source_bundle_digest"),
    )
    for index, value in enumerate(digest_values):
        receipts._digest(value, label=f"completed controller digest {index}")  # noqa: SLF001
    if (
        result.get("schema") != "kestrel.recovery_capsule_controller.v1"
        or result.get("source_sha") != scalars.source_sha
        or result.get("candidate_manifest_digest") != scalars.candidate_manifest_digest
        or result.get("promotion_run_id") != scalars.promotion_run_id
        or result.get("target_workspace_root") != str(request.target_workspace_root)
        or authorization_artifact.get("artifact_id") != scalars.authorization_artifact_id
        or authorization_artifact.get("api_digest") != scalars.authorization_artifact_digest
        or dependency_artifact.get("run_id") != scalars.staging_run_id
        or dependency_artifact.get("artifact_id") != scalars.staging_artifact_id
        or dependency_artifact.get("api_digest") != scalars.staging_artifact_digest
        or repository
        != {
            "full_name": RECOVERY_REPOSITORY,
            "id": scalars.recovery_repository_id,
        }
        or capsule.get("tag") != f"recovery-{scalars.promotion_run_id}-1"
        or prepare.get("tag") != f"release-prepare-authority-{scalars.promotion_run_id}-1"
        or not isinstance(prepare.get("release_id"), int)
        or evidence.get("canonicalization_vector_digest")
        != receipts.canonicalization_vector_digest()
        or provenance
        != {
            "producer": "scripts/recovery_capsule_controller.py",
            "provider": "owner-controller",
            "method": "exact-artifact-authority-bound-recovery-publication",
        }
        or result.get("confidence") != 1
        or result.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("completed recovery controller output conflicts")
    if request.work_root.is_dir() and not request.work_root.is_symlink():
        local_bindings = {
            request.work_root / "capsule-source" / "recovery-capsule-manifest.json": capsule[
                "manifest_digest"
            ],
            request.work_root / "recovery-capsule-publication.json": capsule[
                "publication_receipt_digest"
            ],
            request.work_root / "recovery-capsule-verification.json": capsule[
                "verification_digest"
            ],
            request.work_root / "prepare-capsule-authority-publication.json": prepare[
                "publication_digest"
            ],
        }
        if any(
            not path.is_file() or path.is_symlink() or _path_sha256(path) != expected_digest
            for path, expected_digest in local_bindings.items()
        ):
            raise receipts.ReleaseControlError(
                "completed recovery controller local evidence conflicts"
            )
    return result


def _recover_interrupted_local_stage(
    request: RecoveryControllerRequest,
    *,
    resuming: bool,
) -> None:
    """Remove only request-bound unpublished scratch left by a hard interruption."""

    if not resuming:
        _require_target_workspace_empty(request)
        return
    publication_path = request.work_root / "recovery-capsule-publication.json"
    publication_exists = publication_path.exists() or publication_path.is_symlink()
    preparation_root = request.work_root / "production-preparation"
    if preparation_root.exists() or preparation_root.is_symlink():
        expected_preparation = {
            "candidate-archive.tar",
            "controller-preparation.json",
            "recovery-environment-manifest.json",
        }
        if (
            preparation_root.is_symlink()
            or not preparation_root.is_dir()
            or {entry.name for entry in preparation_root.iterdir()} != expected_preparation
            or any(
                not (preparation_root / name).is_file() or (preparation_root / name).is_symlink()
                for name in expected_preparation
            )
        ):
            raise receipts.ReleaseControlError(
                "recovery controller preparation resume inventory conflicts"
            )
        _canonical_object(
            receipts._read_regular(  # noqa: SLF001
                preparation_root / "controller-preparation.json",
                label="recovery controller preparation resume receipt",
                max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
            ),
            label="recovery controller preparation resume receipt",
        )
    scratch_prefixes = (
        ".production-preparation-",
        ".capsule-authority-binding-",
        ".capsule-build-",
        ".normalized-evidence-",
        ".prepare-capsule-authority-",
    )
    for path in tuple(request.work_root.iterdir()):
        if not path.name.startswith(scratch_prefixes):
            continue
        if path.is_symlink() or not path.is_dir():
            raise receipts.ReleaseControlError(
                "recovery controller interrupted preparation staging is invalid"
            )
        shutil.rmtree(path)
    write_once_scratch_prefixes = tuple(
        f".{name}.tmp-"
        for name in (
            "capsule-stage.json",
            "prepare-capsule-authority-journal.json",
            "prepare-capsule-authority-publication.json",
            "recovery-authority.json",
            "recovery-authority.json.sig",
            "recovery-capsule-publication.json",
            "recovery-capsule-verification.json",
            "recovery-execution-closure.json",
        )
    )
    for path in tuple(request.work_root.iterdir()):
        if not path.name.startswith(write_once_scratch_prefixes):
            continue
        if path.is_symlink() or not path.is_file():
            raise receipts.ReleaseControlError(
                "recovery controller interrupted write-once scratch is invalid"
            )
        path.unlink()
    stage_paths = (
        request.work_root / "recovery-execution-closure.json",
        request.work_root / "recovery-authority.json",
        request.work_root / "recovery-authority.json.sig",
        request.work_root / "capsule-source",
        request.work_root / "capsule-stage.json",
    )
    present = tuple(path for path in stage_paths if path.exists() or path.is_symlink())
    complete_stage = len(present) == len(stage_paths)
    if publication_exists and not complete_stage:
        raise receipts.ReleaseControlError("published recovery capsule has incomplete local stage")
    if not complete_stage:
        for name in ("capsule-authority-binding", "normalized-evidence"):
            path = request.work_root / name
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink() or not path.is_dir():
                raise receipts.ReleaseControlError(
                    "recovery controller unpublished authority scratch is invalid"
                )
            shutil.rmtree(path)
    target_entries = tuple(request.target_workspace_root.iterdir())
    if target_entries:
        if publication_exists or complete_stage:
            raise receipts.ReleaseControlError(
                "published or complete recovery capsule has interrupted target state"
            )
        transaction_root = request.target_workspace_root / "transaction"
        if (
            len(target_entries) != 1
            or target_entries[0] != transaction_root
            or not transaction_root.is_dir()
            or transaction_root.is_symlink()
        ):
            raise receipts.ReleaseControlError(
                "recovery controller resume target inventory conflicts"
            )
        transaction_entries = tuple(transaction_root.iterdir())
        runtime_root = transaction_root / "recovery-runtime"
        if transaction_entries and (
            len(transaction_entries) != 1
            or transaction_entries[0] != runtime_root
            or not runtime_root.is_dir()
            or runtime_root.is_symlink()
        ):
            raise receipts.ReleaseControlError(
                "recovery controller interrupted runtime inventory conflicts"
            )
        shutil.rmtree(transaction_root)
    if present and not complete_stage:
        for path in present:
            if path.is_symlink():
                raise receipts.ReleaseControlError(
                    "recovery controller interrupted local stage contains a symlink"
                )
            if path.is_dir():
                if path.name != "capsule-source":
                    raise receipts.ReleaseControlError(
                        "recovery controller interrupted local stage is invalid"
                    )
                shutil.rmtree(path)
            elif path.is_file():
                path.unlink()
            else:
                raise receipts.ReleaseControlError(
                    "recovery controller interrupted local stage is invalid"
                )
    _require_target_workspace_empty(request)


def _git_output(source_root: Path, *arguments: str) -> bytes:
    raw_git = shutil.which("git")
    if raw_git is None:
        raise receipts.ReleaseControlError("recovery controller Git executable is unavailable")
    git = Path(raw_git).resolve(strict=True)
    if not git.is_file() or git.is_symlink() or not os.access(git, os.X_OK):
        raise receipts.ReleaseControlError("recovery controller Git executable is invalid")
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [str(git), "-C", str(source_root), *arguments],
        check=False,
        capture_output=True,
        timeout=30,
        env={"LANG": "C", "LC_ALL": "C"},
    )
    if completed.returncode != 0 or len(completed.stdout) > 4 * 1024 * 1024:
        raise receipts.ReleaseControlError("recovery controller source Git inspection failed")
    return completed.stdout


def _require_controller_platform(*, os_release: str | None = None) -> None:
    if (
        sys.platform != "linux"
        or platform.machine() != "x86_64"
        or platform.python_implementation() != "CPython"
        or platform.python_version() != "3.11.14"
    ):
        raise receipts.ReleaseControlError(
            "production recovery controller requires Ubuntu 24.04 x86_64 and CPython 3.11.14"
        )
    if os_release is None:
        raw = Path("/etc/os-release").read_bytes()
        if len(raw) > 64 * 1024:
            raise receipts.ReleaseControlError(
                "recovery controller OS release identity is too large"
            )
        os_release = raw.decode("utf-8", "strict")
    fields: dict[str, str] = {}
    for line in os_release.splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        fields[name] = value
    if fields.get("ID") != "ubuntu" or fields.get("VERSION_ID") != "24.04":
        raise receipts.ReleaseControlError(
            "production recovery controller requires Ubuntu 24.04 x86_64 and CPython 3.11.14"
        )


def _require_executing_source(source_root: Path) -> None:
    """Bind the running controller and every local import to one checkout."""

    expected = {
        "recovery_capsule_controller": (
            __file__,
            source_root / "scripts" / "recovery_capsule_controller.py",
        ),
        "bootstrap_recovery": (
            bootstrap_recovery.__file__,
            source_root / "scripts" / "bootstrap_recovery.py",
        ),
        "bootstrap_recovery_capsule_controller": (
            controller_bootstrap.__file__,
            source_root / "scripts" / "bootstrap_recovery_capsule_controller.py",
        ),
        "recovery_launcher": (
            recovery_launcher.__file__,
            source_root / "scripts" / "recovery_launcher.py",
        ),
        "release_candidate_manifest": (
            candidates.__file__,
            source_root / "scripts" / "release_candidate_manifest.py",
        ),
        "release_control_receipt": (
            receipts.__file__,
            source_root / "scripts" / "release_control_receipt.py",
        ),
        "release_promotion_transaction": (
            transaction.__file__,
            source_root / "scripts" / "release_promotion_transaction.py",
        ),
    }
    if ROOT != source_root:
        raise receipts.ReleaseControlError(
            "recovery controller executing source root differs from --source-root"
        )
    for name, (observed_raw, expected_path) in expected.items():
        if observed_raw is None:
            raise receipts.ReleaseControlError(
                f"recovery controller executing source for {name} is unavailable"
            )
        try:
            observed = Path(observed_raw).resolve(strict=True)
            required = expected_path.resolve(strict=True)
        except OSError as exc:
            raise receipts.ReleaseControlError(
                f"recovery controller executing source for {name} is invalid"
            ) from exc
        if observed != required or observed.is_symlink() or not observed.is_file():
            raise receipts.ReleaseControlError(
                f"recovery controller executing source for {name} differs from --source-root"
            )


def _require_bootstrap_handoff(source_root: Path) -> None:
    raw_receipt = os.environ.get("KESTREL_RECOVERY_CONTROLLER_BOOTSTRAP_RECEIPT")
    if raw_receipt is None or not raw_receipt:
        raise receipts.ReleaseControlError("recovery controller bootstrap receipt is unavailable")
    receipt_path = Path(raw_receipt)
    if (
        not receipt_path.is_absolute()
        or receipt_path.is_symlink()
        or not receipt_path.is_file()
        or receipt_path.resolve(strict=True) != receipt_path
    ):
        raise receipts.ReleaseControlError("recovery controller bootstrap receipt path is invalid")
    raw_arguments = tuple(sys.argv[1:])
    if raw_arguments.count("--prepare-only") > 1:
        raise receipts.ReleaseControlError("recovery controller prepare-only mode is duplicated")
    stable_arguments = tuple(argument for argument in raw_arguments if argument != "--prepare-only")
    controller_bootstrap.authorize_inner_gate(
        receipt_path=receipt_path,
        source_root=source_root,
        controller_arguments=stable_arguments,
        executing_script=Path(controller_bootstrap.__file__).resolve(strict=True),
        executing_python=Path(sys.executable).resolve(strict=True),
    )


def _require_clean_source_identity(request: RecoveryControllerRequest) -> None:
    source_root = request.source_root
    expected_sha = receipts._git_sha(  # noqa: SLF001
        request.source_sha, label="recovery controller source SHA"
    )
    top_level = _git_output(source_root, "rev-parse", "--show-toplevel").strip()
    head = _git_output(source_root, "rev-parse", "HEAD^{commit}").strip()
    status = _git_output(
        source_root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if (
        top_level != os.fsencode(source_root)
        or head.decode("ascii", "strict") != expected_sha
        or status
    ):
        raise receipts.ReleaseControlError(
            "recovery controller source is not the exact clean commit"
        )


def _require_target_workspace_identity(request: RecoveryControllerRequest) -> None:
    target = request.target_workspace_root
    if (
        not target.is_absolute()
        or not target.is_dir()
        or target.is_symlink()
        or target.resolve(strict=True) != target
        or target == request.source_root
    ):
        raise receipts.ReleaseControlError(
            "recovery controller target workspace must be a separate real directory"
        )


def _require_target_workspace_empty(request: RecoveryControllerRequest) -> None:
    _require_target_workspace_identity(request)
    if any(request.target_workspace_root.iterdir()):
        raise receipts.ReleaseControlError(
            "recovery controller target workspace must be a separate empty real directory"
        )


def _require_controller_paths(request: RecoveryControllerRequest) -> None:
    _require_executing_source(request.source_root)
    _require_bootstrap_handoff(request.source_root)
    _require_controller_platform()
    source_root = request.source_root
    if (
        not source_root.is_absolute()
        or not source_root.is_dir()
        or source_root.is_symlink()
        or source_root.resolve(strict=True) != source_root
    ):
        raise receipts.ReleaseControlError(
            "recovery controller source root is not one real absolute directory"
        )
    _require_clean_source_identity(request)
    _require_target_workspace_identity(request)
    target = request.target_workspace_root
    work_exists = request.work_root.exists() or request.work_root.is_symlink()
    output_exists = request.output.exists() or request.output.is_symlink()
    if (
        not request.work_root.is_absolute()
        or not request.output.is_absolute()
        or not request.work_root.parent.is_dir()
        or request.work_root.parent.is_symlink()
        or request.work_root.parent.resolve(strict=True) != request.work_root.parent
        or request.work_root.is_relative_to(source_root)
        or request.work_root.is_relative_to(target)
        or not request.output.parent.is_dir()
        or request.output.parent.is_symlink()
        or request.output.parent.resolve(strict=True) != request.output.parent
        or request.output == request.work_root
        or request.output.is_relative_to(source_root)
        or request.output.is_relative_to(target)
    ):
        raise receipts.ReleaseControlError("recovery controller output parent or path is invalid")
    if work_exists and (
        not request.work_root.is_dir()
        or request.work_root.is_symlink()
        or request.work_root.resolve(strict=True) != request.work_root
    ):
        raise receipts.ReleaseControlError("recovery controller resume work root is invalid")
    if output_exists and (
        not request.output.is_file()
        or request.output.is_symlink()
        or request.output.resolve(strict=True) != request.output
    ):
        raise receipts.ReleaseControlError("recovery controller resume output is invalid")
    identity = request.identity_file
    if (
        not identity.is_absolute()
        or not identity.is_file()
        or identity.is_symlink()
        or identity.resolve(strict=True) != identity
        or identity.stat().st_mode & 0o077
    ):
        raise receipts.ReleaseControlError(
            "recovery controller signing identity is not a private regular file"
        )
    authority_inputs = (
        request.current_recovery_owner_authority_snapshot,
        request.current_recovery_repository_observation,
        request.current_recovery_immutable_releases_observation,
        request.current_recovery_controller_context,
    )
    if len(set(authority_inputs)) != len(authority_inputs) or any(
        not path.is_absolute()
        or not path.is_file()
        or path.is_symlink()
        or path.resolve(strict=True) != path
        or path.is_relative_to(source_root)
        or path.is_relative_to(target)
        or path.is_relative_to(request.work_root)
        for path in authority_inputs
    ):
        raise receipts.ReleaseControlError(
            "current recovery authority inputs must be distinct real regular files"
        )


def _copy_evidence_tree(source: Path, destination: Path) -> None:
    if not source.is_dir() or source.is_symlink():
        raise receipts.ReleaseControlError("recovery controller evidence source is invalid")
    if destination.exists() or destination.is_symlink():
        if not destination.is_dir() or destination.is_symlink():
            raise receipts.ReleaseControlError(
                "recovery controller evidence destination is invalid"
            )
    else:
        destination.mkdir(mode=0o700, parents=False)
    for path in sorted(source.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise receipts.ReleaseControlError(
                "recovery controller evidence source contains a symlink"
            )
        relative = path.relative_to(source)
        target = destination / relative
        if path.is_dir():
            target.mkdir(mode=0o700, exist_ok=True)
            continue
        if not path.is_file():
            raise receipts.ReleaseControlError(
                "recovery controller evidence source contains a special file"
            )
        _write_exclusive(
            target,
            receipts._read_regular(  # noqa: SLF001
                path,
                label=f"recovery controller evidence {relative.as_posix()}",
                max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
            ),
        )
    if _directory_file_identity(
        source, label="recovery controller source evidence"
    ) != _directory_file_identity(destination, label="recovery controller copied evidence"):
        raise receipts.ReleaseControlError(
            "recovery controller copied evidence inventory conflicts"
        )


def _require_normalized_evidence_inventory(evidence_root: Path) -> None:
    expected_directories = {
        "authorization-artifact",
        "dependency-artifact",
        "fresh-recovery-sources",
        "reader-credential",
    }
    expected_files = {
        "controller-inputs.json",
        "current-recovery-authority.json",
        "current-recovery-authority.json.sig",
        "github-admission-authority-verification.json",
        "recovery-authority-verification.json",
        "recovery-smoke-report.json",
    }
    if (
        evidence_root.is_symlink()
        or not evidence_root.is_dir()
        or {entry.name for entry in evidence_root.iterdir()}
        != expected_directories | expected_files
    ):
        raise receipts.ReleaseControlError(
            "recovery controller normalized evidence inventory is invalid"
        )
    for name in expected_directories:
        path = evidence_root / name
        if path.is_symlink() or not path.is_dir():
            raise receipts.ReleaseControlError(
                "recovery controller normalized evidence inventory is invalid"
            )
    for name in expected_files:
        path = evidence_root / name
        if path.is_symlink() or not path.is_file():
            raise receipts.ReleaseControlError(
                "recovery controller normalized evidence inventory is invalid"
            )


def _load_or_create_normalized_evidence(
    *,
    work_root: Path,
    tree_sources: Mapping[str, Path],
    file_bytes: Mapping[str, bytes],
) -> Path:
    """Install the exact capsule evidence tree as one atomic unpublished stage."""

    root = work_root / "normalized-evidence"

    def validate(candidate: Path) -> None:
        _require_normalized_evidence_inventory(candidate)
        for name, source in tree_sources.items():
            if _directory_file_identity(
                source, label=f"normalized evidence source {name}"
            ) != _directory_file_identity(
                candidate / name,
                label=f"normalized evidence installed {name}",
            ):
                raise receipts.ReleaseControlError(
                    "recovery controller normalized evidence tree conflicts"
                )
        for name, expected in file_bytes.items():
            observed = receipts._read_regular(  # noqa: SLF001
                candidate / name,
                label=f"normalized evidence installed {name}",
                max_bytes=max(1, len(expected)),
            )
            if observed != expected:
                raise receipts.ReleaseControlError(
                    "recovery controller normalized evidence file conflicts"
                )

    if root.exists() or root.is_symlink():
        validate(root)
        return root
    staging = Path(tempfile.mkdtemp(prefix=".normalized-evidence-", dir=work_root))
    staging.chmod(0o700)
    try:
        for name, source in tree_sources.items():
            _copy_evidence_tree(source, staging / name)
        for name, raw in file_bytes.items():
            _write_exclusive(staging / name, raw)
        validate(staging)
        try:
            receipts._rename_noreplace(staging, root)  # noqa: SLF001
        except FileExistsError:
            validate(root)
        else:
            receipts._fsync_directory(work_root)  # noqa: SLF001
        validate(root)
        return root
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _load_or_create_prepare_authority_assets(
    *,
    work_root: Path,
    assets: Mapping[str, bytes],
) -> Path:
    """Install the exact three-asset prepare handoff as one atomic local stage."""

    if set(assets) != PREPARE_AUTHORITY_ASSETS:
        raise receipts.ReleaseControlError(
            "prepare capsule authority source inventory is not exact"
        )
    root = work_root / "prepare-capsule-authority"

    def validate(candidate: Path) -> None:
        observed = _prepare_authority_asset_bytes(candidate)
        if observed != dict(assets):
            raise receipts.ReleaseControlError("prepare capsule authority local stage conflicts")

    if root.exists() or root.is_symlink():
        validate(root)
        return root
    staging = Path(tempfile.mkdtemp(prefix=".prepare-capsule-authority-", dir=work_root))
    staging.chmod(0o700)
    try:
        for name, raw in sorted(assets.items()):
            _write_exclusive(staging / name, raw)
        validate(staging)
        try:
            receipts._rename_noreplace(staging, root)  # noqa: SLF001
        except FileExistsError:
            validate(root)
        else:
            receipts._fsync_directory(work_root)  # noqa: SLF001
        validate(root)
        return root
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _validate_staging_artifact(root: Path, *, source_sha: str) -> bytes:
    entries = tuple(root.iterdir()) if root.is_dir() and not root.is_symlink() else ()
    if {entry.name for entry in entries} != {
        "recovery",
        "recovery-smoke-report.json",
    } or any(
        (entry.name == "recovery" and (not entry.is_dir() or entry.is_symlink()))
        or (
            entry.name == "recovery-smoke-report.json"
            and (not entry.is_file() or entry.is_symlink())
        )
        for entry in entries
    ):
        raise receipts.ReleaseControlError("recovery dependency artifact inventory is not exact")
    smoke_raw = receipts._read_regular(  # noqa: SLF001
        root / "recovery-smoke-report.json",
        label="production recovery smoke report",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    smoke = _canonical_object(smoke_raw, label="production recovery smoke report")
    receipts._validate_schema(  # noqa: SLF001
        "kestrel.recovery_capsule_smoke.v1",
        smoke,
        label="production recovery smoke report",
    )
    projection = dict(smoke)
    claimed_digest = projection.pop("report_digest", None)
    if (
        smoke.get("source_sha") != source_sha
        or smoke.get("validation_status") != "validated"
        or claimed_digest != _sha256(receipts.canonical_json_bytes(projection))
    ):
        raise receipts.ReleaseControlError("production recovery smoke report binding mismatch")
    return smoke_raw


def _validate_production_preparation(
    *,
    request: RecoveryControllerRequest,
    authorization: AuthorizationMaterial,
    dependency_root: Path,
    root: Path,
    expected_candidate_archive: bytes,
) -> PreparedProductionCapsule:
    """Replay the exact slow preparation without consuming current authority."""

    expected_inventory = {
        "candidate-archive.tar",
        "controller-preparation.json",
        "recovery-environment-manifest.json",
    }
    if (
        root.is_symlink()
        or not root.is_dir()
        or {entry.name for entry in root.iterdir()} != expected_inventory
    ):
        raise receipts.ReleaseControlError("production recovery preparation inventory is invalid")
    candidate_path = root / "candidate-archive.tar"
    environment_path = root / "recovery-environment-manifest.json"
    receipt_path = root / "controller-preparation.json"
    candidate_raw = receipts._read_regular(  # noqa: SLF001
        candidate_path,
        label="production recovery prepared candidate archive",
        max_bytes=2_147_483_648,
    )
    environment_raw = receipts._read_regular(  # noqa: SLF001
        environment_path,
        label="production recovery prepared environment manifest",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    receipt_raw = receipts._read_regular(  # noqa: SLF001
        receipt_path,
        label="production recovery preparation receipt",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    environment = _canonical_object(
        environment_raw, label="production recovery prepared environment manifest"
    )
    receipts._validate_schema(  # noqa: SLF001
        "kestrel.recovery_environment.v1",
        environment,
        label="production recovery prepared environment manifest",
    )
    preparation = _canonical_object(receipt_raw, label="production recovery preparation receipt")
    receipts._require_exact_fields(  # noqa: SLF001
        preparation,
        frozenset(
            {
                "schema",
                "request_journal_digest",
                "source_sha",
                "candidate_manifest_digest",
                "promotion_run_id",
                "authorization",
                "dependency",
                "target_destination",
                "candidate_archive",
                "environment_probe",
                "provenance",
                "confidence",
                "validation_status",
            }
        ),
        label="production recovery preparation receipt",
    )
    authorization_binding = receipts._object(  # noqa: SLF001
        preparation.get("authorization"),
        label="production recovery preparation authorization",
    )
    dependency_binding = receipts._object(  # noqa: SLF001
        preparation.get("dependency"),
        label="production recovery preparation dependency",
    )
    candidate_binding = receipts._object(  # noqa: SLF001
        preparation.get("candidate_archive"),
        label="production recovery preparation candidate archive",
    )
    probe = receipts._object(  # noqa: SLF001
        preparation.get("environment_probe"),
        label="production recovery preparation environment probe",
    )
    sys_path_value = probe.get("sys_path")
    runtime_value = probe.get("runtime")
    if (
        type(sys_path_value) is not list
        or not sys_path_value
        or any(type(item) is not str or not Path(item).is_absolute() for item in sys_path_value)
        or type(runtime_value) is not dict
        or set(runtime_value) != {"implementation", "version", "abi"}
        or any(type(value) is not str or not value for value in runtime_value.values())
    ):
        raise receipts.ReleaseControlError(
            "production recovery preparation environment probe is invalid"
        )
    dependency_receipt = receipts._read_regular(  # noqa: SLF001
        dependency_root / "recovery/dependency-staging-receipt.json",
        label="production recovery dependency staging receipt",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    destination = request.target_workspace_root / "transaction" / "capsule"
    expected_environment_root = destination.parent / "recovery-runtime/environment"
    expected_site_packages = expected_environment_root / "lib/python3.11/site-packages"
    expected_base_library_root = destination.parent / "recovery-runtime/base/lib"
    python_sha256 = receipts._digest(  # noqa: SLF001
        probe.get("python_sha256"),
        label="production recovery preparation Python digest",
    )
    base_library_root = Path(
        receipts._validate_string(  # noqa: SLF001
            probe.get("base_library_root"),
            label="production recovery preparation base library root",
        )
    )
    if (
        candidate_raw != expected_candidate_archive
        or preparation.get("schema") != "kestrel.recovery_capsule_controller_preparation.v1"
        or preparation.get("request_journal_digest") != _grant_request_journal_digest(request)
        or preparation.get("source_sha") != request.source_sha
        or preparation.get("candidate_manifest_digest") != request.candidate_manifest_digest
        or preparation.get("promotion_run_id") != request.promotion_run_id
        or authorization_binding
        != {
            "candidate_manifest_digest": _sha256(authorization.candidate_manifest),
            "transaction_authorization_digest": _sha256(authorization.transaction_authorization),
        }
        or dependency_binding
        != {
            "artifact_api_digest": request.staging_artifact_digest,
            "staging_receipt_digest": _sha256(dependency_receipt),
        }
        or preparation.get("target_destination") != str(destination)
        or candidate_binding
        != {
            "sha256": _sha256(candidate_raw),
            "size_bytes": len(candidate_raw),
        }
        or set(probe)
        != {
            "sys_path",
            "runtime",
            "python_sha256",
            "base_library_root",
            "environment_manifest_digest",
        }
        or probe.get("environment_manifest_digest") != _sha256(environment_raw)
        or environment.get("environment_root") != str(expected_environment_root)
        or environment.get("site_packages_path") != str(expected_site_packages)
        or base_library_root != expected_base_library_root
        or preparation.get("provenance")
        != {
            "producer": "scripts/recovery_capsule_controller.py",
            "provider": "owner-controller",
            "method": "slow-non-authoritative-production-preparation",
        }
        or preparation.get("confidence") != 1
        or preparation.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError("production recovery preparation binding conflicts")
    return PreparedProductionCapsule(
        root=root,
        candidate_archive=candidate_path,
        environment_manifest_raw=environment_raw,
        environment_manifest=environment,
        sys_path=tuple(cast(list[str], sys_path_value)),
        runtime={
            name: cast(str, runtime_value[name]) for name in ("implementation", "version", "abi")
        },
        python_sha256=python_sha256,
        base_library_root=base_library_root,
        receipt=receipt_raw,
    )


def _load_or_create_production_preparation(
    *,
    request: RecoveryControllerRequest,
    authorization: AuthorizationMaterial,
    dependency_root: Path,
) -> PreparedProductionCapsule:
    """Finish slow local setup before reading any short-lived authority."""

    candidate_raw = receipts.deterministic_recovery_capsule_archive(authorization.candidate_root)
    root = request.work_root / "production-preparation"
    if root.exists() or root.is_symlink():
        return _validate_production_preparation(
            request=request,
            authorization=authorization,
            dependency_root=dependency_root,
            root=root,
            expected_candidate_archive=candidate_raw,
        )
    destination = request.target_workspace_root / "transaction" / "capsule"
    (
        sys_path_value,
        runtime,
        python_sha256,
        base_library_root,
        environment,
    ) = _probe_final_python(
        destination=destination,
        dependency_root=dependency_root,
    )
    environment_raw = receipts.canonical_json_bytes(environment)
    dependency_receipt = receipts._read_regular(  # noqa: SLF001
        dependency_root / "recovery/dependency-staging-receipt.json",
        label="production recovery dependency staging receipt",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    preparation: receipts.JSONObject = {
        "schema": "kestrel.recovery_capsule_controller_preparation.v1",
        "request_journal_digest": _grant_request_journal_digest(request),
        "source_sha": request.source_sha,
        "candidate_manifest_digest": request.candidate_manifest_digest,
        "promotion_run_id": request.promotion_run_id,
        "authorization": {
            "candidate_manifest_digest": _sha256(authorization.candidate_manifest),
            "transaction_authorization_digest": _sha256(authorization.transaction_authorization),
        },
        "dependency": {
            "artifact_api_digest": request.staging_artifact_digest,
            "staging_receipt_digest": _sha256(dependency_receipt),
        },
        "target_destination": str(destination),
        "candidate_archive": {
            "sha256": _sha256(candidate_raw),
            "size_bytes": len(candidate_raw),
        },
        "environment_probe": {
            "sys_path": cast(list[receipts.JSONValue], sys_path_value),
            "runtime": cast(receipts.JSONObject, runtime),
            "python_sha256": python_sha256,
            "base_library_root": str(base_library_root),
            "environment_manifest_digest": _sha256(environment_raw),
        },
        "provenance": {
            "producer": "scripts/recovery_capsule_controller.py",
            "provider": "owner-controller",
            "method": "slow-non-authoritative-production-preparation",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    staging = Path(tempfile.mkdtemp(prefix=".production-preparation-", dir=request.work_root))
    staging.chmod(0o700)
    try:
        _write_exclusive(staging / "candidate-archive.tar", candidate_raw)
        _write_exclusive(staging / "recovery-environment-manifest.json", environment_raw)
        _write_exclusive(
            staging / "controller-preparation.json",
            receipts.canonical_json_bytes(preparation),
        )
        os.replace(staging, root)
        staging = root
    except BaseException:
        if staging.exists() and staging != root:
            shutil.rmtree(staging)
        raise
    return _validate_production_preparation(
        request=request,
        authorization=authorization,
        dependency_root=dependency_root,
        root=root,
        expected_candidate_archive=candidate_raw,
    )


def _capsule_stage_record(
    *,
    capsule_root: Path,
    manifest_raw: bytes,
    closure_raw: bytes,
    recovery_authority: RecoveryAuthorityMaterial,
) -> receipts.JSONObject:
    capsule_identity = _directory_file_identity(
        capsule_root, label="production recovery capsule stage"
    )
    return {
        "schema": "kestrel.recovery_capsule_controller_stage.v1",
        "manifest_digest": _sha256(manifest_raw),
        "closure_digest": _sha256(closure_raw),
        "authority_receipt_digest": _sha256(recovery_authority.receipt),
        "authority_signature_digest": _sha256(recovery_authority.signature),
        "capsule_identity_digest": _sha256(receipts.canonical_json_bytes(capsule_identity)),
        "validation_status": "validated",
    }


def _load_production_capsule(
    *,
    work_root: Path,
    preparation: PreparedProductionCapsule,
    fresh_sources: FreshRecoverySources,
    recovery_authority: RecoveryAuthorityMaterial,
) -> tuple[Path, bytes, bytes]:
    if preparation.root != work_root / "production-preparation":
        raise receipts.ReleaseControlError("recovery controller preparation root conflicts")
    regular_paths = {
        name: work_root / name
        for name in (
            "recovery-execution-closure.json",
            "recovery-authority.json",
            "recovery-authority.json.sig",
            "capsule-stage.json",
        )
    }
    capsule_root = work_root / "capsule-source"
    if (
        any(not path.is_file() or path.is_symlink() for path in regular_paths.values())
        or not capsule_root.is_dir()
        or capsule_root.is_symlink()
    ):
        raise receipts.ReleaseControlError(
            "recovery controller local capsule resume inventory is incomplete"
        )
    authority_receipt = receipts._read_regular(  # noqa: SLF001
        regular_paths["recovery-authority.json"],
        label="resumed capsule recovery authority",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    authority_signature = receipts._read_regular(  # noqa: SLF001
        regular_paths["recovery-authority.json.sig"],
        label="resumed capsule recovery authority signature",
        max_bytes=1024 * 1024,
    )
    if (
        authority_receipt != recovery_authority.receipt
        or authority_signature != recovery_authority.signature
    ):
        raise receipts.ReleaseControlError("recovery controller local capsule authority conflicts")
    closure_raw = receipts._read_regular(  # noqa: SLF001
        regular_paths["recovery-execution-closure.json"],
        label="resumed recovery execution closure",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    closure = _canonical_object(closure_raw, label="resumed recovery execution closure")
    receipts._validate_schema(  # noqa: SLF001
        "kestrel.recovery_execution_closure.v1",
        closure,
        label="resumed recovery execution closure",
    )
    _public_key, owner_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
        fresh_sources.owner_signing_keys_observation,
        expected_fingerprint=None,
    )
    _manifest, manifest_raw = receipts.verify_recovery_capsule_root(
        capsule_root,
        expected_owner_key_fingerprint=owner_fingerprint,
    )
    manifest = _canonical_object(manifest_raw, label="resumed recovery capsule manifest")
    repository = receipts._object(  # noqa: SLF001
        manifest.get("recovery_repository"),
        label="resumed recovery capsule repository",
    )
    if repository.get("authority_receipt_digest") != _sha256(authority_receipt) or repository.get(
        "authority_signature_digest"
    ) != _sha256(authority_signature):
        raise receipts.ReleaseControlError(
            "recovery controller local capsule authority binding conflicts"
        )
    stage_raw = receipts._read_regular(  # noqa: SLF001
        regular_paths["capsule-stage.json"],
        label="resumed recovery capsule stage receipt",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    stage = _canonical_object(stage_raw, label="resumed recovery capsule stage receipt")
    if stage != _capsule_stage_record(
        capsule_root=capsule_root,
        manifest_raw=manifest_raw,
        closure_raw=closure_raw,
        recovery_authority=recovery_authority,
    ):
        raise receipts.ReleaseControlError(
            "recovery controller local capsule stage receipt conflicts"
        )
    return capsule_root, manifest_raw, closure_raw


def _require_current_capsule_publish_scope(
    *,
    work_root: Path,
    preparation: PreparedProductionCapsule,
    fresh_sources: FreshRecoverySources,
    recovery_authority: RecoveryAuthorityMaterial,
    tag: str,
    repository_id: int,
    frozen_scope: Mapping[str, object],
) -> receipts.JSONObject:
    """Rejoin the atomic capsule marker to the exact bytes before a grant."""

    capsule_root, manifest_raw, _closure_raw = _load_production_capsule(
        work_root=work_root,
        preparation=preparation,
        fresh_sources=fresh_sources,
        recovery_authority=recovery_authority,
    )
    observed = _capsule_publish_stage_scope(
        capsule_root=capsule_root,
        manifest_raw=manifest_raw,
        tag=tag,
        repository_id=repository_id,
    )
    expected = _canonical_stage_scope(frozen_scope)
    if observed != expected:
        raise receipts.ReleaseControlError("recovery capsule mutation scope changed after staging")
    return observed


def _require_current_prepare_publish_scope(
    *,
    asset_root: Path,
    promotion_run_id: int,
    repository_id: int,
    frozen_scope: Mapping[str, object],
) -> receipts.JSONObject:
    """Recompute the exact prepare asset scope immediately before a grant."""

    observed = _prepare_publish_stage_scope(
        asset_root=asset_root,
        promotion_run_id=promotion_run_id,
        repository_id=repository_id,
    )
    expected = _canonical_stage_scope(frozen_scope)
    if observed != expected:
        raise receipts.ReleaseControlError(
            "prepare capsule authority mutation scope changed after staging"
        )
    return observed


def _create_production_capsule(
    *,
    source_root: Path,
    authorization: AuthorizationMaterial,
    dependency_root: Path,
    evidence_root: Path,
    preparation: PreparedProductionCapsule,
    fresh_sources: FreshRecoverySources,
    recovery_authority: RecoveryAuthorityMaterial,
    destination: Path,
    work_root: Path,
    _clock: Callable[[], datetime],
) -> tuple[Path, bytes, bytes]:
    closure = build_recovery_execution_closure(
        source_root=source_root,
        dependency_root=dependency_root,
        destination=destination,
        candidate_archive=preparation.candidate_archive,
        environment_manifest_output=(preparation.root / "recovery-environment-manifest.json"),
        evidence_root=evidence_root,
        target_source_root=destination.parents[1],
        prepared_environment=preparation,
    )
    closure_raw = receipts.canonical_json_bytes(closure)
    closure_path = work_root / "recovery-execution-closure.json"
    _write_exclusive(closure_path, closure_raw)
    authority_receipt = work_root / "recovery-authority.json"
    authority_signature = work_root / "recovery-authority.json.sig"
    _write_exclusive(authority_receipt, recovery_authority.receipt)
    _write_exclusive(authority_signature, recovery_authority.signature)
    capsule_root = work_root / "capsule-source"
    staging_parent = Path(tempfile.mkdtemp(prefix=".capsule-build-", dir=work_root))
    staging_parent.chmod(0o700)
    staging_capsule = staging_parent / "capsule-source"
    try:
        result = receipts.main(
            [
                "create-recovery-capsule",
                "--candidate-archive",
                str(preparation.candidate_archive),
                "--transaction-authorization",
                str(authorization.root / "release-authorization.json"),
                "--admission-receipt",
                str(authorization.root / "transaction-identity" / "dispatch-admission.json"),
                "--admission-signature",
                str(authorization.root / "transaction-identity" / "dispatch-admission.json.sig"),
                "--admission-verification",
                str(
                    authorization.root
                    / "transaction-identity"
                    / "dispatch-admission-verification.json"
                ),
                "--owner-key-observation",
                str(
                    work_root
                    / "capsule-authority-binding/fresh-sources/owner-signing-keys-observation.json"
                ),
                "--normalized-evidence-root",
                str(evidence_root),
                "--schema-root",
                str(source_root / "schemas"),
                "--source-root",
                str(source_root),
                "--dependency-root",
                str(dependency_root),
                "--gitleaks-image",
                receipts._GITLEAKS_IMAGE,  # noqa: SLF001
                "--gitleaks-ignore",
                ".gitleaksignore",
                "--recovery-authority-receipt",
                str(authority_receipt),
                "--recovery-authority-signature",
                str(authority_signature),
                "--recovery-repository-observation",
                str(work_root / "capsule-authority-binding/fresh-sources/recovery-repository.json"),
                "--execution-closure",
                str(closure_path),
                "--environment-manifest",
                str(preparation.root / "recovery-environment-manifest.json"),
                "--output-root",
                str(staging_capsule),
            ],
            _clock=_clock,
        )
        if result != 0:
            raise receipts.ReleaseControlError("production recovery capsule creation failed")
        _public_key, owner_fingerprint = receipts._offline_owner_signing_key(  # noqa: SLF001
            fresh_sources.owner_signing_keys_observation,
            expected_fingerprint=None,
        )
        _manifest, manifest_raw = receipts.verify_recovery_capsule_root(
            staging_capsule,
            expected_owner_key_fingerprint=owner_fingerprint,
        )
        if capsule_root.exists() or capsule_root.is_symlink():
            raise receipts.ReleaseControlError("production recovery capsule stage creation raced")
        os.replace(staging_capsule, capsule_root)
        stage = _capsule_stage_record(
            capsule_root=capsule_root,
            manifest_raw=manifest_raw,
            closure_raw=closure_raw,
            recovery_authority=recovery_authority,
        )
        _write_exclusive(
            work_root / "capsule-stage.json",
            receipts.canonical_json_bytes(stage),
        )
        return capsule_root, manifest_raw, closure_raw
    finally:
        if staging_parent.exists():
            shutil.rmtree(staging_parent)


def _capture_unsigned_signing_sources(
    *,
    source_root: Path,
    output_root: Path,
    expected_repository_id: int,
    api: transaction.GitHubReadAPI,
    _clock: Callable[[], datetime],
) -> FreshRecoverySources:
    """Renew unpublished signing scratch; only a signed receipt makes it history."""

    if output_root.exists() or output_root.is_symlink():
        if output_root.is_symlink() or not output_root.is_dir():
            raise receipts.ReleaseControlError(
                "unsigned recovery signing source scratch is invalid"
            )
        shutil.rmtree(output_root)
    sources = capture_fresh_recovery_sources(
        source_root=source_root,
        output_root=output_root,
        api=api,
        _clock=_clock,
    )
    if sources.repository_id != expected_repository_id:
        raise receipts.ReleaseControlError("recovery controller signing repository ID changed")
    return sources


def run_production_controller(
    *,
    request: RecoveryControllerRequest,
    actions_api: ActionsArtifactAPI,
    terminal_api: transaction.TerminalReleaseAPI,
    owner_read_api: transaction.GitHubReadAPI,
    recovery_reader_api: transaction.GitHubReadAPI,
    recovery_reader_token: bytes,
    capsule_publisher: CapsulePublisher,
    verification_signer: VerificationSigner = (transaction.sign_recovery_capsule_verification),
    prepare_only: bool = False,
    _clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> receipts.JSONObject:
    """Construct, publish, independently verify, and hand off one recovery capsule."""

    scalars = _validate_controller_request_scalars(request)
    _require_controller_paths(request)
    identity_digest = _path_sha256(request.identity_file)
    source_sha = scalars.source_sha
    candidate_digest = scalars.candidate_manifest_digest
    run_id = scalars.promotion_run_id
    expected_repository_id = scalars.recovery_repository_id
    resuming = _open_controller_workspace(request, scalars)
    completed_result = _load_completed_controller_result(request, scalars)
    if completed_result is not None:
        return completed_result
    _recover_interrupted_local_stage(request, resuming=resuming)

    authorization_artifact = acquire_actions_artifact(
        api=actions_api,
        specification=ActionsArtifactSpec(
            name=f"kestrel-release-transaction-authorization-{run_id}-1",
            workflow_path=".github/workflows/release.yml",
            run_id=run_id,
            artifact_id=scalars.authorization_artifact_id,
            api_digest=scalars.authorization_artifact_digest,
            source_sha=source_sha,
            require_completed_success=False,
        ),
        output_root=request.work_root / "authorization-artifact",
    )
    dependency_artifact = acquire_actions_artifact(
        api=actions_api,
        specification=ActionsArtifactSpec(
            name=f"kestrel-recovery-dependencies-{source_sha}",
            workflow_path=".github/workflows/recovery-dependency-staging.yml",
            run_id=scalars.staging_run_id,
            artifact_id=scalars.staging_artifact_id,
            api_digest=scalars.staging_artifact_digest,
            source_sha=source_sha,
            require_completed_success=True,
        ),
        output_root=request.work_root / "dependency-artifact",
    )
    authorization = validate_authorization_artifact(
        root=authorization_artifact.root,
        source_root=request.source_root,
        source_sha=source_sha,
        promotion_run_id=run_id,
        candidate_manifest_digest=candidate_digest,
        _clock=_clock,
    )
    smoke_raw = _validate_staging_artifact(dependency_artifact.root, source_sha=source_sha)
    preparation = _load_or_create_production_preparation(
        request=request,
        authorization=authorization,
        dependency_root=dependency_artifact.root,
    )
    if prepare_only:
        return _canonical_object(
            preparation.receipt,
            label="production recovery preparation result",
        )
    reader_material = load_recovery_reader_material(
        authorization.root / "transaction-identity" / "recovery-reader"
    )
    authority_verification_raw = receipts._read_regular(  # noqa: SLF001
        authorization.root / "authority-evidence" / "recovery-authority-verification.json",
        label="controller recovery authority verification",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    historical_recovery_authority = _validate_recovery_authority_record(
        raw=authority_verification_raw,
        fresh_owner_signing_keys_observation=(reader_material.owner_signing_keys_observation),
        expected_repository_id=expected_repository_id,
        source_registry=_source_registry(request.source_root),
        authorization_time=receipts.parse_timestamp(
            authorization.transaction.get("authorized_at"),
            label="controller transaction authorized_at",
        ),
        _clock=_clock,
    )
    capsule_authority = _load_or_create_capsule_authority_binding(
        request=request,
        expected_identity_digest=identity_digest,
        reader_material=reader_material,
        transaction_authorization=authorization.transaction,
        expected_repository_id=expected_repository_id,
        owner_read_api=owner_read_api,
        recovery_reader_api=recovery_reader_api,
        recovery_reader_token=recovery_reader_token,
        _clock=_clock,
    )
    current_recovery_authority = capsule_authority.authority
    fresh_sources = capsule_authority.fresh_sources
    reader_verification = capsule_authority.reader_runtime_verification
    if historical_recovery_authority.authority.get(
        "repository"
    ) != current_recovery_authority.authority.get("repository"):
        raise receipts.ReleaseControlError(
            "current recovery authority repository differs from transaction authority"
        )
    controller_inputs: receipts.JSONObject = {
        "schema": "kestrel.recovery_capsule_controller_inputs.v2",
        "source_sha": source_sha,
        "candidate_manifest_digest": candidate_digest,
        "promotion_run_id": run_id,
        "authorization_artifact": {
            "id": scalars.authorization_artifact_id,
            "api_digest": scalars.authorization_artifact_digest,
        },
        "dependency_artifact": {
            "run_id": scalars.staging_run_id,
            "id": scalars.staging_artifact_id,
            "api_digest": scalars.staging_artifact_digest,
        },
        "target_workspace_root": str(request.target_workspace_root),
        "recovery_repository_id": expected_repository_id,
        "recovery_reader": {
            "scope_authority_digest": _sha256(reader_material.scope_authority),
            "historical_runtime_verification_digest": _sha256(reader_material.runtime_verification),
            "current_runtime_verification_digest": _sha256(
                receipts.canonical_json_bytes(reader_verification)
            ),
        },
        "current_recovery_authority": {
            "authority_generation_id": (capsule_authority.authority_generation_id),
            "receipt_digest": _sha256(current_recovery_authority.receipt),
            "signature_digest": _sha256(current_recovery_authority.signature),
        },
        "validation_status": "validated",
    }
    normalized_file_sources = {
        "recovery-smoke-report.json": (dependency_artifact.root / "recovery-smoke-report.json"),
        "recovery-authority-verification.json": (
            authorization.root / "authority-evidence" / "recovery-authority-verification.json"
        ),
        "github-admission-authority-verification.json": (
            authorization.root
            / "authority-evidence"
            / "github-admission-authority-verification.json"
        ),
        "current-recovery-authority.json": (
            capsule_authority.root / "current-recovery-authority.json"
        ),
        "current-recovery-authority.json.sig": (
            capsule_authority.root / "current-recovery-authority.json.sig"
        ),
    }
    normalized_file_bytes = {
        name: receipts._read_regular(  # noqa: SLF001
            source,
            label=f"controller normalized evidence {name}",
            max_bytes=receipts.MAX_SOURCE_ENVELOPE_BYTES,
        )
        for name, source in normalized_file_sources.items()
    }
    normalized_file_bytes["controller-inputs.json"] = receipts.canonical_json_bytes(
        controller_inputs
    )
    evidence_root = _load_or_create_normalized_evidence(
        work_root=request.work_root,
        tree_sources={
            "authorization-artifact": authorization_artifact.evidence_root,
            "dependency-artifact": dependency_artifact.evidence_root,
            "fresh-recovery-sources": capsule_authority.root / "fresh-sources",
            "reader-credential": capsule_authority.root / "reader-credential",
        },
        file_bytes=normalized_file_bytes,
    )

    target_destination = request.target_workspace_root / "transaction" / "capsule"
    capsule_stage_paths = (
        request.work_root / "recovery-execution-closure.json",
        request.work_root / "recovery-authority.json",
        request.work_root / "recovery-authority.json.sig",
        request.work_root / "capsule-source",
        request.work_root / "capsule-stage.json",
    )
    if any(path.exists() or path.is_symlink() for path in capsule_stage_paths):
        capsule_root, manifest_raw, closure_raw = _load_production_capsule(
            work_root=request.work_root,
            preparation=preparation,
            fresh_sources=fresh_sources,
            recovery_authority=current_recovery_authority,
        )
    else:
        _require_clean_source_identity(request)
        _require_target_workspace_empty(request)
        capsule_root, manifest_raw, closure_raw = _create_production_capsule(
            source_root=request.source_root,
            authorization=authorization,
            dependency_root=dependency_artifact.root,
            evidence_root=evidence_root,
            preparation=preparation,
            fresh_sources=fresh_sources,
            recovery_authority=current_recovery_authority,
            destination=target_destination,
            work_root=request.work_root,
            _clock=_clock,
        )
    recovery_tag = f"recovery-{run_id}-1"
    publication_path = request.work_root / "recovery-capsule-publication.json"
    capsule_stage_scope = _capsule_publish_stage_scope(
        capsule_root=capsule_root,
        manifest_raw=manifest_raw,
        tag=recovery_tag,
        repository_id=expected_repository_id,
    )

    def capsule_mutation_guard() -> None:
        current_scope = _require_current_capsule_publish_scope(
            work_root=request.work_root,
            preparation=preparation,
            fresh_sources=fresh_sources,
            recovery_authority=current_recovery_authority,
            tag=recovery_tag,
            repository_id=expected_repository_id,
            frozen_scope=capsule_stage_scope,
        )
        _authorize_current_stage_mutation(
            request=request,
            expected_identity_digest=identity_digest,
            stage_scope=current_scope,
            reader_material=reader_material,
            transaction_authorization=authorization.transaction_authorization,
            transaction_authorization_record=authorization.transaction,
            expected_repository_id=expected_repository_id,
            owner_read_api=owner_read_api,
            recovery_reader_api=recovery_reader_api,
            recovery_reader_token=recovery_reader_token,
            _clock=_clock,
        )

    if not publication_path.exists() and not publication_path.is_symlink():
        capsule_publisher(
            capsule_root,
            recovery_tag,
            expected_repository_id,
            publication_path,
            capsule_mutation_guard,
        )
    publication_raw = receipts._read_regular(  # noqa: SLF001
        publication_path,
        label="controller recovery capsule publication",
        max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
    )
    publication = _canonical_object(
        publication_raw, label="controller recovery capsule publication"
    )
    if (
        publication.get("schema") != "kestrel.recovery_capsule_publication.v1"
        or publication.get("repository") != RECOVERY_REPOSITORY
        or publication.get("repository_id") != expected_repository_id
        or publication.get("tag") != recovery_tag
        or publication.get("manifest_digest") != _sha256(manifest_raw)
        or publication.get("immutable") is not True
        or publication.get("validation_status") != "validated"
    ):
        raise receipts.ReleaseControlError(
            "controller recovery capsule publication receipt conflicts"
        )
    release_id = receipts._safe_integer(  # noqa: SLF001
        publication.get("release_id"),
        label="controller recovery capsule Release ID",
        positive=True,
    )
    remote_sources = capture_remote_capsule_sources(
        source_root=request.source_root,
        recovery_tag=recovery_tag,
        expected_release_id=release_id,
        expected_repository_id=expected_repository_id,
        output_root=request.work_root / "remote-capsule-sources",
        api=recovery_reader_api,
        _clock=_clock,
    )
    verification_claim = transaction.verify_recovery_capsule(
        capsule_manifest=manifest_raw,
        capsule_root=capsule_root,
        recovery_repository_observation=remote_sources.repository_body,
        recovery_release_observation=remote_sources.release_body,
        recovery_assets_observation=remote_sources.assets_body,
        execution_closure=closure_raw,
        expected_candidate_digest=candidate_digest,
        expected_transaction_authorization_digest=_sha256(authorization.transaction_authorization),
        remote_source_records=remote_sources.source_records,
    )
    verification_path = request.work_root / "recovery-capsule-verification.json"
    signing_sources_root = request.work_root / "signing-sources"
    if verification_path.exists() or verification_path.is_symlink():
        signing_sources = _load_fresh_recovery_sources(
            source_root=request.source_root,
            output_root=signing_sources_root,
            replay_at_capture=True,
            _clock=_clock,
        )
        verification_raw = receipts._read_regular(  # noqa: SLF001
            verification_path,
            label="resumed recovery capsule verification",
            max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
        )
        signed_verification = _canonical_object(
            verification_raw,
            label="resumed recovery capsule verification",
        )
        if (
            signed_verification.get("schema") != "kestrel.recovery_capsule_verification.v1"
            or signed_verification.get("verification") != verification_claim
            or signed_verification.get("validation_status") != "validated"
        ):
            raise receipts.ReleaseControlError("resumed recovery capsule verification conflicts")
    else:
        signing_sources = _capture_unsigned_signing_sources(
            source_root=request.source_root,
            output_root=signing_sources_root,
            expected_repository_id=expected_repository_id,
            api=recovery_reader_api,
            _clock=_clock,
        )
        _require_signing_identity_binding(request, identity_digest)
        signed_verification = verification_signer(
            verification_claim=verification_claim,
            identity_file=request.identity_file,
            owner_signing_keys_observation=(signing_sources.owner_signing_keys_observation),
            _clock=_clock,
        )
        verification_raw = receipts.canonical_json_bytes(signed_verification)
        _write_exclusive(verification_path, verification_raw)
    if signing_sources.repository_id != expected_repository_id:
        raise receipts.ReleaseControlError("recovery controller signing repository ID changed")

    prepare_assets = _load_or_create_prepare_authority_assets(
        work_root=request.work_root,
        assets={
            "approval-history-observation.json": (authorization.approval_history_observation),
            "recovery-capsule-publication.json": publication_raw,
            "recovery-capsule-verification.json": verification_raw,
        },
    )
    prepare_stage_scope = _prepare_publish_stage_scope(
        asset_root=prepare_assets,
        promotion_run_id=run_id,
        repository_id=expected_repository_id,
    )

    def prepare_mutation_guard() -> None:
        current_scope = _require_current_prepare_publish_scope(
            asset_root=prepare_assets,
            promotion_run_id=run_id,
            repository_id=expected_repository_id,
            frozen_scope=prepare_stage_scope,
        )
        _authorize_current_stage_mutation(
            request=request,
            expected_identity_digest=identity_digest,
            stage_scope=current_scope,
            reader_material=reader_material,
            transaction_authorization=authorization.transaction_authorization,
            transaction_authorization_record=authorization.transaction,
            expected_repository_id=expected_repository_id,
            owner_read_api=owner_read_api,
            recovery_reader_api=recovery_reader_api,
            recovery_reader_token=recovery_reader_token,
            _clock=_clock,
        )

    prepare_publication = publish_prepare_capsule_authority(
        promotion_run_id=run_id,
        asset_root=prepare_assets,
        journal_path=request.work_root / "prepare-capsule-authority-journal.json",
        candidate_manifest_digest=candidate_digest,
        transaction_authorization=authorization.transaction_authorization,
        owner_signing_keys_observation=(signing_sources.owner_signing_keys_observation),
        api=terminal_api,
        recovery_reader_api=recovery_reader_api,
        mutation_guard=prepare_mutation_guard,
        source_root=request.source_root,
        _clock=_clock,
    )
    prepare_publication_raw = receipts.canonical_json_bytes(prepare_publication)
    _write_exclusive(
        request.work_root / "prepare-capsule-authority-publication.json",
        prepare_publication_raw,
    )
    source_records = {
        "authorization-artifact": receipts.canonical_json_bytes(authorization_artifact.receipt),
        "dependency-artifact": receipts.canonical_json_bytes(dependency_artifact.receipt),
        "controller-inputs": receipts.canonical_json_bytes(controller_inputs),
        "recovery-smoke": smoke_raw,
        "capsule-manifest": manifest_raw,
        "capsule-publication": publication_raw,
        "capsule-verification": verification_raw,
        "signing-owner-keys": signing_sources.owner_signing_keys_observation,
        "current-recovery-authority": current_recovery_authority.receipt,
        "current-recovery-authority-signature": (current_recovery_authority.signature),
        "recovery-reader-scope-authority": reader_material.scope_authority,
        "recovery-reader-historical-verification": (reader_material.runtime_verification),
        "recovery-reader-current-verification": receipts.canonical_json_bytes(reader_verification),
        "prepare-authority-publication": prepare_publication_raw,
    }
    result: receipts.JSONObject = {
        "schema": "kestrel.recovery_capsule_controller.v1",
        "source_sha": source_sha,
        "candidate_manifest_digest": candidate_digest,
        "promotion_run_id": run_id,
        "target_workspace_root": str(request.target_workspace_root),
        "authorization_artifact": authorization_artifact.receipt["artifact"],
        "dependency_artifact": dependency_artifact.receipt["artifact"],
        "recovery_repository": {
            "full_name": RECOVERY_REPOSITORY,
            "id": expected_repository_id,
        },
        "capsule": {
            "tag": recovery_tag,
            "manifest_digest": _sha256(manifest_raw),
            "publication_receipt_digest": _sha256(publication_raw),
            "verification_digest": _sha256(verification_raw),
        },
        "prepare_authority": {
            "tag": prepare_publication["tag_name"],
            "release_id": prepare_publication["release_id"],
            "publication_digest": _sha256(prepare_publication_raw),
        },
        "evidence": {
            "source_bundle_digest": receipts.source_bundle_digest(source_records),
            "canonicalization_vector_digest": receipts.canonicalization_vector_digest(),
        },
        "provenance": {
            "producer": "scripts/recovery_capsule_controller.py",
            "provider": "owner-controller",
            "method": "exact-artifact-authority-bound-recovery-publication",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    if not receipts.write_once(request.output, receipts.canonical_json_bytes(result)):
        raise receipts.ReleaseControlError("recovery controller output creation raced")
    return result


def _required_credential(name: str) -> bytes:
    value = os.environ.get(name)
    if value is None:
        raise receipts.ReleaseControlError(f"production recovery controller requires {name}")
    try:
        return value.encode("ascii", "strict")
    except UnicodeEncodeError as exc:
        raise receipts.ReleaseControlError(
            f"production recovery controller {name} is not ASCII"
        ) from exc


def _capsule_publisher(
    capsule_root: Path,
    tag: str,
    expected_repository_id: int,
    output: Path,
    mutation_guard: Callable[[], None],
) -> None:
    result = receipts.publish_recovery_capsule(
        capsule_root=capsule_root,
        repository=RECOVERY_REPOSITORY,
        tag=tag,
        expected_repository_id=expected_repository_id,
        output=output,
        mutation_guard=mutation_guard,
    )
    if result != 0:
        raise receipts.ReleaseControlError("production recovery capsule publication failed")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--candidate-manifest-digest", required=True)
    parser.add_argument("--promotion-run-id", type=int, required=True)
    parser.add_argument("--authorization-artifact-id", type=int, required=True)
    parser.add_argument("--authorization-artifact-digest", required=True)
    parser.add_argument("--staging-run-id", type=int, required=True)
    parser.add_argument("--staging-artifact-id", type=int, required=True)
    parser.add_argument("--staging-artifact-digest", required=True)
    parser.add_argument("--target-workspace-root", type=Path, required=True)
    parser.add_argument("--recovery-repository-id", type=int, required=True)
    parser.add_argument("--identity-file", type=Path, required=True)
    parser.add_argument("--current-recovery-owner-authority-snapshot", type=Path, required=True)
    parser.add_argument("--current-recovery-repository-observation", type=Path, required=True)
    parser.add_argument(
        "--current-recovery-immutable-releases-observation",
        type=Path,
        required=True,
    )
    parser.add_argument("--current-recovery-controller-context", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        request = RecoveryControllerRequest(
            source_root=args.source_root.absolute(),
            source_sha=args.source_sha,
            candidate_manifest_digest=args.candidate_manifest_digest,
            promotion_run_id=args.promotion_run_id,
            authorization_artifact_id=args.authorization_artifact_id,
            authorization_artifact_digest=args.authorization_artifact_digest,
            staging_run_id=args.staging_run_id,
            staging_artifact_id=args.staging_artifact_id,
            staging_artifact_digest=args.staging_artifact_digest,
            target_workspace_root=args.target_workspace_root.absolute(),
            recovery_repository_id=args.recovery_repository_id,
            identity_file=args.identity_file.absolute(),
            current_recovery_owner_authority_snapshot=(
                args.current_recovery_owner_authority_snapshot.absolute()
            ),
            current_recovery_repository_observation=(
                args.current_recovery_repository_observation.absolute()
            ),
            current_recovery_immutable_releases_observation=(
                args.current_recovery_immutable_releases_observation.absolute()
            ),
            current_recovery_controller_context=(
                args.current_recovery_controller_context.absolute()
            ),
            work_root=args.work_root.absolute(),
            output=args.output.absolute(),
        )
        _validate_controller_request_scalars(request)
        pinned_raw = os.environ.get("KESTREL_PINNED_GH")
        if pinned_raw is None:
            raise receipts.ReleaseControlError(
                "production recovery controller requires KESTREL_PINNED_GH"
            )
        pinned_gh = Path(pinned_raw).resolve(strict=True)
        owner_token = _required_credential("GH_TOKEN")
        reader_token = _required_credential("RELEASE_RECOVERY_READER_TOKEN")
        _require_distinct_credentials(owner_token, reader_token)
        actions_api = GitHubActionsArtifactAPI(pinned_gh=pinned_gh, token=owner_token)
        terminal_api = transaction.GitHubTerminalReleaseAPI(pinned_gh=pinned_gh, token=owner_token)
        owner_api = transaction.DirectGitHubReadAPI(token=owner_token)
        reader_api = transaction.DirectGitHubReadAPI(token=reader_token)
        result = run_production_controller(
            request=request,
            actions_api=actions_api,
            terminal_api=terminal_api,
            owner_read_api=owner_api,
            recovery_reader_api=reader_api,
            recovery_reader_token=reader_token,
            capsule_publisher=_capsule_publisher,
            prepare_only=args.prepare_only,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, sort_keys=True))
    return 0


def _finish_prepare_authority_publication(
    *,
    journal: receipts.JSONObject,
    journal_path: Path,
    api: transaction.TerminalReleaseAPI,
    expected_assets: Mapping[str, tuple[bytes, str]],
    recovery_reader_api: transaction.GitHubReadAPI,
    tag_name: str,
    release_name: str,
    release_body: str,
    repository_id: int,
    run_id: int,
    candidate_manifest_digest: str,
    validation_digests: Mapping[str, receipts.JSONValue],
    mutation_guard: Callable[[], None] | None,
) -> receipts.JSONObject:
    journal_raw = receipts.canonical_json_bytes(journal)
    if journal_path.exists() or journal_path.is_symlink():
        if (
            _canonical_object(
                receipts._read_regular(  # noqa: SLF001
                    journal_path,
                    label="prepare capsule authority journal",
                    max_bytes=receipts.MAX_SOURCE_BODY_BYTES,
                ),
                label="prepare capsule authority journal",
            )
            != journal
        ):
            raise receipts.ReleaseControlError("prepare capsule authority journal conflicts")
    elif not receipts.write_once(journal_path, journal_raw):
        raise receipts.ReleaseControlError("prepare capsule authority journal creation raced")

    def observe() -> transaction.TerminalRelease | None:
        return transaction._inspect_boundary_authority_releases(  # noqa: SLF001
            listing=api.list_releases(RECOVERY_REPOSITORY),
            tag_name=tag_name,
            release_name=release_name,
            release_body=release_body,
            expected_assets=expected_assets,
        )

    release = observe()
    if release is None:
        if mutation_guard is not None:
            mutation_guard()
        release = observe()
        if release is None:
            created_id = api.create_draft(
                RECOVERY_REPOSITORY,
                tag_name=tag_name,
                name=release_name,
                body=release_body,
            )
            release = observe()
            if release is None or not release.draft or release.release_id != created_id:
                raise receipts.ReleaseControlError(
                    "prepare capsule authority draft was not observed exactly"
                )
    release_id = release.release_id

    def observe_bound() -> transaction.TerminalRelease:
        observed = observe()
        if observed is None:
            raise receipts.ReleaseControlError("prepare capsule authority Release disappeared")
        if observed.release_id != release_id:
            raise receipts.ReleaseControlError("prepare capsule authority Release ID changed")
        return observed

    for name, (raw, media_type) in sorted(expected_assets.items()):
        release = observe_bound()
        if name not in {item.name for item in release.assets}:
            if not release.draft:
                raise receipts.ReleaseControlError(
                    "published prepare capsule authority is missing an asset"
                )
            if mutation_guard is not None:
                mutation_guard()
            release = observe_bound()
            if name not in {item.name for item in release.assets}:
                if not release.draft:
                    raise receipts.ReleaseControlError(
                        "published prepare capsule authority is missing an asset"
                    )
                api.upload_asset(
                    RECOVERY_REPOSITORY,
                    release_id=release.release_id,
                    name=name,
                    media_type=media_type,
                    content=raw,
                )
                release = observe_bound()
                if name not in {item.name for item in release.assets}:
                    raise receipts.ReleaseControlError(
                        "prepare capsule authority asset upload was not observed"
                    )
    release = observe_bound()
    if {item.name for item in release.assets} != set(expected_assets):
        raise receipts.ReleaseControlError(
            "prepare capsule authority asset inventory is incomplete before publication"
        )
    if release.draft:
        if mutation_guard is not None:
            mutation_guard()
        release = observe_bound()
        if {item.name for item in release.assets} != set(expected_assets):
            raise receipts.ReleaseControlError(
                "prepare capsule authority asset inventory changed before publication"
            )
        if release.draft:
            api.publish_immutable(RECOVERY_REPOSITORY, release_id=release.release_id)
            release = observe_bound()
    if (
        release is None
        or release.draft
        or not release.immutable
        or {item.name for item in release.assets} != set(expected_assets)
    ):
        raise receipts.ReleaseControlError(
            "prepare capsule authority Release is not exact and immutable"
        )

    independent_release_exchange = recovery_reader_api(
        f"GET /repos/{RECOVERY_REPOSITORY}/releases/tags/{quote(tag_name, safe='')}",
        accept="application/vnd.github+json",
    )
    if independent_release_exchange.http_status != 200:
        raise receipts.ReleaseControlError(
            "independent prepare capsule authority observation failed"
        )
    independent_release = receipts._object(  # noqa: SLF001
        receipts.parse_external_json_bytes(
            independent_release_exchange.response_body,
            label="independent prepare capsule authority Release",
        ),
        label="independent prepare capsule authority Release",
    )
    raw_asset_items = receipts._array(  # noqa: SLF001
        independent_release.get("assets"),
        label="independent prepare capsule authority assets",
    )
    independent_assets: dict[str, bytes] = {}
    asset_ids: set[int] = set()
    for raw_item in raw_asset_items:
        item = receipts._object(  # noqa: SLF001
            raw_item, label="independent prepare capsule authority asset"
        )
        name = receipts._validate_string(  # noqa: SLF001
            item.get("name"), label="independent prepare capsule authority asset name"
        )
        asset_id = receipts._safe_integer(  # noqa: SLF001
            item.get("id"),
            label="independent prepare capsule authority asset ID",
            positive=True,
        )
        if name not in expected_assets or asset_id in asset_ids:
            raise receipts.ReleaseControlError(
                "independent prepare capsule authority asset inventory conflicts"
            )
        asset_ids.add(asset_id)
        expected_raw, media_type = expected_assets[name]
        if (
            item.get("size") != len(expected_raw)
            or item.get("digest") != _sha256(expected_raw)
            or item.get("content_type") != media_type
        ):
            raise receipts.ReleaseControlError(
                "independent prepare capsule authority asset identity conflicts"
            )
        asset_exchange = recovery_reader_api(
            f"GET /repos/{RECOVERY_REPOSITORY}/releases/assets/{asset_id}",
            accept="application/octet-stream",
        )
        if asset_exchange.http_status != 200 or asset_exchange.response_body != expected_raw:
            raise receipts.ReleaseControlError(
                "independent prepare capsule authority asset bytes changed"
            )
        independent_assets[name] = asset_exchange.response_body
    if (
        independent_release.get("id") != release.release_id
        or independent_release.get("tag_name") != tag_name
        or independent_release.get("name") != release_name
        or independent_release.get("body") != release_body
        or independent_release.get("draft") is not False
        or independent_release.get("prerelease") is not False
        or independent_release.get("immutable") is not True
        or set(independent_assets) != set(expected_assets)
    ):
        raise receipts.ReleaseControlError(
            "independent prepare capsule authority Release identity changed"
        )
    return {
        "schema": "kestrel.release_prepare_capsule_authority_publication.v1",
        "repository": RECOVERY_REPOSITORY,
        "repository_id": repository_id,
        "promotion_run_id": run_id,
        "candidate_manifest_digest": candidate_manifest_digest,
        "release_id": release.release_id,
        "tag_name": tag_name,
        "html_url": release.html_url,
        "immutable": True,
        "asset_names": cast(list[receipts.JSONValue], sorted(expected_assets)),
        "evidence": {
            "journal_digest": _sha256(journal_raw),
            "independent_reader_bundle_digest": receipts.source_bundle_digest(independent_assets),
            "validation_digests": dict(validation_digests),
        },
        "provenance": {
            "producer": "scripts/recovery_capsule_controller.py",
            "provider": "github.com",
            "method": "owner-controller-prepare-capsule-authority-publication",
        },
        "confidence": 1,
        "validation_status": "validated",
    }


if __name__ == "__main__":
    raise SystemExit(main())
