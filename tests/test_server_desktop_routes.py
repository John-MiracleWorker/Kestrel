from __future__ import annotations

import hmac
import json
from hashlib import sha256
from importlib import metadata as importlib_metadata
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import nested_memvid_agent.server as server_module
import nested_memvid_agent.server_desktop_routes as desktop_routes
from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.desktop_bootstrap import DesktopLaunchConfig
from nested_memvid_agent.desktop_credentials import (
    DesktopCredentialReadiness,
)
from nested_memvid_agent.secret_broker import (
    SecretBroker,
    SecretBrokerPartialCommitError,
)
from nested_memvid_agent.security_boundary import redact_text
from nested_memvid_agent.server import create_app
from nested_memvid_agent.server_desktop_routes import DesktopShutdownController

_PACKAGE_VERSION = importlib_metadata.version("nested-memvid-agent")

_MEMORY_LAYERS = [
    "working",
    "episodic",
    "semantic",
    "procedural",
    "self",
    "policy",
]

_DESKTOP_PROVIDERS = [
    ("openai", "OpenAI", "OPENAI_API_KEY", "openai_api_key"),
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY", "openrouter_api_key"),
    ("deepseek", "DeepSeek", "DEEPSEEK_API_KEY", "deepseek_api_key"),
    ("kimi", "Kimi", "MOONSHOT_API_KEY", "moonshot_api_key"),
    ("ollama-cloud", "Ollama Cloud", "OLLAMA_API_KEY", "ollama_api_key"),
    ("anthropic", "Anthropic", "ANTHROPIC_API_KEY", "anthropic_api_key"),
    ("grok", "Grok / xAI", "XAI_API_KEY", "xai_api_key"),
    ("gemini", "Gemini", "GEMINI_API_KEY", "gemini_api_key"),
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


def _test_capability(launch: DesktopLaunchConfig) -> str:
    message = (
        "kestrel.desktop.credential.write.v1\0" + launch.launch_nonce
    ).encode()
    return hmac.new(
        launch.api_token.encode(),
        message,
        "sha256",
    ).hexdigest()


class _RecordingSecretBroker(SecretBroker):
    def __init__(self, vault_path: Path) -> None:
        super().__init__(vault_path)
        self.store_calls: list[dict[str, object]] = []

    def store_secret(
        self,
        *,
        name: str,
        purpose: str,
        value: str,
        secret_id: str | None = None,
        validate: bool = False,
    ) -> dict[str, object]:
        self.store_calls.append(
            {
                "name": name,
                "purpose": purpose,
                "value": value,
                "secret_id": secret_id,
                "validate": validate,
            }
        )
        resolved_id = str(secret_id)
        return {
            "id": resolved_id,
            "name": name,
            "purpose": purpose,
            "secret_ref": f"secret://{resolved_id}",
            "configured": True,
            "validated": bool(validate),
            "last_validated_at": None,
            "fingerprint": "sha256:0123456789ab",
            "created_at": "2026-07-30T00:00:00+00:00",
            "updated_at": "2026-07-30T00:00:00+00:00",
            "source": "broker",
        }


def _credential_test_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[object, DesktopLaunchConfig, _RecordingSecretBroker]:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    launch = _desktop_context(profile_root)
    broker = _RecordingSecretBroker(
        tmp_path / "recording-keyring-metadata.json"
    )
    monkeypatch.setattr(
        server_module,
        "build_secret_broker",
        lambda *_args, **_kwargs: broker,
    )
    return (
        create_app(
            _config(profile_root),
            desktop_context=launch,
        ),
        launch,
        broker,
    )


def _credential_headers(
    launch: DesktopLaunchConfig,
    *,
    content_type: str = "application/octet-stream",
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {launch.api_token}",
        "X-Kestrel-Desktop-Credential-Capability": (
            _test_capability(launch)
        ),
        "Content-Type": content_type,
    }


def test_product_setup_reads_current_dynamic_credential_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    launch = _desktop_context(profile_root)
    broker = _RecordingSecretBroker(
        tmp_path / "dynamic-keyring-metadata.json"
    )
    broker.desktop_readiness = DesktopCredentialReadiness(
        state="unavailable",
        backend="macOS Keychain",
        persistence="none",
        reason="backend_unverified",
        remediation=(
            "Complete an authorized credential operation to verify "
            "the operating system credential backend."
        ),
    )
    monkeypatch.setattr(
        server_module,
        "build_secret_broker",
        lambda *_args, **_kwargs: broker,
    )
    app = create_app(
        _config(profile_root),
        desktop_context=launch,
    )

    with TestClient(app) as client:
        initial = client.get(
            "/api/product/setup",
            headers={"Authorization": "Bearer desktop-token"},
        )
        broker.desktop_readiness = DesktopCredentialReadiness(
            state="available",
            backend="macOS Keychain",
            persistence="persistent",
            reason="ready",
            remediation="No recovery needed.",
        )
        updated = client.get(
            "/api/product/setup",
            headers={"Authorization": "Bearer desktop-token"},
        )
        broker.desktop_readiness = None
        missing = client.get(
            "/api/product/setup",
            headers={"Authorization": "Bearer desktop-token"},
        )

    assert initial.status_code == 200
    assert initial.json()["credential_storage"]["reason"] == (
        "backend_unverified"
    )
    assert updated.status_code == 200
    assert updated.json()["credential_storage"] == (
        DesktopCredentialReadiness(
            state="available",
            backend="macOS Keychain",
            persistence="persistent",
            reason="ready",
            remediation="No recovery needed.",
        ).to_public_payload()
    )
    assert missing.status_code == 200
    assert missing.json()["credential_storage"]["reason"] == (
        "metadata_invalid"
    )


def test_desktop_credential_capability_matches_shared_vector() -> None:
    fixture = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "desktop-canonical-vectors.json"
        ).read_text(encoding="utf-8")
    )["credential_capability"]

    assert (
        desktop_routes.derive_desktop_credential_capability(
            fixture["api_token"],
            fixture["launch_nonce"],
        )
        == fixture["hmac_sha256_hex"]
    )


