from __future__ import annotations

import hashlib
import io
import os
import stat
import tarfile
from pathlib import Path

import pytest

from scripts import bootstrap_uv as subject

EXPECTED_VERSION = "uv 0.9.21 (0dc9556ad 2025-12-30)"


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def test_release_platform_pins_are_exact() -> None:
    assert subject.UV_VERSION == EXPECTED_VERSION
    assert subject.PLATFORM_SPECS == {
        ("Linux", "x86_64"): subject.PlatformSpec(
            url=(
                "https://github.com/astral-sh/uv/releases/download/0.9.21/"
                "uv-x86_64-unknown-linux-gnu.tar.gz"
            ),
            archive_sha256="0a1ab27383c28ef1c041f85cbbc609d8e3752dfb4b238d2ad97b208a52232baf",
            binary_sha256="53d4952a603676225cf4c19899b8f23d8d5e20f1d052e7b25b1cc2209e15deb0",
            archive_root="uv-x86_64-unknown-linux-gnu",
        ),
        ("Darwin", "arm64"): subject.PlatformSpec(
            url=(
                "https://github.com/astral-sh/uv/releases/download/0.9.21/"
                "uv-aarch64-apple-darwin.tar.gz"
            ),
            archive_sha256="473977236ef8ac5937c80de08a3599cb6ed6021d0e015e10f88076767877a153",
            binary_sha256="db161bb631ae2094da99e2a5f4f6161b325f169be37df07e46597e25124eccc2",
            archive_root="uv-aarch64-apple-darwin",
        ),
    }


def _uv_payload(version: str = EXPECTED_VERSION) -> bytes:
    return f"#!/bin/sh\nprintf '%s\\n' '{version}'\n".encode()


def _archive(
    uv_payload: bytes,
    *,
    root: str = "uv-aarch64-apple-darwin",
    unsafe: str | None = None,
) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        directory = tarfile.TarInfo(f"{root}/")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        archive.addfile(directory)

        uvx_payload = b"uvx fixture"
        uvx = tarfile.TarInfo(f"{root}/uvx")
        uvx.mode = 0o755
        uvx.size = len(uvx_payload)
        archive.addfile(uvx, io.BytesIO(uvx_payload))

        uv = tarfile.TarInfo(f"{root}/uv")
        uv.mode = 0o755
        uv.size = len(uv_payload)
        archive.addfile(uv, io.BytesIO(uv_payload))

        if unsafe == "duplicate":
            duplicate = tarfile.TarInfo(f"{root}/uv")
            duplicate.mode = 0o755
            duplicate.size = len(uv_payload)
            archive.addfile(duplicate, io.BytesIO(uv_payload))
        elif unsafe == "traversal":
            traversal = tarfile.TarInfo("../escape")
            traversal.size = 1
            archive.addfile(traversal, io.BytesIO(b"x"))
        elif unsafe == "symlink":
            link = tarfile.TarInfo(f"{root}/link")
            link.type = tarfile.SYMTYPE
            link.linkname = f"{root}/uv"
            archive.addfile(link)
        elif unsafe == "hardlink":
            link = tarfile.TarInfo(f"{root}/hardlink")
            link.type = tarfile.LNKTYPE
            link.linkname = f"{root}/uv"
            archive.addfile(link)
        elif unsafe == "device":
            device = tarfile.TarInfo(f"{root}/device")
            device.type = tarfile.CHRTYPE
            archive.addfile(device)
        elif unsafe == "fifo":
            fifo = tarfile.TarInfo(f"{root}/fifo")
            fifo.type = tarfile.FIFOTYPE
            archive.addfile(fifo)
        elif unsafe == "unexpected":
            extra = tarfile.TarInfo(f"{root}/README")
            extra.size = 1
            archive.addfile(extra, io.BytesIO(b"x"))
    return output.getvalue()


def _spec(archive: bytes, uv_payload: bytes) -> subject.PlatformSpec:
    return subject.PlatformSpec(
        url="https://github.test/uv.tar.gz",
        archive_sha256=_sha256(archive),
        binary_sha256=_sha256(uv_payload),
        archive_root="uv-aarch64-apple-darwin",
    )


