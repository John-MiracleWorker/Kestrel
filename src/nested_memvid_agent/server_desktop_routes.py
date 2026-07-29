from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

from .desktop_bootstrap import DesktopLaunchConfig, DesktopReadiness


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


def register_desktop_routes(
    app: Any,
    *,
    launch: DesktopLaunchConfig,
    shutdown_controller: DesktopShutdownController | None = None,
) -> None:
    readiness: DesktopReadiness = launch.readiness()

    @app.get("/api/desktop/readiness")  # type: ignore[untyped-decorator]
    def desktop_readiness() -> dict[str, object]:
        return readiness.to_public_payload()

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
