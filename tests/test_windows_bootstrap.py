from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "install.ps1"


def _powershell() -> str | None:
    return shutil.which("pwsh") or shutil.which("powershell")


def test_windows_bootstrap_is_shipped_at_the_repository_root() -> None:
    assert BOOTSTRAP.is_file()


def test_windows_doctor_source_binds_and_validates_each_supported_path() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")

    assert "struct.calcsize" in source
    assert '"pip", "--version"' in source
    assert '"--distribution", $wslDistribution' in source
    assert '"--exec", "sh", "-c"' in source
    assert '"--exec", "sh", "-lc"' not in source
    assert "guest_python_supported" in source
    assert "guest_python_64_bit" in source
    assert "guest_pip" in source
    assert "guest_bash" in source
    assert "guest_curl" in source
    assert '"--exec", "bash", "--version"' in source
    assert '"--exec", "curl", "--version"' in source
    assert "distribution_architecture" in source
    assert '"context", "show"' in source
    assert "desktop-linux" in source
    assert ".Endpoints.docker.Host" in source
    assert "dockerDesktopLinuxEngine" in source
    assert ".DockerRootDir" in source
    assert ".OSType" in source
    assert ".Architecture" in source
    assert "linux_engine" in source
    assert "local_desktop_context" in source
    assert '"bash",' in source
    assert '"curl"' in source


def test_native_bootstrap_discloses_version_pinned_index_install_assurance() -> None:
    source = BOOTSTRAP.read_text(encoding="utf-8")

    assert "version_pinned_package_index" in source
    assert "not hash-bound" in source
    assert "Published exact wheel" not in source
    assert "$runningOnWindows =" in source
    assert "$isWindows =" not in source


@pytest.mark.skipif(
    sys.platform != "win32" or _powershell() is None,
    reason="native Windows PowerShell is not available",
)
def test_windows_doctor_reports_supported_paths_without_mutation() -> None:
    completed = subprocess.run(
        [
            str(_powershell()),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BOOTSTRAP),
            "-Action",
            "Doctor",
            "-Json",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.stdout.strip(), completed.stderr
    report = json.loads(completed.stdout)

    assert report["schema"] == "kestrel.windows_bootstrap_report.v1"
    assert report["mutation_performed"] is False
    assert set(report["checks"]) == {"docker_desktop", "git", "python", "wsl2"}
    assert set(report["paths"]) == {"docker_desktop", "native_wheel", "wsl2"}
    assert report["bootstrap"]["commands"] == []
    assert report["paths"]["native_wheel"]["install_assurance"] == "version_pinned_package_index"
    assert completed.returncode == (0 if report["passed"] else 1)


@pytest.mark.skipif(
    sys.platform != "win32" or _powershell() is None,
    reason="native Windows PowerShell is not available",
)
def test_windows_bootstrap_only_prints_operator_executed_commands() -> None:
    before = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    completed = subprocess.run(
        [
            str(_powershell()),
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(BOOTSTRAP),
            "-Action",
            "Bootstrap",
            "-Path",
            "Auto",
            "-Json",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    after = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=ROOT,
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    assert completed.stdout.strip(), completed.stderr
    report = json.loads(completed.stdout)

    assert report["mutation_performed"] is False
    assert report["bootstrap"]["operator_execution_required"] is True
    assert report["bootstrap"]["commands"]
    assert before == after
    assert completed.returncode == (0 if report["passed"] else 1)