@pytest.mark.parametrize(
    ("provider_id", "label", "name", "secret_id"),
    _DESKTOP_PROVIDERS,
)
def test_desktop_server_owns_exact_provider_identity(
    provider_id: str,
    label: str,
    name: str,
    secret_id: str,
) -> None:
    assert desktop_routes.desktop_credential_provider(provider_id) == {
        "provider_id": provider_id,
        "label": label,
        "name": name,
        "secret_id": secret_id,
        "purpose": f"Desktop provider API key for {label}.",
    }


@pytest.mark.parametrize(
    "provider_id",
    [
        "mock",
        "ollama",
        "lm-studio",
        "codex-cli",
        "openai-compatible",
        "OPENAI",
        " openai",
        "openai ",
        "custom",
    ],
)
def test_desktop_server_rejects_noncanonical_provider_identity(
    provider_id: str,
) -> None:
    with pytest.raises(ValueError, match="invalid_desktop_request"):
        desktop_routes.desktop_credential_provider(provider_id)


def test_desktop_secret_mutation_requires_capability_before_body_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    launch = _desktop_context(profile_root)
    cache_calls: list[str] = []
    original_cache = server_module._cache_bounded_request_body

    async def observed_cache(request: object, *, limit: int) -> int:
        cache_calls.append(str(getattr(getattr(request, "url", None), "path", "")))
        return await original_cache(request, limit=limit)

    monkeypatch.setattr(
        server_module,
        "_cache_bounded_request_body",
        observed_cache,
    )
    app = create_app(_config(profile_root), desktop_context=launch)
    bearer_only = {
        "Authorization": f"Bearer {launch.api_token}",
    }

    with TestClient(app) as client:
        generic_mutations = [
            client.post(
                "/api/secrets",
                headers=bearer_only,
                json={
                    "name": "OPENAI_API_KEY",
                    "purpose": "must be main-owned",
                    "value": "early-gate-private-sentinel",
                    "validate": False,
                },
            ),
            client.post(
                "/api/secrets/openai_api_key/validate",
                headers=bearer_only,
            ),
            client.delete(
                "/api/secrets/openai_api_key",
                headers=bearer_only,
            ),
        ]
        dedicated = client.post(
            "/api/desktop/credentials/providers/openai",
            headers={
                **bearer_only,
                "Content-Type": "application/octet-stream",
            },
            content=b"early-gate-private-sentinel",
        )

    assert [response.status_code for response in generic_mutations] == [
        403,
        403,
        403,
    ]
    assert all(
        response.json()
        == {"detail": "desktop_credential_capability_required"}
        for response in generic_mutations
    )
    assert dedicated.status_code == 403
    assert dedicated.json() == {
        "detail": "desktop_credential_capability_required"
    }
    assert cache_calls == []
    vault = profile_root / "secrets" / "vault.json"
    assert (
        not vault.exists()
        or b"early-gate-private-sentinel" not in vault.read_bytes()
    )


