from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from nested_memvid_agent.llm import model_catalog as model_catalog_module
from scripts.verify_desktop_resource_manifest import (
    DesktopManifestIdentity,
    load_desktop_public_key,
    verify_developer_resource_manifest,
)

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_desktop_sidecar.py"
SBOM_SCRIPT = ROOT / "scripts" / "generate_desktop_sbom.py"
ENTRYPOINT = ROOT / "packaging" / "kestrel-sidecar-entry.py"
SPEC = ROOT / "packaging" / "kestrel-sidecar.spec"
STAGE_SCRIPT = ROOT / "desktop" / "scripts" / "stage-resources.mjs"
SOURCE_COMMIT = "a" * 40


def _load_python_script(path: Path, name: str) -> ModuleType:
    assert path.is_file(), f"missing {path.relative_to(ROOT)}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run_git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return completed.stdout.strip()


def _git_fixture(root: Path) -> str:
    root.mkdir()
    _run_git(root, "init", "-q")
    _run_git(root, "config", "user.email", "packaging@example.invalid")
    _run_git(root, "config", "user.name", "Packaging Test")
    (root / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _run_git(root, "add", "tracked.txt")
    _run_git(root, "commit", "-qm", "fixture")
    return _run_git(root, "rev-parse", "HEAD")


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _file_inventory(root: Path) -> dict[str, dict[str, object]]:
    return {
        path.relative_to(root).as_posix(): {
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _asset_receipt(
    root: Path,
    *,
    kind: str,
    lock_sha256: str,
) -> dict[str, object]:
    return {
        "schema": "kestrel.desktop.asset-build.v1",
        "kind": kind,
        "source_commit": SOURCE_COMMIT,
        "root": str(root.resolve()),
        "lock_sha256": lock_sha256,
        "files": _file_inventory(root),
    }


def test_task_11b_files_and_exact_pyinstaller_pin_exist() -> None:
    for path in (ENTRYPOINT, SPEC, BUILD_SCRIPT, SBOM_SCRIPT, STAGE_SCRIPT):
        assert path.is_file(), f"missing {path.relative_to(ROOT)}"

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert "pyinstaller==6.21.0" in pyproject["dependency-groups"]["release"]
    locked = {
        package["name"]: package["version"]
        for package in lock["package"]
    }
    assert locked["pyinstaller"] == "6.21.0"


def test_entrypoint_and_spec_cover_runtime_without_legacy_or_development_roots() -> None:
    entrypoint = ENTRYPOINT.read_text(encoding="utf-8")
    spec = SPEC.read_text(encoding="utf-8")
    build_script = BUILD_SCRIPT.read_text(encoding="utf-8")

    assert "from nested_memvid_agent.desktop_sidecar import main" in entrypoint
    assert "main()" in entrypoint
    assert "create(" not in entrypoint
    for required in (
        'collect_submodules("nested_memvid_agent")',
        'collect_all("memvid_sdk")',
        'collect_submodules("anthropic")',
        'collect_submodules("google.genai", filter=_exclude_test_modules)',
        'collect_submodules("keyring.backends")',
        'collect_submodules("mcp", filter=_exclude_mcp_cli)',
        'collect_submodules("openai")',
        'collect_submodules("starlette")',
        'collect_submodules("yaml")',
        'collect_data_files("tzdata")',
        'copy_metadata("nested-memvid-agent")',
        '"prompts/*.md"',
        '"fastapi"',
        '"uvicorn"',
        '"pydantic"',
        '"pydantic_settings"',
        '"nested_memvid_agent.server"',
        '"nested_memvid_agent.llm.mock"',
        "web/dist",
        "THIRD_PARTY_NOTICES.txt",
        "LICENSE",
        "upx=False",
    ):
        assert required in spec
    assert '"--noupx"' not in build_script
    for forbidden in (
        '"qrcode"',
        '"pytest"',
        '"tests"',
        '"benchmark"',
        '".nest"',
        '".env"',
    ):
        assert forbidden in spec
    assert ".mv2" not in spec


def test_frozen_entrypoint_dispatches_the_isolated_provider_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = _load_python_script(ENTRYPOINT, "task11b_frozen_entrypoint")
    observed: list[str] = []
    monkeypatch.setattr(
        entrypoint,
        "_run_desktop_sidecar",
        lambda _arguments: observed.append("sidecar") or 0,
    )
    monkeypatch.setattr(
        entrypoint,
        "_run_provider_http_worker",
        lambda: observed.append("provider-worker") or 0,
    )

    assert entrypoint.main([]) == 0
    assert entrypoint.main([entrypoint.PROVIDER_HTTP_WORKER_ARGUMENT]) == 0
    assert observed == ["sidecar", "provider-worker"]
    with pytest.raises(ValueError, match="unsupported frozen sidecar arguments"):
        entrypoint.main([entrypoint.PROVIDER_HTTP_WORKER_ARGUMENT, "extra"])


def test_frozen_provider_worker_reexecutes_the_bundled_sidecar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)

    assert model_catalog_module._provider_http_worker_command() == [
        str(Path(sys.executable).resolve(strict=True)),
        model_catalog_module.PROVIDER_HTTP_WORKER_ARGUMENT,
    ]


def test_spec_dry_evaluation_passes_flat_supported_data_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pyinstaller = ModuleType("PyInstaller")
    pyinstaller.__path__ = []  # type: ignore[attr-defined]
    utils = ModuleType("PyInstaller.utils")
    utils.__path__ = []  # type: ignore[attr-defined]
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_all = lambda _name: (  # type: ignore[attr-defined]
        [("/source/memvid.data", "memvid_sdk")],
        [("/source/memvid.native", ".")],
        ["memvid_sdk"],
    )
    hooks.collect_data_files = lambda _name, **_kwargs: [  # type: ignore[attr-defined]
        ("/source/tzdata.data", "tzdata")
    ]
    hooks.copy_metadata = lambda _name: [  # type: ignore[attr-defined]
        ("/source/nested_memvid_agent.dist-info", "nested_memvid_agent.dist-info")
    ]
    def collect_submodules(
        name: str,
        **kwargs: object,
    ) -> list[str]:
        if name not in {"google.genai", "mcp"}:
            return [f"{name}.fixture"]
        module_filter = kwargs.get("filter")
        assert callable(module_filter)
        if name == "google.genai":
            return [
                module
                for module in (
                    "google.genai.runtime",
                    "google.genai.tests",
                    "google.genai.tests.test_client",
                    "google.genai._test_api_client",
                )
                if module_filter(module)
            ]
        return [
            module
            for module in ("mcp.client", "mcp.cli", "mcp.cli.cli")
            if module_filter(module)
        ]

    hooks.collect_submodules = collect_submodules  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "PyInstaller", pyinstaller)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils", utils)
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    observed: dict[str, object] = {}

    def analysis(*args: object, **kwargs: object) -> SimpleNamespace:
        datas = kwargs["datas"]
        assert isinstance(datas, list)
        assert all(
            isinstance(item, tuple)
            and len(item) == 2
            and all(isinstance(value, str) for value in item)
            for item in datas
        )
        observed["datas"] = datas
        observed["entrypoints"] = args[0]
        observed["hiddenimports"] = kwargs["hiddenimports"]
        observed["pathex"] = kwargs["pathex"]
        return SimpleNamespace(
            pure=[],
            scripts=[],
            binaries=[],
            datas=datas,
        )

    runpy.run_path(
        str(SPEC),
        init_globals={
            "Analysis": analysis,
            "EXE": lambda *_args, **_kwargs: object(),
            "PYZ": lambda *_args, **_kwargs: object(),
            "SPECPATH": str(SPEC.parent),
        },
    )
    assert observed["datas"]
    assert observed["entrypoints"] == [str(ENTRYPOINT)]
    assert "google.genai.runtime" in observed["hiddenimports"]
    assert "google.genai.tests" not in observed["hiddenimports"]
    assert "google.genai.tests.test_client" not in observed["hiddenimports"]
    assert "google.genai._test_api_client" not in observed["hiddenimports"]
    assert "mcp.client" in observed["hiddenimports"]
    assert "mcp.cli" not in observed["hiddenimports"]
    assert "mcp.cli.cli" not in observed["hiddenimports"]
    assert observed["pathex"] == [str(ROOT / "src")]


def test_build_guard_rejects_dirty_or_wrong_source_and_upx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_python_script(BUILD_SCRIPT, "task11b_build_guard")
    source = tmp_path / "source"
    commit = _git_fixture(source)

    assert build.validate_source_checkout(source, expected_commit=commit) == commit
    with pytest.raises(ValueError, match="source commit mismatch"):
        build.validate_source_checkout(source, expected_commit="f" * 40)

    (source / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(ValueError, match="clean"):
        build.validate_source_checkout(source, expected_commit=commit)

    monkeypatch.setenv("UPX", "/tmp/untrusted-upx")
    with pytest.raises(ValueError, match="UPX"):
        build.validate_upx_disabled()
    monkeypatch.delenv("UPX")
    with pytest.raises(ValueError, match="6.21.0"):
        build.validate_pyinstaller_version("6.20.0")
    assert build.validate_pyinstaller_version("6.21.0") == "6.21.0"


def test_packaging_runtime_roots_fail_closed_when_missing_or_legacy(
    tmp_path: Path,
) -> None:
    build = _load_python_script(BUILD_SCRIPT, "task11b_runtime_roots")
    for relative in build.REQUIRED_RUNTIME_ROOTS:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")

    build.validate_packaging_runtime_roots(tmp_path)
    missing = tmp_path / build.REQUIRED_RUNTIME_ROOTS[0]
    missing.unlink()
    with pytest.raises(ValueError, match="missing runtime root"):
        build.validate_packaging_runtime_roots(tmp_path)
    missing.write_text("fixture\n", encoding="utf-8")

    legacy = tmp_path / build.FORBIDDEN_RUNTIME_ROOTS[0]
    legacy.mkdir(parents=True)
    with pytest.raises(ValueError, match="forbidden runtime root"):
        build.validate_packaging_runtime_roots(tmp_path)


def test_frozen_archive_inventory_requires_core_and_rejects_development_roots() -> None:
    build = _load_python_script(BUILD_SCRIPT, "task11b_frozen_inventory")
    valid_members = {
        member
        for member in build.REQUIRED_FROZEN_ARCHIVE_MEMBERS
        if ".dist-info/" not in member
    }
    valid_members.add("nested_memvid_agent-9.9.9.dist-info/METADATA")
    valid = "\n".join(sorted(valid_members))

    assert set(build.validate_frozen_archive_listing(valid)) == valid_members
    without_metadata = "\n".join(
        sorted(
            member
            for member in valid_members
            if ".dist-info/" not in member
        )
    )
    with pytest.raises(ValueError, match="distribution metadata"):
        build.validate_frozen_archive_listing(without_metadata)
    for forbidden in (
        "google.genai.tests.test_client",
        "google.genai._test_api_client",
        "jsonschema/benchmarks/issue.json",
        "nested_memvid_agent/qrcode/legacy.py",
        "nested_memvid_agent/video_frames/frame.bin",
        "nested_memvid_agent/web_dist/assets/app.js.map",
        "nested_memvid_agent/__pycache__/agent.pyc",
        ".env.production",
        ".nest/state.json",
        "credentials.json",
    ):
        with pytest.raises(ValueError, match="forbidden frozen archive member"):
            build.validate_frozen_archive_listing(f"{valid}\n{forbidden}\n")


def test_bundled_runtime_distributions_must_match_the_python_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_python_script(BUILD_SCRIPT, "task11b_runtime_distributions")
    lock = tmp_path / "uv.lock"
    lock.write_text(
        "version = 1\n"
        + "".join(
            f'[[package]]\nname = "{name}"\nversion = "1.0.0"\n'
            for name in build.REQUIRED_BUNDLED_DISTRIBUTIONS
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        build.importlib.metadata,
        "version",
        lambda _name: "1.0.0",
    )

    validated = build.validate_bundled_runtime_distributions(lock)

    assert validated["openai"] == "1.0.0"
    monkeypatch.setattr(
        build.importlib.metadata,
        "version",
        lambda name: "9.9.9" if name == "openai" else "1.0.0",
    )
    with pytest.raises(ValueError, match="openai.*uv.lock"):
        build.validate_bundled_runtime_distributions(lock)


def test_web_build_receipt_binds_exact_source_lock_and_inventory(
    tmp_path: Path,
) -> None:
    build = _load_python_script(BUILD_SCRIPT, "task11b_web_receipt")
    web_root = tmp_path / "web-dist"
    (web_root / "assets").mkdir(parents=True)
    (web_root / "index.html").write_text("<h1>Kestrel</h1>", encoding="utf-8")
    (web_root / "assets" / "app.js").write_text("export {};\n", encoding="utf-8")
    lock = tmp_path / "package-lock.json"
    lock.write_text('{"lockfileVersion":3,"packages":{}}\n', encoding="utf-8")
    receipt_path = tmp_path / "web-receipt.json"
    receipt = _asset_receipt(
        web_root,
        kind="web",
        lock_sha256=_sha256(lock),
    )
    _write_json(receipt_path, receipt)

    digest = build.validate_web_build_receipt(
        receipt_path,
        source_commit=SOURCE_COMMIT,
        web_lock_path=lock,
        expected_root=web_root,
    )
    assert digest == _sha256(receipt_path)

    (web_root / "assets" / "app.js").write_text("changed\n", encoding="utf-8")
    with pytest.raises(ValueError, match="inventory"):
        build.validate_web_build_receipt(
            receipt_path,
            source_commit=SOURCE_COMMIT,
            web_lock_path=lock,
            expected_root=web_root,
        )


def test_production_web_receipt_step_runs_exact_npm_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    build = _load_python_script(BUILD_SCRIPT, "task11b_web_producer")
    source = tmp_path / "source"
    web_root = source / "web" / "dist"
    web_root.mkdir(parents=True)
    (web_root / "index.html").write_text("<h1>Kestrel</h1>", encoding="utf-8")
    (source / "web" / "package-lock.json").write_text(
        '{"lockfileVersion":3,"packages":{}}\n',
        encoding="utf-8",
    )
    receipt_path = tmp_path / "evidence" / "web-receipt.json"
    commands: list[list[str]] = []
    monkeypatch.setattr(
        build,
        "validate_source_checkout",
        lambda _root, *, expected_commit: expected_commit,
    )
    monkeypatch.setattr(build, "_validate_node_22", lambda _root: None)
    monkeypatch.setattr(
        build,
        "_run_checked",
        lambda command, *, cwd: commands.append(command),
    )

    receipt = build.prepare_web_build_receipt(
        source_root=source,
        expected_commit=SOURCE_COMMIT,
        receipt_path=receipt_path,
    )

    assert commands == [
        ["npm", "--prefix", "web", "ci"],
        ["npm", "--prefix", "web", "run", "licenses:check"],
        ["npm", "--prefix", "web", "run", "build"],
    ]
    assert receipt["source_commit"] == SOURCE_COMMIT
    assert receipt["lock_sha256"] == _sha256(source / "web" / "package-lock.json")
    assert receipt_path.read_bytes() == _canonical_json_bytes(receipt)


def test_sidecar_build_receipt_is_complete_bounded_and_digest_bound(
    tmp_path: Path,
) -> None:
    build = _load_python_script(BUILD_SCRIPT, "task11b_receipt")
    binary = tmp_path / ("kestrel-desktop-sidecar.exe" if os.name == "nt" else "kestrel-desktop-sidecar")
    binary.write_bytes(b"frozen-sidecar")
    entrypoint = tmp_path / "entry.py"
    spec = tmp_path / "sidecar.spec"
    python_lock = tmp_path / "uv.lock"
    entrypoint.write_text("entry\n", encoding="utf-8")
    spec.write_text("spec\n", encoding="utf-8")
    python_lock.write_text("lock\n", encoding="utf-8")

    receipt = build.create_sidecar_build_receipt(
        source_commit=SOURCE_COMMIT,
        app_version="0.5.0",
        binary_path=binary,
        entrypoint_path=entrypoint,
        spec_path=spec,
        python_lock_path=python_lock,
        python_executable=Path(sys.executable),
        python_version="3.12.10",
        pyinstaller_version="6.21.0",
        upx_enabled=False,
        web_asset_receipt_sha256="8" * 64,
    )

    assert set(receipt) == {
        "schema",
        "source_commit",
        "app_version",
        "platform",
        "architecture",
        "python_version",
        "python_executable",
        "python_executable_sha256",
        "pyinstaller_version",
        "entrypoint_sha256",
        "spec_sha256",
        "python_lock_sha256",
        "web_asset_receipt_sha256",
        "binary_path",
        "binary_size",
        "binary_sha256",
        "upx_enabled",
    }
    assert receipt["binary_sha256"] == _sha256(binary)
    assert receipt["python_lock_sha256"] == _sha256(python_lock)
    assert receipt["upx_enabled"] is False
    assert len(build.canonical_receipt_bytes(receipt)) < 64 * 1024
    with pytest.raises(ValueError, match="UPX"):
        build.create_sidecar_build_receipt(
            source_commit=SOURCE_COMMIT,
            app_version="0.5.0",
            binary_path=binary,
            entrypoint_path=entrypoint,
            spec_path=spec,
            python_lock_path=python_lock,
            python_executable=Path(sys.executable),
            python_version="3.12.10",
            pyinstaller_version="6.21.0",
            upx_enabled=True,
            web_asset_receipt_sha256="8" * 64,
        )


def test_desktop_sbom_is_deterministic_and_lock_bound(tmp_path: Path) -> None:
    sbom = _load_python_script(SBOM_SCRIPT, "task11b_sbom")
    uv_lock = tmp_path / "uv.lock"
    desktop_lock = tmp_path / "desktop-package-lock.json"
    web_lock = tmp_path / "web-package-lock.json"
    uv_lock.write_text(
        'version = 1\n[[package]]\nname = "zeta"\nversion = "2.0"\n'
        '[[package]]\nname = "alpha"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    _write_json(
        desktop_lock,
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "kestrel-desktop", "version": "0.5.0"},
                "node_modules/zod": {"version": "4.4.3"},
            },
        },
    )
    _write_json(
        web_lock,
        {
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "kestrel-web", "version": "0.5.0"},
                "node_modules/react": {"version": "19.2.1"},
            },
        },
    )
    sidecar_receipt = {
        "schema": "kestrel.desktop.sidecar-build.v1",
        "source_commit": SOURCE_COMMIT,
        "app_version": "0.5.0",
        "binary_sha256": "9" * 64,
        "python_lock_sha256": _sha256(uv_lock),
        "web_asset_receipt_sha256": "8" * 64,
    }

    first = sbom.build_desktop_sbom(
        uv_lock=uv_lock,
        desktop_lock=desktop_lock,
        web_lock=web_lock,
        sidecar_receipt=sidecar_receipt,
    )
    second = sbom.build_desktop_sbom(
        uv_lock=uv_lock,
        desktop_lock=desktop_lock,
        web_lock=web_lock,
        sidecar_receipt=sidecar_receipt,
    )

    assert sbom.canonical_sbom_bytes(first) == sbom.canonical_sbom_bytes(second)
    assert "timestamp" not in first["metadata"]
    purls = [component["purl"] for component in first["components"]]
    assert purls == sorted(purls)
    assert "pkg:pypi/alpha@1.0" in purls
    assert "pkg:npm/react@19.2.1" in purls
    with pytest.raises(ValueError, match="python lock digest mismatch"):
        sbom.build_desktop_sbom(
            uv_lock=uv_lock,
            desktop_lock=desktop_lock,
            web_lock=web_lock,
            sidecar_receipt={**sidecar_receipt, "python_lock_sha256": "0" * 64},
        )

    sidecar_receipt_path = tmp_path / "sidecar-receipt.json"
    _write_json(sidecar_receipt_path, sidecar_receipt)
    output_path = tmp_path / "sbom.cdx.json"
    receipt_path = tmp_path / "sbom-receipt.json"
    receipt = sbom.generate_sbom(
        uv_lock=uv_lock,
        desktop_lock=desktop_lock,
        web_lock=web_lock,
        sidecar_receipt_path=sidecar_receipt_path,
        output_path=output_path,
        receipt_path=receipt_path,
    )
    assert set(receipt) == {
        "schema",
        "source_commit",
        "app_version",
        "python_lock_sha256",
        "desktop_npm_lock_sha256",
        "web_npm_lock_sha256",
        "sidecar_binary_sha256",
        "web_asset_receipt_sha256",
        "sbom_path",
        "sbom_size",
        "sbom_sha256",
    }
    assert receipt["python_lock_sha256"] == _sha256(uv_lock)
    assert receipt["desktop_npm_lock_sha256"] == _sha256(desktop_lock)
    assert receipt["web_npm_lock_sha256"] == _sha256(web_lock)
    assert receipt["sidecar_binary_sha256"] == sidecar_receipt["binary_sha256"]
    assert (
        receipt["web_asset_receipt_sha256"]
        == sidecar_receipt["web_asset_receipt_sha256"]
    )


