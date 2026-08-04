from __future__ import annotations

import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from threading import Event
from time import monotonic

import pytest
from fastapi.testclient import TestClient

from nested_memvid_agent import server as server_module
from nested_memvid_agent import (
    server_desktop_recovery_routes as recovery_routes_module,
)
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.desktop_bootstrap import DesktopLaunchConfig
from nested_memvid_agent.desktop_memory_health import (
    capture_desktop_memvid_preflight_receipt,
)
from nested_memvid_agent.layers import DEFAULT_LAYER_SPECS
from nested_memvid_agent.run_manager import RunManager
from nested_memvid_agent.server import create_app


def _config(profile_root: Path) -> AgentConfig:
    workspace = profile_root / "workspace"
    workspace.mkdir(parents=True)
    return AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=profile_root / "memory",
        log_dir=profile_root / "logs",
        state_path=profile_root / "state" / "agent.db",
        secret_store_path=profile_root / "secrets" / "vault.json",
        workspace=workspace,
        skills_dir=profile_root / "skills",
        plugins_dir=profile_root / "plugins",
        mcp_config_path=profile_root / "config" / "mcp.json",
        channel_config_path=profile_root / "config" / "channels.json",
        worker_worktree_dir=profile_root / "worktrees",
        require_api_auth=False,
    )


