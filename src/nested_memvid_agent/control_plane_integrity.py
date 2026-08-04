"""Owner-only control-plane key material for authenticating routing receipts.

Adaptive Flock plan, Task 3.  Flock qualification receipts are authenticated
with HMAC-SHA256 envelopes whose key lives at
``<state-directory>/.routing-integrity.key`` beside the SQLite state database.

The atomic publication/crash-recovery pattern mirrors the memory-layer
``.validation-integrity.key`` handling, but this module is intentionally
self-contained: it must not import memory-layer internals, and the key must
never be stored in SQLite, logs, API responses, events, evidence, or Memvid.
Only the derived 16-hex-digit key id (a SHA-256 prefix of the key) ever
leaves this module inside an envelope.

Deviation from the memory-layer pattern, required by the plan: the routing
key file is base64-encoded (the memory key uses hex) so malformed base64 and
wrong-length key material are refused explicitly.  Group/world-readable key
files are refused rather than silently repaired, and an ambiguous temp/final
key state fails closed instead of discarding the temp file.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import secrets
import stat
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Any

from .file_lock import lock_exclusive, unlock
from .private_artifacts import (
    ensure_private_directory,
    open_private_file_descriptor,
    read_private_text,
    write_private_text_exclusive,
)

ROUTING_INTEGRITY_KEY_NAME = ".routing-integrity.key"
AUTHENTICATED_PAYLOAD_ALGORITHM = "hmac-sha256"
AUTHENTICATED_PAYLOAD_SCHEMA = "kestrel.control_plane_authentication.v1"
ROUTING_INTEGRITY_KEY_OK = "ok"
ROUTING_INTEGRITY_KEY_MISSING_OR_MISMATCHED = "missing_or_mismatched"

_ROUTING_INTEGRITY_KEY_BYTES = 32
_ROUTING_INTEGRITY_KEY_ENCODED_SIZE = 44  # base64 of 32 bytes, padding included
_ROUTING_INTEGRITY_KEY_LOCK_NAME = ".routing-integrity.lock"
_ROUTING_INTEGRITY_KEY_TEMP_NAME = f"{ROUTING_INTEGRITY_KEY_NAME}.tmp"


class RoutingIntegrityError(ValueError):
    """Routing integrity key material is missing, unsafe, or ambiguous."""


class AuthenticatedPayload(dict[str, Any]):
    """HMAC-SHA256 envelope over a canonical JSON control-plane payload.

    Keys: ``schema``, ``algorithm``, ``key_id``, ``payload_digest``, ``tag``,
    and ``payload``.  The envelope never contains key material.
    """


def authenticated_payload_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of the canonical payload encoding."""

    return sha256(_canonical_payload_bytes(payload)).hexdigest()


def routing_integrity_key_state(state_dir: Path, *, receipts_present: bool) -> str:
    """Read-only routing key status for desktop recovery.

    Never creates, repairs, or deletes key material: recovery must not mint a
    new key over existing signed receipts.
    """

    key_path = Path(state_dir) / ROUTING_INTEGRITY_KEY_NAME
    try:
        _read_routing_integrity_key(key_path)
    except FileNotFoundError:
        if receipts_present:
            return ROUTING_INTEGRITY_KEY_MISSING_OR_MISMATCHED
        return ROUTING_INTEGRITY_KEY_OK
    except (OSError, ValueError):
        return ROUTING_INTEGRITY_KEY_MISSING_OR_MISMATCHED
    temp_path = Path(state_dir) / _ROUTING_INTEGRITY_KEY_TEMP_NAME
    try:
        temp_metadata = os.lstat(temp_path)
    except FileNotFoundError:
        return ROUTING_INTEGRITY_KEY_OK
    except OSError:
        return ROUTING_INTEGRITY_KEY_MISSING_OR_MISMATCHED
    try:
        final_metadata = os.lstat(key_path)
        if not os.path.samestat(temp_metadata, final_metadata):
            return ROUTING_INTEGRITY_KEY_MISSING_OR_MISMATCHED
    except OSError:
        return ROUTING_INTEGRITY_KEY_MISSING_OR_MISMATCHED
    return ROUTING_INTEGRITY_KEY_OK


class ControlPlaneIntegrity:
    """Sign and verify control-plane payloads with the owner-only routing key."""

    def __init__(self, state_dir: Path, *, create_if_missing: bool = True) -> None:
        self._state_dir = Path(state_dir)
        self._key = _load_or_create_routing_integrity_key(
            self._state_dir,
            create_if_missing=create_if_missing,
        )

    @property
    def key_id(self) -> str:
        return sha256(self._key).hexdigest()[:16]

    def sign(self, payload: Mapping[str, Any]) -> AuthenticatedPayload:
        if not isinstance(payload, Mapping):
            raise ValueError("authenticated payload must be a mapping")
        canonical = _canonical_payload_bytes(payload)
        return AuthenticatedPayload(
            {
                "schema": AUTHENTICATED_PAYLOAD_SCHEMA,
                "algorithm": AUTHENTICATED_PAYLOAD_ALGORITHM,
                "key_id": self.key_id,
                "payload_digest": sha256(canonical).hexdigest(),
                "tag": hmac.new(self._key, canonical, sha256).hexdigest(),
                "payload": dict(payload),
            }
        )

    def verify(self, envelope: Mapping[str, Any]) -> bool:
        """Total verification: malformed or mismatched envelopes return False."""

        if not isinstance(envelope, Mapping):
            return False
        if envelope.get("schema") != AUTHENTICATED_PAYLOAD_SCHEMA:
            return False
        if envelope.get("algorithm") != AUTHENTICATED_PAYLOAD_ALGORITHM:
            return False
        key_id = envelope.get("key_id")
        payload_digest = envelope.get("payload_digest")
        tag = envelope.get("tag")
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            return False
        if not all(isinstance(value, str) for value in (key_id, payload_digest, tag)):
            return False
        try:
            canonical = _canonical_payload_bytes(payload)
        except (TypeError, ValueError):
            return False
        if not hmac.compare_digest(str(key_id), self.key_id):
            return False
        if not hmac.compare_digest(str(payload_digest), sha256(canonical).hexdigest()):
            return False
        expected_tag = hmac.new(self._key, canonical, sha256).hexdigest()
        return hmac.compare_digest(str(tag), expected_tag)