def test_desktop_capability_is_fixed_format_and_not_bearer_equivalent(
    tmp_path: Path,
) -> None:
    launch = _desktop_context(tmp_path)
    capability = _test_capability(launch)

    assert len(capability) == 64
    assert capability != launch.api_token
    assert desktop_routes.desktop_credential_capability_error(
        launch,
        {
            "authorization": f"Bearer {launch.api_token}",
            "x-kestrel-desktop-credential-capability": capability,
        },
    ) is None
    assert desktop_routes.desktop_credential_capability_error(
        launch,
        {
            "authorization": f"Bearer {launch.api_token}",
            "x-kestrel-desktop-credential-capability": launch.api_token,
        },
    ) == (403, "desktop_credential_capability_required")
    assert desktop_routes.desktop_credential_capability_error(
        launch,
        {
            "authorization": f"Bearer {launch.api_token}",
            "x-kestrel-desktop-credential-capability": (
                f" {_test_capability(launch)}"
            ),
        },
    ) == (403, "desktop_credential_capability_required")


def test_desktop_capability_is_sensitive_and_absent_from_readiness(
    tmp_path: Path,
) -> None:
    profile_root = tmp_path / "profile"
    profile_root.mkdir()
    launch = _desktop_context(profile_root)
    capability = _test_capability(launch)
    app = create_app(
        _config(profile_root),
        desktop_context=launch,
    )

    with TestClient(app) as client:
        response = client.get(
            "/api/desktop/readiness",
            headers={
                "Authorization": f"Bearer {launch.api_token}",
            },
        )

    assert response.status_code == 200
    assert capability not in response.text
    assert redact_text(
        f"prefix::{capability}::suffix",
        environ={},
    ) == "prefix::<redacted>::suffix"


def test_desktop_capability_uses_constant_time_fixed_format_comparison(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launch = _desktop_context(tmp_path)
    expected = _test_capability(launch).encode("ascii")
    comparisons: list[tuple[bytes, bytes]] = []
    original_compare_digest = desktop_routes.secrets.compare_digest

    def observed_compare_digest(
        candidate: bytes,
        reference: bytes,
    ) -> bool:
        comparisons.append((candidate, reference))
        return original_compare_digest(candidate, reference)

    monkeypatch.setattr(
        desktop_routes.secrets,
        "compare_digest",
        observed_compare_digest,
    )

    assert desktop_routes.desktop_credential_capability_error(
        launch,
        {
            "authorization": f"Bearer {launch.api_token}",
            "x-kestrel-desktop-credential-capability": (
                expected.decode("ascii")
            ),
        },
    ) is None
    assert desktop_routes.desktop_credential_capability_error(
        launch,
        {
            "authorization": f"Bearer {launch.api_token}",
            "x-kestrel-desktop-credential-capability": "0" * 64,
        },
    ) == (403, "desktop_credential_capability_required")
    capability_comparisons = [
        pair
        for pair in comparisons
        if all(len(value) == 64 for value in pair)
    ]
    assert capability_comparisons == [
        (expected, expected),
        (b"0" * 64, expected),
    ]


@pytest.mark.parametrize(
    ("body", "content_type", "provider_id", "status_code", "detail"),
    [
        (
            b"",
            "application/octet-stream",
            "openai",
            400,
            "invalid_desktop_credential",
        ),
        (
            b"x" * 16_385,
            "application/octet-stream",
            "openai",
            413,
            "desktop_credential_too_large",
        ),
        (
            b"\xff",
            "application/octet-stream",
            "openai",
            400,
            "invalid_desktop_credential",
        ),
        (
            b"before\x00after",
            "application/octet-stream",
            "openai",
            400,
            "invalid_desktop_credential",
        ),
        (
            b"before\rafter",
            "application/octet-stream",
            "openai",
            400,
            "invalid_desktop_credential",
        ),
        (
            b"before\nafter",
            "application/octet-stream",
            "openai",
            400,
            "invalid_desktop_credential",
        ),
        (
            b" leading",
            "application/octet-stream",
            "openai",
            400,
            "invalid_desktop_credential",
        ),
        (
            b"trailing ",
            "application/octet-stream",
            "openai",
            400,
            "invalid_desktop_credential",
        ),
        (
            b"json-is-not-reviewed",
            "application/json",
            "openai",
            415,
            "desktop_credential_content_type_required",
        ),
        (
            b"noncanonical-provider",
            "application/octet-stream",
            "OPENAI",
            400,
            "invalid_desktop_request",
        ),
        (
            b"noncanonical-provider",
            "application/octet-stream",
            "openai-compatible",
            400,
            "invalid_desktop_request",
        ),
    ],
    ids=[
        "empty",
        "too-large",
        "invalid-utf8",
        "nul",
        "carriage-return",
        "line-feed",
        "leading-whitespace",
        "trailing-whitespace",
        "wrong-content-type",
        "uppercase-provider",
        "compatible-provider",
    ],
)
def test_desktop_provider_route_rejects_invalid_bytes_before_broker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: bytes,
    content_type: str,
    provider_id: str,
    status_code: int,
    detail: str,
) -> None:
    app, launch, broker = _credential_test_app(
        tmp_path,
        monkeypatch,
    )

    with TestClient(app) as client:
        response = client.post(
            f"/api/desktop/credentials/providers/{provider_id}",
            headers=_credential_headers(
                launch,
                content_type=content_type,
            ),
            content=body,
        )

    assert response.status_code == status_code
    assert response.json() == {"detail": detail}
    assert broker.store_calls == []
    assert "noncanonical-provider" not in response.text


