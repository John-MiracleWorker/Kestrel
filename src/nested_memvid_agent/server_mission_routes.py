from __future__ import annotations

from pathlib import Path
from typing import Any

from .mission_control import (
    build_mission_launch_binding,
    build_mission_preflight,
    build_mission_preflight_digest,
    inspect_git_worktree,
    inspect_index_without_mutation,
    inspect_provider_readiness,
    validated_mission_plan,
)
from .mission_proof import build_mission_proof
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
            return evaluate_mission_preflight(
                project=project,
                objective=request.objective,
                template_id=request.template_id,
                config=config(),
                state=state,
                runs=runs,
                routing_ledger=routing_ledger,
                routing_config=routing_config,
                mission_plan=(
                    None
                    if request.mission_plan is None
                    else [task.model_dump() for task in request.mission_plan]
                ),
            )
        except (PermissionError, ValueError) as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/mission-proof")  # type: ignore[untyped-decorator]
    def mission_proof(run_id: str) -> dict[str, Any]:
        """Read-only server-authored mission proof projection (JOURNEY-002).

        Aggregates contract, roles, routing, isolation, change, validation,
        review, risks, approval, shipping, capsule, and learning evidence for
        one admitted run, reporting each as present/missing/stale/mismatched
        without UI inference. The projection is read-only and exposes only
        bounded receipt/handle metadata.
        """
        try:
            run = state.get_run(run_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        runs_dir = Path(config().memory_dir).parent / "runs"
        return build_mission_proof(
            state=state,
            run=run,
            routing_ledger=routing_ledger,
            runs_dir=runs_dir,
        )


def evaluate_mission_preflight(
    *,
    project: Any,
    objective: str,
    template_id: str,
    config: Any,
    state: Any,
    runs: Any,
    routing_ledger: Any,
    routing_config: Any,
    mission_plan: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the same live preflight projection for inspection and admission."""

    provider_profiles = routing_ledger.list_provider_profiles(enabled_only=False)
    model_targets = routing_ledger.list_model_targets(enabled_only=False)
    route_policies = routing_ledger.list_policies(enabled_only=False)
    git = inspect_git_worktree(
        project.repository_path,
        timeout_seconds=3.0,
    )
    index = inspect_index_without_mutation(project)
    provider = inspect_provider_readiness(
        project=project,
        config=config,
        routing_config=routing_config,
        provider_profiles=provider_profiles,
        model_targets=model_targets,
        route_policies=route_policies,
        template_id=template_id,
    )
    capability_catalog = _catalog(state=state, runs=runs)
    tasks = validated_mission_plan(template_id, mission_plan)
    launch_binding = build_mission_launch_binding(
        project=project,
        objective=objective,
        template_id=template_id,
        config=config,
        routing_config=routing_config,
        provider_profiles=provider_profiles,
        model_targets=model_targets,
        route_policies=route_policies,
        preflight_digest=build_mission_preflight_digest(
            git=git,
            index=index,
            provider=provider,
            capability_catalog=capability_catalog,
        ),
        mission_plan=tasks,
    )
    return build_mission_preflight(
        project=project,
        objective=objective,
        template_id=template_id,
        git=git,
        index=index,
        provider=provider,
        capability_catalog=capability_catalog,
        mission_plan=tasks,
        launch_binding=launch_binding,
    )
