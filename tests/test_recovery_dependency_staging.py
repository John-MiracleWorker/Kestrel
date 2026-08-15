from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

from scripts import bootstrap_recovery, recovery_launcher
from scripts import release_control_receipt as receipts
from scripts import run_recovery_capsule_smoke as smoke
from scripts import stage_recovery_dependencies as staging
from scripts.stage_recovery_dependencies import (
    BWRAP_BINARY_SHA256,
    BWRAP_PACKAGE_SHA256,
    BWRAP_PACKAGE_URL,
    RECOVERY_PYTHON_ABI,
    RECOVERY_PYTHON_BINARY_SHA256,
    RECOVERY_PYTHON_VERSION,
    RECOVERY_RUNTIME_PLATFORM,
    RECOVERY_WHEEL_PLATFORM,
    stage_recovery_dependencies,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _python_source_archive(*, python: bytes = b"fixture python") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, raw, mode in (
            ("./bin/python3.11", python, 0o755),
            ("./lib/libpython3.11.so.1.0", b"fixture libpython", 0o755),
            ("./lib/python3.11/os.py", b"NAME = 'fixture'\n", 0o644),
        ):
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
        link = tarfile.TarInfo("./lib/libpython3.11.so")
        link.type = tarfile.SYMTYPE
        link.linkname = "libpython3.11.so.1.0"
        archive.addfile(link)
    return stream.getvalue()


def test_python_runtime_archive_is_reproducible_and_binds_every_extracted_file(
    tmp_path: Path,
) -> None:
    source = tmp_path / "python.tar.gz"
    python = b"fixture python"
    source.write_bytes(_python_source_archive(python=python))

    first_manifest, first_archive, first_root = staging._build_python_runtime_archive(  # noqa: SLF001
        source,
        tmp_path / "first",
        expected_python_sha256=_sha256(python),
    )
    second_manifest, second_archive, second_root = staging._build_python_runtime_archive(  # noqa: SLF001
        source,
        tmp_path / "second",
        expected_python_sha256=_sha256(python),
    )

    assert first_archive.read_bytes() == second_archive.read_bytes()
    assert first_manifest == second_manifest
    assert first_manifest["runtime_archive_sha256"] == "sha256:" + _sha256(
        first_archive.read_bytes()
    )
    assert first_manifest["runtime_file_count"] == 4
    assert first_manifest["python_executable_path"] == "bin/python3.11"
    assert (first_root / "lib" / "libpython3.11.so").is_file()
    assert not (first_root / "lib" / "libpython3.11.so").is_symlink()
    assert staging._python_runtime_tree_identity(first_root) == (  # noqa: SLF001
        first_manifest["runtime_file_count"],
        first_manifest["runtime_total_size_bytes"],
        first_manifest["runtime_tree_sha256"],
    )
    assert staging._python_runtime_tree_identity(second_root) == (  # noqa: SLF001
        second_manifest["runtime_file_count"],
        second_manifest["runtime_total_size_bytes"],
        second_manifest["runtime_tree_sha256"],
    )


def test_python_runtime_tree_identity_rejects_stdlib_drift(tmp_path: Path) -> None:
    source = tmp_path / "python.tar.gz"
    python = b"fixture python"
    source.write_bytes(_python_source_archive(python=python))
    manifest, _archive, root = staging._build_python_runtime_archive(  # noqa: SLF001
        source,
        tmp_path / "runtime",
        expected_python_sha256=_sha256(python),
    )

    (root / "lib" / "python3.11" / "os.py").write_bytes(b"POISONED = True\n")

    assert staging._python_runtime_tree_identity(root)[2] != manifest["runtime_tree_sha256"]  # noqa: SLF001


def test_stage_recovery_dependencies_is_pinned_and_emits_a_bound_receipt(
    tmp_path: Path,
) -> None:
    package = b"pinned bubblewrap package fixture"
    bwrap = b"#!/bin/sh\necho 'bubblewrap 0.9.0'\n"
    requirements = (
        b"demo-recovery==1.0.0 \\\n"
        b"    --hash=sha256:"
        + b"1" * 64
        + b"\n"
    )
    wheel = b"trusted recovery wheel"
    loader = b"trusted runtime loader"
    libpython = b"trusted libpython"
    python = b"fixture python"
    python_package = _python_source_archive(python=python)
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "config" / "recovery-requirements.txt").write_bytes(requirements)
    output = tmp_path / "staged"

    def fetch(url: str) -> bytes:
        assert url == BWRAP_PACKAGE_URL
        return package

    def fetch_python(url: str) -> bytes:
        assert url == staging.RECOVERY_PYTHON_PACKAGE_URL
        return python_package

    def extract(package_path: Path, destination: Path) -> Path:
        assert package_path.read_bytes() == package
        binary = destination / "usr" / "bin" / "bwrap"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(bwrap)
        binary.chmod(0o755)
        return binary

    def download_wheels(requirements_path: Path, wheelhouse: Path) -> None:
        assert requirements_path.read_bytes() == requirements
        (wheelhouse / "demo_recovery-1.0.0-py3-none-any.whl").write_bytes(wheel)

    def collect_runtime_files(
        requirements_path: Path,
        wheelhouse: Path,
        work_root: Path,
        bwrap_path: Path,
        python_root: Path,
    ) -> list[tuple[str, Path]]:
        assert requirements_path.read_bytes() == requirements
        assert (wheelhouse / "demo_recovery-1.0.0-py3-none-any.whl").read_bytes() == wheel
        assert bwrap_path.read_bytes() == bwrap
        assert (python_root / "bin" / "python3.11").read_bytes() == python
        loader_path = work_root / "loader"
        libpython_path = work_root / "libpython"
        loader_path.write_bytes(loader)
        libpython_path.write_bytes(libpython)
        return [
            ("/lib64/ld-linux-x86-64.so.2", loader_path),
            (
                "/opt/hostedtoolcache/Python/3.11.14/x64/lib/libpython3.11.so.1.0",
                libpython_path,
            ),
        ]

    report = stage_recovery_dependencies(
        source_root=source,
        output_root=output,
        source_sha="a" * 40,
        fetch=fetch,
        fetch_python=fetch_python,
        extract_bwrap=extract,
        download_wheels=download_wheels,
        collect_runtime_files=collect_runtime_files,
        expected_bwrap_package_sha256=_sha256(package),
        expected_bwrap_binary_sha256=_sha256(bwrap),
        expected_python_package_sha256=_sha256(python_package),
        expected_python_binary_sha256=_sha256(python),
    )

    recovery = output / "recovery"
    assert (recovery / "bin" / "bwrap").read_bytes() == bwrap
    assert (recovery / "requirements.txt").read_bytes() == requirements
    python_runtime_raw = (recovery / "python-runtime-manifest.json").read_bytes()
    python_runtime = json.loads(python_runtime_raw)
    assert (recovery / "python-runtime.tar.gz").is_file()
    assert python_runtime["runtime_archive_sha256"] == "sha256:" + _sha256(
        (recovery / "python-runtime.tar.gz").read_bytes()
    )
    manifest = json.loads((recovery / "wheelhouse-manifest.json").read_bytes())
    assert manifest == {
        "schema": "kestrel.recovery_wheelhouse.v1",
        "wheels": [
            {
                "filename": "demo_recovery-1.0.0-py3-none-any.whl",
                "sha256": "sha256:" + _sha256(wheel),
                "size_bytes": len(wheel),
            }
        ],
    }
    runtime_manifest_raw = (recovery / "runtime-manifest.json").read_bytes()
    runtime_manifest = json.loads(runtime_manifest_raw)
    assert runtime_manifest["schema"] == "kestrel.recovery_runtime.v1"
    assert runtime_manifest["platform"] == RECOVERY_RUNTIME_PLATFORM
    assert runtime_manifest["python_executable_sha256"] == (
        "sha256:" + RECOVERY_PYTHON_BINARY_SHA256
    )
    assert runtime_manifest["python_version"] == RECOVERY_PYTHON_VERSION
    assert [item["sandbox_path"] for item in runtime_manifest["files"]] == [
        "/lib64/ld-linux-x86-64.so.2",
        "/opt/hostedtoolcache/Python/3.11.14/x64/lib/libpython3.11.so.1.0",
    ]
    for item, expected in zip(runtime_manifest["files"], (loader, libpython), strict=True):
        assert (output / item["asset_path"]).read_bytes() == expected
    assert report["schema"] == "kestrel.recovery_dependency_staging.v1"
    assert report["validation_status"] == "validated"
    assert report["inputs"] == {
        "bubblewrap_package_sha256": "sha256:" + _sha256(package),
        "bubblewrap_package_url": BWRAP_PACKAGE_URL,
        "requirements_sha256": "sha256:" + _sha256(requirements),
        "python_version": RECOVERY_PYTHON_VERSION,
        "python_abi": RECOVERY_PYTHON_ABI,
        "python_package_sha256": "sha256:" + _sha256(python_package),
        "python_package_url": staging.RECOVERY_PYTHON_PACKAGE_URL,
        "wheel_platform": RECOVERY_WHEEL_PLATFORM,
        "source_sha": "a" * 40,
    }
    assert report["outputs"]["bubblewrap_sha256"] == "sha256:" + _sha256(bwrap)
    assert report["outputs"]["wheel_count"] == 1
    assert report["outputs"]["runtime_file_count"] == 2
    assert report["outputs"]["runtime_manifest_sha256"] == (
        "sha256:" + _sha256(runtime_manifest_raw)
    )
    assert report["outputs"]["python_runtime_archive_sha256"] == (
        python_runtime["runtime_archive_sha256"]
    )
    assert report["outputs"]["python_runtime_manifest_sha256"] == (
        "sha256:" + _sha256(python_runtime_raw)
    )
    assert report["receipt_digest"].startswith("sha256:")
    manifest_raw = (recovery / "wheelhouse-manifest.json").read_bytes()
    receipt_raw = (recovery / "dependency-staging-receipt.json").read_bytes()
    assert manifest_raw == json.dumps(manifest, separators=(",", ":"), sort_keys=True).encode()
    assert receipt_raw == json.dumps(report, separators=(",", ":"), sort_keys=True).encode()


