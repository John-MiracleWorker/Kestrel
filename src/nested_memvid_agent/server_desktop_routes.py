from __future__ import annotations

import asyncio
import hmac
import re
import secrets
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from threading import Lock
from typing import Any

from starlette.requests import Request

from .desktop_bootstrap import DesktopLaunchConfig, DesktopReadiness
from .secret_broker import SecretBrokerPartialCommitError
from .security_boundary import register_secret_value

_CREDENTIAL_CAPABILITY_CONTEXT = (
    "kestrel.desktop.credential.write.v1\0"
)
_CREDENTIAL_CAPABILITY_HEADER = (
    "x-kestrel-desktop-credential-capability"
)
_CREDENTIAL_CAPABILITY_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_CREDENTIAL_BYTES = 16_384
_DESKTOP_CREDENTIAL_PROVIDERS = {
    "openai": ("OpenAI", "OPENAI_API_KEY", "openai_api_key"),
    "openrouter": (
        "OpenRouter",
        "OPENROUTER_API_KEY",
        "openrouter_api_key",
    ),
    "deepseek": (
        "DeepSeek",
        "DEEPSEEK_API_KEY",
        "deepseek_api_key",
    ),
    "kimi": ("Kimi", "MOONSHOT_API_KEY", "moonshot_api_key"),
    "ollama-cloud": (
        "Ollama Cloud",
        "OLLAMA_API_KEY",
        "ollama_api_key",
    ),
    "anthropic": (
        "Anthropic",
        "ANTHROPIC_API_KEY",
        "anthropic_api_key",
    ),
    "grok": ("Grok / xAI", "XAI_API_KEY", "xai_api_key"),
    "gemini": ("Gemini", "GEMINI_API_KEY", "gemini_api_key"),
}


