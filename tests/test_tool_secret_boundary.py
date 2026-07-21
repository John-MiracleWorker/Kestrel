from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

import nested_memvid_agent.tools.process_tools as process_tools
from nested_memvid_agent.cli import _build_run_manager, _shutdown_run_manager
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.secret_broker import SecretBroker
from nested_memvid_agent.security_boundary import redact_text, register_secret_value
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools
from nested_memvid_agent.validation_runner import IsolatedValidationResult

PINNED_VALIDATION_IMAGE = "example.invalid/kestrel-validation@sha256:" + "a" * 64


def _stub_isolated_validation(
    monkeypatch: MonkeyPatch,
    *,
    stdout: str,
) -> None:
    def isolated_stub(**kwargs: object) -> IsolatedValidationResult:
        assert kwargs["image"] == PINNED_VALIDATION_IMAGE
        command = tuple(str(item) for item in kwargs["command"])  # type: ignore[union-attr]
        return IsolatedValidationResult(
            args=command,
            returncode=0,
            stdout=stdout,
            stderr="",
            isolation={"mode": "oci_snapshot_v1", "host_fallback": False},
        )

    monkeypatch.setattr(process_tools, "run_isolated_validation", isolated_stub)


def _config(workspace: Path, *, vault: Path | None = None, **overrides: Any) -> AgentConfig:
    values: dict[str, Any] = {
        "workspace": workspace,
        "secret_store_path": vault or Path("config/runtime-state.json"),
        "secret_backend": "json",
        "allow_shell": True,
        "allow_file_write": True,
        "allow_codex_cli": True,
        "allow_git_commit": True,
        "state_path": workspace / ".state" / "agent.db",
        "memory_dir": workspace / ".memory",
        "skills_dir": workspace / ".skills",
        "plugins_dir": workspace / ".plugins",
    }
    values.update(overrides)
    return AgentConfig(**values)


def _approved_context(
    workspace: Path,
    call: ToolCall,
    *,
    config: AgentConfig | None = None,
) -> ToolContext:
    memory = build_memory_system("memory", workspace / ".tool-memory")
    return ToolContext(
        memory=memory,
        config=config or _config(workspace),
        workspace=workspace,
        approved_tool_call_ids=frozenset({call.id}),
        approved_tool_call_arguments={call.id: call.arguments},
    )


def _create_vault(workspace: Path, sentinel: str) -> Path:
    path = workspace / "config" / "runtime-state.json"
    broker = SecretBroker(path)
    broker.store_secret(name="provider", purpose="test", value=sentinel)
    return path


def _git(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=workspace,
        capture_output=True,
        text=True,
        check=True,
    )


def _init_git(workspace: Path) -> None:
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.email", "test@example.invalid")
    _git(workspace, "config", "user.name", "Kestrel Test")
    (workspace / "README.md").write_text("seed\n", encoding="utf-8")
    _git(workspace, "add", "README.md")
    _git(workspace, "commit", "-m", "seed")


def test_central_redaction_preserves_json_primitives_but_not_secret_values() -> None:
    payload = (
        '{"OPENAI_API_KEY": null, "token": true, "password": false, '
        '"client_secret": "quoted-secret-value", "api_key": unquoted-secret-value, '
        '"ACCESS_TOKEN": NULL, "SECRET": TRUE, "PASSWD": FALSE}'
    )

    redacted = redact_text(payload, environ={})

    assert '"OPENAI_API_KEY": null' in redacted
    assert '"token": true' in redacted
    assert '"password": false' in redacted
    assert "quoted-secret-value" not in redacted
    assert "unquoted-secret-value" not in redacted
    assert "NULL" not in redacted
    assert "TRUE" not in redacted
    assert "FALSE" not in redacted
    assert redacted.count("<redacted>") == 5


def test_exact_registered_secret_field_cannot_bypass_json_primitive_handling() -> None:
    registered_field = '"ODD_PROVIDER_SECRET": null'
    register_secret_value(registered_field)

    redacted = redact_text(
        f"prefix {registered_field}, suffix",
        environ={},
    )

    assert registered_field not in redacted
    assert "<redacted>" in redacted


