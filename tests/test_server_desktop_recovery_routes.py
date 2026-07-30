from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nested_memvid_agent import server as server_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.desktop_bootstrap import DesktopLaunchConfig
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
        runtime_settings_path=(
            profile_root / "config" / "runtime_settings.json"
        ),
        launch_nonce="launch-nonce",
        api_token="desktop-token",
        parent_pid=4242,
        parent_birth_marker="desktop-parent-birth-marker",
        resource_manifest_digest="sha256:" + ("a" * 64),
    )


def _headers() -> dict[str, str]:
    return {"Authorization": "Bearer desktop-token"}


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
        assert desktop.get(
            "/api/desktop/recovery/support-bundle-preview"
        ).status_code == 401
        assert desktop.get(
            "/api/desktop/recovery",
            headers=_headers(),
        ).status_code == 200


def test_recovery_retry_is_strict_bounded_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "profile"
    root.mkdir()
    monkeypatch.setattr(
        server_module,
        "inspect_desktop_memvid_readiness",
        lambda _memory_dir: True,
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
    assert accepted.json()["schema"] == (
        "kestrel.desktop.recovery-retry-result.v1"
    )
    assert accepted.json()["accepted"] is True
    assert extra.status_code == 400
    assert extra.json()["detail"] == "invalid_desktop_recovery_request"
    assert oversized.status_code == 413
    assert oversized.json()["detail"] == (
        "desktop_recovery_request_too_large"
    )
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

    def unavailable(memory_dir: Path) -> bool:
        inspected.append(memory_dir)
        return False

    monkeypatch.setattr(
        server_module,
        "inspect_desktop_memvid_readiness",
        unavailable,
    )
    app = create_app(
        _config(root),
        desktop_context=_desktop_context(root),
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
