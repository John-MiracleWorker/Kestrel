from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends

from .config import AgentConfig
from .product_readiness import build_product_readiness_report
from .setup_readiness import build_setup_readiness_report


def register_product_routes(
    app: object,
    auth_dependency: Callable[..., Any] | None = None,
    active_config: Callable[[], AgentConfig] | None = None,
) -> None:
    router = APIRouter()
    dependencies = [Depends(auth_dependency)] if auth_dependency is not None else []

    @router.get("/api/product/readiness", dependencies=dependencies)  # type: ignore[misc]
    def product_readiness() -> dict[str, object]:
        return build_product_readiness_report().to_dict()

    @router.get("/api/product/setup", dependencies=dependencies)  # type: ignore[misc]
    def product_setup() -> dict[str, object]:
        config = active_config() if active_config is not None else AgentConfig.from_env()
        return build_setup_readiness_report(config).to_dict()

    app.include_router(router)  # type: ignore[attr-defined]
