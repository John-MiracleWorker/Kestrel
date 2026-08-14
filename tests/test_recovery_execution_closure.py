"""Recovery execution must resolve only the capsule-frozen closure."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import bootstrap_recovery as bootstrap  # noqa: E402
from scripts import recovery_launcher as launcher  # noqa: E402
from scripts import release_control_receipt as receipts  # noqa: E402


def _sha(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize(
    ("entrypoint", "poison_module"),
    [
        ("bootstrap_recovery.py", "recovery_launcher.py"),
        ("recovery_launcher.py", "release_control_receipt.py"),
    ],
)
def test_recovery_entrypoint_rejects_nonisolated_start_before_sibling_import(
    tmp_path: Path, entrypoint: str, poison_module: str
) -> None:
    copied_root = tmp_path / "copied"
    scripts_root = copied_root / "scripts"
    scripts_root.mkdir(parents=True)
    sentinel = tmp_path / "ambient-imported"
    (scripts_root / entrypoint).write_bytes((ROOT / "scripts" / entrypoint).read_bytes())
    (scripts_root / poison_module).write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('loaded')\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(scripts_root / entrypoint), "--help"],
        check=False,
        capture_output=True,
        text=True,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(copied_root)},
    )

    assert result.returncode != 0
    assert not sentinel.exists()
    assert "isolat" in result.stderr.lower()


def _write(path: Path, raw: bytes, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o700 if executable else 0o600)


def _closure(tmp_path: Path) -> tuple[Path, bytes, Path]:
    capsule = tmp_path / "capsule"
    entry = b"import importlib\nimport pkg.helper\nVALUE = importlib.import_module('pkg.plugin')\n"
    helper = b"VALUE = 1\n"
    plugin = b"VALUE = 2\n"
    schema = b'{"schema":"example.v1"}'
    requirements = b"example==1.0 --hash=sha256:" + b"0" * 64 + b"\n"
    wheelhouse = b'{"wheels":[]}'
    tool = b"#!/bin/sh\necho exact-tool 1.0\n"
    _write(capsule / "pkg" / "entry.py", entry)
    _write(capsule / "pkg" / "helper.py", helper)
    _write(capsule / "pkg" / "plugin.py", plugin)
    _write(capsule / "schemas" / "example.schema.json", schema)
    _write(capsule / "recovery" / "requirements.txt", requirements)
    _write(capsule / "recovery" / "wheelhouse-manifest.json", wheelhouse)
    tool_path = tmp_path / "exact-bin" / "exact-tool"
    _write(tool_path, tool, executable=True)
    python_members = [
        {"path": "pkg/entry.py", "sha256": _sha(entry)},
        {"path": "pkg/helper.py", "sha256": _sha(helper)},
        {"path": "pkg/plugin.py", "sha256": _sha(plugin)},
    ]
    closure = {
        "schema": "kestrel.recovery_execution_closure.v1",
        "python_members": python_members,
        "static_imports": [
            {
                "importer": "pkg/entry.py",
                "module": "pkg.helper",
                "member_path": "pkg/helper.py",
                "member_sha256": _sha(helper),
            }
        ],
        "dynamic_imports": [
            {
                "importer": "pkg/entry.py",
                "module": "pkg.plugin",
                "member_path": "pkg/plugin.py",
                "member_sha256": _sha(plugin),
            }
        ],
        "shell_helpers": [],
        "data_resources": [{"path": "schemas/example.schema.json", "sha256": _sha(schema)}],
        "external_executables": [
            {
                "name": "exact-tool",
                "path": str(tool_path),
                "sha256": _sha(tool),
                "version": "exact-tool 1.0",
            }
        ],
        "python_runtime": {
            "implementation": "CPython",
            "version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
            "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        },
        "dependency_lock": {
            "requirements_path": "recovery/requirements.txt",
            "requirements_sha256": _sha(requirements),
            "wheelhouse_manifest_sha256": _sha(wheelhouse),
        },
        "sys_path": [str(capsule)],
        "io_roots": [{"path": str(capsule), "access": "read_write"}],
        "network_policy": {
            "default_deny": True,
            "allowed_endpoints": ["https://api.github.com"],
        },
        "evidence": {
            "source_bundle_digest": _sha(b"sources"),
            "canonicalization_vector_digest": (
                "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
            ),
        },
        "provenance": {
            "producer": "scripts/recovery_launcher.py",
            "provider": "local",
            "method": "static-execution-closure",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    return capsule, _canonical(closure), tool_path


def test_execution_closure_verifies_exact_members_imports_and_tool(
    tmp_path: Path,
) -> None:
    capsule, closure, tool_path = _closure(tmp_path)

    verified = launcher.verify_execution_closure(
        closure=closure,
        capsule_root=capsule,
        active_sys_path=[str(capsule)],
        executable_versions={str(tool_path): "exact-tool 1.0"},
    )

    assert verified["validation_status"] == "validated"
    assert (
        launcher.resolve_dynamic_import(
            closure=closure,
            capsule_root=capsule,
            importer="pkg/entry.py",
            module="pkg.plugin",
        )
        == capsule / "pkg" / "plugin.py"
    )
    assert (
        launcher.resolve_external_executable(
            closure=closure,
            name="exact-tool",
        )
        == tool_path
    )


def test_execution_closure_rejects_poison_ambient_sys_path(tmp_path: Path) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    poison = tmp_path / "poison"
    _write(poison / "pkg" / "plugin.py", b"VALUE = 'attacker'\n")

    with pytest.raises(ValueError, match="sys.path|ambient|closure"):
        launcher.verify_execution_closure(
            closure=closure,
            capsule_root=capsule,
            active_sys_path=[str(poison), str(capsule)],
            executable_versions={str(tool_path): "exact-tool 1.0"},
        )


def test_launcher_does_not_self_certify_declared_sys_path(tmp_path: Path) -> None:
    capsule, closure, _tool_path = _closure(tmp_path)
    poison = capsule / "declared-but-not-active"
    poison.mkdir()
    value = json.loads(closure)
    value["sys_path"] = [str(poison)]
    closure_path = tmp_path / "recovery-execution-closure.json"
    closure_path.write_bytes(_canonical(value))

    with pytest.raises(ValueError, match="python|sys.path|interpreter|closure"):
        launcher.main(
            [
                "verify",
                str(closure_path),
                "--capsule-root",
                str(capsule),
            ]
        )


def test_launcher_refuses_direct_execution_without_a_pinned_os_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    closure_path = tmp_path / "recovery-execution-closure.json"
    closure_path.write_bytes(closure)
    value = json.loads(closure)

    def resolve(*, closure: bytes, name: str) -> Path:
        if name == "python":
            return Path(sys.executable)
        if name == "exact-tool":
            return tool_path
        raise receipts.ReleaseControlError("pinned recovery sandbox is absent")

    monkeypatch.setattr(launcher, "resolve_external_executable", resolve)
    monkeypatch.setattr(
        launcher,
        "inspect_isolated_python",
        lambda executable: (
            [str(capsule)],
            value["python_runtime"],
        ),
    )
    monkeypatch.setattr(launcher, "verify_execution_closure", lambda **kwargs: value)
    monkeypatch.setattr(
        launcher.os,
        "execve",
        lambda *args: (_ for _ in ()).throw(AssertionError("direct execution occurred")),
    )

    with pytest.raises(ValueError, match="sandbox"):
        launcher.main(
            [
                "launch",
                "--capsule-root",
                str(capsule),
                "--executable",
                "exact-tool",
                str(closure_path),
            ]
        )


def test_launcher_rejects_a_self_declared_os_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catch treating a closure-declared digest as independent sandbox authority."""

    capsule, closure, tool_path = _closure(tmp_path)
    sandbox_path = tmp_path / "exact-bin" / "sandbox"
    _write(sandbox_path, b'#!/bin/sh\nexec "$@"\n', executable=True)
    value = json.loads(closure)
    value["external_executables"].append(
        {
            "name": "sandbox",
            "path": str(sandbox_path),
            "sha256": _sha(sandbox_path.read_bytes()),
            "version": "self-declared sandbox 1.0",
        }
    )
    value["external_executables"].sort(key=lambda item: item["name"])
    closure = _canonical(value)
    closure_path = tmp_path / "recovery-execution-closure.json"
    closure_path.write_bytes(closure)

    def resolve(*, closure: bytes, name: str) -> Path:
        if name == "python":
            return Path(sys.executable)
        if name == "exact-tool":
            return tool_path
        if name == "sandbox":
            return sandbox_path
        raise AssertionError(name)

    monkeypatch.setattr(launcher, "resolve_external_executable", resolve)
    monkeypatch.setattr(
        launcher,
        "inspect_isolated_python",
        lambda executable: ([str(capsule)], value["python_runtime"]),
    )
    monkeypatch.setattr(launcher, "verify_execution_closure", lambda **kwargs: value)
    monkeypatch.setattr(
        launcher.os,
        "execve",
        lambda *args: (_ for _ in ()).throw(AssertionError("self-declared sandbox executed")),
    )

    with pytest.raises(ValueError, match="trusted.*sandbox|sandbox.*trusted"):
        launcher.main(
            [
                "launch",
                "--capsule-root",
                str(capsule),
                "--executable",
                "exact-tool",
                str(closure_path),
            ]
        )


