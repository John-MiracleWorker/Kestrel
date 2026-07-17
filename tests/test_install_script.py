from __future__ import annotations

import hashlib
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "install.sh"
START_TELEGRAM = ROOT / "scripts" / "start-telegram-agent.sh"
START_TELEGRAM_STACK = ROOT / "scripts" / "start-telegram-stack.sh"
SET_TELEGRAM_WEBHOOK = ROOT / "scripts" / "set-telegram-webhook.sh"
TELEGRAM_POLLER = ROOT / "scripts" / "telegram-poller.py"
POSIX_SHELL_ONLY = pytest.mark.skipif(
    os.name == "nt",
    reason="installer and operational shell scripts require macOS, Linux, or WSL",
)


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


@POSIX_SHELL_ONLY
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
        "KESTREL_PORT",
        "KESTREL_DRY_RUN",
    ]:
        assert option in result.stdout


@POSIX_SHELL_ONLY
def test_install_defaults_exclude_development_dependencies() -> None:
    result = _run_install(args=["--help"])

    assert result.returncode == 0
    assert "Defaults to memvid,openai,anthropic,gemini,server,mcp." in result.stdout
    assert "memvid,openai,anthropic,gemini,server,mcp,dev" not in result.stdout


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
    assert ".[memvid,openai,anthropic,gemini,server,mcp]" in result.stdout
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
    staged = tmp_path / "install.sh"
    text = INSTALL.read_text(encoding="utf-8")
    release_base = "https://github.com/example/Kestrel/releases/download/v0.3.0"
    replacements = {
        'KESTREL_REF="${KESTREL_REF:-main}"': 'KESTREL_REF="${KESTREL_REF:-v0.3.0}"',
        'DEFAULT_REQUIREMENTS_URL=""': (
            f'DEFAULT_REQUIREMENTS_URL="{release_base}/requirements-release.txt"'
        ),
        'DEFAULT_WHEEL_URL=""': (
            f'DEFAULT_WHEEL_URL="{release_base}/nested_memvid_agent-0.3.0-py3-none-any.whl"'
        ),
        'DEFAULT_CHECKSUMS_URL=""': (
            f'DEFAULT_CHECKSUMS_URL="{release_base}/SHA256SUMS"'
        ),
    }
    for marker, replacement in replacements.items():
        assert text.count(marker) == 1
        text = text.replace(marker, replacement, 1)
    staged.write_text(text, encoding="utf-8")

    result = _run_install(
        env={"KESTREL_DRY_RUN": "1", "KESTREL_HOME": str(tmp_path / "kestrel-home")},
        install=staged,
    )

    assert result.returncode == 0, result.stderr
    assert "fetch https://github.com/John-MiracleWorker/Kestrel.git v0.3.0" in result.stdout
    assert f"locked requirements: {release_base}/requirements-release.txt" in result.stdout
    assert "verify SHA256SUMS" in result.stdout
    assert result.stdout.index("requirements-release.txt --output") < result.stdout.index(
        "-m venv .venv"
    )
    assert "pip install --require-hashes -r .nest/release/requirements-release.txt" in result.stdout
    assert "pip install --no-deps" in result.stdout
    assert "nested_memvid_agent-0.3.0-py3-none-any.whl" in result.stdout
    assert "Using the workbench bundled in the verified release wheel." in result.stdout
    assert "pip install -e" not in result.stdout
    assert "npm ci" not in result.stdout


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
        "  case \"$1\" in\n"
        "    https://*) url=\"$1\"; shift ;;\n"
        "    --output) output=\"$2\"; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        "cp \"$FIXTURE_DIR/${url##*/}\" \"$output\"\n",
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    if failure_mode == "signal_after_backup":
        fake_mv = fake_bin / "mv"
        fake_mv.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "/bin/mv \"$@\"\n"
            "if [[ ! -e \"$SIGNAL_MARKER\" ]]; then\n"
            "  : >\"$SIGNAL_MARKER\"\n"
            "  kill -TERM \"$PPID\"\n"
            "fi\n",
            encoding="utf-8",
        )
        fake_mv.chmod(0o755)
    release_base = "https://example.invalid/releases/download/v0.3.0"

    result = _run_install(
        env={
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FIXTURE_DIR": str(fixtures),
            "SIGNAL_MARKER": str(tmp_path / "signal-injected"),
            "KESTREL_HOME": str(target),
            "KESTREL_REPO": str(source),
            "KESTREL_REF": "HEAD",
            "KESTREL_PYTHON": sys.executable,
            "KESTREL_REQUIREMENTS_URL": f"{release_base}/{requirements.name}",
            "KESTREL_WHEEL_URL": f"{release_base}/{wheel.name}",
            "KESTREL_CHECKSUMS_URL": f"{release_base}/{checksums.name}",
            "KESTREL_SKIP_WEB": "1",
            "KESTREL_SKIP_SMOKE": "1",
        }
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
def test_install_existing_checkout_preserves_origin_and_avoids_forced_checkout(tmp_path: Path) -> None:
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


def test_install_script_detects_python_311_without_bare_python_default() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "KESTREL_PYTHON" in text
    for candidate in ["python3.13", "python3.12", "python3.11", "/opt/homebrew/bin/python3.11"]:
        assert candidate in text
    assert '"$PYTHON_BIN" -m venv .venv' in text
    assert "python -m venv" not in text
    assert "require_supported_platform" in text
    assert "native Windows is unsupported" in text


def test_install_script_clears_pythonpath_before_creating_the_venv() -> None:
    text = INSTALL.read_text(encoding="utf-8")

    assert "unset PYTHONPATH" in text
    assert text.index("unset PYTHONPATH") < text.index('PYTHON_BIN="$(detect_python)"')


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
    assert 'product setup --json --check' in script
    assert 'NEST_AGENT_API_AUTH_TOKEN_ENV' in script
    assert '"Authorization": f"Bearer {token}"' in script
    assert 'curl --fail' not in script


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
    assert 'required=("fastapi","mcp","memvid_sdk","uvicorn")' in script
    assert "web/dist/index.html" in script
