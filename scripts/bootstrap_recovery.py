#!/usr/bin/env python3
"""Safely extract a deterministic recovery capsule before closure verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
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
MAX_PYTHON_RUNTIME_MEMBERS = 32768
MAX_PYTHON_RUNTIME_FILE_BYTES = 512 * 1024 * 1024
MAX_PYTHON_RUNTIME_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_INSTALLED_ENVIRONMENT_MEMBERS = 32768
MAX_INSTALLED_ENVIRONMENT_FILE_BYTES = 512 * 1024 * 1024
MAX_INSTALLED_ENVIRONMENT_TOTAL_BYTES = 1024 * 1024 * 1024
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RECOVERY_PYTHON_PACKAGE_URL = (
    "https://github.com/actions/python-versions/releases/download/"
    "3.11.14-18393181605/python-3.11.14-linux-24.04-x64.tar.gz"
)
RECOVERY_PYTHON_PACKAGE_DIGEST = (
    "sha256:295c25eeb4fdad1ec9526a27fbd9b476d7c79b00547d74d809b306381d0796d5"
)
TRUSTED_RECOVERY_PYTHON_IDENTITIES = {
    ("linux", "x86_64"): frozenset(
        {
            (
                "sha256:dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8",
                "Python 3.11.14",
                "CPython",
                "3.11.14",
                "cp311",
            )
        }
    )
}
TRUSTED_OS_SANDBOX_IDENTITIES = {
    ("linux", "x86_64"): frozenset(
        {
            (
                "sha256:52231e1caf55bcbc667b269f49c63599a6f7db4767ae6a039580d0ff853db712",
                "bubblewrap 0.9.0",
            )
        }
    )
}


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
                descriptor = os.open(target, flags, 0o644)  # lgtm[py/overly-permissive-file] — recovery capsule asset: 0o644 is the required world-readable archive contract (secrets stay 0o600)
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
        "PYTHONDONTWRITEBYTECODE": "1",
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


def _python_runtime_tree_identity(root: Path) -> tuple[int, int, str]:
    if root.is_symlink() or not root.is_dir():
        raise RecoveryBootstrapError("recovery Python runtime tree root is unsafe")
    records: list[dict[str, object]] = []
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_symlink():
            raise RecoveryBootstrapError("recovery Python runtime tree contains a link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RecoveryBootstrapError("recovery Python runtime tree contains a special file")
        size, digest = _path_digest(path, maximum=MAX_PYTHON_RUNTIME_FILE_BYTES)
        total += size
        if (
            total > MAX_PYTHON_RUNTIME_TOTAL_BYTES
            or len(records) >= MAX_PYTHON_RUNTIME_MEMBERS
        ):
            raise RecoveryBootstrapError("recovery Python runtime tree is too large")
        mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        records.append(
            {
                "mode": f"{mode:04o}",
                "path": path.relative_to(root).as_posix(),
                "sha256": digest,
                "size_bytes": size,
            }
        )
    if not records:
        raise RecoveryBootstrapError("recovery Python runtime tree is empty")
    canonical = json.dumps(
        records, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return len(records), total, "sha256:" + hashlib.sha256(canonical).hexdigest()


def _installed_environment_tree_identity(environment_root: Path) -> tuple[int, int, str]:
    """Hash the exact installed site-packages tree without following links."""

    site_packages = environment_root / "lib" / "python3.11" / "site-packages"
    for root in (
        environment_root,
        environment_root / "lib",
        environment_root / "lib" / "python3.11",
        site_packages,
    ):
        if root.is_symlink() or not root.is_dir():
            raise RecoveryBootstrapError(
                "recovery installed environment site-packages root is unsafe"
            )
    records: list[dict[str, object]] = []
    total = 0
    for path in sorted(
        site_packages.rglob("*"),
        key=lambda item: item.relative_to(site_packages).as_posix(),
    ):
        if path.is_symlink():
            raise RecoveryBootstrapError("recovery installed environment contains a link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RecoveryBootstrapError(
                "recovery installed environment contains a special file"
            )
        size, digest = _path_digest(
            path, maximum=MAX_INSTALLED_ENVIRONMENT_FILE_BYTES
        )
        total += size
        if (
            total > MAX_INSTALLED_ENVIRONMENT_TOTAL_BYTES
            or len(records) >= MAX_INSTALLED_ENVIRONMENT_MEMBERS
        ):
            raise RecoveryBootstrapError("recovery installed environment is too large")
        mode = 0o755 if path.stat().st_mode & 0o111 else 0o644
        records.append(
            {
                "mode": f"{mode:04o}",
                "path": path.relative_to(site_packages).as_posix(),
                "sha256": digest,
                "size_bytes": size,
            }
        )
    if not records:
        raise RecoveryBootstrapError("recovery installed environment is empty")
    canonical = json.dumps(
        records, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return len(records), total, "sha256:" + hashlib.sha256(canonical).hexdigest()


def _extract_python_runtime(
    *, archive: Path, destination: Path, manifest: dict[str, object]
) -> Path:
    if archive.is_symlink() or not archive.is_file():
        raise RecoveryBootstrapError("recovery Python runtime archive is unsafe")
    if destination.exists() or destination.is_symlink():
        raise RecoveryBootstrapError("recovery Python runtime destination must be absent")
    destination.mkdir(mode=0o700, parents=True)
    seen: set[str] = set()
    total = 0
    try:
        with tarfile.open(archive, mode="r:gz") as source:
            members = source.getmembers()
            if not members or len(members) > MAX_PYTHON_RUNTIME_MEMBERS * 2:
                raise RecoveryBootstrapError(
                    "recovery Python runtime member cardinality is invalid"
                )
            for member in members:
                relative = _safe_member_path(member.name)
                name = relative.as_posix()
                if (
                    name in seen
                    or (
                        name != "bin"
                        and name != "lib"
                        and name != "bin/python3.11"
                        and not name.startswith("lib/")
                    )
                ):
                    raise RecoveryBootstrapError(
                        "recovery Python runtime member inventory is unsafe"
                    )
                seen.add(name)
                if member.pax_headers or member.sparse is not None:
                    raise RecoveryBootstrapError(
                        "recovery Python runtime uses extended metadata"
                    )
                if member.uid != 0 or member.gid != 0 or member.mtime != 0:
                    raise RecoveryBootstrapError(
                        "recovery Python runtime metadata is nondeterministic"
                    )
                target = destination.joinpath(*relative.parts)
                if member.isdir():
                    if member.mode != 0o755:
                        raise RecoveryBootstrapError(
                            "recovery Python runtime directory mode is invalid"
                        )
                    target.mkdir(mode=0o755, parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                if not member.isreg() or member.mode not in {0o644, 0o755}:
                    raise RecoveryBootstrapError(
                        "recovery Python runtime link or mode is forbidden"
                    )
                if member.size < 0 or member.size > MAX_PYTHON_RUNTIME_FILE_BYTES:
                    raise RecoveryBootstrapError(
                        "recovery Python runtime member size is invalid"
                    )
                total += member.size
                if total > MAX_PYTHON_RUNTIME_TOTAL_BYTES:
                    raise RecoveryBootstrapError("recovery Python runtime is too large")
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                extracted = source.extractfile(member)
                if extracted is None:
                    raise RecoveryBootstrapError(
                        "recovery Python runtime member body is absent"
                    )
                descriptor = os.open(
                    target,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                written = 0
                with os.fdopen(descriptor, "wb") as output:
                    while chunk := extracted.read(1024 * 1024):
                        written += len(chunk)
                        if written > member.size:
                            raise RecoveryBootstrapError(
                                "recovery Python runtime member exceeded its size"
                            )
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                extracted.close()
                if written != member.size:
                    raise RecoveryBootstrapError(
                        "recovery Python runtime member ended early"
                    )
                target.chmod(member.mode)
        identity = _python_runtime_tree_identity(destination)
        expected = (
            manifest.get("runtime_file_count"),
            manifest.get("runtime_total_size_bytes"),
            manifest.get("runtime_tree_sha256"),
        )
        if identity != expected:
            raise RecoveryBootstrapError("recovery Python runtime tree identity mismatch")
        executable = destination / "bin" / "python3.11"
        if (
            executable.is_symlink()
            or not executable.is_file()
            or _path_digest(executable)[1] != manifest.get("python_executable_sha256")
        ):
            raise RecoveryBootstrapError(
                "recovery Python runtime executable identity mismatch"
            )
        executable.chmod(0o500)
        return executable
    except Exception:
        # Retain authenticated-but-invalid bytes for forensic inspection; no
        # staged Python is executed unless this function returns successfully.
        raise


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
) -> tuple[
    Path,
    Path,
    Path,
    Path,
    Path,
    Path,
    dict[str, object],
    dict[str, object],
]:
    runtime = closure.get("python_runtime")
    if type(runtime) is not dict or runtime != {
        "implementation": "CPython",
        "version": "3.11.14",
        "abi": "cp311",
    }:
        raise RecoveryBootstrapError("recovery bootstrap Python runtime mismatch")
    declared_sys_path = closure.get("sys_path")
    if (
        type(declared_sys_path) is not list
        or not declared_sys_path
        or declared_sys_path[0] != str(capsule_root)
        or any(type(item) is not str for item in declared_sys_path)
        or len(declared_sys_path) != len(set(cast(list[str], declared_sys_path)))
    ):
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
    runtime_root = capsule_root.parent / "recovery-runtime"
    base_root = runtime_root / "base"
    environment_root = runtime_root / "environment"
    venv_python = environment_root / "bin" / "python"
    python_item = python_items[0]
    if python_item.get("path") != str(venv_python):
        raise RecoveryBootstrapError("recovery bootstrap Python path mismatch")
    if runtime_root.exists() or runtime_root.is_symlink():
        raise RecoveryBootstrapError("recovery capsule contains a prebuilt execution environment")
    expected_python = python_item.get("sha256")
    python_identity = (
        expected_python,
        python_item.get("version"),
        runtime.get("implementation"),
        runtime.get("version"),
        runtime.get("abi"),
    )
    trusted_python = TRUSTED_RECOVERY_PYTHON_IDENTITIES.get(
        (sys.platform, platform.machine()), frozenset()
    )
    if python_identity not in trusted_python:
        raise RecoveryBootstrapError(
            "recovery bootstrap Python is not a Kestrel-trusted platform identity"
        )
    lock = closure.get("dependency_lock")
    if type(lock) is not dict or lock.get("requirements_path") != "recovery/requirements.txt":
        raise RecoveryBootstrapError("recovery dependency lock is invalid")
    requirements = capsule_root / "recovery" / "requirements.txt"
    environment_manifest_path = capsule_root / "recovery" / "environment-manifest.json"
    wheelhouse_manifest = capsule_root / "recovery" / "wheelhouse-manifest.json"
    runtime_manifest = capsule_root / "recovery" / "runtime-manifest.json"
    python_runtime_manifest = capsule_root / "recovery" / "python-runtime-manifest.json"
    python_runtime_archive = capsule_root / "recovery" / "python-runtime.tar.gz"
    for path, field, label in (
        (requirements, "requirements_sha256", "requirements lock"),
        (
            environment_manifest_path,
            "environment_manifest_sha256",
            "installed environment manifest",
        ),
        (wheelhouse_manifest, "wheelhouse_manifest_sha256", "wheelhouse manifest"),
        (runtime_manifest, "runtime_manifest_sha256", "runtime manifest"),
        (
            python_runtime_manifest,
            "python_runtime_manifest_sha256",
            "Python runtime manifest",
        ),
        (
            python_runtime_archive,
            "python_runtime_archive_sha256",
            "Python runtime archive",
        ),
    ):
        expected_digest = lock.get(field)
        maximum = (
            MAX_PYTHON_RUNTIME_TOTAL_BYTES
            if field == "python_runtime_archive_sha256"
            else 16 * 1024 * 1024
        )
        if type(expected_digest) is not str or _path_digest(path, maximum=maximum)[1] != (
            _checked_digest(expected_digest, label=f"recovery {label} digest")
        ):
            raise RecoveryBootstrapError(f"recovery {label} digest mismatch")
    environment_manifest = _json_without_duplicates(
        environment_manifest_path.read_bytes(),
        label="recovery installed environment manifest",
    )
    if set(environment_manifest) != {
        "schema",
        "platform",
        "python_version",
        "python_abi",
        "environment_root",
        "site_packages_path",
        "site_packages_tree_sha256",
        "site_packages_file_count",
        "site_packages_total_size_bytes",
    }:
        raise RecoveryBootstrapError("recovery installed environment manifest fields mismatch")
    if (
        environment_manifest.get("schema") != "kestrel.recovery_environment.v1"
        or environment_manifest.get("platform") != "ubuntu-24.04-x86_64"
        or environment_manifest.get("python_version") != runtime.get("version")
        or environment_manifest.get("python_abi") != runtime.get("abi")
        or environment_manifest.get("environment_root") != str(environment_root)
        or environment_manifest.get("site_packages_path")
        != str(environment_root / "lib" / "python3.11" / "site-packages")
        or any(
            type(environment_manifest.get(field)) is not int
            or isinstance(environment_manifest.get(field), bool)
            or cast(int, environment_manifest[field]) <= 0
            for field in (
                "site_packages_file_count",
                "site_packages_total_size_bytes",
            )
        )
        or type(environment_manifest.get("site_packages_tree_sha256")) is not str
    ):
        raise RecoveryBootstrapError("recovery installed environment manifest identity mismatch")
    _checked_digest(
        cast(str, environment_manifest["site_packages_tree_sha256"]),
        label="recovery installed environment tree digest",
    )
    python_manifest = _json_without_duplicates(
        python_runtime_manifest.read_bytes(), label="recovery Python runtime manifest"
    )
    if set(python_manifest) != {
        "schema",
        "platform",
        "python_version",
        "python_abi",
        "python_executable_path",
        "python_executable_sha256",
        "source_archive_url",
        "source_archive_sha256",
        "runtime_archive_path",
        "runtime_archive_sha256",
        "runtime_archive_size_bytes",
        "runtime_tree_sha256",
        "runtime_file_count",
        "runtime_total_size_bytes",
    }:
        raise RecoveryBootstrapError("recovery Python runtime manifest fields mismatch")
    if (
        python_manifest.get("schema") != "kestrel.recovery_python_runtime.v1"
        or python_manifest.get("platform") != "ubuntu-24.04-x86_64"
        or python_manifest.get("python_version") != "3.11.14"
        or python_manifest.get("python_abi") != "cp311"
        or python_manifest.get("python_executable_path") != "bin/python3.11"
        or python_manifest.get("python_executable_sha256") != expected_python
        or python_manifest.get("source_archive_url") != RECOVERY_PYTHON_PACKAGE_URL
        or python_manifest.get("source_archive_sha256")
        != RECOVERY_PYTHON_PACKAGE_DIGEST
        or python_manifest.get("runtime_archive_path")
        != "recovery/python-runtime.tar.gz"
        or python_manifest.get("runtime_archive_sha256")
        != lock.get("python_runtime_archive_sha256")
        or _path_digest(
            python_runtime_archive, maximum=MAX_PYTHON_RUNTIME_TOTAL_BYTES
        )
        != (
            python_manifest.get("runtime_archive_size_bytes"),
            python_manifest.get("runtime_archive_sha256"),
        )
        or any(
            type(python_manifest.get(field)) is not int
            or isinstance(python_manifest.get(field), bool)
            or cast(int, python_manifest[field]) <= 0
            for field in (
                "runtime_archive_size_bytes",
                "runtime_file_count",
                "runtime_total_size_bytes",
            )
        )
        or type(python_manifest.get("runtime_tree_sha256")) is not str
    ):
        raise RecoveryBootstrapError("recovery Python runtime manifest identity mismatch")
    _checked_digest(
        cast(str, python_manifest["runtime_tree_sha256"]),
        label="recovery Python runtime tree digest",
    )
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
    return (
        runtime_root,
        base_root,
        environment_root,
        venv_python,
        wheelhouse,
        requirements,
        python_manifest,
        environment_manifest,
    )


def _verify_installed_environment(
    *, environment_root: Path, manifest: dict[str, object]
) -> None:
    """Match and freeze the controller-bound installed site-packages tree."""

    identity = _installed_environment_tree_identity(environment_root)
    expected = (
        manifest.get("site_packages_file_count"),
        manifest.get("site_packages_total_size_bytes"),
        manifest.get("site_packages_tree_sha256"),
    )
    if identity != expected:
        raise RecoveryBootstrapError("recovery installed environment tree identity mismatch")
    site_packages = environment_root / "lib" / "python3.11" / "site-packages"
    for path in sorted(
        site_packages.rglob("*"),
        key=lambda item: (len(item.relative_to(site_packages).parts), item.as_posix()),
        reverse=True,
    ):
        if path.is_file() and not path.is_symlink():
            path.chmod(0o555 if path.stat().st_mode & 0o111 else 0o444)
        elif path.is_dir() and not path.is_symlink():
            path.chmod(0o555)
        else:
            raise RecoveryBootstrapError("recovery installed environment changed while freezing")
    site_packages.chmod(0o555)
    if _installed_environment_tree_identity(environment_root) != identity:
        raise RecoveryBootstrapError("recovery installed environment changed while freezing")


def _prepare_trusted_runtime_files(
    *, capsule_root: Path, closure: dict[str, object]
) -> None:
    """Verify and freeze exact dynamic-loader/runtime bytes before sandbox use."""

    manifest_path = capsule_root / "recovery" / "runtime-manifest.json"
    manifest = _json_without_duplicates(
        manifest_path.read_bytes(), label="recovery runtime manifest"
    )
    if set(manifest) != {
        "schema",
        "platform",
        "python_version",
        "python_executable_sha256",
        "files",
    }:
        raise RecoveryBootstrapError("recovery runtime manifest fields mismatch")
    executables = closure.get("external_executables")
    if type(executables) is not list:
        raise RecoveryBootstrapError("recovery runtime Python identity is absent")
    python_items = [
        item for item in executables if type(item) is dict and item.get("name") == "python"
    ]
    runtime = closure.get("python_runtime")
    if (
        len(python_items) != 1
        or type(runtime) is not dict
        or manifest.get("schema") != "kestrel.recovery_runtime.v1"
        or manifest.get("platform") != "ubuntu-24.04-x86_64"
        or manifest.get("python_version") != runtime.get("version")
        or manifest.get("python_executable_sha256") != python_items[0].get("sha256")
    ):
        raise RecoveryBootstrapError("recovery runtime manifest identity mismatch")
    files = manifest.get("files")
    if type(files) is not list or not files or closure.get("runtime_files") != files:
        raise RecoveryBootstrapError("recovery runtime closure binding mismatch")
    previous = ""
    seen_assets: set[str] = set()
    seen_basenames: set[str] = set()
    loaders: list[Path] = []
    for raw_item in files:
        if type(raw_item) is not dict or set(raw_item) != {
            "asset_path",
            "sandbox_path",
            "sha256",
            "size_bytes",
        }:
            raise RecoveryBootstrapError("recovery runtime file fields mismatch")
        asset_name = raw_item.get("asset_path")
        sandbox_path = raw_item.get("sandbox_path")
        size = raw_item.get("size_bytes")
        digest = raw_item.get("sha256")
        if (
            type(asset_name) is not str
            or type(sandbox_path) is not str
            or not sandbox_path.startswith("/")
            or sandbox_path <= previous
            or sandbox_path.startswith(("/dev/", "/proc/", "/sys/", "/tmp/"))  # nosec B108
            or type(size) is not int
            or isinstance(size, bool)
            or size <= 0
            or type(digest) is not str
            or asset_name in seen_assets
            or PurePosixPath(asset_name).name in seen_basenames
        ):
            raise RecoveryBootstrapError("recovery runtime file identity is invalid")
        relative = _safe_member_path(asset_name)
        if (
            len(relative.parts) != 3
            or relative.parts[:2] != ("recovery", "runtime")
        ):
            raise RecoveryBootstrapError("recovery runtime asset path is invalid")
        path = capsule_root / relative
        if path.name != Path(sandbox_path).name:
            raise RecoveryBootstrapError("recovery runtime basename binding mismatch")
        if (
            path.is_symlink()
            or not path.is_file()
            or _path_digest(path) != (
                size,
                _checked_digest(digest, label="recovery runtime file digest"),
            )
        ):
            raise RecoveryBootstrapError("recovery runtime file identity mismatch")
        if path.name.startswith("ld-linux-"):
            path.chmod(0o500)
            loaders.append(path)
        else:
            path.chmod(0o400)
        seen_assets.add(asset_name)
        seen_basenames.add(path.name)
        previous = sandbox_path
    runtime_root = capsule_root / "recovery" / "runtime"
    actual = {
        path.name
        for path in runtime_root.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if actual != seen_basenames or len(loaders) != 1 or any(
        path.is_dir() or path.is_symlink() for path in runtime_root.iterdir()
    ):
        raise RecoveryBootstrapError("recovery private runtime inventory is not exact")


def _private_loader_command(
    *,
    capsule_root: Path,
    executable: Path,
    arguments: Sequence[str],
    additional_library_roots: Sequence[Path] = (),
    preload_path: Path = Path("/etc/ld.so.preload"),
) -> list[str]:
    if preload_path.exists() or preload_path.is_symlink():
        raise RecoveryBootstrapError(
            "global dynamic-loader preload is outside the recovery bootstrap TCB"
        )
    runtime_root = (capsule_root / "recovery" / "runtime").resolve(strict=True)
    if any(path.is_dir() or path.is_symlink() for path in runtime_root.iterdir()):
        raise RecoveryBootstrapError("private recovery runtime search path is unsafe")
    loaders = [
        path
        for path in runtime_root.iterdir()
        if path.is_file() and path.name.startswith("ld-linux-")
    ]
    if len(loaders) != 1:
        raise RecoveryBootstrapError("private recovery dynamic loader is ambiguous")
    roots = [runtime_root]
    for raw_root in additional_library_roots:
        if raw_root.is_symlink() or not raw_root.is_dir():
            raise RecoveryBootstrapError("private recovery library root is unsafe")
        root = raw_root.resolve(strict=True)
        if root in roots:
            raise RecoveryBootstrapError("private recovery library root is duplicated")
        roots.append(root)
    allowed = {
        path.resolve(strict=True)
        for root in roots
        for path in root.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    checked_executable = executable.resolve(strict=True)
    library_path = ":".join(str(root) for root in roots)
    loader = loaders[0]
    completed = subprocess.run(  # noqa: S603  # nosec B603
        [
            str(loader),
            "--inhibit-cache",
            "--library-path",
            library_path,
            "--list",
            str(checked_executable),
        ],
        check=False,
        capture_output=True,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
        text=True,
        timeout=10,
    )
    combined = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if (
        completed.returncode != 0
        or "not found" in combined
        or len(combined.encode()) > MAX_BOOTSTRAP_OUTPUT_BYTES
    ):
        raise RecoveryBootstrapError("private recovery loader preflight failed")
    resolved: set[Path] = set()
    for raw_line in completed.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("linux-vdso.so"):
            continue
        token = (
            line.split("=>", 1)[1].strip().split(None, 1)[0]
            if "=>" in line
            else line.split(None, 1)[0]
        )
        path = Path(token)
        try:
            dependency = path.resolve(strict=True)
        except OSError as exc:
            raise RecoveryBootstrapError(
                "private recovery loader dependency is missing"
            ) from exc
        if not path.is_absolute() or dependency not in allowed or dependency.parent not in roots:
            raise RecoveryBootstrapError(
                "private recovery loader resolved an ambient dependency"
            )
        resolved.add(dependency)
    if loader not in resolved:
        raise RecoveryBootstrapError("private recovery loader identity was not resolved")
    return [
        str(loader),
        "--inhibit-cache",
        "--library-path",
        library_path,
        str(checked_executable),
        *arguments,
    ]


def _prepare_trusted_os_sandbox(*, capsule_root: Path, closure: dict[str, object]) -> Path:
    """Make only the independently pinned capsule sandbox executable."""

    executables = closure.get("external_executables")
    resources = closure.get("data_resources")
    if type(executables) is not list or type(resources) is not list:
        raise RecoveryBootstrapError("recovery bootstrap sandbox inventory is invalid")
    sandbox_items = [
        item for item in executables if type(item) is dict and item.get("name") == "sandbox"
    ]
    if len(sandbox_items) != 1:
        raise RecoveryBootstrapError("Kestrel-trusted recovery sandbox is absent or ambiguous")
    sandbox = sandbox_items[0]
    expected_path = capsule_root / "recovery" / "bin" / "bwrap"
    if sandbox.get("path") != str(expected_path):
        raise RecoveryBootstrapError("recovery sandbox path is outside the capsule")
    digest = sandbox.get("sha256")
    version = sandbox.get("version")
    if type(digest) is not str or type(version) is not str:
        raise RecoveryBootstrapError("recovery sandbox identity is invalid")
    checked_digest = _checked_digest(digest, label="recovery sandbox digest")
    trusted = TRUSTED_OS_SANDBOX_IDENTITIES.get(
        (sys.platform, platform.machine()), frozenset()
    )
    if (checked_digest, version) not in trusted:
        raise RecoveryBootstrapError(
            "recovery sandbox is not a Kestrel-trusted platform identity"
        )
    resource_items = [
        item
        for item in resources
        if type(item) is dict and item.get("path") == "recovery/bin/bwrap"
    ]
    if len(resource_items) != 1 or resource_items[0].get("sha256") != checked_digest:
        raise RecoveryBootstrapError("recovery sandbox is not bound as a capsule resource")
    if (
        not expected_path.is_file()
        or expected_path.is_symlink()
        or _path_digest(expected_path)[1] != checked_digest
    ):
        raise RecoveryBootstrapError("recovery sandbox binary identity mismatch")
    expected_path.chmod(0o500)
    return expected_path


def _verify_with_bootstrapped_environment(
    *,
    venv_python: Path,
    base_root: Path,
    capsule_root: Path,
    expected_owner_key_fingerprint: str,
) -> dict[str, object]:
    code = (
        "import json,platform,sys;from pathlib import Path;"
        "root=Path(sys.argv[1]).resolve(strict=True);"
        "closure=json.load(open(root/'recovery-execution-closure.json'));"
        "runtime={'implementation':platform.python_implementation(),"
        "'version':platform.python_version(),"
        "'abi':f'cp{sys.version_info.major}{sys.version_info.minor}'};"
        "sys.path[:]=closure['sys_path'];"
        "from scripts import release_control_receipt as r;"
        "from scripts import recovery_launcher as l;"
        "r.verify_recovery_capsule_root(root,expected_owner_key_fingerprint=sys.argv[2]);"
        "raw=r._read_regular(root/'recovery-execution-closure.json',"
        "label='recovery execution closure',max_bytes=l.MAX_CLOSURE_BYTES);"
        "value=l.verify_execution_closure(closure=raw,capsule_root=root,"
        "active_sys_path=sys.path,active_python_runtime=runtime);"
        "sys.stdout.buffer.write(r.canonical_json_bytes(value))"
    )
    command = _private_loader_command(
        capsule_root=capsule_root,
        executable=venv_python,
        arguments=(
            "-I",
            "-S",
            "-B",
            "-c",
            code,
            str(capsule_root),
            expected_owner_key_fingerprint,
        ),
        additional_library_roots=(base_root / "lib",),
    )
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
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
    (
        _runtime_root,
        base_root,
        environment_root,
        venv_python,
        wheelhouse,
        requirements,
        python_manifest,
        environment_manifest,
    ) = _bootstrap_dependency_inputs(capsule_root=capsule_root, closure=closure)
    _prepare_trusted_os_sandbox(capsule_root=capsule_root, closure=closure)
    _prepare_trusted_runtime_files(capsule_root=capsule_root, closure=closure)
    base_python = _extract_python_runtime(
        archive=capsule_root / "recovery" / "python-runtime.tar.gz",
        destination=base_root,
        manifest=python_manifest,
    )
    create_command = _private_loader_command(
        capsule_root=capsule_root,
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
    )
    _run_bootstrap_command(
        create_command,
        environment=_bootstrap_environment(),
    )
    expected_python_digest = cast(str, python_manifest["python_executable_sha256"])
    if (
        not venv_python.is_file()
        or venv_python.is_symlink()
        or _path_digest(venv_python)[1] != expected_python_digest
    ):
        raise RecoveryBootstrapError(
            "recovery bootstrap did not create a regular Python executable"
        )
    _run_bootstrap_command(
        _private_loader_command(
            capsule_root=capsule_root,
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
        environment=_bootstrap_environment(),
    )
    _run_bootstrap_command(
        _private_loader_command(
            capsule_root=capsule_root,
            executable=venv_python,
            arguments=("-I", "-B", "-m", "pip", "--isolated", "check"),
            additional_library_roots=(base_root / "lib",),
        ),
        environment=_bootstrap_environment(),
    )
    _verify_installed_environment(
        environment_root=environment_root,
        manifest=environment_manifest,
    )
    return _verify_with_bootstrapped_environment(
        venv_python=venv_python,
        base_root=base_root,
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