def test_execution_closure_uses_absolute_tool_not_path_shadow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    shadow = tmp_path / "shadow" / "exact-tool"
    _write(shadow, b"#!/bin/sh\necho attacker\n", executable=True)
    monkeypatch.setenv("PATH", str(shadow.parent))

    assert (
        launcher.resolve_external_executable(
            closure=closure,
            name="exact-tool",
        )
        == tool_path
    )
    environment = launcher.build_isolated_environment(closure=closure)
    assert environment["PATH"] == str(tool_path.parent)
    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment


def test_recovery_network_policy_allows_only_the_exact_frozen_origin(
    tmp_path: Path,
) -> None:
    capsule, closure, _ = _closure(tmp_path)

    assert (
        launcher.authorize_network_endpoint(
            closure=closure,
            endpoint="https://api.github.com/repos/John-MiracleWorker/Kestrel",
        )
        == "https://api.github.com/repos/John-MiracleWorker/Kestrel"
    )
    for endpoint in (
        "http://api.github.com/repos/John-MiracleWorker/Kestrel",
        "https://api.github.com.evil.example/repos/John-MiracleWorker/Kestrel",
        "https://user@api.github.com/repos/John-MiracleWorker/Kestrel",
        "https://api.github.com/repos/John-MiracleWorker/Kestrel#fragment",
    ):
        with pytest.raises(ValueError, match="network|endpoint|allowlist"):
            launcher.authorize_network_endpoint(
                closure=closure,
                endpoint=endpoint,
            )

    launcher.authorize_launch_arguments(
        closure=closure,
        arguments=(
            "--api-url=https://api.github.com/repos/John-MiracleWorker/Kestrel",
            f"--input={capsule / 'pkg/entry.py'}",
        ),
    )
    with pytest.raises(ValueError, match="network|endpoint|allowlist"):
        launcher.authorize_launch_arguments(
            closure=closure,
            arguments=("--api-url=https://evil.example/v1",),
        )
    with pytest.raises(ValueError, match="I/O|root|path"):
        launcher.authorize_launch_arguments(
            closure=closure,
            arguments=(f"--input={tmp_path / 'ambient.json'}",),
        )