def _install_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    version: str = EXPECTED_VERSION,
    archive_digest: str | None = None,
    binary_digest: str | None = None,
    unsafe: str | None = None,
) -> tuple[Path, subject.PlatformSpec]:
    uv_payload = _uv_payload(version)
    archive = _archive(uv_payload, unsafe=unsafe)
    spec = _spec(archive, uv_payload)
    if archive_digest is not None:
        spec = subject.PlatformSpec(
            url=spec.url,
            archive_sha256=archive_digest,
            binary_sha256=spec.binary_sha256,
            archive_root=spec.archive_root,
        )
    if binary_digest is not None:
        spec = subject.PlatformSpec(
            url=spec.url,
            archive_sha256=spec.archive_sha256,
            binary_sha256=binary_digest,
            archive_root=spec.archive_root,
        )
    monkeypatch.setattr(subject, "_select_platform", lambda: spec)
    monkeypatch.setattr(subject, "_download", lambda _url, output: output.write_bytes(archive))
    destination = tmp_path / "uv-root"
    return destination, spec


@pytest.mark.skipif(os.name == "nt", reason="uv bootstrap supports POSIX release hosts only")
def test_clean_runner_installs_only_absolute_verified_uv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination, _specification = _install_fixture(monkeypatch, tmp_path)

    installed = subject.bootstrap(destination)

    assert installed == destination / "uv"
    assert installed.is_file() and not installed.is_symlink()
    assert installed.stat().st_mode & stat.S_IXUSR
    assert sorted(path.name for path in destination.iterdir()) == ["uv"]
    assert stat.S_IMODE(destination.stat().st_mode) == 0o700


@pytest.mark.skipif(os.name == "nt", reason="uv bootstrap supports POSIX release hosts only")
def test_path_shadow_cannot_substitute_the_verified_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination, _specification = _install_fixture(monkeypatch, tmp_path)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    shadow_uv = shadow / "uv"
    shadow_uv.write_bytes(_uv_payload("uv 99.0.0 (shadow)"))
    shadow_uv.chmod(0o755)
    monkeypatch.setenv("PATH", str(shadow))

    assert subject.bootstrap(destination) == destination / "uv"


@pytest.mark.skipif(os.name == "nt", reason="uv bootstrap supports POSIX release hosts only")
def test_wrong_reported_version_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination, _specification = _install_fixture(
        monkeypatch, tmp_path, version="uv 0.9.20 (wrong)"
    )

    with pytest.raises(subject.BootstrapError, match="version"):
        subject.bootstrap(destination)


def test_archive_checksum_mismatch_fails_before_inspection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination, _specification = _install_fixture(
        monkeypatch, tmp_path, archive_digest="0" * 64
    )
    inspected = False

    def forbidden_inspection(*_args: object, **_kwargs: object) -> object:
        nonlocal inspected
        inspected = True
        raise AssertionError("archive inspected before checksum verification")

    monkeypatch.setattr(tarfile, "open", forbidden_inspection)
    with pytest.raises(subject.BootstrapError, match="archive SHA-256"):
        subject.bootstrap(destination)
    assert inspected is False


def test_extracted_binary_checksum_mismatch_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    destination, _specification = _install_fixture(
        monkeypatch, tmp_path, binary_digest="0" * 64
    )

    with pytest.raises(subject.BootstrapError, match="binary SHA-256"):
        subject.bootstrap(destination)


@pytest.mark.parametrize(
    "unsafe",
    ["duplicate", "traversal", "symlink", "hardlink", "device", "fifo", "unexpected"],
)
def test_unsafe_or_unexpected_archive_members_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unsafe: str
) -> None:
    destination, _specification = _install_fixture(
        monkeypatch, tmp_path, unsafe=unsafe
    )

    with pytest.raises(subject.BootstrapError, match="archive"):
        subject.bootstrap(destination)


def test_nonempty_or_insecure_destination_fails_before_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    downloaded = False

    def forbidden_download(_url: str, _output: Path) -> None:
        nonlocal downloaded
        downloaded = True

    monkeypatch.setattr(subject, "_download", forbidden_download)
    monkeypatch.setattr(
        subject,
        "_select_platform",
        lambda: subject.PlatformSpec("https://github.test/uv.tar.gz", "0" * 64, "0" * 64, "uv"),
    )
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir(mode=0o700)
    (nonempty / "occupied").write_text("x", encoding="utf-8")
    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target, target_is_directory=True)

    with pytest.raises(subject.BootstrapError, match="absent or empty"):
        subject.bootstrap(nonempty)
    with pytest.raises(subject.BootstrapError, match="mode 0700"):
        subject.bootstrap(insecure)
    with pytest.raises(subject.BootstrapError, match="absent or empty"):
        subject.bootstrap(symlink)
    assert downloaded is False


def test_unsupported_platform_fails_before_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subject.platform, "system", lambda: "Windows")
    monkeypatch.setattr(subject.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(
        subject,
        "_download",
        lambda *_args: pytest.fail("unsupported platform attempted download"),
    )

    with pytest.raises(subject.BootstrapError, match="unsupported uv bootstrap platform"):
        subject.bootstrap(tmp_path / "uv-root")
