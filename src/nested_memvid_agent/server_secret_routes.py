from __future__ import annotations

from typing import Any, cast

from .server_models import SecretStoreRequest


def register_secret_routes(
    app: Any,
    *,
    http_exception: Any,
    secret_broker: Any,
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
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

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
