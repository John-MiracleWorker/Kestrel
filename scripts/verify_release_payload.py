from __future__ import annotations

import argparse
import hashlib
import json
import re
import tarfile
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HASH_OPTION = "--hash=sha256:"
_DISTRIBUTION_NORMALIZER = re.compile(r"[-_.]+")
_MAX_METADATA_BYTES = 2 * 1024 * 1024
DEFAULT_DISTRIBUTION = "nested-memvid-agent"


def _regular_files(root: Path) -> dict[str, Path]:
    files: dict[str, Path] = {}
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"release payload contains a non-regular entry: {path.name}")
        files[path.name] = path
    return files


def _manifest_entries(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="ascii").splitlines(), 1):
        parts = raw_line.split(maxsplit=1)
        if len(parts) != 2:
            raise ValueError(f"invalid SHA256SUMS line {line_number}")
        digest, name = parts
        name = name.lstrip("*")
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"invalid SHA-256 on SHA256SUMS line {line_number}")
        if Path(name).name != name or name in {".", "..", "SHA256SUMS"}:
            raise ValueError(f"unsafe artifact name on SHA256SUMS line {line_number}: {name}")
        if name in entries:
            raise ValueError(f"duplicate artifact in SHA256SUMS: {name}")
        entries[name] = digest
    return entries


def _logical_requirements(path: Path) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].strip() if continued else stripped
        current = f"{current} {fragment}".strip()
        if not continued:
            logical.append(current)
            current = ""
    if current:
        raise ValueError("requirements-release.txt ends with an incomplete continuation")
    return logical


def _verify_hash_locked_requirements(path: Path) -> int:
    requirements = _logical_requirements(path)
    if not requirements:
        raise ValueError("requirements-release.txt is empty")
    for requirement in requirements:
        if requirement.startswith("-"):
            raise ValueError(f"requirements-release.txt contains an option: {requirement}")
        requirement_spec = requirement.split(_HASH_OPTION, maxsplit=1)[0]
        if "==" not in requirement_spec or _HASH_OPTION not in requirement:
            raise ValueError(f"requirement is not exact and hash-locked: {requirement_spec.strip()}")
        hashes = requirement.count(_HASH_OPTION)
        if hashes < 1:
            raise ValueError(f"requirement has no SHA-256 hash: {requirement_spec.strip()}")
    return len(requirements)


def _canonical_distribution(value: str) -> str:
    return _DISTRIBUTION_NORMALIZER.sub("-", value).casefold()


def _release_version(value: str) -> str:
    version = value.removeprefix("v")
    if not version or version != value.lstrip("v") or not re.fullmatch(
        r"[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9.+-]*)?", version
    ):
        raise ValueError(f"invalid expected release version: {value!r}")
    return version


def _metadata_identity(data: bytes, *, label: str) -> tuple[str, str]:
    if len(data) > _MAX_METADATA_BYTES:
        raise ValueError(f"{label} exceeds {_MAX_METADATA_BYTES} bytes")
    message = BytesParser(policy=policy.default).parsebytes(data)
    name = message.get("Name")
    version = message.get("Version")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"{label} is missing Name metadata")
    if not isinstance(version, str) or not version.strip():
        raise ValueError(f"{label} is missing Version metadata")
    return name.strip(), version.strip()


def _assert_identity(
    *,
    actual_distribution: str,
    actual_version: str,
    expected_distribution: str,
    expected_version: str,
    label: str,
) -> None:
    if _canonical_distribution(actual_distribution) != _canonical_distribution(
        expected_distribution
    ):
        raise ValueError(
            f"{label} distribution mismatch: {actual_distribution!r} != "
            f"{expected_distribution!r}"
        )
    if actual_version != expected_version:
        raise ValueError(
            f"{label} version mismatch: {actual_version!r} != {expected_version!r}"
        )


def _verify_wheel_identity(
    path: Path, *, expected_distribution: str, expected_version: str
) -> None:
    filename_parts = path.name.removesuffix(".whl").split("-")
    if len(filename_parts) not in {5, 6}:
        raise ValueError(f"invalid wheel filename: {path.name}")
    _assert_identity(
        actual_distribution=filename_parts[0],
        actual_version=filename_parts[1],
        expected_distribution=expected_distribution,
        expected_version=expected_version,
        label="wheel filename",
    )
    try:
        with zipfile.ZipFile(path) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA")
                and len(Path(name).parts) == 2
            ]
            if len(metadata_names) != 1:
                raise ValueError(
                    f"wheel must contain exactly one dist-info/METADATA: {metadata_names}"
                )
            metadata_name = metadata_names[0]
            info = archive.getinfo(metadata_name)
            if info.file_size > _MAX_METADATA_BYTES:
                raise ValueError(f"wheel METADATA exceeds {_MAX_METADATA_BYTES} bytes")
            metadata = archive.read(metadata_name)
    except zipfile.BadZipFile as exc:
        raise ValueError(f"invalid wheel archive: {path.name}") from exc
    distribution, version = _metadata_identity(metadata, label="wheel METADATA")
    _assert_identity(
        actual_distribution=distribution,
        actual_version=version,
        expected_distribution=expected_distribution,
        expected_version=expected_version,
        label="wheel METADATA",
    )