def test_execution_closure_requires_capsule_inside_an_exact_io_root(
    tmp_path: Path,
) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    value = json.loads(closure)
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    value["io_roots"] = [{"path": str(ambient), "access": "read_write"}]

    with pytest.raises(ValueError, match="I/O|root|capsule"):
        launcher.verify_execution_closure(
            closure=_canonical(value),
            capsule_root=capsule,
            active_sys_path=[str(capsule)],
            executable_versions={str(tool_path): "exact-tool 1.0"},
        )


@pytest.mark.parametrize("mutation", ["missing", "changed", "extra"])
def test_execution_closure_rejects_member_inventory_drift(tmp_path: Path, mutation: str) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    if mutation == "missing":
        (capsule / "pkg" / "helper.py").unlink()
    elif mutation == "changed":
        (capsule / "pkg" / "helper.py").write_bytes(b"VALUE = 99\n")
    else:
        _write(capsule / "pkg" / "extra.py", b"VALUE = 'extra'\n")

    with pytest.raises(ValueError, match="member|inventory|digest|missing|extra"):
        launcher.verify_execution_closure(
            closure=closure,
            capsule_root=capsule,
            active_sys_path=[str(capsule)],
            executable_versions={str(tool_path): "exact-tool 1.0"},
        )


def test_execution_closure_rejects_unlisted_or_nonliteral_dynamic_import(
    tmp_path: Path,
) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    with pytest.raises(ValueError, match="dynamic import|allowlist"):
        launcher.resolve_dynamic_import(
            closure=closure,
            capsule_root=capsule,
            importer="pkg/entry.py",
            module="pkg.unlisted",
        )
    entry = capsule / "pkg" / "entry.py"
    entry.write_bytes(
        b"import importlib\nNAME = 'pkg.plugin'\nVALUE = importlib.import_module(NAME)\n"
    )
    value = json.loads(closure)
    value["python_members"][0]["sha256"] = _sha(entry.read_bytes())

    with pytest.raises(ValueError, match="dynamic import|literal"):
        launcher.verify_execution_closure(
            closure=_canonical(value),
            capsule_root=capsule,
            active_sys_path=[str(capsule)],
            executable_versions={str(tool_path): "exact-tool 1.0"},
        )


