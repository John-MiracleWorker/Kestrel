from __future__ import annotations

import ctypes
import os
import socket
import tempfile
from ctypes import wintypes
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
            {"filesystem": [{"root": "workspace", "path": ".git.", "access": "read"}]},
            "filesystem_scope_path_not_portable",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": ".nest ", "access": "read"}]},
            "filesystem_scope_path_not_portable",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": "input:data", "access": "read"}]},
            "filesystem_scope_path_not_portable",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": "NUL.txt", "access": "read"}]},
            "filesystem_scope_path_not_portable",
        ),
        (
            {"filesystem": [{"root": "workspace", "path": "report?.txt", "access": "read"}]},
            "filesystem_scope_path_not_portable",
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


def test_windows_canonical_resolution_rechecks_control_tree_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    control = workspace / ".git"
    control.mkdir(parents=True)
    monkeypatch.setattr(
        extension_policy,
        "_uses_windows_path_fallback",
        lambda: True,
    )

    with pytest.raises(ExtensionPolicyError, match="control_tree_rejected"):
        extension_policy._canonical_scope_relative(  # noqa: SLF001
            workspace,
            control,
            declared_path="GIT~1",
        )


@pytest.mark.skipif(os.name != "nt", reason="Windows 8.3 alias semantics required")
def test_windows_short_name_alias_cannot_grant_control_tree(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    control = workspace / ".git"
    control.mkdir(parents=True)
    loader = getattr(ctypes, "WinDLL", None)
    if not callable(loader):
        pytest.skip("WinAPI loader is unavailable")
    kernel32 = loader("kernel32", use_last_error=True)
    get_short_path = kernel32.GetShortPathNameW
    get_short_path.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    get_short_path.restype = wintypes.DWORD
    required = int(get_short_path(str(control), None, 0))
    if required <= 1:
        pytest.skip("8.3 aliases are disabled on this volume")
    buffer = ctypes.create_unicode_buffer(required)
    if not get_short_path(str(control), buffer, required):
        pytest.skip("8.3 alias lookup failed")
    alias = Path(buffer.value).name
    if alias.casefold() == control.name.casefold():
        pytest.skip("the control directory has no distinct 8.3 alias")
    scopes = parse_extension_scopes(
        {"filesystem": [{"path": alias, "access": "read"}]}
    )

    with pytest.raises(ExtensionPolicyError, match="control_tree_rejected"):
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


@pytest.mark.skipif(os.name == "nt", reason="descriptor traversal is POSIX-only")
def test_extension_tree_allows_metadata_churn_in_an_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor = tmp_path / "changing-ancestor"
    source = ancestor / "skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Run safely.\n", encoding="utf-8")
    real_open = os.open
    directory_flags = extension_policy._required_directory_flags(
        "extension_snapshot_platform_unsupported"
    )
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    ancestor_before = ancestor.stat()
    churned = False

    def open_after_ancestor_churn(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal churned
        opened_parent = os.fstat(dir_fd) if dir_fd is not None else None
        if (
            not churned
            and path == ancestor.name
            and opened_parent is not None
            and (opened_parent.st_dev, opened_parent.st_ino) == parent_identity
        ):
            metadata = ancestor.stat()
            os.utime(
                ancestor,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000),
            )
            churned = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        extension_policy,
        "_required_directory_flags",
        lambda _error: directory_flags,
    )
    monkeypatch.setattr(extension_policy.os, "open", open_after_ancestor_churn)

    assert extension_tree_digest(source).startswith("sha256:")
    assert churned is True
    ancestor_after = ancestor.stat()
    assert extension_policy._same_stat_identity(ancestor_before, ancestor_after)
    assert not extension_policy._same_stat_snapshot(ancestor_before, ancestor_after)


@pytest.mark.skipif(os.name == "nt", reason="descriptor traversal is POSIX-only")
def test_extension_tree_still_rejects_target_churn_during_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor = tmp_path / "stable-ancestor"
    source = ancestor / "skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Run safely.\n", encoding="utf-8")
    real_open = os.open
    directory_flags = extension_policy._required_directory_flags(
        "extension_snapshot_platform_unsupported"
    )
    parent_identity = (ancestor.stat().st_dev, ancestor.stat().st_ino)
    churned = False

    def open_after_target_churn(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal churned
        opened_parent = os.fstat(dir_fd) if dir_fd is not None else None
        if (
            not churned
            and path == source.name
            and opened_parent is not None
            and (opened_parent.st_dev, opened_parent.st_ino) == parent_identity
        ):
            (source / "raced.txt").write_text("changed\n", encoding="utf-8")
            churned = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        extension_policy,
        "_required_directory_flags",
        lambda _error: directory_flags,
    )
    monkeypatch.setattr(extension_policy.os, "open", open_after_target_churn)

    with pytest.raises(ExtensionPolicyError, match="changed_during_read"):
        extension_tree_digest(source)
    assert churned is True


