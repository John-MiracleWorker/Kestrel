#!/usr/bin/env python3
"""Install the exact uv binary used by the release transaction."""

from __future__ import annotations

import hashlib
import os
import platform
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, cast

UV_VERSION = "uv 0.9.21 (0dc9556ad 2025-12-30)"
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_BINARY_BYTES = 96 * 1024 * 1024


class BootstrapError(RuntimeError):
    """The pinned uv bootstrap contract was not satisfied."""


@dataclass(frozen=True)
class PlatformSpec:
    url: str
    archive_sha256: str
    binary_sha256: str
    archive_root: str


PLATFORM_SPECS = {
    ("Linux", "x86_64"): PlatformSpec(
        url=(
            "https://github.com/astral-sh/uv/releases/download/0.9.21/"
            "uv-x86_64-unknown-linux-gnu.tar.gz"
        ),
        archive_sha256="0a1ab27383c28ef1c041f85cbbc609d8e3752dfb4b238d2ad97b208a52232baf",
        binary_sha256="53d4952a603676225cf4c19899b8f23d8d5e20f1d052e7b25b1cc2209e15deb0",
        archive_root="uv-x86_64-unknown-linux-gnu",
    ),
    ("Darwin", "arm64"): PlatformSpec(
        url=(
            "https://github.com/astral-sh/uv/releases/download/0.9.21/"
            "uv-aarch64-apple-darwin.tar.gz"
        ),
        archive_sha256="473977236ef8ac5937c80de08a3599cb6ed6021d0e015e10f88076767877a153",
        binary_sha256="db161bb631ae2094da99e2a5f4f6161b325f169be37df07e46597e25124eccc2",
        archive_root="uv-aarch64-apple-darwin",
    ),
}


def _select_platform() -> PlatformSpec:
    key = (platform.system(), platform.machine())
    try:
        return PLATFORM_SPECS[key]
    except KeyError as exc:
        raise BootstrapError(
            f"unsupported uv bootstrap platform: {key[0]}/{key[1]}"
        ) from exc


def _prepare_destination(destination: Path) -> None:
    if destination.is_symlink():
        raise BootstrapError("uv destination must be absent or empty")
    if destination.exists():
        if not destination.is_dir() or any(destination.iterdir()):
            raise BootstrapError("uv destination must be absent or empty")
        if stat.S_IMODE(destination.stat().st_mode) != 0o700:
            raise BootstrapError("existing uv destination must have mode 0700")
        return
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise BootstrapError("could not create the uv destination") from exc
    if stat.S_IMODE(destination.stat().st_mode) != 0o700:
        raise BootstrapError("new uv destination does not have mode 0700")


def _download(url: str, output: Path) -> None:
    if not url.startswith("https://github.com/"):
        raise BootstrapError("uv download URL is not the pinned HTTPS origin")
    context = ssl.create_default_context()
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
    )
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "kestrel-release-uv-bootstrap/1"},
        method="GET",
    )
    try:
        with opener.open(request, timeout=60) as response, output.open("xb") as target:
            final_url = response.geturl()
            if not final_url.startswith("https://"):
                raise BootstrapError("uv download redirected outside HTTPS")
            total = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ARCHIVE_BYTES:
                    raise BootstrapError("uv archive exceeds the size limit")
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
    except BootstrapError:
        raise
    except (OSError, urllib.error.URLError) as exc:
        raise BootstrapError("uv archive download failed") from exc
    if output.stat().st_size <= 0:
        raise BootstrapError("uv archive download is empty")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_member_name(raw_name: str) -> str:
    name = raw_name.rstrip("/")
    path = PurePosixPath(name)
    if (
        not name
        or raw_name.startswith("/")
        or path.is_absolute()
        or "\\" in raw_name
        or any(part in {"", ".", ".."} for part in raw_name.split("/"))
    ):
        raise BootstrapError("uv archive has an unsafe member path")
    return name


