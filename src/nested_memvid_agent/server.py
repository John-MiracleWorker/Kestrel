import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from threading import Thread
from typing import Any
from uuid import uuid4

from .behavior_delta_ledger import BehaviorDeltaLedger
from .capability_policy import parent_resource_digest
from .channels import ChannelManager
from .config import AgentConfig
from .event_bus import RunEventBus
from .layers import (
    load_layer_specs,
    prepare_private_memory_artifacts,
    prepare_private_runs_root,
)
from .llm.model_catalog import DEFAULT_API_KEY_ENVS
from .mcp_manager import MCPManager, mcp_sensitive_material_transition
from .models import MemoryLayer, RetrievalQuery
from .plugin_manager import PluginError, PluginManager
from .promotion_ledger import PromotionLedger
from .routine_loop import RoutineLoop
from .routines import RoutineService
from .run_manager import RunCapacityError, RunManager
from .runtime_settings import (
    RuntimeSettingsStore,
    apply_runtime_settings,
    default_runtime_settings_path,
)
from .secret_broker import build_secret_broker
from .self_profile import (
    SELF_PROFILE_QUERY,
    SELF_PROFILE_SCHEMA,
    TRUSTED_ONBOARDING_ORIGIN,
    build_onboarding_profile,
    onboarding_record_content,
    onboarding_record_title,
    onboarding_state_from_reflection,
    persona_presets_public,
)
from .server_support import (
    RequestBodyTooLarge,
    RequestRateLimiter,
    request_headers,
)
from .server_support import (
    api_auth_error as _api_auth_error,
)
from .server_support import (
    bounded_limit as _bounded_limit,
)
from .server_support import (
    cache_bounded_request_body as _cache_bounded_request_body,
)
from .server_support import (
    csv_layers as _csv_layers,
)
from .server_support import (
    execution_response as _execution_response,
)
from .server_support import (
    host_is_trusted as _host_is_trusted,
)
from .server_support import (
    hostname_from_header as _hostname_from_header,
)
from .server_support import (
    hostname_from_url as _hostname_from_url,
)
from .server_support import (
    known_secret_env_names as _known_secret_env_names,
)
from .server_support import (
    tool_response_payload as _tool_response_payload,
)
from .skill_manager import SkillManager
from .state_store import AgentStateStore, CapabilityConflictError

_BROWSER_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "base-uri 'none'; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "form-action 'self'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data: https:; "
        "manifest-src 'self'; "
        "object-src 'none'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "worker-src 'self' blob:"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
        "magnetometer=(), microphone=(), payment=(), usb=()"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def _apply_browser_security_headers(response: Any) -> None:
    for name, value in _BROWSER_SECURITY_HEADERS.items():
        if name not in response.headers:
            response.headers[name] = value


def create_app(config: AgentConfig | None = None) -> Any:
    """Create the local Kestrel web/API app."""

    construction_cleanup: list[Callable[[], None]] = []
    try:
        return _create_app(config, construction_cleanup=construction_cleanup)
    except BaseException:
        for cleanup in reversed(construction_cleanup):
            try:
                cleanup()
            except Exception:
                pass
        raise


