from __future__ import annotations

import hashlib
import os
import runpy
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nested_memvid_agent.runtime_ownership import PrimaryRuntimeOwnership, RuntimeOwnershipError

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
INSTALLER_SERVER_SUPERVISOR = ROOT / "scripts" / "installer-server-supervisor.sh"
START_TELEGRAM = ROOT / "scripts" / "start-telegram-agent.sh"
START_TELEGRAM_STACK = ROOT / "scripts" / "start-telegram-stack.sh"
SET_TELEGRAM_WEBHOOK = ROOT / "scripts" / "set-telegram-webhook.sh"
TELEGRAM_POLLER = ROOT / "scripts" / "telegram-poller.py"
POSIX_SHELL_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="installer and operational shell scripts require macOS, Linux, or WSL",
)
FIXTURE_PACKAGE_ROOTS = (Path("src/nested_memvid_agent"), Path("web/public"))
FIXTURE_EXCLUDED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".nest",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "tiuni-fun",
}
FIXTURE_SECRET_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
TEST_RELEASE_SHA = "a" * 40


def _fixture_source_allowed(relative: Path) -> bool:
    if relative.is_absolute() or ".." in relative.parts:
        return False
    if any(part in FIXTURE_EXCLUDED_DIRECTORIES for part in relative.parts):
        return False
    if relative.name in {".coverage", ".env", ".env.telegram", "local_vault.json"}:
        return False
    if relative.name.startswith(".env.") and not relative.name.endswith(".example"):
        return False
    return relative.suffix.lower() not in FIXTURE_SECRET_SUFFIXES


def _under_fixture_package_root(relative: Path) -> bool:
    return any(relative == root or root in relative.parents for root in FIXTURE_PACKAGE_ROOTS)


def _clean_kestrel_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("NEST_AGENT_")
        and key
        not in {
            "KESTREL_TELEGRAM_RUNTIME",
            "KESTREL_TELEGRAM_HEALTH_PATH",
            "KESTREL_PRINT_ENV",
            "KESTREL_TELEGRAM_ENV",
            "KESTREL_PORT",
            "PUBLIC_URL",
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_WEBHOOK_SECRET",
        }
    }


