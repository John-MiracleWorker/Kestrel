from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bootstrap_recovery_capsule_controller.py"
SOURCE_SHA = "a" * 40


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


@pytest.fixture
def subject() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "bootstrap_recovery_capsule_controller_test_subject",
        SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bootstrap_module_has_only_stdlib_imports() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.partition(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.partition(".")[0])

    assert imported <= sys.stdlib_module_names
    assert not imported & {"cryptography", "jsonschema", "kestrel", "scripts"}


def test_initial_bootstrap_rejects_source_before_transport_or_state(
    tmp_path: Path, subject: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    bootstrap_root = tmp_path / "bootstrap"
    calls: list[str] = []
    request = subject.InitialBootstrapRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        staging_run_id=701,
        staging_artifact_id=702,
        staging_artifact_digest="sha256:" + "b" * 64,
        pinned_gh=tmp_path / "gh",
        bootstrap_root=bootstrap_root,
        controller_arguments=("--fixture",),
    )
    monkeypatch.setattr(subject, "_require_preimport_runtime", lambda _request: None)

    def reject_source(_request: object) -> object:
        calls.append("source")
        raise subject.BootstrapError("source mismatch")

    monkeypatch.setattr(subject, "_require_source_identity", reject_source)
    monkeypatch.setattr(
        subject,
        "_acquire_staging_artifact",
        lambda *_args, **_kwargs: calls.append("transport"),
    )

    with pytest.raises(ValueError, match="source mismatch"):
        subject.run_initial_bootstrap(request)

    assert calls == ["source"]
    assert not bootstrap_root.exists()


def test_artifact_extractor_rejects_traversal_without_partial_output(
    tmp_path: Path, subject: ModuleType
) -> None:
    archive = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
        safe = zipfile.ZipInfo("recovery/requirements.txt")
        safe.create_system = 3
        safe.external_attr = (stat.S_IFREG | 0o600) << 16
        handle.writestr(safe, b"fixture==1 --hash=sha256:" + b"1" * 64 + b"\n")
        unsafe = zipfile.ZipInfo("../escaped")
        unsafe.create_system = 3
        unsafe.external_attr = (stat.S_IFREG | 0o600) << 16
        handle.writestr(unsafe, b"escaped")
    output = tmp_path / "extracted"

    with pytest.raises(ValueError, match="unsafe"):
        subject.safe_extract_actions_artifact(archive, output)

    assert not output.exists()
    assert not (tmp_path / "escaped").exists()