def test_stage_recovery_dependencies_rejects_package_or_binary_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "config" / "recovery-requirements.txt").write_text(
        "demo==1 --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="package.*digest"):
        stage_recovery_dependencies(
            source_root=source,
            output_root=tmp_path / "bad-package",
            source_sha="a" * 40,
            fetch=lambda _url: b"changed",
            expected_bwrap_package_sha256="0" * 64,
        )

    package = b"package"

    def extract(_package: Path, destination: Path) -> Path:
        binary = destination / "usr" / "bin" / "bwrap"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"changed binary")
        binary.chmod(0o755)
        return binary

    with pytest.raises(ValueError, match="binary.*digest"):
        stage_recovery_dependencies(
            source_root=source,
            output_root=tmp_path / "bad-binary",
            source_sha="a" * 40,
            fetch=lambda _url: package,
            extract_bwrap=extract,
            expected_bwrap_package_sha256=_sha256(package),
            expected_bwrap_binary_sha256="0" * 64,
        )

    assert not (tmp_path / "bad-package").exists()
    assert not (tmp_path / "bad-binary").exists()


def test_stage_recovery_dependencies_rejects_symlink_roots(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "config").mkdir(parents=True)
    (source / "config" / "recovery-requirements.txt").write_text(
        "demo==1 --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )
    source_link = tmp_path / "source-link"
    source_link.symlink_to(source, target_is_directory=True)

    with pytest.raises(ValueError, match="source root.*unsafe"):
        stage_recovery_dependencies(
            source_root=source_link,
            output_root=tmp_path / "staged",
            source_sha="a" * 40,
        )

    output_target = tmp_path / "output-target"
    output_link = tmp_path / "output-link"
    output_link.symlink_to(output_target, target_is_directory=True)
    with pytest.raises(ValueError, match="output root.*absent"):
        stage_recovery_dependencies(
            source_root=source,
            output_root=output_link,
            source_sha="a" * 40,
        )


def test_recovery_wheel_acquisition_ignores_ambient_pip_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    class Completed:
        returncode = 0
        stderr = ""
        stdout = ""

    def run(command: list[str], **kwargs: object) -> Completed:
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return Completed()

    monkeypatch.setattr(staging.subprocess, "run", run)
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "fixture==1.0 --hash=sha256:" + "1" * 64 + "\n",
        encoding="utf-8",
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    staging._download_wheels(requirements, wheelhouse)  # noqa: SLF001

    command = observed["command"]
    assert isinstance(command, list)
    assert "--isolated" in command
    assert "--index-url=https://pypi.org/simple" in command
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert not any(name.startswith("PIP_INDEX") for name in environment)


def test_runtime_manifest_rejects_unsafe_or_ambiguous_targets(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.write_bytes(b"runtime")

    with pytest.raises(ValueError, match="unsafe"):
        staging._runtime_manifest(  # noqa: SLF001
            [("/proc/self/exe", runtime)]
        )
    with pytest.raises(ValueError, match="unsafe"):
        staging._runtime_manifest(  # noqa: SLF001
            [("/lib/libc.so.6", runtime), ("/lib/libc.so.6", runtime)]
        )

    other = tmp_path / "other"
    other.write_bytes(b"different runtime")
    with pytest.raises(ValueError, match="basename|collision|ambiguous"):
        staging._runtime_manifest(  # noqa: SLF001
            [("/lib/libc.so.6", runtime), ("/usr/lib/libc.so.6", other)]
        )


def test_runtime_dependency_parser_requires_complete_absolute_elf_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loader = tmp_path / "ld-linux-x86-64.so.2"
    libc = tmp_path / "libc.so.6"
    target = tmp_path / "python"
    for path in (loader, libc, target):
        path.write_bytes(b"\x7fELFfixture")

    class Completed:
        returncode = 0
        stderr = ""
        stdout = (
            f"libc.so.6 => {libc} (0x0001)\n"
            f"{loader} (0x0002)\n"
        )

    monkeypatch.setattr(staging.subprocess, "run", lambda *_args, **_kwargs: Completed())

    assert staging._ldd_dependencies(target, ldd=tmp_path / "ldd") == [  # noqa: SLF001
        (str(libc), libc),
        (str(loader), loader),
    ]

    Completed.stdout = "libssl.so.3 => not found\n"
    with pytest.raises(ValueError, match="incomplete"):
        staging._ldd_dependencies(target, ldd=tmp_path / "ldd")  # noqa: SLF001


def test_production_recovery_dependency_identities_match_frozen_launcher() -> None:
    assert BWRAP_PACKAGE_SHA256 == (
        "1b506492bd9c7fd0cdb4f02ac822f1d3e336b0aead5113c1239baf8db5db562a"
    )
    assert BWRAP_BINARY_SHA256 == (
        "52231e1caf55bcbc667b269f49c63599a6f7db4767ae6a039580d0ff853db712"
    )
    assert (RECOVERY_PYTHON_VERSION, RECOVERY_PYTHON_ABI, RECOVERY_WHEEL_PLATFORM) == (
        "3.11.14",
        "cp311",
        "manylinux2014_x86_64",
    )
    assert RECOVERY_PYTHON_BINARY_SHA256 == (
        "dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8"
    )
    assert RECOVERY_RUNTIME_PLATFORM == "ubuntu-24.04-x86_64"


def test_recovery_dependency_workflow_stages_and_preserves_exact_source_receipt() -> None:
    root = Path(__file__).resolve().parents[1]
    workflow = (
        root / ".github" / "workflows" / "recovery-dependency-staging.yml"
    ).read_text(encoding="utf-8")

    assert "runs-on: ubuntu-24.04" in workflow
    assert 'python-version: "3.11.14"' in workflow
    assert "python scripts/bootstrap_uv.py" in workflow
    assert "--frozen --only-group recovery" in workflow
    assert "--format requirements.txt --no-emit-project --no-header --no-annotate" in workflow
    assert "cmp --silent config/recovery-requirements.txt" in workflow
    assert "python -m scripts.stage_recovery_dependencies" in workflow
    assert '--source-root "$GITHUB_WORKSPACE"' in workflow
    assert "dependency-staging-receipt.json" in workflow
    assert "runtime-manifest.json" in workflow
    assert "python-runtime-manifest.json" in workflow
    assert "python-runtime.tar.gz" in workflow
    assert "dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8" in workflow
    assert 'readlink -f -- "$(command -v python)"' in workflow
    assert 'python_executable="$(command -v python)"' not in workflow
    assert "--no-index" in workflow
    assert "scripts/bootstrap_workflow_tools.sh" in workflow
    assert "python -I -B scripts/run_recovery_capsule_smoke.py" in workflow
    assert '--host-gh "$RECOVERY_SMOKE_GH"' in workflow
    assert "recovery-smoke-report.json" in workflow
    assert "compression-level: 0" in workflow
    assert "retention-days: 30" in workflow
    assert "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02" in workflow

    parsed = yaml.safe_load(workflow)
    stage = parsed["jobs"]["stage"]
    assert stage["env"] == {"RECOVERY_SOURCE_SHA": "${{ inputs.source_sha }}"}
    assert all(
        "${{ inputs.source_sha }}" not in str(step.get("run", ""))
        for step in stage["steps"]
    )


def test_production_recovery_smoke_script_starts_in_isolated_mode() -> None:
    root = Path(__file__).resolve().parents[1]

    completed = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-B",
            str(root / "scripts" / "run_recovery_capsule_smoke.py"),
            "--help",
        ],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert "--dependency-root" in completed.stdout


def test_release_recovery_mutations_enter_only_through_nested_offline_sandbox() -> None:
    workflow = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "release-transaction.yml"
    ).read_text(encoding="utf-8")

    assert "capsule_private_loader" in workflow
    assert "test ! -e /etc/ld.so.preload" in workflow
    assert "exec -c \"$capsule_loader\"" in workflow
    assert "--inhibit-cache" in workflow
    assert "--library-path \"$capsule_library_path\"" in workflow
    assert workflow.count("--executable python") >= 8
    assert workflow.count("materialize-candidate") >= 1
    assert workflow.count("bind-host-actuator") >= 4
    assert "--output \"$host_binding\"" in workflow
    assert '> "$host_binding"' not in workflow

    for line_number, line in enumerate(workflow.splitlines()):
        if "materialize-candidate" in line or "bind-host-actuator" in line:
            nearby = "\n".join(workflow.splitlines()[max(0, line_number - 12) : line_number + 2])
            assert "launch" in nearby
            assert "--executable python" in nearby


