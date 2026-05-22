from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends

from .product_readiness import build_product_readiness_report


def register_product_routes(app: object, auth_dependency: Callable[..., Any] | None = None) -> None:
    router = APIRouter()
    dependencies = [Depends(auth_dependency)] if auth_dependency is not None else []

    @router.get("/api/product/readiness", dependencies=dependencies)  # type: ignore[misc]
    def product_readiness() -> dict[str, object]:
        return build_product_readiness_report().to_dict()

    app.include_router(router)  # type: ignore[attr-defined]
