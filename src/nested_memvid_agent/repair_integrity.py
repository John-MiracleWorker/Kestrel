from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess  # nosec B404
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from .file_lock import lock_exclusive, unlock
from .platform_primitives import chmod_descriptor
from .security_boundary import (
    redact_secrets,
    redact_text,
    sanitized_subprocess_environment,
)

_MAX_UNTRACKED_FILE_BYTES = 128 * 1024 * 1024
_MAX_CHANGED_BYTES = 512 * 1024 * 1024
_MAX_CHANGED_FILES = 10_000
_MAX_CHANGED_PATH_BYTES = 4 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_GIT_OUTPUT_BYTES = 8 * 1024 * 1024
_MAX_GIT_FILTER_CONFIG_BYTES = 64 * 1024
_MAX_GIT_FILTER_DRIVERS = 256
_SNAPSHOT_TIMEOUT_SECONDS = 30.0
_REPAIR_ARTIFACT_ROOT = Path(".nest")
_REPAIR_RECEIPT_KEY_FILE = "repair_receipt_signing.v2.key"
_REPAIR_RECEIPT_KEY_LOCK_FILE = "repair-receipt-key.lock"
_REPAIR_RECEIPT_KEY_TEMP_FILE = ".repair_receipt_signing.v2.key.tmp"
_REPAIR_RECEIPT_KEY_BYTES = 32
_REPAIR_RECEIPT_SCHEMA_VERSION = 2
_REPAIR_INTEGRITY_SCHEMA_VERSION = 2
_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
_DIRECTORY_FLAGS |= getattr(os, "O_NOFOLLOW", 0)
_FILE_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)


@dataclass(frozen=True)
class _RepairDirectoryHandle:
    """A stable private directory reached by descriptor or checked pathname.

    POSIX always uses ``descriptor`` and retains the existing no-follow dirfd
    traversal.  Windows does not implement Python's dirfd APIs and does not let
    ``os.open`` open directories, so it retains an lstat identity for every
    traversed component and revalidates those identities around each operation.
    """

    path: Path
    descriptor: int | None
    path_identities: tuple[tuple[Path, os.stat_result], ...] = ()


def require_git_root(workspace: Path) -> Path:
    """Return the exact, non-symlink Git top-level or fail closed."""

    requested = Path(workspace)
    if requested.is_symlink():
        raise ValueError("Repair workspace root must not be a symbolic link.")
    try:
        root = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"Repair workspace does not exist: {requested}") from exc
    if not root.is_dir():
        raise ValueError(f"Repair workspace is not a directory: {root}")
    top_level = _git_text(root, ["rev-parse", "--show-toplevel"])
    try:
        git_root = Path(top_level).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("Git reported a missing repair workspace root.") from exc
    if git_root != root:
        raise ValueError(
            "Repair tools require the workspace to be the exact Git top-level; "
            f"got {root}, repository root is {git_root}."
        )
    return root


