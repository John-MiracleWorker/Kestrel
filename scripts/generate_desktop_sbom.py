#!/usr/bin/env python3
"""Generate a deterministic desktop CycloneDX SBOM from exact lockfiles."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import quote

SBOM_RECEIPT_SCHEMA = "kestrel.desktop.sbom.v1"
SIDECAR_RECEIPT_SCHEMA = "kestrel.desktop.sidecar-build.v1"
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_RECEIPT_BYTES = 64 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


def _read_regular_bytes(path: Path, *, maximum: int = MAX_INPUT_BYTES) -> bytes:
    candidate = Path(os.path.abspath(path))
    before = os.lstat(candidate)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError(f"input must be a regular non-symlink file: {path}")
    if before.st_size > maximum:
        raise ValueError(f"input exceeds {maximum} bytes: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(candidate, flags)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise ValueError(f"input changed during open: {path}")
        chunks: list[bytes] = []
        remaining = opened.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise ValueError(f"input changed during read: {path}")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.lstat(candidate)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
            or opened.st_mtime_ns != after.st_mtime_ns
        ):
            raise ValueError(f"input changed during read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(_read_regular_bytes(path))


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_read_regular_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON input: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"JSON input must be an object: {path}")
    return value


def _python_components(payload: bytes) -> list[dict[str, Any]]:
    try:
        lock = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("invalid uv.lock TOML") from exc
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError("uv.lock must contain package records")
    components: list[dict[str, Any]] = []
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("uv.lock package record must be an object")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("uv.lock package name is invalid")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"uv.lock version is invalid for {name}")
        normalized = re.sub(r"[-_.]+", "-", name.strip().lower())
        purl = f"pkg:pypi/{quote(normalized, safe='')}@{quote(version.strip(), safe='')}"
        components.append(
            {
                "bom-ref": purl,
                "name": normalized,
                "purl": purl,
                "type": "library",
                "version": version.strip(),
            }
        )
    return components


def _npm_name(package_path: str) -> str:
    marker = "node_modules/"
    if marker not in package_path:
        raise ValueError(f"unsupported npm lock package path: {package_path}")
    tail = package_path.rsplit(marker, maxsplit=1)[-1]
    if not tail or tail.endswith("/node_modules"):
        raise ValueError(f"invalid npm lock package path: {package_path}")
    return tail


def _npm_components(payload: bytes, *, lock_name: str) -> list[dict[str, Any]]:
    try:
        lock = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {lock_name} JSON") from exc
    if not isinstance(lock, dict) or lock.get("lockfileVersion") not in {2, 3}:
        raise ValueError(f"{lock_name} must be an npm lockfile v2 or v3")
    packages = lock.get("packages")
    if not isinstance(packages, dict):
        raise ValueError(f"{lock_name} must contain a packages object")
    components: list[dict[str, Any]] = []
    for package_path, package in packages.items():
        if package_path == "":
            continue
        if not isinstance(package_path, str) or not isinstance(package, dict):
            raise ValueError(f"{lock_name} contains an invalid package record")
        version = package.get("version")
        if not isinstance(version, str) or not version.strip():
            raise ValueError(f"{lock_name} package {package_path} has no version")
        name = _npm_name(package_path)
        purl = f"pkg:npm/{quote(name, safe='/')}@{quote(version.strip(), safe='')}"
        components.append(
            {
                "bom-ref": purl,
                "name": name,
                "purl": purl,
                "type": "library",
                "version": version.strip(),
            }
        )
    return components


def _sidecar_identity(
    receipt: Mapping[str, Any],
    *,
    python_lock_sha256: str,
) -> tuple[str, str, str, str]:
    if receipt.get("schema") != SIDECAR_RECEIPT_SCHEMA:
        raise ValueError("invalid sidecar receipt schema")
    commit = receipt.get("source_commit")
    if not isinstance(commit, str) or not _COMMIT_RE.fullmatch(commit):
        raise ValueError("sidecar receipt source commit is invalid")
    app_version = receipt.get("app_version")
    if not isinstance(app_version, str) or not app_version.strip():
        raise ValueError("sidecar receipt app version is invalid")
    recorded_lock = _require_sha256(
        receipt.get("python_lock_sha256"),
        field="sidecar receipt python lock digest",
    )
    if recorded_lock != python_lock_sha256:
        raise ValueError("python lock digest mismatch")
    binary_sha256 = _require_sha256(
        receipt.get("binary_sha256"),
        field="sidecar receipt binary digest",
    )
    web_receipt_sha256 = _require_sha256(
        receipt.get("web_asset_receipt_sha256"),
        field="sidecar receipt web asset receipt digest",
    )
    return commit, app_version.strip(), binary_sha256, web_receipt_sha256


def canonical_sbom_bytes(sbom: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(sbom),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def build_desktop_sbom(
    *,
    uv_lock: Path,
    desktop_lock: Path,
    web_lock: Path,
    sidecar_receipt: Mapping[str, Any],
) -> dict[str, Any]:
    """Build deterministic component metadata bound to all three lockfiles."""

    uv_payload = _read_regular_bytes(uv_lock)
    desktop_payload = _read_regular_bytes(desktop_lock)
    web_payload = _read_regular_bytes(web_lock)
    lock_digests = {
        "desktop_npm_lock_sha256": _sha256_bytes(desktop_payload),
        "python_lock_sha256": _sha256_bytes(uv_payload),
        "web_npm_lock_sha256": _sha256_bytes(web_payload),
    }
    (
        source_commit,
        app_version,
        sidecar_binary_sha256,
        web_asset_receipt_sha256,
    ) = _sidecar_identity(
        sidecar_receipt,
        python_lock_sha256=lock_digests["python_lock_sha256"],
    )
    components_by_purl: dict[str, dict[str, Any]] = {}
    component_sets = (
        _python_components(uv_payload),
        _npm_components(desktop_payload, lock_name="desktop package-lock.json"),
        _npm_components(web_payload, lock_name="web package-lock.json"),
    )
    for component_set in component_sets:
        for component in component_set:
            components_by_purl[component["purl"]] = component
    components = [
        components_by_purl[purl] for purl in sorted(components_by_purl)
    ]
    identity_payload = json.dumps(
        {
            "app_version": app_version,
            "locks": lock_digests,
            "source_commit": source_commit,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    serial = uuid.uuid5(uuid.NAMESPACE_URL, identity_payload.decode("utf-8"))
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "serialNumber": f"urn:uuid:{serial}",
        "version": 1,
        "metadata": {
            "component": {
                "bom-ref": f"pkg:generic/kestrel@{quote(app_version, safe='')}",
                "name": "Kestrel",
                "type": "application",
                "version": app_version,
            },
            "properties": [
                {"name": f"kestrel:{name}", "value": value}
                for name, value in sorted(
                    {
                        **lock_digests,
                        "sidecar_binary_sha256": sidecar_binary_sha256,
                        "source_commit": source_commit,
                        "web_asset_receipt_sha256": web_asset_receipt_sha256,
                    }.items()
                )
            ],
        },
        "components": components,
    }


def canonical_receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    payload = (
        json.dumps(
            dict(receipt),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(payload) >= MAX_RECEIPT_BYTES:
        raise ValueError("SBOM receipt exceeds 64 KiB")
    return payload


def _write_exclusive(path: Path, payload: bytes) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def generate_sbom(
    *,
    uv_lock: Path,
    desktop_lock: Path,
    web_lock: Path,
    sidecar_receipt_path: Path,
    output_path: Path,
    receipt_path: Path,
) -> dict[str, Any]:
    sidecar_receipt = _read_json_object(sidecar_receipt_path)
    sbom = build_desktop_sbom(
        uv_lock=uv_lock,
        desktop_lock=desktop_lock,
        web_lock=web_lock,
        sidecar_receipt=sidecar_receipt,
    )
    payload = canonical_sbom_bytes(sbom)
    _write_exclusive(output_path, payload)
    properties_raw = sbom["metadata"]["properties"]
    if not isinstance(properties_raw, list):
        raise ValueError("generated SBOM properties are invalid")
    properties = {
        str(item["name"]).removeprefix("kestrel:"): str(item["value"])
        for item in properties_raw
        if isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and str(item["name"]).startswith("kestrel:")
    }
    receipt = {
        "schema": SBOM_RECEIPT_SCHEMA,
        "source_commit": sidecar_receipt["source_commit"],
        "app_version": sidecar_receipt["app_version"],
        "python_lock_sha256": properties["python_lock_sha256"],
        "desktop_npm_lock_sha256": properties["desktop_npm_lock_sha256"],
        "web_npm_lock_sha256": properties["web_npm_lock_sha256"],
        "sidecar_binary_sha256": properties["sidecar_binary_sha256"],
        "web_asset_receipt_sha256": properties["web_asset_receipt_sha256"],
        "sbom_path": str(output_path.resolve()),
        "sbom_size": len(payload),
        "sbom_sha256": _sha256_bytes(payload),
    }
    _write_exclusive(receipt_path, canonical_receipt_bytes(receipt))
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uv-lock", type=Path, required=True)
    parser.add_argument("--desktop-lock", type=Path, required=True)
    parser.add_argument("--web-lock", type=Path, required=True)
    parser.add_argument("--sidecar-receipt", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    receipt = generate_sbom(
        uv_lock=arguments.uv_lock,
        desktop_lock=arguments.desktop_lock,
        web_lock=arguments.web_lock,
        sidecar_receipt_path=arguments.sidecar_receipt,
        output_path=arguments.output,
        receipt_path=arguments.receipt,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
