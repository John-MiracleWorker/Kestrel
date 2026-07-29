from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

from .desktop_bootstrap import DesktopLaunchConfig, DesktopReadiness


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


def register_desktop_routes(
    app: Any,
    *,
    launch: DesktopLaunchConfig,
) -> None:
    readiness: DesktopReadiness = launch.readiness()

    @app.get("/api/desktop/readiness")  # type: ignore[untyped-decorator]
    def desktop_readiness() -> dict[str, object]:
        return readiness.to_public_payload()