def _canonical_payload_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _load_or_create_routing_integrity_key(
    state_dir: Path,
    *,
    create_if_missing: bool,
) -> bytes:
    ensure_private_directory(state_dir)
    descriptor = open_private_file_descriptor(state_dir / _ROUTING_INTEGRITY_KEY_LOCK_NAME)
    try:
        with os.fdopen(descriptor, "r+", encoding="utf-8") as handle:
            descriptor = -1
            lock_exclusive(handle)
            try:
                _recover_routing_integrity_key_temp(state_dir)
                return _load_or_create_routing_integrity_key_locked(
                    state_dir,
                    create_if_missing=create_if_missing,
                )
            finally:
                unlock(handle)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _load_or_create_routing_integrity_key_locked(
    state_dir: Path,
    *,
    create_if_missing: bool,
) -> bytes:
    path = state_dir / ROUTING_INTEGRITY_KEY_NAME
    try:
        return _read_routing_integrity_key(path)
    except FileNotFoundError:
        if not create_if_missing:
            raise RoutingIntegrityError(
                "Routing integrity key is missing; refusing to generate new key "
                "material on a read-only path"
            ) from None
    candidate = secrets.token_bytes(_ROUTING_INTEGRITY_KEY_BYTES)
    encoded = base64.b64encode(candidate).decode("ascii")
    try:
        write_private_text_exclusive(path, encoded)
    except FileExistsError:
        # A concurrent owner published first; bind to the published key.
        return _read_routing_integrity_key(path)
    return candidate


def _read_routing_integrity_key(path: Path) -> bytes:
    metadata = os.lstat(path)
    if stat.S_ISLNK(metadata.st_mode):
        raise RoutingIntegrityError(f"Routing integrity key must not be a symbolic link: {path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RoutingIntegrityError(f"Routing integrity key must be a regular file: {path}")
    if metadata.st_nlink != 1:
        raise RoutingIntegrityError(f"Routing integrity key must not be hard-linked: {path}")
    geteuid = getattr(os, "geteuid", None)
    if os.name != "nt" and callable(geteuid) and metadata.st_uid != geteuid():
        raise PermissionError(f"Routing integrity key must be owned by the current user: {path}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError(f"Routing integrity key must not be group/world readable: {path}")
    encoded = read_private_text(path)
    if encoded is None:
        raise RoutingIntegrityError(f"Routing integrity key could not be read: {path}")
    return _decode_routing_integrity_key(encoded)


def _decode_routing_integrity_key(encoded: str) -> bytes:
    try:
        key = base64.b64decode(encoded.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RoutingIntegrityError("Routing integrity key has invalid base64 encoding") from exc
    if len(key) != _ROUTING_INTEGRITY_KEY_BYTES:
        raise RoutingIntegrityError("Routing integrity key has an invalid size")
    return key


def _recover_routing_integrity_key_temp(state_dir: Path) -> None:
    temporary = state_dir / _ROUTING_INTEGRITY_KEY_TEMP_NAME
    final = state_dir / ROUTING_INTEGRITY_KEY_NAME
    try:
        temp_metadata = os.lstat(temporary)
    except FileNotFoundError:
        return
    try:
        final_metadata = os.lstat(final)
    except FileNotFoundError:
        final_metadata = None
    same_inode = final_metadata is not None and os.path.samestat(
        temp_metadata,
        final_metadata,
    )
    _validate_routing_integrity_temp(
        temporary,
        temp_metadata,
        expected_links=2 if same_inode else 1,
    )
    if same_inode:
        if final_metadata is None:
            raise RoutingIntegrityError("Published routing integrity key metadata disappeared")
        _validate_routing_integrity_temp(final, final_metadata, expected_links=2)
        if final_metadata.st_size != _ROUTING_INTEGRITY_KEY_ENCODED_SIZE:
            raise RoutingIntegrityError("Published routing integrity key has an invalid size")
    elif final_metadata is not None:
        raise RoutingIntegrityError(
            "Ambiguous routing integrity key state: temp and final keys differ"
        )
    temporary.unlink()
    _fsync_routing_integrity_directory(state_dir)


def _validate_routing_integrity_temp(
    path: Path,
    metadata: os.stat_result,
    *,
    expected_links: int,
) -> None:
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != expected_links:
        raise RoutingIntegrityError(
            f"Temporary routing integrity key has unsafe link metadata: {path}"
        )
    geteuid = getattr(os, "geteuid", None)
    if os.name != "nt" and callable(geteuid) and metadata.st_uid != geteuid():
        raise PermissionError(f"Temporary routing integrity key has an unsafe owner: {path}")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) & 0o077:
        raise PermissionError(f"Temporary routing integrity key is not owner-only: {path}")


def _fsync_routing_integrity_directory(state_dir: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(
        state_dir,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
