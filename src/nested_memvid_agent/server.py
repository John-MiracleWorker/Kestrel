import json
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Any

from .app_factory import build_agent
from .channels import ChannelManager
from .config import AgentConfig
from .event_bus import RunEventBus
from .mcp_manager import MCPManager
from .models import MemoryLayer, RetrievalQuery
from .orchestrator import build_memory_system
from .plugin_manager import PluginError, PluginManager
from .run_manager import RunManager
from .runtime_settings import (
    RuntimeSettingsStore,
    apply_runtime_settings,
    default_runtime_settings_path,
)
from .secret_broker import build_secret_broker
from .self_profile import (
    SELF_PROFILE_QUERY,
    SELF_PROFILE_SCHEMA,
    build_onboarding_profile,
    onboarding_record_content,
    onboarding_record_title,
    onboarding_state_from_reflection,
    persona_presets_public,
)
from .server_support import (
    api_auth_error as _api_auth_error,
)
from .server_support import (
    bounded_limit as _bounded_limit,
)
from .server_support import (
    csv_layers as _csv_layers,
)
from .server_support import (
    execution_response as _execution_response,
)
from .server_support import (
    hostname_from_header as _hostname_from_header,
)
from .server_support import (
    host_is_trusted as _host_is_trusted,
)
from .server_support import (
    hostname_from_url as _hostname_from_url,
)
from .server_support import (
    known_secret_env_names as _known_secret_env_names,
)
from .server_support import (
    request_headers,
)
from .server_support import (
    tool_response_payload as _tool_response_payload,
)
from .skill_manager import SkillManager
from .state_store import AgentStateStore


