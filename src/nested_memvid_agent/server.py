import json
import queue
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Any

from .app_factory import build_agent
from .config import AgentConfig
from .event_bus import RunEventBus
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

    app = FastAPI(title="Nested MV2 Agent")

    class CreateRunRequest(BaseModel):  # type: ignore[valid-type,misc]
        message: str
        session_id: str | None = None
        workspace: str | None = None
        model: str | None = None
        autonomy_mode: str = "background"

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

    @app.get("/api/health")  # type: ignore[untyped-decorator]
    def health() -> dict[str, object]:
        return {"ok": True, "name": active_config.name}

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

    @app.post("/api/memory/search")  # type: ignore[untyped-decorator]
    def search_memory(request: MemorySearchRequest) -> list[dict[str, object]]:
        agent = build_agent(active_config, tools=runs.build_registry())
        try:
            layers = tuple(MemoryLayer(layer) for layer in request.layers) if request.layers else tuple(MemoryLayer)
            hits = agent.memory.retrieve(RetrievalQuery(query=request.query, layers=layers, k_per_layer=request.k))
            return [
                {
                    "layer": hit.record.layer.value,
                    "kind": hit.record.kind.value,
                    "title": hit.record.title,
                    "score": hit.score,
                    "snippet": hit.snippet or hit.record.content[:500],
                    "record_id": hit.record.id,
                }
                for hit in hits[: request.k]
            ]
        finally:
            agent.close()

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
