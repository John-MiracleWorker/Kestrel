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
from pathlib import Path

import pytest
import yaml

import scripts.verify_exact_wheel_install as exact_wheel_install
from scripts.release_publication_guard import (
    OCI_RECORD_NAME,
    build_oci_record,
    plan_pypi_files,
    validate_oci_index,
    validate_oci_record,
    validate_release_assets,
    write_oci_record,
)
from scripts.verify_release_payload import verify_release_payload

ROOT = Path(__file__).resolve().parents[1]


def test_release_candidate_workflow_is_read_only_and_expression_safe() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-candidate.yml"
    workflow = workflow_path.read_text(encoding="utf-8")

    assert "^[0-9a-f]{64}$" in workflow
    assert "^sha256:[0-9a-f]{64}$" in workflow
    assert "kestrel-dispatch-identity-${{ github.run_id }}-1" in workflow
    identity_block = workflow.split(
        "Create the canonical candidate dispatch identity", 1
    )[1].split("Upload immutable candidate dispatch identity", 1)[0]
    assert '"producer": "scripts/release_control_receipt.py"' in identity_block
    assert 'git archive --format=tar "$CANDIDATE_SOURCE_SHA"' in workflow
    assert "candidate-manifest.json" in workflow
    assert workflow.count("release_candidate_manifest verify") == 2
    assert "retention-days: 30" in workflow
    assert (
        "name: kestrel-release-candidate-${{ inputs.version }}-${{ inputs.source_sha }}"
        in workflow
    )
    assert "persist-credentials: false" in workflow
    assert 'actor != "John-MiracleWorker"' in workflow
    assert "'.owner.id'" in workflow
    assert "'.owner.login'" in workflow

    for action in (
        "actions/checkout@34e114876b0b11c390a56381ad16ebd13914f8d5",
        "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
        "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
        "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093",
    ):
        assert action in workflow
    assert not re.search(r"uses:\s+[^\s#]+@v[0-9]+", workflow)

    for receipt in (
        "kestrel.check.protected-main-ci.v1",
        "kestrel.check.release-rehearsal.v1",
        "kestrel.check.runtime-reliability-qualification.v1",
        "kestrel.check.release-payload.v1",
        "kestrel.check.nine-row-exact-wheel.v1",
        "kestrel.check.oci-layout.v1",
    ):
        assert receipt in workflow
    assert "run_attempt == 1" in workflow
    assert "qualification/receipts" in workflow
    for exact_observation_join in (
        'run.get("id") != selected_run_ids[name]',
        'run.get("head_sha") != os.environ["CANDIDATE_SOURCE_SHA"]',
        'run.get("head_branch") != "main"',
        'str(run.get("path", "")).split("@", 1)[0] != workflow_path',
        'repository.get("id") != int(os.environ["CANDIDATE_REPOSITORY_ID"])',
        'job.get("run_id") != run["id"]',
        '/attempts/1/jobs?per_page=100',
        'workflow_run.get("repository_id")',
    ):
        assert exact_observation_join in workflow

    forbidden = (
        "git tag ",
        "git push ",
        "gh release create",
        "gh release upload",
        "twine upload",
        "gh attestation",
        "docker push",
        "id-token: write",
        "packages: write",
        "contents: write",
    )
    for fragment in forbidden:
        assert fragment not in workflow

    parsed_workflow = yaml.safe_load(workflow)
    run_blocks = [
        step["run"]
        for job in parsed_workflow["jobs"].values()
        for step in job.get("steps", [])
        if isinstance(step.get("run"), str)
    ]
    assert run_blocks
    assert all("${{ inputs." not in block for block in run_blocks)


