from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient

import nested_memvid_agent.server as server_module
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.desktop_bootstrap import DesktopLaunchConfig
from nested_memvid_agent.server import create_app
from nested_memvid_agent.server_desktop_routes import DesktopShutdownController

_MEMORY_LAYERS = [
    "working",
    "episodic",
    "semantic",
    "procedural",
    "self",
    "policy",
]


def _config(
    profile_root: Path,
    *,
    cors_origins: tuple[str, ...] = (),
) -> AgentConfig:
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
        cors_origins=cors_origins,
    )


def _desktop_context(profile_root: Path) -> DesktopLaunchConfig:
    return DesktopLaunchConfig(
        profile_id="default",
        profile_root=profile_root,
        state_path=profile_root / "state" / "agent.db",
        memory_dir=profile_root / "memory",
        runtime_settings_path=profile_root / "config" / "runtime_settings.json",
        launch_nonce="launch-nonce",
        api_token="desktop-token",
        parent_pid=4242,
        parent_birth_marker="desktop-parent-birth-marker",
        resource_manifest_digest="sha256:" + ("a" * 64),
    )


def test_desktop_readiness_is_auth_and_nonce_digest_bound(tmp_path: Path) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    app = create_app(_config(profile_root), desktop_context=_desktop_context(profile_root))

    with TestClient(app) as client:
        unauthorized = client.get("/api/desktop/readiness")
        wrong_token = client.get(
            "/api/desktop/readiness",
            headers={"Authorization": "Bearer wrong-token"},
        )
        response = client.get(
            "/api/desktop/readiness",
            headers={"Authorization": "Bearer desktop-token"},
        )

    assert unauthorized.status_code == 401
    assert wrong_token.status_code == 401
    assert response.status_code == 200
    assert response.json() == {
        "schema": "kestrel.desktop.readiness.v1",
        "ready": True,
        "profile_id": "default",
        "launch_nonce_digest": sha256(b"launch-nonce").hexdigest(),
        "sidecar_version": "0.5.0",
        "state_schema_version": 21,
        "routing_schema_version": 2,
        "memory_layers": list(_MEMORY_LAYERS),
    }
    assert "desktop-token" not in response.text
    assert "launch-nonce" not in response.text
    serialized = json.dumps(response.json(), sort_keys=True)
    assert "desktop-token" not in serialized
    assert "launch-nonce" not in serialized


def test_browser_server_does_not_expose_desktop_readiness(tmp_path: Path) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()

    with TestClient(create_app(_config(profile_root))) as client:
        health = client.get("/api/health")
        response = client.get("/api/desktop/readiness")

    assert health.status_code == 200
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_desktop_cors_replaces_runtime_origins_and_keeps_preflight_tokenless(
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    app = create_app(
        _config(
            profile_root,
            cors_origins=(
                "http://127.0.0.1:5173",
                "https://configured.example",
            ),
        ),
        desktop_context=_desktop_context(profile_root),
    )

    with TestClient(app) as client:
        preflight = client.options(
            "/api/health",
            headers={
                "Origin": "kestrel://app",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        unauthenticated = client.get(
            "/api/health",
            headers={"Origin": "kestrel://app"},
        )
        authenticated = client.get(
            "/api/health",
            headers={
                "Origin": "kestrel://app",
                "Authorization": "Bearer desktop-token",
            },
        )
        configured_origin = client.get(
            "/api/health",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Authorization": "Bearer desktop-token",
            },
        )
        lookalike_origin = client.get(
            "/api/health",
            headers={
                "Origin": "kestrel://app.evil",
                "Authorization": "Bearer desktop-token",
            },
        )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "kestrel://app"
    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.headers["access-control-allow-origin"] == "kestrel://app"
    assert configured_origin.status_code == 403
    assert "access-control-allow-origin" not in configured_origin.headers
    assert lookalike_origin.status_code == 403
    assert "access-control-allow-origin" not in lookalike_origin.headers


def test_browser_server_retains_its_configured_cors_origin(tmp_path: Path) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    app = create_app(
        _config(
            profile_root,
            cors_origins=("http://127.0.0.1:5173",),
        )
    )

    with TestClient(app) as client:
        preflight = client.options(
            "/api/health",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert preflight.status_code == 200
    assert (
        preflight.headers["access-control-allow-origin"]
        == "http://127.0.0.1:5173"
    )


def test_built_spa_reserves_unknown_api_paths_for_exact_404s(
    tmp_path: Path,
    monkeypatch,
) -> None:
    web_dist = tmp_path / "web-dist"
    web_dist.mkdir()
    (web_dist / "index.html").write_text(
        "<!doctype html><title>Kestrel SPA</title>",
        encoding="utf-8",
    )
    monkeypatch.setattr(server_module, "_resolve_web_dist", lambda: web_dist)
    profile_root = tmp_path / "profile"
    profile_root.mkdir()

    with TestClient(create_app(_config(profile_root))) as client:
        api_responses = [
            client.get("/api/desktop/readiness"),
            client.post("/api/desktop/shutdown"),
            client.put("/api/not-registered"),
            client.patch("/api/not-registered"),
            client.delete("/api/not-registered"),
            client.options("/api/not-registered"),
            client.request("TRACE", "/api/not-registered"),
            client.request("CONNECT", "/api/not-registered"),
        ]
        api_head = client.head("/api/not-registered")
        spa = client.get("/settings/deep-link")

    assert all(response.status_code == 404 for response in api_responses)
    assert all(
        response.json() == {"detail": "Not Found"}
        for response in api_responses
    )
    assert api_head.status_code == 404
    assert spa.status_code == 200
    assert "Kestrel SPA" in spa.text


def test_desktop_shutdown_is_authenticated_idempotent_and_desktop_only(
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    controller = DesktopShutdownController()
    shutdown_requests: list[str] = []
    controller.bind(lambda: shutdown_requests.append("requested"))
    app = create_app(
        _config(profile_root),
        desktop_context=_desktop_context(profile_root),
        desktop_shutdown=controller,
    )

    with TestClient(app) as client:
        unauthorized = client.post("/api/desktop/shutdown")
        wrong_token = client.post(
            "/api/desktop/shutdown",
            headers={"Authorization": "Bearer wrong-token"},
        )
        first = client.post(
            "/api/desktop/shutdown",
            headers={"Authorization": "Bearer desktop-token"},
        )
        repeated = client.post(
            "/api/desktop/shutdown",
            headers={"Authorization": "Bearer desktop-token"},
        )

    assert unauthorized.status_code == 401
    assert wrong_token.status_code == 401
    assert first.status_code == 202
    assert first.json() == {
        "schema": "kestrel.desktop.shutdown.v1",
        "accepted": True,
    }
    assert repeated.status_code == 202
    assert repeated.json() == first.json()
    assert shutdown_requests == ["requested"]

    browser_root = tmp_path / "browser-profile"
    browser_root.mkdir()
    with TestClient(create_app(_config(browser_root))) as client:
        absent = client.post(
            "/api/desktop/shutdown",
            headers={"Authorization": "Bearer desktop-token"},
        )
    assert absent.status_code == 404
