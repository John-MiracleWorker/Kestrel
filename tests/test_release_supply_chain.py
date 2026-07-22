from __future__ import annotations

import hashlib
import io
import json
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

from scripts.verify_release_payload import verify_release_payload

ROOT = Path(__file__).resolve().parents[1]


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
        f"{hashlib.sha256(content).hexdigest()}  {name}\n"
        for name, content in artifacts.items()
    )
    (root / "SHA256SUMS").write_text(manifest, encoding="ascii")


def _write_payload(
    root: Path,
    *,
    wheel_metadata_version: str = "0.4.0",
    sdist_metadata_version: str = "0.4.0",
    sbom_version: str = "0.4.0",
) -> None:
    wheel = root / "nested_memvid_agent-0.4.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "nested_memvid_agent-0.4.0.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: nested-memvid-agent\n"
            f"Version: {wheel_metadata_version}\n",
        )
    sdist = root / "nested_memvid_agent-0.4.0.tar.gz"
    package_info = (
        "Metadata-Version: 2.4\n"
        "Name: nested-memvid-agent\n"
        f"Version: {sdist_metadata_version}\n"
    ).encode()
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo("nested_memvid_agent-0.4.0/PKG-INFO")
        member.size = len(package_info)
        archive.addfile(member, io.BytesIO(package_info))
    artifacts = {
        "install.sh": b"#!/usr/bin/env bash\n",
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
    assert {"build", "maturin", "uv"} <= {
        entry.split("==", 1)[0] for entry in release
    }
    for requirement in release:
        assert "==" in requirement
        assert "--hash=sha256:" in requirement


def test_release_payload_verifier_covers_every_artifact_and_detects_tampering(
    tmp_path: Path,
) -> None:
    _write_payload(tmp_path)

    report = verify_release_payload(tmp_path, expected_version="v0.4.0")

    assert report["verified"] is True
    assert report["artifact_count"] == 5
    assert report["requirement_count"] == 1
    assert report["distribution"] == "nested-memvid-agent"
    assert report["version"] == "0.4.0"

    (tmp_path / "install.sh").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256 mismatch for install.sh"):
        verify_release_payload(tmp_path, expected_version="0.4.0")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"wheel_metadata_version": "0.4.1"}, "wheel METADATA version mismatch"),
        ({"sdist_metadata_version": "0.4.1"}, "sdist PKG-INFO version mismatch"),
        ({"sbom_version": "0.4.1"}, "CycloneDX Kestrel component identity mismatch"),
    ],
)
def test_release_payload_verifier_rejects_internal_identity_drift(
    tmp_path: Path, kwargs: dict[str, str], message: str
) -> None:
    _write_payload(tmp_path, **kwargs)

    with pytest.raises(ValueError, match=message):
        verify_release_payload(tmp_path, expected_version="0.4.0")


