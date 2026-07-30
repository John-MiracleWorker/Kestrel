from __future__ import annotations

from pathlib import Path
from typing import Any

from nested_memvid_agent.desktop_memory_health import (
    inspect_desktop_memvid_readiness,
)
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS
from nested_memvid_agent.models import MemoryLayer


class _Backend:
    def __init__(
        self,
        *,
        path: Path,
        layer: MemoryLayer,
        read_only: bool,
        path_lock_blocking: bool,
        calls: list[tuple[str, object]],
        fail_open: bool = False,
        fail_close: bool = False,
    ) -> None:
        calls.append(
            (
                "construct",
                (path, layer, read_only, path_lock_blocking),
            )
        )
        self.path = path
        self.calls = calls
        self.fail_open = fail_open
        self.fail_close = fail_close

    def open(self) -> None:
        self.calls.append(("open", self.path))
        if self.fail_open:
            raise RuntimeError("sentinel-open-failure")

    def close(self) -> None:
        self.calls.append(("close", self.path))
        if self.fail_close:
            raise RuntimeError("sentinel-close-failure")


def _seed_layer_paths(memory_dir: Path) -> None:
    memory_dir.mkdir()
    for spec in DEFAULT_LAYER_SPECS.values():
        (memory_dir / spec.mv2_file).write_bytes(b"test-mv2")


def test_memvid_recovery_probe_opens_every_existing_layer_read_only(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    calls: list[tuple[str, object]] = []

    def factory(**kwargs: Any) -> _Backend:
        return _Backend(calls=calls, **kwargs)

    assert inspect_desktop_memvid_readiness(
        memory_dir,
        backend_factory=factory,
    )
    constructs = [
        payload for action, payload in calls if action == "construct"
    ]
    assert len(constructs) == len(DEFAULT_LAYER_SPECS)
    assert all(read_only is True for _, _, read_only, _ in constructs)
    assert all(
        path_lock_blocking is False
        for _, _, _, path_lock_blocking in constructs
    )
    assert sum(action == "close" for action, _ in calls) == len(
        DEFAULT_LAYER_SPECS
    )


def test_memvid_recovery_probe_fails_closed_without_creating_missing_layer(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    calls: list[tuple[str, object]] = []

    def factory(**kwargs: Any) -> _Backend:
        return _Backend(calls=calls, **kwargs)

    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        backend_factory=factory,
    )
    assert calls == []
    assert list(memory_dir.iterdir()) == []


def test_memvid_recovery_probe_closes_prior_layers_and_fails_closed(
    tmp_path: Path,
) -> None:
    memory_dir = tmp_path / "memory"
    _seed_layer_paths(memory_dir)
    calls: list[tuple[str, object]] = []
    construction_count = 0

    def factory(**kwargs: Any) -> _Backend:
        nonlocal construction_count
        construction_count += 1
        return _Backend(
            calls=calls,
            fail_open=construction_count == 2,
            **kwargs,
        )

    assert not inspect_desktop_memvid_readiness(
        memory_dir,
        backend_factory=factory,
    )
    closed = [payload for action, payload in calls if action == "close"]
    assert closed == [memory_dir / "working.mv2"]