def _run_install(
    *,
    env: dict[str, str] | None = None,
    args: list[str] | None = None,
    install: Path = INSTALL,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    install_env = os.environ.copy()
    install_env.update(env or {})
    return subprocess.run(
        ["bash", str(install), *(args or [])],
        cwd=cwd,
        env=install_env,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_install_stdin(
    *,
    env: dict[str, str] | None = None,
    install: Path = INSTALL,
    cwd: Path = ROOT,
) -> subprocess.CompletedProcess[str]:
    install_env = os.environ.copy()
    install_env.update(env or {})
    return subprocess.run(
        ["bash"],
        cwd=cwd,
        env=install_env,
        input=install.read_text(encoding="utf-8"),
        text=True,
        capture_output=True,
        check=False,
    )


def _run_installer_function(
    function_call: str,
    *,
    home: Path,
    pid_file: Path,
    port: int,
    dry_run: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    configured_state = Path((extra_env or {}).get("NEST_AGENT_STATE_PATH", ".nest/state/agent.db"))
    effective_state = (
        configured_state if configured_state.is_absolute() else home / configured_state
    )
    env = {
        **_clean_kestrel_env(),
        "KESTREL_HOME": str(home),
        "KESTREL_STATE_PATH": str(effective_state),
        "KESTREL_SERVER_PID": str(pid_file),
        "KESTREL_SERVER_SUPERVISOR_PID": str(pid_file.with_name("server.supervisor.pid")),
        "KESTREL_SERVER_PROCESS_GROUP": str(pid_file.with_name("server.pgid")),
        "KESTREL_SERVER_LOG": str(pid_file.with_name("server.log")),
        "KESTREL_SERVER_SESSION": "kestrel-installer-test",
        "KESTREL_PORT": str(port),
        "KESTREL_START_SERVER": "1",
        "KESTREL_DRY_RUN": "1" if dry_run else "0",
        "PYTHON_BIN": sys.executable,
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["bash", "-c", 'source "$1"; eval "$2"', "bash", str(INSTALL), function_call],
        cwd=home,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _stage_release_installer(
    destination: Path,
    *,
    release_base: str,
    release_sha: str,
    version: str,
    repository: str = "https://github.com/John-MiracleWorker/Kestrel.git",
    ref: str | None = None,
) -> Path:
    text = INSTALL.read_text(encoding="utf-8")
    release_ref = ref or f"v{version}"
    replacements = {
        'DEFAULT_REPO="https://github.com/John-MiracleWorker/Kestrel.git"': (
            f'DEFAULT_REPO="{repository}"'
        ),
        'KESTREL_REF="${KESTREL_REF:-main}"': (f'KESTREL_REF="${{KESTREL_REF:-{release_ref}}}"'),
        'DEFAULT_REQUIREMENTS_URL=""': (
            f'DEFAULT_REQUIREMENTS_URL="{release_base}/requirements-release.txt"'
        ),
        'DEFAULT_WHEEL_URL=""': (
            f'DEFAULT_WHEEL_URL="{release_base}/nested_memvid_agent-{version}-py3-none-any.whl"'
        ),
        'DEFAULT_CHECKSUMS_URL=""': (f'DEFAULT_CHECKSUMS_URL="{release_base}/SHA256SUMS"'),
        'DEFAULT_RELEASE_SHA=""': f'DEFAULT_RELEASE_SHA="{release_sha}"',
        'DEFAULT_RELEASE_VERSION=""': f'DEFAULT_RELEASE_VERSION="{version}"',
    }
    for marker, replacement in replacements.items():
        assert text.count(marker) == 1
        text = text.replace(marker, replacement, 1)
    destination.write_text(text, encoding="utf-8")
    return destination


def _unused_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _write_private_pid_file(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="ascii")
    path.chmod(0o600)


def _write_sqlite_marker(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO marker (value) VALUES (?)", (value,))
    path.chmod(0o600)


def _stage_sqlite_candidate(state_path: Path, value: str) -> tuple[Path, Path]:
    staging_root = state_path.parent / f".{state_path.name}.install-state.test"
    staging_root.mkdir(mode=0o700)
    candidate = staging_root / "candidate.db"
    _write_sqlite_marker(candidate, value)
    return staging_root, candidate


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _wait_for_file(path: Path, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {path}")


def _current_tree_git_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    tracked = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"],
        text=False,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    untracked_runtime = subprocess.run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "-z",
            "--others",
            "--exclude-standard",
            "--",
            "src/nested_memvid_agent",
            "web/public",
        ],
        text=False,
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    extra_paths = [
        Path("config/python-build-bootstrap.txt"),
        Path("config/release-build-bootstrap.txt"),
        Path("install.sh"),
        Path("scripts/installer-server-supervisor.sh"),
        Path("tests/test_install_script.py"),
    ]
    candidate_paths = {
        Path(raw.decode())
        for raw in [*tracked, *untracked_runtime, *(str(path).encode() for path in extra_paths)]
        if raw
    }
    selected_paths = {relative for relative in candidate_paths if _fixture_source_allowed(relative)}
    for relative in selected_paths:
        src = ROOT / relative
        if not src.exists() or src.is_dir():
            continue
        dst = source / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    intended_package_manifest = {
        relative
        for relative in selected_paths
        if _under_fixture_package_root(relative) and (ROOT / relative).is_file()
    }
    copied_package_manifest = {
        path.relative_to(source)
        for package_root in FIXTURE_PACKAGE_ROOTS
        if (source / package_root).is_dir()
        for path in (source / package_root).rglob("*")
        if path.is_file()
    }
    assert copied_package_manifest == intended_package_manifest, (
        "installer fixture package manifest mismatch: "
        f"missing={sorted(intended_package_manifest - copied_package_manifest)!r}, "
        f"unexpected={sorted(copied_package_manifest - intended_package_manifest)!r}"
    )
    copied_files = {path.relative_to(source) for path in source.rglob("*") if path.is_file()}
    assert not (source / "tiuni-fun").exists()
    assert all(_fixture_source_allowed(relative) for relative in copied_files)
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.name", "Kestrel Installer Test"],
        cwd=source,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "kestrel-installer@example.invalid"],
        cwd=source,
        check=True,
    )
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


def _installed_kestrel_checkout(tmp_path: Path) -> Path:
    target = tmp_path / "installed"
    target.mkdir()
    (target / "pyproject.toml").write_text(
        '[project]\nname = "nested-memvid-agent"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    (target / "operator.txt").write_text("operator data\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "add", "."], cwd=target, check=True)
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
            "installed checkout",
        ],
        cwd=target,
        check=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", "https://example.invalid/operator-fork.git"],
        cwd=target,
        check=True,
    )
    return target


@POSIX_SHELL_ONLY
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


@POSIX_SHELL_ONLY
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
    assert "NEST_AGENT_TRUSTED_HOSTS=127.0.0.1,localhost,::1,[::1],testserver" in result.stdout
    assert "NEST_AGENT_REQUIRE_API_AUTH=true" in result.stdout
    assert "NEST_AGENT_API_AUTH_TOKEN_ENV=NEST_AGENT_API_TOKEN" in result.stdout
    assert "*.trycloudflare.com" not in result.stdout


@POSIX_SHELL_ONLY
def test_start_telegram_agent_trusts_only_the_configured_public_host(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.telegram"
    env_file.write_text(
        "PUBLIC_URL=https://assigned-tunnel.trycloudflare.com\n",
        encoding="utf-8",
    )

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
    assert (
        "NEST_AGENT_TRUSTED_HOSTS="
        "127.0.0.1,localhost,::1,[::1],testserver,assigned-tunnel.trycloudflare.com"
    ) in result.stdout
    assert "*.trycloudflare.com" not in result.stdout


@POSIX_SHELL_ONLY
def test_start_telegram_agent_requires_control_plane_token_by_default(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.telegram"
    env_file.write_text("# token intentionally absent\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(START_TELEGRAM)],
        cwd=ROOT,
        env={**_clean_kestrel_env(), "KESTREL_TELEGRAM_ENV": str(env_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Missing API auth token env: NEST_AGENT_API_TOKEN" in result.stderr


@POSIX_SHELL_ONLY
def test_public_telegram_webhook_setup_refuses_disabled_api_auth(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.telegram"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=123:test\n"
        "TELEGRAM_WEBHOOK_SECRET=webhook-secret\n"
        "PUBLIC_URL=https://assigned-tunnel.trycloudflare.com\n"
        "NEST_AGENT_REQUIRE_API_AUTH=false\n"
        "NEST_AGENT_API_TOKEN=control-plane-token\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash", str(SET_TELEGRAM_WEBHOOK)],
        cwd=ROOT,
        env={**_clean_kestrel_env(), "KESTREL_TELEGRAM_ENV": str(env_file)},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "requires NEST_AGENT_REQUIRE_API_AUTH=true" in result.stderr


def test_public_telegram_webhook_subscribes_to_callback_queries() -> None:
    text = SET_TELEGRAM_WEBHOOK.read_text(encoding="utf-8")

    assert (
        '--data-urlencode "allowed_updates=[\\"message\\",\\"edited_message\\",'
        '\\"callback_query\\"]"'
    ) in text


def test_telegram_poller_authenticates_local_control_plane_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    def fake_urlopen(request: object, timeout: int) -> FakeResponse:
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setenv("NEST_AGENT_API_TOKEN", "control-plane-token")
    monkeypatch.setattr("signal.signal", lambda *args: None)
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    namespace = runpy.run_path(str(TELEGRAM_POLLER), run_name="telegram_poller_test")

    namespace["post_json"]("http://127.0.0.1:8765/api/channels/ingest", {"ok": True})

    assert captured["authorization"] == "Bearer control-plane-token"


def test_telegram_poller_default_health_path_follows_the_instance_state_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "isolated" / "state" / "agent.db"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:test")
    monkeypatch.setenv("NEST_AGENT_STATE_PATH", str(state_path))
    monkeypatch.delenv("KESTREL_TELEGRAM_HEALTH_PATH", raising=False)
    monkeypatch.setattr("signal.signal", lambda *args: None)

    namespace = runpy.run_path(str(TELEGRAM_POLLER), run_name="telegram_poller_path_test")

    assert namespace["HEALTH_PATH"] == state_path.parent / "telegram-poller-health.json"


@POSIX_SHELL_ONLY
def test_start_telegram_agent_can_use_isolated_memvid_runtime(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.telegram"
    env_file.write_text(
        "KESTREL_TELEGRAM_RUNTIME=isolated\n"
        "NEST_AGENT_PROVIDER=codex-cli\n"
        "NEST_AGENT_MODEL=gpt-5.5\n"
        "NEST_AGENT_API_KEY_ENV=\n",
        encoding="utf-8",
    )

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
    assert "NEST_AGENT_PROVIDER=codex-cli" in result.stdout
    assert "NEST_AGENT_MODEL=gpt-5.5" in result.stdout
    assert "NEST_AGENT_API_KEY_ENV=\n" in result.stdout


@POSIX_SHELL_ONLY
def test_telegram_stack_uses_isolated_runtime_and_bounded_logs() -> None:
    text = START_TELEGRAM_STACK.read_text(encoding="utf-8")
    result = subprocess.run(
        ["bash", "-n", str(START_TELEGRAM_STACK)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "umask 077" in text
    assert 'chmod 700 "$LOG_DIR"' in text
    assert 'export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"' in text
    assert 'PYTHON_BIN=".venv/bin/python"' in text
    assert '"Authorization": f"Bearer {token}"' in text
    assert '-H "Authorization: Bearer $api_token"' not in text
    assert 'KESTREL_REPLACE_EXISTING="${KESTREL_REPLACE_EXISTING:-false}"' in text
    assert 'KESTREL_SERVER_ACCESS_LOG="${KESTREL_SERVER_ACCESS_LOG:-false}"' in text
    assert '"$LOG_MAX_BYTES" =~ ^[0-9]+$' in text
    assert 'rotate_log "$SERVER_LOG"' in text
    assert 'rotate_log "$POLLER_LOG"' in text
    assert "export KESTREL_TELEGRAM_HEALTH_PATH=" in text


@POSIX_SHELL_ONLY
def test_install_help_documents_github_curl_and_options() -> None:
    result = _run_install(args=["--help"])

    assert result.returncode == 0
    assert (
        "curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/main/install.sh | bash"
        in result.stdout
    )
    for option in [
        "KESTREL_HOME",
        "KESTREL_REPO",
        "KESTREL_REF",
        "KESTREL_PYTHON",
        "KESTREL_EXTRAS",
        "KESTREL_REQUIREMENTS_URL",
        "KESTREL_WHEEL_URL",
        "KESTREL_CHECKSUMS_URL",
        "KESTREL_SKIP_WEB",
        "KESTREL_SKIP_SMOKE",
        "KESTREL_START_SERVER",
        "KESTREL_OPEN_BROWSER",
        "KESTREL_SERVER_SESSION",
        "KESTREL_SERVER_LOG",
        "KESTREL_SERVER_PID",
        "KESTREL_SERVER_SUPERVISOR_PID",
        "KESTREL_SERVER_PROCESS_GROUP",
        "KESTREL_PORT",
        "KESTREL_DRY_RUN",
    ]:
        assert option in result.stdout


@POSIX_SHELL_ONLY
def test_install_defaults_exclude_development_dependencies() -> None:
    result = _run_install(args=["--help"])

    assert result.returncode == 0
    assert "Defaults to memvid,openai,anthropic,gemini,server,mcp,keyring." in result.stdout
    assert "memvid,openai,anthropic,gemini,server,mcp,keyring,dev" not in result.stdout


@POSIX_SHELL_ONLY
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
    assert ".[memvid,openai,anthropic,gemini,server,mcp,keyring]" in result.stdout
    assert "nest-agent init --backend memvid --memory-dir .nest/memory" in result.stdout
    assert "nest-agent memory verify --backend memvid --memory-dir .nest/memory" in result.stdout
    assert ".nest/.install-canary/chat-memory" in result.stdout
    assert "--state-path" in result.stdout
    assert 'mock --message "hello from one-shot install"' in result.stdout
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


@POSIX_SHELL_ONLY
@pytest.mark.parametrize("absolute", [False, True])
def test_install_resolves_the_runtime_state_path_from_kestrel_home(
    tmp_path: Path, absolute: bool
) -> None:
    home = tmp_path / "kestrel-home"
    configured = (
        tmp_path / "external-state" / "agent.db"
        if absolute
        else Path(".nest/custom-state/agent.db")
    )
    if absolute:
        configured.parent.mkdir(mode=0o700)

    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(home),
            "NEST_AGENT_STATE_PATH": str(configured),
        }
    )

    expected = configured if absolute else home / configured
    assert result.returncode == 0, result.stderr
    assert f"state: {expected}" in result.stdout
    assert f'--state-path "{expected}"' in result.stdout


@POSIX_SHELL_ONLY
def test_documented_stdin_installer_path_executes_main(tmp_path: Path) -> None:
    result = _run_install_stdin(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(tmp_path / "kestrel-home"),
        }
    )

    assert result.returncode == 0, result.stderr
    assert "[kestrel-install] Install plan:" in result.stdout
    assert "DRY RUN" in result.stdout


@POSIX_SHELL_ONLY
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


@POSIX_SHELL_ONLY
def test_install_dry_run_does_not_require_simulated_web_build_output(tmp_path: Path) -> None:
    source = _current_tree_git_repo(tmp_path)

    assert not (source / "web" / "dist" / "index.html").exists()
    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(tmp_path / "kestrel-home"),
        },
        install=source / "install.sh",
        cwd=source,
    )

    assert result.returncode == 0, result.stderr
    assert "npm run build --prefix web" in result.stdout


@POSIX_SHELL_ONLY
def test_staged_release_installer_uses_verified_locked_artifacts(tmp_path: Path) -> None:
    release_base = "https://github.com/example/Kestrel/releases/download/v0.3.0"
    staged = _stage_release_installer(
        tmp_path / "install.sh",
        release_base=release_base,
        release_sha=TEST_RELEASE_SHA,
        version="0.3.0",
    )

    result = _run_install(
        env={"KESTREL_DRY_RUN": "1", "KESTREL_HOME": str(tmp_path / "kestrel-home")},
        install=staged,
    )

    assert result.returncode == 0, result.stderr
    assert "fetch --depth 1 --filter=blob:none --no-tags origin v0.3.0" in result.stdout
    assert f"immutable release commit: {TEST_RELEASE_SHA}" in result.stdout
    assert (
        f"verify checkout HEAD equals immutable release commit {TEST_RELEASE_SHA}" in result.stdout
    )
    assert f"locked requirements: {release_base}/requirements-release.txt" in result.stdout
    assert "verify SHA256SUMS" in result.stdout
    assert result.stdout.index("requirements-release.txt --output") < result.stdout.index(
        "-m venv .venv"
    )
    assert (
        "pip install --require-hashes --only-binary=:all: "
        "-r .nest/release/requirements-release.txt" in result.stdout
    )
    assert "pip install --no-deps" in result.stdout
    assert "nested_memvid_agent-0.3.0-py3-none-any.whl" in result.stdout
    assert "Using the workbench bundled in the verified release wheel." in result.stdout
    assert "pip install -e" not in result.stdout
    assert "npm ci" not in result.stdout


@POSIX_SHELL_ONLY
@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("KESTREL_REPO", "https://example.invalid/fork.git", "KESTREL_REPO cannot override"),
        ("KESTREL_REF", "main", "KESTREL_REF cannot override"),
        (
            "KESTREL_REQUIREMENTS_URL",
            "https://replacement.invalid/requirements-release.txt",
            "KESTREL_REQUIREMENTS_URL cannot override",
        ),
        (
            "KESTREL_WHEEL_URL",
            "https://replacement.invalid/nested_memvid_agent-0.4.0-py3-none-any.whl",
            "KESTREL_WHEEL_URL cannot override",
        ),
        (
            "KESTREL_CHECKSUMS_URL",
            "https://replacement.invalid/SHA256SUMS",
            "KESTREL_CHECKSUMS_URL cannot override",
        ),
    ],
)
def test_staged_release_installer_rejects_source_overrides(
    tmp_path: Path, variable: str, value: str, message: str
) -> None:
    staged = _stage_release_installer(
        tmp_path / "install.sh",
        release_base="https://example.invalid/releases/download/v0.4.0",
        release_sha=TEST_RELEASE_SHA,
        version="0.4.0",
    )

    result = _run_install(
        install=staged,
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(tmp_path / "home"),
            variable: value,
        },
    )

    assert result.returncode != 0
    assert message in result.stderr


@POSIX_SHELL_ONLY
def test_staged_release_installer_rejects_moved_tag(tmp_path: Path) -> None:
    source = _current_tree_git_repo(tmp_path)
    original_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    subprocess.run(["git", "tag", "v0.4.0", original_sha], cwd=source, check=True)
    (source / "moved-tag.txt").write_text("moved\n", encoding="utf-8")
    subprocess.run(["git", "add", "moved-tag.txt"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "move tag target"], cwd=source, check=True)
    subprocess.run(["git", "tag", "-f", "v0.4.0"], cwd=source, check=True)
    staged = _stage_release_installer(
        tmp_path / "install.sh",
        release_base="https://example.invalid/releases/download/v0.4.0",
        release_sha=original_sha,
        version="0.4.0",
        repository=str(source),
    )

    result = _run_install(
        install=staged,
        env={
            "KESTREL_HOME": str(tmp_path / "home"),
            "KESTREL_PYTHON": sys.executable,
            "KESTREL_PORT": str(_unused_local_port()),
        },
    )

    assert result.returncode != 0
    assert "Refusing a moved tag or mismatched repository" in result.stderr
    assert original_sha in result.stderr
    assert not (tmp_path / "home").exists()


@POSIX_SHELL_ONLY
def test_staged_release_installer_rejects_mismatched_repository_head(
    tmp_path: Path,
) -> None:
    source = _current_tree_git_repo(tmp_path)
    embedded_sha = "0" * 40
    staged = _stage_release_installer(
        tmp_path / "install.sh",
        release_base="https://example.invalid/releases/download/v0.4.0",
        release_sha=embedded_sha,
        version="0.4.0",
        repository=str(source),
        ref="HEAD",
    )

    result = _run_install(
        install=staged,
        env={
            "KESTREL_HOME": str(tmp_path / "home"),
            "KESTREL_PYTHON": sys.executable,
            "KESTREL_PORT": str(_unused_local_port()),
        },
    )

    assert result.returncode != 0
    assert "Refusing a moved tag or mismatched repository" in result.stderr
    assert embedded_sha in result.stderr
    assert not (tmp_path / "home").exists()


@POSIX_SHELL_ONLY
def test_release_artifact_urls_must_be_complete_and_https(tmp_path: Path) -> None:
    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(tmp_path / "kestrel-home"),
            "KESTREL_WHEEL_URL": "https://example.invalid/nested_memvid_agent-0.3.0.whl",
        }
    )

    assert result.returncode != 0
    assert "must be set together" in result.stderr


