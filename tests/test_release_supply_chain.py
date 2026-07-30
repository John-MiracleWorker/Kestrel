from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import scripts.verify_exact_wheel_install as exact_wheel_install
from scripts.generate_desktop_resource_manifest import (
    canonical_manifest_bytes,
    generate_resource_manifest,
)
from scripts.release_publication_guard import (
    OCI_RECORD_NAME,
    build_oci_record,
    desktop_qualification_signature_bytes,
    plan_pypi_files,
    validate_desktop_publication_evidence,
    validate_oci_index,
    validate_oci_record,
    validate_release_assets,
    write_oci_record,
)
from scripts.verify_desktop_resource_manifest import DesktopManifestIdentity
from scripts.verify_release_payload import verify_release_payload

ROOT = Path(__file__).resolve().parents[1]


def _desktop_identity(
    *,
    build_mode: str = "release",
    key_id: str = "release",
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
        sbom_sha256="",
    )


def _desktop_qualification_receipt(
    *,
    resource_manifest_digest: str,
    build_mode: str = "release",
    key_id: str = "release",
    signed: bool = True,
) -> dict[str, object]:
    return {
        "schema": "kestrel.desktop.qualification.v1",
        "build_mode": build_mode,
        "key_id": key_id,
        "signed": signed,
        "source_commit": "a" * 40,
        "qualified": True,
        "resource_manifest_digest": resource_manifest_digest,
    }


def _desktop_publication_bundle(
    root: Path,
    *,
    build_mode: str = "release",
    key_id: str = "release",
) -> tuple[DesktopManifestIdentity, Ed25519PrivateKey, str]:
    (root / "sidecar").mkdir()
    (root / "sidecar/kestrel-desktop-sidecar").write_bytes(b"sidecar")
    (root / "sbom.cdx.json").write_bytes(b'{"bomFormat":"CycloneDX"}\n')
    private_key = Ed25519PrivateKey.generate()
    manifest = generate_resource_manifest(
        root,
        identity=_desktop_identity(build_mode=build_mode, key_id=key_id),
    )
    manifest_bytes = canonical_manifest_bytes(manifest)
    (root / "kestrel-resource-manifest.json").write_bytes(manifest_bytes)
    (root / "kestrel-resource-manifest.sig").write_bytes(
        private_key.sign(manifest_bytes)
    )
    identity = replace(
        _desktop_identity(build_mode=build_mode, key_id=key_id),
        sbom_sha256=str(manifest["sbom_sha256"]),
    )
    return (
        identity,
        private_key,
        f"sha256:{hashlib.sha256(manifest_bytes).hexdigest()}",
    )


def _signed_qualification(
    private_key: Ed25519PrivateKey,
    receipt: dict[str, object],
) -> tuple[dict[str, object], bytes]:
    return receipt, private_key.sign(
        desktop_qualification_signature_bytes(receipt)
    )


def test_publication_guard_rejects_unsigned_bundle_and_claim_only_receipt(
    tmp_path: Path,
) -> None:
    identity, private_key, manifest_digest = _desktop_publication_bundle(tmp_path)
    receipt = _desktop_qualification_receipt(
        resource_manifest_digest=manifest_digest
    )
    public_keys = {"release": private_key.public_key()}

    (tmp_path / "kestrel-resource-manifest.sig").write_bytes(b"\0" * 64)
    with pytest.raises(ValueError, match="resource signature"):
        validate_desktop_publication_evidence(
            tmp_path,
            [_signed_qualification(private_key, receipt)],
            expected_identity=identity,
            trusted_public_keys=public_keys,
        )

    manifest_bytes = (tmp_path / "kestrel-resource-manifest.json").read_bytes()
    (tmp_path / "kestrel-resource-manifest.sig").write_bytes(
        private_key.sign(manifest_bytes)
    )
    with pytest.raises(ValueError, match="unsigned desktop qualification"):
        validate_desktop_publication_evidence(
            tmp_path,
            [(receipt, b"")],
            expected_identity=identity,
            trusted_public_keys=public_keys,
        )
    with pytest.raises(ValueError, match="signature is invalid"):
        validate_desktop_publication_evidence(
            tmp_path,
            [(receipt, b"\0" * 64)],
            expected_identity=identity,
            trusted_public_keys=public_keys,
        )


def test_publication_guard_categorically_rejects_developer_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity, private_key, manifest_digest = _desktop_publication_bundle(
        tmp_path,
        build_mode="developer",
        key_id="developer",
    )
    receipt = _desktop_qualification_receipt(
        resource_manifest_digest=manifest_digest,
        build_mode="developer",
        key_id="developer",
    )
    monkeypatch.setenv("KESTREL_ALLOW_DEVELOPER_DESKTOP", "1")
    with pytest.raises(ValueError, match="developer desktop manifest"):
        validate_desktop_publication_evidence(
            tmp_path,
            [_signed_qualification(private_key, receipt)],
            expected_identity=identity,
            trusted_public_keys={"developer": private_key.public_key()},
        )


