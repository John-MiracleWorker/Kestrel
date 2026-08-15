"""Recovery execution must resolve only the capsule-frozen closure."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import sysconfig
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


def test_recovery_platform_identities_are_independent_and_hash_pinned() -> None:
    expected_python = {
        ("linux", "x86_64"): frozenset(
            {
                (
                    "sha256:dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8",
                    "Python 3.11.14",
                    "CPython",
                    "3.11.14",
                    "cp311",
                )
            }
        )
    }
    expected_sandbox = {
        ("linux", "x86_64"): frozenset(
            {
                (
                    "sha256:52231e1caf55bcbc667b269f49c63599a6f7db4767ae6a039580d0ff853db712",
                    "bubblewrap 0.9.0",
                )
            }
        )
    }

    assert bootstrap.TRUSTED_RECOVERY_PYTHON_IDENTITIES == expected_python
    assert launcher.TRUSTED_RECOVERY_PYTHON_IDENTITIES == expected_python
    assert bootstrap.TRUSTED_OS_SANDBOX_IDENTITIES == expected_sandbox
    assert launcher.TRUSTED_OS_SANDBOX_IDENTITIES == expected_sandbox


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


def test_capsule_launcher_does_not_import_the_checkout_bootstrap_module() -> None:
    source = (ROOT / "scripts" / "recovery_launcher.py").read_text(encoding="utf-8")

    assert "from scripts import bootstrap_recovery" not in source
    assert "recovery_bootstrap.extract_recovery_capsule" not in source


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
        "runtime_files": [],
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
            "runtime_manifest_sha256": None,
            "python_runtime_manifest_sha256": None,
            "python_runtime_archive_sha256": None,
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


def test_execution_closure_distinguishes_root_resources_from_fixed_authority_assets(
    tmp_path: Path,
) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    ignore = b"fixture allowlist\n"
    _write(capsule / ".gitleaksignore", ignore)
    _write(capsule / "release-authorization.json", b"{}")
    value = json.loads(closure)
    value["data_resources"].append(
        {"path": ".gitleaksignore", "sha256": _sha(ignore)}
    )
    value["data_resources"].sort(key=lambda item: item["path"])

    verified = launcher.verify_execution_closure(
        closure=_canonical(value),
        capsule_root=capsule,
        active_sys_path=[str(capsule)],
        executable_versions={str(tool_path): "exact-tool 1.0"},
    )

    assert verified["validation_status"] == "validated"


def test_offline_capsule_binds_an_exact_separate_host_actuator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capsule, closure, host_python = _closure(tmp_path)
    host = tmp_path / "host"
    workflows = {
        ".github/workflows/release-transaction.yml": b"name: Release transaction\n",
        ".github/workflows/release.yml": b"name: Release\n",
    }
    for name, raw in workflows.items():
        _write(capsule / name, raw)
    source_assets = frozenset(
        {"pkg/entry.py", "pkg/helper.py", "pkg/plugin.py", *workflows}
    )
    schema_assets = frozenset({"schemas/example.schema.json"})
    for name in sorted(source_assets | schema_assets):
        _write(host / name, (capsule / name).read_bytes())
    _write(
        capsule / "recovery-capsule-manifest.json",
        _canonical({"candidate": {"source_sha": "a" * 40}}),
    )
    host_gh = tmp_path / "host-tools" / "gh"
    _write(host_gh, b"pinned gh\n", executable=True)
    value = json.loads(closure)
    value["external_executables"] = [
        {
            "name": "python",
            "path": str(host_python),
            "sha256": _sha(host_python.read_bytes()),
            "version": "Python 3.11.14",
        }
    ]
    value["data_resources"].extend(
        {"path": name, "sha256": _sha(raw)} for name, raw in workflows.items()
    )
    value["data_resources"].sort(key=lambda item: item["path"])
    value["io_roots"].append({"path": str(host), "access": "read"})
    value["io_roots"].sort(key=lambda item: item["path"])
    closure = _canonical(value)
    monkeypatch.setattr(receipts, "_RECOVERY_CAPSULE_SOURCE_ASSETS", source_assets)
    monkeypatch.setattr(receipts, "_RECOVERY_CAPSULE_SCHEMA_ASSETS", schema_assets)
    monkeypatch.setattr(
        receipts,
        "PINNED_GH_BINARY_DIGESTS",
        {(sys.platform, launcher.platform.machine()): _sha(host_gh.read_bytes())},
    )

    binding = launcher.build_host_actuator_binding(
        closure=closure,
        capsule_root=capsule,
        host_root=host,
        host_python=host_python,
        host_gh=host_gh,
    )

    assert binding["authority_plane"] == {
        "name": "offline_capsule",
        "network_authority": "deny_all",
        "role": "interpret_and_verify",
    }
    assert binding["actuation_plane"] == {
        "name": "dispatch_pinned_host_workflow",
        "network_authority": "workflow_scoped",
        "role": "acquire_and_mutate",
    }
    assert binding["candidate_source_sha"] == "a" * 40
    assert binding["validation_status"] == "validated"

    (host / "pkg" / "helper.py").write_bytes(b"VALUE = 'drifted'\n")
    with pytest.raises(ValueError, match="host actuator.*identity|source.*mismatch"):
        launcher.build_host_actuator_binding(
            closure=closure,
            capsule_root=capsule,
            host_root=host,
            host_python=host_python,
            host_gh=host_gh,
        )


def test_closure_verification_never_executes_self_declared_tools_before_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capsule, closure, _tool_path = _closure(tmp_path)
    monkeypatch.setattr(
        launcher.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("self-declared tool executed before the sandbox")
        ),
    )

    verified = launcher.verify_execution_closure(
        closure=closure,
        capsule_root=capsule,
        active_sys_path=[str(capsule)],
    )

    assert verified["validation_status"] == "validated"


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


def test_execution_closure_requires_the_complete_active_sys_path(tmp_path: Path) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    standard_library = tmp_path / "stdlib"
    standard_library.mkdir()
    value = json.loads(closure)
    value["sys_path"] = [str(capsule), str(standard_library)]
    value["io_roots"].append({"path": str(standard_library), "access": "read"})
    value["io_roots"].sort(key=lambda item: item["path"])

    with pytest.raises(ValueError, match="sys.path|complete|closure"):
        launcher.verify_execution_closure(
            closure=_canonical(value),
            capsule_root=capsule,
            active_sys_path=[str(capsule)],
            executable_versions={str(tool_path): "exact-tool 1.0"},
        )


def test_effective_recovery_sys_path_prepends_the_capsule_to_the_probe(
    tmp_path: Path,
) -> None:
    capsule = tmp_path / "capsule"
    standard_library = tmp_path / "stdlib"
    capsule.mkdir()
    standard_library.mkdir()

    assert launcher.effective_recovery_sys_path(
        capsule_root=capsule,
        interpreter_sys_path=[str(standard_library)],
    ) == [str(capsule), str(standard_library)]

    with pytest.raises(ValueError, match="duplicated|sys.path"):
        launcher.effective_recovery_sys_path(
            capsule_root=capsule,
            interpreter_sys_path=[str(capsule)],
        )


def test_isolated_python_probe_includes_hash_locked_environment_dependencies() -> None:
    observed, _runtime = launcher.inspect_isolated_python(Path(sys.executable))

    assert sysconfig.get_paths()["purelib"] in observed


def test_isolated_runpy_bootstrap_can_import_capsule_launcher_dependencies() -> None:
    observed, _runtime = launcher.inspect_isolated_python(Path(sys.executable))
    purelib = sysconfig.get_paths()["purelib"]
    effective = launcher.effective_recovery_sys_path(
        capsule_root=ROOT,
        # The development venv adds editable-project paths after purelib. A
        # production recovery venv is wheel-only, so model its frozen prefix.
        interpreter_sys_path=observed[: observed.index(purelib) + 1],
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            launcher.ISOLATED_PYTHON_BOOTSTRAP,
            json.dumps(effective, separators=(",", ":")),
            str(ROOT / "scripts" / "recovery_launcher.py"),
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONNOUSERSITE": "1"},
        timeout=10,
    )

    assert completed.returncode == 0, completed.stderr


def test_isolated_runpy_bootstrap_rejects_an_ambient_secret() -> None:
    observed, _runtime = launcher.inspect_isolated_python(Path(sys.executable))
    purelib = sysconfig.get_paths()["purelib"]
    effective = launcher.effective_recovery_sys_path(
        capsule_root=ROOT,
        interpreter_sys_path=observed[: observed.index(purelib) + 1],
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-B",
            "-c",
            launcher.ISOLATED_PYTHON_BOOTSTRAP,
            json.dumps(effective, separators=(",", ":")),
            str(ROOT / "scripts" / "recovery_launcher.py"),
            "--help",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            "KESTREL_RECOVERY_SMOKE_SENTINEL": "sandbox-environment-sentinel",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONNOUSERSITE": "1",
        },
        timeout=10,
    )

    assert completed.returncode != 0
    assert "ambient recovery environment" in completed.stderr


def test_python_runtime_tree_identity_detects_stdlib_drift(tmp_path: Path) -> None:
    runtime = tmp_path / "base"
    library = runtime / "lib" / "python3.11" / "pathlib.py"
    _write(library, b"trusted stdlib bytes\n")

    original = launcher._python_runtime_tree_identity(runtime)  # noqa: SLF001
    library.write_bytes(b"drifted stdlib bytes\n")
    drifted = launcher._python_runtime_tree_identity(runtime)  # noqa: SLF001

    assert original != drifted


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
        "resolve_trusted_recovery_python",
        lambda **_kwargs: Path(sys.executable),
    )
    monkeypatch.setattr(
        launcher,
        "inspect_isolated_python",
        lambda executable: (
            [],
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
        "resolve_trusted_recovery_python",
        lambda **_kwargs: Path(sys.executable),
    )
    monkeypatch.setattr(
        launcher,
        "inspect_isolated_python",
        lambda executable: ([], value["python_runtime"]),
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


def test_launcher_accepts_only_the_exact_independently_pinned_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _capsule, closure, _tool_path = _closure(tmp_path)
    sandbox = tmp_path / "exact-bin" / "bwrap"
    _write(sandbox, b"trusted bubblewrap fixture\n", executable=True)
    value = json.loads(closure)
    identity = (_sha(sandbox.read_bytes()), "bubblewrap fixture 1.0")
    value["external_executables"].append(
        {
            "name": "sandbox",
            "path": str(sandbox),
            "sha256": identity[0],
            "version": identity[1],
        }
    )
    value["external_executables"].sort(key=lambda item: item["name"])
    closure = _canonical(value)
    monkeypatch.setattr(
        launcher,
        "TRUSTED_OS_SANDBOX_IDENTITIES",
        {(sys.platform, launcher.platform.machine()): frozenset({identity})},
    )

    assert launcher.resolve_trusted_os_sandbox(closure=closure) == sandbox

    sandbox.write_bytes(b"changed bubblewrap fixture\n")
    with pytest.raises(ValueError, match="identity|digest"):
        launcher.resolve_trusted_os_sandbox(closure=closure)


def test_bubblewrap_profile_is_offline_and_mounts_only_declared_roots(
    tmp_path: Path,
) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    sandbox = tmp_path / "exact-bin" / "bwrap"
    _write(sandbox, b"trusted bubblewrap fixture\n", executable=True)
    value = json.loads(closure)
    value["external_executables"].append(
        {
            "name": "sandbox",
            "path": str(sandbox),
            "sha256": _sha(sandbox.read_bytes()),
            "version": "bubblewrap fixture 1.0",
        }
    )
    value["external_executables"].sort(key=lambda item: item["name"])
    closure = _canonical(value)

    arguments = launcher.build_os_sandbox_arguments(
        closure=closure,
        sandbox=sandbox,
        command=[str(tool_path), "--version"],
        declared_endpoints=[],
    )

    assert arguments[0] == str(sandbox)
    assert "--unshare-net" in arguments
    assert "--share-net" not in arguments
    assert arguments[-3:] == ["--", str(tool_path), "--version"]
    capsule_root = str(capsule.resolve(strict=True))
    assert any(
        arguments[index : index + 3] == ["--bind", capsule_root, capsule_root]
        for index in range(len(arguments) - 2)
    )
    assert str(tmp_path.resolve(strict=True)) not in arguments

    with pytest.raises(ValueError, match="network.*sandbox|sandbox.*network"):
        launcher.build_os_sandbox_arguments(
            closure=closure,
            sandbox=sandbox,
            command=[str(tool_path), "--version"],
            declared_endpoints=["https://api.github.com"],
        )


def test_bubblewrap_profile_mounts_only_digest_bound_runtime_files(
    tmp_path: Path,
) -> None:
    capsule, closure, tool_path = _closure(tmp_path)
    sandbox = tmp_path / "exact-bin" / "bwrap"
    runtime = b"fixture dynamic loader"
    runtime_asset = (
        "recovery/runtime/" + "4" * 64 + "-ld-linux-x86-64.so.2"
    )
    runtime_source = capsule / runtime_asset
    _write(runtime_source, runtime)
    _write(sandbox, b"trusted bubblewrap fixture\n", executable=True)
    value = json.loads(closure)
    value["data_resources"].append(
        {"path": runtime_asset, "sha256": _sha(runtime)}
    )
    value["data_resources"].sort(key=lambda item: item["path"])
    value["runtime_files"] = [
        {
            "asset_path": runtime_asset,
            "sandbox_path": "/lib64/ld-linux-x86-64.so.2",
            "sha256": _sha(runtime),
            "size_bytes": len(runtime),
        }
    ]
    value["external_executables"].append(
        {
            "name": "sandbox",
            "path": str(sandbox),
            "sha256": _sha(sandbox.read_bytes()),
            "version": "bubblewrap fixture 1.0",
        }
    )
    value["external_executables"].sort(key=lambda item: item["name"])
    closure = _canonical(value)

    arguments = launcher.build_os_sandbox_arguments(
        closure=closure,
        sandbox=sandbox,
        command=[str(tool_path), "--version"],
        declared_endpoints=[],
    )

    assert ["--dir", "/lib64"] == arguments[
        arguments.index("/lib64") - 1 : arguments.index("/lib64") + 1
    ]
    assert any(
        arguments[index : index + 3]
        == [
            "--ro-bind",
            str(runtime_source.resolve(strict=True)),
            "/lib64/ld-linux-x86-64.so.2",
        ]
        for index in range(len(arguments) - 2)
    )
    assert any(
        arguments[index : index + 3]
        == [
            "--ro-bind",
            str(runtime_source.resolve(strict=True)),
            str(runtime_source.resolve(strict=True)),
        ]
        for index in range(len(arguments) - 2)
    )

    runtime_source.write_bytes(b"tampered loader")
    with pytest.raises(ValueError, match="runtime file identity|member digest"):
        launcher.build_os_sandbox_arguments(
            closure=closure,
            sandbox=sandbox,
            command=[str(tool_path), "--version"],
            declared_endpoints=[],
        )


def test_private_loader_rejects_preload_and_ambient_dependency_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capsule, closure, _tool_path = _closure(tmp_path)
    sandbox = capsule / "recovery" / "bin" / "bwrap"
    loader = capsule / "recovery" / "runtime" / "ld-linux-x86-64.so.2"
    libc = capsule / "recovery" / "runtime" / "libc.so.6"
    _write(sandbox, b"fixture bwrap", executable=True)
    _write(loader, b"fixture loader", executable=True)
    _write(libc, b"fixture libc")
    value = json.loads(closure)
    for path, target in (
        (loader, "/lib64/ld-linux-x86-64.so.2"),
        (libc, "/lib/x86_64-linux-gnu/libc.so.6"),
    ):
        relative = path.relative_to(capsule).as_posix()
        value["data_resources"].append(
            {"path": relative, "sha256": _sha(path.read_bytes())}
        )
        value["runtime_files"].append(
            {
                "asset_path": relative,
                "sandbox_path": target,
                "sha256": _sha(path.read_bytes()),
                "size_bytes": path.stat().st_size,
            }
        )
    value["data_resources"].sort(key=lambda item: item["path"])
    value["runtime_files"].sort(key=lambda item: item["sandbox_path"])
    value["external_executables"].append(
        {
            "name": "sandbox",
            "path": str(sandbox),
            "sha256": _sha(sandbox.read_bytes()),
            "version": "bubblewrap fixture 1.0",
        }
    )
    value["external_executables"].sort(key=lambda item: item["name"])
    checked = _canonical(value)

    preload = tmp_path / "ld.so.preload"
    preload.write_text("/tmp/poison.so\n", encoding="utf-8")
    with pytest.raises(ValueError, match="preload"):
        launcher._ensure_no_global_loader_preload(preload_path=preload)  # noqa: SLF001

    class Completed:
        returncode = 0
        stderr = ""
        stdout = "libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x1)\n"

    monkeypatch.setattr(launcher.subprocess, "run", lambda *_args, **_kwargs: Completed())
    with pytest.raises(ValueError, match="ambient|private|loader"):
        launcher.private_loader_command(
            closure=checked,
            executable=sandbox,
            arguments=("--version",),
            preload_path=tmp_path / "absent-preload",
        )

    Completed.stdout = f"libc.so.6 => {libc} (0x1)\n{loader} (0x2)\n"
    command = launcher.private_loader_command(
        closure=checked,
        executable=sandbox,
        arguments=("--version",),
        preload_path=tmp_path / "absent-preload",
    )
    assert command[:4] == [
        str(loader),
        "--inhibit-cache",
        "--library-path",
        str(loader.parent),
    ]
    assert command[-2:] == [str(sandbox), "--version"]


def test_materialize_main_verifies_complete_closure_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capsule, closure, tool = _closure(tmp_path)
    closure_path = capsule / "closure.json"
    closure_path.write_bytes(closure)
    destination = tmp_path / "candidate"
    mutated = False

    monkeypatch.setattr(launcher, "resolve_trusted_recovery_python", lambda **_kwargs: tool)
    monkeypatch.setattr(
        launcher,
        "inspect_isolated_python",
        lambda *_args, **_kwargs: (
            [str(capsule)],
            {
                "implementation": "CPython",
                "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
            },
        ),
    )
    monkeypatch.setattr(
        launcher,
        "effective_recovery_sys_path",
        lambda **_kwargs: [str(capsule)],
    )

    def reject(**_kwargs: object) -> dict[str, object]:
        raise receipts.ReleaseControlError("full closure rejected")

    def mutate(**_kwargs: object) -> None:
        nonlocal mutated
        mutated = True

    monkeypatch.setattr(launcher, "verify_execution_closure", reject)
    monkeypatch.setattr(launcher, "materialize_candidate_from_capsule", mutate)

    with pytest.raises(ValueError, match="full closure rejected"):
        launcher.main(
            [
                "materialize-candidate",
                str(closure_path),
                "--capsule-root",
                str(capsule),
                "--destination",
                str(destination),
            ]
        )
    assert mutated is False


def test_host_binding_cli_output_is_authorized_write_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capsule, closure, tool = _closure(tmp_path)
    closure_path = capsule / "closure.json"
    closure_path.write_bytes(closure)
    output = capsule / "binding.json"
    output.write_bytes(b"existing")
    monkeypatch.setattr(launcher, "resolve_trusted_recovery_python", lambda **_kwargs: tool)
    monkeypatch.setattr(
        launcher,
        "inspect_isolated_python",
        lambda *_args, **_kwargs: (
            [str(capsule)],
            {
                "implementation": "CPython",
                "version": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
                "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
            },
        ),
    )
    monkeypatch.setattr(launcher, "effective_recovery_sys_path", lambda **_kwargs: [str(capsule)])
    monkeypatch.setattr(launcher, "verify_execution_closure", lambda **_kwargs: json.loads(closure))
    monkeypatch.setattr(
        launcher,
        "build_host_actuator_binding",
        lambda **_kwargs: {"binding_digest": _sha(b"binding")},
    )

    with pytest.raises((FileExistsError, ValueError), match="exist|write once|output"):
        launcher.main(
            [
                "bind-host-actuator",
                str(closure_path),
                "--capsule-root",
                str(capsule),
                "--host-root",
                str(capsule),
                "--host-python",
                str(tool),
                "--host-gh",
                str(tool),
                "--output",
                str(output),
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


def test_launcher_materializes_candidate_only_from_the_bound_capsule_archive(
    tmp_path: Path,
) -> None:
    capsule, closure, _tool_path = _closure(tmp_path)
    candidate_archive = capsule / "candidate-archive.tar"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        payload = b'{"schema":"kestrel.release_candidate.v1"}'
        member = tarfile.TarInfo("candidate-manifest.json")
        member.mode = 0o644
        member.uid = 0
        member.gid = 0
        member.mtime = 0
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    _write(candidate_archive, stream.getvalue())
    value = json.loads(closure)
    value["data_resources"].append(
        {"path": "candidate-archive.tar", "sha256": _sha(candidate_archive.read_bytes())}
    )
    value["data_resources"].sort(key=lambda item: item["path"])
    value["io_roots"].append({"path": str(tmp_path), "access": "read_write"})
    value["io_roots"].sort(key=lambda item: item["path"])
    destination = tmp_path / "candidate"

    launcher.materialize_candidate_from_capsule(
        closure=_canonical(value),
        capsule_root=capsule,
        destination=destination,
    )

    assert destination.joinpath("candidate-manifest.json").read_bytes() == payload
    candidate_archive.write_bytes(candidate_archive.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="capsule|archive|digest|member"):
        launcher.materialize_candidate_from_capsule(
            closure=_canonical(value),
            capsule_root=capsule,
            destination=tmp_path / "second-candidate",
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


def _bootstrap_archive(
    tmp_path: Path,
    *,
    extra_wheel: bool = False,
    include_sandbox: bool = True,
) -> tuple[Path, Path]:
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
    venv_python = destination.parent / "recovery-runtime" / "environment" / "bin" / "python"
    base_python = Path(sys.executable)
    python_digest = _sha(base_python.read_bytes())
    runtime_raw = b"trusted runtime fixture"
    runtime_asset = "recovery/runtime/ld-linux-x86-64.so.2"
    runtime_files = [
        {
            "asset_path": runtime_asset,
            "sandbox_path": "/lib64/ld-linux-x86-64.so.2",
            "sha256": _sha(runtime_raw),
            "size_bytes": len(runtime_raw),
        }
    ]
    runtime_manifest = _canonical(
        {
            "schema": "kestrel.recovery_runtime.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
            "python_executable_sha256": python_digest,
            "files": runtime_files,
        }
    )
    _write(source / runtime_asset, runtime_raw)
    _write(source / "recovery" / "runtime-manifest.json", runtime_manifest)
    python_runtime_stream = io.BytesIO()
    with tarfile.open(fileobj=python_runtime_stream, mode="w:gz") as archive:
        directory = tarfile.TarInfo("bin")
        directory.type = tarfile.DIRTYPE
        directory.mode = 0o755
        directory.uid = directory.gid = directory.mtime = 0
        archive.addfile(directory)
        binary = tarfile.TarInfo("bin/python3.11")
        binary.mode = 0o755
        binary.uid = binary.gid = binary.mtime = 0
        binary.size = base_python.stat().st_size
        with base_python.open("rb") as body:
            archive.addfile(binary, body)
    python_runtime_archive = python_runtime_stream.getvalue()
    python_tree_records = [
        {
            "mode": "0755",
            "path": "bin/python3.11",
            "sha256": python_digest,
            "size_bytes": base_python.stat().st_size,
        }
    ]
    python_runtime_manifest = _canonical(
        {
            "schema": "kestrel.recovery_python_runtime.v1",
            "platform": "ubuntu-24.04-x86_64",
            "python_version": (
                f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
            ),
            "python_abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
            "python_executable_path": "bin/python3.11",
            "python_executable_sha256": python_digest,
            "source_archive_url": bootstrap.RECOVERY_PYTHON_PACKAGE_URL,
            "source_archive_sha256": bootstrap.RECOVERY_PYTHON_PACKAGE_DIGEST,
            "runtime_archive_path": "recovery/python-runtime.tar.gz",
            "runtime_archive_sha256": _sha(python_runtime_archive),
            "runtime_archive_size_bytes": len(python_runtime_archive),
            "runtime_tree_sha256": _sha(_canonical(python_tree_records)),
            "runtime_file_count": 1,
            "runtime_total_size_bytes": base_python.stat().st_size,
        }
    )
    _write(source / "recovery" / "python-runtime-manifest.json", python_runtime_manifest)
    _write(source / "recovery" / "python-runtime.tar.gz", python_runtime_archive)
    sandbox = source / "recovery" / "bin" / "bwrap"
    sandbox_raw = b"trusted bubblewrap fixture\n"
    if include_sandbox:
        _write(sandbox, sandbox_raw)
    data_resources = [
        {
            "path": "recovery/runtime-manifest.json",
            "sha256": _sha(runtime_manifest),
        },
        {
            "path": "recovery/python-runtime-manifest.json",
            "sha256": _sha(python_runtime_manifest),
        },
        {
            "path": "recovery/python-runtime.tar.gz",
            "sha256": _sha(python_runtime_archive),
        },
        {
            "path": runtime_asset,
            "sha256": _sha(runtime_raw),
        },
        {
            "path": "recovery/requirements.txt",
            "sha256": _sha(requirements),
        },
        {
            "path": "recovery/wheelhouse-manifest.json",
            "sha256": _sha(wheelhouse_manifest),
        },
    ]
    external_executables = [
        {
            "name": "python",
            "path": str(venv_python),
            "sha256": python_digest,
            "version": (
                f"Python {sys.version_info.major}."
                f"{sys.version_info.minor}.{sys.version_info.micro}"
            ),
        }
    ]
    if include_sandbox:
        data_resources.append(
            {
                "path": "recovery/bin/bwrap",
                "sha256": _sha(sandbox_raw),
            }
        )
        external_executables.append(
            {
                "name": "sandbox",
                "path": str(destination / "recovery" / "bin" / "bwrap"),
                "sha256": _sha(sandbox_raw),
                "version": "bubblewrap fixture 1.0",
            }
        )
        external_executables.sort(key=lambda item: item["name"])
    data_resources.sort(key=lambda item: item["path"])
    closure = {
        "schema": "kestrel.recovery_execution_closure.v1",
        "python_members": [{"path": "app.py", "sha256": _sha(app)}],
        "static_imports": [],
        "dynamic_imports": [],
        "shell_helpers": [],
        "data_resources": data_resources,
        "external_executables": external_executables,
        "runtime_files": runtime_files,
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
            "runtime_manifest_sha256": _sha(runtime_manifest),
            "python_runtime_manifest_sha256": _sha(python_runtime_manifest),
            "python_runtime_archive_sha256": _sha(python_runtime_archive),
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


def _trust_current_bootstrap_python(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    monkeypatch.setattr(
        bootstrap,
        "TRUSTED_RECOVERY_PYTHON_IDENTITIES",
        {
            (sys.platform, bootstrap.platform.machine()): frozenset(
                {
                    (
                        _sha(Path(sys.executable).resolve(strict=True).read_bytes()),
                        f"Python {runtime_version}",
                        "CPython",
                        runtime_version,
                        f"cp{sys.version_info.major}{sys.version_info.minor}",
                    )
                }
            )
        },
    )


def _trust_fixture_bootstrap_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        bootstrap,
        "TRUSTED_OS_SANDBOX_IDENTITIES",
        {
            (sys.platform, bootstrap.platform.machine()): frozenset(
                {(_sha(b"trusted bubblewrap fixture\n"), "bubblewrap fixture 1.0")}
            )
        },
    )
    monkeypatch.setattr(
        bootstrap,
        "_private_loader_command",
        lambda *, executable, arguments, **_kwargs: [
            str(executable if "environment" in executable.parts else Path(sys.executable)),
            *arguments,
        ],
    )


def test_bootstrap_recovery_builds_hash_locked_offline_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, destination = _bootstrap_archive(tmp_path)
    _trust_current_bootstrap_python(monkeypatch)
    _trust_fixture_bootstrap_sandbox(monkeypatch)
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

    venv_python = destination.parent / "recovery-runtime" / "environment" / "bin" / "python"
    assert venv_python.is_file()
    assert not venv_python.is_symlink()
    assert verification["validation_status"] == "validated"


def test_bootstrap_recovery_rejects_a_self_declared_python_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, destination = _bootstrap_archive(tmp_path)
    monkeypatch.setattr(bootstrap, "TRUSTED_RECOVERY_PYTHON_IDENTITIES", {})

    with pytest.raises(ValueError, match="trusted.*Python|Python.*trusted"):
        bootstrap.bootstrap_recovery_environment(
            archive=archive,
            destination=destination,
            **_bootstrap_trust(archive),
        )

    assert not (destination.parent / "recovery-runtime").exists()


def test_bootstrap_recovery_prepares_only_an_independently_trusted_capsule_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, destination = _bootstrap_archive(tmp_path, include_sandbox=True)
    _trust_current_bootstrap_python(monkeypatch)
    _trust_fixture_bootstrap_sandbox(monkeypatch)
    monkeypatch.setattr(
        bootstrap,
        "_verify_with_bootstrapped_environment",
        lambda **kwargs: {"validation_status": "validated"},
    )

    bootstrap.bootstrap_recovery_environment(
        archive=archive,
        destination=destination,
        **_bootstrap_trust(archive),
    )

    sandbox = destination / "recovery" / "bin" / "bwrap"
    assert sandbox.read_bytes() == b"trusted bubblewrap fixture\n"
    assert stat.S_IMODE(sandbox.stat().st_mode) == 0o500


def test_bootstrap_recovery_rejects_a_self_declared_capsule_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, destination = _bootstrap_archive(tmp_path, include_sandbox=True)
    _trust_current_bootstrap_python(monkeypatch)
    monkeypatch.setattr(bootstrap, "TRUSTED_OS_SANDBOX_IDENTITIES", {})

    with pytest.raises(ValueError, match="trusted.*sandbox|sandbox.*trusted"):
        bootstrap.bootstrap_recovery_environment(
            archive=archive,
            destination=destination,
            **_bootstrap_trust(archive),
        )

    assert not (destination.parent / "recovery-runtime").exists()


def test_bootstrap_recovery_rejects_unlisted_wheel_before_environment_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive, destination = _bootstrap_archive(tmp_path, extra_wheel=True)
    _trust_current_bootstrap_python(monkeypatch)
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

    assert not (destination.parent / "recovery-runtime").exists()


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

    assert not (destination.parent / "recovery-runtime").exists()