@pytest.mark.parametrize(
    "relative_path",
    (
        "config/runtime-state.json",
        "config/.runtime-state.json.lock",
        "config/.runtime-state.json.attack.tmp",
        "innocent-alias.json",
    ),
)
def test_shell_cat_rejects_custom_vault_artifacts_and_inode_aliases(
    tmp_path: Path,
    relative_path: str,
) -> None:
    sentinel = "opaque-shell-vault-sentinel-123456"
    vault = _create_vault(tmp_path, sentinel)
    temporary = vault.with_name(f".{vault.name}.attack.tmp")
    temporary.write_text(sentinel, encoding="utf-8")
    alias = tmp_path / "innocent-alias.json"
    try:
        os.link(vault, alias)
    except OSError:
        if relative_path == alias.name:
            pytest.skip("hard links are unavailable on this filesystem")

    call = ToolCall(
        name="shell.run",
        arguments={"command": ["cat", relative_path]},
        id=f"cat-{relative_path}",
    )
    result = build_default_tools().execute(call, _approved_context(tmp_path, call))

    assert result.success is False
    assert result.error == "path_not_allowed"
    assert sentinel not in result.content


@pytest.mark.parametrize(
    "relative_path",
    (
        "config/runtime-state.json",
        "config/.runtime-state.json.lock",
        "config/.runtime-state.json.attack.tmp",
        "innocent-alias.json",
    ),
)
def test_patch_apply_rejects_custom_vault_artifacts_and_inode_aliases(
    tmp_path: Path,
    relative_path: str,
) -> None:
    sentinel = "opaque-patch-vault-sentinel-123456"
    vault = _create_vault(tmp_path, sentinel)
    vault.with_name(f".{vault.name}.attack.tmp").write_text(sentinel, encoding="utf-8")
    alias = tmp_path / "innocent-alias.json"
    try:
        os.link(vault, alias)
    except OSError:
        if relative_path == alias.name:
            pytest.skip("hard links are unavailable on this filesystem")
    patch = (
        f"--- a/{relative_path}\n"
        f"+++ b/{relative_path}\n"
        "@@ -1 +1 @@\n"
        "-before\n"
        "+after\n"
    )
    call = ToolCall(name="patch.apply", arguments={"patch": patch}, id="patch-vault")

    result = build_default_tools().execute(call, _approved_context(tmp_path, call))

    assert result.success is False
    assert result.error == "patch_apply_failed"
    assert sentinel not in result.content
    assert sentinel in vault.read_text(encoding="utf-8")


def test_patch_apply_rejects_rename_only_custom_vault_patch(tmp_path: Path) -> None:
    sentinel = "opaque-rename-vault-sentinel-123456"
    vault = _create_vault(tmp_path, sentinel)
    patch = (
        "diff --git a/config/runtime-state.json b/config/public-state.json\n"
        "similarity index 100%\n"
        "rename from config/runtime-state.json\n"
        "rename to config/public-state.json\n"
    )
    call = ToolCall(name="patch.apply", arguments={"patch": patch}, id="rename-vault")

    result = build_default_tools().execute(call, _approved_context(tmp_path, call))

    assert result.success is False
    assert result.error == "patch_apply_failed"
    assert sentinel not in result.content
    assert vault.exists()
    assert not (tmp_path / "config" / "public-state.json").exists()


@pytest.mark.parametrize(
    "patch",
    (
        (
            "diff --git x/config/runtime-state.json y/config/runtime-state.json\n"
            "--- x/config/runtime-state.json\n"
            "+++ y/config/runtime-state.json\n"
            "@@ -1 +1 @@\n-before\n+after\n"
        ),
        (
            'diff --git "a/config/runtime\\055state.json" '
            '"b/config/runtime\\055state.json"\n'
            '--- "a/config/runtime\\055state.json"\n'
            '+++ "b/config/runtime\\055state.json"\n'
            "@@ -1 +1 @@\n-before\n+after\n"
        ),
    ),
)
def test_patch_apply_rejects_stripped_or_encoded_custom_vault_paths(
    tmp_path: Path,
    patch: str,
) -> None:
    sentinel = "opaque-encoded-patch-vault-sentinel-123456"
    vault = _create_vault(tmp_path, sentinel)
    call = ToolCall(name="patch.apply", arguments={"patch": patch}, id="encoded-vault")

    result = build_default_tools().execute(call, _approved_context(tmp_path, call))

    assert result.success is False
    assert result.error == "patch_apply_failed"
    assert sentinel not in result.content
    assert sentinel in vault.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    (
        ("test.run", {"command": [sys.executable, "-c", "print('should not run')"]}),
        ("lint.run", {"command": [sys.executable, "-c", "print('should not run')"]}),
        ("codex.exec", {"prompt": "inspect the workspace"}),
    ),
)
def test_arbitrary_code_tools_fail_closed_with_json_vault_in_workspace(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    tool_name: str,
    arguments: dict[str, Any],
) -> None:
    sentinel = "opaque-process-vault-sentinel-123456"
    _create_vault(tmp_path, sentinel)
    launches: list[object] = []

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        launches.append((args, kwargs))
        raise AssertionError("subprocess must not launch")

    monkeypatch.setattr("nested_memvid_agent.tools.process_tools._start_subprocess", forbidden_launch)
    call = ToolCall(name=tool_name, arguments=arguments, id=f"blocked-{tool_name}")

    result = build_default_tools().execute(call, _approved_context(tmp_path, call))

    assert result.success is False
    assert result.error == "validation_container_required"
    assert sentinel not in result.content
    assert launches == []