@pytest.mark.parametrize(
    ("provider_id", "label", "name", "secret_id"),
    _DESKTOP_PROVIDERS,
)
def test_desktop_provider_route_stores_exact_unverified_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    provider_id: str,
    label: str,
    name: str,
    secret_id: str,
) -> None:
    app, launch, broker = _credential_test_app(
        tmp_path,
        monkeypatch,
    )
    raw_value = (
        "x" * 16_384
        if provider_id == "openai"
        else f"{provider_id}-private-route-sentinel"
    )

    with TestClient(app) as client:
        response = client.post(
            f"/api/desktop/credentials/providers/{provider_id}",
            headers=_credential_headers(launch),
            content=raw_value.encode("utf-8"),
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema": "kestrel.desktop.credential-store.v1",
        "provider_id": provider_id,
        "id": secret_id,
        "name": name,
        "purpose": f"Desktop provider API key for {label}.",
        "secret_ref": f"secret://{secret_id}",
        "configured": True,
        "validated": False,
        "fingerprint": "sha256:0123456789ab",
        "source": "broker",
    }
    assert broker.store_calls == [
        {
            "name": name,
            "purpose": f"Desktop provider API key for {label}.",
            "value": raw_value,
            "secret_id": secret_id,
            "validate": False,
        }
    ]
    assert raw_value not in response.text
    assert raw_value not in caplog.text
    assert _test_capability(launch) not in caplog.text


def test_desktop_provider_route_sanitizes_partial_commit_as_ambiguous(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, launch, broker = _credential_test_app(
        tmp_path,
        monkeypatch,
    )
    raw_value = "partial-commit-private-sentinel"

    def partial_commit(**_kwargs: object) -> dict[str, object]:
        raise SecretBrokerPartialCommitError(
            operation="store",
            stage="metadata_commit_state_unknown",
            secret_ids=("openai_api_key",),
            recovery_usernames=(
                "openai_api_key.v2.private-detail",
            ),
        )

    monkeypatch.setattr(broker, "store_secret", partial_commit)

    with TestClient(app) as client:
        response = client.post(
            "/api/desktop/credentials/providers/openai",
            headers=_credential_headers(launch),
            content=raw_value.encode(),
        )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "desktop_credential_commit_ambiguous"
    }
    assert raw_value not in response.text
    assert "private-detail" not in response.text