def _verify_sdist_identity(
    path: Path, *, expected_distribution: str, expected_version: str
) -> None:
    stem = path.name.removesuffix(".tar.gz")
    try:
        distribution_name, filename_version = stem.rsplit("-", maxsplit=1)
    except ValueError as exc:
        raise ValueError(f"invalid sdist filename: {path.name}") from exc
    _assert_identity(
        actual_distribution=distribution_name,
        actual_version=filename_version,
        expected_distribution=expected_distribution,
        expected_version=expected_version,
        label="sdist filename",
    )
    try:
        with tarfile.open(path, "r:gz") as archive:
            metadata_members = [
                member
                for member in archive.getmembers()
                if member.isfile()
                and member.name.endswith("/PKG-INFO")
                and len(Path(member.name).parts) == 2
            ]
            if len(metadata_members) != 1:
                raise ValueError(
                    "sdist must contain exactly one top-level PKG-INFO: "
                    f"{[member.name for member in metadata_members]}"
                )
            member = metadata_members[0]
            if member.size > _MAX_METADATA_BYTES:
                raise ValueError(f"sdist PKG-INFO exceeds {_MAX_METADATA_BYTES} bytes")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("unable to read sdist PKG-INFO")
            metadata = handle.read(_MAX_METADATA_BYTES + 1)
    except (tarfile.TarError, EOFError) as exc:
        raise ValueError(f"invalid sdist archive: {path.name}") from exc
    distribution, version = _metadata_identity(metadata, label="sdist PKG-INFO")
    _assert_identity(
        actual_distribution=distribution,
        actual_version=version,
        expected_distribution=expected_distribution,
        expected_version=expected_version,
        label="sdist PKG-INFO",
    )


def _component_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    components = payload.get("components", [])
    if isinstance(components, list):
        candidates.extend(item for item in components if isinstance(item, dict))
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        component = metadata.get("component")
        if isinstance(component, dict):
            candidates.append(component)
    return candidates


def _verify_sbom_identity(
    path: Path, *, expected_distribution: str, expected_version: str
) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("sbom.cdx.json is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("sbom.cdx.json must contain a JSON object")
    matching = [
        component
        for component in _component_candidates(payload)
        if _canonical_distribution(str(component.get("name", "")))
        == _canonical_distribution(expected_distribution)
    ]
    identities = {
        (
            _canonical_distribution(str(component.get("name", ""))),
            str(component.get("version", "")),
        )
        for component in matching
    }
    expected_identity = (_canonical_distribution(expected_distribution), expected_version)
    if identities != {expected_identity}:
        raise ValueError(
            "CycloneDX Kestrel component identity mismatch: "
            f"found={sorted(identities)!r}, "
            f"expected={[expected_identity]!r}"
        )


def verify_release_payload(
    root: Path,
    *,
    expected_version: str,
    expected_distribution: str = DEFAULT_DISTRIBUTION,
) -> dict[str, object]:
    expected_version = _release_version(expected_version)
    if _canonical_distribution(expected_distribution) != DEFAULT_DISTRIBUTION:
        raise ValueError(f"unexpected release distribution: {expected_distribution!r}")
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError(f"release payload is not a directory: {root}")
    files = _regular_files(root)
    required = {"SHA256SUMS", "install.sh", "requirements-release.txt", "sbom.cdx.json"}
    missing = sorted(required - files.keys())
    if missing:
        raise ValueError(f"release payload is missing required files: {missing}")

    wheels = sorted(name for name in files if name.endswith(".whl"))
    sdists = sorted(name for name in files if name.endswith(".tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError(f"expected one wheel and one sdist, got wheels={wheels}, sdists={sdists}")
    if not wheels[0].endswith("-py3-none-any.whl"):
        raise ValueError(f"Kestrel release wheel is not platform-independent: {wheels[0]}")

    entries = _manifest_entries(files["SHA256SUMS"])
    payload_names = set(files) - {"SHA256SUMS"}
    if set(entries) != payload_names:
        raise ValueError(
            "SHA256SUMS coverage mismatch: "
            f"missing={sorted(payload_names - set(entries))}, "
            f"unknown={sorted(set(entries) - payload_names)}"
        )
    for name, expected in entries.items():
        actual = hashlib.sha256(files[name].read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"SHA-256 mismatch for {name}")

    _verify_wheel_identity(
        files[wheels[0]],
        expected_distribution=expected_distribution,
        expected_version=expected_version,
    )
    _verify_sdist_identity(
        files[sdists[0]],
        expected_distribution=expected_distribution,
        expected_version=expected_version,
    )
    _verify_sbom_identity(
        files["sbom.cdx.json"],
        expected_distribution=expected_distribution,
        expected_version=expected_version,
    )

    requirement_count = _verify_hash_locked_requirements(files["requirements-release.txt"])
    return {
        "wheel": wheels[0],
        "sdist": sdists[0],
        "distribution": expected_distribution,
        "version": expected_version,
        "artifact_count": len(payload_names),
        "requirement_count": requirement_count,
        "verified": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify the exact, self-checksummed Kestrel release payload."
    )
    parser.add_argument("payload", type=Path)
    parser.add_argument(
        "--expected-version",
        required=True,
        help="Exact release version or v-prefixed release tag.",
    )
    parser.add_argument(
        "--expected-distribution",
        default=DEFAULT_DISTRIBUTION,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args()
    try:
        report = verify_release_payload(
            args.payload,
            expected_version=args.expected_version,
            expected_distribution=args.expected_distribution,
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
