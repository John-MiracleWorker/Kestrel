#!/usr/bin/env python3
"""Fail-closed publication guards for GitHub Releases, GHCR, and PyPI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

if not __package__:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_release_payload import verify_release_payload

OCI_RECORD_NAME = "oci-image-digests.json"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _regular_files(root: Path) -> dict[str, Path]:
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"artifact root is not a directory: {root}")
    files: dict[str, Path] = {}
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"artifact root contains a non-regular entry: {path.name}")
        if Path(path.name).name != path.name or any(ord(char) < 32 for char in path.name):
            raise ValueError(f"unsafe artifact name: {path.name!r}")
        files[path.name] = path
    return files


def _require_digest(value: object, *, label: str) -> str:
    digest = str(value)
    if not _DIGEST_RE.fullmatch(digest):
        raise ValueError(f"invalid {label}: {digest!r}")
    return digest


def build_oci_record(
    *,
    repository: str,
    tag: str,
    commit: str,
    image: str,
    index_digest: str,
    amd64_digest: str,
    arm64_digest: str,
) -> dict[str, object]:
    if not _SHA_RE.fullmatch(commit):
        raise ValueError(f"invalid release commit: {commit!r}")
    return {
        "schema_version": 1,
        "repository": repository,
        "tag": tag,
        "commit": commit,
        "image": image,
        "index_digest": _require_digest(index_digest, label="index digest"),
        "platforms": {
            "linux/amd64": _require_digest(amd64_digest, label="linux/amd64 digest"),
            "linux/arm64": _require_digest(arm64_digest, label="linux/arm64 digest"),
        },
    }


def validate_oci_record(
    payload: object,
    *,
    repository: str,
    tag: str,
    commit: str,
    image: str,
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("OCI digest record must be a JSON object")
    expected_keys = {
        "schema_version",
        "repository",
        "tag",
        "commit",
        "image",
        "index_digest",
        "platforms",
    }
    if set(payload) != expected_keys:
        raise ValueError(
            "OCI digest record fields mismatch: "
            f"missing={sorted(expected_keys - set(payload))}, "
            f"unknown={sorted(set(payload) - expected_keys)}"
        )
    expected_identity = {
        "schema_version": 1,
        "repository": repository,
        "tag": tag,
        "commit": commit,
        "image": image,
    }
    actual_identity = {key: payload.get(key) for key in expected_identity}
    if actual_identity != expected_identity:
        raise ValueError(
            f"OCI digest record identity mismatch: {actual_identity!r} != {expected_identity!r}"
        )
    platforms = payload.get("platforms")
    if not isinstance(platforms, dict) or set(platforms) != {
        "linux/amd64",
        "linux/arm64",
    }:
        raise ValueError(f"OCI digest record platform set mismatch: {platforms!r}")
    _require_digest(payload.get("index_digest"), label="index digest")
    for platform, digest in platforms.items():
        _require_digest(digest, label=f"{platform} digest")
    return payload


def validate_oci_index(manifest: object, record: dict[str, object]) -> None:
    if not isinstance(manifest, dict):
        raise ValueError("OCI index inspection must be a JSON object")
    actual_digest = _require_digest(manifest.get("digest"), label="inspected index digest")
    expected_digest = str(record["index_digest"])
    if actual_digest != expected_digest:
        raise ValueError(f"OCI index digest mismatch: {actual_digest!r} != {expected_digest!r}")
    descriptors = manifest.get("manifests")
    if not isinstance(descriptors, list) or len(descriptors) != 2:
        raise ValueError("OCI index must contain exactly two platform descriptors")
    actual_platforms: dict[str, str] = {}
    for descriptor in descriptors:
        if not isinstance(descriptor, dict) or not isinstance(descriptor.get("platform"), dict):
            raise ValueError(f"invalid OCI platform descriptor: {descriptor!r}")
        platform = descriptor["platform"]
        name = f"{platform.get('os')}/{platform.get('architecture')}"
        if name in actual_platforms:
            raise ValueError(f"duplicate OCI platform descriptor: {name}")
        actual_platforms[name] = _require_digest(
            descriptor.get("digest"), label=f"{name} descriptor digest"
        )
    expected_platforms = record["platforms"]
    if actual_platforms != expected_platforms:
        raise ValueError(
            f"OCI platform descriptors mismatch: {actual_platforms!r} != {expected_platforms!r}"
        )


def write_oci_record(
    root: Path,
    *,
    repository: str,
    tag: str,
    commit: str,
    image: str,
    index_digest: str,
    amd64_digest: str,
    arm64_digest: str,
) -> Path:
    root = root.resolve(strict=True)
    record = build_oci_record(
        repository=repository,
        tag=tag,
        commit=commit,
        image=image,
        index_digest=index_digest,
        amd64_digest=amd64_digest,
        arm64_digest=arm64_digest,
    )
    record_path = root / OCI_RECORD_NAME
    if record_path.exists() and (record_path.is_symlink() or not record_path.is_file()):
        raise ValueError(f"refusing to replace non-regular {OCI_RECORD_NAME}")
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    files = _regular_files(root)
    manifest = root / "SHA256SUMS"
    lines = [
        f"{_sha256(path)}  {name}\n" for name, path in sorted(files.items()) if name != "SHA256SUMS"
    ]
    manifest.write_text("".join(lines), encoding="ascii")
    return record_path


def _release_assets(release: object) -> dict[str, int]:
    if not isinstance(release, dict) or not isinstance(release.get("assets"), list):
        raise ValueError("GitHub release response has no assets list")
    assets: dict[str, int] = {}
    for asset in release["assets"]:
        if not isinstance(asset, dict):
            raise ValueError(f"invalid GitHub release asset: {asset!r}")
        name = asset.get("name")
        asset_id = asset.get("id")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or any(ord(char) < 32 for char in name)
        ):
            raise ValueError(f"unsafe GitHub release asset name: {name!r}")
        if not isinstance(asset_id, int) or asset_id <= 0:
            raise ValueError(f"invalid GitHub release asset id for {name!r}: {asset_id!r}")
        if name in assets:
            raise ValueError(f"duplicate GitHub release asset name: {name}")
        assets[name] = asset_id
    return assets


def validate_release_assets(
    local_root: Path,
    release: object,
    *,
    allow_missing: bool,
    downloaded_root: Path | None = None,
) -> dict[str, int]:
    local = _regular_files(local_root)
    assets = _release_assets(release)
    unknown = sorted(set(assets) - set(local))
    missing = sorted(set(local) - set(assets))
    if unknown or (missing and not allow_missing):
        raise ValueError(f"GitHub release asset set mismatch: missing={missing}, unknown={unknown}")
    if downloaded_root is not None:
        if allow_missing:
            raise ValueError("downloaded asset verification requires an exact asset set")
        downloaded = _regular_files(downloaded_root)
        if set(downloaded) != set(local):
            raise ValueError(
                "downloaded GitHub release asset set mismatch: "
                f"missing={sorted(set(local) - set(downloaded))}, "
                f"unknown={sorted(set(downloaded) - set(local))}"
            )
        for name, path in local.items():
            actual = _sha256(downloaded[name])
            expected = _sha256(path)
            if actual != expected:
                raise ValueError(
                    f"GitHub release asset SHA-256 mismatch for {name}: {actual} != {expected}"
                )
    return assets


def plan_pypi_files(
    local_root: Path,
    remote: object,
    *,
    expected_version: str,
) -> list[Path]:
    local_files = _regular_files(local_root)
    distributions = {
        name: path
        for name, path in local_files.items()
        if name.endswith(".whl") or name.endswith(".tar.gz")
    }
    wheels = [name for name in distributions if name.endswith(".whl")]
    sdists = [name for name in distributions if name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(f"expected one wheel and one sdist, got wheels={wheels}, sdists={sdists}")
    if not isinstance(remote, dict):
        raise ValueError("PyPI response must be a JSON object")
    urls = remote.get("urls", [])
    if not isinstance(urls, list):
        raise ValueError("PyPI response urls must be a list")
    info = remote.get("info")
    if urls and (not isinstance(info, dict) or str(info.get("version")) != expected_version):
        raise ValueError("PyPI response version does not match the release version")
    existing: dict[str, dict[str, Any]] = {}
    for item in urls:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError(f"invalid PyPI file record: {item!r}")
        name = item["filename"]
        if name in existing:
            raise ValueError(f"duplicate PyPI filename: {name}")
        existing[name] = item
    unknown = sorted(set(existing) - set(distributions))
    if unknown:
        raise ValueError(f"PyPI version contains unexpected files: {unknown}")
    for name, item in existing.items():
        digests = item.get("digests")
        if not isinstance(digests, dict):
            raise ValueError(f"PyPI file has no digests: {name}")
        remote_digest = str(digests.get("sha256"))
        local_digest = _sha256(distributions[name])
        if remote_digest != local_digest:
            raise ValueError(f"PyPI SHA-256 mismatch for {name}: {remote_digest} != {local_digest}")
        if item.get("yanked") is True:
            raise ValueError(f"PyPI file is yanked and cannot be treated as published: {name}")
    return [distributions[name] for name in sorted(set(distributions) - set(existing))]


def _fetch_pypi_version(*, api_base: str, project: str, version: str) -> object:
    url = (
        f"{api_base.rstrip('/')}/{urllib.parse.quote(project, safe='')}/"
        f"{urllib.parse.quote(version, safe='')}/json"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Kestrel-release/0.4"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"urls": []}
        raise ValueError(f"PyPI version query failed with HTTP {exc.code}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"PyPI version query failed: {exc}") from exc


def _load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}: {exc}") from exc


def _write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    write = subparsers.add_parser("write-oci-record")
    write.add_argument("root", type=Path)
    for name in ("repository", "tag", "commit", "image", "index-digest"):
        write.add_argument(f"--{name}", required=True)
    write.add_argument("--amd64-digest", required=True)
    write.add_argument("--arm64-digest", required=True)

    verify_record = subparsers.add_parser("verify-oci-record")
    verify_record.add_argument("record", type=Path)
    for name in ("repository", "tag", "commit", "image"):
        verify_record.add_argument(f"--{name}", required=True)

    verify_index = subparsers.add_parser("verify-oci-index")
    verify_index.add_argument("record", type=Path)
    verify_index.add_argument("manifest", type=Path)
    for name in ("repository", "tag", "commit", "image"):
        verify_index.add_argument(f"--{name}", required=True)

    assets = subparsers.add_parser("verify-release-assets")
    assets.add_argument("local_root", type=Path)
    assets.add_argument("release_json", type=Path)
    assets.add_argument("--allow-missing", action="store_true")
    assets.add_argument("--downloaded-root", type=Path)

    pypi = subparsers.add_parser("pypi-plan")
    pypi.add_argument("local_root", type=Path)
    pypi.add_argument("output_root", type=Path)
    pypi.add_argument("--project", required=True)
    pypi.add_argument("--expected-version", required=True)
    pypi.add_argument("--api-base", default="https://pypi.org/pypi")

    args = parser.parse_args()
    try:
        if args.command == "write-oci-record":
            path = write_oci_record(
                args.root,
                repository=args.repository,
                tag=args.tag,
                commit=args.commit,
                image=args.image,
                index_digest=args.index_digest,
                amd64_digest=args.amd64_digest,
                arm64_digest=args.arm64_digest,
            )
            verify_release_payload(args.root, expected_version=args.tag)
            print(path)
        elif args.command in {"verify-oci-record", "verify-oci-index"}:
            record = validate_oci_record(
                _load_json(args.record),
                repository=args.repository,
                tag=args.tag,
                commit=args.commit,
                image=args.image,
            )
            if args.command == "verify-oci-index":
                validate_oci_index(_load_json(args.manifest), record)
            digest = str(record["index_digest"])
            print(digest)
        elif args.command == "verify-release-assets":
            found = validate_release_assets(
                args.local_root,
                _load_json(args.release_json),
                allow_missing=args.allow_missing,
                downloaded_root=args.downloaded_root,
            )
            print(json.dumps(found, sort_keys=True))
        elif args.command == "pypi-plan":
            verify_release_payload(args.local_root, expected_version=args.expected_version)
            remote = _fetch_pypi_version(
                api_base=args.api_base,
                project=args.project,
                version=args.expected_version.removeprefix("v"),
            )
            missing = plan_pypi_files(
                args.local_root,
                remote,
                expected_version=args.expected_version.removeprefix("v"),
            )
            args.output_root.mkdir(parents=False, exist_ok=False)
            for path in missing:
                shutil.copy2(path, args.output_root / path.name)
            required = "true" if missing else "false"
            _write_github_output("upload_required", required)
            _write_github_output("upload_count", str(len(missing)))
            print(json.dumps({"missing": [path.name for path in missing]}, sort_keys=True))
        else:  # pragma: no cover
            raise AssertionError(args.command)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