def _desktop_context(profile_root: Path) -> DesktopLaunchConfig:
    return DesktopLaunchConfig(
        profile_id="default",
        profile_root=profile_root,
        state_path=profile_root / "state" / "agent.db",
        memory_dir=profile_root / "memory",
        runtime_settings_path=(profile_root / "config" / "runtime_settings.json"),
        launch_nonce="launch-nonce",
        api_token="desktop-token",
        parent_pid=4242,
        parent_birth_marker="desktop-parent-birth-marker",
        resource_manifest_digest="sha256:" + ("a" * 64),
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer desktop-token"}


def _context_with_receipt(
    profile_root: Path,
) -> DesktopLaunchConfig:
    memory_dir = profile_root / "memory"
    memory_dir.mkdir(mode=0o700)
    memory_dir.chmod(0o700)
    for spec in DEFAULT_LAYER_SPECS.values():
        path = memory_dir / spec.mv2_file
        path.write_bytes(b"startup-opened-mv2")
        path.chmod(0o600)
    launch = _desktop_context(profile_root)
    receipt = capture_desktop_memvid_preflight_receipt(memory_dir).bind(
        launch_nonce_digest=sha256(launch.launch_nonce.encode("utf-8")).hexdigest(),
        resource_manifest_digest=launch.resource_manifest_digest,
    )
    return replace(launch, memory_preflight_receipt=receipt)


def test_recovery_routes_are_desktop_only_and_authenticated(
    tmp_path: Path,
) -> None:
    browser_root = tmp_path / "browser"
    browser_root.mkdir()
    desktop_root = tmp_path / "desktop"
    desktop_root.mkdir()

    with TestClient(create_app(_config(browser_root))) as browser:
        assert browser.get("/api/desktop/recovery").status_code == 404
        assert (
            browser.post(
                "/api/desktop/recovery/retry",
                json={
                    "schema": "kestrel.desktop.recovery-retry.v1",
                    "action": "retry_readiness",
                },
            ).status_code
            == 404
        )

    with TestClient(
        create_app(
            _config(desktop_root),
            desktop_context=_desktop_context(desktop_root),
        )
    ) as desktop:
        assert desktop.get("/api/desktop/recovery").status_code == 401
        assert desktop.get("/api/desktop/recovery/support-bundle-preview").status_code == 401
        assert (
            desktop.get(
                "/api/desktop/recovery",
                headers=_headers(),
            ).status_code
            == 200
        )


def test_recovery_retry_is_strict_bounded_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "profile"
    root.mkdir()
    monkeypatch.setattr(
        server_module,
        "inspect_desktop_memvid_readiness",
        lambda _memory_dir, **_kwargs: True,
    )
    app = create_app(
        _config(root),
        desktop_context=_desktop_context(root),
    )

    with TestClient(app) as client:
        before = client.get(
            "/api/desktop/recovery",
            headers=_headers(),
        ).json()
        accepted = client.post(
            "/api/desktop/recovery/retry",
            headers=_headers(),
            json={
                "schema": "kestrel.desktop.recovery-retry.v1",
                "action": "retry_readiness",
            },
        )
        extra = client.post(
            "/api/desktop/recovery/retry",
            headers=_headers(),
            json={
                "schema": "kestrel.desktop.recovery-retry.v1",
                "action": "retry_readiness",
                "unexpected": True,
            },
        )
        oversized = client.post(
            "/api/desktop/recovery/retry",
            headers={
                **_headers(),
                "Content-Type": "application/json",
            },
            content=json.dumps(
                {
                    "schema": "kestrel.desktop.recovery-retry.v1",
                    "action": "retry_readiness",
                    "padding": "x" * 2_048,
                }
            ),
        )
        after = client.get(
            "/api/desktop/recovery",
            headers=_headers(),
        ).json()

    assert accepted.status_code == 200
    assert accepted.json()["schema"] == ("kestrel.desktop.recovery-retry-result.v1")
    assert accepted.json()["accepted"] is True
    assert extra.status_code == 400
    assert extra.json()["detail"] == "invalid_desktop_recovery_request"
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == ("desktop_recovery_request_too_large")
    assert after == before


def test_support_preview_is_metadata_only_and_bounded(
    tmp_path: Path,
) -> None:
    root = tmp_path / "profile"
    root.mkdir()
    app = create_app(
        _config(root),
        desktop_context=_desktop_context(root),
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/desktop/recovery/support-bundle-preview",
            headers=_headers(),
        )

    assert response.status_code == 200
    payload = response.json()
    rendered = json.dumps(payload, sort_keys=True)
    assert payload["redacted"] is True
    assert len(rendered.encode("utf-8")) <= 32 * 1024
    assert str(root) not in rendered
    assert "desktop-token" not in rendered
    assert "launch-nonce" not in rendered


def test_desktop_recovery_uses_live_memvid_readiness_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "profile"
    root.mkdir()
    inspected: list[Path] = []

    context = _context_with_receipt(root)

    def unavailable(memory_dir: Path, **kwargs: object) -> bool:
        inspected.append(memory_dir)
        assert kwargs["receipt"] is context.memory_preflight_receipt
        assert kwargs["launch_nonce_digest"] == (
            context.memory_preflight_receipt.launch_nonce_digest
        )
        assert kwargs["resource_manifest_digest"] == (context.resource_manifest_digest)
        return False

    monkeypatch.setattr(
        server_module,
        "inspect_desktop_memvid_readiness",
        unavailable,
    )
    app = create_app(
        _config(root),
        desktop_context=context,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/desktop/recovery",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["memory"] == {"ready": False}
    assert "memvid_reopen_failed" in response.json()["blockers"]
    assert inspected == [root / "memory"]


def test_successful_runtime_close_refreshes_generation_bound_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "profile"
    root.mkdir()
    context = _context_with_receipt(root)
    configured_observers: list[object] = []
    inspected_receipts: list[object] = []
    original_configure = RunManager.configure_memory_close_observer

    def capture_observer(
        self: RunManager,
        observer: object,
    ) -> None:
        configured_observers.append(observer)
        original_configure(self, observer)  # type: ignore[arg-type]

    def inspect(
        _memory_dir: Path,
        **kwargs: object,
    ) -> bool:
        inspected_receipts.append(kwargs["receipt"])
        return True

    monkeypatch.setattr(
        RunManager,
        "configure_memory_close_observer",
        capture_observer,
    )
    monkeypatch.setattr(
        server_module,
        "inspect_desktop_memvid_readiness",
        inspect,
    )
    app = create_app(
        _config(root),
        desktop_context=context,
    )
    assert len(configured_observers) == 1

    changed = root / "memory" / "working.mv2"
    changed.write_bytes(b"runtime-updated-mv2")
    changed.chmod(0o600)
    observer = configured_observers[0]
    assert callable(observer)
    observer()

    with TestClient(app) as client:
        response = client.get(
            "/api/desktop/recovery",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json()["memory"] == {"ready": True}
    assert len(inspected_receipts) == 1
    refreshed = inspected_receipts[0]
    assert refreshed is not context.memory_preflight_receipt
    assert refreshed.launch_nonce_digest == sha256(context.launch_nonce.encode("utf-8")).hexdigest()
    assert refreshed.resource_manifest_digest == context.resource_manifest_digest


def test_recovery_retry_times_out_off_event_loop_and_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "profile"
    root.mkdir()
    entered = Event()
    release = Event()

    def blocking_probe(_memory_dir: Path, **_kwargs: object) -> bool:
        entered.set()
        release.wait(timeout=2)
        return True

    monkeypatch.setattr(
        server_module,
        "inspect_desktop_memvid_readiness",
        blocking_probe,
    )
    monkeypatch.setattr(
        recovery_routes_module,
        "_RECOVERY_INSPECTION_TIMEOUT_SECONDS",
        0.025,
        raising=False,
    )
    app = create_app(
        _config(root),
        desktop_context=_desktop_context(root),
    )

    try:
        with TestClient(app) as client:
            started = monotonic()
            response = client.post(
                "/api/desktop/recovery/retry",
                headers=_headers(),
                json={
                    "schema": "kestrel.desktop.recovery-retry.v1",
                    "action": "retry_readiness",
                },
            )
            elapsed = monotonic() - started
    finally:
        release.set()

    assert entered.is_set()
    assert elapsed < 0.5
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["report"]["blockers"] == ["recovery_inspection_unavailable"]
