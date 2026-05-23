from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends

from .config import AgentConfig
from .product_readiness import build_product_readiness_report
from .provider_certification import build_provider_certification_report
from .setup_readiness import build_setup_readiness_report
from .support_bundle import export_support_bundle


def register_product_routes(
    app: object,
    auth_dependency: Callable[..., Any] | None = None,
    active_config: Callable[[], AgentConfig] | None = None,
) -> None:
    router = APIRouter()
    dependencies = [Depends(auth_dependency)] if auth_dependency is not None else []

    @router.get("/api/product/readiness", dependencies=dependencies)
    def product_readiness() -> dict[str, object]:
        return build_product_readiness_report().to_dict()

    @router.get("/api/product/setup", dependencies=dependencies)
    def product_setup() -> dict[str, object]:
        config = active_config() if active_config is not None else AgentConfig.from_env()
        return build_setup_readiness_report(config).to_dict()

    @router.get("/api/product/provider-certification", dependencies=dependencies)
    def product_provider_certification() -> dict[str, object]:
        config = active_config() if active_config is not None else AgentConfig.from_env()
        return build_provider_certification_report(config).to_dict()

    @router.post("/api/product/support-bundle", dependencies=dependencies)
    def product_support_bundle(log_tail: int = 100) -> dict[str, object]:
        config = active_config() if active_config is not None else AgentConfig.from_env()
        return export_support_bundle(config, log_tail=log_tail).to_dict()

    app.include_router(router)  # type: ignore[attr-defined]
