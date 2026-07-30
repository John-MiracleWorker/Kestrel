#!/usr/bin/env python3
"""Verify a release or explicitly developer Kestrel Desktop resource manifest."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

if not __package__:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

MANIFEST_SCHEMA = "kestrel.desktop.resources.v1"
MANIFEST_NAME = "kestrel-resource-manifest.json"
SIGNATURE_NAME = "kestrel-resource-manifest.sig"
SBOM_NAME = "sbom.cdx.json"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_SIGNATURE_BYTES = 4 * 1024
_MAX_IDENTITY_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
_PLATFORMS = frozenset({"darwin", "linux", "win32"})
_ARCHITECTURES = frozenset({"arm64", "x64"})
_MANIFEST_KEYS = frozenset(
    {
        "schema",
        "build_mode",
        "key_id",
        "source_commit",
        "app_version",
        "platform",
        "architecture",
        "python_lock_sha256",
        "desktop_npm_lock_sha256",
        "web_npm_lock_sha256",
        "sbom_sha256",
        "files",
    }
)


@dataclass(frozen=True)
class DesktopManifestIdentity:
    build_mode: str
    key_id: str
    source_commit: str
    app_version: str
    platform: str
    architecture: str
    python_lock_sha256: str
    desktop_npm_lock_sha256: str
    web_npm_lock_sha256: str
    sbom_sha256: str

    def validate(self, *, allow_empty_sbom: bool = False) -> None:
        if (self.build_mode, self.key_id) not in {
            ("developer", "developer"),
            ("release", "release"),
        }:
            raise ValueError("desktop manifest mode and key must match exactly")
        if not _SOURCE_COMMIT_RE.fullmatch(self.source_commit):
            raise ValueError("desktop manifest source commit is invalid")
        if not _VERSION_RE.fullmatch(self.app_version):
            raise ValueError("desktop manifest app version is invalid")
        if self.platform not in _PLATFORMS:
            raise ValueError("desktop manifest platform is invalid")
        if self.architecture not in _ARCHITECTURES:
            raise ValueError("desktop manifest architecture is invalid")
        for name in (
            "python_lock_sha256",
            "desktop_npm_lock_sha256",
            "web_npm_lock_sha256",
        ):
            if not _SHA256_RE.fullmatch(getattr(self, name)):
                raise ValueError(f"desktop manifest {name} is invalid")
        if not (
            _SHA256_RE.fullmatch(self.sbom_sha256)
            or (allow_empty_sbom and self.sbom_sha256 == "")
        ):
            raise ValueError("desktop manifest sbom_sha256 is invalid")

    @classmethod
    def from_mapping(
        cls,
        raw: Mapping[str, object],
        *,
        allow_empty_sbom: bool = False,
    ) -> DesktopManifestIdentity:
        expected = set(cls.__dataclass_fields__)
        if set(raw) != expected:
            raise ValueError("desktop manifest identity fields mismatch")
        if not all(isinstance(raw[name], str) for name in expected):
            raise ValueError("desktop manifest identity values must be strings")
        identity = cls(**{name: str(raw[name]) for name in expected})
        identity.validate(allow_empty_sbom=allow_empty_sbom)
        return identity


def read_desktop_regular_bounded(path: Path, maximum: int, label: str) -> bytes:
    before = os.lstat(path)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError(f"{label} must be a unique regular file")
    if before.st_size > maximum:
        raise ValueError(f"{label} exceeds its size limit")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size > maximum
        ):
            raise ValueError(f"{label} changed during open")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"{label} changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or after.st_dev != opened.st_dev
            or after.st_ino != opened.st_ino
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError(f"{label} changed during read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _parse_manifest(bytes_value: bytes) -> dict[str, object]:
    try:
        raw: Any = json.loads(bytes_value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("desktop resource manifest is invalid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != _MANIFEST_KEYS:
        raise ValueError("desktop resource manifest fields mismatch")
    from scripts.generate_desktop_resource_manifest import canonical_manifest_bytes

    if canonical_manifest_bytes(raw) != bytes_value:
        raise ValueError("desktop resource manifest is not canonical")
    if raw["schema"] != MANIFEST_SCHEMA:
        raise ValueError("desktop resource manifest schema is invalid")
    files = raw["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("desktop resource manifest files are invalid")
    for relative, entry in files.items():
        if not isinstance(relative, str) or not isinstance(entry, dict):
            raise ValueError("desktop resource manifest file entry is invalid")
        if set(entry) != {"size", "sha256"}:
            raise ValueError("desktop resource manifest file entry is invalid")
        if (
            isinstance(entry["size"], bool)
            or not isinstance(entry["size"], int)
            or entry["size"] < 0
            or not isinstance(entry["sha256"], str)
            or not _SHA256_RE.fullmatch(entry["sha256"])
        ):
            raise ValueError("desktop resource manifest file entry is invalid")
    return raw


def _identity_from_manifest(manifest: Mapping[str, object]) -> DesktopManifestIdentity:
    return DesktopManifestIdentity.from_mapping(
        {
            name: manifest[name]
            for name in DesktopManifestIdentity.__dataclass_fields__
        }
    )


def _verify(
    staged_root: Path,
    *,
    expected_mode: Literal["developer", "release"],
    expected_identity: DesktopManifestIdentity,
    trusted_public_keys: Mapping[str, Ed25519PublicKey],
) -> dict[str, object]:
    expected_identity.validate()
    if expected_identity.build_mode != expected_mode:
        raise ValueError(f"expected packaged metadata is not a {expected_mode} manifest")
    root = Path(staged_root)
    root_metadata = os.lstat(root)
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError("desktop staged root is untrusted")
    manifest_path = root / MANIFEST_NAME
    signature_path = root / SIGNATURE_NAME
    manifest_bytes = read_desktop_regular_bounded(
        manifest_path,
        _MAX_MANIFEST_BYTES,
        "desktop resource manifest",
    )
    signature = read_desktop_regular_bounded(
        signature_path,
        _MAX_SIGNATURE_BYTES,
        "desktop resource signature",
    )
    if len(signature) != 64:
        raise ValueError("desktop resource signature has an invalid size")
    manifest = _parse_manifest(manifest_bytes)
    identity = _identity_from_manifest(manifest)
    if identity.build_mode != expected_mode or identity.key_id != expected_mode:
        raise ValueError(f"candidate is not a {expected_mode} manifest")
    if asdict(identity) != asdict(expected_identity):
        raise ValueError("desktop packaged metadata mismatch")
    public_key = trusted_public_keys.get(identity.key_id)
    if not isinstance(public_key, Ed25519PublicKey):
        raise ValueError("desktop manifest signing key is untrusted")
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise ValueError("desktop resource signature is invalid") from exc

    from scripts.generate_desktop_resource_manifest import inventory_payload_files

    actual_files = inventory_payload_files(root)
    declared_files = manifest["files"]
    if not isinstance(declared_files, dict):
        raise ValueError("desktop resource manifest files are invalid")
    if set(actual_files) != set(declared_files):
        raise ValueError("desktop payload coverage mismatch")
    for relative, actual in actual_files.items():
        declared = declared_files[relative]
        if not isinstance(declared, dict):
            raise ValueError("desktop resource manifest file entry is invalid")
        if actual["size"] != declared["size"]:
            raise ValueError(f"desktop resource size mismatch: {relative}")
        if actual["sha256"] != declared["sha256"]:
            raise ValueError(f"desktop resource digest mismatch: {relative}")
    sbom = declared_files.get(SBOM_NAME)
    if not isinstance(sbom, dict) or sbom.get("sha256") != identity.sbom_sha256:
        raise ValueError("desktop SBOM digest mismatch")
    return manifest


def verify_release_resource_manifest(
    staged_root: Path,
    *,
    expected_identity: DesktopManifestIdentity,
    trusted_public_keys: Mapping[str, Ed25519PublicKey],
) -> dict[str, object]:
    return _verify(
        staged_root,
        expected_mode="release",
        expected_identity=expected_identity,
        trusted_public_keys=trusted_public_keys,
    )


def verify_developer_resource_manifest(
    staged_root: Path,
    *,
    expected_identity: DesktopManifestIdentity,
    trusted_public_keys: Mapping[str, Ed25519PublicKey],
) -> dict[str, object]:
    return _verify(
        staged_root,
        expected_mode="developer",
        expected_identity=expected_identity,
        trusted_public_keys=trusted_public_keys,
    )


def load_desktop_public_key(path: Path) -> Ed25519PublicKey:
    raw = read_desktop_regular_bounded(path, 16 * 1024, "desktop public key")
    try:
        key = serialization.load_pem_public_key(raw)
    except ValueError as exc:
        raise ValueError("desktop public key is invalid") from exc
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("desktop public key is not Ed25519")
    return key


def load_desktop_manifest_identity(path: Path) -> DesktopManifestIdentity:
    try:
        raw: Any = json.loads(
            read_desktop_regular_bounded(
                path,
                _MAX_IDENTITY_BYTES,
                "desktop packaged identity",
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("expected identity must be valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("expected identity must be a JSON object")
    return DesktopManifestIdentity.from_mapping(raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="mode", required=True)
    for mode in ("release", "developer"):
        command = subparsers.add_parser(mode)
        command.add_argument("staged_root", type=Path)
        command.add_argument("--identity", required=True, type=Path)
        command.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args()
    try:
        identity = load_desktop_manifest_identity(args.identity)
        public_key = load_desktop_public_key(args.public_key)
        verifier = (
            verify_release_resource_manifest
            if args.mode == "release"
            else verify_developer_resource_manifest
        )
        verifier(
            args.staged_root,
            expected_identity=identity,
            trusted_public_keys={identity.key_id: public_key},
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