def _copy_bounded(source: BinaryIO, target: BinaryIO) -> str:
    digest = hashlib.sha256()
    total = 0
    while chunk := source.read(1024 * 1024):
        total += len(chunk)
        if total > MAX_BINARY_BYTES:
            raise BootstrapError("uv binary exceeds the size limit")
        digest.update(chunk)
        target.write(chunk)
    if total <= 0:
        raise BootstrapError("uv binary is empty")
    return digest.hexdigest()


def _extract_verified_uv(
    archive_path: Path, output_path: Path, specification: PlatformSpec
) -> None:
    expected = {
        specification.archive_root: "directory",
        f"{specification.archive_root}/uv": "uv",
        f"{specification.archive_root}/uvx": "uvx",
    }
    seen: set[str] = set()
    binary_digest: str | None = None
    try:
        with tarfile.open(archive_path, mode="r:gz") as archive:
            if archive.pax_headers:
                raise BootstrapError("uv archive has global pax headers")
            for member in archive:
                name = _safe_member_name(member.name)
                if name in seen:
                    raise BootstrapError("uv archive has a duplicate member")
                seen.add(name)
                kind = expected.get(name)
                if kind is None:
                    raise BootstrapError("uv archive has an unexpected member")
                if member.pax_headers:
                    raise BootstrapError("uv archive member has pax headers")
                if kind == "directory":
                    if not member.isdir():
                        raise BootstrapError("uv archive root is not a directory")
                    continue
                if not member.isfile() or member.size <= 0:
                    raise BootstrapError("uv archive executable is not a regular file")
                if member.mode & 0o111 == 0:
                    raise BootstrapError("uv archive executable has no execute bit")
                if kind != "uv":
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise BootstrapError("uv archive executable cannot be read")
                with output_path.open("xb") as target:
                    binary_digest = _copy_bounded(cast(BinaryIO, extracted), target)
                    target.flush()
                    os.fsync(target.fileno())
    except BootstrapError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise BootstrapError("uv archive inspection failed") from exc
    if seen != set(expected):
        raise BootstrapError("uv archive member inventory is incomplete")
    if binary_digest != specification.binary_sha256:
        raise BootstrapError("uv binary SHA-256 mismatch")
    output_path.chmod(0o755)


def _verify_version(binary: Path) -> None:
    try:
        completed = subprocess.run(
            [str(binary.resolve(strict=True)), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env={"LANG": "C", "LC_ALL": "C", "PATH": ""},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BootstrapError("uv version execution failed") from exc
    if (
        completed.returncode != 0
        or completed.stdout != f"{UV_VERSION}\n"
        or completed.stderr
    ):
        raise BootstrapError("uv version verification failed")


def bootstrap(destination: Path) -> Path:
    specification = _select_platform()
    if destination.is_symlink():
        raise BootstrapError("uv destination must be absent or empty")
    destination = Path(os.path.abspath(destination))
    _prepare_destination(destination)
    installed = destination / "uv"
    try:
        with tempfile.TemporaryDirectory(prefix="kestrel-uv-bootstrap-") as temporary:
            temporary_root = Path(temporary)
            archive_path = temporary_root / "uv.tar.gz"
            extracted_path = temporary_root / "uv"
            _download(specification.url, archive_path)
            if _file_sha256(archive_path) != specification.archive_sha256:
                raise BootstrapError("uv archive SHA-256 mismatch")
            _extract_verified_uv(archive_path, extracted_path, specification)
            if destination.is_symlink() or stat.S_IMODE(destination.stat().st_mode) != 0o700:
                raise BootstrapError("uv destination changed during bootstrap")
            os.replace(extracted_path, installed)
            installed.chmod(0o755)
        _verify_version(installed)
    except Exception:
        if installed.exists() and not installed.is_symlink():
            installed.unlink()
        raise
    if sorted(path.name for path in destination.iterdir()) != ["uv"]:
        installed.unlink(missing_ok=True)
        raise BootstrapError("uv installation inventory mismatch")
    return installed


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print("usage: bootstrap_uv.py DESTINATION", file=sys.stderr)
        return 2
    try:
        installed = bootstrap(Path(arguments[0]))
    except BootstrapError as exc:
        print(f"uv bootstrap failed: {exc}", file=sys.stderr)
        return 1
    print(installed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