def test_publication_guard_explicitly_rejects_developer_directory_smoke_receipt(
    tmp_path: Path,
) -> None:
    identity, private_key, _manifest_digest = _desktop_publication_bundle(tmp_path)
    smoke_receipt = {
        "schema": "kestrel.desktop.directory-smoke.v1",
        "build_mode": "developer",
        "source_commit": "a" * 40,
        "qualified": True,
    }

    with pytest.raises(
        ValueError,
        match="developer desktop directory smoke cannot be published",
    ):
        validate_desktop_publication_evidence(
            tmp_path,
            [(smoke_receipt, b"\0" * 64)],
            expected_identity=identity,
            trusted_public_keys={"release": private_key.public_key()},
        )


def test_publication_guard_accepts_only_cryptographically_bound_release_evidence(
    tmp_path: Path,
) -> None:
    identity, private_key, manifest_digest = _desktop_publication_bundle(tmp_path)
    receipt = _desktop_qualification_receipt(
        resource_manifest_digest=manifest_digest
    )
    public_keys = {"release": private_key.public_key()}

    assert validate_desktop_publication_evidence(
        tmp_path,
        [_signed_qualification(private_key, receipt)],
        expected_identity=identity,
        trusted_public_keys=public_keys,
    ) == {
        "source_commit": "a" * 40,
        "qualification_count": 1,
        "resource_manifest_digest": manifest_digest,
    }

    drifted_receipt = {
        **receipt,
        "resource_manifest_digest": f"sha256:{'9' * 64}",
    }
    with pytest.raises(ValueError, match="resource manifest digest mismatch"):
        validate_desktop_publication_evidence(
            tmp_path,
            [_signed_qualification(private_key, drifted_receipt)],
            expected_identity=identity,
            trusted_public_keys=public_keys,
        )


def test_desktop_publication_guard_cli_verifies_direct_and_module_evidence(
    tmp_path: Path,
) -> None:
    stage = tmp_path / "stage"
    evidence = tmp_path / "evidence"
    stage.mkdir()
    evidence.mkdir()
    identity, private_key, manifest_digest = _desktop_publication_bundle(stage)
    receipt = _desktop_qualification_receipt(
        resource_manifest_digest=manifest_digest
    )
    _, receipt_signature = _signed_qualification(private_key, receipt)
    identity_path = evidence / "identity.json"
    public_key_path = evidence / "release-public-key.pem"
    receipt_path = evidence / "qualification.json"
    receipt_signature_path = evidence / "qualification.sig"
    identity_path.write_text(json.dumps(asdict(identity)), encoding="utf-8")
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_signature_path.write_bytes(receipt_signature)
    arguments = [
        "verify-desktop-evidence",
        str(stage),
        "--identity",
        str(identity_path),
        "--public-key",
        str(public_key_path),
        "--qualification-receipt",
        str(receipt_path),
        "--qualification-signature",
        str(receipt_signature_path),
    ]

    for entrypoint in (
        [sys.executable, "scripts/release_publication_guard.py"],
        [sys.executable, "-m", "scripts.release_publication_guard"],
    ):
        completed = subprocess.run(
            [*entrypoint, *arguments],
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(completed.stdout)["resource_manifest_digest"] == (
            manifest_digest
        )

    mismatched = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.release_publication_guard",
            *arguments,
            "--qualification-receipt",
            str(receipt_path),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert mismatched.returncode != 0
    assert "receipt/signature count mismatch" in mismatched.stderr


def test_exact_wheel_verifier_supports_direct_and_module_entrypoints() -> None:
    for command in (
        [sys.executable, "scripts/verify_exact_wheel_install.py", "--help"],
        [sys.executable, "-m", "scripts.verify_exact_wheel_install", "--help"],
    ):
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            check=False,
            text=True,
        )
        assert completed.returncode == 0, completed.stderr
        assert "Install and exercise one exact Kestrel release wheel" in completed.stdout