def test_arbitrary_code_fails_closed_with_interrupted_json_vault_temp(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    sentinel = "opaque-interrupted-vault-sentinel-123456"
    vault = tmp_path / "config" / "runtime-state.json"
    vault.parent.mkdir(parents=True)
    vault.with_name(f".{vault.name}.interrupted.tmp").write_text(
        sentinel,
        encoding="utf-8",
    )
    launches: list[object] = []

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        launches.append((args, kwargs))
        raise AssertionError("subprocess must not launch")

    monkeypatch.setattr(
        "nested_memvid_agent.tools.process_tools._start_subprocess",
        forbidden_launch,
    )
    call = ToolCall(
        name="test.run",
        arguments={"command": [sys.executable, "-c", "print('should not run')"]},
        id="interrupted-vault-test",
    )

    result = build_default_tools().execute(call, _approved_context(tmp_path, call))

    assert result.success is False
    assert result.error == "validation_container_required"
    assert sentinel not in result.content
    assert launches == []


def test_json_vault_lock_without_raw_vault_does_not_block_validation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    vault = tmp_path / "config" / "runtime-state.json"
    vault.parent.mkdir(parents=True)
    vault.with_name(f".{vault.name}.lock").write_text("", encoding="utf-8")
    call = ToolCall(
        name="test.run",
        arguments={
            "command": [sys.executable, "-c", "print('safe-empty-json-vault')"]
        },
        id="empty-json-vault-test",
    )

    _stub_isolated_validation(monkeypatch, stdout="safe-empty-json-vault\n")
    config = _config(
        tmp_path,
        validation_container_image=PINNED_VALIDATION_IMAGE,
    )
    result = build_default_tools().execute(
        call,
        _approved_context(tmp_path, call, config=config),
    )

    assert result.success is True
    assert "safe-empty-json-vault" in result.content


def test_repair_patch_and_validation_reject_custom_vault_paths_before_execution(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _git(tmp_path, "switch", "-c", "codex/secret-boundary")
    sentinel = "opaque-repair-vault-sentinel-123456"
    _create_vault(tmp_path, sentinel)
    launches: list[object] = []

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        launches.append((args, kwargs))
        raise AssertionError("subprocess must not launch")

    monkeypatch.setattr("nested_memvid_agent.tools.process_tools._start_subprocess", forbidden_launch)
    registry = build_default_tools()
    patch = (
        "--- a/config/runtime-state.json\n"
        "+++ b/config/runtime-state.json\n"
        "@@ -1 +1 @@\n-before\n+after\n"
    )
    patch_call = ToolCall(
        name="repair.apply_patch",
        arguments={"patch": patch},
        id="repair-patch-vault",
    )
    validation_calls = [
        ToolCall(
            name=tool_name,
            arguments={"command": [sys.executable, "-c", "print('should not run')"]},
            id=f"{tool_name}-vault",
        )
        for tool_name in ("repair.validate", "repair.orchestrate_validate")
    ]

    patched = registry.execute(patch_call, _approved_context(tmp_path, patch_call))
    validations = [
        registry.execute(call, _approved_context(tmp_path, call))
        for call in validation_calls
    ]

    assert patched.success is False
    assert patched.error == "repair_patch_failed"
    assert sentinel not in patched.content
    for validated in validations:
        assert validated.success is False
        assert validated.error in {
            "repair_validation_failed",
            "repair_orchestration_failed",
            "workspace_secret_isolation_required",
        }
        assert sentinel not in validated.content
    assert launches == []


def test_repair_snapshot_and_commit_gates_fail_before_raw_json_vault_inspection(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    _init_git(tmp_path)
    _git(tmp_path, "switch", "-c", "codex/secret-snapshot-boundary")
    sentinel = "opaque-repair-snapshot-vault-sentinel-123456"
    vault = tmp_path / "config" / "runtime-state.json"
    vault.parent.mkdir(parents=True)
    vault.write_text(sentinel, encoding="utf-8")
    snapshots: list[Path] = []

    def forbidden_snapshot(workspace: Path) -> dict[str, Any]:
        snapshots.append(workspace)
        raise AssertionError("repair snapshot must not inspect a raw JSON vault")

    monkeypatch.setattr(
        "nested_memvid_agent.tools.repair_tools.repair_snapshot",
        forbidden_snapshot,
    )
    monkeypatch.setattr(
        "nested_memvid_agent.tools.git_tools.repair_snapshot",
        forbidden_snapshot,
    )
    config = _config(tmp_path)
    registry = build_default_tools()
    status = registry.execute(
        ToolCall(name="repair.status", arguments={}),
        ToolContext(
            memory=build_memory_system("memory", tmp_path / ".repair-status-memory"),
            config=config,
            workspace=tmp_path,
        ),
    )
    commit_call = ToolCall(
        name="git.commit",
        arguments={"message": "must not commit", "repair_review_id": "repair_review_missing"},
        id="blocked-repair-commit",
    )
    committed = registry.execute(
        commit_call,
        _approved_context(tmp_path, commit_call, config=config),
    )

    for result in (status, committed):
        assert result.success is False
        assert result.error == "workspace_secret_isolation_required"
        assert sentinel not in result.content
    assert snapshots == []


def test_subprocess_output_and_errors_are_redacted_before_validation_evidence(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    sentinel = "opaque-subprocess-output-sentinel-123456"
    register_secret_value(sentinel)
    metadata = tmp_path / "config" / "keyring-metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"secrets": {}}\n', encoding="utf-8")
    script = tmp_path / "echo_secret.py"
    script.write_text(f"print({sentinel!r})\n", encoding="utf-8")
    config = _config(
        tmp_path,
        vault=Path("config/keyring-metadata.json"),
        secret_backend="keyring",
        validation_container_image=PINNED_VALIDATION_IMAGE,
    )
    call = ToolCall(
        name="test.run",
        arguments={"command": [sys.executable, str(script)]},
        id="redacted-test-output",
    )

    _stub_isolated_validation(monkeypatch, stdout=f"{sentinel}\n")
    result = build_default_tools().execute(
        call,
        _approved_context(tmp_path, call, config=config),
    )

    assert result.success is True
    assert sentinel not in result.content
    assert sentinel not in json.dumps(result.data, sort_keys=True)
    assert "<redacted>" in result.content
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)

    def successful_codex(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("sanitize_environment") is True
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"delegated stdout {sentinel}",
            stderr=f"delegated stderr {sentinel}",
        )

    monkeypatch.setattr(
        "nested_memvid_agent.tools.command_tools._run_subprocess", successful_codex
    )
    codex_success_call = ToolCall(
        name="codex.exec",
        arguments={"prompt": "safe task"},
        id="redacted-codex-output",
    )
    codex_success = build_default_tools().execute(
        codex_success_call,
        _approved_context(tmp_path, codex_success_call, config=config),
    )
    assert codex_success.success is True
    assert sentinel not in codex_success.content
    assert "<redacted>" in codex_success.content

    def failed_codex(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise RuntimeError(f"delegation failed: {sentinel}")

    monkeypatch.setattr(
        "nested_memvid_agent.tools.command_tools._run_subprocess", failed_codex
    )
    codex_call = ToolCall(
        name="codex.exec",
        arguments={"prompt": "safe task"},
        id="redacted-codex-error",
    )
    codex = build_default_tools().execute(
        codex_call,
        _approved_context(tmp_path, codex_call, config=config),
    )
    assert codex.success is False
    assert sentinel not in codex.content
    assert "<redacted>" in codex.content


def test_outside_workspace_json_vault_still_blocks_same_uid_arbitrary_code(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    sentinel = "opaque-outside-json-vault-sentinel-123456"
    outside_vault = tmp_path.parent / f"{tmp_path.name}-outside-vault.json"
    broker = SecretBroker(outside_vault)
    broker.store_secret(name="provider", purpose="test", value=sentinel)
    config = _config(tmp_path, vault=outside_vault)
    launches: list[object] = []

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        launches.append((args, kwargs))
        raise AssertionError("subprocess must not launch")

    monkeypatch.setattr("nested_memvid_agent.tools.process_tools._start_subprocess", forbidden_launch)
    call = ToolCall(
        name="test.run",
        arguments={"command": [sys.executable, "-c", "print('should not run')"]},
        id="outside-vault-test",
    )

    result = build_default_tools().execute(
        call,
        _approved_context(tmp_path, call, config=config),
    )

    assert result.success is False
    assert result.error == "validation_container_required"
    assert sentinel not in result.content
    assert launches == []


def test_keyring_metadata_inside_workspace_does_not_block_safe_validation(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    metadata = tmp_path / "config" / "runtime-state.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"secrets": {}}\n', encoding="utf-8")
    _stub_isolated_validation(monkeypatch, stdout="safe-keyring-validation\n")
    config = _config(
        tmp_path,
        secret_backend="keyring",
        validation_container_image=PINNED_VALIDATION_IMAGE,
    )
    call = ToolCall(
        name="test.run",
        arguments={"command": [sys.executable, "-c", "print('safe-keyring-validation')"]},
        id="keyring-validation",
    )

    result = build_default_tools().execute(
        call,
        _approved_context(tmp_path, call, config=config),
    )

    assert result.success is True
    assert "safe-keyring-validation" in result.content


def test_keyring_records_require_process_isolation_without_keyring_lookup(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    metadata = tmp_path / "config" / "runtime-state.json"
    metadata.parent.mkdir(parents=True)
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
    launches: list[object] = []

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        launches.append((args, kwargs))
        raise AssertionError("host process must not launch")

    monkeypatch.setattr(
        "nested_memvid_agent.tools.process_tools._start_subprocess",
        forbidden_launch,
    )
    config = _config(tmp_path, secret_backend="keyring")
    call = ToolCall(
        name="test.run",
        arguments={"command": [sys.executable, "-c", "print('unreachable')"]},
        id="keyring-record-validation",
    )

    result = build_default_tools().execute(
        call,
        _approved_context(tmp_path, call, config=config),
    )

    assert result.success is False
    assert result.error == "validation_container_required"
    assert "host fallback is disabled" in result.content
    assert launches == []


def test_sensitive_test_routes_to_configured_private_container(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    metadata = tmp_path / "config" / "runtime-state.json"
    metadata.parent.mkdir(parents=True)
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
    isolated_calls: list[dict[str, object]] = []

    def isolated_stub(**kwargs: object) -> IsolatedValidationResult:
        isolated_calls.append(kwargs)
        return IsolatedValidationResult(
            args=(sys.executable, "-c", "print('contained')"),
            returncode=0,
            stdout="contained\n",
            stderr="",
            isolation={"mode": "oci_snapshot_v1", "host_fallback": False},
        )

    def forbidden_host_launch(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("host process must not launch")

    monkeypatch.setattr(process_tools, "run_isolated_validation", isolated_stub)
    monkeypatch.setattr(process_tools, "_start_subprocess", forbidden_host_launch)
    config = _config(
        tmp_path,
        secret_backend="keyring",
        validation_container_image=PINNED_VALIDATION_IMAGE,
    )
    call = ToolCall(
        name="test.run",
        arguments={"command": [sys.executable, "-c", "print('contained')"]},
        id="keyring-contained-validation",
    )

    result = build_default_tools().execute(
        call,
        _approved_context(tmp_path, call, config=config),
    )

    assert result.success is True
    assert "contained" in result.content
    assert len(isolated_calls) == 1
    assert isolated_calls[0]["image"] == PINNED_VALIDATION_IMAGE


@pytest.mark.parametrize("tool_name", ["test.run", "lint.run", "codex.exec"])
def test_repair_trust_material_blocks_uncontained_host_code(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    tool_name: str,
) -> None:
    trust_root = tmp_path / ".nest"
    trust_root.mkdir(mode=0o700)
    key = trust_root / "repair_receipt_signing.v2.key"
    key.write_bytes(os.urandom(32))
    key.chmod(0o600)
    launches: list[object] = []

    def forbidden_launch(*args: object, **kwargs: object) -> object:
        launches.append((args, kwargs))
        raise AssertionError("host process must not launch")

    monkeypatch.setattr(
        "nested_memvid_agent.tools.process_tools._start_subprocess",
        forbidden_launch,
    )
    arguments = (
        {"prompt": "inspect"}
        if tool_name == "codex.exec"
        else {"command": [sys.executable, "-c", "print('unreachable')"]}
    )
    call = ToolCall(name=tool_name, arguments=arguments, id=f"repair-trust-{tool_name}")

    result = build_default_tools().execute(call, _approved_context(tmp_path, call))

    assert result.success is False
    assert result.error == "validation_container_required"
    assert launches == []


def test_git_diff_show_and_export_never_surface_or_persist_registered_secret(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    sentinel = "opaque-git-output-sentinel-123456"
    vault = _create_vault(tmp_path, sentinel)
    note = tmp_path / "notes.txt"
    note.write_text(f"opaque={sentinel}\n", encoding="utf-8")
    _git(tmp_path, "add", "config/runtime-state.json", "notes.txt")
    _git(tmp_path, "commit", "-m", "secret fixture")
    safe_history = tmp_path / "safe-history.txt"
    safe_history.write_text(f"historical opaque={sentinel}\n", encoding="utf-8")
    _git(tmp_path, "add", "safe-history.txt")
    _git(tmp_path, "commit", "-m", "safe history fixture")
    note.write_text(f"changed opaque={sentinel}\n", encoding="utf-8")
    registry = build_default_tools()
    config = _config(tmp_path)
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / ".git-tool-memory"),
        config=config,
        workspace=tmp_path,
    )

    diff = registry.execute(ToolCall(name="git.diff", arguments={}), context)
    shown = registry.execute(ToolCall(name="git.show", arguments={"rev": "HEAD"}), context)
    explicit_vault = registry.execute(
        ToolCall(
            name="git.show",
            arguments={"rev": "HEAD~1", "path": "config/runtime-state.json"},
        ),
        context,
    )
    export_call = ToolCall(
        name="git.export_patch",
        arguments={"path": ".kestrel/improvements/secret-test/diff.patch"},
        id="export-redacted-patch",
    )
    exported = registry.execute(
        export_call,
        _approved_context(tmp_path, export_call, config=config),
    )

    artifact = tmp_path / ".kestrel" / "improvements" / "secret-test" / "diff.patch"
    assert diff.success is True
    assert shown.success is True
    assert explicit_vault.success is False
    assert explicit_vault.error == "invalid_path"
    assert exported.success is True
    assert artifact.exists()
    for payload in (diff.content, shown.content, explicit_vault.content, exported.content):
        assert sentinel not in payload
    assert sentinel not in artifact.read_text(encoding="utf-8")
    assert "<redacted>" in diff.content
    assert "<redacted>" in shown.content
    assert "<redacted>" in artifact.read_text(encoding="utf-8")
    assert sentinel in vault.read_text(encoding="utf-8")


def test_git_reads_fail_before_deleted_or_historical_custom_vault_can_leak(
    tmp_path: Path,
) -> None:
    _init_git(tmp_path)
    sentinel = "opaque-unregistered-historical-vault-sentinel-123456"
    vault = tmp_path / "config" / "runtime-state.json"
    vault.parent.mkdir(parents=True)
    vault.write_text(f"opaque vault bytes {sentinel}\n", encoding="utf-8")
    _git(tmp_path, "add", "config/runtime-state.json")
    _git(tmp_path, "commit", "-m", "historical vault fixture")
    secret_commit = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "rm", "config/runtime-state.json")
    config = _config(tmp_path)
    registry = build_default_tools()
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / ".historical-git-memory"),
        config=config,
        workspace=tmp_path,
    )

    blob_spec = registry.execute(
        ToolCall(
            name="git.show",
            arguments={"rev": f"{secret_commit}:config/runtime-state.json"},
        ),
        context,
    )
    commit_show = registry.execute(
        ToolCall(name="git.show", arguments={"rev": secret_commit}),
        context,
    )
    staged_diff = registry.execute(
        ToolCall(name="git.diff", arguments={"staged": True}),
        context,
    )
    status = registry.execute(ToolCall(name="git.status", arguments={}), context)
    export_call = ToolCall(
        name="git.export_patch",
        arguments={
            "staged": True,
            "path": ".kestrel/improvements/historical-secret/diff.patch",
        },
        id="historical-secret-export",
    )
    exported = registry.execute(
        export_call,
        _approved_context(tmp_path, export_call, config=config),
    )

    assert blob_spec.success is False
    assert blob_spec.error == "invalid_revision"
    assert commit_show.success is False
    assert commit_show.error == "git_show_path_blocked"
    assert staged_diff.success is False
    assert staged_diff.error == "git_diff_path_blocked"
    assert status.success is False
    assert status.error == "git_status_path_blocked"
    assert exported.success is False
    assert exported.error == "git_export_path_blocked"
    artifact = (
        tmp_path
        / ".kestrel"
        / "improvements"
        / "historical-secret"
        / "diff.patch"
    )
    assert not artifact.exists()
    for result in (blob_spec, commit_show, staged_diff, status, exported):
        assert sentinel not in result.content
        assert "runtime-state.json" not in result.content


def test_git_diff_and_export_revalidate_paths_from_exact_rendered_patch(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    sentinel = "opaque-git-render-race-sentinel-123456"
    malicious_patch = (
        "diff --git a/config/runtime-state.json b/config/runtime-state.json\n"
        "--- a/config/runtime-state.json\n"
        "+++ b/config/runtime-state.json\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        f"+{sentinel}\n"
    )

    def raced_git(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[Any]:
        del kwargs
        if "--name-only" in command:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=b"safe.txt\0",
                stderr=b"",
            )
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=malicious_patch,
            stderr="",
        )

    monkeypatch.setattr(
        "nested_memvid_agent.tools.git_tools.subprocess.run",
        raced_git,
    )
    monkeypatch.setattr(
        "nested_memvid_agent.tools.git_tools._run_subprocess",
        raced_git,
    )
    config = _config(tmp_path)
    registry = build_default_tools()
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / ".git-race-memory"),
        config=config,
        workspace=tmp_path,
    )
    diff = registry.execute(ToolCall(name="git.diff", arguments={}), context)
    export_call = ToolCall(
        name="git.export_patch",
        arguments={"path": ".kestrel/improvements/render-race/diff.patch"},
        id="export-render-race",
    )
    exported = registry.execute(
        export_call,
        _approved_context(tmp_path, export_call, config=config),
    )

    assert diff.success is False
    assert diff.error == "git_diff_path_blocked"
    assert exported.success is False
    assert exported.error == "git_export_path_blocked"
    assert sentinel not in diff.content
    assert sentinel not in exported.content
    assert not (
        tmp_path / ".kestrel" / "improvements" / "render-race" / "diff.patch"
    ).exists()


@pytest.mark.skipif(os.name == "nt", reason="test uses an executable POSIX helper")
@pytest.mark.parametrize("driver_kind", ("external", "textconv"))
def test_git_reads_disable_repository_configured_diff_processes(
    tmp_path: Path,
    driver_kind: str,
) -> None:
    _init_git(tmp_path)
    sentinel = f"opaque-{driver_kind}-diff-driver-sentinel-123456"
    vault = tmp_path / "config" / "runtime-state.json"
    vault.parent.mkdir(parents=True)
    vault.write_text(sentinel, encoding="utf-8")
    marker = tmp_path / f"{driver_kind}-driver-ran"
    driver = tmp_path / f"{driver_kind}-driver.sh"
    driver.write_text(
        "#!/bin/sh\n"
        f"printf invoked > {shlex.quote(str(marker))}\n"
        f"cat {shlex.quote(str(vault))}\n",
        encoding="utf-8",
    )
    driver.chmod(0o700)

    if driver_kind == "external":
        tracked = tmp_path / "safe.txt"
        tracked.write_text("before\n", encoding="utf-8")
        _git(tmp_path, "add", "safe.txt")
        _git(tmp_path, "commit", "-m", "safe external fixture")
        _git(tmp_path, "config", "diff.external", str(driver))
        tracked.write_text("after\n", encoding="utf-8")
    else:
        (tmp_path / ".gitattributes").write_text(
            "*.bin diff=kestrel-leaker\n",
            encoding="utf-8",
        )
        tracked = tmp_path / "safe.bin"
        tracked.write_bytes(b"before\x00payload")
        _git(tmp_path, "add", ".gitattributes", "safe.bin")
        _git(tmp_path, "commit", "-m", "safe textconv fixture")
        _git(tmp_path, "config", "diff.kestrel-leaker.textconv", str(driver))
        tracked.write_bytes(b"after\x00payload")

    config = _config(tmp_path)
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / f".{driver_kind}-git-memory"),
        config=config,
        workspace=tmp_path,
    )
    registry = build_default_tools()
    diff = registry.execute(ToolCall(name="git.diff", arguments={}), context)
    shown = registry.execute(ToolCall(name="git.show", arguments={}), context)
    export_call = ToolCall(
        name="git.export_patch",
        arguments={
            "path": f".kestrel/improvements/{driver_kind}-driver/diff.patch"
        },
        id=f"export-{driver_kind}-driver",
    )
    exported = registry.execute(
        export_call,
        _approved_context(tmp_path, export_call, config=config),
    )

    assert diff.success is True
    assert shown.success is True
    assert exported.success is True
    assert not marker.exists()
    artifact = (
        tmp_path
        / ".kestrel"
        / "improvements"
        / f"{driver_kind}-driver"
        / "diff.patch"
    )
    assert artifact.exists()
    for payload in (diff.content, shown.content, exported.content):
        assert sentinel not in payload
    assert sentinel not in artifact.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="test uses an executable POSIX helper")