@pytest.mark.skipif(os.name == "nt", reason="descriptor traversal is POSIX-only")
def test_extension_tree_rejects_ancestor_identity_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ancestor = tmp_path / "original-ancestor"
    source = ancestor / "skill"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("Run safely.\n", encoding="utf-8")
    displaced = tmp_path / "displaced-ancestor"
    real_open = os.open
    directory_flags = extension_policy._required_directory_flags(
        "extension_snapshot_platform_unsupported"
    )
    parent_identity = (tmp_path.stat().st_dev, tmp_path.stat().st_ino)
    swapped = False

    def open_after_ancestor_swap(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        opened_parent = os.fstat(dir_fd) if dir_fd is not None else None
        if (
            not swapped
            and path == ancestor.name
            and opened_parent is not None
            and (opened_parent.st_dev, opened_parent.st_ino) == parent_identity
        ):
            ancestor.rename(displaced)
            ancestor.mkdir()
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        extension_policy,
        "_required_directory_flags",
        lambda _error: directory_flags,
    )
    monkeypatch.setattr(extension_policy.os, "open", open_after_ancestor_swap)

    with pytest.raises(ExtensionPolicyError, match="changed_during_read"):
        extension_tree_digest(source)
    assert swapped is True


@pytest.mark.skipif(os.name == "nt", reason="descriptor traversal is POSIX-only")
def test_read_scope_allows_metadata_churn_in_an_intermediate_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    ancestor = workspace / "changing-ancestor"
    granted = ancestor / "granted"
    granted.mkdir(parents=True)
    (granted / "payload.txt").write_text("bounded\n", encoding="utf-8")
    scopes = parse_extension_scopes(
        {"filesystem": [{"path": "changing-ancestor/granted", "access": "read"}]}
    )
    resolved = resolve_filesystem_scopes(scopes, workspace)
    real_open = os.open
    directory_flags = extension_policy._required_scope_directory_flags()
    workspace_identity = (workspace.stat().st_dev, workspace.stat().st_ino)
    ancestor_before = ancestor.stat()
    churned = False

    def open_after_scope_ancestor_churn(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o600,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal churned
        opened_parent = os.fstat(dir_fd) if dir_fd is not None else None
        if (
            not churned
            and path == ancestor.name
            and opened_parent is not None
            and (opened_parent.st_dev, opened_parent.st_ino) == workspace_identity
        ):
            metadata = ancestor.stat()
            os.utime(
                ancestor,
                ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 2_000_000_000),
            )
            churned = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(
        extension_policy,
        "_required_scope_directory_flags",
        lambda: directory_flags,
    )
    monkeypatch.setattr(extension_policy.os, "open", open_after_scope_ancestor_churn)

    copied = copy_readonly_filesystem_scope_snapshots(
        resolved,
        tmp_path / "scope-snapshots",
        workspace=workspace,
    )

    assert copied[0].source.joinpath("payload.txt").read_text(encoding="utf-8") == "bounded\n"
    assert churned is True
    ancestor_after = ancestor.stat()
    assert extension_policy._same_stat_identity(ancestor_before, ancestor_after)
    assert not extension_policy._same_stat_snapshot(ancestor_before, ancestor_after)


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


def test_windows_path_fallback_copies_extension_and_read_scope_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extension_policy,
        "_uses_windows_path_fallback",
        lambda: True,
    )
    source = tmp_path / "skill"
    nested = source / "nested"
    nested.mkdir(parents=True)
    (source / "SKILL.md").write_text("Run safely.\n", encoding="utf-8")
    (nested / "tool.py").write_text("print('safe')\n", encoding="utf-8")

    expected_digest = extension_tree_digest(source)
    snapshot = tmp_path / "skill-snapshot"
    assert copy_extension_snapshot(source, snapshot) == expected_digest
    assert (snapshot / "nested" / "tool.py").read_text(encoding="utf-8") == (
        "print('safe')\n"
    )

    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs" / "nested"
    inputs.mkdir(parents=True)
    (inputs / "payload.txt").write_text("bounded\n", encoding="utf-8")
    scopes = parse_extension_scopes(
        {"filesystem": [{"path": "inputs", "access": "read"}]}
    )
    copied = copy_readonly_filesystem_scope_snapshots(
        resolve_filesystem_scopes(scopes, workspace),
        tmp_path / "scope-snapshots",
        workspace=workspace,
    )

    assert copied[0].source.joinpath("nested", "payload.txt").read_text(
        encoding="utf-8"
    ) == "bounded\n"


def test_windows_path_fallback_rejects_ambiguous_nested_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        extension_policy,
        "_uses_windows_path_fallback",
        lambda: True,
    )
    source = tmp_path / "skill"
    source.mkdir()
    (source / ".git.").mkdir()

    with pytest.raises(
        ExtensionPolicyError,
        match="tree_(?:path_not_portable|control_tree_rejected)",
    ):
        extension_tree_digest(source)

    workspace = tmp_path / "workspace"
    inputs = workspace / "inputs"
    inputs.mkdir(parents=True)
    (inputs / ".nest ").mkdir()
    scopes = parse_extension_scopes(
        {"filesystem": [{"path": "inputs", "access": "read"}]}
    )
    resolved = resolve_filesystem_scopes(scopes, workspace)

    with pytest.raises(
        ExtensionPolicyError,
        match="scope_(?:path_not_portable|control_tree_rejected)",
    ):
        copy_readonly_filesystem_scope_snapshots(
            resolved,
            tmp_path / "scope-snapshots-ambiguous",
            workspace=workspace,
        )


def test_windows_snapshot_mode_helper_keeps_temp_files_cleanup_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_file = tmp_path / "snapshot.txt"
    snapshot_file.write_text("temporary\n", encoding="utf-8")
    before = snapshot_file.stat().st_mode
    monkeypatch.setattr(extension_policy.os, "name", "nt")

    extension_policy._chmod_snapshot_path(snapshot_file, 0o400)

    assert snapshot_file.stat().st_mode == before