def test_release_payload_verifier_rejects_filename_identity_drift(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    wheel = tmp_path / "nested_memvid_agent-0.4.0-py3-none-any.whl"
    wheel.rename(tmp_path / "nested_memvid_agent-0.4.1-py3-none-any.whl")
    _rewrite_manifest(tmp_path)

    with pytest.raises(ValueError, match="wheel filename version mismatch"):
        verify_release_payload(tmp_path, expected_version="0.4.0")


def test_release_workflow_builds_once_then_tests_the_exact_wheel_matrix() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.index("  build-release-candidate:") < workflow.index("  cross-platform:")
    assert "needs: build-release-candidate" in workflow
    assert "- os: windows-latest\n            python: \"3.11\"" in workflow
    assert workflow.count("- os: ubuntu-latest") == 3
    assert workflow.count("- os: macos-latest") == 3
    assert workflow.count("- os: macos-15\n            os_suffix: -intel") == 3
    assert workflow.count("format('{0}{1}', matrix.os, matrix.os_suffix)") == 2
    assert workflow.count("machine: arm64") == 3
    assert workflow.count("machine: x86_64") == 6
    assert "machine: AMD64" in workflow
    assert "Verify runner architecture matches the matrix label" in workflow
    assert "actual=platform.machine().casefold()" in workflow
    assert "expected='${{ matrix.machine }}'.casefold()" in workflow
    assert workflow.count("Build Python release artifacts") == 1
    assert "python -m build --no-isolation --outdir dist" in workflow
    assert "python scripts/verify_release_payload.py dist --expected-version" in workflow
    assert "python -m scripts.verify_exact_wheel_install dist" in workflow
    assert "importlib.metadata.version" in workflow
    assert "cross-platform release wheel smoke" in (
        ROOT / "scripts" / "verify_exact_wheel_install.py"
    ).read_text(encoding="utf-8")
    assert "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093" in workflow
    assert "- cross-platform\n      - build-release-candidate" in workflow
    assert "pip install --upgrade pip" not in workflow


def test_release_requires_successful_exact_sha_main_ci_before_build() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "actions: read\n  contents: read" in workflow
    assert "Require successful exact-SHA main CI" in workflow
    assert 'actions/workflows/ci.yml/runs"' in workflow
    assert 'RELEASE_COMMIT_SHA=%s' in workflow
    assert '-f head_sha="$RELEASE_COMMIT_SHA"' in workflow
    assert '-f branch=main' in workflow
    assert 'run.get("conclusion") == "success"' in workflow
    assert workflow.index("Require successful exact-SHA main CI") < workflow.index(
        "Build Python release artifacts"
    )


def test_release_requires_exact_tagged_installer_supervisor() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "supervisor=scripts/installer-server-supervisor.sh" in workflow
    assert 'git ls-files --error-unmatch "$supervisor"' in workflow
    assert 'git cat-file -e "$TAG_COMMIT:$supervisor"' in workflow
    assert 'git hash-object "$supervisor"' in workflow


def test_staged_release_workflow_rejects_all_artifact_url_overrides() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    for variable in (
        "KESTREL_REQUIREMENTS_URL",
        "KESTREL_WHEEL_URL",
        "KESTREL_CHECKSUMS_URL",
    ):
        assert f"{variable}=https://example.invalid/" in workflow
        assert f"accepted {variable} override" in workflow


def test_release_revalidates_the_current_remote_tag_without_fetching_it() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    publish = workflow.split("  publish:", 1)[1]

    assert 'direct_ref="refs/tags/$GITHUB_REF_NAME"' in publish
    assert 'peeled_ref="${direct_ref}^{}"' in publish
    assert 'git ls-remote --tags origin "$direct_ref" "$peeled_ref"' in publish
    assert 'remote_tag_commit="${peeled_sha:-$direct_sha}"' in publish
    assert 'test "$remote_tag_commit" = "$event_commit"' in publish
    assert "git fetch --no-tags origin refs/heads/main" in publish
    assert 'git merge-base --is-ancestor "$event_commit" "$remote_main_commit"' in publish
    assert "git fetch --tags" not in publish
    assert publish.count(
        'git ls-remote --tags origin "$direct_ref" "$peeled_ref"'
    ) == 4
    first_remote_check = publish.index(
        'git ls-remote --tags origin "$direct_ref" "$peeled_ref"'
    )
    assert first_remote_check < publish.index("Log in only for immutable image publication")
    assert first_remote_check < publish.index('docker push "$target"')
    assert publish.index("Revalidate the current remote tag") < publish.index(
        "Attest multi-architecture container provenance"
    )
    final_revalidation = publish.index(
        "Revalidate remote tag and publish version immediately before GitHub release"
    )
    final_peel = publish.index(
        "Re-peel the remote tag immediately before GitHub release creation"
    )
    assert publish.index("Attest release payload provenance") < final_revalidation
    assert final_revalidation < final_peel < publish.index("Publish GitHub release")


def test_release_transfers_scanned_images_and_publishes_exact_multiarch_manifest() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    build = workflow.split("  build-release-candidate:", 1)[1].split(
        "  cross-platform:", 1
    )[0]
    publish = workflow.split("  publish:", 1)[1]

    assert build.index("check_container_vulnerabilities.py") < build.index(
        'docker save "$image"'
    )
    assert 'name: kestrel-containers-${{ github.sha }}' in build
    assert 'name: kestrel-containers-${{ github.sha }}' in publish
    assert "sha256sum -c SHA256SUMS" in publish
    assert "docker buildx build" not in publish
    for label in (
        "org.opencontainers.image.revision",
        "org.opencontainers.image.source",
        "org.opencontainers.image.version",
    ):
        assert label in build
        assert label in publish
    assert 'target="$IMAGE_NAME:sha-${GITHUB_SHA}-${architecture}"' in publish
    assert '--tag "$IMAGE_NAME:sha-${GITHUB_SHA}"' in publish
    assert '--tag "$IMAGE_NAME:$GITHUB_REF_NAME"' in publish
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


def test_container_publish_is_least_privilege_and_attested_by_digest() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
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
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
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
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    policy = (ROOT / "config" / "trivy-dockerfile-ignore.rego").read_text(
        encoding="utf-8"
    )

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
    assert (
        'pip install --no-deps --no-build-isolation -e ".[${INSTALL_EXTRAS}]"'
        in dockerfile
    )
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
        "twine==6.2.0",
    ]