class DesktopShutdownController:
    """One sidecar-scoped, idempotent request to its bound serve loop."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._callback: Callable[[], None] | None = None
        self._requested = False
        self._scheduled = False

    def bind(self, callback: Callable[[], None]) -> None:
        with self._guard:
            if self._callback is not None and self._callback is not callback:
                raise RuntimeError("desktop_shutdown_controller_already_bound")
            self._callback = callback

    def unbind(self, callback: Callable[[], None]) -> None:
        with self._guard:
            if self._callback is callback:
                self._callback = None

    def request_after_response(
        self,
        schedule: Callable[[Callable[[], None]], None],
    ) -> bool:
        callback: Callable[[], None] | None = None
        with self._guard:
            self._requested = True
            if self._callback is not None and not self._scheduled:
                self._scheduled = True
                callback = self._callback
        if callback is None:
            return self._scheduled
        schedule(callback)
        return True


def desktop_auth_error(
    launch: DesktopLaunchConfig,
    headers: Mapping[str, str],
) -> tuple[int, str] | None:
    candidate = ""
    authorization = str(headers.get("authorization", ""))
    x_kestrel_api_key = str(headers.get("x-kestrel-api-key", ""))
    if authorization and authorization.lower().startswith("bearer "):
        candidate = authorization[7:].strip()
    elif x_kestrel_api_key:
        candidate = x_kestrel_api_key.strip()
    if not candidate or not secrets.compare_digest(
        candidate.encode("utf-8"),
        launch.api_token.encode("utf-8"),
    ):
        return 401, "Invalid or missing Kestrel API token."
    return None


def derive_desktop_credential_capability(
    api_token: str,
    launch_nonce: str,
) -> str:
    message = (
        _CREDENTIAL_CAPABILITY_CONTEXT + launch_nonce
    ).encode("utf-8")
    return hmac.new(
        api_token.encode("utf-8"),
        message,
        "sha256",
    ).hexdigest()


def desktop_credential_provider(
    provider_id: str,
) -> dict[str, str]:
    authority = _DESKTOP_CREDENTIAL_PROVIDERS.get(provider_id)
    if authority is None:
        raise ValueError("invalid_desktop_request")
    label, name, secret_id = authority
    return {
        "provider_id": provider_id,
        "label": label,
        "name": name,
        "secret_id": secret_id,
        "purpose": f"Desktop provider API key for {label}.",
    }


def desktop_credential_capability_error(
    launch: DesktopLaunchConfig,
    headers: Mapping[str, str],
) -> tuple[int, str] | None:
    auth_error = desktop_auth_error(launch, headers)
    if auth_error is not None:
        return auth_error
    candidate = str(
        headers.get(_CREDENTIAL_CAPABILITY_HEADER, "")
    )
    if not _CREDENTIAL_CAPABILITY_RE.fullmatch(candidate):
        return 403, "desktop_credential_capability_required"
    expected = derive_desktop_credential_capability(
        launch.api_token,
        launch.launch_nonce,
    )
    if not secrets.compare_digest(
        candidate.encode("ascii"),
        expected.encode("ascii"),
    ):
        return 403, "desktop_credential_capability_required"
    return None


def desktop_credential_mutation_request(
    method: str,
    path: str,
) -> bool:
    normalized_method = method.upper()
    if normalized_method == "POST" and path == "/api/secrets":
        return True
    if (
        normalized_method == "POST"
        and path.startswith("/api/secrets/")
        and path.endswith("/validate")
    ):
        middle = path.removeprefix(
            "/api/secrets/"
        ).removesuffix("/validate")
        return bool(middle) and "/" not in middle
    if (
        normalized_method == "DELETE"
        and path.startswith("/api/secrets/")
    ):
        secret_id = path.removeprefix("/api/secrets/")
        return bool(secret_id) and "/" not in secret_id
    if (
        normalized_method == "POST"
        and path.startswith(
            "/api/desktop/credentials/providers/"
        )
    ):
        provider_id = path.removeprefix(
            "/api/desktop/credentials/providers/"
        )
        return bool(provider_id) and "/" not in provider_id
    return False


def register_desktop_routes(
    app: Any,
    *,
    launch: DesktopLaunchConfig,
    shutdown_controller: DesktopShutdownController | None = None,
    secret_broker: Any | None = None,
    http_exception: Any | None = None,
    sensitive_material_transition: Any | None = None,
) -> None:
    readiness: DesktopReadiness = launch.readiness()
    capability = derive_desktop_credential_capability(
        launch.api_token,
        launch.launch_nonce,
    )
    register_secret_value(capability)

    @app.get("/api/desktop/readiness")  # type: ignore[untyped-decorator]
    def desktop_readiness() -> dict[str, object]:
        return readiness.to_public_payload()

    if secret_broker is not None:
        if http_exception is None:
            raise RuntimeError(
                "desktop_credential_route_dependencies_missing"
            )

        @app.post(  # type: ignore[untyped-decorator]
            "/api/desktop/credentials/providers/{provider_id}"
        )
        async def desktop_store_provider_credential(
            provider_id: str,
            request: Request,
        ) -> dict[str, object]:
            capability_error = desktop_credential_capability_error(
                launch,
                request.headers,
            )
            if capability_error is not None:
                status_code, detail = capability_error
                raise http_exception(
                    status_code=status_code,
                    detail=detail,
                )
            try:
                provider = desktop_credential_provider(provider_id)
            except ValueError as exc:
                raise http_exception(
                    status_code=400,
                    detail="invalid_desktop_request",
                ) from exc
            content_type = str(
                request.headers.get("content-type", "")
            ).strip().lower()
            if content_type != "application/octet-stream":
                raise http_exception(
                    status_code=415,
                    detail=(
                        "desktop_credential_content_type_required"
                    ),
                )
            raw = await request.body()
            if len(raw) > _MAX_CREDENTIAL_BYTES:
                raise http_exception(
                    status_code=413,
                    detail="desktop_credential_too_large",
                )
            owned = bytearray(raw)
            try:
                try:
                    value = owned.decode(
                        "utf-8",
                        errors="strict",
                    )
                except UnicodeDecodeError as exc:
                    raise http_exception(
                        status_code=400,
                        detail="invalid_desktop_credential",
                    ) from exc
                if (
                    not value
                    or "\x00" in value
                    or "\r" in value
                    or "\n" in value
                    or value != value.strip()
                ):
                    raise http_exception(
                        status_code=400,
                        detail="invalid_desktop_credential",
                    )
                transition = (
                    nullcontext()
                    if sensitive_material_transition is None
                    else sensitive_material_transition()
                )
                try:
                    with transition:
                        stored = secret_broker.store_secret(
                            name=provider["name"],
                            purpose=provider["purpose"],
                            value=value,
                            secret_id=provider["secret_id"],
                            validate=False,
                        )
                except SecretBrokerPartialCommitError as exc:
                    raise http_exception(
                        status_code=409,
                        detail=(
                            "desktop_credential_commit_ambiguous"
                        ),
                    ) from exc
                except ValueError as exc:
                    raise http_exception(
                        status_code=400,
                        detail="invalid_desktop_credential",
                    ) from exc
                except RuntimeError as exc:
                    raise http_exception(
                        status_code=503,
                        detail="desktop_credential_storage_unavailable",
                    ) from exc
                fingerprint = stored.get("fingerprint")
                if (
                    stored.get("id") != provider["secret_id"]
                    or stored.get("name") != provider["name"]
                    or stored.get("purpose") != provider["purpose"]
                    or stored.get("secret_ref")
                    != f"secret://{provider['secret_id']}"
                    or stored.get("configured") is not True
                    or stored.get("validated") is not False
                    or stored.get("source")
                    not in {"broker", "keyring"}
                    or not isinstance(fingerprint, str)
                    or not re.fullmatch(
                        r"sha256:[0-9a-f]{12}",
                        fingerprint,
                    )
                ):
                    raise http_exception(
                        status_code=500,
                        detail="invalid_desktop_response",
                    )
                return {
                    "schema": (
                        "kestrel.desktop.credential-store.v1"
                    ),
                    "provider_id": provider["provider_id"],
                    "id": provider["secret_id"],
                    "name": provider["name"],
                    "purpose": provider["purpose"],
                    "secret_ref": (
                        f"secret://{provider['secret_id']}"
                    ),
                    "configured": True,
                    "validated": False,
                    "fingerprint": fingerprint,
                    "source": "broker",
                }
            finally:
                owned[:] = b"\x00" * len(owned)

    if shutdown_controller is not None:

        @app.post("/api/desktop/shutdown", status_code=202)  # type: ignore[untyped-decorator]
        async def desktop_shutdown() -> dict[str, object]:
            loop = asyncio.get_running_loop()

            def schedule(callback: Callable[[], None]) -> None:
                loop.call_soon(callback)

            if not shutdown_controller.request_after_response(schedule):
                return {
                    "schema": "kestrel.desktop.shutdown.v1",
                    "accepted": False,
                }
            return {
                "schema": "kestrel.desktop.shutdown.v1",
                "accepted": True,
            }
