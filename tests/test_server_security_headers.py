from pathlib import Path

from fastapi.testclient import TestClient

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.server import create_app


def _isolated_config(root: Path) -> AgentConfig:
    workspace = root / "workspace"
    workspace.mkdir()
    return AgentConfig(
        backend="memory",
        provider="mock",
        model="mock",
        memory_dir=root / "memory",
        log_dir=root / "logs",
        state_path=root / "state" / "agent.db",
        secret_store_path=root / "secrets" / "vault.json",
        workspace=workspace,
        skills_dir=root / "skills",
        plugins_dir=root / "plugins",
        mcp_config_path=root / "config" / "mcp.json",
        channel_config_path=root / "config" / "channels.json",
        worker_worktree_dir=root / "worktrees",
        require_api_auth=True,
        api_auth_token_env="KESTREL_SECURITY_HEADER_TEST_TOKEN",
    )


def test_security_headers_cover_spa_and_early_auth_errors(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KESTREL_SECURITY_HEADER_TEST_TOKEN", "local-test-token")

    with TestClient(create_app(_isolated_config(tmp_path))) as client:
        page = client.get("/")
        unauthorized = client.get("/api/health")

    assert page.status_code == 200
    assert unauthorized.status_code == 401
    for response in (page, unauthorized):
        assert response.headers["content-security-policy"] == (
            "default-src 'self'; base-uri 'none'; connect-src 'self'; "
            "font-src 'self' data:; form-action 'self'; frame-ancestors 'none'; "
            "img-src 'self' data: https:; manifest-src 'self'; object-src 'none'; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; worker-src 'self' blob:"
        )
        assert response.headers["cross-origin-opener-policy"] == "same-origin"
        assert response.headers["cross-origin-resource-policy"] == "same-origin"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "camera=()" in response.headers["permissions-policy"]
        assert "microphone=()" in response.headers["permissions-policy"]
        assert "strict-transport-security" not in response.headers
