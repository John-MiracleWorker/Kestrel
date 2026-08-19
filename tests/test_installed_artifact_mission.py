from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.run_installed_artifact_mission import (
    REPORT_SCHEMA,
    entry_point,
    free_port,
)


def test_free_port_returns_an_actually_free_port() -> None:
    port = free_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))
    assert 0 < port < 65536


def test_entry_point_path_per_platform(tmp_path: Path) -> None:
    assert entry_point(tmp_path, os_name="posix") == tmp_path / "bin" / "nest-agent"
    assert (
        entry_point(tmp_path, os_name="nt")
        == tmp_path / "Scripts" / "nest-agent.exe"
    )


def test_report_schema_constant_is_versioned() -> None:
    assert REPORT_SCHEMA == "kestrel.installed_artifact_mission.v1"


def test_readiness_probe_retries_bounded_window_before_failing() -> None:
    from scripts.run_installed_artifact_mission import _await_readiness

    port = free_port()
    started = time.monotonic()
    with pytest.raises(ValueError, match="did not accept readiness probes"):
        _await_readiness(
            f"http://127.0.0.1:{port}",
            auth_headers={"Authorization": "Bearer test"},
            deadline_seconds=2.0,
        )
    assert time.monotonic() - started < 15


def test_clean_exit_code_accepts_signal_death_on_posix_only() -> None:
    import signal as signal_module

    from scripts.run_installed_artifact_mission import _accepted_exit_code

    assert _accepted_exit_code(0, os_name="posix") is True
    assert _accepted_exit_code(-signal_module.SIGTERM, os_name="posix") is True
    assert _accepted_exit_code(1, os_name="posix") is False
    assert _accepted_exit_code(-signal_module.SIGKILL, os_name="posix") is False
    assert _accepted_exit_code(1, os_name="nt") is True


@pytest.mark.skipif(
    os.environ.get("RUN_INSTALLED_ARTIFACT_MISSION") != "1",
    reason="integration mission exercise is opt-in via RUN_INSTALLED_ARTIFACT_MISSION=1",
)
def test_installed_artifact_mission_end_to_end(tmp_path: Path) -> None:
    """Build a real release payload, install it, start the installed entry
    point, await readiness, run one mock mission, and verify cleanup.

    Requires uv (pinned in the repo) and network access for the hash-locked
    dependency set; used for owner-local REL-005 receipt production, not as a
    routine gate.
    """
    import tomllib

    from scripts.run_installed_artifact_mission import run_installed_artifact_mission
    from scripts.verify_release_payload import DEFAULT_DISTRIBUTION

    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    version = str(project["project"]["version"])
    assert DEFAULT_DISTRIBUTION == "nested-memvid-agent"

    payload = tmp_path / "payload"
    payload.mkdir()
    extras = "memvid,openai,anthropic,gemini,server,mcp,keyring"
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["PYTHONNOUSERSITE"] = "1"
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-local",
            "--format",
            "requirements.txt",
            "--output-file",
            str(payload / "requirements-release.txt"),
            "--extra",
            "memvid",
            "--extra",
            "openai",
            "--extra",
            "anthropic",
            "--extra",
            "gemini",
            "--extra",
            "server",
            "--extra",
            "mcp",
            "--extra",
            "keyring",
        ],
        cwd=root,
        env=environment,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "build",
            "--no-isolation",
            "--wheel",
            "--sdist",
            "--outdir",
            str(payload),
        ],
        cwd=root,
        env={**environment, "SOURCE_DATE_EPOCH": "1", "PYTHONHASHSEED": "0"},
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.verify_release_payload",
            str(payload),
            "--expected-version",
            version,
        ],
        cwd=root,
        env=environment,
        check=True,
    )

    report = run_installed_artifact_mission(
        payload=payload,
        expected_version=version,
        source_root=root,
        work_root=tmp_path / "mission",
        extras=extras,
    )

    assert report["schema"] == REPORT_SCHEMA
    assert report["passed"] is True
    assert report["readiness"]["unauthenticated_status"] == 401
    assert report["mission"]["terminal_status"] == "completed"
    assert report["cleanup"]["port_released"] is True
    assert report["cleanup"]["lock_released"] is True


def test_installed_artifact_mission_cli_help_works() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.run_installed_artifact_mission", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--expected-version" in completed.stdout
    assert "--work-root" in completed.stdout
