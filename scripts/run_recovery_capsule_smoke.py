#!/usr/bin/env python3
"""Create, extract, bootstrap, and fully verify a real staged recovery capsule."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess  # nosec B404
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from scripts import bootstrap_recovery, recovery_launcher  # noqa: E402
from scripts import release_control_receipt as receipts  # noqa: E402

SMOKE_SCHEMA = "kestrel.recovery_capsule_smoke.v1"
SMOKE_SOURCE_TIME = datetime(2026, 8, 13, 20, 0, 0, tzinfo=UTC)
SMOKE_SIGNING_PRINCIPAL = "John-MiracleWorker"


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _path_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _write(path: Path, raw: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(raw)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    path.chmod(mode)


def _positive_vector(source_root: Path, name: str) -> dict[str, Any]:
    bundle = json.loads(
        (
            source_root
            / "tests"
            / "fixtures"
            / "release-control"
            / "v3"
            / "positive-contract-vectors.json"
        ).read_bytes()
    )
    matches = [item["record"] for item in bundle["vectors"] if item["name"] == name]
    if len(matches) != 1 or type(matches[0]) is not dict:
        raise ValueError(f"recovery smoke vector is absent or ambiguous: {name}")
    return dict(matches[0])


def _smoke_signing_identity(work_root: Path) -> tuple[Path, str, str]:
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    identity = work_root / "synthetic-smoke-signing-key"
    _write(
        identity,
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.OpenSSH,
            serialization.NoEncryption(),
        ),
        mode=0o600,
    )
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.OpenSSH,
        serialization.PublicFormat.OpenSSH,
    ).decode("ascii")
    return identity, public_key, receipts.ssh_public_key_fingerprint(public_key)


def _owner_key_observation(source_root: Path, *, public_key: str) -> bytes:
    registry = json.loads((source_root / "release-control-source-registry.json").read_bytes())
    matches = [
        item
        for item in registry["entries"]
        if item["receipt_schema"] == receipts.SOURCE_OBSERVATION_SCHEMA
        and item["phase"] == "release-control"
        and item["mode"] is None
        and item["name"] == "owner-signing-keys-observation"
    ]
    if len(matches) != 1:
        raise ValueError("recovery smoke owner signing key registry entry is ambiguous")
    entry = matches[0]
    body = receipts.canonical_json_bytes(
        {
            "pages": [
                {
                    "number": 1,
                    "request_url": entry["locator"],
                    "response_headers": [],
                    "body": [
                        {
                            "id": 404,
                            "key": public_key,
                            "title": "Synthetic recovery capsule smoke key",
                        }
                    ],
                }
            ]
        }
    )
    observation = receipts.capture_source(
        registry=registry,
        receipt_schema=receipts.SOURCE_OBSERVATION_SCHEMA,
        phase="release-control",
        mode=None,
        name="owner-signing-keys-observation",
        raw_input=body,
        identity_observation=receipts.canonical_json_bytes(
            {"login": SMOKE_SIGNING_PRINCIPAL}
        ),
        _clock=lambda: SMOKE_SOURCE_TIME,
    )
    return receipts.canonical_json_bytes(observation)


def _probe_final_python(
    *, destination: Path, dependency_root: Path
) -> tuple[list[str], dict[str, str], str, Path, dict[str, Any]]:
    runtime_root = destination.parent / "recovery-runtime"
    base_root = runtime_root / "base"
    environment_root = runtime_root / "environment"
    probe_capsule = runtime_root / "probe-capsule"
    requirements = dependency_root / "recovery" / "requirements.txt"
    wheelhouse = dependency_root / "recovery" / "wheelhouse"
    python_manifest = json.loads(
        (dependency_root / "recovery" / "python-runtime-manifest.json").read_bytes()
    )
    shutil.copytree(
        dependency_root / "recovery" / "runtime",
        probe_capsule / "recovery" / "runtime",
    )
    for path in (probe_capsule / "recovery" / "runtime").iterdir():
        path.chmod(0o500 if path.name.startswith("ld-linux-") else 0o400)
    base_python = bootstrap_recovery._extract_python_runtime(  # noqa: SLF001
        archive=dependency_root / "recovery" / "python-runtime.tar.gz",
        destination=base_root,
        manifest=python_manifest,
    )
    bootstrap_recovery._run_bootstrap_command(  # noqa: SLF001
        bootstrap_recovery._private_loader_command(  # noqa: SLF001
            capsule_root=probe_capsule,
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
        ),
        environment=bootstrap_recovery._bootstrap_environment(),  # noqa: SLF001
    )
    venv_python = environment_root / "bin" / "python"
    try:
        bootstrap_recovery._run_bootstrap_command(  # noqa: SLF001
            bootstrap_recovery._private_loader_command(  # noqa: SLF001
                capsule_root=probe_capsule,
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
            environment=bootstrap_recovery._bootstrap_environment(),  # noqa: SLF001
        )
        bootstrap_recovery._run_bootstrap_command(  # noqa: SLF001
            bootstrap_recovery._private_loader_command(  # noqa: SLF001
                capsule_root=probe_capsule,
                executable=venv_python,
                arguments=("-I", "-B", "-m", "pip", "--isolated", "check"),
                additional_library_roots=(base_root / "lib",),
            ),
            environment=bootstrap_recovery._bootstrap_environment(),  # noqa: SLF001
        )
        probe = (
            "import json,platform,sys;"
            "print(json.dumps({'implementation':platform.python_implementation(),"
            "'version':platform.python_version(),"
            "'abi':f'cp{sys.version_info.major}{sys.version_info.minor}',"
            "'sys_path':sys.path},sort_keys=True,separators=(',',':')))"
        )
        command = bootstrap_recovery._private_loader_command(  # noqa: SLF001
            capsule_root=probe_capsule,
            executable=venv_python,
            arguments=("-I", "-B", "-c", probe),
            additional_library_roots=(base_root / "lib",),
        )
        completed = subprocess.run(  # noqa: S603  # nosec B603
            command,
            check=False,
            capture_output=True,
            env=bootstrap_recovery._bootstrap_environment(),  # noqa: SLF001
            timeout=30,
        )
        if completed.returncode != 0 or len(completed.stdout) > 1024 * 1024:
            raise ValueError("recovery smoke could not inspect the private Python runtime")
        observed = json.loads(completed.stdout)
        if type(observed) is not dict or set(observed) != {
            "implementation",
            "version",
            "abi",
            "sys_path",
        }:
            raise ValueError("recovery smoke private Python probe is invalid")
        raw_path = observed.pop("sys_path")
        if type(raw_path) is not list or any(type(item) is not str for item in raw_path):
            raise ValueError("recovery smoke private Python path is invalid")
        runtime = {name: str(observed[name]) for name in ("implementation", "version", "abi")}
        effective_path = recovery_launcher.effective_recovery_sys_path(
            capsule_root=destination,
            interpreter_sys_path=raw_path,
        )
        count, total, tree_digest = recovery_launcher._installed_environment_tree_identity(  # noqa: SLF001
            environment_root
        )
        environment_manifest = {
            "schema": "kestrel.recovery_environment.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": runtime["version"],
            "python_abi": runtime["abi"],
            "environment_root": str(environment_root),
            "site_packages_path": str(
                environment_root / "lib" / "python3.11" / "site-packages"
            ),
            "site_packages_tree_sha256": tree_digest,
            "site_packages_file_count": count,
            "site_packages_total_size_bytes": total,
        }
        receipts._validate_schema(  # noqa: SLF001
            "kestrel.recovery_environment.v1",
            environment_manifest,
            label="recovery installed environment manifest",
        )
        return (
            effective_path,
            runtime,
            _path_sha256(venv_python),
            base_root / "lib",
            environment_manifest,
        )
    finally:
        shutil.rmtree(runtime_root)


def _execution_closure(
    *,
    source_root: Path,
    dependency_root: Path,
    destination: Path,
    candidate_archive: Path,
    environment_manifest_output: Path,
) -> dict[str, Any]:
    member_bytes: dict[str, bytes] = {}
    for name in sorted(receipts._RECOVERY_CAPSULE_SOURCE_ASSETS):  # noqa: SLF001
        member_bytes[name] = (source_root / name).read_bytes()
    for name in sorted(receipts._RECOVERY_CAPSULE_SCHEMA_ASSETS):  # noqa: SLF001
        member_bytes[name] = (source_root / name).read_bytes()
    evidence = receipts.canonical_json_bytes(
        {"schema": "kestrel.recovery_smoke_evidence.v1", "complete": True}
    )
    member_bytes["evidence/recovery-smoke.json"] = evidence
    member_bytes["candidate-archive.tar"] = receipts._read_regular(  # noqa: SLF001
        candidate_archive,
        label="recovery smoke candidate archive",
        max_bytes=2_147_483_648,
    )
    for name in (
        "recovery/bin/bwrap",
        "recovery/python-runtime-manifest.json",
        "recovery/python-runtime.tar.gz",
        "recovery/requirements.txt",
        "recovery/runtime-manifest.json",
        "recovery/wheelhouse-manifest.json",
    ):
        member_bytes[name] = (dependency_root / name).read_bytes()
    runtime_manifest = json.loads(member_bytes["recovery/runtime-manifest.json"])
    runtime_files = runtime_manifest.get("files")
    if type(runtime_files) is not list or not runtime_files:
        raise ValueError("recovery smoke runtime manifest is incomplete")
    for raw_item in runtime_files:
        if type(raw_item) is not dict or type(raw_item.get("asset_path")) is not str:
            raise ValueError("recovery smoke runtime file is invalid")
        asset_path = raw_item["asset_path"]
        member_bytes[asset_path] = (dependency_root / asset_path).read_bytes()

    python_names = sorted(name for name in member_bytes if name.endswith(".py"))
    shell_names = sorted(name for name in member_bytes if name.endswith(".sh"))
    data_names = sorted(set(member_bytes) - set(python_names) - set(shell_names))
    modules = {recovery_launcher._module_name(name): name for name in python_names}  # noqa: SLF001
    static_edges: set[tuple[str, str, str]] = set()
    dynamic_edges: set[tuple[str, str, str]] = set()
    for name in python_names:
        raw = member_bytes[name]
        static_edges.update(
            recovery_launcher._local_static_imports(  # noqa: SLF001
                member_path=name,
                source=raw,
                modules=modules,
            )
        )
        for importer, module in recovery_launcher._literal_dynamic_imports(  # noqa: SLF001
            member_path=name,
            source=raw,
        ):
            target = modules.get(module)
            if target is None:
                raise ValueError("recovery smoke dynamic import leaves the capsule closure")
            dynamic_edges.add((importer, module, target))

    requirements = dependency_root / "recovery" / "requirements.txt"
    wheelhouse_manifest = dependency_root / "recovery" / "wheelhouse-manifest.json"
    (
        sys_path,
        runtime,
        python_digest,
        base_library_root,
        environment_manifest,
    ) = _probe_final_python(
        destination=destination,
        dependency_root=dependency_root,
    )
    environment_manifest_raw = receipts.canonical_json_bytes(environment_manifest)
    _write(environment_manifest_output, environment_manifest_raw)
    member_bytes["recovery/environment-manifest.json"] = environment_manifest_raw
    data_names = sorted(set(member_bytes) - set(python_names) - set(shell_names))
    python_identity = (
        python_digest,
        f"Python {runtime['version']}",
        runtime["implementation"],
        runtime["version"],
        runtime["abi"],
    )
    trusted_python = bootstrap_recovery.TRUSTED_RECOVERY_PYTHON_IDENTITIES.get(
        (sys.platform, platform.machine()), frozenset()
    )
    if python_identity not in trusted_python:
        raise ValueError("recovery smoke Python identity is not frozen by Kestrel")

    staging_receipt = json.loads(
        (dependency_root / "recovery" / "dependency-staging-receipt.json").read_bytes()
    )
    sandbox_digest = staging_receipt["outputs"]["bubblewrap_sha256"]
    sandbox_version = staging_receipt["outputs"]["bubblewrap_version"]
    sandbox_identity = (sandbox_digest, sandbox_version)
    trusted_sandbox = bootstrap_recovery.TRUSTED_OS_SANDBOX_IDENTITIES.get(
        (sys.platform, platform.machine()), frozenset()
    )
    if sandbox_identity not in trusted_sandbox:
        raise ValueError("recovery smoke sandbox identity is not frozen by Kestrel")

    io_roots = {path: "read" for path in sys_path}
    io_roots[str(destination.parent)] = "read_write"
    io_roots[str(destination)] = "read"
    io_roots[str(base_library_root)] = "read"
    io_roots[str(source_root)] = "read"

    return {
        "schema": "kestrel.recovery_execution_closure.v1",
        "python_members": [
            {"path": name, "sha256": _sha256(member_bytes[name])}
            for name in python_names
        ],
        "static_imports": [
            {
                "importer": importer,
                "module": module,
                "member_path": target,
                "member_sha256": _sha256(member_bytes[target]),
            }
            for importer, module, target in sorted(static_edges)
        ],
        "dynamic_imports": [
            {
                "importer": importer,
                "module": module,
                "member_path": target,
                "member_sha256": _sha256(member_bytes[target]),
            }
            for importer, module, target in sorted(dynamic_edges)
        ],
        "shell_helpers": [
            {"path": name, "sha256": _sha256(member_bytes[name])}
            for name in shell_names
        ],
        "data_resources": [
            {"path": name, "sha256": _sha256(member_bytes[name])}
            for name in data_names
        ],
        "external_executables": [
            {
                "name": "python",
                "path": str(
                    destination.parent
                    / "recovery-runtime"
                    / "environment"
                    / "bin"
                    / "python"
                ),
                "sha256": python_digest,
                "version": f"Python {runtime['version']}",
            },
            {
                "name": "sandbox",
                "path": str(destination / "recovery" / "bin" / "bwrap"),
                "sha256": sandbox_digest,
                "version": sandbox_version,
            },
        ],
        "runtime_files": runtime_files,
        "python_runtime": runtime,
        "dependency_lock": {
            "requirements_path": "recovery/requirements.txt",
            "requirements_sha256": _path_sha256(requirements),
            "environment_manifest_sha256": _sha256(environment_manifest_raw),
            "wheelhouse_manifest_sha256": _path_sha256(wheelhouse_manifest),
            "runtime_manifest_sha256": _path_sha256(
                dependency_root / "recovery" / "runtime-manifest.json"
            ),
            "python_runtime_manifest_sha256": _path_sha256(
                dependency_root / "recovery" / "python-runtime-manifest.json"
            ),
            "python_runtime_archive_sha256": _path_sha256(
                dependency_root / "recovery" / "python-runtime.tar.gz"
            ),
        },
        "sys_path": sys_path,
        "io_roots": [
            {"path": path, "access": io_roots[path]} for path in sorted(io_roots)
        ],
        "network_policy": {"default_deny": True, "allowed_endpoints": []},
        "evidence": {
            "source_bundle_digest": receipts.source_bundle_digest(member_bytes),
            "canonicalization_vector_digest": receipts.CANONICALIZATION_VECTOR_DIGEST,
        },
        "provenance": {
            "producer": "scripts/recovery_launcher.py",
            "provider": "local",
            "method": "static-execution-closure",
        },
        "confidence": 1,
        "validation_status": "validated",
    }


def _run_network_denied_capsule_command(
    capsule_root: Path, *, command: list[str]
) -> None:
    if capsule_root.is_symlink() or not capsule_root.is_dir():
        raise ValueError("recovery smoke capsule root is invalid")
    capsule_root = capsule_root.resolve(strict=True)
    python = capsule_root.parent / "recovery-runtime" / "environment" / "bin" / "python"
    base_library = capsule_root.parent / "recovery-runtime" / "base" / "lib"
    launcher = capsule_root / "scripts" / "recovery_launcher.py"
    closure = capsule_root / "recovery-execution-closure.json"
    if any(path.is_symlink() or not path.is_file() for path in (python, launcher, closure)):
        raise ValueError("recovery smoke launch closure is incomplete")
    closure_raw = closure.read_bytes()
    outer_bootstrap = (
        "import runpy,sys;"
        "target=sys.argv.pop(1);"
        "runpy.run_path(target,run_name='__main__')"
    )
    arguments = (
        "-I",
        "-S",
        "-B",
        "-c",
        outer_bootstrap,
        str(launcher),
        "launch",
        str(closure),
        "--capsule-root",
        str(capsule_root),
        "--executable",
        "python",
        "--",
        str(launcher),
        *command,
    )
    private_command = recovery_launcher.private_loader_command(
        closure=closure_raw,
        executable=python,
        arguments=arguments,
        additional_library_roots=(base_library,),
    )
    completed = subprocess.run(  # noqa: S603  # nosec B603
        private_command,
        check=False,
        capture_output=True,
        env={
            "KESTREL_RECOVERY_SMOKE_SENTINEL": "sandbox-environment-sentinel",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
        },
        timeout=300,
    )
    if (
        completed.returncode != 0
        or len(completed.stdout) > 4 * 1024 * 1024
        or len(completed.stderr) > 4 * 1024 * 1024
    ):
        detail = completed.stderr.decode("utf-8", "replace")[-4000:]
        raise ValueError(f"network-denied recovery capsule execution failed: {detail}")


def _smoke_report(
    *,
    source_sha: str,
    dependency_staging_receipt_digest: str,
    capsule_manifest_digest: str,
    capsule_archive_digest: str,
    owner_key_fingerprint: str,
    host_actuator_binding_digest: str,
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": SMOKE_SCHEMA,
        "source_sha": source_sha,
        "dependency_staging_receipt_digest": dependency_staging_receipt_digest,
        "capsule_manifest_digest": capsule_manifest_digest,
        "capsule_archive_digest": capsule_archive_digest,
        "owner_key_fingerprint": owner_key_fingerprint,
        "host_actuator_binding_digest": host_actuator_binding_digest,
        "network_policy": "offline",
        "sandbox_execution": "network_denied_verified",
        "provenance": {
            "producer": "scripts/run_recovery_capsule_smoke.py",
            "provider": "local",
            "method": "deterministic-recovery-capsule-smoke",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    report["report_digest"] = _sha256(receipts.canonical_json_bytes(report))
    receipts._validate_schema(  # noqa: SLF001
        SMOKE_SCHEMA,
        report,
        label="recovery capsule smoke report",
    )
    return report


def run_smoke(
    *,
    source_root: Path,
    dependency_root: Path,
    source_sha: str,
    work_root: Path,
    host_gh: Path,
) -> dict[str, Any]:
    if sys.platform != "linux" or platform.machine() != "x86_64":
        raise ValueError("recovery capsule smoke requires frozen Linux x86_64 identities")
    if receipts.GIT_SHA_RE.fullmatch(source_sha) is None:
        raise ValueError("recovery capsule smoke source SHA is invalid")
    if source_root.is_symlink() or dependency_root.is_symlink() or host_gh.is_symlink():
        raise ValueError("recovery capsule smoke input root or tool is a symlink")
    source_root = source_root.resolve(strict=True)
    dependency_root = dependency_root.resolve(strict=True)
    host_gh = host_gh.resolve(strict=True)
    if work_root.exists() or work_root.is_symlink():
        raise ValueError("recovery capsule smoke work root must be absent")
    work_root.mkdir(mode=0o700)
    destination = work_root / "extracted-capsule"
    destination.mkdir(mode=0o700)

    identity, public_key, owner_fingerprint = _smoke_signing_identity(work_root)
    owner_keys = _owner_key_observation(source_root, public_key=public_key)
    _write(work_root / "owner-signing-keys-observation.json", owner_keys)

    transaction = json.loads(
        (
            source_root
            / "tests"
            / "fixtures"
            / "release-control"
            / "v3"
            / "server-authorization"
            / "initiate.json"
        ).read_bytes()
    )
    transaction["candidate"]["source_sha"] = source_sha
    transaction["promotion_run"]["head_sha"] = source_sha
    transaction["promotion_run"]["workflow_sha"] = source_sha
    transaction_raw = receipts.canonical_json_bytes(transaction)
    _write(work_root / "transaction-authorization.json", transaction_raw)

    admission = _positive_vector(source_root, "dispatch-admission")
    run = transaction["promotion_run"]
    admission.update(
        {
            "transaction_nonce": run["transaction_nonce"],
            "adopted_run_id": run["run_id"],
            "run_attempt": run["run_attempt"],
            "repository_id": run["repository_id"],
            "workflow_id": run["workflow_id"],
            "workflow_path": run["workflow_path"],
            "expected_ref": run["ref"],
            "expected_head_sha": source_sha,
            "signing_key_fingerprint": owner_fingerprint,
        }
    )
    admission_raw = receipts.canonical_json_bytes(admission)
    admission_signature = receipts.sign_receipt_detached(
        receipt=admission_raw,
        identity_file=identity,
        principal=SMOKE_SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    _write(work_root / "dispatch-admission.json", admission_raw)
    _write(work_root / "dispatch-admission.json.sig", admission_signature)
    _write(
        work_root / "dispatch-admission-verification.json",
        receipts.canonical_json_bytes(
            {
                "receipt_digest": _sha256(admission_raw),
                "signature_digest": _sha256(admission_signature),
                "verification_digest": _sha256(b"synthetic recovery smoke admission"),
            }
        ),
    )

    recovery_authority = receipts.canonical_json_bytes(
        _positive_vector(source_root, "recovery-repository-authority")
    )
    recovery_signature = receipts.sign_receipt_detached(
        receipt=recovery_authority,
        identity_file=identity,
        principal=SMOKE_SIGNING_PRINCIPAL,
        namespace=receipts.SIGNING_NAMESPACE,
    )
    _write(work_root / "recovery-authority.json", recovery_authority)
    _write(work_root / "recovery-authority.json.sig", recovery_signature)
    _write(
        work_root / "recovery-repository-observation.json",
        receipts.canonical_json_bytes(
            {"full_name": "John-MiracleWorker/Kestrel-Release-Recovery", "id": 304}
        ),
    )

    evidence_root = work_root / "normalized-evidence"
    _write(
        evidence_root / "recovery-smoke.json",
        receipts.canonical_json_bytes(
            {"schema": "kestrel.recovery_smoke_evidence.v1", "complete": True}
        ),
    )
    candidate_root = work_root / "candidate"
    _write(candidate_root / "candidate-manifest.json", receipts.canonical_json_bytes({}))
    candidate_archive = work_root / "candidate-archive.tar"
    _write(
        candidate_archive,
        receipts.deterministic_recovery_capsule_archive(candidate_root),
    )
    closure = _execution_closure(
        source_root=source_root,
        dependency_root=dependency_root,
        destination=destination,
        candidate_archive=candidate_archive,
        environment_manifest_output=work_root / "environment-manifest.json",
    )
    closure_raw = receipts.canonical_json_bytes(closure)
    _write(work_root / "recovery-execution-closure.json", closure_raw)

    capsule_root = work_root / "capsule-source"
    result = receipts.main(
        [
            "create-recovery-capsule",
            "--candidate-archive",
            str(candidate_archive),
            "--transaction-authorization",
            str(work_root / "transaction-authorization.json"),
            "--admission-receipt",
            str(work_root / "dispatch-admission.json"),
            "--admission-signature",
            str(work_root / "dispatch-admission.json.sig"),
            "--admission-verification",
            str(work_root / "dispatch-admission-verification.json"),
            "--owner-key-observation",
            str(work_root / "owner-signing-keys-observation.json"),
            "--normalized-evidence-root",
            str(evidence_root),
            "--schema-root",
            str(source_root / "schemas"),
            "--source-root",
            str(source_root),
            "--dependency-root",
            str(dependency_root),
            "--gitleaks-image",
            receipts._GITLEAKS_IMAGE,  # noqa: SLF001
            "--gitleaks-ignore",
            ".gitleaksignore",
            "--recovery-authority-receipt",
            str(work_root / "recovery-authority.json"),
            "--recovery-authority-signature",
            str(work_root / "recovery-authority.json.sig"),
            "--recovery-repository-observation",
            str(work_root / "recovery-repository-observation.json"),
            "--execution-closure",
            str(work_root / "recovery-execution-closure.json"),
            "--environment-manifest",
            str(work_root / "environment-manifest.json"),
            "--output-root",
            str(capsule_root),
        ],
        _clock=lambda: SMOKE_SOURCE_TIME,
    )
    if result != 0:
        raise ValueError("recovery capsule production creation failed")
    receipts.verify_recovery_capsule_root(
        capsule_root,
        expected_owner_key_fingerprint=owner_fingerprint,
    )
    archive = work_root / "recovery-capsule.tar"
    _write(archive, receipts.deterministic_recovery_capsule_archive(capsule_root))
    manifest = capsule_root / "recovery-capsule-manifest.json"
    verification = bootstrap_recovery.bootstrap_recovery_environment(
        archive=archive,
        destination=destination,
        expected_archive_digest=_path_sha256(archive),
        expected_manifest_digest=_path_sha256(manifest),
        expected_owner_key_fingerprint=owner_fingerprint,
    )
    if verification.get("validation_status") != "validated":
        raise ValueError("recovery capsule smoke verification is incomplete")
    closure_path = destination / "recovery-execution-closure.json"
    _run_network_denied_capsule_command(
        destination,
        command=[
            "verify",
            str(closure_path),
            "--capsule-root",
            str(destination),
        ],
    )
    materialized = work_root / "materialized-candidate"
    _run_network_denied_capsule_command(
        destination,
        command=[
            "materialize-candidate",
            str(closure_path),
            "--capsule-root",
            str(destination),
            "--destination",
            str(materialized),
        ],
    )
    if not (materialized / "candidate-manifest.json").is_file():
        raise ValueError("recovery capsule nested materialization did not produce the candidate")
    host_inputs = work_root / "host-actuator-inputs"
    host_inputs.mkdir(mode=0o700)
    host_python = host_inputs / "python"
    host_gh_copy = host_inputs / "gh"
    _write(host_python, Path(sys.executable).resolve(strict=True).read_bytes(), mode=0o700)
    _write(host_gh_copy, host_gh.read_bytes(), mode=0o700)
    binding_path = work_root / "host-actuator-binding.json"
    _run_network_denied_capsule_command(
        destination,
        command=[
            "bind-host-actuator",
            str(closure_path),
            "--capsule-root",
            str(destination),
            "--host-root",
            str(source_root),
            "--host-python",
            str(host_python),
            "--host-gh",
            str(host_gh_copy),
            "--output",
            str(binding_path),
        ],
    )
    host_binding = json.loads(binding_path.read_bytes())
    receipts._validate_schema(  # noqa: SLF001
        "kestrel.recovery_host_actuator_binding.v1",
        host_binding,
        label="recovery smoke host actuator binding",
    )
    dependency_receipt = json.loads(
        (dependency_root / "recovery" / "dependency-staging-receipt.json").read_bytes()
    )
    return _smoke_report(
        source_sha=source_sha,
        dependency_staging_receipt_digest=dependency_receipt["receipt_digest"],
        capsule_manifest_digest=_path_sha256(manifest),
        capsule_archive_digest=_path_sha256(archive),
        owner_key_fingerprint=owner_fingerprint,
        host_actuator_binding_digest=host_binding["binding_digest"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--dependency-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--host-gh", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = run_smoke(
            source_root=args.source_root,
            dependency_root=args.dependency_root,
            source_sha=args.source_sha,
            work_root=args.work_root,
            host_gh=args.host_gh,
        )
        _write(args.output, receipts.canonical_json_bytes(report))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