@POSIX_SHELL_ONLY
def test_source_installer_cannot_enter_unbound_release_artifact_mode(
    tmp_path: Path,
) -> None:
    release_base = "https://example.invalid/releases/download/v0.4.0"
    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(tmp_path / "home"),
            "KESTREL_REQUIREMENTS_URL": f"{release_base}/requirements-release.txt",
            "KESTREL_WHEEL_URL": (f"{release_base}/nested_memvid_agent-0.4.0-py3-none-any.whl"),
            "KESTREL_CHECKSUMS_URL": f"{release_base}/SHA256SUMS",
        }
    )

    assert result.returncode != 0
    assert "installer-embedded immutable Git commit SHA" in result.stderr


@POSIX_SHELL_ONLY
@pytest.mark.parametrize(
    "failure_mode",
    ["invalid_wheel", "signal_after_backup", "preexisting_recovery"],
)
def test_failed_release_install_restores_checkout_and_existing_venv(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    source = _current_tree_git_repo(tmp_path)
    target = _installed_kestrel_checkout(tmp_path)
    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    original_branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=target,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text(".venv/\n.venv.release-previous/\n.nest/\n", encoding="utf-8")
    sentinel = target / ".venv" / "operator-runtime.txt"
    sentinel.parent.mkdir()
    sentinel.write_text("working runtime\n", encoding="utf-8")
    stale_recovery = target / ".venv.release-previous" / "stale-runtime.txt"
    if failure_mode == "preexisting_recovery":
        stale_recovery.parent.mkdir()
        stale_recovery.write_text("stale recovery\n", encoding="utf-8")

    fixtures = tmp_path / "release-assets"
    fixtures.mkdir()
    requirements = fixtures / "requirements-release.txt"
    requirements.write_text("# intentionally empty test lock\n", encoding="utf-8")
    wheel = fixtures / "nested_memvid_agent-0.3.0-py3-none-any.whl"
    wheel.write_bytes(b"intentionally invalid wheel")
    checksums = fixtures / "SHA256SUMS"
    checksums.write_text(
        "\n".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}"
            for path in (requirements, wheel)
        )
        + "\n",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_curl = fake_bin / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "url=''\n"
        "output=''\n"
        "while [[ $# -gt 0 ]]; do\n"
        '  case "$1" in\n'
        '    https://*) url="$1"; shift ;;\n'
        '    --output) output="$2"; shift 2 ;;\n'
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        'cp "$FIXTURE_DIR/${url##*/}" "$output"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    if failure_mode == "signal_after_backup":
        fake_mv = fake_bin / "mv"
        fake_mv.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            '/bin/mv "$@"\n'
            'if [[ ! -e "$SIGNAL_MARKER" ]]; then\n'
            '  : >"$SIGNAL_MARKER"\n'
            '  kill -TERM "$PPID"\n'
            "fi\n",
            encoding="utf-8",
        )
        fake_mv.chmod(0o755)
    release_base = "https://example.invalid/releases/download/v0.3.0"
    source_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    staged = _stage_release_installer(
        tmp_path / "release-install.sh",
        release_base=release_base,
        release_sha=source_head,
        version="0.3.0",
        repository=str(source),
        ref="HEAD",
    )

    result = _run_install(
        install=staged,
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FIXTURE_DIR": str(fixtures),
            "SIGNAL_MARKER": str(tmp_path / "signal-injected"),
            "KESTREL_HOME": str(target),
            "KESTREL_PYTHON": sys.executable,
            "KESTREL_PORT": str(_unused_local_port()),
            "KESTREL_SKIP_WEB": "1",
            "KESTREL_SKIP_SMOKE": "1",
        },
    )

    assert result.returncode != 0
    assert "restoring the previous checkout and Python environment" in result.stdout
    if failure_mode == "signal_after_backup":
        assert result.returncode == 143
        assert (tmp_path / "signal-injected").exists()
    if failure_mode == "preexisting_recovery":
        assert "prior release recovery environment already exists" in result.stderr
    assert sentinel.read_text(encoding="utf-8") == "working runtime\n"
    if failure_mode == "preexisting_recovery":
        assert stale_recovery.read_text(encoding="utf-8") == "stale recovery\n"
    else:
        assert not (target / ".venv.release-previous").exists()
    restored_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert restored_head == original_head
    restored_branch = subprocess.run(
        ["git", "symbolic-ref", "--quiet", "--short", "HEAD"],
        cwd=target,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    assert restored_branch == original_branch


@POSIX_SHELL_ONLY
def test_install_refuses_non_git_nonempty_target_even_in_dry_run(tmp_path: Path) -> None:
    target = tmp_path / "occupied"
    target.mkdir()
    (target / "keep.txt").write_text("do not overwrite\n", encoding="utf-8")

    result = _run_install(env={"KESTREL_DRY_RUN": "1", "KESTREL_HOME": str(target)})

    assert result.returncode != 0
    assert "Refusing to install into non-git nonempty directory" in result.stderr


@POSIX_SHELL_ONLY
@pytest.mark.parametrize("dirty_kind", ["tracked", "untracked"])
def test_install_refuses_dirty_existing_checkout_without_overwriting_operator_data(
    tmp_path: Path,
    dirty_kind: str,
) -> None:
    target = _installed_kestrel_checkout(tmp_path)
    if dirty_kind == "tracked":
        protected = target / "operator.txt"
        protected.write_text("locally modified operator data\n", encoding="utf-8")
    else:
        protected = target / "local-only.txt"
        protected.write_text("untracked operator data\n", encoding="utf-8")
    before = protected.read_text(encoding="utf-8")

    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(target),
            "KESTREL_REPO": "https://example.invalid/upstream.git",
            "KESTREL_SKIP_WEB": "1",
            "KESTREL_SKIP_SMOKE": "1",
        }
    )

    assert result.returncode != 0
    assert "Refusing to update a dirty Kestrel checkout" in result.stderr
    assert protected.read_text(encoding="utf-8") == before
    assert (
        subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=target,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        == "https://example.invalid/operator-fork.git"
    )


@POSIX_SHELL_ONLY
def test_install_existing_checkout_preserves_origin_and_avoids_forced_checkout(
    tmp_path: Path,
) -> None:
    target = _installed_kestrel_checkout(tmp_path)

    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(target),
            "KESTREL_REPO": "https://example.invalid/upstream.git",
            "KESTREL_REF": "release-test",
            "KESTREL_SKIP_WEB": "1",
            "KESTREL_SKIP_SMOKE": "1",
        }
    )

    assert result.returncode == 0, result.stderr
    assert "fetch https://example.invalid/upstream.git release-test" in result.stdout
    assert "fetch --depth 1" not in result.stdout
    assert "remote add origin" not in result.stdout
    assert "checkout --detach --no-overwrite-ignore FETCH_HEAD" in result.stdout
    assert "checkout -f" not in result.stdout
    assert "remote set-url" not in result.stdout
    assert (
        subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=target,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        == "https://example.invalid/operator-fork.git"
    )


@POSIX_SHELL_ONLY
def test_new_install_fetches_only_requested_ref_without_reachable_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.name", "Kestrel Installer Test"], cwd=source, check=True)
    subprocess.run(
        ["git", "config", "user.email", "kestrel-installer@example.invalid"],
        cwd=source,
        check=True,
    )
    (source / "pyproject.toml").write_text(
        '[project]\nname = "nested-memvid-agent"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    historical = source / "removed-history-only.txt"
    historical.write_text("historical fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "historical fixture"], cwd=source, check=True)
    historical.unlink()
    (source / "README.md").write_text("current tree\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "current tree"], cwd=source, check=True)

    target = tmp_path / "installed"
    plan = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(target),
            "KESTREL_REPO": str(source),
            "KESTREL_REF": "HEAD",
            "KESTREL_SKIP_WEB": "1",
            "KESTREL_SKIP_SMOKE": "1",
        }
    )
    assert plan.returncode == 0, plan.stderr
    assert f"git init -q {target}.install-staging" in plan.stdout
    assert f"remote add origin {source}" in plan.stdout
    assert "fetch --depth 1 --filter=blob:none --no-tags origin HEAD" in plan.stdout
    assert "git clone" not in plan.stdout

    target.mkdir()
    result = _run_installer_function(
        "ensure_git_target",
        home=target,
        pid_file=target / ".nest" / "server.pid",
        port=_unused_local_port(),
        extra_env={"KESTREL_REPO": str(source), "KESTREL_REF": "HEAD"},
    )

    assert result.returncode == 0, result.stderr
    assert (target / "README.md").read_text(encoding="utf-8") == "current tree\n"
    assert not (target / "removed-history-only.txt").exists()
    assert (
        subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=target,
            check=True,
            text=True,
            capture_output=True,
        ).stdout.strip()
        == "1"
    )
    assert subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=target,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip() == str(source)


