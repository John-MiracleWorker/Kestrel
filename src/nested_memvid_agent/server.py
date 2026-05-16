import json
import queue
from contextlib import asynccontextmanager
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Any

from .app_factory import build_agent
from .channels import ChannelManager, ChannelPayloadError
from .config import AgentConfig
from .event_bus import RunEventBus
from .event_log import JsonlEventLog
from .mcp_manager import MCPManager
from .models import MemoryLayer, RetrievalQuery
from .run_manager import RunManager
from .skill_manager import SkillManager
from .state_store import AgentStateStore


def create_app(config: AgentConfig | None = None) -> Any:
    """Create the local web/API app for the full Nested MV2 Agent."""

    try:
        fastapi_module = import_module("fastapi")
        responses_module = import_module("starlette.responses")
        staticfiles_module = import_module("starlette.staticfiles")
        pydantic_module = import_module("pydantic")
    except ImportError as exc:
        raise RuntimeError("Install server extras with `pip install -e '.[server]'`.") from exc

    FastAPI = fastapi_module.FastAPI
    HTTPException = fastapi_module.HTTPException
    BaseModel = pydantic_module.BaseModel
    Field = pydantic_module.Field
    StreamingResponse = responses_module.StreamingResponse
    FileResponse = responses_module.FileResponse
    StaticFiles = staticfiles_module.StaticFiles

    active_config = config or AgentConfig.from_env()
    state = AgentStateStore(active_config.state_path)
    events = RunEventBus(state)
    mcp = MCPManager(state)
    skills = SkillManager(active_config.skills_dir, state)
    runs = RunManager(config=active_config, state=state, events=events, mcp=mcp, skills=skills)
    channels = ChannelManager(active_config)

    @asynccontextmanager
    async def lifespan(app_instance: Any) -> Any:
        del app_instance
        try:
            yield
        finally:
            mcp.shutdown()

    app = FastAPI(title="Nested MV2 Agent", lifespan=lifespan)

    class CreateRunRequest(BaseModel):  # type: ignore[valid-type,misc]
        message: str
        session_id: str | None = None
        workspace: str | None = None
        model: str | None = None
        autonomy_mode: str = "background"

    class ChannelIngestRequest(BaseModel):  # type: ignore[valid-type,misc]
        provider: str
        payload: dict[str, Any] = Field(default_factory=dict)
        channel_id: str | None = None
        send: bool | None = None

    class ToolInvokeRequest(BaseModel):  # type: ignore[valid-type,misc]
        arguments: dict[str, Any] = Field(default_factory=dict)
        session_id: str = "manual"
        run_id: str | None = None

    class ApprovalDecisionRequest(BaseModel):  # type: ignore[valid-type,misc]
        approved: bool
        arguments: dict[str, Any] | None = None

    class MCPServerRequest(BaseModel):  # type: ignore[valid-type,misc]
        id: str
        name: str | None = None
        transport: str = "stdio"
        command: str | None = None
        args: list[str] = Field(default_factory=list)
        env: dict[str, str] = Field(default_factory=dict)
        url: str | None = None
        enabled: bool = True
        tools: list[dict[str, Any]] = Field(default_factory=list)
        risk_policy: str = "approval_by_default"

    class SubagentRequest(BaseModel):  # type: ignore[valid-type,misc]
        run_id: str
        profile: str = "worker"
        goal: str
        task_id: str | None = None

    class MemorySearchRequest(BaseModel):  # type: ignore[valid-type,misc]
        query: str
        layers: list[str] | None = None
        k: int = 8

    class MemoryConsolidateRequest(BaseModel):  # type: ignore[valid-type,misc]
        query: str
        source_layer: str | None = None
        validation_score: float = 0.7
        repeat_count: int = 1
        explicit_instruction: bool = False
        dry_run: bool = False

    class MemoryLearnRequest(BaseModel):  # type: ignore[valid-type,misc]
        title: str
        content: str
        kind: str = "observation"
        source_layer: str = "working"
        target_layer: str | None = None
        confidence: float = 0.6
        importance: float = 0.5
        validation_score: float = 0.7
        repeat_count: int = 1
        explicit_instruction: bool = False
        dry_run: bool = False

    class ContextPackAPIRequest(BaseModel):  # type: ignore[valid-type,misc]
        query: str
        token_budget: int | None = None
        layers: list[str] | None = None
        expand_raw: bool | None = None
        include_telemetry: bool = True

    class ContextExpandAPIRequest(BaseModel):  # type: ignore[valid-type,misc]
        frame_id: str | None = None
        record_id: str | None = None
        max_tokens: int = 2000
        include_children: bool = False
        include_parents: bool = False

    class CapsuleSummarizeAPIRequest(BaseModel):  # type: ignore[valid-type,misc]
        dry_run: bool = True

    class CapsuleApplyAPIRequest(BaseModel):  # type: ignore[valid-type,misc]
        dry_run: bool = False
        include_policy: bool = False

    @app.get("/api/health")  # type: ignore[untyped-decorator]
    def health() -> dict[str, object]:
        return {"ok": True, "name": active_config.name}

    @app.get("/api/channels")  # type: ignore[untyped-decorator]
    def list_channels() -> list[dict[str, object]]:
        return channels.list_channels()

    @app.post("/api/channels/ingest")  # type: ignore[untyped-decorator]
    def ingest_channel(request: ChannelIngestRequest) -> dict[str, object]:
        try:
            return channels.handle_payload(
                provider=request.provider,
                channel_id=request.channel_id,
                payload=request.payload,
                send=request.send,
            ).to_public_dict()
        except ChannelPayloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/channels/{provider}/webhook")  # type: ignore[untyped-decorator]
    def channel_webhook(
        provider: str,
        payload: dict[str, Any],
        channel_id: str | None = None,
        send: bool | None = None,
    ) -> dict[str, object]:
        try:
            return channels.handle_payload(
                provider=provider,
                channel_id=channel_id,
                payload=payload,
                send=send,
            ).to_public_dict()
        except ChannelPayloadError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/runs")  # type: ignore[untyped-decorator]
    def create_run(request: CreateRunRequest) -> dict[str, object]:
        run = runs.create_run(
            message=request.message,
            session_id=request.session_id,
            workspace=Path(request.workspace) if request.workspace else None,
            model=request.model,
        )
        return asdict(run)

    @app.get("/api/runs")  # type: ignore[untyped-decorator]
    def list_runs() -> list[dict[str, object]]:
        return runs.list_runs()

    @app.get("/api/sessions")  # type: ignore[untyped-decorator]
    def list_sessions() -> list[dict[str, object]]:
        return runs.list_sessions()

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

    @app.get("/api/mcp/servers")  # type: ignore[untyped-decorator]
    def list_mcp_servers() -> list[dict[str, object]]:
        return mcp.list_servers()

    @app.post("/api/mcp/servers")  # type: ignore[untyped-decorator]
    def add_mcp_server(request: MCPServerRequest) -> dict[str, object]:
        return mcp.add_server(request.model_dump())

    @app.delete("/api/mcp/servers/{server_id}")  # type: ignore[untyped-decorator]
    def delete_mcp_server(server_id: str) -> dict[str, bool]:
        mcp.delete_server(server_id)
        return {"ok": True}

    @app.post("/api/mcp/servers/{server_id}/connect")  # type: ignore[untyped-decorator]
    def connect_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp.connect_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/disconnect")  # type: ignore[untyped-decorator]
    def disconnect_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp.disconnect_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/restart")  # type: ignore[untyped-decorator]
    def restart_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp.restart_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/mcp/servers/{server_id}/health")  # type: ignore[untyped-decorator]
    def mcp_server_health(server_id: str) -> dict[str, object]:
        try:
            return mcp.server_health(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/sync")  # type: ignore[untyped-decorator]
    def sync_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp.sync_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/test")  # type: ignore[untyped-decorator]
    def test_mcp_server(server_id: str) -> dict[str, object]:
        try:
            return mcp.test_server(server_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/api/mcp/servers/{server_id}/tools/{tool_name}/invoke")  # type: ignore[untyped-decorator]
    def invoke_mcp_tool(server_id: str, tool_name: str, request: ToolInvokeRequest) -> dict[str, object]:
        try:
            execution = mcp.invoke_tool(server_id, tool_name, request.arguments)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {
            "tool": execution.call.name,
            "tool_call_id": execution.call.id,
            "success": execution.success,
            "content": execution.content,
            "data": execution.data,
            "error": execution.error,
        }

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

    @app.post("/api/skills/discover")  # type: ignore[untyped-decorator]
    def discover_skills() -> list[dict[str, object]]:
        return skills.discover()

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

    def _search_memory(query: str, layers: list[str] | None = None, k: int = 8) -> list[dict[str, object]]:
        if k < 1 or k > 50:
            raise HTTPException(status_code=400, detail="k must be between 1 and 50")
        agent = build_agent(active_config, tools=runs.build_registry())
        try:
            selected_layers = tuple(MemoryLayer(layer) for layer in layers) if layers else tuple(MemoryLayer)
            hits = agent.memory.retrieve(
                RetrievalQuery(query=query, layers=selected_layers, k_per_layer=k)
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
    def search_memory_get(query: str, layers: str | None = None, k: int = 8) -> list[dict[str, object]]:
        return _search_memory(query=query, layers=_csv_layers(layers), k=k)

    @app.post("/api/memory/search")  # type: ignore[untyped-decorator]
    def search_memory(request: MemorySearchRequest) -> list[dict[str, object]]:
        return _search_memory(query=request.query, layers=request.layers, k=request.k)

    @app.get("/api/memory/verify")  # type: ignore[untyped-decorator]
    def verify_memory() -> dict[str, bool]:
        agent = build_agent(active_config, tools=runs.build_registry())
        try:
            return {layer.value: ok for layer, ok in agent.memory.verify_all().items()}
        finally:
            agent.close()

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
            del path
            return FileResponse(web_dist / "index.html")

    return app


def _csv_layers(value: str | None) -> list[str] | None:
    if value is None or not value.strip():
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _bounded_limit(value: int, *, default: int, maximum: int) -> int:
    if value < 1:
        return default
    return min(value, maximum)


def _tool_response_payload(execution: Any) -> dict[str, object]:
    if execution.content.startswith("{"):
        payload = json.loads(execution.content)
        if isinstance(payload, dict):
            return dict(payload)
    return {"success": execution.success, "content": execution.content, "error": execution.error}
