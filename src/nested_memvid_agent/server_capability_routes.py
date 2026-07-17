from __future__ import annotations

from typing import Any, cast

from .capability_policy import parent_resource_digest, tool_spec_digest
from .server_models import CapabilityToggleRequest
from .state_store import CapabilityConflictError


def register_capability_routes(
    app: Any,
    *,
    http_exception: Any,
    state: Any,
    runs: Any,
    mcp: Any,
    skills: Any,
) -> None:
    """Expose one server-authoritative catalog for every executable capability."""

    @app.get("/api/capabilities")  # type: ignore[untyped-decorator]
    def list_capabilities() -> dict[str, object]:
        items = _catalog(state=state, runs=runs)
        return {
            "items": items,
            "counts": {
                "total": len(items),
                "configured_enabled": sum(
                    1 for item in items if bool(item["configured_enabled"])
                ),
                "effective_enabled": sum(
                    1 for item in items if bool(item["effective_enabled"])
                ),
                "blocked": sum(1 for item in items if item["blocked_by"]),
            },
        }

    @app.get("/api/capabilities/history")  # type: ignore[untyped-decorator]
    def capability_history(
        kind: str | None = None,
        capability_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        try:
            return cast(
                list[dict[str, object]],
                state.list_capability_changes(
                    kind=kind,
                    capability_id=capability_id,
                    limit=max(1, min(limit, 500)),
                ),
            )
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc

    @app.put("/api/capabilities/{kind}/{capability_id}")  # type: ignore[untyped-decorator]
    def set_capability(
        kind: str,
        capability_id: str,
        request: CapabilityToggleRequest,
    ) -> dict[str, object]:
        try:
            if kind == "tool":
                registry = runs.build_registry()
                canonical = registry.canonical_name(capability_id)
                if canonical is None:
                    raise KeyError(f"Unknown tool capability: {capability_id}")
                capability_id = canonical
            before = _find_capability(
                _catalog(state=state, runs=runs), kind=kind, capability_id=capability_id
            )
            canonical_id = str(before["id"])
            digest = _resource_digest(state, runs, kind, canonical_id)
            state.set_capability_override(
                kind,
                canonical_id,
                request.enabled,
                expected_revision=request.expected_revision,
                default_enabled=bool(before["default_enabled"]),
                resource_digest=digest,
                updated_by="owner",
            )

            affected_tools = _affected_tool_names(runs, kind, canonical_id)
            if kind == "mcp_server":
                mcp.set_enabled(canonical_id, request.enabled)
            elif kind == "skill":
                skills.set_enabled(canonical_id, request.enabled)
            if not request.enabled:
                revoked = runs.revoke_pending_approvals_for_tools(affected_tools)
            else:
                revoked = 0

            capability = _find_capability(
                _catalog(state=state, runs=runs), kind=kind, capability_id=canonical_id
            )
            return {
                "capability": capability,
                "revoked_approvals": revoked,
                "applies_to": "future_invocations",
            }
        except CapabilityConflictError as exc:
            raise http_exception(
                status_code=409,
                detail={"error": "capability_revision_conflict", "current": exc.current},
            ) from exc
        except KeyError as exc:
            raise http_exception(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise http_exception(status_code=400, detail=str(exc)) from exc


def _catalog(*, state: Any, runs: Any) -> list[dict[str, object]]:
    registry = runs.build_registry()
    specs = getattr(registry, "all_specs", registry.specs)()
    items: list[dict[str, object]] = []
    for spec in specs:
        decision = runs.capabilities.tool_decision(spec)
        parent_key = None
        if spec.source == "mcp" and spec.server_id:
            parent_key = f"mcp_server:{spec.server_id}"
        elif spec.source == "skill" and spec.skill_id:
            parent_key = f"skill:{spec.skill_id}"
        items.append(
            {
                "key": f"tool:{spec.name}",
                "kind": "tool",
                "id": spec.name,
                "name": spec.name,
                "description": spec.description,
                **decision.to_public_dict(),
                "risk": spec.risk,
                "requires_approval": spec.requires_approval,
                "source": spec.source,
                "parent_key": parent_key,
                "status": "available" if decision.effective_enabled else "blocked",
            }
        )

    for server in state.list_mcp_servers():
        server_id = str(server["id"])
        decision = runs.capabilities.parent_decision(
            "mcp_server", server_id, entity_enabled=bool(server.get("enabled", False))
        )
        vetting = dict(server.get("vetting", {}) or {})
        risk = "high" if "high_risk_tools" in vetting.get("risk_reasons", []) else "medium"
        items.append(
            {
                "key": f"mcp_server:{server_id}",
                "kind": "mcp_server",
                "id": server_id,
                "name": str(server.get("name") or server_id),
                "description": f"{server.get('transport', 'stdio')} MCP server",
                **decision.to_public_dict(),
                "risk": risk,
                "requires_approval": bool(vetting.get("connect_requires_approval", False)),
                "source": "mcp",
                "parent_key": None,
                "status": str(server.get("status") or "configured"),
            }
        )

    for skill in state.list_skills():
        skill_id = str(skill["id"])
        decision = runs.capabilities.parent_decision(
            "skill", skill_id, entity_enabled=bool(skill.get("enabled", False))
        )
        manifest = dict(skill.get("manifest", {}) or {})
        risk = str(manifest.get("risk", "medium"))
        runtime = manifest.get("runtime", {})
        executable = isinstance(runtime, dict) and runtime.get("type") in {
            "python",
            "shell",
            "container",
        }
        items.append(
            {
                "key": f"skill:{skill_id}",
                "kind": "skill",
                "id": skill_id,
                "name": str(skill.get("name") or skill_id),
                "description": str(skill.get("description") or "Skill capsule"),
                **decision.to_public_dict(),
                "risk": "high" if executable else risk,
                "requires_approval": executable
                or bool(manifest.get("requires_approval", risk in {"medium", "high"})),
                "source": "skill",
                "parent_key": None,
                "status": "available" if decision.effective_enabled else "blocked",
            }
        )
    return sorted(items, key=lambda item: (str(item["kind"]), str(item["name"]).lower()))


def _find_capability(
    items: list[dict[str, object]], *, kind: str, capability_id: str
) -> dict[str, object]:
    if kind not in {"tool", "mcp_server", "skill"}:
        raise ValueError(f"Unsupported capability kind: {kind}")
    for item in items:
        if item["kind"] != kind:
            continue
        if item["id"] == capability_id:
            return item
    raise KeyError(f"Unknown {kind} capability: {capability_id}")


def _affected_tool_names(runs: Any, kind: str, capability_id: str) -> set[str]:
    registry = runs.build_registry()
    specs = getattr(registry, "all_specs", registry.specs)()
    if kind == "tool":
        canonical = registry.canonical_name(capability_id)
        if canonical is None:
            raise KeyError(f"Unknown tool capability: {capability_id}")
        spec = registry.spec_for(canonical)
        return {canonical, *(spec.aliases if spec is not None else ())}
    if kind == "mcp_server":
        return {spec.name for spec in specs if spec.server_id == capability_id}
    if kind == "skill":
        return {spec.name for spec in specs if spec.skill_id == capability_id}
    raise ValueError(f"Unsupported capability kind: {kind}")


def _resource_digest(state: Any, runs: Any, kind: str, capability_id: str) -> str:
    if kind == "tool":
        registry = runs.build_registry()
        canonical = registry.canonical_name(capability_id)
        spec = registry.spec_for(canonical or capability_id)
        if spec is None:
            raise KeyError(f"Unknown tool capability: {capability_id}")
        return tool_spec_digest(spec)
    if kind == "mcp_server":
        return parent_resource_digest(state, kind, capability_id)
    if kind == "skill":
        return parent_resource_digest(state, kind, capability_id)
    raise ValueError(f"Unsupported capability kind: {kind}")
