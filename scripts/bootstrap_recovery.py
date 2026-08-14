#!/usr/bin/env python3
"""Safely extract a deterministic recovery capsule before closure verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess  # nosec B404
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import cast

if __name__ == "__main__" and not (
    sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode
):
    sys.stderr.write("recovery bootstrap requires Python -I -S -B isolation\n")
    raise SystemExit(2)

MAX_MEMBERS = 8192
MAX_MEMBER_BYTES = 2_147_483_648
MAX_TOTAL_BYTES = 2_147_483_648
MAX_BOOTSTRAP_OUTPUT_BYTES = 4 * 1024 * 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class RecoveryBootstrapError(ValueError):
    """The recovery capsule is not safe to extract."""


def _safe_member_path(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or "\\" in name
        or "\x00" in name
    ):
        raise RecoveryBootstrapError("recovery archive member path is unsafe")
    return path


def _prepare_destination(destination: Path) -> None:
    if destination.exists() or destination.is_symlink():
        if not destination.is_dir() or destination.is_symlink() or any(destination.iterdir()):
            raise RecoveryBootstrapError("recovery extraction destination must be absent or empty")
        destination.chmod(0o700)
        return
    destination.mkdir(mode=0o700, parents=False)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def extract_recovery_capsule(*, archive: Path, destination: Path) -> None:
    """Extract only deterministic regular files/directories without tarfile.extract."""

    if not archive.is_file() or archive.is_symlink():
        raise RecoveryBootstrapError("recovery archive is not a regular file")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise RecoveryBootstrapError("recovery destination parent is not a real directory")
    _prepare_destination(destination)
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(archive, mode="r:") as source:
            members = source.getmembers()
            if not members or len(members) > MAX_MEMBERS:
                raise RecoveryBootstrapError("recovery archive member cardinality is invalid")
            for member in members:
                relative = _safe_member_path(member.name)
                normalized = relative.as_posix()
                if normalized in seen:
                    raise RecoveryBootstrapError("recovery archive has a duplicate member path")
                seen.add(normalized)
                if member.pax_headers or member.sparse is not None:
                    raise RecoveryBootstrapError(
                        "recovery archive uses forbidden extended metadata"
                    )
                if member.uid != 0 or member.gid != 0 or member.mtime != 0:
                    raise RecoveryBootstrapError(
                        "recovery archive member metadata is nondeterministic"
                    )
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    if member.mode != 0o755:
                        raise RecoveryBootstrapError("recovery archive directory mode is invalid")
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                if not member.isreg():
                    raise RecoveryBootstrapError(
                        "recovery archive link or special member is forbidden"
                    )
                if member.mode != 0o644:
                    raise RecoveryBootstrapError("recovery archive file mode is invalid")
                if member.size < 0 or member.size > MAX_MEMBER_BYTES:
                    raise RecoveryBootstrapError("recovery archive member size is invalid")
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise RecoveryBootstrapError("recovery archive expands beyond the total limit")
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                extracted = source.extractfile(member)
                if extracted is None:
                    raise RecoveryBootstrapError("recovery archive regular member has no body")
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(target, flags, 0o644)
                written = 0
                try:
                    with os.fdopen(descriptor, "wb", closefd=True) as output:
                        while chunk := extracted.read(1024 * 1024):
                            written += len(chunk)
                            if written > member.size:
                                raise RecoveryBootstrapError(
                                    "recovery archive member exceeded declared size"
                                )
                            output.write(chunk)
                        if written != member.size:
                            raise RecoveryBootstrapError(
                                "recovery archive member ended before declared size"
                            )
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    extracted.close()
                target.chmod(0o644)
                _fsync_directory(target.parent)
        _fsync_directory(destination)
        _fsync_directory(destination.parent)
    except Exception:
        # Leave any extracted bytes for forensic inspection. They are never executed
        # because successful return is the only authority to continue bootstrap.
        raise


def _bootstrap_environment() -> dict[str, str]:
    return {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "",
        "PYTHONNOUSERSITE": "1",
        "PYTHONSAFEPATH": "1",
    }


def _run_bootstrap_command(command: list[str], *, environment: dict[str, str]) -> None:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
        check=False,
        capture_output=True,
        env=environment,
        timeout=300,
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_BOOTSTRAP_OUTPUT_BYTES
        or len(completed.stderr) > MAX_BOOTSTRAP_OUTPUT_BYTES
    ):
        raise RecoveryBootstrapError("recovery environment bootstrap command failed")


def _checked_digest(value: str, *, label: str) -> str:
    if DIGEST_RE.fullmatch(value) is None:
        raise RecoveryBootstrapError(f"{label} is not an exact SHA-256 digest")
    return value


def _path_digest(path: Path, *, maximum: int = MAX_TOTAL_BYTES) -> tuple[int, str]:
    if not path.is_file() or path.is_symlink():
        raise RecoveryBootstrapError("recovery bootstrap input is not a regular file")
    size = path.stat().st_size
    if size < 0 or size > maximum:
        raise RecoveryBootstrapError("recovery bootstrap input exceeds its size limit")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return size, "sha256:" + digest.hexdigest()


def _json_without_duplicates(raw: bytes, *, label: str) -> dict[str, object]:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise RecoveryBootstrapError(f"{label} has a duplicate object key")
            value[key] = item
        return value

    try:
        parsed = json.loads(raw, object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryBootstrapError(f"{label} is not valid JSON") from exc
    if type(parsed) is not dict:
        raise RecoveryBootstrapError(f"{label} must be an object")
    return cast(dict[str, object], parsed)


def _trusted_capsule_inputs(
    *, capsule_root: Path, expected_manifest_digest: str
) -> tuple[dict[str, object], bytes, dict[str, object]]:
    """Authenticate every extracted byte before importing capsule Python."""

    manifest_path = capsule_root / "recovery-capsule-manifest.json"
    _, manifest_digest = _path_digest(manifest_path, maximum=64 * 1024 * 1024)
    if manifest_digest != _checked_digest(
        expected_manifest_digest, label="expected recovery manifest digest"
    ):
        raise RecoveryBootstrapError("recovery capsule manifest digest mismatch")
    manifest_raw = manifest_path.read_bytes()
    manifest = _json_without_duplicates(manifest_raw, label="recovery capsule manifest")
    raw_assets = manifest.get("assets")
    if type(raw_assets) is not list or not raw_assets:
        raise RecoveryBootstrapError("recovery capsule manifest asset inventory is empty")
    expected: dict[str, tuple[int, str]] = {}
    previous = ""
    total = 0
    for raw_item in raw_assets:
        if type(raw_item) is not dict or set(raw_item) != {
            "media_type",
            "name",
            "sha256",
            "size_bytes",
        }:
            raise RecoveryBootstrapError("recovery capsule manifest asset fields mismatch")
        name = raw_item.get("name")
        size = raw_item.get("size_bytes")
        digest = raw_item.get("sha256")
        if type(name) is not str or name <= previous:
            raise RecoveryBootstrapError("recovery capsule manifest assets are not sorted unique")
        relative = _safe_member_path(name)
        if relative.as_posix() != name or name == "recovery-capsule-manifest.json":
            raise RecoveryBootstrapError("recovery capsule manifest asset path is unsafe")
        if type(size) is not int or isinstance(size, bool) or size < 0:
            raise RecoveryBootstrapError("recovery capsule manifest asset size is invalid")
        if type(digest) is not str:
            raise RecoveryBootstrapError("recovery capsule manifest asset digest is invalid")
        expected[name] = (size, _checked_digest(digest, label="recovery asset digest"))
        previous = name
        total += size
        if total > MAX_TOTAL_BYTES:
            raise RecoveryBootstrapError("recovery capsule manifest inventory is too large")
    actual = {
        path.relative_to(capsule_root).as_posix()
        for path in capsule_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual != {*expected, "recovery-capsule-manifest.json"}:
        raise RecoveryBootstrapError("recovery capsule authenticated file inventory mismatch")
    for name, identity in expected.items():
        if _path_digest(capsule_root / name) != identity:
            raise RecoveryBootstrapError(f"recovery capsule asset identity mismatch: {name}")
    closure_path = capsule_root / "recovery-execution-closure.json"
    if "recovery-execution-closure.json" not in expected:
        raise RecoveryBootstrapError("recovery capsule execution closure is missing")
    closure_raw = closure_path.read_bytes()
    closure = _json_without_duplicates(closure_raw, label="recovery execution closure")
    return manifest, closure_raw, closure


def _bootstrap_dependency_inputs(
    *, capsule_root: Path, closure: dict[str, object]
) -> tuple[Path, Path, Path, Path]:
    runtime = closure.get("python_runtime")
    actual_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    if type(runtime) is not dict or runtime != {
        "implementation": "CPython",
        "version": actual_version,
        "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
    }:
        raise RecoveryBootstrapError("recovery bootstrap Python runtime mismatch")
    if closure.get("sys_path") != [str(capsule_root)]:
        raise RecoveryBootstrapError("recovery bootstrap capsule path mismatch")
    network = closure.get("network_policy")
    if type(network) is not dict or network.get("default_deny") is not True:
        raise RecoveryBootstrapError("recovery bootstrap network policy is not deny-first")
    executables = closure.get("external_executables")
    if type(executables) is not list:
        raise RecoveryBootstrapError("recovery bootstrap executable inventory is invalid")
    python_items = [
        item for item in executables if type(item) is dict and item.get("name") == "python"
    ]
    if len(python_items) != 1:
        raise RecoveryBootstrapError("recovery bootstrap Python executable is absent or ambiguous")
    venv_root = capsule_root / "venv"
    venv_python = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python_item = python_items[0]
    if python_item.get("path") != str(venv_python):
        raise RecoveryBootstrapError("recovery bootstrap Python path mismatch")
    if venv_root.exists() or venv_root.is_symlink():
        raise RecoveryBootstrapError("recovery capsule contains a prebuilt execution environment")
    expected_python = python_item.get("sha256")
    base_python = Path(sys.executable).resolve(strict=True)
    if type(expected_python) is not str or _path_digest(base_python)[1] != _checked_digest(
        expected_python, label="recovery bootstrap Python digest"
    ):
        raise RecoveryBootstrapError("recovery bootstrap Python binary mismatch")
    lock = closure.get("dependency_lock")
    if type(lock) is not dict or lock.get("requirements_path") != "recovery/requirements.txt":
        raise RecoveryBootstrapError("recovery dependency lock is invalid")
    requirements = capsule_root / "recovery" / "requirements.txt"
    wheelhouse_manifest = capsule_root / "recovery" / "wheelhouse-manifest.json"
    for path, field, label in (
        (requirements, "requirements_sha256", "requirements lock"),
        (wheelhouse_manifest, "wheelhouse_manifest_sha256", "wheelhouse manifest"),
    ):
        expected_digest = lock.get(field)
        if type(expected_digest) is not str or _path_digest(path, maximum=16 * 1024 * 1024)[
            1
        ] != _checked_digest(expected_digest, label=f"recovery {label} digest"):
            raise RecoveryBootstrapError(f"recovery {label} digest mismatch")
    wheelhouse_value = _json_without_duplicates(
        wheelhouse_manifest.read_bytes(), label="recovery wheelhouse manifest"
    )
    wheels = wheelhouse_value.get("wheels")
    if (
        wheelhouse_value.get("schema") != "kestrel.recovery_wheelhouse.v1"
        or type(wheels) is not list
    ):
        raise RecoveryBootstrapError("recovery wheelhouse manifest fields mismatch")
    wheelhouse = capsule_root / "recovery" / "wheelhouse"
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise RecoveryBootstrapError("recovery wheelhouse directory is invalid")
    expected_wheels: dict[str, tuple[int, str]] = {}
    previous = ""
    for raw_wheel in wheels:
        if type(raw_wheel) is not dict or set(raw_wheel) != {
            "filename",
            "sha256",
            "size_bytes",
        }:
            raise RecoveryBootstrapError("recovery wheelhouse wheel fields mismatch")
        filename = raw_wheel.get("filename")
        digest = raw_wheel.get("sha256")
        size = raw_wheel.get("size_bytes")
        if (
            type(filename) is not str
            or PurePosixPath(filename).name != filename
            or not filename.endswith(".whl")
            or filename <= previous
            or type(size) is not int
            or isinstance(size, bool)
            or size <= 0
            or type(digest) is not str
        ):
            raise RecoveryBootstrapError("recovery wheelhouse wheel identity is invalid")
        expected_wheels[filename] = (
            size,
            _checked_digest(digest, label="recovery wheel digest"),
        )
        previous = filename
    actual_wheels = {path.name for path in wheelhouse.iterdir() if path.is_file()}
    if actual_wheels != set(expected_wheels):
        raise RecoveryBootstrapError("recovery wheelhouse inventory mismatch")
    for filename, identity in expected_wheels.items():
        if _path_digest(wheelhouse / filename) != identity:
            raise RecoveryBootstrapError("recovery wheelhouse wheel identity mismatch")
    return venv_root, venv_python, wheelhouse, requirements


def _verify_with_bootstrapped_environment(
    *,
    venv_python: Path,
    capsule_root: Path,
    expected_owner_key_fingerprint: str,
) -> dict[str, object]:
    code = (
        "import json,sys;from pathlib import Path;"
        "root=Path(sys.argv[1]).resolve(strict=True);sys.path.insert(0,str(root));"
        "from scripts import release_control_receipt as r;"
        "from scripts import recovery_launcher as l;"
        "r.verify_recovery_capsule_root(root,expected_owner_key_fingerprint=sys.argv[2]);"
        "raw=r._read_regular(root/'recovery-execution-closure.json',"
        "label='recovery execution closure',max_bytes=l.MAX_CLOSURE_BYTES);"
        "value=l.verify_execution_closure(closure=raw,capsule_root=root,"
        "active_sys_path=[str(root)]);"
        "sys.stdout.buffer.write(r.canonical_json_bytes(value))"
    )
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [
            str(venv_python),
            "-I",
            "-B",
            "-c",
            code,
            str(capsule_root),
            expected_owner_key_fingerprint,
        ],
        check=False,
        capture_output=True,
        env=_bootstrap_environment(),
        timeout=300,
    )
    if (
        completed.returncode != 0
        or not completed.stdout
        or len(completed.stdout) > MAX_BOOTSTRAP_OUTPUT_BYTES
        or len(completed.stderr) > MAX_BOOTSTRAP_OUTPUT_BYTES
    ):
        raise RecoveryBootstrapError("recovery capsule full verification failed")
    value = _json_without_duplicates(completed.stdout, label="recovery closure verification")
    if value.get("validation_status") != "validated":
        raise RecoveryBootstrapError("recovery capsule full verification is not validated")
    return value


def bootstrap_recovery_environment(
    *,
    archive: Path,
    destination: Path,
    expected_archive_digest: str,
    expected_manifest_digest: str,
    expected_owner_key_fingerprint: str,
) -> dict[str, object]:
    """Authenticate, extract, build an offline venv, then verify the full closure."""

    if _path_digest(archive)[1] != _checked_digest(
        expected_archive_digest, label="expected recovery archive digest"
    ):
        raise RecoveryBootstrapError("recovery capsule archive digest mismatch")
    owner_fingerprint = _checked_digest(
        expected_owner_key_fingerprint,
        label="expected owner signing key fingerprint",
    )
    extract_recovery_capsule(archive=archive, destination=destination)
    capsule_root = destination.resolve(strict=True)
    _, _, closure = _trusted_capsule_inputs(
        capsule_root=capsule_root,
        expected_manifest_digest=expected_manifest_digest,
    )
    venv_root, venv_python, wheelhouse, requirements = _bootstrap_dependency_inputs(
        capsule_root=capsule_root,
        closure=closure,
    )
    _run_bootstrap_command(
        [sys.executable, "-I", "-m", "venv", "--copies", str(venv_root)],
        environment=_bootstrap_environment(),
    )
    if not venv_python.is_file() or venv_python.is_symlink():
        raise RecoveryBootstrapError(
            "recovery bootstrap did not create a regular Python executable"
        )
    _run_bootstrap_command(
        [
            str(venv_python),
            "-I",
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(requirements),
        ],
        environment=_bootstrap_environment(),
    )
    _run_bootstrap_command(
        [str(venv_python), "-I", "-m", "pip", "--isolated", "check"],
        environment=_bootstrap_environment(),
    )
    return _verify_with_bootstrapped_environment(
        venv_python=venv_python,
        capsule_root=capsule_root,
        expected_owner_key_fingerprint=owner_fingerprint,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--expected-archive-digest", required=True)
    parser.add_argument("--expected-manifest-digest", required=True)
    parser.add_argument("--expected-owner-key-fingerprint", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bootstrap_recovery_environment(
        archive=Path(args.archive),
        destination=Path(args.destination),
        expected_archive_digest=args.expected_archive_digest,
        expected_manifest_digest=args.expected_manifest_digest,
        expected_owner_key_fingerprint=args.expected_owner_key_fingerprint,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
