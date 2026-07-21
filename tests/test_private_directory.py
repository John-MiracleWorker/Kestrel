from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

import nested_memvid_agent.private_directory as private_directory
from nested_memvid_agent.private_directory import (
    PrivateDirectoryError,
    private_temporary_directory,
    validate_owner_private_directory,
)


def test_windows_private_sddl_allows_only_owner_system_and_administrators() -> None:
    current_sid = "S-1-5-21-1000-1001-1002-1003"
    safe = private_directory._windows_private_directory_sddl(current_sid)  # noqa: SLF001

    private_directory._validate_windows_private_sddl(  # noqa: SLF001
        safe,
        current_sid=current_sid,
    )

    with pytest.raises(PrivateDirectoryError, match="trustee_unsafe"):
        private_directory._validate_windows_private_sddl(  # noqa: SLF001
            safe + "(A;OICI;FA;;;WD)",
            current_sid=current_sid,
        )
    with pytest.raises(PrivateDirectoryError, match="dacl_inherited"):
        private_directory._validate_windows_private_sddl(  # noqa: SLF001
            safe.replace("D:P", "D:"),
            current_sid=current_sid,
        )


def test_windows_private_sddl_compares_machine_relative_aliases_by_full_sid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_sid = "S-1-5-21-1000-1001-1002-500"
    encoded = "O:LAD:P(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"

    monkeypatch.setattr(private_directory, "_is_windows", lambda: True)
    monkeypatch.setattr(
        private_directory,
        "_windows_expand_sddl_sid_alias",
        lambda value: current_sid if value == "LA" else value,
    )

    private_directory._validate_windows_private_sddl(  # noqa: SLF001
        encoded,
        current_sid=current_sid,
    )

    other_machine_sid = "S-1-5-21-2000-2001-2002-500"
    with pytest.raises(PrivateDirectoryError, match="wrong_windows_owner"):
        private_directory._validate_windows_private_sddl(  # noqa: SLF001
            encoded,
            current_sid=other_machine_sid,
        )


def test_windows_private_temp_portable_seam_cleans_exact_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "private-root"
    removed: list[Path] = []

    def create(*, prefix: str, parent: Path | None = None) -> Path:
        assert prefix == "kestrel-test-"
        assert parent is None
        root.mkdir()
        return root

    def remove(path: Path) -> None:
        removed.append(path)
        shutil.rmtree(path)

    monkeypatch.setattr(private_directory, "_is_windows", lambda: True)
    monkeypatch.setattr(
        private_directory,
        "_create_windows_private_temp_directory",
        create,
    )
    monkeypatch.setattr(private_directory, "_remove_private_tree", remove)

    with private_temporary_directory(prefix="kestrel-test-") as selected:
        assert selected == root
        (selected / "snapshot.txt").write_text("bounded\n", encoding="utf-8")

    assert removed == [root]
    assert not root.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode cleanup semantics required")
def test_private_temp_cleanup_recovers_hardened_snapshot_permissions(
    tmp_path: Path,
) -> None:
    retained: Path | None = None

    with private_temporary_directory(
        prefix="kestrel-hardened-",
        parent=tmp_path,
    ) as root:
        retained = root
        nested = root / "snapshot" / "nested"
        nested.mkdir(parents=True, mode=0o700)
        payload = nested / "payload.txt"
        payload.write_text("private\n", encoding="utf-8")
        payload.chmod(0o400)
        nested.chmod(0o500)
        nested.parent.chmod(0o500)
        root.chmod(0o500)

    assert retained is not None
    assert not retained.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL semantics required")
def test_windows_private_temp_has_verified_protected_acl_and_cleans() -> None:
    retained: Path | None = None

    with private_temporary_directory(prefix="kestrel-acl-test-") as root:
        retained = root
        validate_owner_private_directory(root)
        current_sid = private_directory._windows_current_user_sid()  # noqa: SLF001
        private_directory._validate_windows_private_sddl(  # noqa: SLF001
            private_directory._windows_directory_sddl(root),  # noqa: SLF001
            current_sid=current_sid,
        )
        nested = root / "nested"
        nested.mkdir()
        (nested / "payload.txt").write_text("private\n", encoding="utf-8")

    assert retained is not None
    assert not retained.exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL semantics required")
def test_windows_private_directory_rejects_everyone_allow_ace(
    tmp_path: Path,
) -> None:
    root = tmp_path / "weak-root"
    current_sid = private_directory._windows_current_user_sid()  # noqa: SLF001
    weak = (
        private_directory._windows_private_directory_sddl(current_sid)  # noqa: SLF001
        + "(A;OICI;FA;;;WD)"
    )
    private_directory._windows_create_directory_with_sddl(root, weak)  # noqa: SLF001
    try:
        with pytest.raises(PrivateDirectoryError, match="trustee_unsafe"):
            validate_owner_private_directory(root)
    finally:
        shutil.rmtree(root)


@pytest.mark.skipif(os.name != "nt", reason="native Windows ACL semantics required")
def test_windows_empty_weak_directory_can_be_explicitly_hardened(
    tmp_path: Path,
) -> None:
    root = tmp_path / "empty-weak-root"
    current_sid = private_directory._windows_current_user_sid()  # noqa: SLF001
    weak = (
        f"O:{current_sid}D:"
        f"(A;OICI;FA;;;{current_sid})"
        "(A;OICI;FA;;;WD)"
    )
    private_directory._windows_create_directory_with_sddl(root, weak)  # noqa: SLF001

    private_directory.harden_empty_owner_private_directory(root)

    validate_owner_private_directory(root)
    root.rmdir()