def test_staging_artifact_rejects_direct_metadata_substitution_before_download(
    tmp_path: Path, subject: ModuleType
) -> None:
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
        info = zipfile.ZipInfo("recovery/fixture.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        handle.writestr(info, b"fixture")
    archive_raw = archive.read_bytes()
    artifact = {
        "id": 702,
        "name": f"kestrel-recovery-dependencies-{SOURCE_SHA}",
        "size_in_bytes": len(archive_raw),
        "expired": False,
        "digest": _sha256(archive_raw),
        "workflow_run": {
            "id": 701,
            "repository_id": 303,
            "head_repository_id": 303,
            "head_branch": "main",
            "head_sha": SOURCE_SHA,
        },
    }
    run = {
        "id": 701,
        "workflow_id": 88,
        "path": ".github/workflows/recovery-dependency-staging.yml",
        "event": "workflow_dispatch",
        "head_branch": "main",
        "run_attempt": 1,
        "head_sha": SOURCE_SHA,
        "status": "completed",
        "conclusion": "success",
        "repository": {"id": 303, "full_name": "John-MiracleWorker/Kestrel"},
    }
    direct = dict(artifact)
    direct["id"] = 999
    responses = iter(
        (
            _canonical(run),
            _canonical([{"total_count": 1, "artifacts": [artifact]}]),
            _canonical(direct),
        )
    )
    calls: list[str] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        endpoint = command[-1]
        calls.append(endpoint)
        if endpoint.endswith("/zip"):
            raise AssertionError("substituted metadata must fail before download")
        return subprocess.CompletedProcess(command, 0, next(responses), b"")

    request = subject.InitialBootstrapRequest(
        source_root=tmp_path,
        source_sha=SOURCE_SHA,
        staging_run_id=701,
        staging_artifact_id=702,
        staging_artifact_digest=_sha256(archive_raw),
        pinned_gh=tmp_path / "gh",
        bootstrap_root=tmp_path / "bootstrap",
        controller_arguments=("--fixture",),
    )
    output = tmp_path / "staging-artifact"

    with pytest.raises(ValueError, match="metadata|identity"):
        subject.acquire_staging_artifact(
            request,
            token=b"owner-token-for-test",
            output_root=output,
            runner=runner,
        )

    assert len(calls) == 3
    assert not output.exists()


@pytest.mark.skipif(os.name == "nt", reason="recovery capsule bootstrap requires POSIX (fchmod, ELF, bubblewrap)")
def test_staging_artifact_exact_resume_revalidates_without_transport(
    tmp_path: Path, subject: ModuleType
) -> None:
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as handle:
        info = zipfile.ZipInfo("recovery/fixture.txt")
        info.create_system = 3
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        handle.writestr(info, b"fixture")
    archive_raw = archive.read_bytes()
    artifact = {
        "id": 702,
        "name": f"kestrel-recovery-dependencies-{SOURCE_SHA}",
        "size_in_bytes": len(archive_raw),
        "expired": False,
        "digest": _sha256(archive_raw),
        "workflow_run": {
            "id": 701,
            "repository_id": 303,
            "head_repository_id": 303,
            "head_branch": "main",
            "head_sha": SOURCE_SHA,
        },
    }
    run = {
        "id": 701,
        "path": ".github/workflows/recovery-dependency-staging.yml",
        "event": "workflow_dispatch",
        "run_attempt": 1,
        "head_sha": SOURCE_SHA,
        "status": "completed",
        "conclusion": "success",
        "repository": {"id": 303, "full_name": "John-MiracleWorker/Kestrel"},
    }
    responses = iter(
        (
            _canonical(run),
            _canonical([{"total_count": 1, "artifacts": [artifact]}]),
            _canonical(artifact),
        )
    )

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        if command[-1].endswith("/zip"):
            destination = kwargs["stdout"]
            assert hasattr(destination, "write")
            destination.write(archive_raw)  # type: ignore[union-attr]
            return subprocess.CompletedProcess(command, 0, b"", b"")
        return subprocess.CompletedProcess(command, 0, next(responses), b"")

    request = subject.InitialBootstrapRequest(
        source_root=tmp_path,
        source_sha=SOURCE_SHA,
        staging_run_id=701,
        staging_artifact_id=702,
        staging_artifact_digest=_sha256(archive_raw),
        pinned_gh=tmp_path / "gh",
        bootstrap_root=tmp_path / "bootstrap",
        controller_arguments=("--fixture",),
    )
    output = tmp_path / "staging-artifact"
    first = subject.acquire_staging_artifact(
        request,
        token=b"owner-token-for-test",
        output_root=output,
        runner=runner,
    )

    def forbidden_transport(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an exact local artifact resume must not use transport")

    second = subject.acquire_staging_artifact(
        request,
        token=b"owner-token-for-test",
        output_root=output,
        runner=forbidden_transport,
    )

    assert first == second == output / "contents"

    (output / "contents/recovery/fixture.txt").write_bytes(b"substituted")
    with pytest.raises(ValueError, match="contents changed"):
        subject.acquire_staging_artifact(
            request,
            token=b"owner-token-for-test",
            output_root=output,
            runner=forbidden_transport,
        )


def _staging_fixture(tmp_path: Path) -> tuple[Path, Path]:
    source_root = tmp_path / "source"
    (source_root / "config").mkdir(parents=True)
    requirements = b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n"
    (source_root / "config/recovery-requirements.txt").write_bytes(requirements)
    root = tmp_path / "artifact-contents"
    recovery = root / "recovery"
    wheelhouse = recovery / "wheelhouse"
    runtime_root = recovery / "runtime"
    bin_root = recovery / "bin"
    wheelhouse.mkdir(parents=True)
    runtime_root.mkdir()
    bin_root.mkdir()
    wheel = b"fixture wheel bytes"
    wheel_name = "fixture-1.0-py3-none-any.whl"
    (wheelhouse / wheel_name).write_bytes(wheel)
    wheel_manifest = _canonical(
        {
            "schema": "kestrel.recovery_wheelhouse.v1",
            "wheels": [
                {
                    "filename": wheel_name,
                    "sha256": _sha256(wheel),
                    "size_bytes": len(wheel),
                }
            ],
        }
    )
    runtime = b"runtime library"
    (runtime_root / "libfixture.so").write_bytes(runtime)
    runtime_manifest = _canonical(
        {
            "schema": "kestrel.recovery_runtime.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": "3.11.14",
            "python_executable_sha256": "sha256:" + "2" * 64,
            "files": [
                {
                    "asset_path": "recovery/runtime/libfixture.so",
                    "sandbox_path": "/opt/fixture/libfixture.so",
                    "sha256": _sha256(runtime),
                    "size_bytes": len(runtime),
                }
            ],
        }
    )
    python_archive = b"python runtime archive"
    python_manifest = _canonical(
        {
            "schema": "kestrel.recovery_python_runtime.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": "3.11.14",
            "python_abi": "cp311",
            "python_executable_path": "bin/python3.11",
            "python_executable_sha256": "sha256:" + "2" * 64,
            "source_archive_url": "https://example.invalid/python.tar.gz",
            "source_archive_sha256": "sha256:" + "3" * 64,
            "runtime_archive_path": "recovery/python-runtime.tar.gz",
            "runtime_archive_sha256": _sha256(python_archive),
            "runtime_archive_size_bytes": len(python_archive),
            "runtime_tree_sha256": "sha256:" + "4" * 64,
            "runtime_file_count": 1,
            "runtime_total_size_bytes": 1,
        }
    )
    bwrap = b"bubblewrap binary"
    (bin_root / "bwrap").write_bytes(bwrap)
    (recovery / "requirements.txt").write_bytes(requirements)
    (recovery / "wheelhouse-manifest.json").write_bytes(wheel_manifest)
    (recovery / "runtime-manifest.json").write_bytes(runtime_manifest)
    (recovery / "python-runtime-manifest.json").write_bytes(python_manifest)
    (recovery / "python-runtime.tar.gz").write_bytes(python_archive)
    smoke = _canonical(
        {
            "schema": "kestrel.recovery_capsule_smoke.v1",
            "source_sha": SOURCE_SHA,
            "validation_status": "validated",
        }
    )
    (root / "recovery-smoke-report.json").write_bytes(smoke)
    receipt = {
        "schema": "kestrel.recovery_dependency_staging.v1",
        "inputs": {
            "bubblewrap_package_url": "https://example.invalid/bwrap.deb",
            "bubblewrap_package_sha256": "sha256:" + "5" * 64,
            "requirements_sha256": _sha256(requirements),
            "python_package_url": "https://example.invalid/python.tar.gz",
            "python_package_sha256": "sha256:" + "3" * 64,
            "python_version": "3.11.14",
            "python_abi": "cp311",
            "wheel_platform": "manylinux2014_x86_64",
            "source_sha": SOURCE_SHA,
        },
        "outputs": {
            "bubblewrap_sha256": _sha256(bwrap),
            "bubblewrap_version": "bubblewrap 0.9.0",
            "wheelhouse_manifest_sha256": _sha256(wheel_manifest),
            "wheel_count": 1,
            "runtime_manifest_sha256": _sha256(runtime_manifest),
            "runtime_file_count": 1,
            "python_runtime_manifest_sha256": _sha256(python_manifest),
            "python_runtime_archive_sha256": _sha256(python_archive),
        },
        "provenance": {
            "method": "checksum-pinned-recovery-dependency-staging",
            "producer": "scripts/stage_recovery_dependencies.py",
            "provider": "github.com+archive.ubuntu.com+pypi.org",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    receipt["receipt_digest"] = _sha256(_canonical(receipt))
    (recovery / "dependency-staging-receipt.json").write_bytes(_canonical(receipt))
    return source_root, root


def test_staging_fixture_matches_workflow_upload_root_contract() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/recovery-dependency-staging.yml"
    ).read_text(encoding="utf-8")

    assert (
        '--output "${RUNNER_TEMP}/kestrel-recovery-dependencies/recovery-smoke-report.json"'
    ) in workflow
    assert "path: ${{ runner.temp }}/kestrel-recovery-dependencies" in workflow


def test_staging_validator_binds_every_wheel_before_installation(
    tmp_path: Path, subject: ModuleType
) -> None:
    source_root, root = _staging_fixture(tmp_path)

    validated = subject.validate_staging_artifact(
        root,
        source_root=source_root,
        source_sha=SOURCE_SHA,
    )

    assert validated.requirements == root / "recovery/requirements.txt"
    assert validated.wheelhouse == root / "recovery/wheelhouse"
    wheel = validated.wheelhouse / "fixture-1.0-py3-none-any.whl"
    wheel.write_bytes(b"substituted wheel")
    with pytest.raises(ValueError, match="wheel"):
        subject.validate_staging_artifact(
            root,
            source_root=source_root,
            source_sha=SOURCE_SHA,
        )


@pytest.mark.skipif(os.name == "nt", reason="recovery capsule bootstrap requires POSIX (fchmod, ELF, bubblewrap)")
def test_environment_builder_installs_offline_and_freezes_import_tree(
    tmp_path: Path, subject: ModuleType
) -> None:
    source_root, root = _staging_fixture(tmp_path)
    validated = subject.validate_staging_artifact(
        root,
        source_root=source_root,
        source_sha=SOURCE_SHA,
    )
    base_python = tmp_path / "base-python"
    base_python.write_bytes(b"exact base python")
    base_python.chmod(0o500)
    environment = tmp_path / "controller-environment"
    calls: list[list[str]] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(command)
        if "venv" in command:
            built_environment = Path(command[-1])
            python = built_environment / "bin/python"
            python.parent.mkdir(parents=True)
            python.write_bytes(base_python.read_bytes())
            python.chmod(0o500)
            (built_environment / "lib/python3.11/site-packages").mkdir(parents=True)
        elif "install" in command:
            built_environment = Path(command[0]).parent.parent
            (built_environment / "lib/python3.11/site-packages/dependency.py").write_bytes(
                b"VALUE = 1\n"
            )
        return subprocess.CompletedProcess(command, 0, b"", b"")

    manifest = subject.build_controller_environment(
        validated,
        environment_root=environment,
        base_python=base_python,
        runner=runner,
    )

    install = next(command for command in calls if "install" in command)
    assert "--no-index" in install
    assert "--require-hashes" in install
    assert "--only-binary=:all:" in install
    assert "--no-compile" in install
    assert any("check" in command for command in calls)
    assert manifest["python_sha256"] == _sha256(base_python.read_bytes())
    assert manifest["site_packages_file_count"] == 1
    dependency = environment / "lib/python3.11/site-packages/dependency.py"
    assert stat.S_IMODE(dependency.stat().st_mode) == 0o444
    assert stat.S_IMODE(dependency.parent.stat().st_mode) == 0o555

    def forbidden_reinstall(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an exact local environment resume must not reinstall")

    resumed = subject.build_controller_environment(
        validated,
        environment_root=environment,
        base_python=base_python,
        runner=forbidden_reinstall,
    )

    assert resumed == manifest

    dependency.chmod(0o644)
    dependency.write_bytes(b"VALUE = 2\n")
    dependency.chmod(0o444)
    with pytest.raises(ValueError, match="environment.*(receipt|identity)|tree"):
        subject.build_controller_environment(
            validated,
            environment_root=environment,
            base_python=base_python,
            runner=forbidden_reinstall,
        )


@pytest.mark.skipif(os.name == "nt", reason="recovery capsule bootstrap requires POSIX (fchmod, ELF, bubblewrap)")
def test_initial_bootstrap_records_environment_then_executes_inner_gate(
    tmp_path: Path, subject: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    scripts = source_root / "scripts"
    scripts.mkdir(parents=True)
    for name in (
        subject.SCRIPT_PATH.name,
        "recovery_capsule_controller.py",
        *subject.LOCAL_IMPORT_NAMES,
    ):
        (scripts / name).write_bytes(f"# {name}\n".encode())
    bootstrap_root = tmp_path / "bootstrap"
    bootstrap_root.mkdir()
    staging_root = bootstrap_root / "staging-artifact"
    contents = staging_root / "contents"
    contents.mkdir(parents=True)
    acquisition_raw = _canonical({"acquired": True})
    (staging_root / "acquisition-receipt.json").write_bytes(acquisition_raw)
    dependency_raw = _canonical({"staged": True})
    dependency_path = contents / "dependency-staging-receipt.json"
    dependency_path.write_bytes(dependency_raw)
    requirements = contents / "requirements.txt"
    requirements.write_bytes(b"fixture")
    wheelhouse = contents / "wheelhouse"
    wheelhouse.mkdir()
    validated = subject.ValidatedStaging(
        root=contents,
        requirements=requirements,
        wheelhouse=wheelhouse,
        receipt={"validation_status": "validated"},
        receipt_raw=dependency_raw,
    )
    request = subject.InitialBootstrapRequest(
        source_root=source_root,
        source_sha=SOURCE_SHA,
        staging_run_id=701,
        staging_artifact_id=702,
        staging_artifact_digest="sha256:" + "d" * 64,
        pinned_gh=tmp_path / "gh",
        bootstrap_root=bootstrap_root,
        controller_arguments=("--fixture",),
    )
    events: list[str] = []
    monkeypatch.setattr(subject, "SCRIPT_PATH", scripts / subject.SCRIPT_PATH.name)
    monkeypatch.setattr(
        subject,
        "_require_preimport_runtime",
        lambda _request: events.append("runtime"),
    )
    monkeypatch.setattr(
        subject,
        "_require_source_identity",
        lambda _request: events.append("source") or ("sha256:" + "a" * 64),
    )
    monkeypatch.setattr(
        subject,
        "_require_pinned_gh",
        lambda _path: events.append("gh"),
    )
    monkeypatch.setattr(
        subject,
        "_acquire_staging_artifact",
        lambda _request: events.append("acquire") or contents,
    )
    monkeypatch.setattr(
        subject,
        "validate_staging_artifact",
        lambda *_args, **_kwargs: events.append("validate") or validated,
    )

    def build_environment(
        _staging: object, *, environment_root: Path, **_kwargs: object
    ) -> dict[str, object]:
        events.append("build")
        python = environment_root / "bin/python"
        site = environment_root / "lib/python3.11/site-packages"
        site.mkdir(parents=True)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_bytes(b"environment python")
        python.chmod(0o500)
        (site / "dependency.py").write_bytes(b"VALUE = 1\n")
        count, total, digest = subject.site_packages_identity(environment_root)
        return {
            "root": str(environment_root),
            "python_path": str(python),
            "python_sha256": _sha256(python.read_bytes()),
            "site_packages_path": str(site),
            "site_packages_file_count": count,
            "site_packages_total_size_bytes": total,
            "site_packages_tree_sha256": digest,
        }

    monkeypatch.setattr(subject, "build_controller_environment", build_environment)
    monkeypatch.setattr(
        subject,
        "_source_tree_identity",
        lambda _root: "sha256:" + "a" * 64,
    )
    executed: list[Path] = []
    monkeypatch.setattr(
        subject,
        "_exec_inner_gate",
        lambda _request, receipt_path: executed.append(receipt_path),
    )
    receipt_path = bootstrap_root / "bootstrap-receipt.json"
    receipt_path.write_bytes(b'{"interrupted":')
    interrupted_temporary = bootstrap_root / ".bootstrap-receipt.json.tmp-interrupted"
    interrupted_temporary.write_bytes(b"complete but unpublished scratch")

    subject.run_initial_bootstrap(request)

    assert events == ["runtime", "source", "gh", "acquire", "validate", "build"]
    assert not interrupted_temporary.exists()
    assert executed == [receipt_path]
    receipt = json.loads(receipt_path.read_bytes())
    assert receipt["source"]["tree_sha256"] == "sha256:" + "a" * 64
    assert receipt["staging_artifact"]["artifact_id"] == 702
    assert receipt["environment"]["site_packages_file_count"] == 1


@pytest.mark.skipif(os.name == "nt", reason="recovery capsule bootstrap requires POSIX (fchmod, ELF, bubblewrap)")
def test_bootstrap_write_once_never_exposes_a_partial_final_file(
    tmp_path: Path, subject: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "bootstrap-receipt.json"
    raw = _canonical({"complete": True})
    real_link = subject.os.link

    def interrupt_before_publication(source: Path, target: Path) -> None:
        assert Path(source).read_bytes() == raw
        assert Path(target) == output
        assert not output.exists()
        raise RuntimeError("simulated hard interruption before atomic publication")

    monkeypatch.setattr(subject.os, "link", interrupt_before_publication)
    with pytest.raises(RuntimeError, match="hard interruption"):
        subject._write_once(output, raw)  # noqa: SLF001

    assert not output.exists()
    assert not tuple(tmp_path.glob(".bootstrap-receipt.json.tmp-*"))

    monkeypatch.setattr(subject.os, "link", real_link)
    subject._write_once(output, raw)  # noqa: SLF001
    assert output.read_bytes() == raw


def test_prepare_only_is_a_resume_mode_not_part_of_the_bootstrap_identity(
    tmp_path: Path, subject: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    pinned_gh = tmp_path / "gh"
    pinned_gh.write_bytes(b"gh")
    bootstrap_root = tmp_path / "bootstrap"
    bootstrap_root.mkdir()
    observed: list[object] = []
    monkeypatch.setattr(subject, "run_initial_bootstrap", lambda request: observed.append(request))
    base = [
        "--bootstrap-root",
        str(bootstrap_root),
        "--pinned-gh",
        str(pinned_gh),
        "--source-root",
        str(source_root),
        "--source-sha",
        SOURCE_SHA,
        "--staging-run-id",
        "701",
        "--staging-artifact-id",
        "702",
        "--staging-artifact-digest",
        "sha256:" + "d" * 64,
        "--candidate-manifest-digest",
        "sha256:" + "c" * 64,
    ]

    assert subject.main([*base, "--prepare-only"]) == 0
    assert subject.main(base) == 0

    prepared, resumed = observed
    assert prepared.prepare_only is True
    assert resumed.prepare_only is False
    assert prepared.controller_arguments == resumed.controller_arguments
    assert "--prepare-only" not in prepared.controller_arguments


@pytest.mark.skipif(os.name == "nt", reason="recovery capsule bootstrap requires POSIX (fchmod, ELF, bubblewrap)")
def test_frozen_runtime_replay_separates_pinned_content_from_frozen_modes(
    tmp_path: Path, subject: ModuleType
) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / "bin/python3.11"
    library = runtime / "lib/libpython.so"
    python.parent.mkdir(parents=True)
    library.parent.mkdir(parents=True)
    python.write_bytes(b"python")
    library.write_bytes(b"library")
    python.chmod(0o500)
    library.chmod(0o444)
    python.parent.chmod(0o555)
    library.parent.chmod(0o555)

    observed = subject._runtime_inventory(tmp_path)
    committed = observed.replace(b"file\t500\t", b"file\t755\t").replace(
        b"file\t444\t", b"file\t644\t"
    )

    assert subject._runtime_content_inventory(observed) == (
        subject._runtime_content_inventory(committed)
    )
    assert subject._runtime_has_frozen_modes(tmp_path) is True
    library.chmod(0o644)
    assert subject._runtime_has_frozen_modes(tmp_path) is False


def test_inner_gate_rejects_installed_dependency_tampering_before_controller(
    tmp_path: Path, subject: ModuleType
) -> None:
    source_root = tmp_path / "source"
    scripts = source_root / "scripts"
    scripts.mkdir(parents=True)
    bootstrap_script = scripts / SCRIPT.name
    bootstrap_script.write_bytes(SCRIPT.read_bytes())
    controller = scripts / "recovery_capsule_controller.py"
    controller.write_bytes(b"raise AssertionError('controller must not execute')\n")
    local_imports = []
    for name in (
        "bootstrap_recovery.py",
        "recovery_launcher.py",
        "release_candidate_manifest.py",
        "release_control_receipt.py",
        "release_promotion_transaction.py",
    ):
        path = scripts / name
        path.write_bytes(f"# {name}\n".encode())
        local_imports.append({"path": str(path), "sha256": _sha256(path.read_bytes())})
    environment = tmp_path / "environment"
    site_packages = environment / "lib/python3.11/site-packages"
    site_packages.mkdir(parents=True)
    dependency = site_packages / "dependency.py"
    dependency.write_bytes(b"VALUE = 1\n")
    python = environment / "bin/python"
    python.parent.mkdir()
    python.write_bytes(b"exact python fixture")
    python.chmod(0o500)
    count, total, tree_digest = subject.site_packages_identity(environment)
    receipt = {
        "schema": "kestrel.recovery_controller_bootstrap.v1",
        "source": {
            "root": str(source_root),
            "sha": SOURCE_SHA,
            "tree_sha256": "sha256:" + "a" * 64,
            "bootstrap_path": str(bootstrap_script),
            "bootstrap_sha256": _sha256(bootstrap_script.read_bytes()),
            "controller_path": str(controller),
            "controller_sha256": _sha256(controller.read_bytes()),
            "local_imports": local_imports,
        },
        "runtime": {
            "bootstrap_python_path": str(python),
            "bootstrap_python_sha256": _sha256(python.read_bytes()),
            "bootstrap_runtime_tree_sha256": "sha256:" + "b" * 64,
            "python_path": str(python),
            "python_sha256": _sha256(python.read_bytes()),
        },
        "environment": {
            "root": str(environment),
            "python_path": str(python),
            "python_sha256": _sha256(python.read_bytes()),
            "site_packages_path": str(site_packages),
            "site_packages_file_count": count,
            "site_packages_total_size_bytes": total,
            "site_packages_tree_sha256": tree_digest,
        },
        "pinned_gh": {
            "path": str(tmp_path / "gh"),
            "sha256": "sha256:" + "c" * 64,
            "version": "fixture",
        },
        "staging_artifact": {
            "root": str(tmp_path / "staging-artifact"),
            "run_id": 701,
            "artifact_id": 702,
            "artifact_digest": "sha256:" + "d" * 64,
            "acquisition_receipt_digest": "sha256:" + "e" * 64,
            "dependency_receipt_digest": "sha256:" + "f" * 64,
        },
        "controller_arguments_digest": _sha256(_canonical(["--fixture"])),
        "validation_status": "validated",
    }
    receipt_path = tmp_path / "bootstrap-receipt.json"
    receipt_path.write_bytes(_canonical(receipt))

    authorized = subject.authorize_inner_gate(
        receipt_path=receipt_path,
        source_root=source_root,
        controller_arguments=("--fixture",),
        executing_script=bootstrap_script,
        executing_python=python,
        require_source_git=False,
        require_external_bindings=False,
    )
    assert authorized == (controller, site_packages)

    dependency.write_bytes(b"VALUE = 2\n")
    with pytest.raises(ValueError, match="site-packages"):
        subject.authorize_inner_gate(
            receipt_path=receipt_path,
            source_root=source_root,
            controller_arguments=("--fixture",),
            executing_script=bootstrap_script,
            executing_python=python,
            require_source_git=False,
            require_external_bindings=False,
        )