@POSIX_SHELL_ONLY
def test_failed_fresh_fetch_leaves_retryable_target(tmp_path: Path) -> None:
    source = _current_tree_git_repo(tmp_path)
    target = tmp_path / "fresh-install"
    target.mkdir()

    failed = _run_installer_function(
        "ensure_git_target",
        home=target,
        pid_file=target / ".nest" / "server.pid",
        port=_unused_local_port(),
        extra_env={
            "KESTREL_REPO": str(tmp_path / "missing-repository"),
            "KESTREL_REF": "HEAD",
        },
    )

    assert failed.returncode != 0
    assert not (target / ".git").exists()
    assert list(target.iterdir()) == []

    retried = _run_installer_function(
        "ensure_git_target",
        home=target,
        pid_file=target / ".nest" / "server.pid",
        port=_unused_local_port(),
        extra_env={"KESTREL_REPO": str(source), "KESTREL_REF": "HEAD"},
    )

    assert retried.returncode == 0, retried.stderr
    assert (target / "pyproject.toml").is_file()


def test_install_script_detects_only_supported_python_without_bare_python_default() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "KESTREL_PYTHON" in text
    for candidate in ["python3.13", "python3.12", "python3.11", "/opt/homebrew/bin/python3.11"]:
        assert candidate in text
    assert '"$PYTHON_BIN" -m venv .venv' in text
    assert "python -m venv" not in text
    assert "(3, 11) <= sys.version_info < (3, 14)" in text
    assert "require_supported_platform" in text
    assert "native Windows is unsupported" in text


@POSIX_SHELL_ONLY
def test_installer_rejects_explicit_unsupported_python(tmp_path: Path) -> None:
    fake_python = tmp_path / "python3.14"
    fake_python.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    fake_python.chmod(0o755)

    result = _run_install(
        env={
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(tmp_path / "home"),
            "KESTREL_PYTHON": str(fake_python),
        }
    )

    assert result.returncode != 0
    assert "Python 3.14 and newer are not supported" in result.stderr


@POSIX_SHELL_ONLY
@pytest.mark.parametrize(
    ("system", "architecture", "accepted"),
    [
        ("Darwin", "arm64", True),
        ("Linux", "x86_64", True),
        ("Linux", "aarch64", False),
        ("Linux", "arm64", False),
    ],
)
def test_one_shot_platform_support_is_explicit(
    tmp_path: Path, system: str, architecture: str, accepted: bool
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        "#!/usr/bin/env bash\n"
        f"if [[ \"${{1:-}}\" == '-s' ]]; then printf '%s\\n' '{system}'; "
        f"else printf '%s\\n' '{architecture}'; fi\n",
        encoding="utf-8",
    )
    fake_uname.chmod(0o755)

    result = _run_install(
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "KESTREL_DRY_RUN": "1",
            "KESTREL_HOME": str(tmp_path / "home"),
            "KESTREL_SKIP_WEB": "1",
        }
    )

    assert (result.returncode == 0) is accepted, result.stderr
    if not accepted:
        assert "one-shot installer does not support Linux ARM64" in result.stderr
        assert "published linux/arm64 container image" in result.stderr


def test_install_script_clears_pythonpath_before_creating_the_venv() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "unset PYTHONPATH" in text
    assert text.index("unset PYTHONPATH") < text.index('PYTHON_BIN="$(detect_python)"')


def test_source_installer_uses_hash_locked_nonisolated_build_bootstrap() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "config/python-build-bootstrap.txt" in text
    assert "pip install --require-hashes --only-binary=:all:" in text
    assert 'pip install --no-build-isolation -e ".[${KESTREL_EXTRAS}]"' in text
    assert "pip install --upgrade pip" not in text


def test_install_script_launches_server_detached_and_checks_health() -> None:
    text = INSTALL.read_text(encoding="utf-8")
    supervisor = INSTALLER_SERVER_SUPERVISOR.read_text(encoding="utf-8")

    assert "start_server_detached" in text
    assert "wait_for_server" in text
    assert "/api/health/ready" in text
    assert "KESTREL_SERVER_PID" in text
    assert 'child_pid="$!"' in supervisor
    assert "printf '%s\\n' \"$$\"" in supervisor
    assert supervisor.index("printf '%s\\n' \"$$\"") < supervisor.index('child_pid="$!"')
    assert "printf '%s\\n' \"$child_pgid\"" in supervisor
    assert supervisor.index("printf '%s\\n' \"$child_pgid\"") < supervisor.index(
        "printf '%s\\n' \"$child_pid\""
    )
    assert 'chmod 600 "$pid_tmp"' in supervisor
    assert 'mv -f -- "$pid_tmp" "$pid_file"' in supervisor
    assert "screen -dmS" in text
    assert "nohup" in text
    assert "stop_port_listener" not in text
    assert "installer-server-supervisor.sh" in text
    assert "KESTREL_SERVER_SUPERVISOR_PID" in text
    main_body = text.split("main() {", maxsplit=1)[1]
    assert main_body.index("require_offline_server_upgrade_preflight") < main_body.index(
        "ensure_git_target"
    )
    assert main_body.index("commit_staged_state") < main_body.index("start_server_detached")
    assert main_body.index("start_server_detached") < main_body.index(
        "finalize_release_install_transaction"
    )
    assert main_body.index("validate_candidate_memory_isolated") < main_body.index(
        "finalize_release_install_transaction"
    )
    assert main_body.index("commit_staged_web_assets") < main_body.index(
        "finalize_release_install_transaction"
    )
    assert main_body.index("validate_candidate_memory_isolated") < main_body.index(
        "commit_staged_memory"
    )
    assert main_body.index("commit_staged_memory") < main_body.index(
        "finalize_release_install_transaction"
    )
    assert main_body.index("start_server_detached") < main_body.index(
        "finish_post_commit_maintenance"
    )
    assert "run .venv/bin/nest-agent server --backend memvid" not in text