def test_release_candidate_workflow_keeps_the_exact_nine_row_wheel_matrix() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )

    matrix = workflow.split("  cross-platform-exact-wheel:", 1)[1].split(
        "  finalize-candidate:", 1
    )[0]
    assert matrix.count("- os: ubuntu-latest") == 3
    assert matrix.count("- os: macos-latest") == 3
    assert matrix.count("- os: windows-latest") == 3
    for version in ("3.11", "3.12", "3.13"):
        assert matrix.count(f'python: "{version}"') == 3
    assert matrix.count("machine: x86_64") == 3
    assert matrix.count("machine: arm64") == 3
    assert matrix.count("machine: AMD64") == 3
    assert "python -m scripts.verify_exact_wheel_install" in matrix
    assert "python -m build" not in matrix


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
    wheel_metadata_version: str = "0.5.2",
    sdist_metadata_version: str = "0.5.2",
    sbom_version: str = "0.5.2",
) -> None:
    wheel = root / "nested_memvid_agent-0.5.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "nested_memvid_agent-0.5.2.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            "Name: nested-memvid-agent\n"
            f"Version: {wheel_metadata_version}\n",
        )
    sdist = root / "nested_memvid_agent-0.5.2.tar.gz"
    package_info = (
        f"Metadata-Version: 2.4\nName: nested-memvid-agent\nVersion: {sdist_metadata_version}\n"
    ).encode()
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo("nested_memvid_agent-0.5.2/PKG-INFO")
        member.size = len(package_info)
        archive.addfile(member, io.BytesIO(package_info))
    artifacts = {
        "install.sh": b"#!/usr/bin/env bash\n",
        "install.ps1": b'param([string] $Version = "0.5.2")\n',
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

    report = verify_release_payload(tmp_path, expected_version="v0.5.2")

    assert report["verified"] is True
    assert report["artifact_count"] == 6
    assert report["requirement_count"] == 1
    assert report["distribution"] == "nested-memvid-agent"
    assert report["version"] == "0.5.2"

    (tmp_path / "install.sh").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256 mismatch for install.sh"):
        verify_release_payload(tmp_path, expected_version="0.5.2")


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
        verify_release_payload(tmp_path, expected_version="0.5.2")


def test_release_payload_verifier_rejects_filename_identity_drift(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    wheel = tmp_path / "nested_memvid_agent-0.5.2-py3-none-any.whl"
    wheel.rename(tmp_path / "nested_memvid_agent-0.4.12-py3-none-any.whl")
    _rewrite_manifest(tmp_path)

    with pytest.raises(ValueError, match="wheel filename version mismatch"):
        verify_release_payload(tmp_path, expected_version="0.5.2")


def test_release_payload_verifier_rejects_checksummed_unknown_artifact(tmp_path: Path) -> None:
    _write_payload(tmp_path)
    (tmp_path / "unexpected.bin").write_bytes(b"not part of the public release contract")
    _rewrite_manifest(tmp_path)

    with pytest.raises(ValueError, match="unexpected artifacts.*unexpected.bin"):
        verify_release_payload(tmp_path, expected_version="0.5.2")


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
        tag="v0.5.2",
        commit="d" * 40,
        image="ghcr.io/john-miracleworker/kestrel",
        index_digest=index_digest,
        amd64_digest=amd64_digest,
        arm64_digest=arm64_digest,
    )

    report = verify_release_payload(tmp_path, expected_version="v0.5.2")
    assert report["artifact_count"] == 7
    sums = (tmp_path / "SHA256SUMS").read_text(encoding="ascii")
    assert f"  {OCI_RECORD_NAME}\n" in sums
    record = validate_oci_record(
        json.loads(record_path.read_text(encoding="utf-8")),
        repository="John-MiracleWorker/Kestrel",
        tag="v0.5.2",
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
            tag="v0.5.2",
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
    wheel = tmp_path / "nested_memvid_agent-0.5.2-py3-none-any.whl"
    sdist = tmp_path / "nested_memvid_agent-0.5.2.tar.gz"
    wheel_record = {
        "filename": wheel.name,
        "digests": {"sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "yanked": False,
    }
    remote = {"info": {"version": "0.5.2"}, "urls": [wheel_record]}

    assert plan_pypi_files(tmp_path, remote, expected_version="0.5.2") == [sdist]
    exact = {
        "info": {"version": "0.5.2"},
        "urls": [
            wheel_record,
            {
                "filename": sdist.name,
                "digests": {"sha256": hashlib.sha256(sdist.read_bytes()).hexdigest()},
                "yanked": False,
            },
        ],
    }
    assert plan_pypi_files(tmp_path, exact, expected_version="0.5.2") == []

    mismatched = json.loads(json.dumps(remote))
    mismatched["urls"][0]["digests"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="PyPI SHA-256 mismatch"):
        plan_pypi_files(tmp_path, mismatched, expected_version="0.5.2")
    yanked = json.loads(json.dumps(remote))
    yanked["urls"][0]["yanked"] = True
    with pytest.raises(ValueError, match="PyPI file is yanked"):
        plan_pypi_files(tmp_path, yanked, expected_version="0.5.2")
    with pytest.raises(ValueError, match="unexpected files"):
        plan_pypi_files(
            tmp_path,
            {
                "info": {"version": "0.5.2"},
                "urls": [
                    *exact["urls"],
                    {
                        "filename": "attacker-0.5.2.whl",
                        "digests": {"sha256": "0" * 64},
                    },
                ],
            },
            expected_version="0.5.2",
        )


def test_oci_record_builder_rejects_non_digest_inputs() -> None:
    with pytest.raises(ValueError, match="invalid index digest"):
        build_oci_record(
            repository="John-MiracleWorker/Kestrel",
            tag="v0.5.2",
            commit="d" * 40,
            image="ghcr.io/john-miracleworker/kestrel",
            index_digest="latest",
            amd64_digest="sha256:" + "b" * 64,
            arm64_digest="sha256:" + "c" * 64,
        )


def test_release_workflow_builds_once_then_tests_the_exact_wheel_matrix() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )

    assert workflow.index("  build-release-candidate:") < workflow.index(
        "  cross-platform-exact-wheel:"
    )
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
    assert "needs: cross-platform-exact-wheel" in workflow
    assert "pip install --upgrade pip" not in workflow


def test_release_payload_stages_and_checksums_windows_diagnostics() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )

    assert "cp install.ps1 dist/install.ps1" in workflow
    assert 'Path("dist/install.ps1")' in workflow
    assert "sha256sum install.sh install.ps1 requirements-release.txt" in workflow


def test_release_requires_successful_exact_sha_main_ci_before_build() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )

    gate = workflow.index(
        "Prove the exact protected-main source and select attempt-one prerequisites"
    )
    build = workflow.index("Build Python release artifacts")
    assert "actions: read\n      contents: read" in workflow
    assert "actions/workflows/${path}/runs?head_sha=${CANDIDATE_SOURCE_SHA}" in workflow
    assert "branch=main&event=push&per_page=100" in workflow
    assert 'run.get("head_sha") == os.environ["CANDIDATE_SOURCE_SHA"]' in workflow
    assert 'run.get("conclusion") == "success"' in workflow
    assert gate < build