def _tar_with_member(name: str, *, kind: bytes = tarfile.REGTYPE) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.mode = 0o644
        info.uid = 0
        info.gid = 0
        info.mtime = 0
        if kind == tarfile.REGTYPE:
            payload = b"safe"
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        else:
            info.linkname = "target"
            archive.addfile(info)
    return stream.getvalue()


@pytest.mark.parametrize(
    "archive",
    [
        _tar_with_member("../escape"),
        _tar_with_member("absolute", kind=tarfile.SYMTYPE),
    ],
)
def test_bootstrap_recovery_rejects_traversal_and_links(tmp_path: Path, archive: bytes) -> None:
    archive_path = tmp_path / "capsule.tar"
    archive_path.write_bytes(archive)

    with pytest.raises(ValueError, match="path|link|member|archive"):
        bootstrap.extract_recovery_capsule(
            archive=archive_path,
            destination=tmp_path / "extracted",
        )


def test_bootstrap_recovery_extracts_only_deterministic_regular_members(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "capsule.tar"
    archive_path.write_bytes(_tar_with_member("records/release-authorization.json"))
    destination = tmp_path / "extracted"

    bootstrap.extract_recovery_capsule(archive=archive_path, destination=destination)

    output = destination / "records" / "release-authorization.json"
    assert output.read_bytes() == b"safe"
    assert stat.S_IMODE(output.stat().st_mode) == 0o644
    assert not (tmp_path / "escape").exists()


def _bootstrap_archive(tmp_path: Path, *, extra_wheel: bool = False) -> tuple[Path, Path]:
    source = tmp_path / "capsule-source"
    destination = tmp_path / "capsule"
    app = b"VALUE = 1\n"
    requirements = b""
    wheelhouse_manifest = _canonical({"schema": "kestrel.recovery_wheelhouse.v1", "wheels": []})
    _write(source / "app.py", app)
    _write(source / "recovery" / "requirements.txt", requirements)
    _write(source / "recovery" / "wheelhouse-manifest.json", wheelhouse_manifest)
    (source / "recovery" / "wheelhouse").mkdir()
    if extra_wheel:
        _write(source / "recovery" / "wheelhouse" / "unexpected.whl", b"wheel")
    venv_python = destination / (
        "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python"
    )
    base_python = Path(sys.executable)
    closure = {
        "schema": "kestrel.recovery_execution_closure.v1",
        "python_members": [{"path": "app.py", "sha256": _sha(app)}],
        "static_imports": [],
        "dynamic_imports": [],
        "shell_helpers": [],
        "data_resources": [
            {
                "path": "recovery/requirements.txt",
                "sha256": _sha(requirements),
            },
            {
                "path": "recovery/wheelhouse-manifest.json",
                "sha256": _sha(wheelhouse_manifest),
            },
        ],
        "external_executables": [
            {
                "name": "python",
                "path": str(venv_python),
                "sha256": _sha(base_python.read_bytes()),
                "version": (
                    f"Python {sys.version_info.major}."
                    f"{sys.version_info.minor}.{sys.version_info.micro}"
                ),
            }
        ],
        "python_runtime": {
            "implementation": "CPython",
            "version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
            "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        },
        "dependency_lock": {
            "requirements_path": "recovery/requirements.txt",
            "requirements_sha256": _sha(requirements),
            "wheelhouse_manifest_sha256": _sha(wheelhouse_manifest),
        },
        "sys_path": [str(destination)],
        "io_roots": [{"path": str(destination), "access": "read_write"}],
        "network_policy": {
            "default_deny": True,
            "allowed_endpoints": ["https://api.github.com"],
        },
        "evidence": {
            "source_bundle_digest": _sha(b"bootstrap sources"),
            "canonicalization_vector_digest": (
                "sha256:7d37d1815caf0bb822d1244edcde67b2872cac94c9a6238d036198c55d196054"
            ),
        },
        "provenance": {
            "producer": "scripts/recovery_launcher.py",
            "provider": "local",
            "method": "static-execution-closure",
        },
        "confidence": 1,
        "validation_status": "validated",
    }
    _write(source / "recovery-execution-closure.json", _canonical(closure))
    assets = [
        {
            "media_type": "application/octet-stream",
            "name": path.relative_to(source).as_posix(),
            "sha256": _sha(path.read_bytes()),
            "size_bytes": path.stat().st_size,
        }
        for path in sorted(source.rglob("*"), key=lambda item: item.as_posix())
        if path.is_file()
    ]
    _write(source / "recovery-capsule-manifest.json", _canonical({"assets": assets}))
    archive = tmp_path / "recovery-capsule.tar"
    archive.write_bytes(receipts.deterministic_recovery_capsule_archive(source))
    return archive, destination


def _bootstrap_trust(archive: Path) -> dict[str, str]:
    with tarfile.open(archive, mode="r:") as source:
        member = source.getmember("recovery-capsule-manifest.json")
        extracted = source.extractfile(member)
        assert extracted is not None
        manifest = extracted.read()
    return {
        "expected_archive_digest": _sha(archive.read_bytes()),
        "expected_manifest_digest": _sha(manifest),
        "expected_owner_key_fingerprint": (
            "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"
        ),
    }


def test_bootstrap_recovery_builds_hash_locked_offline_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, destination = _bootstrap_archive(tmp_path)
    monkeypatch.setattr(
        bootstrap,
        "_verify_with_bootstrapped_environment",
        lambda **kwargs: {"validation_status": "validated"},
    )

    verification = bootstrap.bootstrap_recovery_environment(
        archive=archive,
        destination=destination,
        **_bootstrap_trust(archive),
    )

    venv_python = destination / (
        "venv/Scripts/python.exe" if os.name == "nt" else "venv/bin/python"
    )
    assert venv_python.is_file()
    assert not venv_python.is_symlink()
    assert verification["validation_status"] == "validated"


def test_bootstrap_recovery_rejects_unlisted_wheel_before_environment_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, destination = _bootstrap_archive(tmp_path, extra_wheel=True)
    monkeypatch.setattr(
        bootstrap,
        "_verify_with_bootstrapped_environment",
        lambda **kwargs: {"validation_status": "validated"},
    )

    with pytest.raises(ValueError, match="wheelhouse|wheel"):
        bootstrap.bootstrap_recovery_environment(
            archive=archive,
            destination=destination,
            **_bootstrap_trust(archive),
        )

    assert not (destination / "venv").exists()


def test_bootstrap_recovery_requires_verified_capsule_before_environment_creation(
    tmp_path: Path,
) -> None:
    archive, destination = _bootstrap_archive(tmp_path)

    with pytest.raises(ValueError, match="capsule manifest|recovery capsule"):
        bootstrap.bootstrap_recovery_environment(
            archive=archive,
            destination=destination,
            expected_archive_digest=_sha(archive.read_bytes()),
            expected_manifest_digest="sha256:" + "0" * 64,
            expected_owner_key_fingerprint=(
                "sha256:7959022879dce518da9c176e536be639e71b165dccf703a01dd936e08349cad6"
            ),
        )

    assert not (destination / "venv").exists()
