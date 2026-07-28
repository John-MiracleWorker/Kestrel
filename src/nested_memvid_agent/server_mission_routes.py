from __future__ import annotations

from typing import Any

from .mission_control import (
    build_mission_preflight,
    inspect_git_worktree,
    inspect_index_without_mutation,
    inspect_provider_readiness,
)
from .server_capability_routes import _catalog
from .server_models import MissionPreflightRequest


def register_mission_routes(
    app: Any,
    *,
    active_config: Any,
    state: Any,
    runs: Any,
    routing_ledger: Any,
    routing_config: Any,
    http_exception: Any,
) -> None:
    """Register the read-only task-first Mission Control preflight."""

    def config() -> Any:
        return active_config() if callable(active_config) else active_config

    @app.post(  # type: ignore[untyped-decorator]
        "/api/projects/{project_id}/mission/preflight"
    )
    def mission_preflight(
        project_id: str,
        request: MissionPreflightRequest,
    ) -> dict[str, Any]:
        try:
            project = state.get_project(project_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        if project.archived_at is not None:
            raise http_exception(status_code=409, detail="project_is_archived")
        try:
            return build_mission_preflight(
                project=project,
                objective=request.objective,
                template_id=request.template_id,
                git=inspect_git_worktree(
                    project.repository_path,
                    timeout_seconds=3.0,
                ),
                index=inspect_index_without_mutation(project),
                provider=inspect_provider_readiness(
                    project=project,
                    config=config(),
                    routing_config=routing_config,
                    provider_profiles=routing_ledger.list_provider_profiles(
                        enabled_only=False
                    ),
                    model_targets=routing_ledger.list_model_targets(
                        enabled_only=False
                    ),
                    template_id=request.template_id,
                ),
                capability_catalog=_catalog(state=state, runs=runs),
            )
        except (PermissionError, ValueError) as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
