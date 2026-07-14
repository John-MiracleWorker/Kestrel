from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
START_TELEGRAM = ROOT / "scripts" / "start-telegram-agent.sh"


def _clean_kestrel_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("NEST_AGENT_")
        and key
        not in {
            "KESTREL_TELEGRAM_RUNTIME",
            "KESTREL_PRINT_ENV",
            "KESTREL_TELEGRAM_ENV",
            "KESTREL_PORT",
        }
    }


def _run_install(*, env: dict[str, str] | None = None, args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    install_env = os.environ.copy()
    install_env.update(env or {})
    return subprocess.run(
        ["bash", str(INSTALL), *(args or [])],
        cwd=ROOT,
        env=install_env,
        text=True,
        capture_output=True,
        check=False,
    )


def _current_tree_git_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        text=False,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    extra_paths = [Path("install.sh"), Path("tests/test_install_script.py")]
    for raw in [*tracked, *(str(path).encode() for path in extra_paths)]:
        if not raw:
            continue
        relative = Path(raw.decode())
        src = ROOT / relative
        if not src.exists() or src.is_dir():
            continue
        dst = source / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Kestrel Installer Test",
            "-c",
            "user.email=kestrel-installer@example.invalid",
            "commit",
            "-q",
            "-m",
            "installer smoke source",
        ],
        cwd=source,
        check=True,
    )
    return source


