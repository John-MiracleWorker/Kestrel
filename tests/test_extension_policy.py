from __future__ import annotations

import os
import socket
import tempfile
from pathlib import Path

import pytest

import nested_memvid_agent.extension_policy as extension_policy
from nested_memvid_agent.extension_policy import (
    ExtensionPolicyError,
    copy_extension_snapshot,
    copy_readonly_filesystem_scope_snapshots,
    extension_tree_digest,
    parse_extension_scopes,
    resolve_filesystem_scopes,
)


def test_extension_scopes_default_deny_and_canonicalize() -> None:
    empty = parse_extension_scopes({})
    explicit = parse_extension_scopes(
        {
            "network": {"mode": "none"},
            "secrets": [],
            "filesystem": [
                {"root": "workspace", "path": "reports", "access": "read"},
                {"root": "workspace", "path": "inputs", "access": "read"},
            ],
        }
    )

    assert empty.to_payload() == {
        "filesystem": [],
        "network": {"mode": "none"},
        "secrets": [],
    }
    assert explicit.to_payload()["filesystem"] == [
        {"root": "workspace", "path": "inputs", "access": "read"},
        {"root": "workspace", "path": "reports", "access": "read"},
    ]
    assert explicit.digest().startswith("sha256:")
    assert explicit.digest() == parse_extension_scopes(explicit.to_payload()).digest()


@pytest.mark.parametrize(
    ("payload", "error"),
    [
        ({"network": {"mode": "egress"}}, "extension_network_scope_unsupported"),
        ({"secrets": ["github"]}, "extension_secret_scopes_unsupported"),
        (
            {"filesystem": [{"root": "workspace", "path": "/tmp", "access": "read"}]},
            "filesystem_scope_path_must_be_relative",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": "../private", "access": "read"}]},
            "filesystem_scope_path_must_be_relative",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": "private\nfile", "access": "read"}]},
            "filesystem_scope_path_not_portable",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": ".", "access": "read"}]},
            "filesystem_scope_workspace_root_rejected",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": ".git", "access": "read"}]},
            "filesystem_scope_control_tree_rejected",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": "inputs/.NEST/data", "access": "read"}]},
            "filesystem_scope_control_tree_rejected",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": "outputs", "access": "write"}]},
            "extension_write_scope_unsupported",
        ),
        (
            {
                "filesystem": [
                    {"root": "workspace", "path": "data", "access": "read"},
                    {"root": "workspace", "path": "data/private", "access": "read"},
                ]
            },
            "overlapping_filesystem_scopes",
        ),
    ],
)
def test_extension_scopes_reject_unsupported_or_ambiguous_grants(
    payload: dict[str, object], error: str
) -> None:
    with pytest.raises(ExtensionPolicyError, match=error):
        parse_extension_scopes(payload)


def test_filesystem_scope_resolves_inside_workspace_and_rejects_symlink_escape(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True)
    scopes = parse_extension_scopes(
        {"filesystem": [{"root": "workspace", "path": "inputs", "access": "read"}]}
    )

    resolved = resolve_filesystem_scopes(scopes, workspace)

    assert resolved[0].source == inputs.resolve()
    assert resolved[0].target == "/workspace/inputs"
    assert resolved[0].access == "read"

    outside = tmp_path / "outside"
    outside.mkdir()
    link = workspace / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this host")
    escape_scope = parse_extension_scopes(
        {"filesystem": [{"root": "workspace", "path": "escape", "access": "read"}]}
    )
    with pytest.raises(ExtensionPolicyError, match="symlink_rejected"):
        resolve_filesystem_scopes(escape_scope, workspace)


