from __future__ import annotations

import sqlite3
import stat
from dataclasses import asdict
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from .project_setup import build_project_setup_draft
from .projects import (
    ProjectConflictError,
    export_project,
    import_project_document,
)
from .repo_index import RepositoryIndex, RepositoryIndexError
from .security_boundary import redact_text
from .server_capability_routes import _affected_tool_names, _catalog
from .server_models import (
    ProjectCreateRequest,
    ProjectImportRequest,
    ProjectIndexRebuildRequest,
    ProjectSetupDraftRequest,
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

    index_rebuild_lock = Lock()

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

    @app.post("/api/projects/setup-draft")  # type: ignore[untyped-decorator]
    def inspect_project_setup_draft(
        request: ProjectSetupDraftRequest,
    ) -> dict[str, Any]:
        active = config()
        try:
            return build_project_setup_draft(
                repository_path=request.repository_path,
                provider=str(active.provider),
                model=str(active.model),
                base_url=active.base_url,
                capability_catalog=_catalog(state=state, runs=runs),
                direct_estimated_cost_usd=request.direct_estimated_cost_usd,
                cost_budget=request.cost_budget,
            )
        except (PermissionError, ValueError) as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

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

    @app.get("/api/projects/{project_id}/index")  # type: ignore[untyped-decorator]
    def get_project_index(project_id: str) -> dict[str, Any]:
        project = project_or_404(project_id)
        sidecar = (
            Path(project.repository_path)
            / ".nest"
            / "repo-index"
            / f"{project.project_id}.sqlite"
        )
        try:
            sidecar_metadata = sidecar.lstat()
        except FileNotFoundError:
            return {
                "schema": "kestrel.project_index_status.v1",
                "project_id": project.project_id,
                "project_revision": project.revision,
                "status": "missing",
                "freshness": "missing",
                "aggregate_digest": project.baseline_index_digest,
                "indexed_at": None,
                "detail": "No repository index exists for this project.",
            }
        except OSError as exc:
            return {
                "schema": "kestrel.project_index_status.v1",
                "project_id": project.project_id,
                "project_revision": project.revision,
                "status": "rebuild_required",
                "freshness": "unknown",
                "aggregate_digest": project.baseline_index_digest,
                "indexed_at": None,
                "detail": redact_text(str(exc)),
            }
        if stat.S_ISLNK(sidecar_metadata.st_mode) or not stat.S_ISREG(
            sidecar_metadata.st_mode
        ):
            return {
                "schema": "kestrel.project_index_status.v1",
                "project_id": project.project_id,
                "project_revision": project.revision,
                "status": "rebuild_required",
                "freshness": "unknown",
                "aggregate_digest": project.baseline_index_digest,
                "indexed_at": None,
                "detail": (
                    "Repository-index sidecar is not a trusted regular file "
                    f"({sidecar_metadata.st_size} bytes)."
                ),
            }
        try:
            status = RepositoryIndex(
                project_id=project.project_id,
                repository_root=Path(project.repository_path),
                create=False,
            ).status()
        except (OSError, RepositoryIndexError, sqlite3.Error, ValueError) as exc:
            return {
                "schema": "kestrel.project_index_status.v1",
                "project_id": project.project_id,
                "project_revision": project.revision,
                "status": "rebuild_required",
                "freshness": "unknown",
                "aggregate_digest": project.baseline_index_digest,
                "indexed_at": None,
                "detail": redact_text(str(exc)),
            }
        if (
            project.baseline_index_digest is None
            or project.baseline_index_digest != status.aggregate_digest
        ):
            return {
                "schema": "kestrel.project_index_status.v1",
                "project_id": project.project_id,
                "project_revision": project.revision,
                "status": "rebuild_required",
                "freshness": "unknown",
                "aggregate_digest": status.aggregate_digest,
                "indexed_at": status.indexed_at,
                "indexed_files": None,
                "git_head": status.git_head,
                "git_tree": status.git_tree,
                "detail": (
                    "The valid index generation is not bound to the current project "
                    "revision; rebuild it explicitly."
                ),
            }
        return {
            "schema": "kestrel.project_index_status.v1",
            "project_id": project.project_id,
            "project_revision": project.revision,
            "status": "ready",
            "freshness": status.freshness.value,
            "aggregate_digest": status.aggregate_digest,
            "indexed_at": status.indexed_at,
            "indexed_files": None,
            "git_head": status.git_head,
            "git_tree": status.git_tree,
            "detail": (
                "Repository index matches the current project snapshot."
                if status.freshness.value == "current"
                else "Repository contents changed after the last index build."
            ),
        }

    @app.post("/api/projects/{project_id}/index/rebuild")  # type: ignore[untyped-decorator]
    def rebuild_project_index(
        project_id: str,
        request: ProjectIndexRebuildRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        project = project_or_404(project_id)
        if project.archived_at is not None:
            raise http_exception(
                status_code=409,
                detail="cannot_rebuild_index_for_archived_project",
            )
        if project.revision != request.expected_project_revision:
            raise http_exception(
                status_code=409,
                detail={
                    "error": "project_revision_conflict",
                    "current": asdict(project),
                },
            )
        if not index_rebuild_lock.acquire(blocking=False):
            raise http_exception(
                status_code=409,
                detail="repository_index_rebuild_in_progress",
            )
        try:
            report = RepositoryIndex(
                project_id=project.project_id,
                repository_root=Path(project.repository_path),
            ).rebuild()
            updated = state.update_project(
                project.project_id,
                expected_revision=request.expected_project_revision,
                active_capability_keys=active_capability_keys(),
                baseline_index_digest=report.aggregate_digest,
            )
        except ProjectConflictError as exc:
            raise http_exception(
                status_code=409,
                detail={
                    "error": "project_revision_conflict_after_index_build",
                    "current": asdict(exc.current),
                },
            ) from exc
        except (OSError, RepositoryIndexError, sqlite3.Error, ValueError) as exc:
            raise http_exception(
                status_code=409,
                detail={
                    "error": "repository_index_rebuild_failed",
                    "message": redact_text(str(exc)),
                },
            ) from exc
        finally:
            index_rebuild_lock.release()
        return {
            "schema": "kestrel.project_index_rebuild.v1",
            "project": asdict(updated),
            "report": asdict(report),
        }

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