def test_git_reads_disable_repository_configured_fsmonitor(tmp_path: Path) -> None:
    _init_git(tmp_path)
    sentinel = "opaque-fsmonitor-vault-sentinel-123456"
    vault = tmp_path / ".git" / "runtime-state.json"
    vault.write_text(sentinel, encoding="utf-8")
    marker = tmp_path / "fsmonitor-ran"
    monitor = tmp_path / "fsmonitor.sh"
    monitor.write_text(
        "#!/bin/sh\n"
        f"cat {shlex.quote(str(vault))} > {shlex.quote(str(marker))}\n"
        "printf token\\n\n",
        encoding="utf-8",
    )
    monitor.chmod(0o700)
    _git(tmp_path, "config", "core.fsmonitor", str(monitor))
    config = _config(tmp_path, vault=vault)
    context = ToolContext(
        memory=build_memory_system("memory", tmp_path / ".git" / "fsmonitor-memory"),
        config=config,
        workspace=tmp_path,
    )

    status = build_default_tools().execute(
        ToolCall(name="git.status", arguments={}),
        context,
    )

    assert status.success is True
    assert sentinel not in status.content
    assert not marker.exists()


def test_cli_run_manager_primes_broker_and_shares_resolver_with_mcp(tmp_path: Path) -> None:
    sentinel = "opaque-cli-broker-sentinel-123456"
    vault = _create_vault(tmp_path, sentinel)
    config = _config(tmp_path, vault=Path("config/runtime-state.json"))

    manager = _build_run_manager(config, recover_startup_work=False)
    try:
        assert manager.secret_resolver is not None
        assert manager.mcp.secret_resolver is not None
        assert manager.secret_resolver("secret://provider") == sentinel
        assert manager.mcp.secret_resolver("secret://provider") == sentinel
        assert vault == tmp_path / config.secret_store_path
    finally:
        _shutdown_run_manager(manager)


def test_cli_read_only_observer_does_not_construct_or_mutate_secret_backend(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    vault = tmp_path / "config" / "observer-state.json"
    config = _config(
        tmp_path,
        vault=Path("config/observer-state.json"),
        secret_backend="keyring",
    )
    broker_builds: list[object] = []

    def forbidden_broker(*args: object, **kwargs: object) -> object:
        broker_builds.append((args, kwargs))
        raise AssertionError("read-only observer must not initialize a secret backend")

    monkeypatch.setattr("nested_memvid_agent.cli.build_secret_broker", forbidden_broker)

    manager = _build_run_manager(config, read_only_observer=True)
    try:
        assert manager.secret_resolver is None
        assert manager.mcp.secret_resolver is None
    finally:
        _shutdown_run_manager(manager)

    assert broker_builds == []
    assert not vault.exists()
    assert not vault.with_name(f".{vault.name}.lock").exists()
