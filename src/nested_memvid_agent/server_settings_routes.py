from __future__ import annotations

from typing import Any, NoReturn, cast

from .config import AgentConfig
from .effective_settings import (
    apply_setting_change,
    descriptor_for,
    project_settings,
)
from .runtime_settings import (
    RuntimeSettingsConflict,
    RuntimeSettingsStore,
)


def register_settings_routes(
    app: Any,
    *,
    active_config: Any,
    settings_store: RuntimeSettingsStore | None = None,
    capabilities: Any | None = None,
    validate_config_update: Any | None = None,
    on_config_update: Any | None = None,
    on_commit: Any | None = None,
    http_exception: Any | None = None,
) -> None:
    """Register the truthful effective-settings projection routes.

    All values are sourced from the runtime settings store and the live
    config; capability blockers are supplied by the owning capability
    catalog. This module never persists settings itself.
    """

    def config() -> AgentConfig:
        resolved = active_config() if callable(active_config) else active_config
        return cast(AgentConfig, resolved)

    def store() -> RuntimeSettingsStore:
        if settings_store is None:
            _raise(http_exception, 503, "runtime_settings_store_unavailable")
        return settings_store

    def capability_items() -> tuple[dict[str, object], ...]:
        if capabilities is None:
            return ()
        resolved = capabilities() if callable(capabilities) else capabilities
        items: list[dict[str, object]] = []
        for item in resolved or ():
            if isinstance(item, dict):
                items.append(item)
            else:
                items.append(
                    {
                        "key": getattr(item, "key", None),
                        "effective_enabled": getattr(item, "effective_enabled", True),
                    }
                )
        return tuple(items)

    def current_projection() -> Any:
        active = config()
        runtime = store().load(active)
        return project_settings(
            runtime=runtime,
            capabilities=capability_items(),
            config=active,
        )

    @app.get("/api/settings")  # type: ignore[untyped-decorator]
    def list_settings() -> dict[str, object]:
        return cast(dict[str, object], current_projection().to_public_dict())

    @app.put("/api/settings/{setting_id}")  # type: ignore[untyped-decorator]
    def update_setting(setting_id: str, request: dict[str, Any]) -> dict[str, object]:
        expected_revision = request.get("expected_revision")
        if not isinstance(expected_revision, str) or not expected_revision.strip():
            _raise(http_exception, 400, "expected_revision_is_required")
        if "value" not in request:
            _raise(http_exception, 400, "setting_value_is_required")
        try:
            descriptor = descriptor_for(setting_id)
        except KeyError:
            _raise(http_exception, 404, f"unknown_setting: {setting_id}")
        def activate(previous_config: AgentConfig, next_config: AgentConfig) -> None:
            del previous_config
            if on_config_update is not None:
                on_config_update(next_config)

        try:
            update = apply_setting_change(
                store(),
                config(),
                setting_id=setting_id,
                value=request["value"],
                expected_revision=expected_revision.strip(),
                validate_config=validate_config_update,
                activate_config=activate,
                rollback_config=on_config_update,
            )
        except RuntimeSettingsConflict:
            current = current_projection().require(setting_id).to_public_dict()
            raise http_exception(
                status_code=409,
                detail={
                    "error": "setting_revision_conflict",
                    "current": current,
                },
            ) from None
        except (OSError, ValueError) as exc:
            _raise(http_exception, 400, str(exc))

        projection = project_settings(
            runtime=update.settings,
            capabilities=capability_items(),
            config=update.config,
        )
        fresh = projection.require(setting_id).to_public_dict()
        commit_effects = on_commit(update) if on_commit is not None else {}
        revoked = commit_effects.get("revoked_approvals", 0)
        authority_changes = commit_effects.get("authority_changes", [])
        return {
            "schema": "kestrel.effective_settings_mutation.v1",
            "setting": fresh,
            "projection": projection.to_public_dict(),
            "revision": update.settings.revision,
            "store_revision": update.settings.revision,
            "undo_available": bool(fresh["undo_available"]),
            "undo": {
                "available": bool(fresh["undo_available"]),
                "setting_id": setting_id,
                "key": descriptor.key,
            },
            "revoked_approvals": revoked,
            "authority_changes": list(authority_changes),
        }


def _raise(http_exception: Any | None, status_code: int, detail: str) -> NoReturn:
    if http_exception is not None:
        raise http_exception(status_code=status_code, detail=detail)
    raise RuntimeError(detail)
