from __future__ import annotations

from contextlib import nullcontext
from typing import Any, cast

from .server_models import SecretStoreRequest


def register_secret_routes(
    app: Any,
    *,
    http_exception: Any,
    secret_broker: Any,
    sensitive_material_transition: Any | None = None,
) -> None:
    @app.get("/api/secrets")  # type: ignore[untyped-decorator]
    def list_secrets() -> list[dict[str, object]]:
        return cast(list[dict[str, object]], secret_broker.list_secrets())

    @app.get("/api/secrets/{secret_id}")  # type: ignore[untyped-decorator]
    def get_secret(secret_id: str) -> dict[str, object]:
        try:
            return cast(dict[str, object], secret_broker.get_secret(secret_id))
        except KeyError as exc:
            raise http_exception(status_code=404, detail="secret_not_found") from exc

    @app.post("/api/secrets")  # type: ignore[untyped-decorator]
    def store_secret(request: SecretStoreRequest) -> dict[str, object]:
        try:
            transition = (
                nullcontext()
                if sensitive_material_transition is None
                else sensitive_material_transition()
            )
            with transition:
                return cast(
                    dict[str, object],
                    secret_broker.store_secret(
                        name=request.name,
                        purpose=request.purpose,
                        value=request.value,
                        secret_id=request.id,
                        validate=request.validate_now,
                    ),
                )
        except (ValueError, RuntimeError) as exc:
            code = getattr(exc, "code", None)
            if isinstance(code, str):
                raise http_exception(
                    status_code=409,
                    detail=code,
                ) from exc
            if isinstance(exc, ValueError):
                raise http_exception(status_code=400, detail=str(exc)) from exc
            raise

    @app.post("/api/secrets/{secret_id}/validate")  # type: ignore[untyped-decorator]
    def validate_secret(secret_id: str) -> dict[str, object]:
        try:
            return cast(dict[str, object], secret_broker.validate_secret(secret_id))
        except KeyError as exc:
            raise http_exception(status_code=404, detail="secret_not_found") from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/secrets/{secret_id}")  # type: ignore[untyped-decorator]
    def delete_secret(secret_id: str) -> dict[str, bool]:
        try:
            secret_broker.delete_secret(secret_id)
            return {"ok": True}
        except KeyError as exc:
            raise http_exception(status_code=404, detail="secret_not_found") from exc
