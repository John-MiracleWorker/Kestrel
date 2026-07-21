from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import nested_memvid_agent.mcp_manager as mcp_module
import nested_memvid_agent.private_directory as private_directory_module
from nested_memvid_agent.capability_policy import CapabilityPolicy, parent_resource_digest
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.event_bus import RunEventBus
from nested_memvid_agent.mcp_manager import (
    MCPLaunchIdentityError,
    MCPManager,
    MCPServerConfig,
    mcp_sensitive_material_transition,
)
from nested_memvid_agent.plugin_manager import (
    GitHubPluginSource,
    PluginError,
    PluginManager,
)
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.runtime_models import ToolSpec
from nested_memvid_agent.secret_broker import SecretBroker
from nested_memvid_agent.skill_manager import SkillManager
from nested_memvid_agent.state_store import AgentStateStore


class _FakeFetcher:
    def __init__(self, source: Path) -> None:
        self.source = source

    def fetch(
        self,
        source: GitHubPluginSource,
        destination: Path,
        ref: str | None = None,
    ) -> str:
        del source, ref
        shutil.copytree(self.source, destination)
        return "a" * 40


def test_launch_artifact_byte_io_uses_binary_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "server.py"
    payload = b"#!\r\nprint('literal')\n\x1a\x00"
    source.write_bytes(payload)
    snapshot_root = tmp_path / "snapshots"
    snapshot_root.mkdir(mode=0o700)
    destination = snapshot_root / "server.py"
    synthetic_binary_flag = 1 << 29
    native_binary_flag = getattr(os, "O_BINARY", 0)
    real_open = os.open
    observed_flags: list[int] = []

    def open_without_synthetic_flag(
        path: os.PathLike[str] | str,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if Path(path) in {source, destination}:
            observed_flags.append(flags)
        native_flags = (flags & ~synthetic_binary_flag) | native_binary_flag
        return real_open(path, native_flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(mcp_module.os, "O_BINARY", synthetic_binary_flag, raising=False)
    monkeypatch.setattr(mcp_module.os, "open", open_without_synthetic_flag)
    monkeypatch.setattr(
        mcp_module,
        "_assert_private_snapshot_permissions",
        lambda _path: None,
    )

    assert mcp_module._launch_file_has_shebang(source) is True
    assert mcp_module._hash_launch_file(source, label="launch artifact") == (
        "sha256:" + mcp_module.hashlib.sha256(payload).hexdigest()
    )
    mcp_module._copy_private_launch_file(source, destination, executable=False)

    assert destination.read_bytes() == payload
    assert len(observed_flags) == 4
    assert all(flags & synthetic_binary_flag for flags in observed_flags)


def _copy_native_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    source = Path(shutil.which("true") or sys.executable).resolve()
    shutil.copyfile(source, path)
    path.chmod(0o755)
    return path


def _write_static_plugin(repo: Path) -> None:
    repo.mkdir()
    (repo / "server.js").write_text('import "./helper.js";\n', encoding="utf-8")
    (repo / "helper.js").write_text("// reviewed helper v1\n", encoding="utf-8")
    (repo / "kestrel.plugin.json").write_text(
        json.dumps(
            {
                "id": "artifact",
                "name": "Artifact MCP",
                "description": "Artifact identity fixture.",
                "mcp_servers": [
                    {
                        "id": "node",
                        "transport": "stdio",
                        "command": "node",
                        "args": ["server.js"],
                        "secret_env": {"TOKEN": "secret://plugin-token"},
                        "tools": [{"name": "echo", "description": "Echo."}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _static_tool() -> list[dict[str, object]]:
    return [{"name": "echo", "description": "Echo."}]


def _windows_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        pytest.skip("Windows junction creation is unavailable on this runner")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction semantics")
def test_mcp_launch_tree_rejects_junction_before_resolving_it(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "server.py"
    sentinel.write_text("# untouched\n", encoding="utf-8")
    junction = tmp_path / "plugin-tree"
    _windows_junction(junction, outside)

    try:
        with pytest.raises(MCPLaunchIdentityError, match="reparse point"):
            mcp_module._resolve_launch_tree(str(junction))
        assert sentinel.read_text(encoding="utf-8") == "# untouched\n"
    finally:
        if os.path.lexists(junction):
            os.rmdir(junction)


def test_local_launch_artifact_parser_accepts_windows_drives_and_rejects_remote_urls() -> None:
    windows_path = r"C:\Users\operator\server.py"

    assert (
        mcp_module._local_launch_artifact_path(windows_path, label="Python script")
        == windows_path
    )
    with pytest.raises(MCPLaunchIdentityError, match="must be a local file"):
        mcp_module._local_launch_artifact_path(
            "https://example.invalid/server.py",
            label="Python script",
        )
    with pytest.raises(MCPLaunchIdentityError, match="must be a local file"):
        mcp_module._local_launch_artifact_path(
            "file://remote-host/server.py",
            label="Python script",
        )


def test_path_drift_after_approval_fails_before_secret_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable_name = "fixture-mcp.exe" if os.name == "nt" else "fixture-mcp"
    first = _copy_native_executable(tmp_path / "first" / executable_name)
    second = _copy_native_executable(tmp_path / "second" / executable_name)
    resolved_secrets: list[str] = []
    monkeypatch.setenv("PATH", str(first.parent))
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(
        state,
        secret_resolver=lambda ref: resolved_secrets.append(ref) or "opaque-secret",
    )
    manager.add_server(
        {
            "id": "path-drift",
            "transport": "stdio",
            "command": executable_name,
            "secret_env": {"TOKEN": "secret://mcp-token"},
            "tools": _static_tool(),
        }
    )
    approved = manager.approve_server_connect("path-drift")
    assert approved["vetting"]["stdio_launch_resource"]["identity"]["executable_path"] == str(
        first.resolve()
    )

    monkeypatch.setenv("PATH", str(second.parent))
    result = manager.connect_server("path-drift")
    persisted = state.get_mcp_server("path-drift")

    assert result["ok"] is False
    assert "launch artifact changed" in result["message"]
    assert resolved_secrets == []
    assert persisted["vetting"].get("connect_approved") is not True
    assert persisted["vetting"].get("connect_approved_launch_digest") is None


def test_script_mutation_revokes_connect_approval(tmp_path: Path) -> None:
    script = tmp_path / "server.py"
    script.write_text("# reviewed v1\n", encoding="utf-8")
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    manager.add_server(
        {
            "id": "script-drift",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )
    manager.approve_server_connect("script-drift")

    script.write_text("# unapproved v2\n", encoding="utf-8")
    result = manager.connect_server("script-drift")

    assert result["ok"] is False
    assert "launch artifact changed" in result["message"]
    assert state.get_mcp_server("script-drift")["vetting"].get("connect_approved") is not True


@pytest.mark.parametrize(
    ("command", "args", "message"),
    [
        ("npx", ["@modelcontextprotocol/server-filesystem"], "package runners"),
        ("npx", ["@modelcontextprotocol/server-filesystem@1.0.0"], "package runners"),
        ("uvx", ["mcp-server-fetch==1.0.0"], "package runners"),
        (sys.executable, ["-m", "example_mcp.server"], "python -m"),
    ],
)
def test_mutable_package_and_python_module_launchers_are_rejected(
    tmp_path: Path,
    command: str,
    args: list[str],
    message: str,
) -> None:
    manager = MCPManager(AgentStateStore(tmp_path / "state.db"))

    with pytest.raises(ValueError, match=message):
        manager.add_server(
            {
                "id": "mutable-launch",
                "transport": "stdio",
                "command": command,
                "args": args,
            }
        )


def test_plugin_rejects_package_runner_even_with_version_pin(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "kestrel.plugin.json").write_text(
        json.dumps(
            {
                "id": "unpinned",
                "name": "Unpinned",
                "description": "Must be rejected.",
                "mcp_servers": [
                    {
                        "id": "runner",
                        "transport": "stdio",
                        "command": "npx",
                        "args": ["@modelcontextprotocol/server-filesystem@1.0.0"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manager = PluginManager(
        tmp_path / "plugins",
        AgentStateStore(tmp_path / "state.db"),
        fetcher=_FakeFetcher(repo),
    )

    with pytest.raises(PluginError, match="package runners"):
        manager.install("owner/unpinned")


def test_windows_acl_seam_rejects_weak_nested_snapshot_before_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "snapshot"
    nested = root / "nested"
    nested.mkdir(parents=True, mode=0o700)
    (nested / "server.py").write_text("# reviewed\n", encoding="utf-8")
    hash_called = False

    def validate(path: Path) -> None:
        if path == nested:
            raise private_directory_module.PrivateDirectoryError(
                "private_directory_windows_trustee_unsafe"
            )

    def unexpected_hash(_root: Path) -> str:
        nonlocal hash_called
        hash_called = True
        return "sha256:unreachable"

    monkeypatch.setattr(mcp_module, "_uses_windows_snapshot_acls", lambda: True)
    monkeypatch.setattr(mcp_module, "validate_owner_private_directory", validate)
    monkeypatch.setattr(mcp_module, "_hash_launch_tree", unexpected_hash)

    with pytest.raises(MCPLaunchIdentityError) as raised:
        mcp_module._hash_private_launch_tree(root)

    assert raised.value.code == "snapshot_permissions"
    assert hash_called is False


def test_windows_acl_seam_creates_reuses_and_cleans_protected_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "server.py"
    source.write_text("# reviewed\n", encoding="utf-8")
    base = tmp_path / "mcp_artifacts"
    destination = base / ("a" * 64) / source.name
    created: list[Path] = []
    validated: list[Path] = []

    def atomic_create(path: Path) -> Path:
        path.mkdir(mode=0o700)
        created.append(path)
        return path

    def validate(path: Path) -> None:
        assert path.is_dir()
        validated.append(path)

    monkeypatch.setattr(mcp_module, "_uses_windows_snapshot_acls", lambda: True)
    monkeypatch.setattr(mcp_module, "create_owner_private_directory", atomic_create)
    monkeypatch.setattr(mcp_module, "validate_owner_private_directory", validate)

    mcp_module._ensure_private_snapshot_directory(
        base,
        allow_harden_empty=True,
        harden_existing_posix=True,
    )
    expected = mcp_module._hash_launch_file(source, label="launch artifact")
    for _ in range(2):
        mcp_module._ensure_private_file_snapshot(
            source,
            destination,
            expected_digest=expected,
            executable=False,
        )

    assert destination.read_text(encoding="utf-8") == "# reviewed\n"
    assert created == [base, destination.parent]
    assert base in validated
    assert destination.parent in validated
    mcp_module._remove_private_snapshot_tree(destination.parent)
    assert not destination.parent.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission semantics required")
def test_snapshot_base_retains_posix_hardening_without_repairing_digest_roots(
    tmp_path: Path,
) -> None:
    base = tmp_path / "mcp_artifacts"
    base.mkdir(mode=0o755)

    mcp_module._ensure_private_snapshot_directory(
        base,
        allow_harden_empty=True,
        harden_existing_posix=True,
    )

    assert base.stat().st_mode & 0o777 == 0o700
    digest = base / ("a" * 64)
    digest.mkdir(mode=0o755)
    with pytest.raises(MCPLaunchIdentityError) as raised:
        mcp_module._ensure_private_snapshot_directory(
            digest,
            allow_harden_empty=True,
            harden_existing_posix=False,
        )
    assert raised.value.code == "snapshot_permissions"


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL semantics required")
@pytest.mark.parametrize("weak_trustee", ["WD", "BU"])
def test_windows_nonempty_weak_snapshot_acl_fails_closed(
    tmp_path: Path,
    weak_trustee: str,
) -> None:
    base = tmp_path / f"weak-{weak_trustee}"
    current_sid = private_directory_module._windows_current_user_sid()
    protection = "P" if weak_trustee == "WD" else ""
    weak_sddl = (
        f"O:{current_sid}D:{protection}"
        f"(A;OICI;FA;;;{current_sid})"
        f"(A;OICI;FA;;;{weak_trustee})"
    )
    private_directory_module._windows_create_directory_with_sddl(base, weak_sddl)
    (base / "occupied.txt").write_text("untrusted inheritance\n", encoding="utf-8")
    try:
        with pytest.raises(MCPLaunchIdentityError) as raised:
            mcp_module._ensure_private_snapshot_directory(
                base,
                allow_harden_empty=True,
                harden_existing_posix=True,
            )
        assert raised.value.code == "snapshot_permissions"
    finally:
        shutil.rmtree(base)


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL semantics required")
def test_windows_protected_snapshot_creates_reuses_and_cleans(
    tmp_path: Path,
) -> None:
    source = tmp_path / "server.py"
    source.write_text("# reviewed\n", encoding="utf-8")
    base = tmp_path / "mcp_artifacts"
    destination = base / ("a" * 64) / source.name

    mcp_module._ensure_private_snapshot_directory(
        base,
        allow_harden_empty=True,
        harden_existing_posix=True,
    )
    expected = mcp_module._hash_launch_file(source, label="launch artifact")
    for _ in range(2):
        mcp_module._ensure_private_file_snapshot(
            source,
            destination,
            expected_digest=expected,
            executable=False,
        )

    private_directory_module.validate_owner_private_directory(base)
    private_directory_module.validate_owner_private_directory(destination.parent)
    assert destination.read_text(encoding="utf-8") == "# reviewed\n"
    mcp_module._remove_private_snapshot_tree(destination.parent)
    assert not destination.parent.exists()


def test_private_tree_snapshot_failure_cleans_protected_temporary_directory(
    tmp_path: Path,
) -> None:
    source = tmp_path / "plugin"
    source.mkdir()
    (source / "server.py").write_text("# reviewed\n", encoding="utf-8")
    base = tmp_path / "mcp_artifacts"
    mcp_module._ensure_private_snapshot_directory(
        base,
        allow_harden_empty=True,
        harden_existing_posix=True,
    )
    destination = base / ("a" * 64)

    with pytest.raises(MCPLaunchIdentityError, match="changed before"):
        mcp_module._ensure_private_tree_snapshot(
            source,
            destination,
            expected_digest="sha256:" + "0" * 64,
        )

    assert list(base.glob(".mcp-snapshot-*")) == []
    assert not destination.exists()


def test_unchanged_identity_connects_and_launches_exact_resolved_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    manager.add_server(
        {
            "id": "stable",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
        }
    )
    approved = manager.approve_server_connect("stable")
    captured: dict[str, Any] = {}

    class _Parameters:
        def __init__(
            self,
            *,
            command: str,
            args: list[str],
            env: dict[str, str] | None,
            cwd: str,
        ) -> None:
            captured.update(command=command, args=args, env=env, cwd=cwd)

    class _StreamContext:
        async def __aenter__(self) -> tuple[object, object]:
            captured["stream_entered"] = True
            return object(), object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class _Session:
        def __init__(self, read_stream: object, write_stream: object) -> None:
            del read_stream, write_stream

        async def __aenter__(self) -> _Session:
            return self

        async def initialize(self) -> None:
            return None

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    mcp_sdk = SimpleNamespace(
        StdioServerParameters=_Parameters,
        ClientSession=_Session,
    )
    stdio_sdk = SimpleNamespace(stdio_client=lambda _params: _StreamContext())
    real_import = mcp_module.import_module

    def fake_import(name: str) -> Any:
        if name == "mcp":
            return mcp_sdk
        if name == "mcp.client.stdio":
            return stdio_sdk
        return real_import(name)

    monkeypatch.setattr(mcp_module, "import_module", fake_import)
    server = mcp_module._server_from_state(approved)
    context = mcp_module._session_context(server)

    async def exercise() -> None:
        await context.__aenter__()
        await context.__aexit__(None, None, None)

    asyncio.run(exercise())

    snapshot = approved["vetting"]["stdio_launch_snapshot"]
    snapshot_root = Path(str(snapshot["root"]))
    snapshot_artifact = Path(str(snapshot["artifact_path"]))
    if os.name != "nt":
        assert snapshot_root.stat().st_mode & 0o077 == 0
        assert snapshot_artifact.stat().st_mode & 0o077 == 0
    else:
        assert snapshot_root.is_dir()
        assert snapshot_artifact.is_file()
    assert captured["command"] == str(Path(sys.executable).absolute())
    assert captured["args"] == [snapshot["artifact_path"]]
    assert captured["cwd"] == snapshot["root"]
    assert Path(str(captured["args"][0])).read_text(encoding="utf-8") == "# reviewed\n"
    assert captured["stream_entered"] is True


@pytest.mark.skipif(os.name == "nt", reason="portable POSIX permission mutation")
def test_launch_adjacent_snapshot_permission_change_blocks_process_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    manager.add_server(
        {
            "id": "acl-launch-race",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
        }
    )
    approved = manager.approve_server_connect("acl-launch-race")
    server = mcp_module._server_from_state(approved)
    snapshot_root = Path(
        str(approved["vetting"]["stdio_launch_snapshot"]["root"])
    )
    observations = {"parameters": False, "stdio_client": False}

    class _Parameters:
        def __init__(
            self,
            *,
            command: str,
            args: list[str],
            env: dict[str, str] | None,
            cwd: str,
        ) -> None:
            del command, args, env, cwd
            observations["parameters"] = True
            snapshot_root.chmod(0o755)

    def forbidden_stdio_client(_params: object) -> object:
        observations["stdio_client"] = True
        raise AssertionError("process creation must not be reached")

    mcp_sdk = SimpleNamespace(StdioServerParameters=_Parameters)
    stdio_sdk = SimpleNamespace(stdio_client=forbidden_stdio_client)
    real_import = mcp_module.import_module

    def fake_import(name: str) -> Any:
        if name == "mcp":
            return mcp_sdk
        if name == "mcp.client.stdio":
            return stdio_sdk
        return real_import(name)

    monkeypatch.setattr(mcp_module, "import_module", fake_import)
    context = mcp_module._session_context(
        server,
        snapshot_root=manager._snapshot_root(),
    )
    try:
        with pytest.raises(MCPLaunchIdentityError) as raised:
            asyncio.run(context.__aenter__())
    finally:
        snapshot_root.chmod(0o700)

    assert raised.value.code == "snapshot_permissions"
    assert observations == {"parameters": True, "stdio_client": False}


def test_validation_to_launch_mutation_never_reaches_process_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    manager.add_server(
        {
            "id": "launch-race",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
        }
    )
    server = mcp_module._server_from_state(manager.approve_server_connect("launch-race"))

    def mutate_during_validation(
        _server: object,
        *,
        secret_resolver: object = None,
    ) -> dict[str, str]:
        del secret_resolver
        script.write_text("# changed after validation\n", encoding="utf-8")
        return {}

    captured: dict[str, Any] = {}

    class _Parameters:
        def __init__(
            self,
            *,
            command: str,
            args: list[str],
            env: dict[str, str] | None,
            cwd: str,
        ) -> None:
            captured.update(command=command, args=args, env=env, cwd=cwd)

    class _StreamContext:
        async def __aenter__(self) -> tuple[object, object]:
            captured["launched_content"] = Path(captured["args"][0]).read_text(encoding="utf-8")
            return object(), object()

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    class _Session:
        def __init__(self, read_stream: object, write_stream: object) -> None:
            del read_stream, write_stream

        async def __aenter__(self) -> _Session:
            return self

        async def initialize(self) -> None:
            return None

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

    mcp_sdk = SimpleNamespace(
        StdioServerParameters=_Parameters,
        ClientSession=_Session,
    )
    stdio_sdk = SimpleNamespace(stdio_client=lambda _params: _StreamContext())
    real_import = mcp_module.import_module

    def fake_import(name: str) -> Any:
        if name == "mcp":
            return mcp_sdk
        if name == "mcp.client.stdio":
            return stdio_sdk
        return real_import(name)

    monkeypatch.setattr(mcp_module, "_runtime_env", mutate_during_validation)
    monkeypatch.setattr(mcp_module, "import_module", fake_import)
    context = mcp_module._session_context(server)

    async def exercise() -> None:
        await context.__aenter__()
        await context.__aexit__(None, None, None)

    asyncio.run(exercise())

    assert script.read_text(encoding="utf-8") == "# changed after validation\n"
    assert captured["launched_content"] == "# reviewed\n"
    assert captured["args"] == [server.vetting["stdio_launch_snapshot"]["artifact_path"]]


def test_plugin_artifact_drift_blocks_capability_approval_and_sync_clears_connect(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _write_static_plugin(repo)
    state = AgentStateStore(tmp_path / "state.db")
    plugins = PluginManager(
        tmp_path / "plugins",
        state,
        fetcher=_FakeFetcher(repo),
    )
    plugins.install("owner/artifact", enable=True)
    server_id = "plugin.artifact.node"
    row = state.get_mcp_server(server_id)
    baseline = parent_resource_digest(state, "mcp_server", server_id)
    state.set_capability_override(
        "mcp_server",
        server_id,
        True,
        expected_revision=0,
        default_enabled=True,
        resource_digest=baseline,
    )
    mcp = MCPManager(state)
    approved = mcp.approve_server_connect(server_id)
    snapshot_root = Path(approved["vetting"]["stdio_launch_snapshot"]["root"])
    assert (snapshot_root / "helper.js").read_text(encoding="utf-8") == ("// reviewed helper v1\n")

    identity = row["vetting"]["stdio_launch_resource"]["identity"]
    artifact = Path(str(identity["artifact_locator"]))
    helper = artifact.parent / "helper.js"
    helper.write_text("// unapproved helper v2\n", encoding="utf-8")
    policy = CapabilityPolicy(state, AgentConfig())
    decision = policy.parent_decision(
        "mcp_server",
        server_id,
        entity_enabled=True,
    )

    assert decision.effective_enabled is False
    assert decision.blocked_by == ("resource_changed",)
    plugins.sync_all()
    synced = state.get_mcp_server(server_id)
    assert synced["vetting"].get("connect_approved") is not True
    assert synced["vetting"].get("connect_approved_launch_digest") is None


def test_tool_approval_digest_changes_with_mcp_artifact(tmp_path: Path) -> None:
    script = tmp_path / "server.py"
    script.write_text("# reviewed v1\n", encoding="utf-8")
    config = AgentConfig(
        backend="memory",
        provider="mock",
        state_path=tmp_path / "state.db",
        memory_dir=tmp_path / "memory",
        log_dir=tmp_path / "logs",
        skills_dir=tmp_path / "skills",
        plugins_dir=tmp_path / "plugins",
    )
    state = AgentStateStore(config.state_path)
    mcp = MCPManager(state)
    mcp.add_server(
        {
            "id": "approval-digest",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )
    runs = RunManager(
        config=config,
        state=state,
        events=RunEventBus(state),
        mcp=mcp,
        skills=SkillManager(config.skills_dir, state),
        plugins=PluginManager(config.plugins_dir, state),
        recover_startup_work=False,
        auto_start=False,
    )
    spec = ToolSpec(
        name="mcp.approval-digest.echo",
        description="Echo.",
        parameters={"type": "object"},
        risk="medium",
        requires_approval=True,
        source="mcp",
        server_id="approval-digest",
        capabilities=("mcp",),
    )
    before = runs.tool_resource_digest(spec)
    script.write_text("# unapproved v2\n", encoding="utf-8")
    after = runs.tool_resource_digest(spec)

    assert after != before
    runs.shutdown(timeout_seconds=1)


@pytest.mark.parametrize("backend", ["", "json", "file", "local"])
def test_raw_json_vault_inside_workspace_blocks_stdio_without_secret_leak(
    tmp_path: Path,
    backend: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    vault = workspace / "config" / "runtime-vault.json"
    vault.parent.mkdir()
    raw_secret = "opaque-vault-secret-that-must-not-leak"
    vault.write_text(json.dumps({"token": raw_secret}), encoding="utf-8")
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    resolved: list[str] = []
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(
        state,
        workspace=workspace,
        secret_store_path=vault,
        secret_backend=backend,
        secret_resolver=lambda ref: resolved.append(ref) or raw_secret,
    )
    manager.add_server(
        {
            "id": "vault-scope",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "secret_env": {"TOKEN": "secret://vault-token"},
        }
    )

    with pytest.raises(MCPLaunchIdentityError) as raised:
        manager.approve_server_connect("vault-scope")

    assert "raw JSON secret vault" in str(raised.value)
    assert raw_secret not in str(raised.value)
    assert str(vault) not in str(raised.value)
    assert resolved == []


def test_secret_bearing_standalone_script_is_rejected_before_resolution(
    tmp_path: Path,
) -> None:
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    resolved: list[str] = []
    manager = MCPManager(
        AgentStateStore(tmp_path / "state.db"),
        secret_backend="keyring",
        secret_resolver=lambda ref: resolved.append(ref) or "opaque-secret",
    )
    manager.add_server(
        {
            "id": "standalone-secret-script",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "secret_env": {"TOKEN": "secret://standalone-token"},
        }
    )

    with pytest.raises(MCPLaunchIdentityError, match="installed plugin"):
        manager.approve_server_connect("standalone-secret-script")

    assert resolved == []


def test_secret_bearing_aliased_interpreter_is_rejected_before_resolution(
    tmp_path: Path,
) -> None:
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    interpreter_alias = tmp_path / "reviewed-runtime"
    try:
        interpreter_alias.symlink_to(Path(sys.executable).resolve())
    except OSError:
        pytest.skip("Executable symlinks are unavailable on this platform.")
    resolved: list[str] = []
    manager = MCPManager(
        AgentStateStore(tmp_path / "state.db"),
        secret_backend="keyring",
        secret_resolver=lambda ref: resolved.append(ref) or "opaque-secret",
    )
    manager.add_server(
        {
            "id": "aliased-secret-script",
            "transport": "stdio",
            "command": str(interpreter_alias),
            "args": [str(script)],
            "secret_env": {"TOKEN": "secret://standalone-token"},
        }
    )

    with pytest.raises(MCPLaunchIdentityError, match="(?:installed plugin|reparse point)"):
        manager.approve_server_connect("aliased-secret-script")

    assert resolved == []


def test_secret_bearing_direct_shebang_script_is_rejected_before_resolution(
    tmp_path: Path,
) -> None:
    script = tmp_path / "server"
    script.write_text("#!/usr/bin/env python3\nprint('unreachable')\n", encoding="utf-8")
    script.chmod(0o755)
    resolved: list[str] = []
    manager = MCPManager(
        AgentStateStore(tmp_path / "state.db"),
        secret_backend="keyring",
        secret_resolver=lambda ref: resolved.append(ref) or "opaque-secret",
    )
    manager.add_server(
        {
            "id": "direct-shebang-secret-script",
            "transport": "stdio",
            "command": str(script),
            "secret_env": {"TOKEN": "secret://standalone-token"},
        }
    )

    with pytest.raises(MCPLaunchIdentityError, match="shebang script"):
        manager.approve_server_connect("direct-shebang-secret-script")

    assert resolved == []


@pytest.mark.parametrize("backend", ["", "JSON", " file ", "LoCaL"])
def test_existing_raw_vault_outside_workspace_blocks_all_backend_aliases(
    tmp_path: Path,
    backend: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside_vault = tmp_path / "outside" / "outside-vault.json"
    outside_vault.parent.mkdir()
    outside_vault.write_text("{}", encoding="utf-8")
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    manager = MCPManager(
        AgentStateStore(tmp_path / "state.db"),
        workspace=workspace,
        secret_store_path=outside_vault,
        secret_backend=backend,
    )
    manager.add_server(
        {
            "id": "outside-raw-vault",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )

    with pytest.raises(MCPLaunchIdentityError, match="raw JSON secret vault"):
        manager.approve_server_connect("outside-raw-vault")


@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_existing_raw_vault_path_alias_blocks_stdio(
    tmp_path: Path,
    alias_kind: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    vault = tmp_path / "outside" / "vault.json"
    vault.parent.mkdir()
    vault.write_text("{}", encoding="utf-8")
    alias = tmp_path / "aliases" / "configured-vault.json"
    alias.parent.mkdir()
    try:
        if alias_kind == "symlink":
            alias.symlink_to(vault)
        else:
            alias.hardlink_to(vault)
    except OSError:
        pytest.skip(f"{alias_kind} aliases are unavailable on this platform.")
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    manager = MCPManager(
        AgentStateStore(tmp_path / "state.db"),
        workspace=workspace,
        secret_store_path=alias,
        secret_backend="json",
    )
    manager.add_server(
        {
            "id": "aliased-raw-vault",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )

    with pytest.raises(MCPLaunchIdentityError, match="raw JSON secret vault"):
        manager.approve_server_connect("aliased-raw-vault")


@pytest.mark.parametrize("operation", ["approve", "connect", "health", "sync", "invoke"])
def test_existing_raw_vault_blocks_every_stdio_lifecycle_entry_point(
    tmp_path: Path,
    operation: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    vault = tmp_path / "outside-vault.json"
    vault.write_text("{}", encoding="utf-8")
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(
        state,
        workspace=workspace,
        secret_store_path=vault,
        secret_backend="json",
    )
    manager.add_server(
        {
            "id": "raw-vault-lifecycle",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )

    if operation == "approve":
        with pytest.raises(MCPLaunchIdentityError) as raised:
            manager.approve_server_connect("raw-vault-lifecycle")
        message = str(raised.value)
    elif operation == "connect":
        message = manager.connect_server("raw-vault-lifecycle")["message"]
    elif operation == "health":
        message = manager.server_health("raw-vault-lifecycle")["message"]
    elif operation == "sync":
        message = manager.sync_server("raw-vault-lifecycle")["error"]
    else:
        execution = manager.invoke_tool("raw-vault-lifecycle", "echo", {})
        assert execution.success is False
        message = execution.content

    assert "raw JSON secret vault" in message
    assert str(vault) not in message


@pytest.mark.parametrize("backend", ["", "json", "file", "local"])
def test_nonexistent_raw_vault_does_not_block_or_get_created_by_mcp(
    tmp_path: Path,
    backend: str,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    vault = tmp_path / "outside" / "not-created.json"
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    manager = MCPManager(
        AgentStateStore(tmp_path / "state.db"),
        workspace=workspace,
        secret_store_path=vault,
        secret_backend=backend,
    )
    manager.add_server(
        {
            "id": "missing-raw-vault",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )

    approved = manager.approve_server_connect("missing-raw-vault")
    connected = manager.connect_server("missing-raw-vault")

    assert approved["vetting"]["connect_approved"] is True
    assert connected["ok"] is True
    assert not vault.exists()


def test_keyring_metadata_does_not_block_stdio_approval(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = workspace / "keyring-metadata.json"
    metadata.write_text("{}", encoding="utf-8")
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(
        state,
        workspace=workspace,
        secret_store_path=metadata,
        secret_backend=" KeyRing ",
    )
    manager.add_server(
        {
            "id": "allowed-keyring",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )

    approved = manager.approve_server_connect("allowed-keyring")

    assert approved["vetting"]["connect_approved"] is True


def test_keyring_records_block_stdio_without_resolving_values(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    metadata = workspace / "keyring-metadata.json"
    metadata.write_text(
        json.dumps(
            {
                "backend": "keyring",
                "secrets": {
                    "provider": {
                        "id": "provider",
                        "keyring_username": "provider:v1",
                        "keyring_state": "active",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    resolved: list[str] = []
    manager = MCPManager(
        AgentStateStore(tmp_path / "state.db"),
        workspace=workspace,
        secret_store_path=metadata,
        secret_backend="keyring",
        secret_resolver=lambda ref: resolved.append(ref) or "unreachable",
    )
    manager.add_server(
        {
            "id": "blocked-keyring",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )

    with pytest.raises(MCPLaunchIdentityError) as raised:
        manager.approve_server_connect("blocked-keyring")

    assert raised.value.code == "keyring_secret_store_blocks_stdio"
    assert "not same-account process isolation" in str(raised.value)
    assert resolved == []


def test_repair_trust_material_blocks_stdio(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    trust_root = workspace / ".nest"
    trust_root.mkdir(parents=True)
    key = trust_root / "repair_receipt_signing.v2.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    script = tmp_path / "server.py"
    script.write_text("# reviewed\n", encoding="utf-8")
    manager = MCPManager(
        AgentStateStore(tmp_path / "state.db"),
        workspace=workspace,
        secret_store_path=workspace / "missing-vault.json",
        secret_backend="json",
    )
    manager.add_server(
        {
            "id": "blocked-repair-trust",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(script)],
            "tools": _static_tool(),
        }
    )

    with pytest.raises(MCPLaunchIdentityError) as raised:
        manager.approve_server_connect("blocked-repair-trust")

    assert raised.value.code == "repair_trust_blocks_stdio"


def test_sensitive_material_transition_closes_stdio_before_vault_publication(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "secrets.json"
    manager = MCPManager(AgentStateStore(tmp_path / "state.db"))
    observations: list[tuple[float, bool]] = []

    class _Worker:
        server = SimpleNamespace(transport="stdio")

        def close(self, *, timeout: float) -> bool:
            observations.append((timeout, vault.exists()))
            return True

    manager._sessions["active-stdio"] = _Worker()  # type: ignore[assignment]

    try:
        with mcp_sensitive_material_transition() as closed:
            SecretBroker(vault).store_secret(
                name="TOKEN",
                purpose="lifecycle coupling",
                value="material-created-after-close",
            )
    finally:
        manager.shutdown()

    assert closed == ("active-stdio",)
    assert observations == [(manager.timeout_seconds, False)]
    assert vault.is_file()
    assert "active-stdio" not in manager._sessions


def test_sensitive_material_transition_aborts_when_stdio_close_is_unverified(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "secrets.json"
    manager = MCPManager(AgentStateStore(tmp_path / "state.db"))
    close_attempts = 0

    class _Worker:
        server = SimpleNamespace(transport="stdio")

        def close(self, *, timeout: float) -> bool:
            nonlocal close_attempts
            del timeout
            close_attempts += 1
            return close_attempts > 1

    manager._sessions["stuck-stdio"] = _Worker()  # type: ignore[assignment]

    try:
        with pytest.raises(MCPLaunchIdentityError) as raised:
            with mcp_sensitive_material_transition():
                SecretBroker(vault).store_secret(
                    name="TOKEN",
                    purpose="must not publish",
                    value="must-not-be-written",
                )
    finally:
        manager.shutdown()

    assert raised.value.code == "mcp_stdio_quiesce_failed"
    assert not vault.exists()


def test_manager_shutdown_reports_unverified_worker_and_keeps_it_tracked(
    tmp_path: Path,
) -> None:
    manager = MCPManager(AgentStateStore(tmp_path / "state.db"))
    allow_close = False

    class _Worker:
        def close(self, *, timeout: float) -> bool:
            del timeout
            return allow_close

    worker = _Worker()
    manager._sessions["stuck-stdio"] = worker  # type: ignore[assignment]

    assert manager.shutdown() is False
    assert manager._sessions["stuck-stdio"] is worker

    allow_close = True
    assert manager.shutdown() is True
    assert "stuck-stdio" not in manager._sessions


def test_failed_public_disconnect_and_reconfigure_keep_stdio_worker_tracked(
    tmp_path: Path,
) -> None:
    state = AgentStateStore(tmp_path / "state.db")
    manager = MCPManager(state)
    original_script = tmp_path / "original_server.py"
    changed_script = tmp_path / "changed_server.py"
    original_script.write_text("print('fixture')\n", encoding="utf-8")
    changed_script.write_text("print('changed')\n", encoding="utf-8")
    manager.add_server(
        {
            "id": "stuck-stdio",
            "name": "Stuck stdio",
            "transport": "stdio",
            "command": sys.executable,
            "args": [str(original_script)],
            "enabled": True,
            "tools": _static_tool(),
        }
    )
    row = state.get_mcp_server("stuck-stdio")
    row["status"] = "online"
    row["session_state"] = "connected"
    state.upsert_mcp_server(row)
    allow_close = False

    class _Worker:
        server = SimpleNamespace(transport="stdio")
        fingerprint = "stale-fingerprint"
        is_open = True

        def close(self, *, timeout: float) -> bool:
            del timeout
            return allow_close

    worker = _Worker()
    manager._sessions["stuck-stdio"] = worker  # type: ignore[assignment]

    try:
        with pytest.raises(MCPLaunchIdentityError) as disconnect_error:
            manager.disconnect_server("stuck-stdio")
        assert disconnect_error.value.code == "mcp_session_close_failed"
        assert manager._sessions["stuck-stdio"] is worker
        persisted = state.get_mcp_server("stuck-stdio")
        assert persisted["status"] == "online"
        assert persisted["session_state"] == "connected"

        with pytest.raises(MCPLaunchIdentityError) as reconfigure_error:
            manager.add_server(
                {
                    "id": "stuck-stdio",
                    "name": "Changed stdio",
                    "transport": "stdio",
                    "command": sys.executable,
                    "args": [str(changed_script)],
                    "enabled": True,
                    "tools": _static_tool(),
                }
            )
        assert reconfigure_error.value.code == "mcp_session_close_failed"
        assert manager._sessions["stuck-stdio"] is worker
        assert state.get_mcp_server("stuck-stdio")["name"] == "Stuck stdio"

        with pytest.raises(MCPLaunchIdentityError) as transition_error:
            with mcp_sensitive_material_transition():
                pytest.fail("sensitive transition must not start")
        assert transition_error.value.code == "mcp_stdio_quiesce_failed"
        assert manager._sessions["stuck-stdio"] is worker
    finally:
        allow_close = True
        manager.shutdown()


def test_mcp_stream_cleanup_runs_even_when_session_exit_fails() -> None:
    stream_closed = False

    class _FailingSession:
        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            del exc_type, exc, tb
            raise RuntimeError("session exit failed")

    class _Stream:
        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            nonlocal stream_closed
            del exc_type, exc, tb
            stream_closed = True

    context = mcp_module._ClientSessionContext(_Stream())
    context.session = _FailingSession()

    with pytest.raises(RuntimeError, match="session exit failed"):
        asyncio.run(context.__aexit__(None, None, None))

    assert stream_closed is True


def test_mcp_protocol_error_result_is_not_reported_as_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protocol_result = SimpleNamespace(
        content=[SimpleNamespace(text="remote tool rejected the request")],
        isError=True,
    )
    with pytest.raises(mcp_module.MCPRemoteToolError, match="remote tool rejected"):
        mcp_module._tool_result_to_text(protocol_result)

    manager = MCPManager(AgentStateStore(tmp_path / "state.db"))

    def rejected_call(*args: object, **kwargs: object) -> str:
        del args, kwargs
        raise mcp_module.MCPRemoteToolError("remote tool rejected the request")

    monkeypatch.setattr(manager, "_call_live_tool", rejected_call)
    result = manager._call_tool_after_connect_approval(
        MCPServerConfig(
            id="remote-error",
            name="remote-error",
            transport="stdio",
            command=str(Path(shutil.which("true") or sys.executable).resolve()),
        ),
        "reject",
        {},
    )

    assert result.success is False
    assert result.error == "mcp_tool_remote_error"
    assert result.data["remote_error"] is True
    assert result.data["retryable"] is False
    assert result.data["session_state"] == "connected"


def test_mcp_worker_disconnect_failure_remains_fail_closed() -> None:
    allow_close = False

    class _FailingContext:
        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            nonlocal allow_close
            del exc_type, exc, tb
            if not allow_close:
                raise RuntimeError("stream teardown failed")

    worker = mcp_module._MCPSessionWorker(
        server=MCPServerConfig(id="sticky-close", name="sticky-close", transport="stdio"),
        fingerprint="fixture",
    )
    worker._session_context = _FailingContext()
    worker._session = object()
    worker._ensure_loop(timeout=1.0)

    assert worker.close(timeout=1.0) is False
    assert worker.reusable is False
    assert worker.is_open is True
    assert worker._session_context is not None
    assert worker.close(timeout=1.0) is False

    allow_close = True
    assert worker.close(timeout=1.0) is True
