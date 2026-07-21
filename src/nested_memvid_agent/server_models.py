from __future__ import annotations

from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

from .routine_limits import (
    MAX_ROUTINE_INTERVAL_SECONDS,
    MAX_ROUTINE_MISFIRE_GRACE_SECONDS,
    MIN_ROUTINE_INTERVAL_SECONDS,
    MIN_ROUTINE_MISFIRE_GRACE_SECONDS,
)

_StrictRoutineRevision = Annotated[int, Field(strict=True, ge=1)]
_StrictRoutineEnabled = Annotated[bool, Field(strict=True)]
_StrictRoutineInterval = Annotated[
    int,
    Field(
        strict=True,
        ge=MIN_ROUTINE_INTERVAL_SECONDS,
        le=MAX_ROUTINE_INTERVAL_SECONDS,
    ),
]
_StrictRoutineMisfireGrace = Annotated[
    int,
    Field(
        strict=True,
        ge=MIN_ROUTINE_MISFIRE_GRACE_SECONDS,
        le=MAX_ROUTINE_MISFIRE_GRACE_SECONDS,
    ),
]
_RoutineIdempotencyKey = Annotated[
    str,
    Field(
        strict=True,
        min_length=16,
        max_length=128,
        pattern=r"^[A-Za-z0-9._:-]+$",
    ),
]


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str
    session_id: str | None = None
    workspace: str | None = None
    provider: str | None = None
    model: str | None = None
    autonomy_mode: str = "background"


class ChannelIngestRequest(BaseModel):
    provider: str
    payload: dict[str, Any] = Field(default_factory=dict)
    channel_id: str | None = None
    send: bool | None = None


class ChannelConfigRequest(BaseModel):
    id: str
    provider: str = "webhook"
    enabled: bool = True
    send_enabled: bool = False
    auto_reply: bool = False
    token_env: str | None = None
    webhook_url_env: str | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class TelegramWebhookRequest(BaseModel):
    url: str = ""
    chat_id: str = ""
    text: str = "Kestrel Telegram channel test."
    drop_pending_updates: bool = False


class ToolInvokeRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    session_id: str = "manual"
    run_id: str | None = None


class CapabilityToggleRequest(BaseModel):
    enabled: bool
    expected_revision: int = Field(ge=0)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    arguments: dict[str, Any] | None = None


class MCPServerRequest(BaseModel):
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
    secret_env: dict[str, str] = Field(default_factory=dict)


class SecretStoreRequest(BaseModel):
    name: str
    value: str
    purpose: str = ""
    id: str | None = None
    validate_now: bool = Field(default=False, alias="validate")


class SubagentRequest(BaseModel):
    run_id: str
    profile: str = "worker"
    goal: str
    task_id: str | None = None


class SchedulerStepRequest(BaseModel):
    max_tasks: int | None = None


class SchedulerRunRequest(BaseModel):
    max_tasks: int | None = None
    max_cycles: int | None = None


class RoutineCreateRequest(BaseModel):
    routine_id: str | None = None
    name: str
    prompt: str
    schedule_kind: str = "interval"
    start_at: str
    interval_seconds: _StrictRoutineInterval | None = None
    workspace: str | None = None
    provider: str | None = None
    model: str | None = None
    autonomy_mode: str = "background"
    misfire_grace_seconds: _StrictRoutineMisfireGrace = 60


class RoutineUpdateRequest(BaseModel):
    expected_revision: _StrictRoutineRevision
    name: str | None = None
    prompt: str | None = None
    schedule_kind: str | None = None
    start_at: str | None = None
    interval_seconds: _StrictRoutineInterval | None = None
    workspace: str | None = None
    provider: str | None = None
    model: str | None = None
    autonomy_mode: str | None = None
    misfire_grace_seconds: _StrictRoutineMisfireGrace | None = None


class RoutineToggleRequest(BaseModel):
    enabled: _StrictRoutineEnabled
    expected_revision: _StrictRoutineRevision


class RoutineRunNowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: _StrictRoutineRevision
    idempotency_key: _RoutineIdempotencyKey


class MemorySearchRequest(BaseModel):
    query: str
    layers: list[str] | None = None
    k: int = 8
    mode: str = "auto"
    include_inactive: bool = False


class MemoryInspectAPIRequest(BaseModel):
    query: str | None = None
    layers: list[str] | None = None
    k: int = 20
    include_inactive: bool = False


class MemoryConsolidateRequest(BaseModel):
    query: str
    source_layer: str | None = None
    validation_evidence: dict[str, Any] | None = None
    validation_score: float = 0.7
    repeat_count: int = 1
    explicit_instruction: bool = False
    dry_run: bool = False


class MemoryLearnRequest(BaseModel):
    title: str
    content: str
    kind: str = "observation"
    source_layer: str = "working"
    target_layer: str | None = None
    confidence: float = 0.6
    importance: float = 0.5
    validation_evidence: dict[str, Any] | None = None
    validation_score: float = 0.7
    repeat_count: int = 1
    explicit_instruction: bool = False
    dry_run: bool = False


class MemoryCorrectRequest(BaseModel):
    target_record_id: str
    correction_text: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    dry_run: bool = False


class MemoryCompactRequest(BaseModel):
    layer: str = "working"
    apply: bool = False


class SelfRememberRequest(BaseModel):
    title: str
    content: str
    schema_: str = Field(alias="schema")
    validation_status: str
    confidence: float = 0.82
    importance: float = 0.72


class SelfOnboardingRequest(BaseModel):
    agent_name: str = "Kestrel"
    user_name: str = ""
    preferred_name: str = ""
    persona: str = "steady"
    working_style: str = ""
    goals: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    communication_notes: str = ""
    continuous_learning: bool = True


class SelfChangeRequest(BaseModel):
    request: str
    rationale: str = ""


class WebSearchRequest(BaseModel):
    query: str
    max_results: int | None = None


class WebFetchRequest(BaseModel):
    url: str
    max_bytes: int | None = None


class ContextPackAPIRequest(BaseModel):
    query: str
    token_budget: int | None = None
    layers: list[str] | None = None
    expand_raw: bool | None = None
    include_telemetry: bool = True


class ContextExpandAPIRequest(BaseModel):
    frame_id: str | None = None
    record_id: str | None = None
    max_tokens: int = 2000
    include_children: bool = False
    include_parents: bool = False


class CapsuleSummarizeAPIRequest(BaseModel):
    dry_run: bool = True


class CapsuleApplyAPIRequest(BaseModel):
    dry_run: bool = False
    include_policy: bool = False


class SkillInstallRequest(BaseModel):
    manifest: dict[str, Any]
    instructions: str
    overwrite: bool = False
    dry_run: bool = False


class PluginInstallRequest(BaseModel):
    source: str
    ref: str | None = None
    enable: bool = False
    overwrite: bool = False


class PluginReviewRequest(BaseModel):
    source: str
    ref: str | None = None


class PluginUpdateRequest(BaseModel):
    ref: str | None = None


class DiagnosisRequest(BaseModel):
    failure_text: str
    source: str | None = None
    k: int = 5
