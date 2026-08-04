#!/usr/bin/env python3
"""Generate the complete canonical Kestrel Desktop resource manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import unicodedata
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

if not __package__:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_desktop_resource_manifest import (
    DesktopManifestIdentity,
    read_desktop_regular_bounded,
)

MANIFEST_SCHEMA = "kestrel.desktop.resources.v1"
MANIFEST_NAME = "kestrel-resource-manifest.json"
SIGNATURE_NAME = "kestrel-resource-manifest.sig"
SBOM_NAME = "sbom.cdx.json"
CONTROL_FILES = frozenset({MANIFEST_NAME, SIGNATURE_NAME})
_READ_BYTES = 1024 * 1024
_MAX_IDENTITY_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _canonical_value(value: object) -> object:
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _canonical_value(value[key])
            for key in sorted(value)
        }
    return value


def canonical_manifest_bytes(manifest: dict[str, object]) -> bytes:
    return (
        json.dumps(
            _canonical_value(manifest),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _unsafe_name(name: str) -> bool:
    return (
        not name
        or name in {".", ".."}
        or "\\" in name
        or "/" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    )


def _normalized_relative(parts: tuple[str, ...]) -> str:
    if not parts or any(_unsafe_name(part) for part in parts):
        raise ValueError("staged payload contains an unsafe path")
    relative = PurePosixPath(*parts).as_posix()
    if (
        relative.startswith("/")
        or relative.endswith("/")
        or PurePosixPath(relative).as_posix() != relative
    ):
        raise ValueError("staged payload contains an unsafe path")
    return relative


def _portable_fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _inspect_regular_file(path: Path) -> dict[str, object]:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ValueError(f"staged payload is not a unique regular file: {path}")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            for chunk in iter(lambda: handle.read(_READ_BYTES), b""):
                digest.update(chunk)
        after = os.lstat(path)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError(f"staged payload changed during hashing: {path}")
        return {
            "size": opened.st_size,
            "sha256": digest.hexdigest(),
        }
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_portable_relative_paths(relative_paths: list[str]) -> None:
    casefolded: dict[str, str] = {}
    for relative in relative_paths:
        parts = tuple(PurePosixPath(relative).parts)
        if _normalized_relative(parts) != relative:
            raise ValueError(f"staged payload contains an unsafe path: {relative}")
        for length in range(1, len(parts) + 1):
            prefix = PurePosixPath(*parts[:length]).as_posix()
            folded = _portable_fold(prefix)
            previous = casefolded.get(folded)
            if previous is not None and previous != prefix:
                raise ValueError(
                    f"staged payload contains case-colliding paths: {previous}, {prefix}"
                )
            casefolded[folded] = prefix


def inventory_payload_files(staged_root: Path) -> dict[str, dict[str, object]]:
    root = Path(staged_root)
    metadata = os.lstat(root)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("staged root must be a real directory")
    files: dict[str, dict[str, object]] = {}
    casefolded: dict[str, str] = {}

    def visit(directory: Path, parts: tuple[str, ...]) -> None:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
        for entry in ordered:
            if entry.is_symlink():
                raise ValueError(f"staged payload contains a symlink: {entry.path}")
            relative_parts = (*parts, entry.name)
            relative = _normalized_relative(relative_parts)
            child_metadata = entry.stat(follow_symlinks=False)
            validate_portable_relative_paths([*casefolded.values(), relative])
            casefolded[_portable_fold(relative)] = relative
            if stat.S_ISDIR(child_metadata.st_mode):
                visit(Path(entry.path), relative_parts)
                continue
            if not stat.S_ISREG(child_metadata.st_mode):
                raise ValueError(f"staged payload contains a special file: {relative}")
            if len(relative_parts) == 1 and relative in CONTROL_FILES:
                continue
            files[relative] = _inspect_regular_file(Path(entry.path))
    visit(root, ())
    if not files:
        raise ValueError("staged payload has no regular files")
    return files


def generate_resource_manifest(
    staged_root: Path,
    *,
    identity: DesktopManifestIdentity,
) -> dict[str, object]:
    identity.validate(allow_empty_sbom=True)
    files = inventory_payload_files(staged_root)
    sbom = files.get(SBOM_NAME)
    if sbom is None:
        raise ValueError(f"staged payload is missing {SBOM_NAME}")
    sbom_sha256 = str(sbom["sha256"])
    if identity.sbom_sha256 and identity.sbom_sha256 != sbom_sha256:
        raise ValueError("packaged SBOM digest does not match the staged payload")
    payload = asdict(identity)
    payload["sbom_sha256"] = sbom_sha256
    return {
        "schema": MANIFEST_SCHEMA,
        **payload,
        "files": files,
    }


def _load_identity(path: Path) -> DesktopManifestIdentity:
    try:
        raw: Any = json.loads(
            read_desktop_regular_bounded(
                path,
                _MAX_IDENTITY_BYTES,
                "desktop packaged identity",
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("identity must be valid UTF-8 JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("identity must be a JSON object")
    return DesktopManifestIdentity.from_mapping(raw, allow_empty_sbom=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("staged_root", type=Path)
    parser.add_argument("--identity", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        identity = _load_identity(args.identity)
        manifest = generate_resource_manifest(args.staged_root, identity=identity)
        output = args.output.resolve(strict=False)
        expected = (args.staged_root / MANIFEST_NAME).resolve(strict=False)
        if output != expected:
            raise ValueError(f"manifest output must be {MANIFEST_NAME} in the staged root")
        if output.exists():
            metadata = os.lstat(output)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise ValueError("refusing to replace an untrusted manifest path")
        output.write_bytes(canonical_manifest_bytes(manifest))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
