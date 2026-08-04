#!/usr/bin/env python3
"""Build a digest-bound frozen desktop sidecar from an exact clean commit."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import stat
import subprocess
import sys
import tomllib
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

PYINSTALLER_VERSION = "6.21.0"
RECEIPT_SCHEMA = "kestrel.desktop.sidecar-build.v1"
MAX_RECEIPT_BYTES = 64 * 1024
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DISTRIBUTION_METADATA_RE = re.compile(
    r"nested_memvid_agent-[^/]+\.dist-info/METADATA"
)

REQUIRED_RUNTIME_ROOTS = (
    "packaging/kestrel-sidecar-entry.py",
    "packaging/kestrel-sidecar.spec",
    "src/nested_memvid_agent/desktop_sidecar.py",
    "src/nested_memvid_agent/desktop_memory_health.py",
    "src/nested_memvid_agent/backends/memvid_backend.py",
    "src/nested_memvid_agent/llm/mock.py",
    "src/nested_memvid_agent/server.py",
    "src/nested_memvid_agent/server_desktop_routes.py",
    "web/dist/index.html",
    "web/public/THIRD_PARTY_NOTICES.txt",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
)
FORBIDDEN_RUNTIME_ROOTS = (
    "src/nested_memvid_agent/qrcode",
    "src/nested_memvid_agent/qr",
    "src/nested_memvid_agent/video_frames",
    "src/nested_memvid_agent/memvid_v1",
)
REQUIRED_BUNDLED_DISTRIBUTIONS = (
    "anthropic",
    "click",
    "cryptography",
    "fastapi",
    "google-genai",
    "httplib2",
    "keyring",
    "mcp",
    "memvid-sdk",
    "nested-memvid-agent",
    "numpy",
    "openai",
    "pillow",
    "pydantic",
    "pydantic-settings",
    "pygments",
    "python-multipart",
    "pyyaml",
    "starlette",
    "tzdata",
    "uvicorn",
)
REQUIRED_FROZEN_ARCHIVE_MEMBERS = (
    "anthropic",
    "fastapi",
    "google.genai",
    "keyring.backends",
    "mcp.client",
    "memvid_sdk",
    "nested_memvid_agent.desktop_sidecar",
    "nested_memvid_agent.llm.provider_http_worker",
    "nested_memvid_agent/prompts/system_prompt.md",
    "nested_memvid_agent/web_dist/index.html",
    "openai",
    "uvicorn",
)
_WEB_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "kind",
        "source_commit",
        "root",
        "lock_sha256",
        "files",
    }
)
_FORBIDDEN_WEB_NAMES = frozenset(
    {
        ".cache",
        ".env",
        ".nest",
        "__pycache__",
        "credentials.json",
        "qrcode",
        "tests",
        "video_frames",
        "vite.config.js",
        "vite.config.ts",
    }
)


def _run_git(source_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=source_root,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"git inspection failed: {detail}")
    return completed.stdout.strip()


def _require_commit(value: str, *, field: str) -> str:
    normalized = value.strip().lower()
    if not _COMMIT_RE.fullmatch(normalized):
        raise ValueError(f"{field} must be an exact 40-character lowercase commit")
    return normalized


def validate_source_checkout(
    source_root: Path,
    *,
    expected_commit: str,
) -> str:
    """Require a real Git worktree at the exact clean source commit."""

    root = source_root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("source root must be a directory")
    expected = _require_commit(expected_commit, field="expected source commit")
    actual = _require_commit(
        _run_git(root, "rev-parse", "--verify", "HEAD^{commit}"),
        field="source commit",
    )
    if actual != expected:
        raise ValueError(
            f"source commit mismatch: expected {expected}, found {actual}"
        )
    status = _run_git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise ValueError("source checkout must be clean, including untracked files")
    return actual


def validate_upx_disabled() -> None:
    """Reject environment-based UPX discovery."""

    configured = [name for name in ("UPX", "UPX_DIR") if os.environ.get(name)]
    if configured:
        raise ValueError(f"UPX must be disabled; remove {', '.join(configured)}")


def validate_pyinstaller_version(version: str) -> str:
    normalized = version.strip()
    if normalized != PYINSTALLER_VERSION:
        raise ValueError(
            f"PyInstaller must be exactly {PYINSTALLER_VERSION}, found {normalized}"
        )
    return normalized


def validate_packaging_runtime_roots(source_root: Path) -> None:
    """Fail closed when a required runtime input is absent or legacy v1 appears."""

    root = source_root.resolve(strict=True)
    for relative in REQUIRED_RUNTIME_ROOTS:
        candidate = root / relative
        if not candidate.is_file() or candidate.is_symlink():
            raise ValueError(f"missing runtime root: {relative}")
    for relative in FORBIDDEN_RUNTIME_ROOTS:
        candidate = root / relative
        if candidate.exists() or candidate.is_symlink():
            raise ValueError(f"forbidden runtime root: {relative}")


def _forbidden_frozen_archive_member(name: str) -> bool:
    normalized = unicodedata.normalize("NFKC", name).casefold().replace("\\", "/")
    path_components = normalized.split("/")
    module_components = [
        component
        for path_component in path_components
        for component in path_component.split(".")
        if component
    ]
    return (
        normalized.endswith(".map")
        or any(
            component in {
                ".cache",
                ".nest",
                "__pycache__",
                "benchmark",
                "benchmarks",
                "credentials.json",
                "memvid_v1",
                "pytest",
                "qr",
                "qrcode",
                "test",
                "tests",
                "video_frames",
            }
            or component == ".env"
            or component.startswith(".env.")
            for component in path_components
        )
        or any(
            component
            in {
                "_pytest",
                "benchmark",
                "benchmarks",
                "memvid_v1",
                "pytest",
                "qr",
                "qrcode",
                "test",
                "tests",
                "video_frames",
            }
            or component.startswith("test_")
            or component.startswith("_test_")
            for component in module_components
        )
        or normalized == "mcp.cli"
        or normalized.startswith("mcp.cli.")
    )


def validate_frozen_archive_listing(listing: str) -> tuple[str, ...]:
    """Require the frozen core and reject development or private payload roots."""

    members = tuple(
        line.strip()
        for line in listing.splitlines()
        if line.strip()
        and not line.startswith("Options in ")
        and not line.startswith("Contents of ")
    )
    if not members:
        raise ValueError("frozen archive inventory is empty")
    for member in members:
        if _forbidden_frozen_archive_member(member):
            raise ValueError(f"forbidden frozen archive member: {member}")
    if not any(_DISTRIBUTION_METADATA_RE.fullmatch(member) for member in members):
        raise ValueError(
            "frozen archive is missing Kestrel distribution metadata"
        )
    missing = sorted(set(REQUIRED_FROZEN_ARCHIVE_MEMBERS).difference(members))
    if missing:
        raise ValueError(
            "frozen archive is missing required runtime members: "
            + ", ".join(missing)
        )
    return members


def validate_frozen_binary_inventory(binary_path: Path) -> tuple[str, ...]:
    """Inspect the exact PyInstaller archive before issuing its build receipt."""

    binary = binary_path.resolve(strict=True)
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller.utils.cliutils.archive_viewer",
            "--recursive",
            "--brief",
            str(binary),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"could not inspect frozen archive: {detail}")
    return validate_frozen_archive_listing(completed.stdout)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(_read_regular_bounded(path)).hexdigest()


def _read_regular_bounded(
    path: Path,
    *,
    maximum: int | None = None,
) -> bytes:
    candidate = Path(os.path.abspath(path))
    before = os.lstat(candidate)
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
    ):
        raise ValueError(f"not a unique regular file: {path}")
    if maximum is not None and before.st_size > maximum:
        raise ValueError(f"file exceeds {maximum} bytes: {path}")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        # O_BINARY: without it Windows opens in text mode and translates
        # \r\n -> \n, so the bytes read no longer match st_size and the digest
        # is computed over translated content. Binary mode keeps read == size.
        | getattr(os, "O_BINARY", 0)
    )
    descriptor = os.open(candidate, flags)
    chunks: list[bytes] = []
    digest = hashlib.sha256()
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != before.st_dev
            or opened.st_ino != before.st_ino
            or opened.st_size != before.st_size
        ):
            raise ValueError(f"file changed during open: {path}")
        expected = opened.st_size
        read_total = 0
        # Read to real EOF rather than trusting the pre-read size to bound the
        # loop: Windows reports a stale st_size under delayed write-back, so a
        # fixed-count loop can hit a legitimate short/empty read and misread it
        # as truncation. EOF terminates; we then reconcile the byte count.
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            digest.update(chunk)
            read_total += len(chunk)
        if read_total != expected:
            raise ValueError(f"file changed during read: {path}")
        after = os.lstat(candidate)
        # Identity invariant: same file (dev+ino), still a unique regular
        # file, unchanged size. mtime_ns is deliberately excluded — Windows
        # has coarse timestamp granularity and can jitter mtime on read,
        # which false-positives the guard. dev/ino/size/nlink are the real
        # tamper/replacement signals; a changed mtime with identical
        # dev/ino/size/nlink is not a substitution.
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or opened.st_dev != after.st_dev
            or opened.st_ino != after.st_ino
            or opened.st_size != after.st_size
        ):
            raise ValueError(f"file changed during read: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return value


def _canonical_distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).casefold()


def validate_bundled_runtime_distributions(
    python_lock_path: Path,
) -> dict[str, str]:
    """Require every bundled core distribution at a version present in uv.lock."""

    try:
        lock = tomllib.loads(_read_regular_bounded(python_lock_path).decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("uv.lock is invalid") from exc
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError("uv.lock has no package records")
    locked_versions: dict[str, set[str]] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise ValueError("uv.lock contains an invalid package record")
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError("uv.lock contains an invalid package identity")
        locked_versions.setdefault(
            _canonical_distribution_name(name),
            set(),
        ).add(version)

    validated: dict[str, str] = {}
    for distribution in REQUIRED_BUNDLED_DISTRIBUTIONS:
        normalized = _canonical_distribution_name(distribution)
        expected = locked_versions.get(normalized)
        if not expected:
            raise ValueError(f"{distribution} is absent from uv.lock")
        try:
            installed = importlib.metadata.version(distribution)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError(
                f"{distribution} is required for the fully bundled runtime"
            ) from exc
        if installed not in expected:
            raise ValueError(
                f"{distribution} version {installed} is not present in uv.lock"
            )
        validated[distribution] = installed
    return validated


def _canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(value),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _inventory_web_root(root: Path) -> dict[str, dict[str, object]]:
    metadata = os.lstat(root)
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError("web asset root must be a real directory")
    files: dict[str, dict[str, object]] = {}
    portable_paths: dict[str, str] = {}

    def visit(directory: Path, parts: tuple[str, ...]) -> None:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda entry: entry.name)
        for entry in entries:
            child_parts = (*parts, entry.name)
            relative = "/".join(child_parts)
            for length in range(1, len(child_parts) + 1):
                prefix = "/".join(child_parts[:length])
                folded = unicodedata.normalize("NFKC", prefix).casefold()
                previous = portable_paths.get(folded)
                if previous is not None and previous != prefix:
                    raise ValueError(
                        "web asset inventory contains case-colliding paths: "
                        f"{previous}, {prefix}"
                    )
                portable_paths[folded] = prefix
            candidate = Path(entry.path)
            candidate_metadata = os.lstat(candidate)
            if stat.S_ISLNK(candidate_metadata.st_mode):
                raise ValueError(f"web asset inventory contains a symlink: {relative}")
            if stat.S_ISDIR(candidate_metadata.st_mode):
                visit(candidate, child_parts)
                continue
            if not stat.S_ISREG(candidate_metadata.st_mode):
                raise ValueError(
                    f"web asset inventory contains a special file: {relative}"
                )
            folded_parts = tuple(
                unicodedata.normalize("NFKC", part).casefold()
                for part in child_parts
            )
            if (
                any(
                    part in _FORBIDDEN_WEB_NAMES or part.startswith(".env")
                    for part in folded_parts
                )
                or relative.casefold().endswith(".map")
            ):
                raise ValueError(f"forbidden web asset payload: {relative}")
            payload = _read_regular_bounded(candidate)
            files[relative] = {
                "size": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            }

    visit(root, ())
    if "index.html" not in files:
        raise ValueError("web asset inventory is missing index.html")
    return files


def validate_web_build_receipt(
    receipt_path: Path,
    *,
    source_commit: str,
    web_lock_path: Path,
    expected_root: Path,
) -> str:
    """Verify exact source, lock, and file coverage for ignored web/dist assets."""

    payload = _read_regular_bounded(receipt_path, maximum=MAX_RECEIPT_BYTES - 1)
    if len(payload) >= MAX_RECEIPT_BYTES:
        raise ValueError("web build receipt exceeds 64 KiB")
    try:
        receipt = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("web build receipt is invalid JSON") from exc
    if not isinstance(receipt, dict) or set(receipt) != _WEB_RECEIPT_KEYS:
        raise ValueError("web build receipt fields mismatch")
    if _canonical_json_bytes(receipt) != payload:
        raise ValueError("web build receipt is not canonical")
    if receipt["schema"] != "kestrel.desktop.asset-build.v1":
        raise ValueError("web build receipt schema mismatch")
    if receipt["kind"] != "web":
        raise ValueError("web build receipt kind mismatch")
    expected_commit = _require_commit(source_commit, field="source commit")
    if receipt["source_commit"] != expected_commit:
        raise ValueError("web build receipt source commit mismatch")
    root = expected_root.resolve(strict=True)
    if receipt["root"] != str(root):
        raise ValueError("web build receipt root mismatch")
    lock_digest = _sha256_file(web_lock_path)
    if _require_sha256(receipt["lock_sha256"], field="web lock digest") != lock_digest:
        raise ValueError("web build receipt lock digest mismatch")
    actual_files = _inventory_web_root(root)
    if receipt["files"] != actual_files:
        raise ValueError("web build receipt inventory mismatch")
    return hashlib.sha256(payload).hexdigest()


def create_web_build_receipt(
    *,
    source_commit: str,
    web_lock_path: Path,
    web_root: Path,
) -> dict[str, Any]:
    """Create the canonical inventory receipt after an exact npm build."""

    root = web_root.resolve(strict=True)
    return {
        "schema": "kestrel.desktop.asset-build.v1",
        "kind": "web",
        "source_commit": _require_commit(source_commit, field="source commit"),
        "root": str(root),
        "lock_sha256": _sha256_file(web_lock_path),
        "files": _inventory_web_root(root),
    }


def _run_checked(command: list[str], *, cwd: Path) -> None:
    completed = subprocess.run(command, cwd=cwd, check=False)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {completed.returncode}: {' '.join(command)}"
        )


def _validate_node_22(source_root: Path) -> None:
    completed = subprocess.run(
        ["node", "--version"],
        cwd=source_root,
        capture_output=True,
        check=False,
        text=True,
    )
    version = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"v22\.\d+\.\d+", version):
        raise ValueError(f"desktop web assets require Node 22, found {version or 'unavailable'}")


def prepare_web_build_receipt(
    *,
    source_root: Path,
    expected_commit: str,
    receipt_path: Path,
) -> dict[str, Any]:
    """Run the production npm-ci/build contract and emit its exact receipt."""

    source = source_root.resolve(strict=True)
    commit = validate_source_checkout(source, expected_commit=expected_commit)
    destination = receipt_path.resolve()
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("web build receipt must be outside the source checkout")
    if destination.exists():
        raise ValueError("web build receipt must not already exist")
    _validate_node_22(source)
    _run_checked(["npm", "--prefix", "web", "ci"], cwd=source)
    _run_checked(["npm", "--prefix", "web", "run", "licenses:check"], cwd=source)
    _run_checked(["npm", "--prefix", "web", "run", "build"], cwd=source)
    validate_source_checkout(source, expected_commit=commit)
    receipt = create_web_build_receipt(
        source_commit=commit,
        web_lock_path=source / "web" / "package-lock.json",
        web_root=source / "web" / "dist",
    )
    _write_exclusive(destination, _canonical_json_bytes(receipt))
    return receipt


def _platform_name() -> str:
    if sys.platform == "win32":
        return "win32"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ValueError(f"unsupported platform: {sys.platform}")


def _architecture_name() -> str:
    machine = platform.machine().lower()
    if machine in {"arm64", "aarch64"}:
        return "arm64"
    if machine in {"amd64", "x86_64"}:
        return "x64"
    raise ValueError(f"unsupported architecture: {machine}")


def canonical_receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    encoded = (
        json.dumps(
            dict(receipt),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    if len(encoded) >= MAX_RECEIPT_BYTES:
        raise ValueError("sidecar build receipt exceeds 64 KiB")
    return encoded


def create_sidecar_build_receipt(
    *,
    source_commit: str,
    app_version: str,
    binary_path: Path,
    entrypoint_path: Path,
    spec_path: Path,
    python_lock_path: Path,
    python_executable: Path,
    python_version: str,
    pyinstaller_version: str,
    upx_enabled: bool,
    web_asset_receipt_sha256: str,
) -> dict[str, Any]:
    """Create the complete bounded receipt consumed by desktop staging."""

    commit = _require_commit(source_commit, field="source commit")
    version = app_version.strip()
    if not version:
        raise ValueError("app version is required")
    if upx_enabled:
        raise ValueError("UPX is forbidden for desktop sidecar builds")
    frozen_binary = binary_path.resolve(strict=True)
    interpreter = python_executable.resolve(strict=True)
    receipt: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "source_commit": commit,
        "app_version": version,
        "platform": _platform_name(),
        "architecture": _architecture_name(),
        "python_version": python_version.strip(),
        "python_executable": str(interpreter),
        "python_executable_sha256": _sha256_file(interpreter),
        "pyinstaller_version": validate_pyinstaller_version(pyinstaller_version),
        "entrypoint_sha256": _sha256_file(entrypoint_path),
        "spec_sha256": _sha256_file(spec_path),
        "python_lock_sha256": _sha256_file(python_lock_path),
        "web_asset_receipt_sha256": _require_sha256(
            web_asset_receipt_sha256,
            field="web asset receipt digest",
        ),
        "binary_path": str(frozen_binary),
        "binary_size": frozen_binary.stat().st_size,
        "binary_sha256": _sha256_file(frozen_binary),
        "upx_enabled": False,
    }
    canonical_receipt_bytes(receipt)
    return receipt


def _write_exclusive(path: Path, payload: bytes) -> None:
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _read_app_version(pyproject_path: Path) -> str:
    with pyproject_path.open("rb") as handle:
        value = tomllib.load(handle).get("project", {}).get("version")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("project.version is missing from pyproject.toml")
    return value.strip()


def _resolve_output_binary(dist_root: Path) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    binary = dist_root / f"kestrel-desktop-sidecar{suffix}"
    if not binary.is_file() or binary.is_symlink():
        raise ValueError(f"PyInstaller did not produce the expected binary: {binary}")
    return binary


def build_sidecar(
    *,
    source_root: Path,
    expected_commit: str,
    output_root: Path,
    receipt_path: Path,
    web_receipt_path: Path,
) -> dict[str, Any]:
    """Run the frozen build only after all exact-source guards pass."""

    source = source_root.resolve(strict=True)
    commit = validate_source_checkout(source, expected_commit=expected_commit)
    validate_upx_disabled()
    validate_packaging_runtime_roots(source)
    validate_bundled_runtime_distributions(source / "uv.lock")
    web_receipt_sha256 = validate_web_build_receipt(
        web_receipt_path,
        source_commit=commit,
        web_lock_path=source / "web" / "package-lock.json",
        expected_root=source / "web" / "dist",
    )
    pyinstaller_version = validate_pyinstaller_version(
        importlib.metadata.version("pyinstaller")
    )
    output = output_root.resolve()
    try:
        output.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("sidecar build output must be outside the source checkout")
    if output.exists():
        raise ValueError("sidecar build output must not already exist")
    if receipt_path.resolve().is_relative_to(output):
        raise ValueError("sidecar build receipt must be outside the build output")
    output.mkdir(parents=True)
    dist_root = output / "dist"
    work_root = output / "work"
    environment = dict(os.environ)
    environment.pop("UPX", None)
    environment.pop("UPX_DIR", None)
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--clean",
        "--noconfirm",
        "--distpath",
        str(dist_root),
        "--workpath",
        str(work_root),
        str(source / "packaging" / "kestrel-sidecar.spec"),
    ]
    completed = subprocess.run(
        command,
        cwd=source,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"PyInstaller failed with exit code {completed.returncode}")
    validate_source_checkout(source, expected_commit=commit)
    if (
        validate_web_build_receipt(
            web_receipt_path,
            source_commit=commit,
            web_lock_path=source / "web" / "package-lock.json",
            expected_root=source / "web" / "dist",
        )
        != web_receipt_sha256
    ):
        raise ValueError("web build receipt changed during sidecar build")
    binary = _resolve_output_binary(dist_root)
    validate_frozen_binary_inventory(binary)
    receipt = create_sidecar_build_receipt(
        source_commit=commit,
        app_version=_read_app_version(source / "pyproject.toml"),
        binary_path=binary,
        entrypoint_path=source / "packaging" / "kestrel-sidecar-entry.py",
        spec_path=source / "packaging" / "kestrel-sidecar.spec",
        python_lock_path=source / "uv.lock",
        python_executable=Path(sys.executable),
        python_version=platform.python_version(),
        pyinstaller_version=pyinstaller_version,
        upx_enabled=False,
        web_asset_receipt_sha256=web_receipt_sha256,
    )
    _write_exclusive(receipt_path, canonical_receipt_bytes(receipt))
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser(
        "prepare-web",
        help="run npm ci/build and write the exact ignored web/dist receipt",
    )
    prepare.add_argument("--source-root", type=Path, required=True)
    prepare.add_argument("--expected-source-commit", required=True)
    prepare.add_argument("--receipt", type=Path, required=True)
    build = commands.add_parser(
        "build-sidecar",
        help="validate receipts and run the pinned PyInstaller sidecar build",
    )
    build.add_argument("--source-root", type=Path, required=True)
    build.add_argument("--expected-source-commit", required=True)
    build.add_argument("--output-root", type=Path, required=True)
    build.add_argument("--receipt", type=Path, required=True)
    build.add_argument("--web-receipt", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "prepare-web":
        receipt = prepare_web_build_receipt(
            source_root=arguments.source_root,
            expected_commit=arguments.expected_source_commit,
            receipt_path=arguments.receipt,
        )
        print(json.dumps(receipt, sort_keys=True))
        return 0
    receipt = build_sidecar(
        source_root=arguments.source_root,
        expected_commit=arguments.expected_source_commit,
        output_root=arguments.output_root,
        receipt_path=arguments.receipt,
        web_receipt_path=arguments.web_receipt,
    )
    print(json.dumps(receipt, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