def test_recovery_tcb_bootstrap_authenticates_tree_before_first_python() -> None:
    root = Path(__file__).resolve().parents[1]
    script = (root / "scripts" / "bootstrap_recovery_tcb.sh").read_text(encoding="utf-8")

    assert staging.RECOVERY_PYTHON_PACKAGE_URL in script
    assert staging.RECOVERY_PYTHON_PACKAGE_SHA256 in script
    assert "test ! -e /etc/ld.so.preload" in script
    assert "sha256sum" in script
    assert "tar" in script
    assert "runtime-tree" in script
    assert "PYTHONPATH" not in script
    assert "PYTHONHOME" not in script
    assert script.index("runtime-tree") < script.index("bin/python3.11")


def test_production_smoke_builds_a_schema_valid_complete_execution_closure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = Path(__file__).resolve().parents[1]
    dependency_root = tmp_path / "dependencies"
    recovery = dependency_root / "recovery"
    (recovery / "bin").mkdir(parents=True)
    (recovery / "runtime").mkdir()
    requirements = b"fixture==1.0 --hash=sha256:" + b"1" * 64 + b"\n"
    sandbox = b"fixture bubblewrap"
    wheelhouse_manifest = json.dumps(
        {
            "schema": "kestrel.recovery_wheelhouse.v1",
            "wheels": [
                {
                    "filename": "fixture-1.0-py3-none-any.whl",
                    "sha256": "sha256:" + "2" * 64,
                    "size_bytes": 1,
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    (recovery / "bin" / "bwrap").write_bytes(sandbox)
    (recovery / "requirements.txt").write_bytes(requirements)
    (recovery / "wheelhouse-manifest.json").write_bytes(wheelhouse_manifest)
    runtime_file = b"fixture ELF runtime"
    runtime_asset = "recovery/runtime/libpython3.11.so.1.0"
    runtime_manifest = receipts.canonical_json_bytes(
        {
            "schema": "kestrel.recovery_runtime.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": "3.11.14",
            "python_executable_sha256": "sha256:" + "3" * 64,
            "files": [
                {
                    "asset_path": runtime_asset,
                    "sandbox_path": (
                        "/opt/hostedtoolcache/Python/3.11.14/x64/lib/"
                        "libpython3.11.so.1.0"
                    ),
                    "sha256": "sha256:" + _sha256(runtime_file),
                    "size_bytes": len(runtime_file),
                }
            ],
        }
    )
    (dependency_root / runtime_asset).write_bytes(runtime_file)
    (recovery / "runtime-manifest.json").write_bytes(runtime_manifest)
    python_runtime_archive = b"fixture deterministic Python runtime archive"
    python_runtime_manifest = receipts.canonical_json_bytes(
        {
            "schema": "kestrel.recovery_python_runtime.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": "3.11.14",
            "python_abi": "cp311",
            "python_executable_path": "bin/python3.11",
            "python_executable_sha256": "sha256:" + "3" * 64,
            "source_archive_url": staging.RECOVERY_PYTHON_PACKAGE_URL,
            "source_archive_sha256": "sha256:" + staging.RECOVERY_PYTHON_PACKAGE_SHA256,
            "runtime_archive_path": "recovery/python-runtime.tar.gz",
            "runtime_archive_sha256": "sha256:" + _sha256(python_runtime_archive),
            "runtime_archive_size_bytes": len(python_runtime_archive),
            "runtime_tree_sha256": "sha256:" + "5" * 64,
            "runtime_file_count": 1,
            "runtime_total_size_bytes": 1,
        }
    )
    (recovery / "python-runtime-manifest.json").write_bytes(python_runtime_manifest)
    (recovery / "python-runtime.tar.gz").write_bytes(python_runtime_archive)
    sandbox_digest = "sha256:" + _sha256(sandbox)
    (recovery / "dependency-staging-receipt.json").write_text(
        json.dumps(
            {
                "outputs": {
                    "bubblewrap_sha256": sandbox_digest,
                    "bubblewrap_version": "bubblewrap fixture 1.0",
                        "runtime_manifest_sha256": "sha256:" + _sha256(runtime_manifest),
                        "runtime_file_count": 1,
                        "python_runtime_manifest_sha256": (
                            "sha256:" + _sha256(python_runtime_manifest)
                        ),
                        "python_runtime_archive_sha256": (
                            "sha256:" + _sha256(python_runtime_archive)
                        ),
                }
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "extracted-capsule"
    destination.mkdir()
    candidate_archive = tmp_path / "candidate-archive.tar"
    candidate_archive.write_bytes(b"fixture candidate archive")
    python_digest = "sha256:" + "3" * 64
    runtime = {"implementation": "CPython", "version": "3.11.14", "abi": "cp311"}
    monkeypatch.setattr(
        smoke,
        "_probe_final_python",
        lambda **_kwargs: (
            [str(destination)],
            runtime,
            python_digest,
            destination.parent / "recovery-runtime" / "base" / "lib",
        ),
    )
    monkeypatch.setattr(
        bootstrap_recovery,
        "TRUSTED_RECOVERY_PYTHON_IDENTITIES",
        {
            (smoke.sys.platform, smoke.platform.machine()): frozenset(
                {(python_digest, "Python 3.11.14", "CPython", "3.11.14", "cp311")}
            )
        },
    )
    monkeypatch.setattr(
        bootstrap_recovery,
        "TRUSTED_OS_SANDBOX_IDENTITIES",
        {
            (smoke.sys.platform, smoke.platform.machine()): frozenset(
                {(sandbox_digest, "bubblewrap fixture 1.0")}
            )
        },
    )

    closure = smoke._execution_closure(  # noqa: SLF001
        source_root=source_root,
        dependency_root=dependency_root,
        destination=destination,
        candidate_archive=candidate_archive,
    )
    raw = receipts.canonical_json_bytes(closure)

    assert recovery_launcher._closure(raw) == closure  # noqa: SLF001
    members = {
        item["path"]
        for field in ("python_members", "shell_helpers", "data_resources")
        for item in closure[field]
    }
    assert receipts._RECOVERY_CAPSULE_SOURCE_ASSETS <= members  # noqa: SLF001
    assert receipts._RECOVERY_CAPSULE_SCHEMA_ASSETS <= members  # noqa: SLF001
    assert next(
        item
        for item in closure["data_resources"]
        if item["path"] == "candidate-archive.tar"
    ) == {
        "path": "candidate-archive.tar",
        "sha256": "sha256:" + _sha256(candidate_archive.read_bytes()),
    }
    assert {
        f"schemas/{path.name}" for path in (source_root / "schemas").glob("*.json")
    } == receipts._RECOVERY_CAPSULE_SCHEMA_ASSETS  # noqa: SLF001
    assert closure["network_policy"] == {
        "default_deny": True,
        "allowed_endpoints": [],
    }
    assert closure["runtime_files"] == json.loads(runtime_manifest)["files"]
    assert closure["dependency_lock"]["runtime_manifest_sha256"] == (
        "sha256:" + _sha256(runtime_manifest)
    )
    assert closure["dependency_lock"]["python_runtime_manifest_sha256"] == (
        "sha256:" + _sha256(python_runtime_manifest)
    )
    assert closure["dependency_lock"]["python_runtime_archive_sha256"] == (
        "sha256:" + _sha256(python_runtime_archive)
    )
    assert {item["path"]: item["access"] for item in closure["io_roots"]}[
        str(source_root)
    ] == "read"


def test_production_smoke_report_is_schema_valid_and_self_digesting() -> None:
    report = smoke._smoke_report(  # noqa: SLF001
        source_sha="a" * 40,
        dependency_staging_receipt_digest="sha256:" + "1" * 64,
        capsule_manifest_digest="sha256:" + "2" * 64,
        capsule_archive_digest="sha256:" + "3" * 64,
        owner_key_fingerprint="sha256:" + "4" * 64,
        host_actuator_binding_digest="sha256:" + "5" * 64,
    )

    unsigned = dict(report)
    claimed = unsigned.pop("report_digest")
    assert claimed == "sha256:" + hashlib.sha256(
        receipts.canonical_json_bytes(unsigned)
    ).hexdigest()
    assert report["provenance"] == {
        "producer": "scripts/run_recovery_capsule_smoke.py",
        "provider": "local",
        "method": "deterministic-recovery-capsule-smoke",
    }
    receipts._validate_schema(  # noqa: SLF001
        smoke.SMOKE_SCHEMA,
        report,
        label="test recovery capsule smoke report",
    )


def test_production_smoke_invokes_a_nested_verification_through_the_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capsule = tmp_path / "capsule"
    python = tmp_path / "recovery-runtime" / "environment" / "bin" / "python"
    base_library = tmp_path / "recovery-runtime" / "base" / "lib"
    launcher = capsule / "scripts" / "recovery_launcher.py"
    closure = capsule / "recovery-execution-closure.json"
    for path in (python, launcher):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    base_library.mkdir(parents=True)
    closure.write_bytes(
        receipts.canonical_json_bytes({"sys_path": [str(capsule)]})
    )
    observed: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = b""
        stderr = b""

    def run(command: list[str], **kwargs: object) -> Completed:
        observed["command"] = command
        observed["environment"] = kwargs["env"]
        return Completed()

    monkeypatch.setattr(smoke.subprocess, "run", run)
    monkeypatch.setattr(
        smoke.recovery_launcher,
        "private_loader_command",
        lambda *, executable, arguments, **_kwargs: [str(executable), *arguments],
    )

    smoke._run_network_denied_capsule_command(  # noqa: SLF001
        capsule,
        command=["verify", str(closure), "--capsule-root", str(capsule)],
    )

    command = observed["command"]
    assert isinstance(command, list)
    assert command[0] == str(python)
    assert "launch" in command
    assert "--executable" in command
    assert command.count(str(launcher)) == 2
    assert "verify" in command
    assert command.count(str(closure)) == 2
    environment = observed["environment"]
    assert isinstance(environment, dict)
    assert environment["KESTREL_RECOVERY_SMOKE_SENTINEL"] == (
        "sandbox-environment-sentinel"
    )
