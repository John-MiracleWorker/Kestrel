from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CONTROL_HOST = os.getenv("KESTREL_CONTROL_HOST", "127.0.0.1")
DEFAULT_CONTROL_PORT = int(os.getenv("KESTREL_CONTROL_PORT", "8749"))
CONTROL_STREAM_LIMIT = 1024 * 1024


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _coerce_dict(value: dict[str, Any] | None) -> dict[str, Any]:
    return dict(value or {})


def _coerce_list(value: list[Any] | None) -> list[Any]:
    return list(value or [])


@dataclass(frozen=True)
class LocalOperatorPaths:
    home: Path
    run_dir: Path
    state_dir: Path
    control_socket: Path
    control_host: str
    control_port: int
    heartbeat_state_json: Path
    runtime_profile_json: Path


def resolve_local_operator_paths(home_override: str | None = None) -> LocalOperatorPaths:
    home = Path(home_override or os.getenv("KESTREL_HOME") or "~/.kestrel").expanduser()
    run_dir = home / "run"
    state_dir = home / "state"
    return LocalOperatorPaths(
        home=home,
        run_dir=run_dir,
        state_dir=state_dir,
        control_socket=run_dir / "control.sock",
        control_host=os.getenv("KESTREL_CONTROL_HOST", DEFAULT_CONTROL_HOST),
        control_port=int(os.getenv("KESTREL_CONTROL_PORT", str(DEFAULT_CONTROL_PORT))),
        heartbeat_state_json=state_dir / "heartbeat.json",
        runtime_profile_json=state_dir / "runtime_profile.json",
    )


def _read_json_snapshot(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def read_local_operator_status_snapshot(
    paths: LocalOperatorPaths | None = None,
) -> dict[str, Any]:
    paths = paths or resolve_local_operator_paths()
    snapshot = _read_json_snapshot(paths.heartbeat_state_json)
    runtime_profile = read_local_operator_runtime_profile(paths)
    if runtime_profile and "runtime_profile" not in snapshot:
        snapshot["runtime_profile"] = runtime_profile
    return snapshot


def read_local_operator_runtime_profile(
    paths: LocalOperatorPaths | None = None,
) -> dict[str, Any]:
    paths = paths or resolve_local_operator_paths()
    return _read_json_snapshot(paths.runtime_profile_json)


def local_operator_socket_available(paths: LocalOperatorPaths | None = None) -> bool:
    paths = paths or resolve_local_operator_paths()
    if os.name == "nt":
        try:
            with socket.create_connection((paths.control_host, paths.control_port), timeout=1):
                return True
        except OSError:
            return False
    return paths.control_socket.exists()


class LocalOperatorClientError(RuntimeError):
    pass


async def send_local_operator_stream(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    paths: LocalOperatorPaths | None = None,
    timeout_seconds: float = 30,
):
    paths = paths or resolve_local_operator_paths()
    if os.name != "nt" and not paths.control_socket.exists():
        raise LocalOperatorClientError(f"Control socket not found at {paths.control_socket}")

    request_id = str(uuid.uuid4())
    if os.name == "nt":
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(paths.control_host, paths.control_port, limit=CONTROL_STREAM_LIMIT),
            timeout=timeout_seconds,
        )
    else:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(paths.control_socket), limit=CONTROL_STREAM_LIMIT),
            timeout=timeout_seconds,
        )

    writer.write(
        (
            json.dumps(
                {
                    "request_id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
            + "\n"
        ).encode("utf-8")
    )
    await writer.drain()

    try:
        while True:
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout_seconds)
            if not raw:
                break
            response = json.loads(raw.decode("utf-8"))
            if response.get("request_id") != request_id:
                continue
            if not response.get("ok", False):
                error = response.get("error") or {}
                raise LocalOperatorClientError(error.get("message") or "Unknown local operator control failure")
            yield response
            if response.get("done"):
                break
    finally:
        writer.close()
        await writer.wait_closed()


async def send_local_operator_request(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    paths: LocalOperatorPaths | None = None,
    timeout_seconds: float = 30,
) -> dict[str, Any]:
    async for response in send_local_operator_stream(
        method,
        params=params,
        paths=paths,
        timeout_seconds=timeout_seconds,
    ):
        if "result" in response:
            return response["result"]
    raise LocalOperatorClientError(f"No result received for {method}")


@dataclass(frozen=True)
class AutonomyPolicy:
    mode: str = "suggest_first"
    require_approval_for_mutations: bool = True
    reasoning_escalation: bool = False
    runtime_mode: str = "native"
    local_first: bool = True
    updated_at: str = field(default_factory=_now_iso)

    @classmethod
    def from_config(
        cls,
        config: dict[str, Any] | None,
        *,
        runtime_mode: str = "native",
    ) -> "AutonomyPolicy":
        cfg = dict(config or {})
        agent = cfg.get("agent") or {}
        proactivity = agent.get("proactivity") or {}
        permissions = cfg.get("permissions") or {}
        models = cfg.get("models") or {}
        mode = str(proactivity.get("background_execution") or "suggest_first").strip().lower() or "suggest_first"
        return cls(
            mode=mode,
            require_approval_for_mutations=bool(
                permissions.get("require_approval_for_mutations", True)
            ),
            reasoning_escalation=bool(models.get("reasoning_escalation", False)),
            runtime_mode=str(runtime_mode or "native"),
            local_first=True,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "require_approval_for_mutations": self.require_approval_for_mutations,
            "reasoning_escalation": self.reasoning_escalation,
            "runtime_mode": self.runtime_mode,
            "local_first": self.local_first,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class AgentProfile:
    profile_id: str
    workspace_id: str
    runtime_mode: str
    autonomy_policy: dict[str, Any]
    local_models: dict[str, Any] = field(default_factory=dict)
    media_capabilities: dict[str, Any] = field(default_factory=dict)
    automation_permissions: dict[str, Any] = field(default_factory=dict)
    control_plane: dict[str, Any] = field(default_factory=dict)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "workspace_id": self.workspace_id,
            "runtime_mode": self.runtime_mode,
            "autonomy_policy": _coerce_dict(self.autonomy_policy),
            "local_models": _coerce_dict(self.local_models),
            "media_capabilities": _coerce_dict(self.media_capabilities),
            "automation_permissions": _coerce_dict(self.automation_permissions),
            "control_plane": _coerce_dict(self.control_plane),
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class BackgroundSuggestion:
    id: str
    workspace_id: str
    title: str
    body: str
    goal: str
    source: str
    fingerprint: str
    status: str = "pending"
    notification_type: str = "info"
    task_kind: str = "task"
    auto_start_allowed: bool = False
    task_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    decided_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "body": self.body,
            "goal": self.goal,
            "source": self.source,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "notification_type": self.notification_type,
            "task_kind": self.task_kind,
            "auto_start_allowed": self.auto_start_allowed,
            "task_id": self.task_id,
            "metadata": _coerce_dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True)
class ResearchSession:
    id: str
    workspace_id: str
    task_id: str
    title: str
    prompt: str
    status: str = "queued"
    notebook_path: str = ""
    summary: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    completed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "title": self.title,
            "prompt": self.prompt,
            "status": self.status,
            "notebook_path": self.notebook_path,
            "summary": self.summary,
            "sources": _coerce_list(self.sources),
            "artifacts": _coerce_list(self.artifacts),
            "metadata": _coerce_dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True)