def repair_snapshot(workspace: Path) -> dict[str, Any]:
    """Return a content-complete, deterministic fingerprint of a repair candidate.

    Git's ordinary text diff omits untracked files and may omit binary contents.
    This fingerprint combines a full-index binary diff with a sorted manifest and
    content digest for every untracked regular file. Kestrel's own private repair
    receipts are excluded so recording evidence does not invalidate that evidence.
    """

    root = require_git_root(workspace)
    deadline = time.monotonic() + _SNAPSHOT_TIMEOUT_SECONDS
    branch = _git_text(root, ["branch", "--show-current"])
    head_sha = _git_text(root, ["rev-parse", "HEAD"])
    tracked_files = _git_z_paths(
        root,
        [
            "diff",
            "--name-only",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            "-z",
            "HEAD",
            "--",
        ],
    )
    untracked_files = _git_z_paths(
        root,
        ["ls-files", "--others", "--exclude-standard", "-z", "--"],
    )
    tracked_files = sorted({path for path in tracked_files if not _is_private_repair_path(path)})
    untracked_files = sorted(
        {path for path in untracked_files if not _is_private_repair_path(path)}
    )
    changed_files = sorted(set(tracked_files) | set(untracked_files))
    if len(changed_files) > _MAX_CHANGED_FILES:
        raise ValueError(
            f"Repair has more than {_MAX_CHANGED_FILES} changed files; split the repair."
        )
    path_bytes = sum(len(os.fsencode(path)) for path in changed_files)
    if path_bytes > _MAX_CHANGED_PATH_BYTES:
        raise ValueError(
            f"Repair path manifest exceeds {_MAX_CHANGED_PATH_BYTES} bytes; split the repair."
        )
    untracked_set = set(untracked_files)
    changed_manifest: list[dict[str, Any]] = []
    total_bytes = 0
    for relative_path in changed_files:
        if time.monotonic() > deadline:
            raise TimeoutError("Repair fingerprint exceeded its bounded time budget.")
        entry = _changed_path_manifest(
            root,
            relative_path,
            reject_symlink=relative_path in untracked_set,
            max_bytes=_MAX_CHANGED_BYTES - total_bytes,
            deadline=deadline,
        )
        total_bytes += int(entry.get("size", 0))
        if total_bytes > _MAX_CHANGED_BYTES:
            raise ValueError(
                f"Repair content exceeds {_MAX_CHANGED_BYTES} bytes; split the repair."
            )
        changed_manifest.append(entry)
    untracked_manifest = [entry for entry in changed_manifest if entry["path"] in untracked_set]
    fingerprint_payload = {
        "schema_version": 1,
        "head_sha": head_sha,
        # A final-path manifest is invariant to index staging. This lets commit
        # stage exact reviewed paths and then recheck the same candidate digest.
        "changed": changed_manifest,
    }
    canonical = json.dumps(
        fingerprint_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    diff_digest = hashlib.sha256(canonical).hexdigest()
    return {
        "schema_version": 1,
        "branch": branch,
        "head_sha": head_sha,
        "diff_digest": diff_digest,
        # Keep the established public field while upgrading its semantics.
        "diff_hash": diff_digest,
        "candidate_bytes": total_bytes,
        "path_manifest_bytes": path_bytes,
        "tracked_files": tracked_files,
        "untracked_files": untracked_files,
        "untracked_manifest": untracked_manifest,
        "changed_manifest": changed_manifest,
        "changed_files": changed_files,
        "empty": not changed_files,
    }


def write_validation_receipt(
    workspace: Path,
    *,
    tool_name: str,
    command: list[str],
    success: bool,
    returncode: int | None,
    content: str,
    validation_evidence: dict[str, object],
    snapshot: dict[str, Any],
    started_at: str,
    isolation_attestation: dict[str, Any],
) -> dict[str, Any]:
    trusted_isolation = _validated_isolation_attestation(
        isolation_attestation, snapshot=snapshot
    )
    finished_at = _now()
    safe_command = redact_secrets(command)
    if not isinstance(safe_command, list):
        safe_command = ["<redacted>"]
    safe_content = redact_text(content)
    safe_evidence = redact_secrets(validation_evidence)
    if not isinstance(safe_evidence, dict):
        safe_evidence = {}
    seed = json.dumps(
        {
            "tool": tool_name,
            "command": safe_command,
            "started_at": started_at,
            "finished_at": finished_at,
            "diff_digest": snapshot["diff_digest"],
            "nonce": uuid4().hex,
        },
        sort_keys=True,
    )
    validation_id = f"repair_validation_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"
    content_bytes = safe_content.encode("utf-8", errors="replace")
    receipt = {
        "schema_version": _REPAIR_RECEIPT_SCHEMA_VERSION,
        "validation_id": validation_id,
        "tool": tool_name,
        "command": safe_command,
        "success": success,
        "returncode": returncode,
        "started_at": started_at,
        "finished_at": finished_at,
        "output_sha256": hashlib.sha256(content_bytes).hexdigest(),
        "output_chars": len(safe_content),
        "output_excerpt": safe_content[:16_000],
        "output_redacted": True,
        "validation_evidence": safe_evidence,
        "repair_snapshot": snapshot,
        "execution_isolation": trusted_isolation,
    }
    # Rotate away from every legacy or previously exposed workspace key only
    # after the untrusted candidate has finished inside the container. A new
    # validation invalidates earlier review gates by design.
    _rotate_receipt_key(workspace)
    write_repair_artifact(workspace, "repair_validations", validation_id, receipt)
    return receipt


def write_repair_artifact(
    workspace: Path,
    collection: str,
    artifact_id: str,
    payload: dict[str, Any],
) -> Path:
    _validate_artifact_component(collection, expected_prefix="repair_")
    _validate_artifact_component(artifact_id, expected_prefix="repair_")
    relative = _REPAIR_ARTIFACT_ROOT / collection / f"{artifact_id}.json"
    signing_key = _load_or_create_receipt_key(workspace)
    signed_payload = _signed_artifact_payload(payload, signing_key=signing_key)
    encoded = json.dumps(signed_payload, indent=2, sort_keys=True).encode("utf-8")
    if len(encoded) > _MAX_ARTIFACT_BYTES:
        raise ValueError(f"Repair artifact exceeds {_MAX_ARTIFACT_BYTES} bytes.")
    with _repair_directory(workspace, collection=collection, create=True) as directory:
        descriptor = _open_private_file_at(
            directory,
            f"{artifact_id}.json",
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_receipt_key_directory(directory)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return relative


def load_validation_receipt(workspace: Path, validation_id: str) -> dict[str, Any]:
    payload = load_repair_artifact(
        workspace,
        collection="repair_validations",
        artifact_id=validation_id,
        expected_prefix="repair_validation_",
        id_field="validation_id",
    )
    if payload.get("schema_version") != _REPAIR_RECEIPT_SCHEMA_VERSION:
        raise ValueError(
            "Legacy repair validation receipts are not trusted; run isolated validation again."
        )
    snapshot = payload.get("repair_snapshot")
    isolation = payload.get("execution_isolation")
    if not isinstance(snapshot, dict) or not isinstance(isolation, dict):
        raise ValueError("Repair validation receipt has no trusted OCI attestation.")
    _validated_isolation_attestation(isolation, snapshot=snapshot)
    return payload


def load_review_receipt(workspace: Path, review_id: str) -> dict[str, Any]:
    payload = load_repair_artifact(
        workspace,
        collection="repair_reviews",
        artifact_id=review_id,
        expected_prefix="repair_review_",
        id_field="review_id",
    )
    if payload.get("schema_version") != _REPAIR_RECEIPT_SCHEMA_VERSION:
        raise ValueError(
            "Legacy repair review receipts are not trusted; validate and review again."
        )
    return payload


def load_repair_artifact(
    workspace: Path,
    *,
    collection: str,
    artifact_id: str,
    expected_prefix: str,
    id_field: str,
) -> dict[str, Any]:
    _validate_artifact_component(collection, expected_prefix="repair_")
    _validate_artifact_component(artifact_id, expected_prefix=expected_prefix)
    with _repair_directory(workspace, collection=collection, create=False) as directory:
        descriptor = _open_private_file_at(
            directory, f"{artifact_id}.json", _FILE_FLAGS
        )
        try:
            metadata = os.fstat(descriptor)
            _validate_private_file_metadata(metadata, artifact_id)
            if metadata.st_size > _MAX_ARTIFACT_BYTES:
                raise ValueError(f"Repair artifact exceeds {_MAX_ARTIFACT_BYTES} bytes.")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                text = handle.read(_MAX_ARTIFACT_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    try:
        payload = json.loads(text or "")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Repair artifact is invalid JSON: {artifact_id}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Repair artifact identity mismatch: {artifact_id}")
    _verify_artifact_signature(
        payload,
        artifact_id,
        signing_key=_load_receipt_key(workspace),
    )
    if payload.get(id_field) != artifact_id:
        raise ValueError(f"Repair artifact identity mismatch: {artifact_id}")
    return payload


@contextmanager
def repair_action_lock(workspace: Path) -> Iterator[None]:
    with _repair_directory(workspace, create=True) as directory:
        descriptor = _open_private_file_at(
            directory,
            "repair-actions.lock",
            os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _validate_private_file_metadata(os.fstat(descriptor), "repair-actions.lock")
            with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
                descriptor = -1
                lock_exclusive(handle)
                try:
                    yield
                finally:
                    unlock(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)


def utc_now() -> str:
    return _now()


def _artifact_signature_payload(payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in payload.items() if key != "_integrity"}
    return json.dumps(
        unsigned,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _signed_artifact_payload(
    payload: dict[str, Any],
    *,
    signing_key: bytes,
) -> dict[str, Any]:
    # Round-trip before signing so the exact serialized JSON value is covered,
    # and so callers cannot mutate nested objects after the write begins.
    copied = json.loads(json.dumps(payload, ensure_ascii=True))
    if not isinstance(copied, dict):
        raise ValueError("Repair artifact payload must be a JSON object.")
    signature = hmac.new(
        signing_key,
        _artifact_signature_payload(copied),
        hashlib.sha256,
    ).hexdigest()
    copied["_integrity"] = {
        "schema_version": _REPAIR_INTEGRITY_SCHEMA_VERSION,
        "algorithm": "hmac-sha256",
        "key_id": hashlib.sha256(signing_key).hexdigest()[:16],
        "signature": signature,
        "key_scope": "workspace_v2_excluded_from_oci_snapshot",
        "process_bound": False,
        "legacy_workspace_key_accepted": False,
    }
    return copied


def _verify_artifact_signature(
    payload: dict[str, Any],
    artifact_id: str,
    *,
    signing_key: bytes,
) -> None:
    integrity = payload.get("_integrity")
    if not isinstance(integrity, dict):
        raise ValueError(f"Repair artifact is unsigned: {artifact_id}")
    if (
        integrity.get("schema_version") != _REPAIR_INTEGRITY_SCHEMA_VERSION
        or integrity.get("algorithm") != "hmac-sha256"
        or integrity.get("key_id") != hashlib.sha256(signing_key).hexdigest()[:16]
        or integrity.get("key_scope") != "workspace_v2_excluded_from_oci_snapshot"
        or integrity.get("legacy_workspace_key_accepted") is not False
    ):
        raise ValueError(
            "Repair artifact was not created by this Kestrel workspace; validate and review again."
        )
    expected = hmac.new(
        signing_key,
        _artifact_signature_payload(payload),
        hashlib.sha256,
    ).hexdigest()
    signature = str(integrity.get("signature", ""))
    if not hmac.compare_digest(signature, expected):
        raise ValueError(f"Repair artifact integrity check failed: {artifact_id}")


def _validated_isolation_attestation(
    value: dict[str, Any], *, snapshot: dict[str, Any]
) -> dict[str, Any]:
    copied = json.loads(json.dumps(value, ensure_ascii=True))
    if not isinstance(copied, dict):
        raise ValueError("Repair validation isolation attestation must be an object.")
    image = copied.get("image")
    digest = copied.get("source_tree_digest")
    required = (
        copied.get("schema_version") == 1
        and copied.get("mode") == "oci_snapshot_v1"
        and isinstance(image, str)
        and "@sha256:" in image
        and len(image.rsplit("@sha256:", 1)[-1]) == 64
        and all(character in "0123456789abcdef" for character in image.rsplit("@sha256:", 1)[-1])
        and copied.get("network") == "none"
        and copied.get("workspace_mount") == "private_read_only_snapshot"
        and copied.get("host_fallback") is False
        and isinstance(digest, str)
        and digest.startswith("sha256:")
        and len(digest) == 71
        and copied.get("repair_diff_digest") == snapshot.get("diff_digest")
        and copied.get("repair_head_sha") == snapshot.get("head_sha")
        and copied.get("repair_branch") == snapshot.get("branch")
    )
    if not required:
        raise ValueError("Repair validation receipt has an invalid OCI isolation attestation.")
    return copied


def _rotate_receipt_key(workspace: Path) -> bytes:
    """Atomically replace any earlier trust key after isolated validation."""

    with _mcp_sensitive_material_transition():
        with _receipt_key_lock(workspace):
            with _repair_directory(workspace, create=True) as directory:
                _recover_receipt_key_temp(directory)
                try:
                    _load_receipt_key_from_directory(directory)
                except FileNotFoundError:
                    pass
                candidate = secrets.token_bytes(_REPAIR_RECEIPT_KEY_BYTES)
                identity = _write_receipt_key_temp(directory, candidate)
                try:
                    temp_metadata = _stat_private_at(
                        directory, _REPAIR_RECEIPT_KEY_TEMP_FILE
                    )
                    if (temp_metadata.st_dev, temp_metadata.st_ino) != identity:
                        raise ValueError(
                            "Temporary repair receipt signing key identity changed."
                        )
                    _replace_private_at(
                        directory,
                        _REPAIR_RECEIPT_KEY_TEMP_FILE,
                        _REPAIR_RECEIPT_KEY_FILE,
                    )
                    _sync_receipt_key_directory(directory)
                    published = _load_receipt_key_from_directory(directory)
                    if not hmac.compare_digest(published, candidate):
                        raise ValueError(
                            "Rotated repair receipt signing key identity changed."
                        )
                    return published
                finally:
                    try:
                        _remove_receipt_key_temp(
                            directory,
                            expected_identity=identity,
                            missing_ok=True,
                        )
                    except (FileNotFoundError, ValueError):
                        pass


def _load_or_create_receipt_key(workspace: Path) -> bytes:
    with _mcp_sensitive_material_transition():
        with _receipt_key_lock(workspace):
            with _repair_directory(workspace, create=True) as directory:
                _recover_receipt_key_temp(directory)
                try:
                    return _load_receipt_key_from_directory(directory)
                except FileNotFoundError:
                    pass

                candidate = secrets.token_bytes(_REPAIR_RECEIPT_KEY_BYTES)
                temp_identity: tuple[int, int] | None = None
                try:
                    temp_identity = _write_receipt_key_temp(directory, candidate)
                    try:
                        _publish_receipt_key_temp(
                            directory, expected_identity=temp_identity
                        )
                    except FileExistsError:
                        # Another same-owner publisher that does not use Kestrel's
                        # lock may have won. Never replace it; validate and use it.
                        _remove_receipt_key_temp(
                            directory,
                            expected_identity=temp_identity,
                        )
                        temp_identity = None
                        _sync_receipt_key_directory(directory)
                        return _load_receipt_key_from_directory(directory)
                    _remove_receipt_key_temp(
                        directory,
                        expected_identity=temp_identity,
                    )
                    temp_identity = None
                    _sync_receipt_key_directory(directory)
                    published = _load_receipt_key_from_directory(directory)
                    if not hmac.compare_digest(published, candidate):
                        raise ValueError(
                            "Published repair receipt signing key identity changed."
                        )
                    return published
                except BaseException:
                    if temp_identity is not None:
                        try:
                            _remove_receipt_key_temp(
                                directory,
                                expected_identity=temp_identity,
                                missing_ok=True,
                            )
                            _sync_receipt_key_directory(directory)
                        except BaseException:
                            # Preserve the original publication failure. A safe
                            # orphan is removed by the next locked open.
                            pass
                    raise


@contextmanager
def _mcp_sensitive_material_transition() -> Iterator[tuple[str, ...]]:
    """Lazily couple receipt creation to local MCP stdio quiescence."""

    from .mcp_manager import mcp_sensitive_material_transition

    with mcp_sensitive_material_transition() as closed:
        yield closed


def _load_receipt_key(workspace: Path) -> bytes:
    with _receipt_key_lock(workspace):
        with _repair_directory(workspace, create=False) as directory:
            _recover_receipt_key_temp(directory)
            return _load_receipt_key_from_directory(directory)


def _load_receipt_key_from_directory(directory: _RepairDirectoryHandle) -> bytes:
    descriptor = _open_private_file_at(
        directory, _REPAIR_RECEIPT_KEY_FILE, _FILE_FLAGS
    )
    try:
        metadata = os.fstat(descriptor)
        _validate_private_file_metadata(metadata, _REPAIR_RECEIPT_KEY_FILE)
        if metadata.st_size != _REPAIR_RECEIPT_KEY_BYTES:
            raise ValueError("Repair receipt signing key has an invalid size.")
        if os.name != "nt":
            chmod_descriptor(descriptor, 0o600)
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            key = handle.read(_REPAIR_RECEIPT_KEY_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(key) != _REPAIR_RECEIPT_KEY_BYTES:
        raise ValueError("Repair receipt signing key has an invalid size.")
    return key


def _write_receipt_key_temp(
    directory: _RepairDirectoryHandle,
    candidate: bytes,
) -> tuple[int, int]:
    descriptor = _open_private_file_at(
        directory,
        _REPAIR_RECEIPT_KEY_TEMP_FILE,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        # os.write uses the CRT descriptor mode on Windows.  Without O_BINARY,
        # random key bytes containing LF are expanded to CRLF and no longer
        # have the required fixed length.
        | getattr(os, "O_BINARY", 0),
        0o600,
    )
    identity: tuple[int, int] | None = None
    failed = False
    try:
        metadata = os.fstat(descriptor)
        _validate_private_file_metadata(metadata, _REPAIR_RECEIPT_KEY_TEMP_FILE)
        identity = metadata.st_dev, metadata.st_ino
        if os.name != "nt":
            chmod_descriptor(descriptor, 0o600)
        _write_receipt_key_bytes(descriptor, candidate)
        _sync_receipt_key_file(descriptor)
        metadata = os.fstat(descriptor)
        if metadata.st_size != _REPAIR_RECEIPT_KEY_BYTES:
            raise ValueError("Temporary repair receipt signing key has an invalid size.")
        return metadata.st_dev, metadata.st_ino
    except BaseException:
        failed = True
        raise
    finally:
        try:
            os.close(descriptor)
        except BaseException:
            # A close failure is authoritative only after an otherwise
            # successful write.  During failure recovery, retain the original
            # write/sync exception and let the next locked open recover any
            # identity-validated orphan.
            if not failed:
                raise
        if failed and identity is not None:
            try:
                _remove_receipt_key_temp(
                    directory,
                    expected_identity=identity,
                    missing_ok=True,
                )
                _sync_receipt_key_directory(directory)
            except BaseException:
                # Recovery on the next locked open validates the orphan before
                # removing it; never mask the original write/sync failure.
                pass


def _write_receipt_key_bytes(descriptor: int, candidate: bytes) -> None:
    remaining = memoryview(candidate)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise OSError("Repair receipt signing key write made no progress.")
        remaining = remaining[written:]


def _sync_receipt_key_file(descriptor: int) -> None:
    os.fsync(descriptor)


def _sync_receipt_key_directory(directory: _RepairDirectoryHandle) -> None:
    if directory.descriptor is not None:
        os.fsync(directory.descriptor)
    else:
        # CPython cannot open a Windows directory as a file descriptor.  File
        # contents are flushed before publication; revalidate the entire
        # checked path in lieu of a directory fsync unavailable through os.
        _validate_repair_directory_handle(directory)


def _publish_receipt_key_temp(
    directory: _RepairDirectoryHandle,
    *,
    expected_identity: tuple[int, int],
) -> None:
    metadata = _stat_private_at(directory, _REPAIR_RECEIPT_KEY_TEMP_FILE)
    _validate_receipt_key_temp_metadata(metadata, expected_links=1)
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        raise ValueError("Temporary repair receipt signing key identity changed.")
    if metadata.st_size != _REPAIR_RECEIPT_KEY_BYTES:
        raise ValueError("Temporary repair receipt signing key has an invalid size.")
    _link_private_at(
        directory,
        _REPAIR_RECEIPT_KEY_TEMP_FILE,
        _REPAIR_RECEIPT_KEY_FILE,
    )


def _remove_receipt_key_temp(
    directory: _RepairDirectoryHandle,
    *,
    expected_identity: tuple[int, int],
    missing_ok: bool = False,
) -> None:
    try:
        metadata = _stat_private_at(directory, _REPAIR_RECEIPT_KEY_TEMP_FILE)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    if (metadata.st_dev, metadata.st_ino) != expected_identity:
        raise ValueError("Temporary repair receipt signing key identity changed.")
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink not in {1, 2}:
        raise ValueError("Temporary repair receipt signing key has unsafe link metadata.")
    _require_current_owner(metadata, _REPAIR_RECEIPT_KEY_TEMP_FILE)
    _unlink_private_at(directory, _REPAIR_RECEIPT_KEY_TEMP_FILE)


def _recover_receipt_key_temp(directory: _RepairDirectoryHandle) -> None:
    try:
        temp_metadata = _stat_private_at(directory, _REPAIR_RECEIPT_KEY_TEMP_FILE)
    except FileNotFoundError:
        return
    try:
        final_metadata = _stat_private_at(directory, _REPAIR_RECEIPT_KEY_FILE)
    except FileNotFoundError:
        final_metadata = None

    same_inode = final_metadata is not None and (
        temp_metadata.st_dev,
        temp_metadata.st_ino,
    ) == (final_metadata.st_dev, final_metadata.st_ino)
    _validate_receipt_key_temp_metadata(
        temp_metadata,
        expected_links=2 if same_inode else 1,
    )
    if same_inode:
        if final_metadata is None:
            raise RuntimeError("Published repair receipt key metadata disappeared.")
        _validate_receipt_key_temp_metadata(final_metadata, expected_links=2)
        if temp_metadata.st_size != _REPAIR_RECEIPT_KEY_BYTES:
            raise ValueError("Published repair receipt signing key has an invalid size.")
    _unlink_private_at(directory, _REPAIR_RECEIPT_KEY_TEMP_FILE)
    _sync_receipt_key_directory(directory)


def _validate_receipt_key_temp_metadata(
    metadata: os.stat_result,
    *,
    expected_links: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != expected_links:
        raise ValueError("Temporary repair receipt signing key has unsafe link metadata.")
    _require_current_owner(metadata, _REPAIR_RECEIPT_KEY_TEMP_FILE)
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError("Temporary repair receipt signing key is not owner-only.")


@contextmanager
def _receipt_key_lock(workspace: Path) -> Iterator[None]:
    """Serialize signing-key publication and reads across threads/processes."""

    with _repair_directory(workspace, create=True) as directory:
        descriptor = _open_private_file_at(
            directory,
            _REPAIR_RECEIPT_KEY_LOCK_FILE,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            _validate_private_file_metadata(
                os.fstat(descriptor),
                _REPAIR_RECEIPT_KEY_LOCK_FILE,
            )
            if os.name != "nt":
                chmod_descriptor(descriptor, 0o600)
            with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
                descriptor = -1
                lock_exclusive(handle)
                try:
                    yield
                finally:
                    unlock(handle)
        finally:
            if descriptor >= 0:
                os.close(descriptor)


@contextmanager
def _repair_directory(
    workspace: Path,
    *,
    collection: str | None = None,
    create: bool,
) -> Iterator[_RepairDirectoryHandle]:
    """Open ``.nest`` through the strongest safe primitive on this platform."""

    root = require_git_root(workspace)
    if _uses_windows_path_fallback():
        root_metadata = root.lstat()
        _validate_private_directory_metadata(root_metadata, str(root))
        root_handle = _RepairDirectoryHandle(
            path=root,
            descriptor=None,
            path_identities=((root, root_metadata),),
        )
        nest_handle = _open_private_directory_path(
            root_handle,
            _REPAIR_ARTIFACT_ROOT.name,
            create=create,
        )
        selected = nest_handle
        if collection is not None:
            selected = _open_private_directory_path(
                nest_handle,
                collection,
                create=create,
            )
        try:
            yield selected
        finally:
            _validate_repair_directory_handle(selected)
        return

    _require_posix_repair_dirfd_support()
    root_descriptor = os.open(root, _DIRECTORY_FLAGS)
    nest_descriptor = -1
    collection_descriptor = -1
    try:
        nest_descriptor = _open_private_directory_at(
            root_descriptor,
            _REPAIR_ARTIFACT_ROOT.name,
            create=create,
        )
        selected_descriptor = nest_descriptor
        if collection is not None:
            collection_descriptor = _open_private_directory_at(
                nest_descriptor,
                collection,
                create=create,
            )
            selected_descriptor = collection_descriptor
        selected_path = root / _REPAIR_ARTIFACT_ROOT
        if collection is not None:
            selected_path /= collection
        yield _RepairDirectoryHandle(
            path=selected_path,
            descriptor=selected_descriptor,
        )
    finally:
        if collection_descriptor >= 0:
            os.close(collection_descriptor)
        if nest_descriptor >= 0:
            os.close(nest_descriptor)
        os.close(root_descriptor)


def _uses_windows_path_fallback() -> bool:
    return os.name == "nt"


def _require_posix_repair_dirfd_support() -> None:
    required_flags = (
        _optional_os_flag("O_DIRECTORY"),
        _optional_os_flag("O_NOFOLLOW"),
    )
    if (
        any(value is None for value in required_flags)
        or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd
        or os.unlink not in os.supports_dir_fd
    ):
        raise RuntimeError("secure_repair_dirfd_platform_unsupported")


def _optional_os_flag(name: str) -> int | None:
    value: object = getattr(os, name, None)
    return value if isinstance(value, int) and value != 0 else None


def _open_private_directory_path(
    parent: _RepairDirectoryHandle,
    name: str,
    *,
    create: bool,
) -> _RepairDirectoryHandle:
    _validate_repair_directory_handle(parent)
    path = parent.path / name
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    metadata = path.lstat()
    _validate_private_directory_metadata(metadata, name)
    if os.name != "nt":
        path.chmod(0o700)
    handle = _RepairDirectoryHandle(
        path=path,
        descriptor=None,
        path_identities=parent.path_identities + ((path, metadata),),
    )
    _validate_repair_directory_handle(handle)
    return handle


def _validate_private_directory_metadata(metadata: os.stat_result, name: str) -> None:
    if _metadata_is_reparse_point(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError(f"Repair artifact component is not a real directory: {name}")
    _require_current_owner(metadata, name)


def _validate_repair_directory_handle(directory: _RepairDirectoryHandle) -> None:
    if directory.descriptor is not None:
        return
    for path, expected in directory.path_identities:
        try:
            current = path.lstat()
        except OSError as exc:
            raise ValueError(f"Repair artifact directory changed: {path.name}") from exc
        _validate_private_directory_metadata(current, path.name or str(path))
        if not _same_file_identity(expected, current):
            raise ValueError(f"Repair artifact directory changed: {path.name}")


def _open_private_file_at(
    directory: _RepairDirectoryHandle,
    name: str,
    flags: int,
    mode: int = 0o777,
) -> int:
    if directory.descriptor is not None:
        return os.open(name, flags, mode, dir_fd=directory.descriptor)

    _validate_repair_directory_handle(directory)
    path = directory.path / name
    before = _lstat_optional(path)
    if before is not None and _metadata_is_reparse_point(before):
        raise ValueError(f"Repair artifact must not be a reparse point: {name}")
    descriptor = os.open(path, flags | getattr(os, "O_NOINHERIT", 0), mode)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if _metadata_is_reparse_point(current):
            raise ValueError(f"Repair artifact must not be a reparse point: {name}")
        if not _same_file_identity(opened, current):
            raise ValueError(f"Repair artifact identity changed while opening: {name}")
        if before is not None and not _same_file_identity(before, opened):
            raise ValueError(f"Repair artifact identity changed while opening: {name}")
        _validate_repair_directory_handle(directory)
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _stat_private_at(
    directory: _RepairDirectoryHandle,
    name: str,
) -> os.stat_result:
    if directory.descriptor is not None:
        return os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    _validate_repair_directory_handle(directory)
    metadata = (directory.path / name).lstat()
    if _metadata_is_reparse_point(metadata):
        raise ValueError(f"Repair artifact must not be a reparse point: {name}")
    _validate_repair_directory_handle(directory)
    return metadata


def _replace_private_at(
    directory: _RepairDirectoryHandle,
    source: str,
    destination: str,
) -> None:
    if directory.descriptor is not None:
        os.replace(
            source,
            destination,
            src_dir_fd=directory.descriptor,
            dst_dir_fd=directory.descriptor,
        )
        return
    source_metadata = _stat_private_at(directory, source)
    destination_path = directory.path / destination
    destination_metadata = _lstat_optional(destination_path)
    if destination_metadata is not None and _metadata_is_reparse_point(
        destination_metadata
    ):
        raise ValueError(
            f"Repair artifact must not be a reparse point: {destination}"
        )
    os.replace(directory.path / source, destination_path)
    published = _stat_private_at(directory, destination)
    if not _same_file_identity(source_metadata, published):
        raise ValueError(f"Repair artifact identity changed while publishing: {destination}")


def _link_private_at(
    directory: _RepairDirectoryHandle,
    source: str,
    destination: str,
) -> None:
    if directory.descriptor is not None:
        os.link(
            source,
            destination,
            src_dir_fd=directory.descriptor,
            dst_dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        return
    source_metadata = _stat_private_at(directory, source)
    destination_path = directory.path / destination
    if _lstat_optional(destination_path) is not None:
        raise FileExistsError(destination_path)
    os.link(directory.path / source, destination_path, follow_symlinks=False)
    linked = _stat_private_at(directory, destination)
    current_source = _stat_private_at(directory, source)
    if not (
        _same_file_identity(source_metadata, linked)
        and _same_file_identity(linked, current_source)
    ):
        raise ValueError(f"Repair artifact identity changed while publishing: {destination}")


def _unlink_private_at(directory: _RepairDirectoryHandle, name: str) -> None:
    if directory.descriptor is not None:
        os.unlink(name, dir_fd=directory.descriptor)
        return
    _stat_private_at(directory, name)
    (directory.path / name).unlink()
    _validate_repair_directory_handle(directory)


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _metadata_is_reparse_point(metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode):
        return True
    attributes = getattr(metadata, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _same_file_identity(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (
        stat.S_IFMT(expected.st_mode) == stat.S_IFMT(actual.st_mode)
        and expected.st_dev == actual.st_dev
        and expected.st_ino == actual.st_ino
    )


def _open_private_directory_at(parent_descriptor: int, name: str, *, create: bool) -> int:
    if create:
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            pass
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
    except FileNotFoundError:
        raise
    try:
        metadata = os.fstat(descriptor)
        _validate_private_directory_metadata(metadata, name)
        if os.name != "nt":
            chmod_descriptor(descriptor, 0o700)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _validate_private_file_metadata(metadata: os.stat_result, name: str) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValueError(f"Repair artifact must be a single-link regular file: {name}")
    _require_current_owner(metadata, name)


def _require_current_owner(metadata: os.stat_result, name: str) -> None:
    if os.name == "nt":
        return
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and metadata.st_uid != geteuid():
        raise PermissionError(f"Repair artifacts must be owned by the current user: {name}")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _git_text(workspace: Path, arguments: list[str]) -> str:
    return _git_bytes(workspace, arguments).decode("utf-8", errors="surrogateescape").strip()


def _git_bytes(workspace: Path, arguments: list[str]) -> bytes:
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        completed = subprocess.run(  # noqa: S603 - fixed git executable and structured argv  # nosec
            hardened_readonly_git_command(arguments, workspace=workspace),
            cwd=workspace,
            env=hardened_readonly_git_environment(),
            stdin=subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            timeout=30,
            check=False,
        )
        stdout_size = stdout_file.tell()
        stderr_size = stderr_file.tell()
        if stdout_size > _MAX_GIT_OUTPUT_BYTES or stderr_size > _MAX_GIT_OUTPUT_BYTES:
            raise ValueError("Git output exceeded the bounded repair fingerprint budget.")
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr_bytes = stderr_file.read()
    if completed.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        raise RuntimeError(
            f"git command failed ({completed.returncode}): git {' '.join(arguments)}\n{stderr}"
        )
    return stdout


def hardened_readonly_git_command(
    arguments: list[str],
    *,
    workspace: Path | None = None,
) -> list[str]:
    """Bind host Git probes to a resolved binary and inert execution config."""

    command = [
        trusted_git_executable(),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "diff.external=",
        "-c",
        "filter.lfs.clean=",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.process=",
        "-c",
        "filter.lfs.required=false",
        "-c",
        "credential.helper=",
        "-c",
        "protocol.ext.allow=never",
    ]
    if workspace is not None:
        for driver in _configured_filter_drivers(workspace):
            command.extend(
                [
                    "-c",
                    f"filter.{driver}.clean=",
                    "-c",
                    f"filter.{driver}.smudge=",
                    "-c",
                    f"filter.{driver}.process=",
                    "-c",
                    f"filter.{driver}.required=false",
                ]
            )
    command.extend(arguments)
    return command


def hardened_readonly_git_environment() -> dict[str, str]:
    """Remove credentials and every inherited Git routing/config override."""

    environment = {
        name: value
        for name, value in sanitized_subprocess_environment().items()
        if not name.upper().startswith("GIT_")
    }
    environment.update(
        {
            # User/system configuration is excluded. Repository-local filter
            # names are discovered separately and explicitly neutralized on the
            # command line because generic Git commands still consult local
            # configuration even when GIT_CONFIG names another file.
            "GIT_CONFIG": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
        }
    )
    return environment


def _configured_filter_drivers(workspace: Path) -> tuple[str, ...]:
    """Return bounded local filter names without evaluating filter commands."""

    requested = Path(workspace)
    if not requested.exists() or not requested.is_dir():
        return ()
    environment = hardened_readonly_git_environment()
    # `git config` treats GIT_CONFIG specially; remove it so local/worktree
    # config and their includes can be enumerated before generic commands run.
    environment.pop("GIT_CONFIG", None)
    command = [
        trusted_git_executable(),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "config",
        "--includes",
        "--name-only",
        "-z",
        "--get-regexp",
        r"^filter\..*\.(clean|smudge|process|required)$",
    ]
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        try:
            completed = subprocess.run(  # noqa: S603 - trusted Git config query  # nosec
                command,
                cwd=requested,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Timed out while neutralizing repository Git filters.") from exc
        stdout_size = stdout_file.tell()
        stderr_size = stderr_file.tell()
        if (
            stdout_size > _MAX_GIT_FILTER_CONFIG_BYTES
            or stderr_size > _MAX_GIT_FILTER_CONFIG_BYTES
        ):
            raise ValueError("Repository Git filter configuration exceeds the safety budget.")
        stdout_file.seek(0)
        raw = stdout_file.read()
        stderr_file.seek(0)
        error = stderr_file.read().decode("utf-8", errors="replace").strip()
    # Git config returns 1 when no keys match and 128 outside a repository.
    if completed.returncode == 1:
        return ()
    if completed.returncode != 0:
        if "not a git repository" in error.casefold():
            return ()
        raise RuntimeError("Unable to enumerate repository Git filters safely.")

    drivers: set[str] = set()
    pattern = re.compile(r"^filter\.(.+)\.(clean|smudge|process|required)$", re.IGNORECASE)
    for encoded_key in raw.split(b"\0"):
        if not encoded_key:
            continue
        try:
            key = encoded_key.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise ValueError("Repository Git filter name is not valid UTF-8.") from exc
        match = pattern.fullmatch(key)
        if match is None:
            raise ValueError("Repository Git filter configuration is malformed.")
        driver = match.group(1)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", driver):
            raise ValueError("Repository Git filter name is outside the safe grammar.")
        drivers.add(driver)
        if len(drivers) > _MAX_GIT_FILTER_DRIVERS:
            raise ValueError("Repository defines too many Git filter drivers.")
    return tuple(sorted(drivers, key=str.casefold))


def trusted_git_executable() -> str:
    """Resolve Git once per probe without allowing repository-controlled lookup."""

    candidates = [Path("/usr/bin/git"), Path("/bin/git")]
    discovered = shutil.which("git")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
            metadata = resolved.stat()
        except OSError:
            continue
        if not resolved.is_file():
            continue
        if os.name != "nt" and metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            continue
        return str(resolved)
    raise RuntimeError("A trusted, non-group-writable Git executable is required.")


def _git_z_paths(workspace: Path, arguments: list[str]) -> list[str]:
    raw = _git_bytes(workspace, arguments)
    if len(raw) > _MAX_CHANGED_PATH_BYTES:
        raise ValueError(
            f"Repair path manifest exceeds {_MAX_CHANGED_PATH_BYTES} bytes; split the repair."
        )
    paths = [os.fsdecode(item) for item in raw.split(b"\0") if item]
    for path in paths:
        _validate_relative_git_path(path)
    return paths


def _validate_relative_git_path(path: str) -> None:
    pure = PurePosixPath(path)
    if not path or pure.is_absolute() or ".." in pure.parts or "\x00" in path:
        raise ValueError(f"Unsafe repair path reported by git: {path!r}")


def _is_private_repair_path(path: str) -> bool:
    exact = {
        ".nest/repair-actions.lock",
        f".nest/{_REPAIR_RECEIPT_KEY_FILE}",
        f".nest/{_REPAIR_RECEIPT_KEY_LOCK_FILE}",
        f".nest/{_REPAIR_RECEIPT_KEY_TEMP_FILE}",
    }
    prefixes = (
        ".nest/repair_validations/",
        ".nest/repair_reviews/",
        ".nest/repair_rollbacks/",
        ".nest/repair_rollback_journals/",
        ".nest/repair_rollback_quarantine/",
        ".nest/repair_indexes/",
    )
    return path in exact or path.startswith(prefixes)


def _changed_path_manifest(
    workspace: Path,
    relative_path: str,
    *,
    reject_symlink: bool,
    max_bytes: int,
    deadline: float,
) -> dict[str, Any]:
    _validate_relative_git_path(relative_path)
    candidate = workspace / Path(relative_path)
    root = workspace.resolve()
    _reject_symlink_path_components(root, relative_path, include_leaf=False)
    resolved_parent = candidate.parent.resolve()
    if resolved_parent != root and root not in resolved_parent.parents:
        raise ValueError(f"Untracked repair path escapes the workspace: {relative_path}")
    try:
        before = os.lstat(candidate)
    except FileNotFoundError:
        return {"path": relative_path, "type": "deleted"}
    if stat.S_ISLNK(before.st_mode):
        if reject_symlink:
            raise ValueError(
                f"Untracked symbolic links are not accepted in repairs: {relative_path}"
            )
        link_target = os.readlink(candidate)
        target_bytes = os.fsencode(link_target)
        if len(target_bytes) > max_bytes:
            raise ValueError(f"Repair content exceeds its aggregate byte budget: {relative_path}")
        return {
            "path": relative_path,
            "type": "symlink",
            "mode": stat.S_IMODE(before.st_mode),
            "size": len(target_bytes),
            "sha256": hashlib.sha256(target_bytes).hexdigest(),
        }
    if not stat.S_ISREG(before.st_mode):
        raise ValueError(f"Changed repair paths must be regular files: {relative_path}")
    if before.st_size > _MAX_UNTRACKED_FILE_BYTES:
        raise ValueError(
            f"Changed repair file exceeds {_MAX_UNTRACKED_FILE_BYTES} bytes: {relative_path}"
        )
    if before.st_size > max_bytes:
        raise ValueError(f"Repair content exceeds its aggregate byte budget: {relative_path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(candidate, flags)
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode):
            raise ValueError(f"Changed repair path changed type: {relative_path}")
        if not os.path.samestat(before, opened_before):
            raise ValueError(f"Changed repair path changed during fingerprinting: {relative_path}")
        digest = hashlib.sha256()
        size = 0
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("Repair fingerprint exceeded its bounded time budget.")
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise ValueError(
                    f"Repair content exceeds its aggregate byte budget: {relative_path}"
                )
            digest.update(chunk)
        opened_after = os.fstat(descriptor)
        after = os.lstat(candidate)
        if (
            not os.path.samestat(opened_before, opened_after)
            or not os.path.samestat(opened_after, after)
            or _mutable_stat_fields(opened_before) != _mutable_stat_fields(opened_after)
            or _mutable_stat_fields(opened_after) != _mutable_stat_fields(after)
        ):
            raise ValueError(f"Changed repair path changed during fingerprinting: {relative_path}")
    finally:
        os.close(descriptor)
    return {
        "path": relative_path,
        "type": "regular",
        "mode": stat.S_IMODE(before.st_mode),
        "size": size,
        "sha256": digest.hexdigest(),
    }


def _mutable_stat_fields(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        stat.S_IMODE(metadata.st_mode),
    )


def _reject_symlink_path_components(root: Path, relative_path: str, *, include_leaf: bool) -> None:
    parts = Path(relative_path).parts
    limit = len(parts) if include_leaf else max(0, len(parts) - 1)
    current = root
    for part in parts[:limit]:
        current = current / part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"Repair paths must not traverse symbolic links: {relative_path}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError(f"Repair path parent is not a directory: {relative_path}")


def _validate_artifact_component(value: str, *, expected_prefix: str) -> None:
    if (
        not value.startswith(expected_prefix)
        or not value.replace("_", "").replace("-", "").isalnum()
    ):
        raise ValueError(f"Invalid repair artifact identifier: {value}")