def test_desktop_provider_route_sanitizes_unconfirmable_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, launch, broker = _credential_test_app(
        tmp_path,
        monkeypatch,
    )
    raw_value = "projection-private-sentinel"

    monkeypatch.setattr(
        broker,
        "store_secret",
        lambda **_kwargs: {
            "id": "openai_api_key",
            "name": "OPENAI_API_KEY",
            "purpose": "Desktop provider API key for OpenAI.",
            "secret_ref": "secret://openai_api_key",
            "configured": True,
            "validated": False,
            "fingerprint": None,
            "source": "broker",
        },
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/desktop/credentials/providers/openai",
            headers=_credential_headers(launch),
            content=raw_value.encode(),
        )

    assert response.status_code == 500
    assert response.json() == {
        "detail": "invalid_desktop_response"
    }
    assert raw_value not in response.text


def test_browser_secret_route_remains_compatible_without_desktop_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_root = tmp_path / "browser-profile"
    profile_root.mkdir()
    broker = _RecordingSecretBroker(
        tmp_path / "browser-vault.json"
    )
    monkeypatch.setattr(
        server_module,
        "build_secret_broker",
        lambda *_args, **_kwargs: broker,
    )
    app = create_app(_config(profile_root))

    with TestClient(app) as client:
        response = client.post(
            "/api/secrets",
            json={
                "name": "BROWSER_TEST_KEY",
                "purpose": "Browser compatibility test.",
                "value": "browser-private-sentinel",
                "validate": True,
            },
        )

    assert response.status_code == 200
    assert broker.store_calls == [
        {
            "name": "BROWSER_TEST_KEY",
            "purpose": "Browser compatibility test.",
            "value": "browser-private-sentinel",
            "secret_id": None,
            "validate": True,
        }
    ]


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
        "sidecar_version": _PACKAGE_VERSION,
        "state_schema_version": 22,
        "routing_schema_version": 5,
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


@pytest.mark.parametrize(
    ("web_dist_enabled", "deep_link_status"),
    [(False, 404), (True, 200)],
    ids=["without-built-spa", "with-built-spa"],
)
def test_spa_fallback_preserves_api_router_semantics(
    tmp_path: Path,
    monkeypatch,
    web_dist_enabled: bool,
    deep_link_status: int,
) -> None:
    web_dist: Path | None = None
    if web_dist_enabled:
        web_dist = tmp_path / "web-dist"
        web_dist.mkdir()
        (web_dist / "index.html").write_text(
            "<!doctype html><title>Kestrel SPA</title>",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        server_module,
        "_resolve_web_dist",
        lambda: web_dist,
    )
    profile_root = tmp_path / "profile"
    profile_root.mkdir()

    with TestClient(create_app(_config(profile_root))) as client:
        known_wrong_method = client.post("/api/health")
        known_trailing_slash = client.get(
            "/api/health/",
            follow_redirects=False,
        )
        known_head = client.head("/api/health")
        unknown_responses = [
            client.get("/api/desktop/readiness"),
            client.post("/api/desktop/shutdown"),
            client.get("/api/not-registered"),
            client.post("/api/not-registered"),
            client.put("/api/not-registered"),
            client.patch("/api/not-registered"),
            client.delete("/api/not-registered"),
            client.options("/api/not-registered"),
            client.request("TRACE", "/api/not-registered"),
            client.request("CONNECT", "/api/not-registered"),
            client.request("PROPFIND", "/api/not-registered"),
        ]
        unknown_head = client.head("/api/not-registered")
        spa = client.get("/settings/deep-link")

    assert known_wrong_method.status_code == 405
    assert known_wrong_method.headers["allow"] == "GET"
    assert known_wrong_method.json() == {"detail": "Method Not Allowed"}
    assert known_trailing_slash.status_code == 307
    assert (
        known_trailing_slash.headers["location"]
        == "http://testserver/api/health"
    )
    assert known_head.status_code == 405
    assert known_head.headers["allow"] == "GET"
    assert known_head.content == b""
    assert all(response.status_code == 404 for response in unknown_responses)
    assert all(
        response.json() == {"detail": "Not Found"}
        for response in unknown_responses
    )
    assert unknown_head.status_code == 404
    assert spa.status_code == deep_link_status
    if web_dist_enabled:
        assert "Kestrel SPA" in spa.text
    else:
        assert spa.json() == {"detail": "Not Found"}


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