def test_filesystem_scope_rejects_final_and_intermediate_symlink_components(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    real = workspace / "real"
    nested = real / "nested"
    nested.mkdir(parents=True)
    try:
        (workspace / "final-link").symlink_to(real, target_is_directory=True)
        (workspace / "component-link").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this host")

    for path in ("final-link", "component-link/nested"):
        scopes = parse_extension_scopes(
            {"filesystem": [{"root": "workspace", "path": path, "access": "read"}]}
        )
        with pytest.raises(ExtensionPolicyError, match="symlink_rejected"):
            resolve_filesystem_scopes(scopes, workspace)


def test_extension_tree_digest_binds_files_and_snapshot_rejects_symlinks(tmp_path: Path) -> None:
    source = tmp_path / "skill"
    source.mkdir()
    script = source / "skill.py"
    script.write_text("print('first')\n", encoding="utf-8")
    (source / "SKILL.md").write_text("Run safely.\n", encoding="utf-8")
    first = extension_tree_digest(source)

    snapshot = tmp_path / "snapshot"
    copied = copy_extension_snapshot(source, snapshot)

    assert copied == first
    script.write_text("print('second')\n", encoding="utf-8")
    assert extension_tree_digest(source) != first

    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (source / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this host")
    with pytest.raises(ExtensionPolicyError, match="symlink_rejected"):
        extension_tree_digest(source)


def test_filesystem_scope_rejects_nested_control_tree_and_hardlinks(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    (inputs / ".git").mkdir(parents=True)
    scopes = parse_extension_scopes(
        {"filesystem": [{"path": "inputs", "access": "read"}]}
    )
    with pytest.raises(ExtensionPolicyError, match="control_tree_rejected"):
        copy_readonly_filesystem_scope_snapshots(
            resolve_filesystem_scopes(scopes, workspace),
            tmp_path / "control-snapshots",
            workspace=workspace,
        )

    (inputs / ".git").rmdir()
    original = inputs / "original.txt"
    original.write_text("one inode", encoding="utf-8")
    linked = inputs / "linked.txt"
    try:
        os.link(original, linked)
    except OSError:
        pytest.skip("hardlinks are unavailable on this host")
    with pytest.raises(ExtensionPolicyError, match="hardlink_rejected"):
        copy_readonly_filesystem_scope_snapshots(
            resolve_filesystem_scopes(scopes, workspace),
            tmp_path / "hardlink-snapshots",
            workspace=workspace,
        )

    direct = parse_extension_scopes(
        {"filesystem": [{"path": "inputs/linked.txt", "access": "read"}]}
    )
    with pytest.raises(ExtensionPolicyError, match="hardlink_rejected"):
        resolve_filesystem_scopes(direct, workspace)


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_filesystem_scope_rejects_nested_unix_socket() -> None:
    with tempfile.TemporaryDirectory(prefix="kst-sock-", dir="/tmp") as temp_name:
        workspace = Path(temp_name) / "workspace"
        nested = workspace / "inputs" / "nested"
        nested.mkdir(parents=True)
        socket_path = nested / "agent.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(socket_path))
            scopes = parse_extension_scopes(
                {"filesystem": [{"path": "inputs", "access": "read"}]}
            )
            with pytest.raises(ExtensionPolicyError, match="nonregular_rejected"):
                copy_readonly_filesystem_scope_snapshots(
                    resolve_filesystem_scopes(scopes, workspace),
                    Path(temp_name) / "socket-snapshots",
                    workspace=workspace,
                )
        finally:
            server.close()


def test_filesystem_scope_scan_has_shared_entry_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True)
    for index in range(3):
        (inputs / f"{index}.txt").write_text(str(index), encoding="utf-8")
    monkeypatch.setattr(extension_policy, "MAX_FILESYSTEM_SCOPE_ENTRIES", 3)
    scopes = parse_extension_scopes(
        {"filesystem": [{"path": "inputs", "access": "read"}]}
    )

    with pytest.raises(ExtensionPolicyError, match="entry_limit_exceeded"):
        copy_readonly_filesystem_scope_snapshots(
            resolve_filesystem_scopes(scopes, workspace),
            tmp_path / "bounded-snapshots",
            workspace=workspace,
        )


def test_filesystem_scope_entry_bound_charges_each_empty_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "workspace"
    (workspace / "first").mkdir(parents=True)
    (workspace / "second").mkdir()
    monkeypatch.setattr(extension_policy, "MAX_FILESYSTEM_SCOPE_ENTRIES", 1)
    scopes = parse_extension_scopes(
        {
            "filesystem": [
                {"path": "first", "access": "read"},
                {"path": "second", "access": "read"},
            ]
        }
    )

    with pytest.raises(ExtensionPolicyError, match="entry_limit_exceeded"):
        copy_readonly_filesystem_scope_snapshots(
            resolve_filesystem_scopes(scopes, workspace),
            tmp_path / "empty-root-snapshots",
            workspace=workspace,
        )


def test_read_scope_snapshot_uses_dirfd_and_rejects_intermediate_swap(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    granted = workspace / "grant" / "nested"
    granted.mkdir(parents=True)
    (granted / "allowed.txt").write_text("allowed", encoding="utf-8")
    scopes = parse_extension_scopes(
        {"filesystem": [{"path": "grant/nested", "access": "read"}]}
    )
    resolved = resolve_filesystem_scopes(scopes, workspace)

    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "nested" / "secret.txt").write_text("secret", encoding="utf-8")
    (workspace / "grant").rename(workspace / "grant-original")
    try:
        (workspace / "grant").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this host")

    with pytest.raises(ExtensionPolicyError):
        copy_readonly_filesystem_scope_snapshots(
            resolved,
            tmp_path / "snapshots",
            workspace=workspace,
        )
    assert not (tmp_path / "snapshots" / "scope-00" / "secret.txt").exists()


def test_extension_tree_rejects_hardlinks_and_bounds_empty_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "skill"
    source.mkdir()
    payload = source / "skill.py"
    payload.write_text("print('safe')\n", encoding="utf-8")
    try:
        os.link(payload, tmp_path / "shared.py")
    except OSError:
        pytest.skip("hardlinks are unavailable on this host")
    with pytest.raises(ExtensionPolicyError, match="hardlink_rejected"):
        extension_tree_digest(source)
    with pytest.raises(ExtensionPolicyError, match="hardlink_rejected"):
        copy_extension_snapshot(source, tmp_path / "snapshot")

    payload.unlink()
    for index in range(3):
        (source / f"dir-{index}").mkdir()
    monkeypatch.setattr(extension_policy, "MAX_EXTENSION_ENTRIES", 2)
    with pytest.raises(ExtensionPolicyError, match="entry_limit_exceeded"):
        extension_tree_digest(source)
