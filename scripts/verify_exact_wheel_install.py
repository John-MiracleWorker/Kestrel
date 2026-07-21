#!/usr/bin/env python3
"""Install and exercise one exact Kestrel release wheel on any supported runner."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_release_payload import DEFAULT_DISTRIBUTION, verify_release_payload

DEFAULT_EXTRAS = "memvid,openai,anthropic,gemini,server,mcp,keyring"


def _venv_python(root: Path) -> Path:
    if os.name == "nt":
        return root / "Scripts" / "python.exe"
    return root / "bin" / "python"


def _run(command: list[str], *, cwd: Path, environment: dict[str, str]) -> None:
    subprocess.run(command, cwd=cwd, env=environment, check=True)


def _create_venv(root: Path) -> Path:
    # uv-managed CPython builds on macOS keep libpython relative to the base
    # executable. Copying that executable into a venv breaks its dylib lookup;
    # POSIX venvs should retain the standard interpreter symlink. Windows uses
    # launchers/copies because creating symlinks may require extra privileges.
    venv.EnvBuilder(with_pip=True, symlinks=os.name != "nt").create(root)
    python = _venv_python(root)
    if not python.is_file():
        raise OSError(f"virtual environment Python was not created: {python}")
    # Keep the venv entrypoint path. Resolving a POSIX symlink here would invoke
    # the base interpreter directly and bypass the environment just created.
    return python.absolute()


def verify_exact_wheel_install(
    payload: Path,
    *,
    expected_version: str,
    source_root: Path,
    work_root: Path,
    extras: str = DEFAULT_EXTRAS,
) -> dict[str, object]:
    report = verify_release_payload(payload, expected_version=expected_version)
    payload = payload.resolve(strict=True)
    source_root = source_root.resolve(strict=True)
    if work_root.exists():
        raise ValueError(f"exact-wheel work root already exists: {work_root}")
    work_root.mkdir(parents=True, mode=0o700)
    venv_root = work_root / "venv"
    smoke_root = work_root / "smoke"
    smoke_root.mkdir()
    python = _create_venv(venv_root)

    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"

    requirements = payload / "requirements-release.txt"
    wheel = payload / str(report["wheel"])
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(requirements),
        ],
        cwd=smoke_root,
        environment=environment,
    )
    _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            f"{wheel}[{extras}]",
        ],
        cwd=smoke_root,
        environment=environment,
    )
    _run(
        [str(python), "-m", "pip", "check"],
        cwd=smoke_root,
        environment=environment,
    )
    _run(
        [
            str(python),
            "-I",
            "-c",
            (
                "import importlib.metadata, keyring, memvid_sdk, nested_memvid_agent, sys; "
                f"actual=importlib.metadata.version({DEFAULT_DISTRIBUTION!r}); "
                "assert actual == sys.argv[1], (actual, sys.argv[1]); "
                "assert callable(keyring.get_keyring); "
                "assert callable(memvid_sdk.create); "
                "assert callable(memvid_sdk.use); "
                "assert nested_memvid_agent.__file__"
            ),
            str(report["version"]),
        ],
        cwd=smoke_root,
        environment=environment,
    )
    _run(
        [
            str(python),
            "-I",
            "-m",
            "nested_memvid_agent.cli",
            "doctor",
            "--backend",
            "memvid",
            "--memory-dir",
            str(smoke_root / "doctor-memory"),
            "--provider",
            "mock",
            "--model",
            "mock",
        ],
        cwd=smoke_root,
        environment=environment,
    )
    _run(
        [
            str(python),
            "-I",
            "-m",
            "nested_memvid_agent.cli",
            "chat",
            "--backend",
            "memvid",
            "--memory-dir",
            str(smoke_root / "chat-memory"),
            "--provider",
            "mock",
            "--model",
            "mock",
            "--message",
            "cross-platform release wheel smoke",
        ],
        cwd=smoke_root,
        environment=environment,
    )
    _run(
        [
            str(python),
            "-I",
            str(source_root / "scripts" / "verify_installed_memvid.py"),
            "--source-root",
            str(source_root),
            "--memory-dir",
            str(smoke_root / "integration-memory"),
        ],
        cwd=smoke_root,
        environment=environment,
    )
    return {
        **report,
        "python": str(python),
        "work_root": str(work_root),
        "installed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("payload", type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--source-root", type=Path, default=Path.cwd())
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--extras", default=DEFAULT_EXTRAS)
    args = parser.parse_args()

    if args.work_root is None:
        parent = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()))
        work_root = Path(tempfile.mkdtemp(prefix="kestrel-exact-wheel-", dir=parent))
        work_root.rmdir()
    else:
        work_root = args.work_root
    try:
        report = verify_exact_wheel_install(
            args.payload,
            expected_version=args.expected_version,
            source_root=args.source_root,
            work_root=work_root,
            extras=args.extras,
        )
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
