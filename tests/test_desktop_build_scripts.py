from __future__ import annotations

import os
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from scripts.generate_desktop_resource_manifest import (
    canonical_manifest_bytes,
    generate_resource_manifest,
    validate_portable_relative_paths,
)
from scripts.verify_desktop_resource_manifest import (
    DesktopManifestIdentity,
    verify_developer_resource_manifest,
    verify_release_resource_manifest,
)

MANIFEST_NAME = "kestrel-resource-manifest.json"
SIGNATURE_NAME = "kestrel-resource-manifest.sig"
ROOT = Path(__file__).resolve().parents[1]


def test_desktop_manifest_scripts_support_direct_and_module_entrypoints() -> None:
    for module in (
        "generate_desktop_resource_manifest",
        "verify_desktop_resource_manifest",
    ):
        for command in (
            [sys.executable, f"scripts/{module}.py", "--help"],
            [sys.executable, "-m", f"scripts.{module}", "--help"],
        ):
            completed = subprocess.run(
                command,
                cwd=ROOT,
                capture_output=True,
                check=False,
                text=True,
            )
            assert completed.returncode == 0, completed.stderr


def _sha256(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def _stage_payload(root: Path) -> None:
    files = {
        "sidecar/kestrel-desktop-sidecar": b"frozen-sidecar",
        "web/dist/index.html": b"<h1>Kestrel</h1>",
        "licenses/THIRD_PARTY_NOTICES.txt": b"Notices\n",
        "sbom.cdx.json": b'{"bomFormat":"CycloneDX"}\n',
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _identity(
    *,
    build_mode: str,
    key_id: str,
    sbom_sha256: str = "",
) -> DesktopManifestIdentity:
    return DesktopManifestIdentity(
        build_mode=build_mode,
        key_id=key_id,
        source_commit="a" * 40,
        app_version="0.5.0",
        platform="darwin",
        architecture="arm64",
        python_lock_sha256="1" * 64,
        desktop_npm_lock_sha256="2" * 64,
        web_npm_lock_sha256="3" * 64,
        sbom_sha256=sbom_sha256,
    )


def _generate(root: Path, *, build_mode: str, key_id: str) -> dict[str, object]:
    return generate_resource_manifest(
        root,
        identity=_identity(build_mode=build_mode, key_id=key_id),
    )


def _write_signed(
    root: Path,
    manifest: dict[str, object],
    private_key: Ed25519PrivateKey,
) -> None:
    canonical = canonical_manifest_bytes(manifest)
    (root / MANIFEST_NAME).write_bytes(canonical)
    (root / SIGNATURE_NAME).write_bytes(private_key.sign(canonical))


def test_manifest_binds_provenance_and_covers_every_payload_file(tmp_path: Path) -> None:
    _stage_payload(tmp_path)

    manifest = _generate(tmp_path, build_mode="release", key_id="release")

    assert manifest == {
        "schema": "kestrel.desktop.resources.v1",
        "build_mode": "release",
        "key_id": "release",
        "source_commit": "a" * 40,
        "app_version": "0.5.0",
        "platform": "darwin",
        "architecture": "arm64",
        "python_lock_sha256": "1" * 64,
        "desktop_npm_lock_sha256": "2" * 64,
        "web_npm_lock_sha256": "3" * 64,
        "sbom_sha256": _sha256(tmp_path / "sbom.cdx.json"),
        "files": {
            "licenses/THIRD_PARTY_NOTICES.txt": {
                "size": 8,
                "sha256": _sha256(tmp_path / "licenses/THIRD_PARTY_NOTICES.txt"),
            },
            "sbom.cdx.json": {
                "size": 26,
                "sha256": _sha256(tmp_path / "sbom.cdx.json"),
            },
            "sidecar/kestrel-desktop-sidecar": {
                "size": 14,
                "sha256": _sha256(tmp_path / "sidecar/kestrel-desktop-sidecar"),
            },
            "web/dist/index.html": {
                "size": 16,
                "sha256": _sha256(tmp_path / "web/dist/index.html"),
            },
        },
    }


def test_manifest_generation_rejects_unsafe_or_ambiguous_stage_entries(tmp_path: Path) -> None:
    _stage_payload(tmp_path)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.write_bytes(b"outside")
    (tmp_path / "linked").symlink_to(outside)
    try:
        with pytest.raises(ValueError, match="symlink"):
            _generate(tmp_path, build_mode="release", key_id="release")
    finally:
        outside.unlink()

    (tmp_path / "linked").unlink()
    with pytest.raises(ValueError, match="case-colliding"):
        validate_portable_relative_paths(["Case.txt", "case.TXT"])


@pytest.mark.parametrize(
    "relative_paths",
    [
        ["Café/first.bin", "Cafe\u0301/second.bin"],
        ["Ａrtifact/first.bin", "Artifact/second.bin"],
    ],
)
def test_portable_paths_reject_nfkc_casefolded_component_collisions(
    relative_paths: list[str],
) -> None:
    with pytest.raises(ValueError, match="case-colliding"):
        validate_portable_relative_paths(relative_paths)


@pytest.mark.parametrize(
    ("build_mode", "key_id"),
    [
        ("release", "developer"),
        ("developer", "release"),
        ("developer", "developer-other"),
    ],
)
def test_manifest_generation_requires_exact_mode_key_pair(
    tmp_path: Path,
    build_mode: str,
    key_id: str,
) -> None:
    _stage_payload(tmp_path)

    with pytest.raises(ValueError, match="mode and key"):
        _generate(tmp_path, build_mode=build_mode, key_id=key_id)


def test_release_verifier_rejects_developer_while_explicit_verifier_accepts(
    tmp_path: Path,
) -> None:
    _stage_payload(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    manifest = _generate(tmp_path, build_mode="developer", key_id="developer")
    _write_signed(tmp_path, manifest, private_key)
    expected = _identity(
        build_mode="developer",
        key_id="developer",
        sbom_sha256=_sha256(tmp_path / "sbom.cdx.json"),
    )
    public_keys = {"developer": private_key.public_key()}

    with pytest.raises(ValueError, match="release manifest"):
        verify_release_resource_manifest(
            tmp_path,
            expected_identity=expected,
            trusted_public_keys=public_keys,
        )

    verified = verify_developer_resource_manifest(
        tmp_path,
        expected_identity=expected,
        trusted_public_keys=public_keys,
    )
    assert verified["build_mode"] == "developer"
    assert verified["key_id"] == "developer"


def test_verifier_rejects_extra_changed_and_metadata_drift(tmp_path: Path) -> None:
    _stage_payload(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    manifest = _generate(tmp_path, build_mode="release", key_id="release")
    _write_signed(tmp_path, manifest, private_key)
    expected = _identity(
        build_mode="release",
        key_id="release",
        sbom_sha256=_sha256(tmp_path / "sbom.cdx.json"),
    )
    public_keys = {"release": private_key.public_key()}

    (tmp_path / "extra.bin").write_bytes(b"unlisted")
    with pytest.raises(ValueError, match="payload coverage"):
        verify_release_resource_manifest(
            tmp_path,
            expected_identity=expected,
            trusted_public_keys=public_keys,
        )
    (tmp_path / "extra.bin").unlink()

    (tmp_path / "web/dist/index.html").write_bytes(b"X" * 16)
    with pytest.raises(ValueError, match="digest mismatch"):
        verify_release_resource_manifest(
            tmp_path,
            expected_identity=expected,
            trusted_public_keys=public_keys,
        )
    (tmp_path / "web/dist/index.html").write_bytes(b"<h1>Kestrel</h1>")

    drifted = DesktopManifestIdentity(
        **{
            **expected.__dict__,
            "desktop_npm_lock_sha256": "9" * 64,
        }
    )
    with pytest.raises(ValueError, match="packaged metadata mismatch"):
        verify_release_resource_manifest(
            tmp_path,
            expected_identity=drifted,
            trusted_public_keys=public_keys,
        )


def test_manifest_controls_are_excluded_but_validated_separately(tmp_path: Path) -> None:
    _stage_payload(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    manifest = _generate(tmp_path, build_mode="release", key_id="release")
    _write_signed(tmp_path, manifest, private_key)

    assert MANIFEST_NAME not in manifest["files"]
    assert SIGNATURE_NAME not in manifest["files"]
    verify_release_resource_manifest(
        tmp_path,
        expected_identity=_identity(
            build_mode="release",
            key_id="release",
            sbom_sha256=_sha256(tmp_path / "sbom.cdx.json"),
        ),
        trusted_public_keys={"release": private_key.public_key()},
    )

    (tmp_path / SIGNATURE_NAME).write_bytes(b"x" * 64)
    with pytest.raises(ValueError, match="signature"):
        verify_release_resource_manifest(
            tmp_path,
            expected_identity=_identity(
                build_mode="release",
                key_id="release",
                sbom_sha256=_sha256(tmp_path / "sbom.cdx.json"),
            ),
            trusted_public_keys={"release": private_key.public_key()},
        )


def test_verifier_ignores_no_environment_developer_bypass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage_payload(tmp_path)
    private_key = Ed25519PrivateKey.generate()
    manifest = _generate(tmp_path, build_mode="developer", key_id="developer")
    _write_signed(tmp_path, manifest, private_key)
    monkeypatch.setenv("KESTREL_ALLOW_DEVELOPER_DESKTOP", "1")
    monkeypatch.setenv("KESTREL_DESKTOP_BUILD_MODE", "developer")

    with pytest.raises(ValueError, match="release manifest"):
        verify_release_resource_manifest(
            tmp_path,
            expected_identity=_identity(
                build_mode="developer",
                key_id="developer",
                sbom_sha256=_sha256(tmp_path / "sbom.cdx.json"),
            ),
            trusted_public_keys={"developer": private_key.public_key()},
        )

    assert os.environ["KESTREL_DESKTOP_BUILD_MODE"] == "developer"