def test_release_secret_scan_materializes_only_exact_candidate_source() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )

    assert 'git archive --format=tar "$RELEASE_COMMIT_SHA"' in workflow
    assert '-v "$candidate_source:/repo:ro"' in workflow
    assert "dir --redact=100 --no-banner ." in workflow
    assert "git --redact=100 --no-banner ." not in workflow


def test_release_requires_exact_tagged_installer_supervisor() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")

    assert "scripts/installer-server-supervisor.sh" in installer
    assert 'git archive --format=tar "$CANDIDATE_SOURCE_SHA"' in workflow
    assert "Create the exact candidate source archive" in workflow
    assert "source_tree" in workflow


def test_staged_release_workflow_rejects_all_artifact_url_overrides() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )

    for variable in (
        "KESTREL_REQUIREMENTS_URL",
        "KESTREL_WHEEL_URL",
        "KESTREL_CHECKSUMS_URL",
    ):
        assert f"{variable}=https://example.invalid/" in workflow
        assert f"accepted {variable} override" in workflow


def test_staged_release_installer_uses_runner_owned_home_paths() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )
    staged_installer = workflow.split(
        "      - name: Validate staged release installer plan", 1
    )[1].split("      - name: Export locked default release dependencies", 1)[0]

    assert "KESTREL_HOME=/tmp/" not in staged_installer
    assert staged_installer.count('KESTREL_HOME="${RUNNER_TEMP}/kestrel-installer-') == 7


def test_release_transaction_atomically_creates_and_reobserves_the_tag() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["commit-github-release"]["steps"]
    by_name = {step.get("name"): step for step in steps if step.get("name")}
    execute = by_name["Atomically create the marker and publish the exact draft"]["run"]
    observe = by_name["Observe exact commit surfaces and create the commit plan"]["run"]
    record = by_name["Record commit outcome from fresh post-state"]

    assert "plan-commit" in observe
    assert "commit-tag-source.json" in observe
    tag_post = execute.index("/git/tags")
    ref_post = execute.index("/git/refs")
    release_patch = execute.index("/releases/")
    assert tag_post < ref_post < release_patch
    assert "transaction._product_release_publish_patch()" in execute
    assert "release_verification = run_gh" in execute
    assert '"verify-asset"' in execute
    assert record["if"] == "${{ always() }}"
    assert "commit-post-release-list.json" in record["run"]
    assert "git fetch --tags" not in execute
    assert "--clobber" not in execute


