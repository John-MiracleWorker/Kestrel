from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request

from .desktop_recovery import DesktopRecoveryService

_MAX_RETRY_REQUEST_BYTES = 1_024
_RETRY_SCHEMA = "kestrel.desktop.recovery-retry.v1"
_RETRY_RESULT_SCHEMA = (
    "kestrel.desktop.recovery-retry-result.v1"
)


def register_desktop_recovery_routes(
    app: Any,
    *,
    service: DesktopRecoveryService,
    http_exception: Any,
) -> None:
    @app.get("/api/desktop/recovery")  # type: ignore[untyped-decorator]
    def desktop_recovery() -> dict[str, object]:
        return service.inspect().to_public_payload()

    @app.get(  # type: ignore[untyped-decorator]
        "/api/desktop/recovery/support-bundle-preview"
    )
    def desktop_recovery_support_preview() -> dict[str, object]:
        return service.support_bundle_preview()

    @app.post("/api/desktop/recovery/retry")  # type: ignore[untyped-decorator]
    async def desktop_recovery_retry(
        request: Request,
    ) -> dict[str, object]:
        raw = await request.body()
        if len(raw) > _MAX_RETRY_REQUEST_BYTES:
            raise http_exception(
                status_code=413,
                detail="desktop_recovery_request_too_large",
            )
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeError):
            payload = None
        if (
            not isinstance(payload, dict)
            or set(payload) != {"schema", "action"}
            or payload.get("schema") != _RETRY_SCHEMA
            or payload.get("action") != "retry_readiness"
        ):
            raise http_exception(
                status_code=400,
                detail="invalid_desktop_recovery_request",
            )
        report = service.retry_readiness()
        return {
            "schema": _RETRY_RESULT_SCHEMA,
            "accepted": report.can_auto_resume,
            "report": report.to_public_payload(),
        }
