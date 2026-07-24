from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .models import ModelTarget, ProviderProfile, RoutePolicy


class RoutingRevisionConflict(RuntimeError):
    def __init__(self, resource: str, resource_id: str, current_revision: int) -> None:
        self.resource = resource
        self.resource_id = resource_id
        self.current_revision = current_revision
        super().__init__(f"{resource}_revision_conflict:{resource_id}:{current_revision}")


@dataclass(frozen=True)
class ProviderProfileEntry:
    profile: ProviderProfile
    revision: int
    created_at: str
    updated_at: str

    def to_public_payload(self) -> dict[str, Any]:
        return {
            **self.profile.to_public_payload(),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ModelTargetEntry:
    target: ModelTarget
    revision: int
    created_at: str
    updated_at: str

    def to_public_payload(self) -> dict[str, Any]:
        return {
            **self.target.to_public_payload(),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RoutePolicyEntry:
    policy: RoutePolicy
    enabled: bool
    revision: int
    created_at: str
    updated_at: str

    def to_public_payload(self) -> dict[str, Any]:
        return {
            **asdict(self.policy),
            "enabled": self.enabled,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class RouteDecisionEntry:
    decision_id: str
    run_id: str
    task_id: str
    subagent_id: str | None
    attempt: int
    status: str
    mode: str
    policy_id: str
    policy_revision: int
    contract_digest: str
    selected_target_id: str
    selected_target_revision: int
    selected_profile_id: str
    selected_profile_revision: int
    selected_provider: str
    selected_model: str
    selection_kind: str
    score: float
    predicted_success: float | None
    estimated_cost_usd: float | None
    reason_codes: tuple[str, ...]
    candidate_snapshot: tuple[dict[str, Any], ...]
    actionable: bool
    router_version: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        payload["candidate_snapshot"] = [dict(item) for item in self.candidate_snapshot]
        return payload


@dataclass(frozen=True)
class RouteOutcomeEntry:
    outcome_id: str
    decision_id: str
    run_id: str
    task_id: str
    subagent_id: str | None
    attempt: int
    execution_status: str
    validation_passed: bool
    validation_codes: tuple[str, ...]
    failure_category: str | None
    provider_failure_code: str | None
    latency_seconds: float | None
    input_tokens: int | None
    output_tokens: int | None
    actual_cost_usd: float | None
    tool_count: int
    changed_file_count: int | None
    retry_count: int
    escalated: bool
    reward_components: dict[str, float]
    outcome_labels: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    created_at: str

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["validation_codes"] = list(self.validation_codes)
        payload["outcome_labels"] = list(self.outcome_labels)
        payload["evidence_refs"] = list(self.evidence_refs)
        return payload
