from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import Any
from uuid import uuid4

from .routine_limits import MAX_ROUTINE_HISTORY_LIMIT
from .server_models import (
    RoutineCreateRequest,
    RoutineRunNowRequest,
    RoutineToggleRequest,
    RoutineUpdateRequest,
)
from .state_store import RoutineConflictError, RoutineRunNowConflictError


def register_routine_routes(
    app: Any,
    *,
    active_config: Any,
    state: Any,
    service: Any,
    loop: Any | None,
    http_exception: Any,
) -> None:
    """Register owner-controlled durable routine routes.

    Read routes inherit the normal API ingress policy. Mutation and dispatch
    routes additionally require launch-controlled API authentication so an
    unauthenticated loopback web page cannot author persistent machine turns.
    """

    def config() -> Any:
        return active_config() if callable(active_config) else active_config

    def require_owner_api() -> None:
        if not bool(config().require_api_auth):
            raise http_exception(
                status_code=403,
                detail="routine_mutation_requires_api_auth",
            )

    def require_dispatch_enabled() -> None:
        require_owner_api()
        if not bool(config().enable_proactive_routines):
            raise http_exception(
                status_code=403,
                detail="proactive_routines_disabled",
            )

    def routine_or_404(routine_id: str) -> Any:
        try:
            return state.get_routine(routine_id)
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc

    def mutate(operation: Any) -> dict[str, Any]:
        try:
            return asdict(operation())
        except RoutineConflictError as exc:
            raise http_exception(
                status_code=409,
                detail={
                    "error": "routine_revision_conflict",
                    "current": asdict(exc.current),
                },
            ) from exc
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise http_exception(status_code=409, detail="routine_id_already_exists") from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.get("/api/routines/status")  # type: ignore[untyped-decorator]
    def routine_status() -> dict[str, Any]:
        return {
            "enabled": bool(config().enable_proactive_routines),
            "loop": loop.status().to_dict() if loop is not None else None,
        }

    @app.post("/api/routines/actions/tick")  # type: ignore[untyped-decorator]
    def tick_routines() -> dict[str, Any]:
        require_dispatch_enabled()
        result = service.tick()
        return _tick_payload(result)

    @app.get("/api/routines")  # type: ignore[untyped-decorator]
    def list_routines() -> list[dict[str, Any]]:
        return [asdict(item) for item in state.list_routines()]

    @app.post("/api/routines")  # type: ignore[untyped-decorator]
    def create_routine(request: RoutineCreateRequest) -> dict[str, Any]:
        require_owner_api()
        routine_id = request.routine_id or f"routine_{uuid4().hex}"
        return mutate(
            lambda: state.create_routine(
                routine_id=routine_id,
                name=request.name,
                prompt=request.prompt,
                schedule_kind=request.schedule_kind,
                start_at=request.start_at,
                interval_seconds=request.interval_seconds,
                enabled=False,
                workspace=request.workspace,
                provider=request.provider,
                model=request.model,
                autonomy_mode=request.autonomy_mode,
                misfire_grace_seconds=request.misfire_grace_seconds,
            )
        )

    @app.post("/api/routines/{routine_id}/actions/run-now")  # type: ignore[untyped-decorator]
    def run_routine_now(
        routine_id: str,
        request: RoutineRunNowRequest,
    ) -> dict[str, Any]:
        require_dispatch_enabled()
        try:
            return _tick_payload(
                service.run_now(
                    routine_id,
                    expected_revision=request.expected_revision,
                    idempotency_key=request.idempotency_key,
                )
            )
        except RoutineConflictError as exc:
            raise http_exception(
                status_code=409,
                detail={
                    "error": "routine_revision_conflict",
                    "current": asdict(exc.current),
                },
            ) from exc
        except RoutineRunNowConflictError as exc:
            detail: dict[str, Any] = {"error": exc.code}
            if exc.current is not None:
                detail["current"] = asdict(exc.current)
            if exc.occurrence is not None:
                detail["occurrence"] = asdict(exc.occurrence)
            raise http_exception(status_code=409, detail=detail) from exc
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.get("/api/routines/{routine_id}")  # type: ignore[untyped-decorator]
    def get_routine(routine_id: str) -> dict[str, Any]:
        return asdict(routine_or_404(routine_id))

    @app.put("/api/routines/{routine_id}")  # type: ignore[untyped-decorator]
    def update_routine(
        routine_id: str,
        request: RoutineUpdateRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        fields = request.model_dump(exclude_unset=True)
        expected_revision = int(fields.pop("expected_revision"))
        return mutate(
            lambda: state.update_routine(
                routine_id,
                expected_revision=expected_revision,
                **fields,
            )
        )

    @app.put("/api/routines/{routine_id}/enabled")  # type: ignore[untyped-decorator]
    def set_routine_enabled(
        routine_id: str,
        request: RoutineToggleRequest,
    ) -> dict[str, Any]:
        require_owner_api()
        return mutate(
            lambda: state.update_routine(
                routine_id,
                expected_revision=request.expected_revision,
                enabled=request.enabled,
            )
        )

    @app.delete("/api/routines/{routine_id}")  # type: ignore[untyped-decorator]
    def delete_routine(routine_id: str, expected_revision: int) -> dict[str, Any]:
        require_owner_api()
        return mutate(
            lambda: state.delete_routine(
                routine_id,
                expected_revision=expected_revision,
            )
        )

    @app.get("/api/routines/{routine_id}/history")  # type: ignore[untyped-decorator]
    def routine_history(routine_id: str, limit: int = 100) -> list[dict[str, Any]]:
        routine_or_404(routine_id)
        try:
            bounded_limit = max(1, min(int(limit), MAX_ROUTINE_HISTORY_LIMIT))
            occurrences = state.list_routine_occurrences(
                routine_id,
                limit=bounded_limit,
            )
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc
        return [asdict(item) for item in occurrences]


def _tick_payload(result: Any) -> dict[str, Any]:
    if hasattr(result, "to_payload"):
        payload = result.to_payload()
    elif hasattr(result, "to_dict"):
        payload = result.to_dict()
    elif hasattr(result, "__dataclass_fields__"):
        payload = asdict(result)
    else:
        payload = dict(result)
    return payload if isinstance(payload, dict) else {"result": payload}