def test_install_script_is_valid_bash() -> None:
    assert INSTALL.exists()

    result = subprocess.run(
        ["bash", "-n", str(INSTALL)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_start_telegram_agent_defaults_to_shared_memvid_runtime(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.telegram"
    env_file.write_text("# test env intentionally has no runtime overrides\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(START_TELEGRAM)],
        cwd=ROOT,
        env={
            **_clean_kestrel_env(),
            "KESTREL_TELEGRAM_ENV": str(env_file),
            "KESTREL_PRINT_ENV": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "KESTREL_TELEGRAM_RUNTIME=shared" in result.stdout
    assert "NEST_AGENT_BACKEND=memvid" in result.stdout
    assert "NEST_AGENT_MEMORY_DIR=.nest/memory" in result.stdout
    assert "NEST_AGENT_LOG_DIR=.nest/logs" in result.stdout
    assert "NEST_AGENT_STATE_PATH=.nest/state/agent.db" in result.stdout
    assert "NEST_AGENT_SECRET_STORE_PATH=.nest/secrets/local_vault.json" in result.stdout
    assert "NEST_AGENT_TRUSTED_HOSTS=127.0.0.1,localhost,::1,[::1],testserver,*.trycloudflare.com" in result.stdout


def test_start_telegram_agent_can_use_isolated_memvid_runtime(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.telegram"
    env_file.write_text("KESTREL_TELEGRAM_RUNTIME=isolated\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(START_TELEGRAM)],
        cwd=ROOT,
        env={
            **_clean_kestrel_env(),
            "KESTREL_TELEGRAM_ENV": str(env_file),
            "KESTREL_PRINT_ENV": "1",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "KESTREL_TELEGRAM_RUNTIME=isolated" in result.stdout
    assert "NEST_AGENT_BACKEND=memvid" in result.stdout
    assert "NEST_AGENT_MEMORY_DIR=.nest/telegram/memory" in result.stdout
    assert "NEST_AGENT_LOG_DIR=.nest/telegram/logs" in result.stdout
    assert "NEST_AGENT_STATE_PATH=.nest/telegram/state/agent.db" in result.stdout
    assert "NEST_AGENT_SECRET_STORE_PATH=.nest/telegram/secrets/local_vault.json" in result.stdout


def test_install_help_documents_github_curl_and_options() -> None:
    result = _run_install(args=["--help"])

    assert result.returncode == 0
    assert "curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/main/install.sh | bash" in result.stdout
    for option in [
        "KESTREL_HOME",
        "KESTREL_REPO",
        "KESTREL_REF",
        "KESTREL_PYTHON",
        "KESTREL_EXTRAS",
        "KESTREL_SKIP_WEB",
        "KESTREL_SKIP_SMOKE",
        "KESTREL_START_SERVER",
        "KESTREL_OPEN_BROWSER",
        "KESTREL_SERVER_SESSION",
        "KESTREL_SERVER_LOG",
        "KESTREL_SERVER_PID",
        "KESTREL_PORT",
        "KESTREL_DRY_RUN",
    ]:
        assert option in result.stdout


def test_install_defaults_exclude_development_dependencies() -> None:
    result = _run_install(args=["--help"])

    assert result.returncode == 0
    assert "Defaults to memvid,openai,server,mcp." in result.stdout
    assert "memvid,openai,server,mcp,dev" not in result.stdout


def test_install_dry_run_uses_memvid_mock_defaults(tmp_path: Path) -> None:
    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(tmp_path / "kestrel-home"),
        }
    )

    assert result.returncode == 0, result.stderr
    assert "DRY RUN" in result.stdout
    assert "https://github.com/John-MiracleWorker/Kestrel.git" in result.stdout
    assert ".[memvid,openai,server,mcp]" in result.stdout
    assert "nest-agent init --backend memvid --memory-dir .nest/memory" in result.stdout
    assert "nest-agent memory verify --backend memvid --memory-dir .nest/memory" in result.stdout
    assert 'nest-agent chat --backend memory --memory-dir .nest/install-smoke-memory --provider mock --model mock --message "hello from one-shot install"' in result.stdout
    assert "server auto-start: disabled" in result.stdout
    assert "browser open: disabled" in result.stdout
    assert "server pid:" in result.stdout
    assert ".nest/server.pid" in result.stdout
    assert "health check: skipped" in result.stdout
    assert "web UI: skipped" in result.stdout
    assert "launch command: skipped" in result.stdout
    assert "NEST_AGENT_ALLOW_SHELL=false" in result.stdout
    assert "NEST_AGENT_ALLOW_POLICY_WRITES=false" in result.stdout
    assert "NEST_AGENT_ALLOW_PLUGIN_INSTALL=false" in result.stdout


def test_install_dry_run_can_disable_server_autostart(tmp_path: Path) -> None:
    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_START_SERVER": "0",
            "KESTREL_HOME": str(tmp_path / "kestrel-home"),
        }
    )

    assert result.returncode == 0, result.stderr
    assert "server auto-start: disabled" in result.stdout
    assert "browser open: disabled" in result.stdout
    assert "health check: skipped" in result.stdout


def test_install_refuses_non_git_nonempty_target_even_in_dry_run(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("do not overwrite\n", encoding="utf-8")

    result = _run_install(env={"KESTREL_DRY_RUN": "1", "KESTREL_HOME": str(target)})

    assert result.returncode != 0
    assert "Refusing to install into non-git nonempty directory" in result.stderr


def test_install_script_detects_python_311_without_bare_python_default() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "KESTREL_PYTHON" in text
    for candidate in ["python3.13", "python3.12", "python3.11", "/opt/homebrew/bin/python3.11"]:
        assert candidate in text
    assert '"$PYTHON_BIN" -m venv .venv' in text
    assert "python -m venv" not in text


def test_install_script_launches_server_detached_and_checks_health() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "start_server_detached" in text
    assert "wait_for_server" in text
    assert "/api/health" in text
    assert "KESTREL_SERVER_PID" in text
    assert "child=$!" in text
    assert "screen -dmS" in text
    assert "nohup" in text
    assert "run .venv/bin/nest-agent server --backend memvid" not in text


@pytest.mark.skipif(
    os.getenv("RUN_MEMVID_INTEGRATION") != "1" or os.getenv("RUN_INSTALLER_INTEGRATION") != "1",
    reason="requires RUN_MEMVID_INTEGRATION=1 and RUN_INSTALLER_INTEGRATION=1",
)
def test_install_from_local_repo_smoke_with_memvid(tmp_path: Path) -> None:
    source_repo = _current_tree_git_repo(tmp_path)
    result = _run_install(
        env={
            "KESTREL_HOME": str(tmp_path / "installed"),
            "KESTREL_REPO": str(source_repo),
            "KESTREL_REF": "HEAD",
            "KESTREL_SKIP_WEB": "1",
            "KESTREL_START_SERVER": "0",
        }
    )

    assert result.returncode == 0, result.stderr
    assert "Kestrel install complete" in result.stdout