class Procedure:
    id: str
    workspace_id: str
    name: str
    description: str
    trigger_text: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    source_task_id: str = ""
    enabled: bool = True
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description,
            "trigger_text": self.trigger_text,
            "steps": _coerce_list(self.steps),
            "source_task_id": self.source_task_id,
            "enabled": self.enabled,
            "confidence": self.confidence,
            "metadata": _coerce_dict(self.metadata),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ArtifactManifest:
    id: str
    task_id: str
    artifact_type: str
    path: str = ""
    url: str = ""
    mime_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "artifact_type": self.artifact_type,
            "path": self.path,
            "url": self.url,
            "mime_type": self.mime_type,
            "metadata": _coerce_dict(self.metadata),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class VerifierResult:
    ok: bool
    final_response: str
    reason: str
    evidence_count: int = 0
    citations: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "final_response": self.final_response,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
            "citations": _coerce_list(self.citations),
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class LearningEvent:
    id: str
    workspace_id: str
    task_id: str
    event_type: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "summary": self.summary,
            "payload": _coerce_dict(self.payload),
            "created_at": self.created_at,
        }


__all__ = [
    "AgentProfile",
    "ArtifactManifest",
    "AutonomyPolicy",
    "BackgroundSuggestion",
    "CONTROL_STREAM_LIMIT",
    "DEFAULT_CONTROL_HOST",
    "DEFAULT_CONTROL_PORT",
    "LearningEvent",
    "LocalOperatorClientError",
    "LocalOperatorPaths",
    "Procedure",
    "ResearchSession",
    "VerifierResult",
    "control_socket_available",
    "local_operator_socket_available",
    "read_local_operator_runtime_profile",
    "read_local_operator_status_snapshot",
    "resolve_local_operator_paths",
    "send_local_operator_request",
    "send_local_operator_stream",
]

# Backward-compatible alias for callers that expect the CLI naming.
control_socket_available = local_operator_socket_available