def test_release_publishes_only_verified_distributions_through_pypi_oidc() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    job = workflow["jobs"]["publish-pypi"]
    pypi = json.dumps(job, sort_keys=True)

    assert job["needs"] == "verify-github-ghcr"
    assert job["environment"] == {"name": "pypi"}
    assert job["permissions"] == {
        "actions": "read",
        "contents": "read",
        "id-token": "write",
    }
    assert "pypi-attestations 0.0.30" in pypi
    assert "pypi-attestations verify pypi --offline" in pypi
    assert "https://pypi.org/integrity/nested-memvid-agent/" in pypi
    assert "_verify_pypi_integrity_provenance" in pypi
    assert 'state[\\"missing\\"]' in pypi
    assert "skip-existing" not in pypi
    assert (
        "pypa/gh-action-pypi-publish@"
        "ba38be9e461d3875417946c167d0b5f3d385a247"
    ) in pypi
    assert "transaction/pypi-dist" in pypi
    publish = next(
        step for step in job["steps"] if step.get("name") == "Publish only the missing exact PyPI distributions"
    )
    assert publish["if"] == "${{ steps.pypi-plan.outputs.publish == 'create' }}"


def test_release_transfers_scanned_images_and_publishes_exact_multiarch_manifest() -> None:
    candidate = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )
    transaction = (
        ROOT / ".github" / "workflows" / "release-transaction.yml"
    ).read_text(encoding="utf-8")
    build = candidate.split("  build-release-candidate:", 1)[1].split(
        "  cross-platform-exact-wheel:", 1
    )[0]
    prepare = transaction.split("  prepare-github-ghcr:", 1)[1].split(
        "  commit-github-release:", 1
    )[0]

    assert build.index("check_container_vulnerabilities.py") < build.index(
        'RAW_OCI_ROOT="$raw_oci_root"'
    )
    assert "name: kestrel-containers-${{ inputs.source_sha }}" in build
    assert "containers/oci-descriptor.json" in build
    assert "docker buildx build" not in prepare
    for label in (
        "org.opencontainers.image.revision",
        "org.opencontainers.image.source",
        "org.opencontainers.image.version",
    ):
        assert label in build
    assert '"index_ref": f"{repository}@{index_digest}"' in build
    assert '"manifest_ref": f"{repository}@{manifest_digest}"' in build
    assert "transaction.fetch_ghcr_push_token(" in prepare
    assert 'principal=os.environ["PROMOTION_ACTOR"]' in prepare
    assert "transaction.DirectOCIWriteAPI(" in prepare
    assert "registry_writer.upload_blob(" in prepare
    assert "registry_writer.put_manifest(" in prepare
    assert "transaction._product_release_publish_patch()" in transaction
    assert '"make_latest": False' not in transaction
    assert "docker push" not in prepare
    assert "/manifests/{candidate" not in prepare


def test_release_reruns_are_noop_or_collision_safe_across_publication_surfaces() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    jobs = workflow["jobs"]
    prepare = json.dumps(jobs["prepare-github-ghcr"], sort_keys=True)
    commit = json.dumps(jobs["commit-github-release"], sort_keys=True)
    pypi = json.dumps(jobs["publish-pypi"], sort_keys=True)

    assert "plan-preparation" in prepare
    assert "plan-commit" in commit
    assert "_missing_product_release_assets" in prepare
    assert "uploaded Release asset is not exact by Release ID" in prepare
    assert 'for filename in state[\\"missing\\"]' in pypi
    assert "recover_committed" in prepare
    assert "release-execution-authorization.json" in prepare
    assert "--clobber" not in prepare + commit
    assert "skip-existing" not in pypi
    for job_name, outcome in (
        ("prepare-github-ghcr", "release-preparation-outcome.json"),
        ("commit-github-release", "release-commit-outcome.json"),
        ("publish-pypi", "release-pypi-outcome.json"),
    ):
        record = next(
            step
            for step in jobs[job_name]["steps"]
            if outcome in str(step.get("run", ""))
        )
        assert record["if"] == "${{ always() }}"