@POSIX_SHELL_ONLY
def test_installer_process_exists_treats_zombie_as_stopped(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import os, time\n"
                "child = os.fork()\n"
                "if child == 0:\n"
                "    os._exit(0)\n"
                "print(child, flush=True)\n"
                "time.sleep(30)\n"
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        zombie_pid = int(holder.stdout.readline().strip())
        deadline = time.monotonic() + 5
        process_state = ""
        while time.monotonic() < deadline:
            process_state = subprocess.run(
                ["ps", "-p", str(zombie_pid), "-o", "stat="],
                text=True,
                capture_output=True,
                check=False,
            ).stdout.strip()
            if process_state.startswith("Z"):
                break
            time.sleep(0.02)
        assert process_state.startswith("Z"), process_state
        assert (
            subprocess.run(
                ["kill", "-0", str(zombie_pid)],
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )

        result = _run_installer_function(
            'process_exists "$LIVE_PID" && ! process_exists "$ZOMBIE_PID"',
            home=home,
            pid_file=home / ".nest" / "server.pid",
            port=_unused_local_port(),
            extra_env={
                "LIVE_PID": str(holder.pid),
                "ZOMBIE_PID": str(zombie_pid),
            },
        )
        assert result.returncode == 0, result.stderr
    finally:
        _stop_process(holder)


@POSIX_SHELL_ONLY
def test_installer_server_handoff_polls_readiness_endpoint(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    port = _unused_local_port()

    result = _run_installer_function(
        "health_url",
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=port,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"http://127.0.0.1:{port}/api/health/ready"


@POSIX_SHELL_ONLY
def test_installer_refuses_symbolic_link_server_pid_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "pid-target"
    unrelated = subprocess.Popen(["sleep", "30"], text=True)
    try:
        _write_private_pid_file(target, unrelated.pid)
        pid_file = home / ".nest" / "server.pid"
        pid_file.parent.mkdir()
        pid_file.symlink_to(target)

        result = _run_installer_function(
            "require_offline_server_upgrade_preflight",
            home=home,
            pid_file=pid_file,
            port=_unused_local_port(),
        )

        assert result.returncode != 0
        assert "symbolic-link Kestrel server PID file" in result.stderr
        assert unrelated.poll() is None
        assert pid_file.is_symlink()
    finally:
        _stop_process(unrelated)


@POSIX_SHELL_ONLY
def test_installer_refuses_pid_reuse_by_unrelated_process(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    unrelated = subprocess.Popen(["sleep", "30"], cwd=home, text=True)
    pid_file = home / ".nest" / "server.pid"
    try:
        _write_private_pid_file(pid_file, unrelated.pid)

        result = _run_installer_function(
            "require_offline_server_upgrade_preflight",
            home=home,
            pid_file=pid_file,
            port=_unused_local_port(),
        )

        assert result.returncode != 0
        assert "is not the expected current-user standard Kestrel server" in result.stderr
        assert "Refusing to mutate the installation or terminate it" in result.stderr
        assert unrelated.poll() is None
    finally:
        _stop_process(unrelated)


@POSIX_SHELL_ONLY
def test_installer_refuses_non_private_server_pid_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    pid_file = home / ".nest" / "server.pid"
    _write_private_pid_file(pid_file, 99_999_999)
    pid_file.chmod(0o644)

    result = _run_installer_function(
        "require_offline_server_upgrade_preflight",
        home=home,
        pid_file=pid_file,
        port=_unused_local_port(),
    )

    assert result.returncode != 0
    assert "non-private Kestrel server PID file" in result.stderr
    assert pid_file.exists()


@POSIX_SHELL_ONLY
def test_installer_refuses_unrelated_port_listener_without_killing_it(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    port = _unused_local_port()
    listener = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import socket,sys,time; "
                "sock=socket.socket(); "
                "sock.bind(('127.0.0.1', int(sys.argv[1]))); "
                "sock.listen(); print('ready', flush=True); time.sleep(30)"
            ),
            str(port),
        ],
        cwd=home,
        text=True,
        stdout=subprocess.PIPE,
    )
    try:
        assert listener.stdout is not None
        assert listener.stdout.readline().strip() == "ready"

        result = _run_installer_function(
            "require_offline_server_upgrade_preflight",
            home=home,
            pid_file=home / ".nest" / "server.pid",
            port=port,
        )

        assert result.returncode != 0
        assert "Configured port" in result.stderr
        assert "occupied or cannot be verified free" in result.stderr
        assert listener.poll() is None
    finally:
        _stop_process(listener)


@POSIX_SHELL_ONLY
def test_installer_removes_private_stale_pid_file(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    pid_file = home / ".nest" / "server.pid"
    stale_pid = 99_999_999
    _write_private_pid_file(pid_file, stale_pid)

    result = _run_installer_function(
        "require_offline_server_upgrade_preflight",
        home=home,
        pid_file=pid_file,
        port=_unused_local_port(),
    )

    assert result.returncode == 0, result.stderr
    assert f"non-running PID {stale_pid}" in result.stdout
    assert not pid_file.exists()


@POSIX_SHELL_ONLY
@pytest.mark.parametrize("start_server", ["0", "1"])
def test_installer_refuses_live_owned_kestrel_server_without_stopping_it(
    tmp_path: Path, start_server: str
) -> None:
    home = tmp_path / "home"
    executable = home / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env bash\ntrap 'exit 0' TERM INT\nwhile :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    pid_file = home / ".nest" / "server.pid"
    log_file = home / ".nest" / "server.log"
    supervisor_pid_file = home / ".nest" / "server.supervisor.pid"
    process_group_file = home / ".nest" / "server.pgid"
    pid_file.parent.mkdir()
    port = _unused_local_port()
    supervisor = subprocess.Popen(
        [
            "bash",
            str(INSTALLER_SERVER_SUPERVISOR),
            "--pid-file",
            str(pid_file),
            "--supervisor-pid-file",
            str(supervisor_pid_file),
            "--process-group-file",
            str(process_group_file),
            "--log-file",
            str(log_file),
            "--",
            str(executable),
            "server",
            "--backend",
            "memvid",
            "--memory-dir",
            ".nest/memory",
            "--provider",
            "mock",
            "--model",
            "mock",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=home,
        text=True,
    )
    try:
        _wait_for_file(supervisor_pid_file)
        _wait_for_file(pid_file)
        child_pid = int(pid_file.read_text(encoding="ascii").strip())
        assert pid_file.stat().st_mode & 0o077 == 0
        assert not pid_file.is_symlink()

        result = _run_installer_function(
            "require_offline_server_upgrade_preflight",
            home=home,
            pid_file=pid_file,
            port=port,
            extra_env={
                "KESTREL_START_SERVER": start_server,
                "KESTREL_SERVER_SUPERVISOR_PID": str(supervisor_pid_file),
                "KESTREL_SERVER_PROCESS_GROUP": str(process_group_file),
            },
        )

        assert result.returncode != 0
        assert "verified installer-managed Kestrel supervisor is running" in result.stderr
        assert "No checkout, .venv, or memory changes were made" in result.stderr
        assert supervisor.poll() is None
        assert subprocess.run(["kill", "-0", str(child_pid)], check=False).returncode == 0
        assert pid_file.exists()
    finally:
        _stop_process(supervisor)


@POSIX_SHELL_ONLY
@pytest.mark.parametrize("start_server", ["0", "1"])
def test_full_installer_refuses_live_service_before_any_install_mutation(
    tmp_path: Path, start_server: str
) -> None:
    target = _installed_kestrel_checkout(tmp_path)
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text(".venv/\n.nest/\n", encoding="utf-8")
    sentinel = target / ".venv" / "operator-runtime.txt"
    executable = target / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    sentinel.write_text("original runtime\n", encoding="utf-8")
    executable.write_text(
        "#!/usr/bin/env bash\ntrap 'exit 0' TERM INT\nwhile :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    memory_sentinel = target / ".nest" / "memory" / "sentinel.mv2"
    memory_sentinel.parent.mkdir(parents=True)
    memory_sentinel.write_bytes(b"original-memory-bytes")
    pid_file = target / ".nest" / "server.pid"
    supervisor_pid_file = target / ".nest" / "server.supervisor.pid"
    process_group_file = target / ".nest" / "server.pgid"
    log_file = target / ".nest" / "server.log"
    port = _unused_local_port()
    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=target,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()
    supervisor = subprocess.Popen(
        [
            "bash",
            str(INSTALLER_SERVER_SUPERVISOR),
            "--pid-file",
            str(pid_file),
            "--supervisor-pid-file",
            str(supervisor_pid_file),
            "--process-group-file",
            str(process_group_file),
            "--log-file",
            str(log_file),
            "--",
            str(executable),
            "server",
            "--backend",
            "memvid",
            "--memory-dir",
            ".nest/memory",
            "--provider",
            "mock",
            "--model",
            "mock",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=target,
        text=True,
    )
    try:
        _wait_for_file(supervisor_pid_file)
        _wait_for_file(pid_file)
        result = _run_install(
            env={
                "KESTREL_HOME": str(target),
                "KESTREL_REPO": str(tmp_path / "must-not-fetch"),
                "KESTREL_REF": "HEAD",
                "KESTREL_PYTHON": sys.executable,
                "KESTREL_SKIP_WEB": "1",
                "KESTREL_SKIP_SMOKE": "1",
                "KESTREL_START_SERVER": start_server,
                "KESTREL_PORT": str(port),
                "KESTREL_SERVER_PID": str(pid_file),
                "KESTREL_SERVER_SUPERVISOR_PID": str(supervisor_pid_file),
                "KESTREL_SERVER_PROCESS_GROUP": str(process_group_file),
                "KESTREL_SERVER_LOG": str(log_file),
            }
        )

        assert result.returncode != 0
        assert "No checkout, .venv, or memory changes were made" in result.stderr
        assert "git -C" not in result.stdout
        assert sentinel.read_text(encoding="utf-8") == "original runtime\n"
        assert memory_sentinel.read_bytes() == b"original-memory-bytes"
        assert (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=target,
                check=True,
                text=True,
                capture_output=True,
            ).stdout.strip()
            == original_head
        )
        assert supervisor.poll() is None
    finally:
        _stop_process(supervisor)


@POSIX_SHELL_ONLY
def test_offline_preflight_detects_untracked_standard_server_without_pid_file(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    executable = home / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env bash\ntrap 'exit 0' TERM INT\nwhile :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    port = _unused_local_port()
    server = subprocess.Popen(
        [
            str(executable),
            "server",
            "--backend",
            "memvid",
            "--memory-dir",
            ".nest/memory",
            "--provider",
            "mock",
            "--model",
            "mock",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=home,
        text=True,
    )
    try:
        result = _run_installer_function(
            "require_offline_server_upgrade_preflight",
            home=home,
            pid_file=home / ".nest" / "server.pid",
            port=port,
            extra_env={"KESTREL_START_SERVER": "0"},
        )

        assert result.returncode != 0
        assert f"untracked PID {server.pid}" in result.stderr
        assert server.poll() is None
    finally:
        _stop_process(server)


@POSIX_SHELL_ONLY
def test_failed_server_health_stops_tracked_supervisor_and_child(tmp_path: Path) -> None:
    home = tmp_path / "home"
    executable = home / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    marker = home / "child.pid"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$$\" >{shlex.quote(str(marker))}\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    scripts_dir = home / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INSTALLER_SERVER_SUPERVISOR, scripts_dir / INSTALLER_SERVER_SUPERVISOR.name)
    pid_file = home / ".nest" / "server.pid"
    supervisor_pid_file = home / ".nest" / "server.supervisor.pid"
    result = _run_installer_function(
        "wait_for_server_status() { return 1; }; start_server_detached",
        home=home,
        pid_file=pid_file,
        port=_unused_local_port(),
        extra_env={"KESTREL_SERVER_SUPERVISOR_PID": str(supervisor_pid_file)},
    )

    assert result.returncode != 0
    assert "candidate Kestrel server did not become healthy" in result.stderr
    assert "did not become healthy and was stopped" in result.stderr
    if marker.exists():
        child_pid = int(marker.read_text(encoding="ascii").strip())
        assert subprocess.run(["kill", "-0", str(child_pid)], check=False).returncode != 0
    assert not pid_file.exists()
    assert not supervisor_pid_file.exists()
    assert "Proved the failed launch supervisor, full server process group" in result.stdout


@POSIX_SHELL_ONLY
def test_failed_child_pid_publication_still_cleans_via_supervisor_identity(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    executable = home / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    marker = home / "delayed-child.pid"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$$\" >{shlex.quote(str(marker))}\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    scripts_dir = home / "scripts"
    scripts_dir.mkdir()
    supervisor = INSTALLER_SERVER_SUPERVISOR.read_text(encoding="utf-8")
    publication = 'pid_tmp="$(mktemp "${pid_file}.tmp.XXXXXX")"'
    assert supervisor.count(publication) == 1
    supervisor = supervisor.replace(publication, f"sleep 30\n{publication}", 1)
    local_supervisor = scripts_dir / INSTALLER_SERVER_SUPERVISOR.name
    local_supervisor.write_text(supervisor, encoding="utf-8")
    local_supervisor.chmod(0o755)
    pid_file = home / ".nest" / "server.pid"
    supervisor_pid_file = home / ".nest" / "server.supervisor.pid"
    process_group_file = home / ".nest" / "server.pgid"

    result = _run_installer_function(
        "start_server_detached",
        home=home,
        pid_file=pid_file,
        port=_unused_local_port(),
        extra_env={"KESTREL_SERVER_SUPERVISOR_PID": str(supervisor_pid_file)},
    )

    assert result.returncode != 0
    diagnostics = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert "candidate Kestrel server could not be launched" in result.stderr, diagnostics
    assert "failed supervisor and child were stopped" in result.stderr, diagnostics
    _wait_for_file(marker)
    child_pid = int(marker.read_text(encoding="ascii").strip())
    assert subprocess.run(["kill", "-0", str(child_pid)], check=False).returncode != 0
    assert not pid_file.exists()
    assert not supervisor_pid_file.exists()
    assert not process_group_file.exists()
    assert "Proved the failed launch supervisor, full server process group" in result.stdout


@POSIX_SHELL_ONLY
def test_failed_health_kills_term_ignoring_server_and_grandchild_group(tmp_path: Path) -> None:
    home = tmp_path / "home"
    executable = home / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    server_marker = home / "stubborn-server.pid"
    descendant_marker = home / "stubborn-descendant.pid"
    late_mutation = home / "late-mutation"
    late_mutation_command = f"printf 'escaped' >{shlex.quote(str(late_mutation))}"
    grandchild = home / "stubborn-grandchild"
    grandchild.write_text(
        "#!/usr/bin/env bash\n"
        "trap '' TERM INT\n"
        f"trap {shlex.quote(late_mutation_command)} EXIT\n"
        f"printf '%s\\n' \"$$\" >{shlex.quote(str(descendant_marker))}\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    grandchild.chmod(0o700)
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "trap '' TERM INT\n"
        f"printf '%s\\n' \"$$\" >{shlex.quote(str(server_marker))}\n"
        f"{shlex.quote(str(grandchild))} &\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    scripts_dir = home / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INSTALLER_SERVER_SUPERVISOR, scripts_dir / INSTALLER_SERVER_SUPERVISOR.name)
    pid_file = home / ".nest" / "server.pid"
    supervisor_pid_file = home / ".nest" / "server.supervisor.pid"
    process_group_file = home / ".nest" / "server.pgid"

    result = _run_installer_function(
        (
            "wait_for_server_status() { local _; for _ in {1..100}; do "
            '[[ -f "$DESCENDANT_PID_FILE" ]] && return 1; sleep 0.05; done; return 1; }; '
            "start_server_detached"
        ),
        home=home,
        pid_file=pid_file,
        port=_unused_local_port(),
        extra_env={
            "DESCENDANT_PID_FILE": str(descendant_marker),
            "KESTREL_SERVER_SUPERVISOR_PID": str(supervisor_pid_file),
            "KESTREL_SERVER_PROCESS_GROUP": str(process_group_file),
        },
    )

    assert result.returncode != 0
    _wait_for_file(server_marker)
    _wait_for_file(descendant_marker)
    server_pid = int(server_marker.read_text(encoding="ascii").strip())
    descendant_pid = int(descendant_marker.read_text(encoding="ascii").strip())
    assert subprocess.run(["kill", "-0", str(server_pid)], check=False).returncode != 0
    assert subprocess.run(["kill", "-0", str(descendant_pid)], check=False).returncode != 0
    assert not pid_file.exists()
    assert not supervisor_pid_file.exists()
    assert not process_group_file.exists()
    assert "full server process group" in result.stdout
    assert not late_mutation.exists()


@POSIX_SHELL_ONLY
def test_browser_open_failure_is_post_commit_best_effort(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_open = fake_bin / "open"
    fake_open.write_text("#!/usr/bin/env bash\nexit 41\n", encoding="utf-8")
    fake_open.chmod(0o755)

    result = _run_installer_function(
        "open_web_ui_best_effort",
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=_unused_local_port(),
        extra_env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "KESTREL_OPEN_BROWSER": "1",
            "KESTREL_START_SERVER": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "install and server startup committed successfully" in result.stdout
    assert "Open http://127.0.0.1:" in result.stdout


@POSIX_SHELL_ONLY
def test_candidate_memory_validation_never_opens_live_memory(tmp_path: Path) -> None:
    home = tmp_path / "home"
    executable = home / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    invocation_log = home / "invocations.log"
    executable.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >>{shlex.quote(str(invocation_log))}\n"
        "memory_dir=''\n"
        "previous=''\n"
        'for argument in "$@"; do\n'
        '  if [[ "$previous" == \'--memory-dir\' ]]; then memory_dir="$argument"; fi\n'
        '  previous="$argument"\n'
        "done\n"
        'if [[ "${1:-}" == \'init\' ]]; then mkdir -p "$memory_dir"; '
        "printf 'candidate-write' >\"$memory_dir/candidate.mv2\"; fi\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    live_memory = home / ".nest" / "memory"
    live_memory.mkdir(parents=True)
    sentinel = live_memory / "working.mv2"
    sentinel.write_bytes(b"byte-identical-live-memory")

    result = _run_installer_function(
        "validate_candidate_memory_isolated; cleanup_install_canary",
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=_unused_local_port(),
    )

    assert result.returncode == 0, result.stderr
    assert sentinel.read_bytes() == b"byte-identical-live-memory"
    assert sorted(path.name for path in live_memory.iterdir()) == ["working.mv2"]
    invocations = invocation_log.read_text(encoding="utf-8")
    assert str(live_memory) not in invocations
    assert "clean-memory" in invocations
    assert "existing-memory-copy" in invocations


@POSIX_SHELL_ONLY
def test_candidate_state_snapshot_never_mutates_live_wal_or_shm_bytes(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    state = home / ".nest" / "state" / "agent.db"
    state.parent.mkdir(parents=True)
    connection = sqlite3.connect(state)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        connection.execute("CREATE TABLE durable (value TEXT NOT NULL)")
        connection.execute("INSERT INTO durable (value) VALUES ('preserve-me')")
        connection.commit()
        artifacts = {
            suffix: Path(f"{state}{suffix}").read_bytes() for suffix in ("", "-wal", "-shm")
        }
        canary_root = home / ".nest" / ".install-canary.snapshot-test"
        candidate = canary_root / "state" / "agent.db"

        result = _run_installer_function(
            (
                f"INSTALL_CANARY_ROOT={shlex.quote(str(canary_root))}; "
                f"INSTALL_CANARY_STATE_PATH={shlex.quote(str(candidate))}; "
                "stage_candidate_state_isolated; "
                "cleanup_install_canary"
            ),
            home=home,
            pid_file=home / ".nest" / "server.pid",
            port=_unused_local_port(),
        )

        assert result.returncode == 0, result.stderr
        for suffix, payload in artifacts.items():
            assert Path(f"{state}{suffix}").read_bytes() == payload
    finally:
        connection.close()


@POSIX_SHELL_ONLY
def test_web_asset_swap_rolls_back_old_dist_on_transaction_failure(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old_dist = home / "web" / "dist"
    old_dist.mkdir(parents=True)
    (old_dist / "index.html").write_text("old-assets\n", encoding="utf-8")
    staged = home / "web" / ".dist.install.test"
    staged.mkdir()
    (staged / "index.html").write_text("candidate-assets\n", encoding="utf-8")

    result = _run_installer_function(
        f"start_release_install_transaction; INSTALL_WEB_STAGED_DIR={shlex.quote(str(staged))}; "
        "commit_staged_web_assets; false",
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=_unused_local_port(),
    )

    assert result.returncode != 0
    assert (old_dist / "index.html").read_text(encoding="utf-8") == "old-assets\n"
    assert not (home / "web" / ".dist.release-previous").exists()
    assert "Restored the previous web workbench assets" in result.stdout


@POSIX_SHELL_ONLY
def test_memory_swap_rolls_back_old_memory_on_transaction_failure(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old_memory = home / ".nest" / "memory"
    old_memory.mkdir(parents=True)
    sentinel = old_memory / "working.mv2"
    sentinel.write_bytes(b"byte-identical-live-memory")
    staged = home / ".nest" / ".install-canary.test" / "existing-memory-copy"
    staged.mkdir(parents=True)
    (staged / "working.mv2").write_bytes(b"verified-candidate-memory")

    result = _run_installer_function(
        f"start_release_install_transaction; INSTALL_CANARY_ROOT={shlex.quote(str(staged.parent))}; "
        f"INSTALL_MEMORY_STAGED_DIR={shlex.quote(str(staged))}; commit_staged_memory; false",
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=_unused_local_port(),
    )

    assert result.returncode != 0
    assert sentinel.read_bytes() == b"byte-identical-live-memory"
    assert sorted(path.name for path in old_memory.iterdir()) == ["working.mv2"]
    assert not (home / ".nest" / ".memory.release-previous").exists()
    assert "Restored the previous memory directory" in result.stdout


@POSIX_SHELL_ONLY
def test_migrated_candidate_state_is_committed_to_custom_runtime_path(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    executable = home / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sqlite3\n"
        "import sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "def option(name):\n"
        "    return args[args.index(name) + 1]\n"
        "if args and args[0] == 'routines':\n"
        "    with sqlite3.connect(option('--state-path')) as connection:\n"
        "        connection.execute(\"UPDATE marker SET value = 'migrated'\")\n"
        "elif args and args[0] == 'init':\n"
        "    memory = Path(option('--memory-dir'))\n"
        "    memory.mkdir(parents=True, exist_ok=True)\n"
        "    (memory / 'candidate.mv2').write_bytes(b'candidate')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    custom_state = tmp_path / "custom-runtime" / "durable.db"
    custom_state.parent.mkdir(mode=0o700)
    _write_sqlite_marker(custom_state, "original")

    result = _run_installer_function(
        (
            "start_release_install_transaction; "
            "validate_candidate_memory_isolated; "
            "prepare_migrated_state_for_commit; "
            "commit_staged_state; "
            "finalize_release_install_transaction; "
            "finish_post_commit_maintenance"
        ),
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=_unused_local_port(),
        extra_env={"NEST_AGENT_STATE_PATH": str(custom_state)},
    )

    assert result.returncode == 0, result.stderr
    with sqlite3.connect(custom_state) as connection:
        assert connection.execute("SELECT value FROM marker").fetchone() == ("migrated",)
    assert not Path(f"{custom_state}.release-previous").exists()
    assert not list(custom_state.parent.glob(f".{custom_state.name}.install-state.*"))


@POSIX_SHELL_ONLY
def test_server_launch_failure_restores_state_and_every_sqlite_sidecar(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = home / ".nest" / "state" / "agent.db"
    _write_sqlite_marker(state, "original")
    originals = {"": state.read_bytes()}
    for suffix in ("-wal", "-shm", "-journal"):
        payload = f"original{suffix}".encode()
        Path(f"{state}{suffix}").write_bytes(payload)
        originals[suffix] = payload
    staging_root, candidate = _stage_sqlite_candidate(state, "migrated")

    result = _run_installer_function(
        (
            "start_release_install_transaction; "
            f"INSTALL_STATE_STAGED_ROOT={shlex.quote(str(staging_root))}; "
            f"INSTALL_STATE_STAGED_PATH={shlex.quote(str(candidate))}; "
            "commit_staged_state; "
            "launch_standard_server_detached() { "
            "SERVER_LAUNCH_ATTEMPTED=1; return 1; }; "
            "start_server_detached"
        ),
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=_unused_local_port(),
    )

    assert result.returncode != 0
    assert "candidate Kestrel server could not be launched" in result.stderr
    for suffix, payload in originals.items():
        assert Path(f"{state}{suffix}").read_bytes() == payload
    assert not Path(f"{state}.release-previous").exists()
    assert not staging_root.exists()
    assert "Restored the previous state database and SQLite sidecars" in result.stdout


@POSIX_SHELL_ONLY
def test_post_migration_health_failure_restores_state_and_sidecars_byte_identically(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    executable = home / ".venv" / "bin" / "nest-agent"
    executable.parent.mkdir(parents=True)
    state = tmp_path / "custom-state" / "durable.db"
    state.parent.mkdir(mode=0o700)
    _write_sqlite_marker(state, "original")
    originals = {"": state.read_bytes()}
    for suffix in ("-wal", "-shm", "-journal"):
        payload = f"byte-identical{suffix}".encode()
        Path(f"{state}{suffix}").write_bytes(payload)
        originals[suffix] = payload
    staging_root, candidate = _stage_sqlite_candidate(state, "migrated")

    executable.write_text(
        "#!/usr/bin/env bash\n"
        "state_path=''\n"
        "previous=''\n"
        'for argument in "$@"; do\n'
        '  if [[ "$previous" == \'--state-path\' ]]; then state_path="$argument"; fi\n'
        '  previous="$argument"\n'
        "done\n"
        "printf 'candidate-sidecar' >\"${state_path}-wal\"\n"
        "trap 'exit 0' TERM INT\n"
        "while :; do sleep 0.1; done\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    scripts_dir = home / "scripts"
    scripts_dir.mkdir()
    shutil.copy2(INSTALLER_SERVER_SUPERVISOR, scripts_dir / INSTALLER_SERVER_SUPERVISOR.name)
    pid_file = home / ".nest" / "server.pid"
    supervisor_pid_file = home / ".nest" / "server.supervisor.pid"
    process_group_file = home / ".nest" / "server.pgid"
    recovery_seen = tmp_path / "recovery-seen-during-health"

    result = _run_installer_function(
        (
            "start_release_install_transaction; "
            f"INSTALL_STATE_STAGED_ROOT={shlex.quote(str(staging_root))}; "
            f"INSTALL_STATE_STAGED_PATH={shlex.quote(str(candidate))}; "
            "commit_staged_state; "
            "wait_for_server_status() { "
            '[[ -f "$EXPECTED_RECOVERY_DB" ]] && : >"$RECOVERY_SEEN"; '
            "return 1; }; "
            "start_server_detached"
        ),
        home=home,
        pid_file=pid_file,
        port=_unused_local_port(),
        extra_env={
            "NEST_AGENT_STATE_PATH": str(state),
            "KESTREL_SERVER_SUPERVISOR_PID": str(supervisor_pid_file),
            "KESTREL_SERVER_PROCESS_GROUP": str(process_group_file),
            "EXPECTED_RECOVERY_DB": str(Path(f"{state}.release-previous") / state.name),
            "RECOVERY_SEEN": str(recovery_seen),
        },
    )

    assert result.returncode != 0
    assert "candidate Kestrel server did not become healthy" in result.stderr
    assert recovery_seen.is_file()
    for suffix, payload in originals.items():
        assert Path(f"{state}{suffix}").read_bytes() == payload
    assert not Path(f"{state}.release-previous").exists()
    assert not staging_root.exists()
    assert not pid_file.exists()
    assert not supervisor_pid_file.exists()
    assert not process_group_file.exists()


@POSIX_SHELL_ONLY
def test_post_unlock_rollback_fails_closed_when_runtime_contender_owns_state(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    state = home / ".nest" / "state" / "agent.db"
    _write_sqlite_marker(state, "original")
    staging_root, candidate = _stage_sqlite_candidate(state, "migrated")
    runtime_lock = state.parent / f".{state.name}.kestrel-runtime-owner.lock"
    memory_lock = home / ".nest" / ".memory.kestrel-memory.lock"
    holder_ready = tmp_path / "contender-ready"
    holder_pid_path = tmp_path / "contender.pid"
    holder_script = tmp_path / "hold_runtime_ownership.py"
    holder_script.write_text(
        "import fcntl\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "from pathlib import Path\n"
        "handles = []\n"
        "for value in sys.argv[1:3]:\n"
        "    path = Path(value)\n"
        "    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)\n"
        "    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)\n"
        "    handle = os.fdopen(descriptor, 'r+')\n"
        "    fcntl.flock(handle, fcntl.LOCK_EX)\n"
        "    handles.append(handle)\n"
        "Path(sys.argv[3]).touch()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    result = _run_installer_function(
        (
            "start_release_install_transaction; "
            "acquire_maintenance_lock; "
            f"INSTALL_STATE_STAGED_ROOT={shlex.quote(str(staging_root))}; "
            f"INSTALL_STATE_STAGED_PATH={shlex.quote(str(candidate))}; "
            "commit_staged_state; "
            "MAINTENANCE_LOCK_RELEASED_FOR_SERVER=1; "
            "release_maintenance_lock; "
            '"$PYTHON_BIN" "$LOCK_HOLDER" "$RUNTIME_LOCK" "$MEMORY_LOCK" '
            '"$HOLDER_READY" >/dev/null 2>&1 & '
            'printf "%s\\n" "$!" >"$HOLDER_PID_PATH"; '
            'for _ in {1..100}; do [[ -f "$HOLDER_READY" ]] && break; sleep 0.05; done; '
            '[[ -f "$HOLDER_READY" ]]; '
            "SERVER_LAUNCH_ATTEMPTED=1; "
            "cleanup_failed_server_launch() { SERVER_LAUNCH_ATTEMPTED=0; return 0; }; "
            "false"
        ),
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=_unused_local_port(),
        extra_env={
            "HOLDER_PID_PATH": str(holder_pid_path),
            "HOLDER_READY": str(holder_ready),
            "LOCK_HOLDER": str(holder_script),
            "MEMORY_LOCK": str(memory_lock),
            "RUNTIME_LOCK": str(runtime_lock),
        },
    )

    try:
        assert result.returncode != 0
        assert "could not be reacquired for rollback" in result.stdout
        with sqlite3.connect(state) as connection:
            assert connection.execute("SELECT value FROM marker").fetchone() == ("migrated",)
        recovery = Path(f"{state}.release-previous")
        assert recovery.is_dir()
        with sqlite3.connect(recovery / state.name) as connection:
            assert connection.execute("SELECT value FROM marker").fetchone() == ("original",)
        assert not staging_root.exists()
    finally:
        if holder_pid_path.is_file():
            holder_pid = int(holder_pid_path.read_text(encoding="ascii").strip())
            try:
                os.kill(holder_pid, 15)
            except ProcessLookupError:
                pass


def test_server_handoff_remains_rollback_owned_until_transaction_acceptance() -> None:
    script = INSTALL.read_text(encoding="utf-8")
    handoff = script.split('if is_true "$KESTREL_START_SERVER"; then', 1)[1]

    release_lock = handoff.index("MAINTENANCE_LOCK_RELEASED_FOR_SERVER=1")
    launch = handoff.index("start_server_detached", release_lock)
    accept = handoff.index("finalize_release_install_transaction", launch)
    clear_server_cleanup = handoff.index("SERVER_LAUNCH_ATTEMPTED=0", accept)
    clear_reacquire = handoff.index("MAINTENANCE_LOCK_RELEASED_FOR_SERVER=0", accept)
    assert release_lock < launch < accept < clear_server_cleanup
    assert accept < clear_reacquire


@POSIX_SHELL_ONLY
def test_candidate_future_state_schema_rolls_back_checkout_and_venv_without_live_mutation(
    tmp_path: Path,
) -> None:
    home = _installed_kestrel_checkout(tmp_path)
    (home / "operator.txt").write_text("operator data at original head\n", encoding="utf-8")
    subprocess.run(["git", "add", "operator.txt"], cwd=home, check=True)
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
            "original operator head",
        ],
        cwd=home,
        check=True,
    )
    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=home, text=True, capture_output=True, check=True
    ).stdout.strip()
    candidate_head = subprocess.run(
        ["git", "rev-parse", "HEAD^"], cwd=home, text=True, capture_output=True, check=True
    ).stdout.strip()
    original_branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=home,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()

    old_venv = home / ".venv"
    old_venv.mkdir()
    old_venv_sentinel = old_venv / "operator-venv"
    old_venv_sentinel.write_bytes(b"byte-identical-old-venv")
    candidate_venv = home / ".candidate-venv"
    candidate_bin = candidate_venv / "bin"
    candidate_bin.mkdir(parents=True)
    invocation_log = home / "candidate-invocations.log"
    fake_agent = candidate_bin / "nest-agent"
    fake_agent.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"log = Path({str(invocation_log)!r})\n"
        "args = sys.argv[1:]\n"
        "log.write_text(log.read_text() + ' '.join(args) + '\\n' if log.exists() else ' '.join(args) + '\\n')\n"
        "def option(name):\n"
        "    return args[args.index(name) + 1]\n"
        "if args and args[0] == 'routines':\n"
        "    from nested_memvid_agent.state_store import AgentStateStore\n"
        "    AgentStateStore(Path(option('--state-path'))).list_routines()\n"
        "elif args and args[0] == 'init':\n"
        "    memory = Path(option('--memory-dir'))\n"
        "    memory.mkdir(parents=True, exist_ok=True)\n"
        "    (memory / 'candidate.mv2').write_bytes(b'candidate')\n",
        encoding="utf-8",
    )
    fake_agent.chmod(0o700)

    live_memory = home / ".nest" / "memory"
    live_memory.mkdir(parents=True)
    memory_sentinel = live_memory / "working.mv2"
    memory_sentinel.write_bytes(b"byte-identical-live-memory")
    live_state = home / ".nest" / "state" / "agent.db"
    live_state.parent.mkdir()
    with sqlite3.connect(live_state) as connection:
        connection.execute(
            "CREATE TABLE schema_version "
            "(id INTEGER PRIMARY KEY, version INTEGER NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_version (id, version, updated_at) VALUES (1, 999999, 'future')"
        )
    live_state.chmod(0o600)
    state_bytes = live_state.read_bytes()
    operator_bytes = (home / "operator.txt").read_bytes()

    result = _run_installer_function(
        (
            "start_release_install_transaction; "
            'RELEASE_EXISTING_CHECKOUT=1; RELEASE_ORIGINAL_HEAD="$ORIGINAL_HEAD"; '
            'RELEASE_ORIGINAL_BRANCH="$ORIGINAL_BRANCH"; '
            'git checkout --detach "$CANDIDATE_HEAD"; '
            'prepare_release_venv_replacement; mv -- "$CANDIDATE_VENV" .venv; '
            "validate_candidate_memory_isolated"
        ),
        home=home,
        pid_file=home / ".nest" / "server.pid",
        port=_unused_local_port(),
        extra_env={
            "CANDIDATE_HEAD": candidate_head,
            "CANDIDATE_VENV": str(candidate_venv),
            "ORIGINAL_BRANCH": original_branch,
            "ORIGINAL_HEAD": original_head,
        },
    )

    assert result.returncode != 0
    assert "newer than supported" in result.stderr
    assert live_state.read_bytes() == state_bytes
    assert memory_sentinel.read_bytes() == b"byte-identical-live-memory"
    assert sorted(path.name for path in live_memory.iterdir()) == ["working.mv2"]
    assert old_venv_sentinel.read_bytes() == b"byte-identical-old-venv"
    assert not (home / ".venv.release-previous").exists()
    assert (home / "operator.txt").read_bytes() == operator_bytes
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=home,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        == original_head
    )
    invocations = invocation_log.read_text(encoding="utf-8")
    assert str(live_state) not in invocations
    assert ".install-canary." in invocations


@POSIX_SHELL_ONLY
def test_installer_maintenance_lock_rejects_active_direct_memvid_owner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    memory = home / ".nest" / "memory"
    memory.mkdir(parents=True)
    sentinel = memory / "working.mv2"
    sentinel.write_bytes(b"live-memory")
    memory_lock = home / ".nest" / ".memory.kestrel-memory.lock"
    memory_lock.touch(mode=0o600)
    ready = tmp_path / "memory-lock-ready"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys, time; from pathlib import Path; "
                "handle=open(sys.argv[1], 'r+'); fcntl.flock(handle, fcntl.LOCK_SH); "
                "Path(sys.argv[2]).touch(); time.sleep(30)"
            ),
            str(memory_lock),
            str(ready),
        ],
        text=True,
    )
    staged = home / ".nest" / ".install-canary.test" / "existing-memory-copy"
    staged.mkdir(parents=True)
    (staged / "working.mv2").write_bytes(b"candidate-memory")
    try:
        _wait_for_file(ready)
        result = _run_installer_function(
            (
                "start_release_install_transaction; acquire_maintenance_lock; "
                f"INSTALL_MEMORY_STAGED_DIR={shlex.quote(str(staged))}; commit_staged_memory"
            ),
            home=home,
            pid_file=home / ".nest" / "server.pid",
            port=_unused_local_port(),
        )

        assert result.returncode != 0
        assert "direct Memvid command" in result.stderr
        assert sentinel.read_bytes() == b"live-memory"
        assert sorted(path.name for path in memory.iterdir()) == ["working.mv2"]
        assert staged.is_dir()
    finally:
        _stop_process(holder)


@POSIX_SHELL_ONLY
def test_installer_rejects_symlinked_runtime_ancestor_before_mutation(tmp_path: Path) -> None:
    home = _installed_kestrel_checkout(tmp_path)
    old_venv = home / ".venv"
    old_venv.mkdir()
    venv_sentinel = old_venv / "operator-venv"
    venv_sentinel.write_bytes(b"unchanged-venv")
    external = tmp_path / "external-runtime"
    external.mkdir()
    external_sentinel = external / "do-not-touch"
    external_sentinel.write_bytes(b"external-data")
    (home / ".nest").symlink_to(external, target_is_directory=True)
    original_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=home, text=True, capture_output=True, check=True
    ).stdout.strip()

    result = _run_install(
        env={
            "KESTREL_HOME": str(home),
            "KESTREL_PYTHON": sys.executable,
            "KESTREL_REPO": str(home),
            "KESTREL_PORT": str(_unused_local_port()),
            "KESTREL_SKIP_WEB": "1",
            "KESTREL_SKIP_SMOKE": "1",
        }
    )

    assert result.returncode != 0
    assert "symbolic-link ancestor" in result.stderr
    assert external_sentinel.read_bytes() == b"external-data"
    assert venv_sentinel.read_bytes() == b"unchanged-venv"
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=home,
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        == original_head
    )


@POSIX_SHELL_ONLY
def test_installer_maintenance_lock_blocks_primary_runtime_contender(tmp_path: Path) -> None:
    home = _installed_kestrel_checkout(tmp_path)
    ready = tmp_path / "installer-lock-ready"
    release = tmp_path / "release-installer-lock"
    env = {
        **_clean_kestrel_env(),
        "KESTREL_HOME": str(home),
        "KESTREL_STATE_PATH": str(home / ".nest" / "state" / "agent.db"),
        "KESTREL_SERVER_PID": str(home / ".nest" / "server.pid"),
        "KESTREL_SERVER_SUPERVISOR_PID": str(home / ".nest" / "server.supervisor.pid"),
        "KESTREL_SERVER_PROCESS_GROUP": str(home / ".nest" / "server.pgid"),
        "KESTREL_SERVER_LOG": str(home / ".nest" / "server.log"),
        "KESTREL_PORT": str(_unused_local_port()),
        "KESTREL_START_SERVER": "0",
        "KESTREL_DRY_RUN": "0",
        "PYTHON_BIN": sys.executable,
        "READY_FILE": str(ready),
        "RELEASE_FILE": str(release),
    }
    holder = subprocess.Popen(
        [
            "bash",
            "-c",
            (
                'source "$1"; start_release_install_transaction; acquire_maintenance_lock; '
                ': >"$READY_FILE"; while [[ ! -e "$RELEASE_FILE" ]]; do sleep 0.05; done; '
                "finalize_release_install_transaction; finish_post_commit_maintenance"
            ),
            "bash",
            str(INSTALL),
        ],
        cwd=home,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    ownership = PrimaryRuntimeOwnership(home / ".nest" / "state" / "agent.db")
    try:
        _wait_for_file(ready)
        with pytest.raises(RuntimeOwnershipError):
            ownership.acquire()
        release.touch()
        stdout, stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, f"{stdout}\n{stderr}"
        ownership.acquire()
        assert ownership.acquired is True
    finally:
        ownership.release()
        release.touch(exist_ok=True)
        _stop_process(holder)


@POSIX_SHELL_ONLY
def test_installer_dry_run_does_not_inspect_or_kill_pid_owner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    unrelated = subprocess.Popen(["sleep", "30"], text=True)
    target = tmp_path / "pid-target"
    pid_file = home / ".nest" / "server.pid"
    pid_file.parent.mkdir()
    try:
        _write_private_pid_file(target, unrelated.pid)
        pid_file.symlink_to(target)

        result = _run_installer_function(
            "require_offline_server_upgrade_preflight",
            home=home,
            pid_file=pid_file,
            port=_unused_local_port(),
            dry_run=True,
        )

        assert result.returncode == 0, result.stderr
        assert unrelated.poll() is None
        assert pid_file.is_symlink()
    finally:
        _stop_process(unrelated)


@POSIX_SHELL_ONLY
def test_fresh_install_preflight_skips_pid_scan_before_home_exists(tmp_path: Path) -> None:
    home = tmp_path / "not-yet-installed"
    marker = tmp_path / "ps-was-called"
    pid_file = home / ".nest" / "server.pid"
    env = {
        **_clean_kestrel_env(),
        "KESTREL_HOME": str(home),
        "KESTREL_SERVER_PID": str(pid_file),
        "KESTREL_SERVER_SUPERVISOR_PID": str(pid_file.with_name("server.supervisor.pid")),
        "KESTREL_SERVER_PROCESS_GROUP": str(pid_file.with_name("server.pgid")),
        "KESTREL_SERVER_LOG": str(pid_file.with_name("server.log")),
        "KESTREL_PORT": str(_unused_local_port()),
        "KESTREL_START_SERVER": "0",
        "KESTREL_DRY_RUN": "0",
        "PYTHON_BIN": sys.executable,
        "PS_MARKER": str(marker),
    }

    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; '
                'ps() { : >"$PS_MARKER"; return 97; }; '
                "require_offline_server_upgrade_preflight"
            ),
            "bash",
            str(INSTALL),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert "No such file or directory" not in result.stderr


@POSIX_SHELL_ONLY
@pytest.mark.skipif(
    os.getenv("RUN_MEMVID_INTEGRATION") != "1" or os.getenv("RUN_INSTALLER_INTEGRATION") != "1",
    reason="requires RUN_MEMVID_INTEGRATION=1 and RUN_INSTALLER_INTEGRATION=1",
)
def test_install_from_local_repo_smoke_with_memvid(tmp_path: Path) -> None:
    source_repo = _current_tree_git_repo(tmp_path)
    external_pythonpath = tmp_path / "external-pythonpath"
    external_pythonpath.mkdir()
    result = _run_install(
        env={
            "KESTREL_HOME": str(tmp_path / "installed"),
            "KESTREL_REPO": str(source_repo),
            "KESTREL_REF": "HEAD",
            "KESTREL_SKIP_WEB": "1",
            "KESTREL_START_SERVER": "0",
            "PYTHONPATH": str(external_pythonpath),
        }
    )

    assert result.returncode == 0, result.stderr
    assert "Kestrel install complete" in result.stdout
    installed_python = tmp_path / "installed" / ".venv" / "bin" / "python"
    dependency_probe = subprocess.run(
        [
            str(installed_python),
            "-c",
            (
                "from importlib.metadata import version; "
                "print(version('openai'), version('anthropic'), version('google-genai'))"
            ),
        ],
        env={**os.environ, "PYTHONPATH": ""},
        text=True,
        capture_output=True,
        check=False,
    )
    assert dependency_probe.returncode == 0, dependency_probe.stderr


def test_upgrade_rollback_uses_positional_memory_backup_id_and_fails_loudly() -> None:
    script = (ROOT / "scripts" / "upgrade-kestrel.sh").read_text(encoding="utf-8")
    restore_line = next(
        line for line in script.splitlines() if 'memory restore "$MEMORY_BACKUP_ID"' in line
    )
    assert "--backup-id" not in restore_line
    assert "|| true" not in restore_line
    assert "restore_failed=1" in script


def test_upgrade_uses_failing_setup_check_and_standard_readiness_port() -> None:
    script = (ROOT / "scripts" / "upgrade-kestrel.sh").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8765/api/health/ready" in script
    assert "http://127.0.0.1:8766" not in script
    assert "product setup --json --check" in script
    assert "NEST_AGENT_API_AUTH_TOKEN_ENV" in script
    assert '"Authorization": f"Bearer {token}"' in script
    assert "curl --fail" not in script


def test_launchd_setup_script_is_executable_and_credential_free() -> None:
    script_path = ROOT / "scripts" / "setup-launchd.sh"
    script = script_path.read_text(encoding="utf-8")

    index_entry = subprocess.run(
        ["git", "ls-files", "--stage", "--", "scripts/setup-launchd.sh"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.split()
    assert index_entry[0] == "100755"
    payload_source = script.split("payload =", maxsplit=1)[1].split("}\n", maxsplit=1)[0]
    assert "EnvironmentVariables" not in payload_source
    assert '[[ "${1:-}" == "--check" ]]' in script
    assert "chmod(plist_path, 0o600)" in script


def test_installer_verifies_runtime_modules_and_web_assets() -> None:
    script = INSTALL.read_text(encoding="utf-8")

    assert "verify_installed_runtime" in script
    assert "Installed checkout is missing the regular installer server supervisor script" in script
    assert 'required=("fastapi","keyring","mcp","memvid_sdk","uvicorn")' in script
    assert "Staged web workbench build is missing index.html" in script
