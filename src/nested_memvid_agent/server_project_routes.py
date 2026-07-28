from __future__ import annotations

import sqlite3
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from .projects import (
    ProjectConflictError,
    export_project,
    import_project_document,
)
from .server_capability_routes import _affected_tool_names, _catalog
from .server_models import (
    ProjectCreateRequest,
    ProjectImportRequest,
    ProjectUpdateRequest,
)


def register_project_routes(
    app: Any,
    *,
    active_config: Any,
    state: Any,
    runs: Any,
    http_exception: Any,
) -> None:
    """Register owner-controlled project profile routes."""

    def config() -> Any:
        return active_config() if callable(active_config) else active_config

    def require_owner_api() -> None:
        if not bool(config().require_api_auth):
            raise http_exception(
                status_code=403,
                detail="project_mutation_requires_api_auth",
            )

    def active_capability_keys() -> frozenset[str]:
        return frozenset(
            str(item["key"])
            for item in _catalog(state=state, runs=runs)
            if bool(item["effective_enabled"])
        )

    def project_or_404(project_id: str) -> Any:
        try:
            return state.get_project(project_id)
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    def mutation(operation: Any) -> dict[str, Any]:
        try:
            return asdict(operation())
        except ProjectConflictError as exc:
            raise http_exception(
                status_code=409,
                detail={
                    "error": "project_revision_conflict",
                    "current": asdict(exc.current),
                },
            ) from exc
        except sqlite3.IntegrityError as exc:
            raise http_exception(
                status_code=409,
                detail="project_id_or_repository_path_already_exists",
            ) from exc
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except (PermissionError, ValueError) as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.get("/api/projects")  # type: ignore[untyped-decorator]
    def list_projects(include_archived: bool = False) -> dict[str, object]:
        items = [
            asdict(item)
            for item in state.list_projects(include_archived=include_archived)
        ]
        return {"items": items, "count": len(items)}

    @app.post("/api/projects", status_code=201)  # type: ignore[untyped-decorator]
    def create_project(request: ProjectCreateRequest) -> dict[str, Any]:
        require_owner_api()
        payload = request.model_dump()
        payload["project_id"] = request.project_id or f"project_{uuid4().hex}"
        payload["repository_path"] = Path(request.repository_path)
        payload["allowed_paths"] = tuple(request.allowed_paths)
        payload["test_recipes"] = tuple(
            item.model_dump(exclude_none=True) for item in request.test_recipes
        )
        payload["build_recipes"] = tuple(
            item.model_dump(exclude_none=True) for item in request.build_recipes
        )
        payload["capability_ceiling"] = (
            None
            if request.capability_ceiling is None
            else tuple(request.capability_ceiling)
        )
        return mutation(
            lambda: state.create_project(
                **payload,
                active_capability_keys=active_capability_keys(),
            )
        )

    @app.post("/api/projects/import", status_code=201)  # type: ignore[untyped-decorator]
    def import_project(request: ProjectImportRequest) -> dict[str, Any]:
        require_owner_api()
        active = active_capability_keys()

        def operation() -> Any:
            fields = import_project_document(
                request.document,
                active_capability_keys=active,
            )
            fields["repository_path"] = Path(str(fields["repository_path"]))
            fields["allowed_paths"] = tuple(fields["allowed_paths"])
            fields["test_recipes"] = tuple(fields["test_recipes"])
            fields["build_recipes"] = tuple(fields["build_recipes"])
            fields["capability_ceiling"] = tuple(fields["capability_ceiling"])
            return state.create_project(
                **fields,
                active_capability_keys=active,
            )

        return mutation(operation)

    @app.get("/api/projects/{project_id}/export")  # type: ignore[untyped-decorator]
    def export_project_route(project_id: str) -> dict[str, object]:
        return export_project(project_or_404(project_id))

    @app.get("/api/projects/{project_id}")  # type: ignore[untyped-decorator]
    def get_project(project_id: str) -> dict[str, Any]:
        return asdict(project_or_404(project_id))

    @app.put("/api/projects/{project_id}")  # type: ignore[untyped-decorator]
    def update_project(
        project_id: str,
        request: ProjectUpdateRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        before = project_or_404(project_id)
        fields = request.model_dump(exclude_unset=True)
        expected_revision = int(fields.pop("expected_revision"))
        for recipe_field in ("test_recipes", "build_recipes"):
            recipes = fields.get(recipe_field)
            if recipes is not None:
                fields[recipe_field] = tuple(
                    recipe.model_dump(exclude_none=True)
                    for recipe in getattr(request, recipe_field) or ()
                )
        if fields.get("allowed_paths") is not None:
            fields["allowed_paths"] = tuple(fields["allowed_paths"])
        if fields.get("capability_ceiling") is not None:
            fields["capability_ceiling"] = tuple(fields["capability_ceiling"])
        if fields.get("repository_path") is not None:
            fields["repository_path"] = Path(str(fields["repository_path"]))
        updated = mutation(
            lambda: state.update_project(
                project_id,
                expected_revision=expected_revision,
                active_capability_keys=active_capability_keys(),
                **fields,
            )
        )
        removed = set(before.capability_ceiling) - set(
            str(item) for item in updated["capability_ceiling"]
        )
        affected_tools: set[str] = set()
        for key in removed:
            kind, _, capability_id = key.partition(":")
            affected_tools.update(_affected_tool_names(runs, kind, capability_id))
        project_run_ids = {
            run.run_id
            for run in state.list_runs()
            if run.project_id == project_id
        }
        updated["revoked_approvals"] = (
            runs.revoke_pending_approvals_for_tools(
                affected_tools,
                reason="project_capability_ceiling_narrowed",
                run_ids=project_run_ids,
            )
            if affected_tools
            else 0
        )
        return updated

    @app.delete("/api/projects/{project_id}")  # type: ignore[untyped-decorator]
    def archive_project(
        project_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        require_owner_api()
        return mutation(
            lambda: state.archive_project(
                project_id,
                expected_revision=expected_revision,
            )
        )