def create_app(config: AgentConfig | None = None) -> Any:
    """Create the local web/API app for the full Nested MV2 Agent."""

    try:
        fastapi_module = import_module("fastapi")
        responses_module = import_module("starlette.responses")
        staticfiles_module = import_module("starlette.staticfiles")
        cors_module = import_module("starlette.middleware.cors")
        from .server_channel_routes import register_channel_routes
        from .server_diagnosis_routes import register_diagnosis_routes
        from .server_mcp_routes import register_mcp_routes
        from .server_models import (
            ApprovalDecisionRequest,
            CapsuleApplyAPIRequest,
            CapsuleSummarizeAPIRequest,
            ContextExpandAPIRequest,
            ContextPackAPIRequest,
            CreateRunRequest,
            MemoryCompactRequest,
            MemoryConsolidateRequest,
            MemoryCorrectRequest,
            MemoryInspectAPIRequest,
            MemoryLearnRequest,
            MemorySearchRequest,
            PluginInstallRequest,
            PluginReviewRequest,
            PluginUpdateRequest,
            SchedulerRunRequest,
            SchedulerStepRequest,
            SelfChangeRequest,
            SelfOnboardingRequest,
            SelfRememberRequest,
            SkillInstallRequest,
            SubagentRequest,
            ToolInvokeRequest,
        )
        from .server_observability_routes import register_observability_routes
        from .server_runtime_routes import register_runtime_routes
        from .server_secret_routes import register_secret_routes
        from .server_tool_routes import register_tool_routes, tool_invoke_response
        from .server_web_routes import register_web_routes
    except ImportError as exc:
        raise RuntimeError("Install server extras with `pip install -e '.[server]'`.") from exc

    FastAPI = fastapi_module.FastAPI
    HTTPException = fastapi_module.HTTPException
    Header = fastapi_module.Header
    Request = fastapi_module.Request
    StreamingResponse = responses_module.StreamingResponse
    FileResponse = responses_module.FileResponse
    StaticFiles = staticfiles_module.StaticFiles
    CORSMiddleware = cors_module.CORSMiddleware

    base_config = config or AgentConfig.from_env()
    runtime_settings_store = RuntimeSettingsStore(default_runtime_settings_path(base_config))
    active_config = apply_runtime_settings(base_config, runtime_settings_store.load(base_config))
    secret_broker = build_secret_broker(
        active_config.secret_store_path, backend=active_config.secret_backend
    )
    state = AgentStateStore(active_config.state_path)
    events = RunEventBus(state)
    mcp = MCPManager(
        state,
        allow_network_endpoints=active_config.allow_mcp_network_endpoints,
        secret_resolver=secret_broker.resolve,
    )
    skills = SkillManager(active_config.skills_dir, state)
    plugins = PluginManager(active_config.plugins_dir, state)
    runs = RunManager(
        config=active_config, state=state, events=events, mcp=mcp, skills=skills, plugins=plugins
    )
    channels = ChannelManager(active_config, secret_resolver=secret_broker.resolve, run_manager=runs)
    secret_broker.register_allowed_env_names(
        _known_secret_env_names(channels.list_channels(), mcp.list_servers())
    )

    def update_active_config(next_config: AgentConfig) -> None:
        nonlocal active_config
        active_config = next_config
        runs.config = next_config
        channels.config = next_config

    def require_api_auth(
        authorization: str | None = Header(default=None),
        x_kestrel_api_key: str | None = Header(default=None),
    ) -> bool:
        auth_error = _api_auth_error(
            active_config,
            {"authorization": authorization or "", "x-kestrel-api-key": x_kestrel_api_key or ""},
        )
        if auth_error is not None:
            status_code, detail = auth_error
            raise HTTPException(status_code=status_code, detail=detail)
        return True

    def audit_plugin(action: str, plugin: dict[str, Any]) -> None:
        memory = build_memory_system(active_config.backend, active_config.memory_dir)
        try:
            plugins.write_audit_memory(memory, action=action, plugin=plugin)
        finally:
            memory.close_all()

    def inspect_memory_payload(
        *, query: str | None, layers: list[str] | None, k: int, include_inactive: bool = False
    ) -> dict[str, object]:
        arguments: dict[str, object] = {
            "query": query.strip() if query and query.strip() else "memory",
            "k": _bounded_limit(k, default=20, maximum=100),
        }
        if layers:
            arguments["layers"] = layers
        if include_inactive:
            arguments["include_inactive"] = True
        execution = runs.invoke_tool(
            tool_name="memory.inspect", arguments=arguments, session_id="api"
        )
        return _tool_response_payload(execution)

    def filter_cognition_items(payload: dict[str, object], schema: str) -> list[dict[str, object]]:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            return []
        rows: list[dict[str, object]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            record = item.get("record")
            if not isinstance(record, dict):
                continue
            metadata = record.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("cognition_schema") != schema:
                continue
            rows.append(item)
        return rows

    def require_plugin_install_enabled() -> None:
        if not active_config.allow_plugin_install:
            raise HTTPException(status_code=403, detail="plugin_install_disabled")

    @asynccontextmanager
    async def lifespan(app_instance: Any) -> Any:
        del app_instance
        try:
            yield
        finally:
            channels.close()
            mcp.shutdown()

    app = FastAPI(
        title="Nested MV2 Agent",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_config.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")  # type: ignore[untyped-decorator]
    async def local_ingress_guard(request: Any, call_next: Any) -> Any:
        headers = request_headers(request)
        host = _hostname_from_header(str(headers.get("host", "")))
        trusted_hosts = set(active_config.trusted_hosts)
        if not _host_is_trusted(host, trusted_hosts):  # nosec
            return responses_module.JSONResponse({"detail": "untrusted_host"}, status_code=400)
        origin = str(headers.get("origin", "")).strip()
        if origin:
            origin_host = _hostname_from_url(origin)
            if origin_host and not _host_is_trusted(origin_host, trusted_hosts):  # nosec
                return responses_module.JSONResponse(
                    {"detail": "untrusted_origin"}, status_code=403
                )
        path = str(getattr(getattr(request, "url", None), "path", ""))
        if path == "/api" or path.startswith("/api/"):
            auth_error = _api_auth_error(active_config, headers)
            if auth_error is not None:
                status_code, detail = auth_error
                return responses_module.JSONResponse({"detail": detail}, status_code=status_code)
        return await call_next(request)

    register_runtime_routes(
        app,
        active_config=lambda: active_config,
        state=state,
        settings_store=runtime_settings_store,
        on_config_update=update_active_config,
        http_exception=HTTPException,
    )

    register_secret_routes(
        app,
        http_exception=HTTPException,
        secret_broker=secret_broker,
    )
    register_channel_routes(
        app,
        http_exception=HTTPException,
        request_type=Request,
        channels=channels,
        secret_broker=secret_broker,
        mcp=mcp,
    )

    @app.post("/api/runs")  # type: ignore[untyped-decorator]
    def create_run(request: CreateRunRequest) -> dict[str, object]:
        run = runs.create_run(
            message=request.message,
            session_id=request.session_id,
            workspace=Path(request.workspace) if request.workspace else None,
            provider=request.provider,
            model=request.model,
            autonomy_mode=request.autonomy_mode,
        )
        return asdict(run)

    @app.get("/api/runs")  # type: ignore[untyped-decorator]
    def list_runs() -> list[dict[str, object]]:
        return runs.list_runs()

    @app.get("/api/sessions")  # type: ignore[untyped-decorator]
    def list_sessions() -> list[dict[str, object]]:
        return runs.list_sessions()

    @app.get("/api/sessions/{session_id}/runs")  # type: ignore[untyped-decorator]
    def list_session_runs(session_id: str) -> list[dict[str, object]]:
        return runs.list_runs_for_session(session_id)

    @app.get("/api/runs/{run_id}")  # type: ignore[untyped-decorator]
    def get_run(run_id: str) -> dict[str, object]:
        try:
            return runs.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/cancel")  # type: ignore[untyped-decorator]
    def cancel_run(run_id: str) -> dict[str, object]:
        try:
            return runs.cancel_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/task-graph")  # type: ignore[untyped-decorator]
    def task_graph(run_id: str) -> dict[str, object]:
        try:
            return runs.task_graph(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/approve-task")  # type: ignore[untyped-decorator]
    def approve_task(run_id: str, request: dict[str, str]) -> dict[str, object]:
        try:
            return runs.approve_task(run_id, str(request["task_id"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/scheduler/step")  # type: ignore[untyped-decorator]
    def scheduler_step(run_id: str, request: SchedulerStepRequest) -> dict[str, object]:
        try:
            return runs.run_scheduler_step(run_id, max_tasks=request.max_tasks)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/scheduler/run")  # type: ignore[untyped-decorator]
    def scheduler_run(run_id: str, request: SchedulerRunRequest) -> dict[str, object]:
        try:
            return runs.run_scheduler_until_idle(
                run_id, max_tasks=request.max_tasks, max_cycles=request.max_cycles
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    register_observability_routes(
        app,
        active_config=active_config,
        http_exception=HTTPException,
        streaming_response=StreamingResponse,
        state=state,
        events=events,
        runs=runs,
    )

    register_tool_routes(app, runs=runs)

    @app.get("/api/self")  # type: ignore[untyped-decorator]
    def inspect_self() -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="self.inspect",
            arguments={"include_tools": True},
            session_id="api",
        )
        if execution.success:
            return execution.data
        return _execution_response(execution)

    @app.get("/api/self/onboarding")  # type: ignore[untyped-decorator]
    def inspect_self_onboarding() -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="self.reflect",
            arguments={"query": SELF_PROFILE_QUERY, "k": 8},
            session_id="api",
        )
        rows = []
        if execution.success and isinstance(execution.data, dict):
            raw_rows = execution.data.get("self_memory_hits")
            rows = raw_rows if isinstance(raw_rows, list) else []
        state_payload = onboarding_state_from_reflection(rows)
        state_payload["reflection"] = execution.data.get("reflection") if isinstance(execution.data, dict) else None
        return state_payload

    @app.post("/api/self/onboarding")  # type: ignore[untyped-decorator]
    def save_self_onboarding(request: SelfOnboardingRequest) -> dict[str, object]:
        profile = build_onboarding_profile(request.model_dump())
        execution = runs.invoke_tool(
            tool_name="self.remember",
            arguments={
                "title": onboarding_record_title(profile),
                "content": onboarding_record_content(profile),
                "schema": SELF_PROFILE_SCHEMA,
                "validation_status": "user_confirmed",
                "confidence": 0.92,
                "importance": 0.84,
                "source": "web.onboarding_wizard",
                "locator": "api://self/onboarding",
            },
            session_id="api",
        )
        return {
            "success": execution.success,
            "profile": profile,
            "personas": persona_presets_public(),
            "memory": _execution_response(execution),
        }

    @app.post("/api/self/remember")  # type: ignore[untyped-decorator]
    def remember_self(request: SelfRememberRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="self.remember",
            arguments=request.model_dump(by_alias=True),
            session_id="api",
        )
        return _execution_response(execution)

    @app.post("/api/self/propose-change")  # type: ignore[untyped-decorator]
    def propose_self_change(request: SelfChangeRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="self.propose_change",
            arguments=request.model_dump(),
            session_id="api",
        )
        return _execution_response(execution)

    register_web_routes(app, runs=runs)

    @app.get("/api/approvals")  # type: ignore[untyped-decorator]
    def list_approvals(status: str | None = None) -> list[dict[str, object]]:
        return state.list_approvals(status=status)

    @app.post("/api/approvals/{approval_id}/decision")  # type: ignore[untyped-decorator]
    def decide_approval(approval_id: str, request: ApprovalDecisionRequest) -> dict[str, object]:
        try:
            return runs.decide_approval(
                approval_id,
                approved=request.approved,
                arguments=request.arguments,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    register_mcp_routes(
        app,
        http_exception=HTTPException,
        state=state,
        mcp=mcp,
        runs=runs,
        secret_broker=secret_broker,
    )

    @app.get("/api/plugins")  # type: ignore[untyped-decorator]
    def list_plugins() -> list[dict[str, object]]:
        return plugins.list_plugins()

    @app.post("/api/plugins/review")  # type: ignore[untyped-decorator]
    def review_plugin(request: PluginReviewRequest) -> dict[str, object]:
        require_plugin_install_enabled()
        try:
            return plugins.review(request.source, ref=request.ref)
        except PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/plugins/{plugin_id}")  # type: ignore[untyped-decorator]
    def get_plugin(plugin_id: str) -> dict[str, object]:
        try:
            return plugins.get_plugin(plugin_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/plugins/sync")  # type: ignore[untyped-decorator]
    def sync_plugins() -> list[dict[str, object]]:
        require_plugin_install_enabled()
        plugins.sync_all()
        return plugins.list_plugins()

    @app.post("/api/plugins/{plugin_id}/sync")  # type: ignore[untyped-decorator]
    def sync_plugin(plugin_id: str) -> dict[str, object]:
        require_plugin_install_enabled()
        try:
            plugins.sync_plugin(plugin_id)
            return plugins.get_plugin(plugin_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/plugins/install")  # type: ignore[untyped-decorator]
    def install_plugin(request: PluginInstallRequest) -> dict[str, object]:
        require_plugin_install_enabled()
        try:
            plugin = plugins.install(
                request.source,
                ref=request.ref,
                enable=request.enable,
                overwrite=request.overwrite,
            )
            audit_plugin("install", plugin)
            return plugin
        except (PluginError, FileExistsError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/plugins/{plugin_id}/enable")  # type: ignore[untyped-decorator]
    def enable_plugin(plugin_id: str) -> dict[str, object]:
        require_plugin_install_enabled()
        try:
            plugin = plugins.set_enabled(plugin_id, True)
            audit_plugin("enable", plugin)
            return plugin
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PluginError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/plugins/{plugin_id}/disable")  # type: ignore[untyped-decorator]
    def disable_plugin(plugin_id: str) -> dict[str, object]:
        try:
            plugin = plugins.set_enabled(plugin_id, False)
            audit_plugin("disable", plugin)
            return plugin
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/plugins/{plugin_id}/update")  # type: ignore[untyped-decorator]
    def update_plugin(
        plugin_id: str, request: PluginUpdateRequest | None = None
    ) -> dict[str, object]:
        require_plugin_install_enabled()
        try:
            plugin = plugins.update(plugin_id, ref=request.ref if request else None)
            audit_plugin("update", plugin)
            return plugin
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PluginError, FileExistsError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/plugins/{plugin_id}")  # type: ignore[untyped-decorator]
    def remove_plugin(plugin_id: str) -> dict[str, object]:
        try:
            return plugins.remove(plugin_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/subagents")  # type: ignore[untyped-decorator]
    def create_subagent(request: SubagentRequest) -> dict[str, object]:
        try:
            return runs.create_subagent(
                run_id=request.run_id,
                profile=request.profile,
                goal=request.goal,
                task_id=request.task_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/skills")  # type: ignore[untyped-decorator]
    def list_skills() -> list[dict[str, object]]:
        return skills.list_skills()

    @app.get("/api/skills/{skill_id}")  # type: ignore[untyped-decorator]
    def get_skill(skill_id: str) -> dict[str, object]:
        try:
            return skills.state.get_skill(skill_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/skills/discover")  # type: ignore[untyped-decorator]
    def discover_skills() -> dict[str, object]:
        return skills.discover_report()

    @app.post("/api/skills/install")  # type: ignore[untyped-decorator]
    def install_skill(request: SkillInstallRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="skill.install",
            arguments=request.model_dump(),
            session_id="api",
        )
        return _tool_response_payload(execution)

    @app.post("/api/skills/{skill_id}/enable")  # type: ignore[untyped-decorator]
    def enable_skill(skill_id: str) -> dict[str, object]:
        try:
            return skills.set_enabled(skill_id, True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/skills/{skill_id}/disable")  # type: ignore[untyped-decorator]
    def disable_skill(skill_id: str) -> dict[str, object]:
        try:
            return skills.set_enabled(skill_id, False)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/skills/{skill_id}/run")  # type: ignore[untyped-decorator]
    def run_skill(skill_id: str, request: ToolInvokeRequest) -> dict[str, object]:
        return tool_invoke_response(runs, f"skill.{skill_id}.run", request)

    def _search_memory(
        query: str,
        layers: list[str] | None = None,
        k: int = 8,
        include_inactive: bool = False,
    ) -> list[dict[str, object]]:
        if k < 1 or k > 50:
            raise HTTPException(status_code=400, detail="k must be between 1 and 50")
        agent = build_agent(active_config, tools=runs.build_registry(), state=state)
        try:
            selected_layers = (
                tuple(MemoryLayer(layer) for layer in layers) if layers else tuple(MemoryLayer)
            )
            hits = agent.memory.retrieve(
                RetrievalQuery(
                    query=query,
                    layers=selected_layers,
                    k_per_layer=k,
                    include_inactive=include_inactive,
                )
            )
            return [
                {
                    "layer": hit.record.layer.value,
                    "kind": hit.record.kind.value,
                    "title": hit.record.title,
                    "score": hit.score,
                    "snippet": hit.snippet or hit.record.content[:500],
                    "record_id": hit.record.id,
                }
                for hit in hits[:k]
            ]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            agent.close()

    @app.get("/api/memory/search")  # type: ignore[untyped-decorator]
    def search_memory_get(
        query: str, layers: str | None = None, k: int = 8, include_inactive: bool = False
    ) -> list[dict[str, object]]:
        return _search_memory(
            query=query, layers=_csv_layers(layers), k=k, include_inactive=include_inactive
        )

    @app.post("/api/memory/search")  # type: ignore[untyped-decorator]
    def search_memory(request: MemorySearchRequest) -> list[dict[str, object]]:
        return _search_memory(
            query=request.query,
            layers=request.layers,
            k=request.k,
            include_inactive=request.include_inactive,
        )

    @app.get("/api/memory/verify")  # type: ignore[untyped-decorator]
    def verify_memory() -> dict[str, bool]:
        agent = build_agent(active_config, tools=runs.build_registry(), state=state)
        try:
            return {layer.value: ok for layer, ok in agent.memory.verify_all().items()}
        finally:
            agent.close()

    @app.get("/api/memory/layers")  # type: ignore[untyped-decorator]
    def memory_layers() -> list[dict[str, object]]:
        agent = build_agent(active_config, tools=runs.build_registry(), state=state)
        try:
            verify = agent.memory.verify_all()
            rows: list[dict[str, object]] = []
            for layer in MemoryLayer:
                backend = agent.memory.backends[layer]
                path = Path(str(getattr(backend, "path", "")))
                rows.append(
                    {
                        "layer": layer.value,
                        "path": str(path),
                        "exists": path.exists(),
                        "ok": bool(verify.get(layer, False)),
                        "backend": type(backend).__name__,
                    }
                )
            return rows
        finally:
            agent.close()

    @app.get("/api/memory/inspect")  # type: ignore[untyped-decorator]
    def inspect_memory_get(
        query: str | None = None,
        layers: str | None = None,
        k: int = 20,
        include_inactive: bool = False,
    ) -> dict[str, object]:
        return inspect_memory_payload(
            query=query, layers=_csv_layers(layers), k=k, include_inactive=include_inactive
        )

    @app.post("/api/memory/inspect")  # type: ignore[untyped-decorator]
    def inspect_memory(request: MemoryInspectAPIRequest) -> dict[str, object]:
        return inspect_memory_payload(
            query=request.query,
            layers=request.layers,
            k=request.k,
            include_inactive=request.include_inactive,
        )

    @app.post("/api/memory/consolidate")  # type: ignore[untyped-decorator]
    def consolidate_memory(request: MemoryConsolidateRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="memory.consolidate",
            arguments=request.model_dump(),
            session_id="api",
        )
        if execution.content.startswith("{"):
            payload = json.loads(execution.content)
            if isinstance(payload, dict):
                return dict(payload)
        return {
            "success": execution.success,
            "content": execution.content,
            "error": execution.error,
        }

    @app.post("/api/memory/learn")  # type: ignore[untyped-decorator]
    def learn_memory(request: MemoryLearnRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="memory.learn",
            arguments=request.model_dump(),
            session_id="api",
        )
        if execution.content.startswith("{"):
            payload = json.loads(execution.content)
            if isinstance(payload, dict):
                return dict(payload)
        return {
            "success": execution.success,
            "content": execution.content,
            "error": execution.error,
        }

    @app.post("/api/memory/correct")  # type: ignore[untyped-decorator]
    def correct_memory(request: MemoryCorrectRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="memory.correct",
            arguments=request.model_dump(),
            session_id="api",
        )
        if execution.content.startswith("{"):
            payload = json.loads(execution.content)
            if isinstance(payload, dict):
                return dict(payload)
        return {
            "success": execution.success,
            "content": execution.content,
            "error": execution.error,
        }

    @app.post("/api/memory/compact")  # type: ignore[untyped-decorator]
    def compact_memory(request: MemoryCompactRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="memory.compact",
            arguments=request.model_dump(),
            session_id="api",
        )
        if execution.content.startswith("{"):
            payload = json.loads(execution.content)
            if isinstance(payload, dict):
                return dict(payload)
        return {
            "success": execution.success,
            "content": execution.content,
            "error": execution.error,
        }

    @app.post("/api/context/pack")  # type: ignore[untyped-decorator]
    def pack_context(request: ContextPackAPIRequest) -> dict[str, object]:
        arguments = request.model_dump(exclude_none=True)
        execution = runs.invoke_tool(
            tool_name="context.pack", arguments=arguments, session_id="api"
        )
        return _tool_response_payload(execution)

    @app.post("/api/context/expand")  # type: ignore[untyped-decorator]
    def expand_context(request: ContextExpandAPIRequest) -> dict[str, object]:
        arguments = request.model_dump(exclude_none=True)
        execution = runs.invoke_tool(
            tool_name="context.expand", arguments=arguments, session_id="api"
        )
        return _tool_response_payload(execution)

    @app.get("/api/context")  # type: ignore[untyped-decorator]
    def get_context(
        query: str,
        token_budget: int | None = None,
        layers: str | None = None,
        expand_raw: bool = False,
        include_telemetry: bool = True,
    ) -> dict[str, object]:
        arguments: dict[str, object] = {
            "query": query,
            "expand_raw": expand_raw,
            "include_telemetry": include_telemetry,
        }
        if token_budget is not None:
            arguments["token_budget"] = token_budget
        parsed_layers = _csv_layers(layers)
        if parsed_layers is not None:
            arguments["layers"] = parsed_layers
        execution = runs.invoke_tool(
            tool_name="context.pack", arguments=arguments, session_id="api"
        )
        payload = _tool_response_payload(execution)
        if not execution.success:
            raise HTTPException(status_code=400, detail=payload)
        return payload

    @app.post("/api/capsules/{run_id}/summarize")  # type: ignore[untyped-decorator]
    def summarize_capsule(run_id: str, request: CapsuleSummarizeAPIRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="capsule.summarize",
            arguments={"run_id": run_id, "dry_run": request.dry_run},
            session_id="api",
        )
        return _tool_response_payload(execution)

    @app.post("/api/capsules/{run_id}/apply")  # type: ignore[untyped-decorator]
    def apply_capsule(run_id: str, request: CapsuleApplyAPIRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="capsule.apply",
            arguments={
                "run_id": run_id,
                "dry_run": request.dry_run,
                "include_policy": request.include_policy,
            },
            session_id="api",
            run_id=run_id,
        )
        return _tool_response_payload(execution)

    @app.get("/api/memory/conflicts")  # type: ignore[untyped-decorator]
    def memory_conflicts(query: str, layers: str | None = None, k: int = 8) -> dict[str, object]:
        arguments: dict[str, object] = {"query": query, "k": k}
        if layers:
            arguments["layers"] = [layer.strip() for layer in layers.split(",") if layer.strip()]
        execution = runs.invoke_tool(
            tool_name="memory.conflicts", arguments=arguments, session_id="api"
        )
        return _tool_response_payload(execution)

    @app.get("/api/cognition/lessons")  # type: ignore[untyped-decorator]
    def list_lessons(query: str | None = None, k: int = 20) -> dict[str, object]:
        payload = inspect_memory_payload(
            query=query or "LessonCard lesson failure corrected strategy",
            layers=["procedural", "episodic"],
            k=k,
        )
        return {"items": filter_cognition_items(payload, "lesson_card.v1")}

    @app.get("/api/cognition/failures")  # type: ignore[untyped-decorator]
    def list_failures(query: str | None = None, k: int = 20) -> dict[str, object]:
        payload = inspect_memory_payload(
            query=query or "FailureEpisode failure diagnosis tool failed",
            layers=["episodic"],
            k=k,
        )
        return {"items": filter_cognition_items(payload, "failure_episode.v1")}

    register_diagnosis_routes(app, runs=runs)

    web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
    if web_dist.exists():
        assets = web_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/")  # type: ignore[untyped-decorator]
        def index() -> Any:
            return FileResponse(web_dist / "index.html")

        @app.get("/{path:path}")  # type: ignore[untyped-decorator]
        def spa_fallback(path: str) -> Any:
            if path == "api" or path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not_found")
            return FileResponse(web_dist / "index.html")

    return app
