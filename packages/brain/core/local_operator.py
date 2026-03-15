from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_SHARED_PATH = Path(__file__).resolve().parents[2] / "shared"
if str(_SHARED_PATH) not in sys.path:
    sys.path.append(str(_SHARED_PATH))

_local_operator = importlib.import_module("local_operator")

AgentProfile = _local_operator.AgentProfile
ArtifactManifest = _local_operator.ArtifactManifest
AutonomyPolicy = _local_operator.AutonomyPolicy
BackgroundSuggestion = _local_operator.BackgroundSuggestion
LearningEvent = _local_operator.LearningEvent
LocalOperatorClientError = _local_operator.LocalOperatorClientError
LocalOperatorPaths = _local_operator.LocalOperatorPaths
Procedure = _local_operator.Procedure
ResearchSession = _local_operator.ResearchSession
VerifierResult = _local_operator.VerifierResult
control_socket_available = _local_operator.control_socket_available
read_local_operator_runtime_profile = _local_operator.read_local_operator_runtime_profile
read_local_operator_status_snapshot = _local_operator.read_local_operator_status_snapshot
resolve_local_operator_paths = _local_operator.resolve_local_operator_paths
send_local_operator_request = _local_operator.send_local_operator_request
send_local_operator_stream = _local_operator.send_local_operator_stream


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _coerce_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def derive_brain_autonomy_policy(
    runtime_profile: dict[str, Any] | None,
    *,
    fallback: str = "moderate",
) -> str:
    profile = _coerce_dict(runtime_profile)
    autonomy = _coerce_dict(profile.get("autonomy_policy"))
    mode = str(autonomy.get("mode") or "").strip().lower()
    require_approval = bool(autonomy.get("require_approval_for_mutations", True))
    if mode == "notify_only":
        return "conservative"
    if mode == "auto_start_safe" and not require_approval:
        return "full"
    if mode in {"suggest_first", "auto_start_safe"}:
        return "moderate"
    return fallback


def derive_workspace_agent_profile_sync(
    *,
    fallback_autonomy_policy: str = "moderate",
    runtime_profile: dict[str, Any] | None = None,
    status_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = _coerce_dict(runtime_profile) or read_local_operator_runtime_profile()
    status = _coerce_dict(status_snapshot) or read_local_operator_status_snapshot()
    autonomy = _coerce_dict(profile.get("autonomy_policy"))
    control_plane = _coerce_dict(profile.get("control_plane"))
    if not control_plane:
        control_plane = _coerce_dict(_coerce_dict(status.get("runtime_profile")).get("control_plane"))

    brain_autonomy = derive_brain_autonomy_policy(profile, fallback=fallback_autonomy_policy)
    runtime_defaults: dict[str, Any] = {}
    kernel_policy_json: dict[str, Any] = {}

    if profile or status:
        runtime_defaults = {
            "local_operator": {
                "connected": bool(profile),
                "brain_autonomy_policy": brain_autonomy,
                "runtime_profile": profile,
                "status_snapshot": status,
                "agent_profile": _coerce_dict(profile.get("agent_profile")),
                "control_plane": control_plane,
                "autonomy_policy": autonomy,
                "local_models": _coerce_dict(profile.get("local_models")),
                "media_capabilities": _coerce_dict(profile.get("media_capabilities")),
                "automation_permissions": _coerce_dict(profile.get("automation_permissions")),
                "updated_at": str(profile.get("updated_at") or ""),
            }
        }
        kernel_policy_json = {
            "routing_strategy": "local_first" if autonomy.get("local_first", True) else "",
            "background_execution": str(autonomy.get("mode") or ""),
            "reasoning_escalation": bool(autonomy.get("reasoning_escalation", False)),
            "require_approval_for_mutations": bool(autonomy.get("require_approval_for_mutations", True)),
            "control_plane": control_plane,
            "preferred_categories": _coerce_list(
                _coerce_dict(profile.get("agent_profile")).get("preferred_categories")
            ),
        }

    return {
        "autonomy_policy": brain_autonomy,
        "runtime_defaults": runtime_defaults,
        "kernel_policy_json": kernel_policy_json,
    }

__all__ = [
    "AgentProfile",
    "ArtifactManifest",
    "AutonomyPolicy",
    "BackgroundSuggestion",
    "LearningEvent",
    "LocalOperatorClientError",
    "LocalOperatorPaths",
    "Procedure",
    "ResearchSession",
    "VerifierResult",
    "control_socket_available",
    "derive_brain_autonomy_policy",
    "derive_workspace_agent_profile_sync",
    "read_local_operator_runtime_profile",
    "read_local_operator_status_snapshot",
    "resolve_local_operator_paths",
    "send_local_operator_request",
    "send_local_operator_stream",
]