def test_exact_wheel_venv_uses_portable_interpreter_linkage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    base_python = tmp_path / "base-python"
    base_python.write_bytes(b"")

    class FakeBuilder:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def create(self, root: Path) -> None:
            python = exact_wheel_install._venv_python(root)
            python.parent.mkdir(parents=True)
            if os.name == "nt":
                python.write_bytes(b"")
            else:
                python.symlink_to(base_python)

    monkeypatch.setattr(exact_wheel_install.venv, "EnvBuilder", FakeBuilder)
    root = tmp_path / "venv"

    python = exact_wheel_install._create_venv(root)

    assert captured == {"with_pip": True, "symlinks": os.name != "nt"}
    assert python == exact_wheel_install._venv_python(root).absolute()
    if os.name != "nt":
        assert python.is_symlink()


def _logical_requirements(path: Path) -> list[str]:
    logical: list[str] = []
    current = ""
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("-r "):
            logical.extend(_logical_requirements(path.parent / stripped.removeprefix("-r ")))
            continue
        continued = stripped.endswith("\\")
        fragment = stripped[:-1].strip() if continued else stripped
        current = f"{current} {fragment}".strip()
        if not continued:
            logical.append(current)
            current = ""
    assert not current
    return logical


def _rewrite_manifest(root: Path) -> None:
    artifacts = {
        path.name: path.read_bytes()
        for path in root.iterdir()
        if path.is_file() and path.name != "SHA256SUMS"
    }
    manifest = "".join(
        f"{hashlib.sha256(content).hexdigest()}  {name}\n" for name, content in artifacts.items()
    )
    (root / "SHA256SUMS").write_text(manifest, encoding="ascii")


def _write_payload(
    root: Path,
    *,
    wheel_metadata_version: str = "0.5.0",
    sdist_metadata_version: str = "0.5.0",
    sbom_version: str = "0.5.0",
) -> None:
    wheel = root / "nested_memvid_agent-0.5.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "nested_memvid_agent-0.5.0.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: nested-memvid-agent\n"
            f"Version: {wheel_metadata_version}\n",
        )
    sdist = root / "nested_memvid_agent-0.5.0.tar.gz"
    package_info = (
        f"Metadata-Version: 2.4\nName: nested-memvid-agent\nVersion: {sdist_metadata_version}\n"
    ).encode()
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo("nested_memvid_agent-0.5.0/PKG-INFO")
        member.size = len(package_info)
        archive.addfile(member, io.BytesIO(package_info))
    artifacts = {
        "install.sh": b"#!/usr/bin/env bash\n",
        "install.ps1": b'param([string] $Version = "0.5.0")\n',
        "requirements-release.txt": (
            b"memvid-sdk==2.0.160 \\\n"
            b"    --hash=sha256:8eab5aec9a30eb459f553ed091038b6916d02a2f33569b32a7aee1b556820243\n"
        ),
        "sbom.cdx.json": json.dumps(
            {
                "bomFormat": "CycloneDX",
                "components": [
                    {
                        "type": "library",
                        "name": "nested-memvid-agent",
                        "version": sbom_version,
                    }
                ],
            }
        ).encode(),
    }
    for name, content in artifacts.items():
        (root / name).write_bytes(content)
    _rewrite_manifest(root)


def test_build_bootstraps_are_exact_and_hash_locked() -> None:
    common = _logical_requirements(ROOT / "config" / "python-build-bootstrap.txt")
    release = _logical_requirements(ROOT / "config" / "release-build-bootstrap.txt")

    assert {entry.split("==", 1)[0] for entry in common} == {
        "packaging",
        "pip",
        "setuptools",
        "wheel",
    }
    assert {"build", "maturin", "uv"} <= {entry.split("==", 1)[0] for entry in release}
    for requirement in release:
        assert "==" in requirement
        assert "--hash=sha256:" in requirement


def test_release_payload_verifier_covers_every_artifact_and_detects_tampering(
    tmp_path: Path,
) -> None:
    _write_payload(tmp_path)

    report = verify_release_payload(tmp_path, expected_version="v0.5.0")

    assert report["verified"] is True
    assert report["artifact_count"] == 6
    assert report["requirement_count"] == 1
    assert report["distribution"] == "nested-memvid-agent"
    assert report["version"] == "0.5.0"

    (tmp_path / "install.sh").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256 mismatch for install.sh"):
        verify_release_payload(tmp_path, expected_version="0.5.0")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"wheel_metadata_version": "0.4.12"}, "wheel METADATA version mismatch"),
        ({"sdist_metadata_version": "0.4.12"}, "sdist PKG-INFO version mismatch"),
        ({"sbom_version": "0.4.12"}, "CycloneDX Kestrel component identity mismatch"),
    ],
)
def test_release_payload_verifier_rejects_internal_identity_drift(
    tmp_path: Path, kwargs: dict[str, str], message: str
) -> None:
    _write_payload(tmp_path, **kwargs)

    with pytest.raises(ValueError, match=message):
        verify_release_payload(tmp_path, expected_version="0.5.0")


