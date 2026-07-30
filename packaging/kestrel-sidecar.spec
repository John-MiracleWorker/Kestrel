# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller recipe for the fully bundled Kestrel desktop sidecar."""

from pathlib import Path

from PyInstaller.utils.hooks import (
    collect_all,
    collect_data_files,
    collect_submodules,
    copy_metadata,
)


project_root = Path(SPECPATH).resolve().parent
source_root = project_root / "src"
web_dist = project_root / "web" / "dist"
# Source payload root: web/dist (staged under nested_memvid_agent/web_dist).

memvid_datas, memvid_binaries, memvid_hiddenimports = collect_all("memvid_sdk")
tzdata_datas = collect_data_files("tzdata")
agent_datas = collect_data_files(
    "nested_memvid_agent",
    includes=["prompts/*.md"],
)
agent_metadata = copy_metadata("nested-memvid-agent")


def _exclude_mcp_cli(module_name):
    return module_name != "mcp.cli" and not module_name.startswith("mcp.cli.")


def _exclude_test_modules(module_name):
    components = module_name.split(".")
    return not any(
        component in {"test", "tests", "benchmark", "benchmarks"}
        or component.startswith("test_")
        or component.startswith("_test_")
        for component in components
    )


def _include_runtime_data(toc_entry):
    destination = str(toc_entry[0]).replace("\\", "/")
    components = destination.split("/")
    return not (
        destination.endswith(".map")
        or any(
            component in {
                ".cache",
                ".nest",
                "__pycache__",
                "benchmark",
                "benchmarks",
                "credentials.json",
                "qrcode",
                "test",
                "tests",
                "video_frames",
            }
            or component == ".env"
            or component.startswith(".env.")
            for component in components
        )
    )


web_datas = [
    (
        str(path),
        (
            "nested_memvid_agent/web_dist"
            if path.relative_to(web_dist).parent.as_posix() == "."
            else (
                "nested_memvid_agent/web_dist/"
                f"{path.relative_to(web_dist).parent.as_posix()}"
            )
        ),
    )
    for path in sorted(web_dist.rglob("*"))
    if path.is_file() and not path.is_symlink()
]
hiddenimports = sorted(
    {
        *collect_submodules("anthropic"),
        *collect_submodules("google.genai", filter=_exclude_test_modules),
        *collect_submodules("mcp", filter=_exclude_mcp_cli),
        *collect_submodules("nested_memvid_agent"),
        *collect_submodules("openai"),
        *collect_submodules("starlette"),
        *collect_submodules("yaml"),
        *collect_submodules("keyring.backends"),
        *memvid_hiddenimports,
        "fastapi",
        "nested_memvid_agent.llm.mock",
        "nested_memvid_agent.server",
        "pydantic",
        "pydantic_settings",
        "uvicorn",
    }
)
datas = [
    *agent_datas,
    *agent_metadata,
    *memvid_datas,
    *tzdata_datas,
    *web_datas,
    (str(project_root / "LICENSE"), "licenses"),
    (
        str(project_root / "web" / "public" / "THIRD_PARTY_NOTICES.txt"),
        "licenses",
    ),
]
excludes = [
    ".env",
    ".nest",
    "_pytest",
    "benchmark",
    "google.genai._test_api_client",
    "google.genai.tests",
    "jsonschema.benchmarks",
    "mcp.cli",
    "pytest",
    "qrcode",
    "tests",
]

analysis = Analysis(
    [str(project_root / "packaging" / "kestrel-sidecar-entry.py")],
    pathex=[str(source_root)],
    binaries=memvid_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
analysis.datas = [entry for entry in analysis.datas if _include_runtime_data(entry)]
pyz = PYZ(analysis.pure)

# UPX is disabled in the spec because PyInstaller rejects makespec-only switches
# when it receives a pre-existing spec file.
executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="kestrel-desktop-sidecar",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