def test_draft_release_assets_are_exact_and_digest_verified_before_publish() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "release-transaction.yml").read_text(
            encoding="utf-8"
        )
    )
    prepare_steps = workflow["jobs"]["prepare-github-ghcr"]["steps"]
    commit_steps = workflow["jobs"]["commit-github-release"]["steps"]
    prepare = next(
        step["run"]
        for step in prepare_steps
        if step.get("name") == "Execute only the authorized preparation plan"
    )
    commit = next(
        step["run"]
        for step in commit_steps
        if step.get("name") == "Atomically create the marker and publish the exact draft"
    )

    assert "create_github_release_draft" in prepare
    assert "upload_github_release_assets" in prepare
    assert "_missing_product_release_assets" in prepare
    assert "_product_release_asset_upload_request" in prepare
    assert "_resolve_product_release_asset_upload_target" in prepare
    assert "authorized_release_id" in prepare
    assert "uploaded Release asset is not exact by Release ID" in prepare
    assert "candidate Release asset changed before upload" in prepare
    assert "transaction._product_release_publish_patch()" in commit
    assert "commit-last-moment-preconditions.json" in commit
    assert "_classify_ghcr_digest_observation" in commit
    assert "initiate marker no longer targets exact locked main" in commit
    assert "last-moment tag state differs from its plan" in commit
    assert "commit-last-moment-prepublish-release-list.json" in commit
    assert "product Release changed after marker verification" in commit
    assert "release_verification = run_gh" in commit
    assert '"verify-asset"' in commit
    assert "--clobber" not in prepare


def test_container_publish_is_least_privilege_and_attested_by_digest() -> None:
    workflow_path = ROOT / ".github" / "workflows" / "release-transaction.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]
    assert jobs["prepare-github-ghcr"]["permissions"]["packages"] == "write"
    assert jobs["commit-github-release"]["permissions"]["packages"] == "read"
    assert jobs["verify-github-ghcr"]["permissions"]["packages"] == "read"
    assert sum(
        job["permissions"].get("packages") == "write" for job in jobs.values()
    ) == 1

    attestation = next(
        step
        for step in jobs["commit-github-release"]["steps"]
        if step.get("name") == "Create only the missing OCI repository custom attestation"
    )
    assert attestation["with"]["subject-name"] == (
        "${{ steps.commit-plan.outputs.oci_name }}"
    )
    assert attestation["with"]["subject-digest"] == (
        "${{ steps.commit-plan.outputs.oci_digest }}"
    )
    assert attestation["with"]["predicate-type"] == (
        "https://kestrel.dev/attestations/release-promotion/v1"
    )
    assert "push-to-registry" not in attestation["with"]
    assert "create-storage-record" not in attestation["with"]
    assert workflow_text.count(
        "users/John-MiracleWorker/packages/container/kestrel/versions?per_page=100"
    ) == 7
    assert workflow_text.count("tags_by_digest.get(digest, [])") == 8
    assert workflow_text.count("transaction.DirectOCIReadAPI(") == 9
    assert workflow_text.count("registry_api.read_digest(") == 8
    assert workflow_text.count("max_bytes=2_147_483_648") == 10
    assert "response.read(2_147_483_649)" not in workflow_text
    assert '"tags": []' not in workflow_text


def test_release_uses_candidate_digests_not_docker_push_output() -> None:
    workflow = (
        ROOT / ".github" / "workflows" / "release-transaction.yml"
    ).read_text(encoding="utf-8")
    prepare = workflow.split("  prepare-github-ghcr:", 1)[1].split(
        "  commit-github-release:", 1
    )[0]

    assert "_expected_oci_object_digests" in prepare
    assert "transaction.fetch_ghcr_push_token(" in prepare
    assert "transaction.DirectOCIWriteAPI(" in prepare
    assert "registry_writer.put_manifest(" in prepare
    assert "transaction.DirectOCIReadAPI(" in prepare
    assert "registry_api.read_digest(" in prepare
    assert "max_bytes=2_147_483_648" in prepare
    assert "docker push" not in prepare
    assert r"digest: (sha256:[0-9a-f]{64}) size:" not in prepare


def test_trivy_dockerfile_exception_is_exact_not_rule_wide() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-candidate.yml").read_text(
        encoding="utf-8"
    )
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
        "pypi-attestations==0.0.30",
        "twine==6.2.0",
    ]
