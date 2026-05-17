import json
import os
import queue
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib import import_module
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

from .app_factory import build_agent
from .channels import ChannelManager, ChannelPayloadError
from .config import AgentConfig
from .event_bus import RunEventBus
from .event_log import JsonlEventLog
from .mcp_manager import MCPManager
from .models import MemoryLayer, RetrievalQuery
from .orchestrator import build_memory_system
from .plugin_manager import PluginError, PluginManager
from .run_manager import RunManager
from .secret_broker import build_secret_broker
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
        from .server_mcp_routes import register_mcp_routes
        from .server_models import (
            ApprovalDecisionRequest,
            CapsuleApplyAPIRequest,
            CapsuleSummarizeAPIRequest,
            ChannelConfigRequest,
            ChannelIngestRequest,
            ContextExpandAPIRequest,
            ContextPackAPIRequest,
            CreateRunRequest,
            DiagnosisRequest,
            MemoryCompactRequest,
            MemoryConsolidateRequest,
            MemoryCorrectRequest,
            MemoryInspectAPIRequest,
            MemoryLearnRequest,
            MemorySearchRequest,
            PluginInstallRequest,
            PluginUpdateRequest,
            SchedulerRunRequest,
            SchedulerStepRequest,
            SecretStoreRequest,
            SelfChangeRequest,
            SelfRememberRequest,
            SkillInstallRequest,
            SubagentRequest,
            ToolInvokeRequest,
            WebFetchRequest,
            WebSearchRequest,
        )
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

    active_config = config or AgentConfig.from_env()
    secret_broker = build_secret_broker(active_config.secret_store_path, backend=active_config.secret_backend)
    state = AgentStateStore(active_config.state_path)
    events = RunEventBus(state)
    mcp = MCPManager(state, allow_network_endpoints=active_config.allow_mcp_network_endpoints, secret_resolver=secret_broker.resolve)
    skills = SkillManager(active_config.skills_dir, state)
    plugins = PluginManager(active_config.plugins_dir, state)
    runs = RunManager(config=active_config, state=state, events=events, mcp=mcp, skills=skills, plugins=plugins)
    channels = ChannelManager(active_config, secret_resolver=secret_broker.resolve)
    secret_broker.register_allowed_env_names(_known_secret_env_names(channels.list_channels(), mcp.list_servers()))

    def require_api_auth(
        authorization: str | None = Header(default=None),
        x_kestrel_api_key: str | None = Header(default=None),
    ) -> bool:
        auth_error = _api_auth_error(active_config, {"authorization": authorization or "", "x-kestrel-api-key": x_kestrel_api_key or ""})
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

    async def _request_body(request: object) -> bytes:
        body = getattr(request, "body", None)
        if not callable(body):
            raise ValueError("request body is unavailable")
        raw = await body()
        return raw if isinstance(raw, bytes) else bytes(raw)

    def parse_json_body(raw: bytes) -> dict[str, Any]:
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object.")
        return parsed

    def inspect_memory_payload(*, query: str | None, layers: list[str] | None, k: int, include_inactive: bool = False) -> dict[str, object]:
        arguments: dict[str, object] = {"query": query.strip() if query and query.strip() else "memory", "k": _bounded_limit(k, default=20, maximum=100)}
        if layers:
            arguments["layers"] = layers
        if include_inactive:
            arguments["include_inactive"] = True
        execution = runs.invoke_tool(tool_name="memory.inspect", arguments=arguments, session_id="api")
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

    def channel_public(channel: dict[str, Any]) -> dict[str, object]:
        settings = channel.get("settings")
        safe = dict(channel)
        safe["settings"] = dict(settings) if isinstance(settings, dict) else {}
        token_env = str(safe.get("token_env") or "")
        webhook_env = str(safe.get("webhook_url_env") or "")
        signature_env = ""
        if isinstance(settings, dict):
            signature_env = str(settings.get("signature_secret_env") or "")
        safe["env_status"] = {
            "token_env_configured": bool(token_env and secret_broker.resolve(token_env)),
            "token_env_status": secret_broker.status(token_env) if token_env else {"configured": False},
            "webhook_url_env_configured": bool(webhook_env and secret_broker.resolve(webhook_env)),
            "webhook_url_env_status": secret_broker.status(webhook_env) if webhook_env else {"configured": False},
            "signature_secret_env": signature_env or None,
            "signature_secret_env_configured": bool(signature_env and secret_broker.resolve(signature_env)),
            "signature_secret_env_status": secret_broker.status(signature_env) if signature_env else {"configured": False},
        }
        return safe

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
        if "0.0.0.0" not in trusted_hosts and "*" not in trusted_hosts and host not in trusted_hosts:  # nosec
            return responses_module.JSONResponse({"detail": "untrusted_host"}, status_code=400)
        origin = str(headers.get("origin", "")).strip()
        if origin:
            origin_host = _hostname_from_url(origin)
            if origin_host and "0.0.0.0" not in trusted_hosts and "*" not in trusted_hosts and origin_host not in trusted_hosts:  # nosec
                return responses_module.JSONResponse({"detail": "untrusted_origin"}, status_code=403)
        path = str(getattr(getattr(request, "url", None), "path", ""))
        if path == "/api" or path.startswith("/api/"):
            auth_error = _api_auth_error(active_config, headers)
            if auth_error is not None:
                status_code, detail = auth_error
                return responses_module.JSONResponse({"detail": detail}, status_code=status_code)
        return await call_next(request)

    @app.get("/api/health")  # type: ignore[untyped-decorator]
    def health() -> dict[str, object]:
        return {"ok": True, "name": active_config.name}

    @app.get("/api/runtime/config")  # type: ignore[untyped-decorator]
    def runtime_config() -> dict[str, object]:
        try:
            package_version = importlib_metadata.version("nested-memvid-agent")
        except importlib_metadata.PackageNotFoundError:
            package_version = None
        provider_env = active_config.api_key_env
        fallback_env = active_config.fallback_api_key_env
        return {
            "name": active_config.name,
            "version": package_version,
            "schema_version": state.schema_version(),
            "provider": {
                "name": active_config.provider,
                "model": active_config.model,
                "base_url_configured": bool(active_config.base_url),
                "api_key_env": provider_env,
                "api_key_configured": bool(provider_env and os.getenv(provider_env)),
                "fallback_provider": active_config.fallback_provider,
                "fallback_model": active_config.fallback_model,
                "fallback_base_url_configured": bool(active_config.fallback_base_url),
                "fallback_api_key_env": fallback_env,
                "fallback_api_key_configured": bool(fallback_env and os.getenv(fallback_env)),
                "stream": active_config.stream,
                "timeout_seconds": active_config.timeout_seconds,
                "max_retries": active_config.max_retries,
            },
            "feature_flags": {
                "allow_shell": active_config.allow_shell,
                "allow_file_write": active_config.allow_file_write,
                "allow_policy_writes": active_config.allow_policy_writes,
                "allow_codex_cli": active_config.allow_codex_cli,
                "allow_plugin_install": active_config.allow_plugin_install,
                "allow_git_commit": active_config.allow_git_commit,
                "allow_git_push": active_config.allow_git_push,
                "allow_remote_mutation": active_config.allow_remote_mutation,
                "allow_memory_import": active_config.allow_memory_import,
                "allow_executable_skills": active_config.allow_executable_skills,
                "allow_mcp_network_endpoints": active_config.allow_mcp_network_endpoints,
                "allow_web": active_config.allow_web,
                "allow_self_modification": active_config.allow_self_modification,
                "require_approval_for_high_risk_tools": active_config.require_approval_for_high_risk_tools,
                "enable_agentic_cycle": active_config.enable_agentic_cycle,
                "enable_autonomous_scheduler": active_config.enable_autonomous_scheduler,
                "enable_worker_isolation": active_config.enable_worker_isolation,
                "enable_task_capsules": active_config.enable_task_capsules,
                "enable_auto_consolidation": active_config.enable_auto_consolidation,
                "auto_consolidation_dry_run": active_config.auto_consolidation_dry_run,
                "enable_channel_delivery": active_config.enable_channel_delivery,
                "require_api_auth": active_config.require_api_auth,
            },
            "git_safety": {
                "git_write_mode": active_config.git_write_mode,
                "protected_branches": list(active_config.protected_branches),
            },
            "limits": {
                "max_tool_rounds": active_config.max_tool_rounds,
                "context_budget_chars": active_config.context_budget_chars,
                "context_pack_token_budget": active_config.context_pack_token_budget,
                "max_scheduler_tasks": active_config.max_scheduler_tasks,
                "max_scheduler_cycles": active_config.max_scheduler_cycles,
                "tool_timeout_seconds": active_config.tool_timeout_seconds,
                "web_timeout_seconds": active_config.web_timeout_seconds,
                "web_max_results": active_config.web_max_results,
                "web_max_bytes": active_config.web_max_bytes,
                "web_backend": active_config.web_backend,
                "secret_backend": active_config.secret_backend,
            },
            "paths": {
                "workspace": str(active_config.workspace),
                "memory_dir": str(active_config.memory_dir),
                "state_path": str(active_config.state_path),
                "log_dir": str(active_config.log_dir),
                "skills_dir": str(active_config.skills_dir),
                "plugins_dir": str(active_config.plugins_dir),
                "mcp_config_path": str(active_config.mcp_config_path),
                "channel_config_path": str(active_config.channel_config_path),
                "worker_worktree_dir": str(active_config.worker_worktree_dir),
                "secret_store_path": str(active_config.secret_store_path),
            },
            "validation_commands": [
                "python -m compileall -q src tests scripts",
                "python -m pytest -q",
                "python scripts/run_golden_evals.py --backend memory --provider mock",
                'PYTHONPATH=src python -m nested_memvid_agent.cli chat --backend memory --provider mock --message "hello"',
                "npm run test --prefix web",
                "npm run build --prefix web",
            ],
        }

    @app.get("/api/secrets")  # type: ignore[untyped-decorator]
    def list_secrets() -> list[dict[str, object]]:
        return secret_broker.list_secrets()

    @app.get("/api/secrets/{secret_id}")  # type: ignore[untyped-decorator]
    def get_secret(secret_id: str) -> dict[str, object]:
        try:
            return secret_broker.get_secret(secret_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="secret_not_found") from exc

    @app.post("/api/secrets")  # type: ignore[untyped-decorator]
    def store_secret(request: SecretStoreRequest) -> dict[str, object]:
        try:
            return secret_broker.store_secret(
                name=request.name,
                purpose=request.purpose,
                value=request.value,
                secret_id=request.id,
                validate=request.validate_now,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/secrets/{secret_id}/validate")  # type: ignore[untyped-decorator]
    def validate_secret(secret_id: str) -> dict[str, object]:
        try:
            return secret_broker.validate_secret(secret_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="secret_not_found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/secrets/{secret_id}")  # type: ignore[untyped-decorator]
    def delete_secret(secret_id: str) -> dict[str, bool]:
        try:
            secret_broker.delete_secret(secret_id)
            return {"ok": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="secret_not_found") from exc

    @app.get("/api/channels")  # type: ignore[untyped-decorator]
    def list_channels() -> list[dict[str, object]]:
        return [channel_public(channel) for channel in channels.list_channels()]

    @app.get("/api/channels/{channel_id}")  # type: ignore[untyped-decorator]
    def get_channel(channel_id: str) -> dict[str, object]:
        try:
            return channel_public(channels.get_channel(channel_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/channels")  # type: ignore[untyped-decorator]
    def upsert_channel(request: ChannelConfigRequest) -> dict[str, object]:
        channel = channels.upsert_channel(request.model_dump())
        secret_broker.register_allowed_env_names(_known_secret_env_names([channel], mcp.list_servers()))
        return channel_public(channel)

    @app.put("/api/channels/{channel_id}")  # type: ignore[untyped-decorator]
    def update_channel(channel_id: str, request: ChannelConfigRequest) -> dict[str, object]:
        payload = request.model_dump()
        payload["id"] = channel_id
        channel = channels.upsert_channel(payload)
        secret_broker.register_allowed_env_names(_known_secret_env_names([channel], mcp.list_servers()))
        return channel_public(channel)

    @app.delete("/api/channels/{channel_id}")  # type: ignore[untyped-decorator]
    def delete_channel(channel_id: str) -> dict[str, bool]:
        try:
            channels.delete_channel(channel_id)
            return {"ok": True}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/channels/ingest")  # type: ignore[untyped-decorator]
    async def ingest_channel(http_request: Request) -> dict[str, object]:  # type: ignore[valid-type]
        try:
            raw = await _request_body(http_request)
            body = parse_json_body(raw)
            request = ChannelIngestRequest(**body)
            return channels.handle_payload(
                provider=request.provider,
                channel_id=request.channel_id,
                payload=request.payload,
                raw_body=raw,
                send=request.send,
                headers=request_headers(http_request),
            ).to_public_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ChannelPayloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/channels/{provider}/webhook")  # type: ignore[untyped-decorator]
    async def channel_webhook(
        provider: str,
        request: Request,  # type: ignore[valid-type]
        channel_id: str | None = None,
        send: bool | None = None,
    ) -> dict[str, object]:
        try:
            raw = await _request_body(request)
            payload = parse_json_body(raw)
            return channels.handle_payload(
                provider=provider,
                channel_id=channel_id,
                payload=payload,
                raw_body=raw,
                send=send,
                headers=request_headers(request),
                require_signature=True,
            ).to_public_dict()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ChannelPayloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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
            return runs.run_scheduler_until_idle(run_id, max_tasks=request.max_tasks, max_cycles=request.max_cycles)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/runs/{run_id}/events")  # type: ignore[untyped-decorator]
    def run_events(run_id: str, after_id: int = 0) -> Any:
        try:
            state.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        def stream() -> Any:
            subscriber = events.subscribe(run_id, after_id=after_id)
            try:
                while True:
                    try:
                        event = subscriber.get(timeout=15)
                        yield event.to_sse()
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                events.unsubscribe(run_id, subscriber)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/runs/{run_id}/trace")  # type: ignore[untyped-decorator]
    def run_trace(run_id: str, limit: int = 1000) -> dict[str, object]:
        try:
            return runs.run_trace(run_id, limit=_bounded_limit(limit, default=1000, maximum=5000))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/logs")  # type: ignore[untyped-decorator]
    def logs(limit: int = 100) -> list[dict[str, object]]:
        event_log = JsonlEventLog(active_config.log_dir / "events.jsonl")
        return [asdict(event) for event in event_log.tail(limit=_bounded_limit(limit, default=100, maximum=500))]

    @app.get("/api/tools")  # type: ignore[untyped-decorator]
    def list_tools() -> list[dict[str, object]]:
        return [spec.to_public_dict() for spec in runs.build_registry().specs()]

    @app.post("/api/tools/{tool_name}/invoke")  # type: ignore[untyped-decorator]
    def invoke_tool(tool_name: str, request: ToolInvokeRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name=tool_name,
            arguments=request.arguments,
            session_id=request.session_id,
            run_id=request.run_id,
        )
        return {
            "tool": execution.call.name,
            "tool_call_id": execution.call.id,
            "success": execution.success,
            "content": execution.content,
            "data": execution.data,
            "error": execution.error,
        }

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

    @app.post("/api/web/search")  # type: ignore[untyped-decorator]
    def search_web(request: WebSearchRequest) -> dict[str, object]:
        arguments: dict[str, object] = {"query": request.query}
        if request.max_results is not None:
            arguments["max_results"] = request.max_results
        execution = runs.invoke_tool(tool_name="web.search", arguments=arguments, session_id="api")
        return _execution_response(execution)

    @app.post("/api/web/fetch")  # type: ignore[untyped-decorator]
    def fetch_web(request: WebFetchRequest) -> dict[str, object]:
        arguments: dict[str, object] = {"url": request.url}
        if request.max_bytes is not None:
            arguments["max_bytes"] = request.max_bytes
        execution = runs.invoke_tool(tool_name="web.fetch", arguments=arguments, session_id="api")
        return _execution_response(execution)

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

    @app.post("/api/plugins/{plugin_id}/disable")  # type: ignore[untyped-decorator]
    def disable_plugin(plugin_id: str) -> dict[str, object]:
        try:
            plugin = plugins.set_enabled(plugin_id, False)
            audit_plugin("disable", plugin)
            return plugin
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/plugins/{plugin_id}/update")  # type: ignore[untyped-decorator]
    def update_plugin(plugin_id: str, request: PluginUpdateRequest | None = None) -> dict[str, object]:
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
    def discover_skills() -> list[dict[str, object]]:
        return skills.discover()

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
        result: dict[str, object] = invoke_tool(f"skill.{skill_id}.run", request)
        return result

    def _search_memory(
        query: str,
        layers: list[str] | None = None,
        k: int = 8,
        include_inactive: bool = False,
    ) -> list[dict[str, object]]:
        if k < 1 or k > 50:
            raise HTTPException(status_code=400, detail="k must be between 1 and 50")
        agent = build_agent(active_config, tools=runs.build_registry())
        try:
            selected_layers = tuple(MemoryLayer(layer) for layer in layers) if layers else tuple(MemoryLayer)
            hits = agent.memory.retrieve(
                RetrievalQuery(query=query, layers=selected_layers, k_per_layer=k, include_inactive=include_inactive)
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
    def search_memory_get(query: str, layers: str | None = None, k: int = 8, include_inactive: bool = False) -> list[dict[str, object]]:
        return _search_memory(query=query, layers=_csv_layers(layers), k=k, include_inactive=include_inactive)

    @app.post("/api/memory/search")  # type: ignore[untyped-decorator]
    def search_memory(request: MemorySearchRequest) -> list[dict[str, object]]:
        return _search_memory(query=request.query, layers=request.layers, k=request.k, include_inactive=request.include_inactive)

    @app.get("/api/memory/verify")  # type: ignore[untyped-decorator]
    def verify_memory() -> dict[str, bool]:
        agent = build_agent(active_config, tools=runs.build_registry())
        try:
            return {layer.value: ok for layer, ok in agent.memory.verify_all().items()}
        finally:
            agent.close()

    @app.get("/api/memory/layers")  # type: ignore[untyped-decorator]
    def memory_layers() -> list[dict[str, object]]:
        agent = build_agent(active_config, tools=runs.build_registry())
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
    def inspect_memory_get(query: str | None = None, layers: str | None = None, k: int = 20, include_inactive: bool = False) -> dict[str, object]:
        return inspect_memory_payload(query=query, layers=_csv_layers(layers), k=k, include_inactive=include_inactive)

    @app.post("/api/memory/inspect")  # type: ignore[untyped-decorator]
    def inspect_memory(request: MemoryInspectAPIRequest) -> dict[str, object]:
        return inspect_memory_payload(query=request.query, layers=request.layers, k=request.k, include_inactive=request.include_inactive)

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
        return {"success": execution.success, "content": execution.content, "error": execution.error}

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
        return {"success": execution.success, "content": execution.content, "error": execution.error}

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
        return {"success": execution.success, "content": execution.content, "error": execution.error}

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
        return {"success": execution.success, "content": execution.content, "error": execution.error}

    @app.post("/api/context/pack")  # type: ignore[untyped-decorator]
    def pack_context(request: ContextPackAPIRequest) -> dict[str, object]:
        arguments = request.model_dump(exclude_none=True)
        execution = runs.invoke_tool(tool_name="context.pack", arguments=arguments, session_id="api")
        return _tool_response_payload(execution)

    @app.post("/api/context/expand")  # type: ignore[untyped-decorator]
    def expand_context(request: ContextExpandAPIRequest) -> dict[str, object]:
        arguments = request.model_dump(exclude_none=True)
        execution = runs.invoke_tool(tool_name="context.expand", arguments=arguments, session_id="api")
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
        execution = runs.invoke_tool(tool_name="context.pack", arguments=arguments, session_id="api")
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
        execution = runs.invoke_tool(tool_name="memory.conflicts", arguments=arguments, session_id="api")
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

    @app.post("/api/diagnosis/classify")  # type: ignore[untyped-decorator]
    def classify_diagnosis(request: DiagnosisRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="diagnosis.classify",
            arguments={"failure_text": request.failure_text, "source": request.source or "api"},
            session_id="api",
        )
        return _tool_response_payload(execution)

    @app.post("/api/diagnosis/recall")  # type: ignore[untyped-decorator]
    def recall_diagnosis(request: DiagnosisRequest) -> dict[str, object]:
        execution = runs.invoke_tool(
            tool_name="diagnosis.recall",
            arguments={"failure_text": request.failure_text, "source": request.source or "api", "k": request.k},
            session_id="api",
        )
        return _tool_response_payload(execution)

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