def _stage_fixture(tmp_path: Path) -> dict[str, Any]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    build = _load_python_script(BUILD_SCRIPT, "task11b_stage_receipt")
    sidecar_root = tmp_path / "sidecar"
    web_root = tmp_path / "web"
    desktop_root = tmp_path / "credential"
    notices_root = tmp_path / "notices"
    evidence_root = tmp_path / "evidence"
    for root in (sidecar_root, web_root, desktop_root, notices_root, evidence_root):
        root.mkdir()
    binary = sidecar_root / (
        "kestrel-desktop-sidecar.exe" if os.name == "nt" else "kestrel-desktop-sidecar"
    )
    binary.write_bytes(b"frozen-sidecar")
    (web_root / "assets").mkdir()
    (web_root / "index.html").write_text("<h1>Kestrel</h1>", encoding="utf-8")
    (web_root / "assets/app.js").write_text("export {};\n", encoding="utf-8")
    for name in ("index.html", "form.js", "styles.css", "preload.js"):
        (desktop_root / name).write_text(f"{name}\n", encoding="utf-8")
    (notices_root / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    (notices_root / "THIRD_PARTY_NOTICES.txt").write_text("Notices\n", encoding="utf-8")
    entrypoint = evidence_root / "entry.py"
    spec = evidence_root / "sidecar.spec"
    python_lock = evidence_root / "uv.lock"
    desktop_lock = evidence_root / "desktop-lock.json"
    web_lock = evidence_root / "web-lock.json"
    entrypoint.write_text("entry\n", encoding="utf-8")
    spec.write_text("spec\n", encoding="utf-8")
    python_lock.write_text("lock\n", encoding="utf-8")
    desktop_lock.write_text("desktop-lock\n", encoding="utf-8")
    web_lock.write_text("web-lock\n", encoding="utf-8")
    web_receipt = _asset_receipt(
        web_root,
        kind="web",
        lock_sha256=_sha256(web_lock),
    )
    sidecar_receipt = build.create_sidecar_build_receipt(
        source_commit=SOURCE_COMMIT,
        app_version="0.5.0",
        binary_path=binary,
        entrypoint_path=entrypoint,
        spec_path=spec,
        python_lock_path=python_lock,
        python_executable=Path(sys.executable),
        python_version="3.12.10",
        pyinstaller_version="6.21.0",
        upx_enabled=False,
        web_asset_receipt_sha256=hashlib.sha256(
            _canonical_json_bytes(web_receipt)
        ).hexdigest(),
    )
    sbom_path = evidence_root / "sbom.cdx.json"
    _write_json(
        sbom_path,
        {
            "bomFormat": "CycloneDX",
            "specVersion": "1.6",
            "serialNumber": f"urn:uuid:{'1' * 32}",
            "version": 1,
            "metadata": {"component": {"name": "Kestrel", "version": "0.5.0"}},
            "components": [],
        },
    )
    sbom_receipt = {
        "schema": "kestrel.desktop.sbom.v1",
        "source_commit": SOURCE_COMMIT,
        "app_version": "0.5.0",
        "python_lock_sha256": _sha256(python_lock),
        "desktop_npm_lock_sha256": _sha256(desktop_lock),
        "web_npm_lock_sha256": _sha256(web_lock),
        "sidecar_binary_sha256": sidecar_receipt["binary_sha256"],
        "web_asset_receipt_sha256": sidecar_receipt[
            "web_asset_receipt_sha256"
        ],
        "sbom_path": str(sbom_path.resolve()),
        "sbom_size": sbom_path.stat().st_size,
        "sbom_sha256": _sha256(sbom_path),
    }
    receipts = {
        "sidecar": sidecar_receipt,
        "web": web_receipt,
        "desktop": _asset_receipt(
            desktop_root,
            kind="desktop-credential",
            lock_sha256=_sha256(desktop_lock),
        ),
        "notices": _asset_receipt(notices_root, kind="notices", lock_sha256="0" * 64),
        "sbom": sbom_receipt,
    }
    receipt_paths: dict[str, Path] = {}
    for name, receipt in receipts.items():
        path = evidence_root / f"{name}-receipt.json"
        _write_json(path, receipt)
        receipt_paths[name] = path
    identity = {
        "build_mode": "developer",
        "key_id": "developer",
        "source_commit": SOURCE_COMMIT,
        "app_version": "0.5.0",
        "platform": sys.platform,
        "architecture": (
            "arm64" if platform.machine().lower() in {"arm64", "aarch64"} else "x64"
        ),
        "python_lock_sha256": _sha256(python_lock),
        "desktop_npm_lock_sha256": _sha256(desktop_lock),
        "web_npm_lock_sha256": _sha256(web_lock),
        "sbom_sha256": _sha256(sbom_path),
    }
    identity_path = evidence_root / "identity.json"
    _write_json(identity_path, identity)
    return {
        "receipts": receipt_paths,
        "identity": identity_path,
        "output": tmp_path / "stage",
        "stage_receipt": evidence_root / "stage-receipt.json",
    }


def _stage_command(fixture: dict[str, Any]) -> list[str]:
    receipts = fixture["receipts"]
    return [
        "node",
        str(STAGE_SCRIPT),
        "--python",
        sys.executable,
        "--sidecar-receipt",
        str(receipts["sidecar"]),
        "--web-receipt",
        str(receipts["web"]),
        "--desktop-receipt",
        str(receipts["desktop"]),
        "--notices-receipt",
        str(receipts["notices"]),
        "--sbom-receipt",
        str(receipts["sbom"]),
        "--identity",
        str(fixture["identity"]),
        "--output",
        str(fixture["output"]),
        "--receipt",
        str(fixture["stage_receipt"]),
    ]


def test_stage_consumes_exact_receipts_signs_manifest_and_persists_no_private_key(
    tmp_path: Path,
) -> None:
    assert STAGE_SCRIPT.is_file(), "missing desktop/scripts/stage-resources.mjs"
    fixture = _stage_fixture(tmp_path)

    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    stage = fixture["output"]
    stage_receipt = json.loads(fixture["stage_receipt"].read_text(encoding="utf-8"))
    manifest = json.loads(
        (stage / "kestrel-resource-manifest.json").read_text(encoding="utf-8")
    )
    assert stage_receipt["schema"] == "kestrel.desktop.stage.v1"
    assert stage_receipt["source_commit"] == SOURCE_COMMIT
    assert stage_receipt["resource_root"] == str(stage.resolve())
    assert len(
        json.dumps(stage_receipt, sort_keys=True, separators=(",", ":")).encode()
    ) < 64 * 1024
    expected_payload = {
        "desktop-developer-public-key.pem",
        "desktop/dist/credential/form.js",
        "desktop/dist/credential/index.html",
        "desktop/dist/credential/preload.js",
        "desktop/dist/credential/styles.css",
        "licenses/LICENSE",
        "licenses/THIRD_PARTY_NOTICES.txt",
        "sbom.cdx.json",
        f"sidecar/{'kestrel-desktop-sidecar.exe' if os.name == 'nt' else 'kestrel-desktop-sidecar'}",
        "web/dist/assets/app.js",
        "web/dist/index.html",
    }
    assert set(manifest["files"]) == expected_payload
    assert not list(stage.rglob("*private*"))
    assert not list(stage.rglob("*.key"))
    public_key = load_desktop_public_key(
        stage / "desktop-developer-public-key.pem"
    )
    expected_identity = DesktopManifestIdentity.from_mapping(
        json.loads(fixture["identity"].read_text(encoding="utf-8"))
    )
    verify_developer_resource_manifest(
        stage,
        expected_identity=expected_identity,
        trusted_public_keys={"developer": public_key},
    )
    main_source = (ROOT / "desktop" / "src" / "main.ts").read_text(encoding="utf-8")
    assert 'buildTrust.buildMode === "developer"' in main_source
    public_key_start = main_source.index("const publicKeyBytes")
    key_lookup = main_source[
        public_key_start:
        main_source.index("const publicKey =", public_key_start)
    ]
    assert "app.getAppPath()" in key_lookup
    assert "resourceRoot" not in key_lookup


def test_stage_accepts_python_canonical_integer_and_unicode_asset_keys(
    tmp_path: Path,
) -> None:
    fixture = _stage_fixture(tmp_path)
    web_receipt_path = fixture["receipts"]["web"]
    web_receipt = json.loads(web_receipt_path.read_text(encoding="utf-8"))
    web_root = Path(web_receipt["root"])
    (web_root / "10").write_text("ten\n", encoding="utf-8")
    (web_root / "2").write_text("two\n", encoding="utf-8")
    (web_root / "\ue000").write_text("private-use\n", encoding="utf-8")
    (web_root / "\U00010000").write_text("astral\n", encoding="utf-8")
    updated_web = _asset_receipt(
        web_root,
        kind="web",
        lock_sha256=web_receipt["lock_sha256"],
    )
    _write_json(web_receipt_path, updated_web)
    sidecar_receipt_path = fixture["receipts"]["sidecar"]
    sidecar_receipt = json.loads(
        sidecar_receipt_path.read_text(encoding="utf-8")
    )
    sidecar_receipt["web_asset_receipt_sha256"] = _sha256(web_receipt_path)
    _write_json(sidecar_receipt_path, sidecar_receipt)
    sbom_receipt_path = fixture["receipts"]["sbom"]
    sbom_receipt = json.loads(sbom_receipt_path.read_text(encoding="utf-8"))
    sbom_receipt["web_asset_receipt_sha256"] = _sha256(web_receipt_path)
    _write_json(sbom_receipt_path, sbom_receipt)

    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    manifest = json.loads(
        (fixture["output"] / "kestrel-resource-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert "web/dist/10" in manifest["files"]
    assert "web/dist/2" in manifest["files"]
    assert "web/dist/\ue000" in manifest["files"]
    assert "web/dist/\U00010000" in manifest["files"]


def test_stage_refuses_preexisting_output_and_wrong_host_identity(
    tmp_path: Path,
) -> None:
    fixture = _stage_fixture(tmp_path)
    fixture["output"].mkdir()
    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode != 0
    assert "must not already exist" in completed.stderr

    fixture = _stage_fixture(tmp_path / "wrong-host")
    identity = json.loads(fixture["identity"].read_text(encoding="utf-8"))
    identity["platform"] = "linux" if sys.platform != "linux" else "darwin"
    _write_json(fixture["identity"], identity)
    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode != 0
    assert "staging host" in completed.stderr


def test_stage_rejects_declared_casefold_prefix_collisions(
    tmp_path: Path,
) -> None:
    fixture = _stage_fixture(tmp_path)
    web_receipt_path = fixture["receipts"]["web"]
    web_receipt = json.loads(web_receipt_path.read_text(encoding="utf-8"))
    exemplar = next(iter(web_receipt["files"].values()))
    web_receipt["files"]["Case/item.js"] = exemplar
    web_receipt["files"]["case/other.js"] = exemplar
    _write_json(web_receipt_path, web_receipt)

    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode != 0
    assert "case-colliding" in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX link/special-file construction")
@pytest.mark.parametrize("kind", ["hardlink", "symlink", "special"])
def test_stage_rejects_linked_or_special_asset_payloads(
    tmp_path: Path,
    kind: str,
) -> None:
    fixture = _stage_fixture(tmp_path)
    web_receipt_path = fixture["receipts"]["web"]
    web_receipt = json.loads(web_receipt_path.read_text(encoding="utf-8"))
    web_root = Path(web_receipt["root"])
    source = web_root / "assets" / "app.js"
    candidate = web_root / "assets" / f"{kind}.js"
    if kind == "hardlink":
        os.link(source, candidate)
    elif kind == "symlink":
        candidate.symlink_to(source)
    else:
        os.mkfifo(candidate)
    _write_json(
        web_receipt_path,
        _asset_receipt(
            web_root,
            kind="web",
            lock_sha256=web_receipt["lock_sha256"],
        ),
    )

    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode != 0
    assert (
        "unique regular file" in completed.stderr
        or "forbidden staged payload" in completed.stderr
    )


@pytest.mark.parametrize(
    "forbidden",
    [
        ".env",
        ".env.production",
        ".nest/state.json",
        "credentials.json",
        "tests/test_payload.py",
        "__pycache__/payload.pyc",
        ".cache/data.bin",
        "assets/app.js.map",
        "qrcode/legacy.py",
        "video_frames/legacy.bin",
        "vite.config.js",
    ],
)
def test_stage_rejects_forbidden_payload_roots(
    tmp_path: Path,
    forbidden: str,
) -> None:
    assert STAGE_SCRIPT.is_file(), "missing desktop/scripts/stage-resources.mjs"
    fixture = _stage_fixture(tmp_path)
    web_receipt_path = fixture["receipts"]["web"]
    web_receipt = json.loads(web_receipt_path.read_text(encoding="utf-8"))
    web_root = Path(web_receipt["root"])
    forbidden_path = web_root / forbidden
    forbidden_path.parent.mkdir(parents=True, exist_ok=True)
    forbidden_path.write_text("forbidden\n", encoding="utf-8")
    _write_json(
        web_receipt_path,
        _asset_receipt(
            web_root,
            kind="web",
            lock_sha256=web_receipt["lock_sha256"],
        ),
    )

    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode != 0
    assert "forbidden staged payload" in completed.stderr


def test_stage_requires_complete_notices_and_exact_asset_inventory(
    tmp_path: Path,
) -> None:
    assert STAGE_SCRIPT.is_file(), "missing desktop/scripts/stage-resources.mjs"
    fixture = _stage_fixture(tmp_path)
    notices_receipt_path = fixture["receipts"]["notices"]
    notices_receipt = json.loads(notices_receipt_path.read_text(encoding="utf-8"))
    notices_receipt["files"].pop("LICENSE")
    _write_json(notices_receipt_path, notices_receipt)
    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode != 0
    assert "notice" in completed.stderr.lower()

    fixture = _stage_fixture(tmp_path / "second")
    web_receipt = json.loads(
        fixture["receipts"]["web"].read_text(encoding="utf-8")
    )
    (Path(web_receipt["root"]) / "unlisted.js").write_text(
        "unlisted\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        _stage_command(fixture),
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode != 0
    assert "inventory" in completed.stderr.lower()
