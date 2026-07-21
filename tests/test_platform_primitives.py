from __future__ import annotations

import pytest

from nested_memvid_agent import platform_primitives


def test_chmod_descriptor_uses_available_primitive(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[int, int]] = []

    def fake_fchmod(descriptor: int, mode: int) -> None:
        calls.append((descriptor, mode))

    monkeypatch.setattr(platform_primitives.os, "fchmod", fake_fchmod, raising=False)

    platform_primitives.chmod_descriptor(17, 0o600)

    assert calls == [(17, 0o600)]


def test_chmod_descriptor_fails_closed_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(platform_primitives.os, "fchmod", raising=False)

    with pytest.raises(OSError, match="descriptor mode changes are unavailable"):
        platform_primitives.chmod_descriptor(17, 0o600)


def test_signal_process_group_uses_available_primitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[int, int]] = []

    def fake_killpg(group_id: int, signal_number: int) -> None:
        calls.append((group_id, signal_number))

    monkeypatch.setattr(platform_primitives.os, "killpg", fake_killpg, raising=False)

    platform_primitives.signal_process_group(23, 15)

    assert calls == [(23, 15)]


def test_signal_process_group_fails_closed_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(platform_primitives.os, "killpg", raising=False)

    with pytest.raises(OSError, match="process-group signalling is unavailable"):
        platform_primitives.signal_process_group(23, 15)


def test_required_signal_returns_only_integer_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform_primitives.signal, "KESTREL_TEST_SIGNAL", 42, raising=False)
    assert platform_primitives.required_signal("KESTREL_TEST_SIGNAL") == 42

    monkeypatch.setattr(
        platform_primitives.signal,
        "KESTREL_TEST_SIGNAL",
        object(),
        raising=False,
    )
    with pytest.raises(OSError, match="KESTREL_TEST_SIGNAL is unavailable"):
        platform_primitives.required_signal("KESTREL_TEST_SIGNAL")
