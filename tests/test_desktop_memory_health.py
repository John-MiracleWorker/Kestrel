from __future__ import annotations

import stat
from pathlib import Path

from nested_memvid_agent.desktop_memory_health import (
    capture_desktop_memvid_preflight_receipt,
    inspect_desktop_memvid_readiness,
)
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS

_LAUNCH_NONCE_DIGEST = "a" * 64
_RESOURCE_MANIFEST_DIGEST = "sha256:" + ("b" * 64)
_VALID_MV2 = b"MV2\x00" + (b"x" * 300) + b"MV2FOOT!" + (b"\x00" * 64)


def _seed_layer_paths(memory_dir: Path) -> None:
    memory_dir.mkdir(mode=0o700)
    memory_dir.chmod(0o700)
    for spec in DEFAULT_LAYER_SPECS.values():
        path = memory_dir / spec.mv2_file
        path.write_bytes(_VALID_MV2)
        path.chmod(0o600)


def _bound_receipt(memory_dir: Path):
    return capture_desktop_memvid_preflight_receipt(memory_dir).bind(
        launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
    )


def _metadata_snapshot(
    root: Path,
) -> dict[str, tuple[int, int, int, int, int]]:
    paths = [root, *sorted(root.rglob("*"))]
    return {
        "." if path == root else str(path.relative_to(root)): (
            stat.S_IMODE(path.lstat().st_mode),
            path.lstat().st_size,
            path.lstat().st_mtime_ns,
            path.lstat().st_ctime_ns,
            path.lstat().st_nlink,
        )
        for path in paths
    }


def test_memvid_recovery_probe_is_bounded_nonmutating_and_generation_bound(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    receipt = _bound_receipt(memory_dir)
    probed: list[Path] = []

    def sdk_metadata_probe(filename: str) -> bool:
        probed.append(Path(filename))
        return True

    before = _metadata_snapshot(memory_dir)
    assert inspect_desktop_memvid_readiness(
        memory_dir,
        receipt=receipt,
        launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
        sdk_metadata_probe=sdk_metadata_probe,
        max_layer_bytes=len(_VALID_MV2),
    )
    assert _metadata_snapshot(memory_dir) == before
    assert probed == [memory_dir / spec.mv2_file for spec in DEFAULT_LAYER_SPECS.values()]
    assert not any(".kestrel.lock" in path.name for path in memory_dir.iterdir())


def test_memvid_recovery_probe_fails_closed_without_current_launch_receipt(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    calls: list[str] = []

    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        receipt=None,
        launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
        sdk_metadata_probe=lambda filename: calls.append(filename) is None,
    )
    assert calls == []


def test_memvid_recovery_probe_fails_closed_for_wrong_launch_generation(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    receipt = _bound_receipt(memory_dir)
    calls: list[str] = []

    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        receipt=receipt,
        launch_nonce_digest="c" * 64,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
        sdk_metadata_probe=lambda filename: calls.append(filename) is None,
    )
    assert calls == []


def test_memvid_recovery_probe_rejects_same_size_content_change(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    receipt = _bound_receipt(memory_dir)
    changed = memory_dir / "working.mv2"
    changed.write_bytes(b"MV2\x00" + (b"y" * 300) + b"MV2FOOT!" + (b"\x00" * 64))
    changed.chmod(0o600)
    calls: list[str] = []

    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        receipt=receipt,
        launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
        sdk_metadata_probe=lambda filename: calls.append(filename) is None,
    )
    assert calls == []


def test_memvid_recovery_probe_fails_closed_without_creating_missing_layer(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(mode=0o700)
    memory_dir.chmod(0o700)

    try:
        receipt = capture_desktop_memvid_preflight_receipt(memory_dir)
    except FileNotFoundError:
        receipt = None

    assert receipt is None
    assert list(memory_dir.iterdir()) == []


def test_memvid_recovery_probe_requires_mv2_header_and_bounded_footer(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    receipt = _bound_receipt(memory_dir)
    invalid = memory_dir / "working.mv2"
    invalid.write_bytes(b"NOT2" + _VALID_MV2[4:])
    invalid.chmod(0o600)
    receipt = _bound_receipt(memory_dir)
    calls: list[str] = []

    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        receipt=receipt,
        launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
        sdk_metadata_probe=lambda filename: calls.append(filename) is None,
    )
    assert calls == []

    invalid.write_bytes(b"MV2\x00" + (b"x" * (len(_VALID_MV2) - 4)))
    invalid.chmod(0o600)
    receipt = _bound_receipt(memory_dir)
    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        receipt=receipt,
        launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
        sdk_metadata_probe=lambda filename: calls.append(filename) is None,
    )
    assert calls == []


def test_memvid_recovery_probe_enforces_finite_layer_size_without_sdk_probe(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    receipt = _bound_receipt(memory_dir)
    calls: list[str] = []

    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        receipt=receipt,
        launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
        sdk_metadata_probe=lambda filename: calls.append(filename) is None,
        max_layer_bytes=len(_VALID_MV2) - 1,
    )
    assert calls == []


def test_memvid_recovery_probe_ignores_untrusted_exact_index_content(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    exact_index = memory_dir / "working.mv2.records.json"
    exact_index.write_text(
        "{invalid-and-intentionally-large:" + ("x" * 64_000),
        encoding="utf-8",
    )
    exact_index.chmod(0o000)
    receipt = _bound_receipt(memory_dir)
    calls: list[str] = []

    try:
        assert inspect_desktop_memvid_readiness(
            memory_dir,
            receipt=receipt,
            launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
            resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
            sdk_metadata_probe=lambda filename: not calls.append(filename),
        )
    finally:
        exact_index.chmod(0o600)
    assert len(calls) == len(DEFAULT_LAYER_SPECS)


def test_memvid_recovery_probe_fails_closed_when_sdk_metadata_probe_fails(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    receipt = _bound_receipt(memory_dir)
    calls: list[str] = []

    def sdk_metadata_probe(filename: str) -> bool:
        calls.append(filename)
        return len(calls) != 2

    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        receipt=receipt,
        launch_nonce_digest=_LAUNCH_NONCE_DIGEST,
        resource_manifest_digest=_RESOURCE_MANIFEST_DIGEST,
        sdk_metadata_probe=sdk_metadata_probe,
    )
    assert len(calls) == 2