def _create_app(
    config: AgentConfig | None,
    *,
    construction_cleanup: list[Callable[[], None]],
) -> Any:
    """Assemble an app while exposing acquired resources to the factory guard."""

    try:
        fastapi_module = import_module("fastapi")
        responses_module = import_module("starlette.responses")
        staticfiles_module = import_module("starlette.staticfiles")
        cors_module = import_module("starlette.middleware.cors")
        from .server_behavior_delta_routes import register_behavior_delta_routes
        from .server_capability_routes import register_capability_routes
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
        from .server_product_routes import register_product_routes
        from .server_routine_routes import register_routine_routes
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
    _prepare_private_runtime_artifacts(active_config)
    workspace = active_config.workspace.expanduser().resolve()
    secret_store_path = active_config.secret_store_path.expanduser()
    if not secret_store_path.is_absolute():
        secret_store_path = workspace / secret_store_path
    secret_store_path = secret_store_path.resolve()
    secret_broker = build_secret_broker(
        secret_store_path, backend=active_config.secret_backend
    )
    state = AgentStateStore(active_config.state_path)
    events = RunEventBus(state)
    mcp = MCPManager(
        state,
        allow_network_endpoints=active_config.allow_mcp_network_endpoints,
        secret_resolver=secret_broker.resolve,
        workspace=workspace,
        secret_store_path=secret_store_path,
        secret_backend=active_config.secret_backend,
    )
    skills = SkillManager(active_config.skills_dir, state)
    plugins = PluginManager(active_config.plugins_dir, state)
    runs = RunManager(
        config=active_config,
        state=state,
        events=events,
        mcp=mcp,
        skills=skills,
        plugins=plugins,
        secret_resolver=secret_broker.resolve,
        enforce_single_owner=True,
        auto_start=False,
    )

    def abort_runtime_construction() -> None:
        runs_stopped = runs.shutdown(timeout_seconds=5.0)
        if not runs_stopped:
            runs_stopped = runs.shutdown(timeout_seconds=1.0)
        mcp_stopped = mcp.shutdown()
        if not runs_stopped or not mcp_stopped:
            raise RuntimeError("runtime_shutdown_incomplete")

    construction_cleanup.append(abort_runtime_construction)
    channels = ChannelManager(active_config, secret_resolver=secret_broker.resolve, run_manager=runs)
    routine_service = RoutineService(
        state,
        runs,
        claim_ttl_seconds=active_config.routine_claim_ttl_seconds,
        max_occurrences_per_tick=active_config.max_routines_per_tick,
    )
    routine_loop = (
        RoutineLoop(
            routine_service,
            interval_seconds=active_config.routine_poll_interval_seconds,
        )
        if active_config.enable_proactive_routines
        else None
    )
    secret_broker.register_allowed_env_names(
        _known_secret_env_names(channels.list_channels(), mcp.list_servers())
        | _provider_secret_env_names(active_config)
    )

    def update_active_config(next_config: AgentConfig) -> None:
        nonlocal active_config
        active_config = next_config
        runs.config = next_config
        channels.config = next_config
        secret_broker.register_allowed_env_names(_provider_secret_env_names(next_config))

    channels.configure_runtime_settings(
        settings_store=runtime_settings_store,
        config_update_handler=update_active_config,
    )

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
        agent = runs.build_runtime_agent(active_config)
        try:
            plugins.write_audit_memory(agent.memory, action=action, plugin=plugin)
        finally:
            runs.close_runtime_agent(agent)

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
            runs.start()
            if routine_loop is not None:
                routine_loop.start()
            if active_config.provider_startup_probe:
                Thread(
                    target=_probe_provider_health,
                    kwargs={"config": active_config, "secret_resolver": secret_broker.resolve},
                    name="kestrel-provider-startup-probe",
                    daemon=True,
                ).start()
            yield
        finally:
            shutdown_incomplete = False
            try:
                loop_stopped = (
                    routine_loop is None
                    or routine_loop.close(timeout_seconds=5.0)
                )
                if routine_loop is not None and not loop_stopped:
                    loop_stopped = routine_loop.close(timeout_seconds=1.0)
                shutdown_incomplete = not loop_stopped
            except Exception:  # noqa: BLE001 - finish all dependency cleanup before reporting
                shutdown_incomplete = True
            try:
                runs_stopped = runs.shutdown(timeout_seconds=5.0)
                if not runs_stopped:
                    runs_stopped = runs.shutdown(timeout_seconds=1.0)
                shutdown_incomplete = shutdown_incomplete or not runs_stopped
            except Exception:  # noqa: BLE001 - finish all dependency cleanup before reporting
                shutdown_incomplete = True
            try:
                channels.close()
            except Exception:  # noqa: BLE001 - MCP cleanup must still run
                shutdown_incomplete = True
            try:
                mcp_stopped = mcp.shutdown()
                shutdown_incomplete = shutdown_incomplete or not mcp_stopped
            except Exception:  # noqa: BLE001 - report a fixed, non-secret lifecycle error
                shutdown_incomplete = True
            if shutdown_incomplete:
                raise RuntimeError("runtime_shutdown_incomplete") from None

    app = FastAPI(
        title="Kestrel",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(active_config.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    rate_limiter = RequestRateLimiter()

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
            if not origin_host or not _host_is_trusted(origin_host, trusted_hosts):  # nosec
                return responses_module.JSONResponse(
                    {"detail": "untrusted_origin"}, status_code=403
                )
        path = str(getattr(getattr(request, "url", None), "path", ""))
        method = str(getattr(request, "method", "GET")).upper()
        api_path = path == "/api" or path.startswith("/api/")
        guarded_path = api_path or path == "/metrics"
        public_telegram_webhook = (
            method == "POST" and path == "/api/channels/telegram/webhook"
        )
        cors_preflight = (
            method == "OPTIONS"
            and bool(origin)
            and bool(str(headers.get("access-control-request-method", "")).strip())
        )
        if guarded_path:
            if not public_telegram_webhook and not cors_preflight:
                auth_error = _api_auth_error(active_config, headers)
                if auth_error is not None:
                    status_code, detail = auth_error
                    return responses_module.JSONResponse({"detail": detail}, status_code=status_code)
            content_length = str(headers.get("content-length", "")).strip()
            if content_length:
                try:
                    request_bytes = int(content_length)
                except ValueError:
                    return responses_module.JSONResponse({"detail": "invalid_content_length"}, status_code=400)
                if request_bytes > active_config.max_request_body_bytes:
                    return responses_module.JSONResponse({"detail": "request_body_too_large"}, status_code=413)
            if method not in {"GET", "HEAD", "OPTIONS"}:
                try:
                    await _cache_bounded_request_body(
                        request,
                        limit=active_config.max_request_body_bytes,
                    )
                except RequestBodyTooLarge:
                    return responses_module.JSONResponse(
                        {"detail": "request_body_too_large"},
                        status_code=413,
                    )
                client = getattr(request, "client", None)
                client_host = str(getattr(client, "host", "local"))
                if not rate_limiter.allow(
                    client_host,
                    limit=active_config.api_rate_limit_requests,
                    window_seconds=active_config.api_rate_limit_window_seconds,
                    max_keys=active_config.api_rate_limit_max_clients,
                ):
                    return responses_module.JSONResponse({"detail": "rate_limit_exceeded"}, status_code=429)
        return await call_next(request)

    @app.middleware("http")  # type: ignore[untyped-decorator]
    async def request_correlation(request: Any, call_next: Any) -> Any:
        candidate = str(request.headers.get("x-request-id", "")).strip()
        request_id = (
            candidate
            if 0 < len(candidate) <= 128
            and all(character.isalnum() or character in "-_." for character in candidate)
            else f"req_{uuid4().hex}"
        )
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        _apply_browser_security_headers(response)
        return response

    register_runtime_routes(
        app,
        active_config=lambda: active_config,
        state=state,
        settings_store=runtime_settings_store,
        validate_config_update=_prepare_private_runtime_artifacts,
        on_config_update=update_active_config,
        secret_broker=secret_broker,
        http_exception=HTTPException,
        runs=runs,
        routine_loop=routine_loop,
        provider_probe=lambda: _probe_provider_health(
            config=active_config,
            secret_resolver=secret_broker.resolve,
        ),
    )
    register_routine_routes(
        app,
        active_config=lambda: active_config,
        state=state,
        service=routine_service,
        loop=routine_loop,
        http_exception=HTTPException,
    )

    register_secret_routes(
        app,
        http_exception=HTTPException,
        secret_broker=secret_broker,
        sensitive_material_transition=mcp_sensitive_material_transition,
    )
    register_product_routes(app, active_config=lambda: active_config, secret_resolver=secret_broker.resolve)
    register_channel_routes(
        app,
        http_exception=HTTPException,
        request_type=Request,
        channels=channels,
        secret_broker=secret_broker,
        mcp=mcp,
    )

    @app.post("/api/runs")  # type: ignore[untyped-decorator]
    def create_run(
        request: CreateRunRequest,
        http_request: Request,  # type: ignore[valid-type]
    ) -> dict[str, object]:
        try:
            run = runs.create_run(
                message=request.message,
                session_id=request.session_id,
                workspace=Path(request.workspace) if request.workspace else None,
                provider=request.provider,
                model=request.model,
                autonomy_mode=request.autonomy_mode,
            )
        except RunCapacityError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        runs.events.publish(
            run.run_id,
            "request.correlated",
            {
                "request_id": str(
                    getattr(getattr(http_request, "state", None), "request_id", "unknown")
                )
            },
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
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/scheduler/step")  # type: ignore[untyped-decorator]
    def scheduler_step(run_id: str, request: SchedulerStepRequest) -> dict[str, object]:
        try:
            return runs.run_scheduler_step(run_id, max_tasks=request.max_tasks)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/runs/{run_id}/scheduler/run")  # type: ignore[untyped-decorator]
    def scheduler_run(run_id: str, request: SchedulerRunRequest) -> dict[str, object]:
        try:
            return runs.run_scheduler_until_idle(
                run_id, max_tasks=request.max_tasks, max_cycles=request.max_cycles
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    register_observability_routes(
        app,
        active_config=lambda: active_config,
        http_exception=HTTPException,
        streaming_response=StreamingResponse,
        state=state,
        events=events,
        runs=runs,
        routine_loop=routine_loop,
    )
    register_behavior_delta_routes(app, http_exception=HTTPException, ledger=BehaviorDeltaLedger(state))


    @app.get("/api/learning/dashboard")  # type: ignore[untyped-decorator]
    def learning_dashboard(since: str = "30d") -> dict[str, object]:
        ledger = PromotionLedger(state)
        return ledger.learning_dashboard(since=_parse_since_window(since)).to_payload()

    register_tool_routes(app, runs=runs)
    register_capability_routes(
        app,
        http_exception=HTTPException,
        state=state,
        runs=runs,
        mcp=mcp,
        skills=skills,
    )

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
            raw_rows = execution.data.get("trusted_onboarding_hits")
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
            trusted_request_origin=TRUSTED_ONBOARDING_ORIGIN,
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
        return runs.list_approvals(status=status)

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
        mcp.close_disabled_sessions()
        return plugins.list_plugins()

    @app.post("/api/plugins/{plugin_id}/sync")  # type: ignore[untyped-decorator]
    def sync_plugin(plugin_id: str) -> dict[str, object]:
        require_plugin_install_enabled()
        try:
            plugins.sync_plugin(plugin_id)
            mcp.close_disabled_sessions()
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
            mcp.close_disabled_sessions()
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
            mcp.close_disabled_sessions()
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
            mcp.close_disabled_sessions()
            audit_plugin("update", plugin)
            return plugin
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (PluginError, FileExistsError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/plugins/{plugin_id}")  # type: ignore[untyped-decorator]
    def remove_plugin(plugin_id: str) -> dict[str, object]:
        try:
            result = plugins.remove(plugin_id)
            mcp.close_disabled_sessions()
            return result
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
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

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
            return _set_legacy_skill_enabled(skill_id, enabled=True)
        except CapabilityConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "capability_revision_conflict", "current": exc.current},
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/skills/{skill_id}/disable")  # type: ignore[untyped-decorator]
    def disable_skill(skill_id: str) -> dict[str, object]:
        try:
            return _set_legacy_skill_enabled(skill_id, enabled=False)
        except CapabilityConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={"error": "capability_revision_conflict", "current": exc.current},
            ) from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _set_legacy_skill_enabled(skill_id: str, *, enabled: bool) -> dict[str, object]:
        """Keep compatibility endpoints inside the audited capability boundary."""

        skill = state.get_skill(skill_id)
        decision = runs.capabilities.parent_decision(
            "skill",
            skill_id,
            entity_enabled=bool(skill.get("enabled", False)),
        )
        state.set_capability_override(
            "skill",
            skill_id,
            enabled,
            expected_revision=decision.revision,
            default_enabled=decision.default_enabled,
            resource_digest=parent_resource_digest(state, "skill", skill_id),
            updated_by="owner:legacy-skill-endpoint",
        )
        updated = skills.set_enabled(skill_id, enabled)
        if not enabled:
            registry = runs.build_registry()
            specs = getattr(registry, "all_specs", registry.specs)()
            runs.revoke_pending_approvals_for_tools(
                {spec.name for spec in specs if spec.skill_id == skill_id}
            )
        return updated

    @app.post("/api/skills/{skill_id}/run")  # type: ignore[untyped-decorator]
    def run_skill(skill_id: str, request: ToolInvokeRequest) -> dict[str, object]:
        return tool_invoke_response(runs, f"skill.{skill_id}.run", request)

    def _search_memory(
        query: str,
        layers: list[str] | None = None,
        k: int = 8,
        mode: str = "auto",
        include_inactive: bool = False,
    ) -> list[dict[str, object]]:
        if k < 1 or k > 50:
            raise HTTPException(status_code=400, detail="k must be between 1 and 50")
        if mode not in {"auto", "lex", "vec", "vector", "hybrid"}:
            raise HTTPException(status_code=400, detail="mode must be auto, lex, vector, or hybrid")
        agent = runs.build_runtime_agent(active_config)
        try:
            selected_layers = (
                tuple(MemoryLayer(layer) for layer in layers) if layers else tuple(MemoryLayer)
            )
            hits = agent.memory.retrieve(
                RetrievalQuery(
                    query=query,
                    layers=selected_layers,
                    k_per_layer=k,
                    mode=mode,
                    include_inactive=include_inactive,
                )
            )
            return [
                {
                    "layer": hit.record.layer.value,
                    "kind": hit.record.kind.value,
                    "title": hit.record.title,
                    "score": hit.score,
                    "source_backend": hit.source_backend,
                    "snippet": hit.snippet or hit.record.content[:500],
                    "record_id": hit.record.id,
                }
                for hit in hits[:k]
            ]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            runs.close_runtime_agent(agent)

    @app.get("/api/memory/search")  # type: ignore[untyped-decorator]
    def search_memory_get(
        query: str,
        layers: str | None = None,
        k: int = 8,
        mode: str = "auto",
        include_inactive: bool = False,
    ) -> list[dict[str, object]]:
        return _search_memory(
            query=query, layers=_csv_layers(layers), k=k, mode=mode, include_inactive=include_inactive
        )

    @app.post("/api/memory/search")  # type: ignore[untyped-decorator]
    def search_memory(request: MemorySearchRequest) -> list[dict[str, object]]:
        return _search_memory(
            query=request.query,
            layers=request.layers,
            k=request.k,
            mode=request.mode,
            include_inactive=request.include_inactive,
        )

    @app.get("/api/memory/verify")  # type: ignore[untyped-decorator]
    def verify_memory() -> dict[str, bool]:
        agent = runs.build_runtime_agent(active_config)
        try:
            return {layer.value: ok for layer, ok in agent.memory.verify_all().items()}
        finally:
            runs.close_runtime_agent(agent)

    @app.get("/api/memory/layers")  # type: ignore[untyped-decorator]
    def memory_layers() -> list[dict[str, object]]:
        agent = runs.build_runtime_agent(active_config)
        try:
            verify = agent.memory.verify_all()
            vector_status = agent.memory.vector_index_status()
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
                        "vector": vector_status[layer].to_payload(),
                    }
                )
            return rows
        finally:
            runs.close_runtime_agent(agent)

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

    web_dist = _resolve_web_dist()
    if web_dist is not None:
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

    construction_cleanup.clear()
    return app


def _prepare_private_runtime_artifacts(config: AgentConfig) -> None:
    specs = load_layer_specs(config.layer_config_path) if config.layer_config_path else None
    prepare_private_memory_artifacts(config.memory_dir, specs=specs)
    prepare_private_runs_root(config.memory_dir.parent / "runs")


def _resolve_web_dist() -> Path | None:
    module_path = Path(__file__).resolve()
    candidates = (
        module_path.parent / "web_dist",
        module_path.parents[2] / "web" / "dist",
    )
    return next((candidate for candidate in candidates if (candidate / "index.html").is_file()), None)


def _probe_provider_health(
    *,
    config: AgentConfig,
    secret_resolver: Callable[[str | None], str | None],
) -> dict[str, object]:
    from .llm.factory import build_llm_provider, provider_health_id
    from .llm.resilience import global_provider_health_registry
    from .runtime_models import ChatMessage, LLMOptions

    try:
        provider = build_llm_provider(config, secret_resolver=secret_resolver)
        provider.generate(
            [ChatMessage(role="user", content="Reply exactly KESTREL_PROVIDER_OK.")],
            [],
            LLMOptions(timeout_seconds=min(30, max(1, config.timeout_seconds))),
        )
    except Exception:  # noqa: BLE001
        # The resilient wrapper records a redacted operational failure in the health registry.
        pass
    provider_id = provider_health_id(config)
    snapshot = global_provider_health_registry.snapshot(provider_id)
    return {"provider_id": provider_id, "operational": snapshot.get("state") == "healthy", **snapshot}


def _provider_secret_env_names(config: AgentConfig) -> set[str]:
    names = {value for value in DEFAULT_API_KEY_ENVS.values() if value}
    for value in (config.api_key_env, config.fallback_api_key_env):
        if value:
            names.add(value)
    return names


def _parse_since_window(raw: str | None) -> Any:
    from datetime import UTC, datetime, timedelta

    if raw is None:
        return datetime.now(UTC) - timedelta(days=30)
    value = raw.strip()
    if not value or value.lower() in {"all", "all-time", "all_time"}:
        return None
    now = datetime.now(UTC)
    if value.endswith("d") and value[:-1].isdigit():
        return now - timedelta(days=int(value[:-1]))
    if value.endswith("h") and value[:-1].isdigit():
        return now - timedelta(hours=int(value[:-1]))
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