def test_release_payload_verifier_rejects_filename_identity_drift(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    wheel = tmp_path / "nested_memvid_agent-0.5.0-py3-none-any.whl"
    wheel.rename(tmp_path / "nested_memvid_agent-0.4.12-py3-none-any.whl")
    _rewrite_manifest(tmp_path)

    with pytest.raises(ValueError, match="wheel filename version mismatch"):
        verify_release_payload(tmp_path, expected_version="0.5.0")


def test_release_payload_verifier_rejects_checksummed_unknown_artifact(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    (tmp_path / "unexpected.bin").write_bytes(b"not part of the public release contract")
    _rewrite_manifest(tmp_path)

    with pytest.raises(ValueError, match="unexpected artifacts.*unexpected.bin"):
        verify_release_payload(tmp_path, expected_version="0.5.0")


def test_oci_record_is_identity_bound_and_added_to_the_release_manifest(
    tmp_path: Path,
) -> None:
    _write_payload(tmp_path)
    index_digest = "sha256:" + "a" * 64
    amd64_digest = "sha256:" + "b" * 64
    arm64_digest = "sha256:" + "c" * 64

    record_path = write_oci_record(
        tmp_path,
        repository="John-MiracleWorker/Kestrel",
        tag="v0.5.0",
        commit="d" * 40,
        image="ghcr.io/john-miracleworker/kestrel",
        index_digest=index_digest,
        amd64_digest=amd64_digest,
        arm64_digest=arm64_digest,
    )

    report = verify_release_payload(tmp_path, expected_version="v0.5.0")
    assert report["artifact_count"] == 7
    sums = (tmp_path / "SHA256SUMS").read_text(encoding="ascii")
    assert f"  {OCI_RECORD_NAME}\n" in sums
    record = validate_oci_record(
        json.loads(record_path.read_text(encoding="utf-8")),
        repository="John-MiracleWorker/Kestrel",
        tag="v0.5.0",
        commit="d" * 40,
        image="ghcr.io/john-miracleworker/kestrel",
    )
    validate_oci_index(
        {
            "digest": index_digest,
            "manifests": [
                {
                    "digest": amd64_digest,
                    "platform": {"os": "linux", "architecture": "amd64"},
                },
                {
                    "digest": arm64_digest,
                    "platform": {"os": "linux", "architecture": "arm64"},
                },
            ],
        },
        record,
    )
    with pytest.raises(ValueError, match="identity mismatch"):
        validate_oci_record(
            record,
            repository="attacker/Kestrel",
            tag="v0.5.0",
            commit="d" * 40,
            image="ghcr.io/john-miracleworker/kestrel",
        )
    with pytest.raises(ValueError, match="index digest mismatch"):
        validate_oci_index({"digest": "sha256:" + "e" * 64, "manifests": []}, record)


def test_release_asset_guard_rejects_unknown_missing_and_changed_assets(
    tmp_path: Path,
) -> None:
    local = tmp_path / "local"
    downloaded = tmp_path / "downloaded"
    local.mkdir()
    downloaded.mkdir()
    (local / "one.whl").write_bytes(b"wheel")
    (local / "SHA256SUMS").write_bytes(b"sum")
    release = {
        "assets": [
            {"id": 1, "name": "one.whl"},
            {"id": 2, "name": "SHA256SUMS"},
        ]
    }

    assert validate_release_assets(local, release, allow_missing=False) == {
        "one.whl": 1,
        "SHA256SUMS": 2,
    }
    shutil_release = {"assets": [{"id": 1, "name": "one.whl"}]}
    validate_release_assets(local, shutil_release, allow_missing=True)
    with pytest.raises(ValueError, match="missing=.*SHA256SUMS"):
        validate_release_assets(local, shutil_release, allow_missing=False)
    with pytest.raises(ValueError, match="unknown=.*attacker.txt"):
        validate_release_assets(
            local,
            {"assets": [*release["assets"], {"id": 3, "name": "attacker.txt"}]},
            allow_missing=False,
        )

    (downloaded / "one.whl").write_bytes(b"wheel")
    (downloaded / "SHA256SUMS").write_bytes(b"sum")
    validate_release_assets(local, release, allow_missing=False, downloaded_root=downloaded)
    (downloaded / "one.whl").write_bytes(b"changed")
    with pytest.raises(ValueError, match="SHA-256 mismatch for one.whl"):
        validate_release_assets(local, release, allow_missing=False, downloaded_root=downloaded)


def test_pypi_partial_recovery_skips_only_exact_existing_files(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    wheel = tmp_path / "nested_memvid_agent-0.5.0-py3-none-any.whl"
    sdist = tmp_path / "nested_memvid_agent-0.5.0.tar.gz"
    wheel_record = {
        "filename": wheel.name,
        "digests": {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "yanked": False,
    }
    remote = {"info": {"version": "0.5.0"}, "urls": [wheel_record]}

    assert plan_pypi_files(tmp_path, remote, expected_version="0.5.0") == [sdist]
    exact = {
        "info": {"version": "0.5.0"},
        "urls": [
            wheel_record,
            {
                "filename": sdist.name,
                "digests": {"sha256": hashlib.sha256(sdist.read_bytes()).hexdigest()},
                "yanked": False,
            },
        ],
    }
    assert plan_pypi_files(tmp_path, exact, expected_version="0.5.0") == []

    mismatched = json.loads(json.dumps(remote))
    mismatched["urls"][0]["digests"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="PyPI SHA-256 mismatch"):
        plan_pypi_files(tmp_path, mismatched, expected_version="0.5.0")
    yanked = json.loads(json.dumps(remote))
    yanked["urls"][0]["yanked"] = True
    with pytest.raises(ValueError, match="PyPI file is yanked"):
        plan_pypi_files(tmp_path, yanked, expected_version="0.5.0")
    with pytest.raises(ValueError, match="unexpected files"):
        plan_pypi_files(
            tmp_path,
            {
                "info": {"version": "0.5.0"},
                "urls": [
                    *exact["urls"],
                    {
                        "filename": "attacker-0.5.0.whl",
                        "digests": {"sha256": "0" * 64},
                    },
                ],
            },
            expected_version="0.5.0",
        )


def test_oci_record_builder_rejects_non_digest_inputs() -> None:
    with pytest.raises(ValueError, match="invalid index digest"):
        build_oci_record(
            repository="John-MiracleWorker/Kestrel",
            tag="v0.5.0",
            commit="d" * 40,
            image="ghcr.io/john-miracleworker/kestrel",
            index_digest="latest",
            amd64_digest="sha256:" + "b" * 64,
            arm64_digest="sha256:" + "c" * 64,
        )


def test_release_workflow_builds_once_then_tests_the_exact_wheel_matrix() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert workflow.index("  build-release-candidate:") < workflow.index("  cross-platform:")
    assert "needs: build-release-candidate" in workflow
    assert '- os: windows-latest\n            python: "3.11"' in workflow
    assert '- os: windows-latest\n            python: "3.13"' in workflow
    assert workflow.count("- os: ubuntu-latest") == 3
    assert workflow.count("- os: macos-latest") == 3
    # Intel-macOS rows were removed in v0.4.8: cryptography 49+ no longer ships
    # Intel wheels, so pip cannot resolve a binary distribution there.
    assert workflow.count("- os: windows-latest") == 3
    assert workflow.count("runs-on: ${{ matrix.os }}") == 1
    assert workflow.count("machine: arm64") == 3
    assert workflow.count("machine: x86_64") == 3
    assert "machine: AMD64" in workflow
    assert "Verify runner architecture matches the matrix label" in workflow
    assert "actual=platform.machine().casefold()" in workflow
    assert "expected='${{ matrix.machine }}'.casefold()" in workflow
    assert workflow.count("Build Python release artifacts") == 1
    assert "python -m build --no-isolation --outdir dist" in workflow
    assert "python scripts/verify_release_payload.py dist --expected-version" in workflow
    assert "python -m scripts.verify_exact_wheel_install dist" in workflow
    assert "powershell -NoProfile -ExecutionPolicy Bypass -File dist/install.ps1" in workflow
    assert "importlib.metadata.version" in workflow
    assert "cross-platform release wheel smoke" in (
        ROOT / "scripts" / "verify_exact_wheel_install.py"
    ).read_text(encoding="utf-8")
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow
    assert "- cross-platform\n      - build-release-candidate" in workflow
    assert "pip install --upgrade pip" not in workflow


def test_release_payload_stages_and_checksums_windows_diagnostics() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "cp install.ps1 dist/install.ps1" in workflow
    assert 'Path("dist/install.ps1")' in workflow
    assert "sha256sum install.sh install.ps1 requirements-release.txt" in workflow


def test_release_requires_successful_exact_sha_main_ci_before_build() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "actions: read\n  contents: read" in workflow
    assert "Require successful exact-SHA main CI" in workflow
    assert 'actions/workflows/ci.yml/runs"' in workflow
    assert "RELEASE_COMMIT_SHA=%s" in workflow
    assert '-f head_sha="$RELEASE_COMMIT_SHA"' in workflow
    assert "-f branch=main" in workflow
    assert 'run.get("conclusion") == "success"' in workflow
    assert workflow.index("Require successful exact-SHA main CI") < workflow.index(
        "Build Python release artifacts"
    )


def test_release_secret_scan_materializes_only_exact_candidate_source() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert 'git archive --format=tar "$RELEASE_COMMIT_SHA"' in workflow
    assert '-v "$candidate_source:/repo:ro"' in workflow
    assert "dir --redact=100 --no-banner ." in workflow
    assert "git --redact=100 --no-banner ." not in workflow


def test_release_requires_exact_tagged_installer_supervisor() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "supervisor=scripts/installer-server-supervisor.sh" in workflow
    assert 'git ls-files --error-unmatch "$supervisor"' in workflow
    assert 'git cat-file -e "$TAG_COMMIT:$supervisor"' in workflow
    assert 'git hash-object "$supervisor"' in workflow


def test_staged_release_workflow_rejects_all_artifact_url_overrides() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    for variable in (
        "KESTREL_REQUIREMENTS_URL",
        "KESTREL_WHEEL_URL",
        "KESTREL_CHECKSUMS_URL",
    ):
        assert f"{variable}=https://example.invalid/" in workflow
        assert f"accepted {variable} override" in workflow


def test_release_revalidates_the_current_remote_tag_without_fetching_it() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    publish = workflow.split("  publish:", 1)[1]

    assert 'direct_ref="refs/tags/$GITHUB_REF_NAME"' in publish
    assert 'peeled_ref="${direct_ref}^{}"' in publish
    assert 'git ls-remote --tags origin "$direct_ref" "$peeled_ref"' in publish
    assert 'remote_tag_commit="${peeled_sha:-$direct_sha}"' in publish
    assert 'test "$remote_tag_commit" = "$event_commit"' in publish
    assert "git fetch --no-tags origin refs/heads/main" in publish
    assert 'git merge-base --is-ancestor "$event_commit" "$remote_main_commit"' in publish
    assert "git fetch --tags" not in publish
    assert publish.count('git ls-remote --tags origin "$direct_ref" "$peeled_ref"') == 4
    first_remote_check = publish.index('git ls-remote --tags origin "$direct_ref" "$peeled_ref"')
    assert first_remote_check < publish.index(
        "Publish exact images without overwriting conflicting GHCR refs"
    )
    assert first_remote_check < publish.index('docker push "$candidate"')
    assert publish.index("Revalidate the current remote tag") < publish.index(
        "Attest multi-architecture container provenance"
    )
    final_revalidation = publish.index(
        "Revalidate remote tag and publish version immediately before GitHub release"
    )
    draft_creation = publish.index("Create draft GitHub release and upload the complete payload")
    final_peel = publish.index("Revalidate remote tag and main after draft upload")
    immutable_publish = publish.index("Publish and verify immutable GitHub release")
    assert publish.index("Attest release payload provenance") < final_revalidation
    assert final_revalidation < draft_creation < final_peel < immutable_publish
    assert publish.index('gh release upload "$GITHUB_REF_NAME" dist/* --clobber') < final_peel
    assert final_peel < publish.index('gh release edit "$GITHUB_REF_NAME" --draft=false')
    immutable_gate = publish.index(
        "Require immutable GitHub releases before any publication mutation"
    )
    first_ghcr_mutation = publish.index(
        "Publish exact images without overwriting conflicting GHCR refs"
    )
    assert publish.index("Inspect an existing release before any GHCR mutation") < immutable_gate
    assert immutable_gate < first_ghcr_mutation < draft_creation
    assert immutable_gate < publish.index('docker push "$candidate"')


def test_release_publishes_only_verified_distributions_through_pypi_oidc() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    pypi = workflow.split("  publish-pypi:", 1)[1]

    assert "needs: publish" in pypi
    assert "environment:\n      name: pypi" in pypi
    assert "permissions:\n      actions: read\n      contents: read\n      id-token: write" in pypi
    assert "secrets." not in pypi
    assert "Download exact immutable release payload" in pypi
    assert "name: kestrel-published-release-${{ github.sha }}" in pypi
    assert "name: kestrel-release-${{ github.sha }}\n" not in pypi
    assert "sha256sum -c SHA256SUMS" in pypi
    assert 'test "${#wheels[@]}" -eq 1' in pypi
    assert 'test "${#sdists[@]}" -eq 1' in pypi
    assert "release_publication_guard.py pypi-plan" in pypi
    assert "Compare every PyPI file by exact filename and SHA-256" in pypi
    assert "if: steps.pypi-state.outputs.upload_required == 'true'" in pypi
    assert "skip-existing" not in pypi
    assert ("pypa/gh-action-pypi-publish@ba38be9e461d3875417946c167d0b5f3d385a247") in pypi
    assert "packages-dir: pypi-dist/" in pypi


def test_release_transfers_scanned_images_and_publishes_exact_multiarch_manifest() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    build = workflow.split("  build-release-candidate:", 1)[1].split("  cross-platform:", 1)[0]
    publish = workflow.split("  publish:", 1)[1]

    assert build.index("check_container_vulnerabilities.py") < build.index('docker save "$image"')
    assert "name: kestrel-containers-${{ github.sha }}" in build
    assert "name: kestrel-containers-${{ github.sha }}" in publish
    assert "sha256sum -c SHA256SUMS" in publish
    assert "docker buildx build" not in publish
    for label in (
        "org.opencontainers.image.revision",
        "org.opencontainers.image.source",
        "org.opencontainers.image.version",
    ):
        assert label in build
        assert label in publish
    assert 'stable_arch="$IMAGE_NAME:sha-${GITHUB_SHA}-${architecture}"' in publish
    assert '--tag "$stable_index"' in publish
    assert '--tag "$version_ref"' in publish
    assert '"$IMAGE_NAME@$amd64_digest"' in publish
    assert '"$IMAGE_NAME@$arm64_digest"' in publish
    assert '"$IMAGE_NAME:sha-${GITHUB_SHA}-amd64" \\' not in publish
    assert "kestrel-${architecture}-pushed.digest" in publish
    assert 'r"(?m)^[^\\s]+: digest: (sha256:[0-9a-f]{64}) size:' in publish
    assert '("linux", "amd64"): amd64_digest_path.read_text' in publish
    assert '("linux", "arm64"): arm64_digest_path.read_text' in publish
    assert "platforms != expected" in publish
    assert "len(descriptors) != 2" in publish
    assert 'docker pull --platform "linux/${architecture}"' in publish
    assert 'docker run --rm --platform "linux/${architecture}"' in publish
    assert "nest-agent doctor" in publish


def test_release_reruns_are_noop_or_collision_safe_across_publication_surfaces() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    publish = workflow.split("  publish:", 1)[1]

    release_probe = publish.index("Inspect an existing release before any GHCR mutation")
    first_registry_write = publish.index('docker push "$candidate"')
    assert release_probe < first_registry_write
    assert 'test "$(jq -r \'.immutable // false\' "$release_json")" = true' in publish
    assert "published OCI digest record does not match SHA256SUMS" in publish
    assert publish.count("release_publication_guard.py verify-oci-index") == 1
    assert 'release_commit="$(git rev-parse "$GITHUB_SHA^{commit}")"' in publish
    assert 'platform_ref="$IMAGE_NAME:sha-${GITHUB_SHA}-${architecture}"' in publish
    assert 'test "$actual_digest" = "$expected_digest"' in publish
    assert "printf 'complete=true\\n' >> \"$GITHUB_OUTPUT\"" in publish
    assert publish.count("if: steps.release-state.outputs.complete != 'true'") >= 10

    assert (
        'candidate="$IMAGE_NAME:candidate-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${architecture}"'
        in publish
    )
    assert (
        'candidate_index="$IMAGE_NAME:candidate-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-index"'
        in publish
    )
    assert 'echo "refusing to overwrite $reference@$existing with $expected"' in publish
    assert 'echo "refusing to overwrite $version_ref@$version_digest' in publish
    assert 'require_absent_ref "$candidate"' in publish
    assert 'ensure_exact_ref "$stable_arch" "$candidate_digest"' in publish
    assert 'ensure_exact_ref "$stable_index" "$index_digest"' in publish
    assert "--prefer-index=false" in publish

    record = publish.index("Bind the verified OCI digest into the release payload")
    payload_attestation = publish.index("Attest release payload provenance")
    draft = publish.index("Create draft GitHub release and upload the complete payload")
    assert first_registry_write < record < payload_attestation < draft
    assert "write-oci-record dist" in publish

    immutable_publish = publish.index("Publish and verify immutable GitHub release")
    full_download = publish.index("Download and verify the complete immutable release payload")
    downstream_upload = publish.index("Upload exact immutable payload for PyPI recovery")
    assert immutable_publish < full_download < downstream_upload
    immutable_step = publish[full_download:downstream_upload]
    assert "kestrel-immutable-release.json" in immutable_step
    assert "verify-release-assets" in immutable_step
    assert "verify_release_payload.py" in immutable_step
    assert "verify-oci-record" in immutable_step
    assert '"repos/$GITHUB_REPOSITORY/releases/assets/$asset_id"' in immutable_step
    assert "oci-image-digests.json" in immutable_step
    assert (
        "if: steps.release-state.outputs.complete != 'true'"
        not in publish[full_download : publish.index("Remove registry credentials from the runner")]
    )


def test_draft_release_assets_are_exact_and_digest_verified_before_publish() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    publish = workflow.split("  publish:", 1)[1]
    draft = publish.index("Create draft GitHub release and upload the complete payload")
    upload = publish.index('gh release upload "$GITHUB_REF_NAME" dist/* --clobber')
    exact = publish.index('--downloaded-root "$downloaded"')
    final_revalidation = publish.index("Revalidate remote tag and main after draft upload")
    immutable_publish = publish.index("Publish and verify immutable GitHub release")

    assert draft < upload < exact < final_revalidation < immutable_publish
    assert "verify-release-assets" in publish
    assert "--allow-missing" in publish
    assert publish.count("kestrel-draft-after-upload.json") >= 4
    assert '"repos/$GITHUB_REPOSITORY/releases/assets/$asset_id"' in publish


def test_container_publish_is_least_privilege_and_attested_by_digest() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    before_publish, publish = workflow.split("  publish:", 1)

    assert "packages: write" not in before_publish
    assert publish.count("packages: write") == 1
    assert "GHCR_TOKEN: ${{ github.token }}" in publish
    assert "push-to-registry: true" in publish
    assert "subject-name: ${{ steps.image.outputs.name }}" in publish
    assert "subject-digest: ${{ steps.verify-image.outputs.digest }}" in publish
    assert publish.index("Verify public multi-architecture manifest") < publish.index(
        "Attest multi-architecture container provenance"
    )
    assert publish.index("docker logout ghcr.io") < publish.index(
        "Verify public multi-architecture manifest"
    )


def test_release_push_digest_capture_accepts_one_unambiguous_docker_result() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    pattern = r"(?m)^[^\s]+: digest: (sha256:[0-9a-f]{64}) size: [0-9]+\s*$"
    digest = "sha256:" + "a" * 64
    output = (
        "The push refers to repository [ghcr.io/example/kestrel]\n"
        "layer: Layer already exists\n"
        f"sha-deadbeef-amd64: digest: {digest} size: 1234\n"
    )

    assert re.findall(pattern, output) == [digest]
    assert re.findall(pattern, output + output) == [digest, digest]
    assert 'r"(?m)^[^\\s]+: digest: (sha256:[0-9a-f]{64}) size:' in workflow


def test_trivy_dockerfile_exception_is_exact_not_rule_wide() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    policy = (ROOT / "config" / "trivy-dockerfile-ignore.rego").read_text(encoding="utf-8")

    assert "--ignore-policy config/trivy-dockerfile-ignore.rego" in workflow
    assert "--severity HIGH,CRITICAL" in workflow
    assert "--exit-code 1" in workflow
    assert "default ignore = false" in policy
    assert 'input.ID == "DS-0031"' in policy
    assert 'input.Namespace == "builtin.dockerfile.DS031"' in policy
    assert 'input.CauseMetadata.Provider == "Dockerfile"' in policy
    assert "nonsecret_runtime_messages[input.Message]" in policy
    assert policy.count("Possible exposure of secret env") == 3
    for name in (
        "NEST_AGENT_REQUIRE_API_AUTH",
        "NEST_AGENT_SECRET_BACKEND",
        "NEST_AGENT_SECRET_STORE_PATH",
    ):
        assert name in policy


def test_docker_builds_kestrel_and_memvid_without_isolated_resolution() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    memvid = next(package for package in lock["package"] if package["name"] == "memvid-sdk")

    assert "config/release-build-bootstrap.txt" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--only-binary=:all:" in dockerfile
    assert "pip wheel \\" in dockerfile
    assert dockerfile.count("--no-build-isolation") >= 2
    assert memvid["sdist"]["url"] in dockerfile
    assert memvid["sdist"]["hash"].removeprefix("sha256:") in dockerfile
    assert 'pip install --no-deps --no-build-isolation -e ".[${INSTALL_EXTRAS}]"' in dockerfile
    assert "pip install --upgrade pip" not in dockerfile


def test_pyproject_declares_exact_release_build_frontends() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))

    assert pyproject["build-system"]["requires"] == ["setuptools==83.0.0"]
    assert pyproject["project"]["requires-python"] == ">=3.11,<3.14"
    assert lock["requires-python"] == ">=3.11, <3.14"
    assert "build==1.5.0" in pyproject["project"]["optional-dependencies"]["dev"]
    assert pyproject["dependency-groups"]["release"] == [
        "cyclonedx-bom==7.3.0",
        "pip-audit==2.10.1",
        "pyinstaller==6.21.0",
        "twine==6.2.0",
    ]
