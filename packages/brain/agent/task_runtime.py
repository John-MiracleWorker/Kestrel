from __future__ import annotations

"""
Shared task runtime factory for queued and direct task execution.
"""

from dataclasses import dataclass

from core.feature_mode import enabled_bundles_for_mode, parse_feature_mode
from provider_config import ProviderConfig
from providers_registry import get_provider, resolve_provider

from agent.core.memory import WorkingMemory
from agent.core.reflection import ReflectionEngine
from agent.core.verifier import VerifierEngine
from agent.evidence import EvidenceChain
from agent.guardrails import Guardrails
from agent.learner import TaskLearner
from agent.loop import AgentLoop
from agent.model_router import ModelRouter
from agent.simulation import OutcomeSimulator
from agent.task_profiles import TaskProfile, filter_registry_for_profile, infer_task_profile
from agent.tools import build_tool_registry


@dataclass
class TaskRuntimeBundle:
    task_loop: AgentLoop
    provider_name: str
    model: str


async def build_task_runtime_bundle(
    *,
    task,
    runtime_ctx,
    pool,
    event_callback=None,
    model_override: str | None = None,
):
    feature_mode = parse_feature_mode(getattr(runtime_ctx, "feature_mode", "core"))
    existing_profile = getattr(task, "task_profile", "") or ""
    try:
        task_profile = TaskProfile(existing_profile)
    except ValueError:
        task_profile = infer_task_profile(task.goal, feature_mode)
    task.task_profile = task_profile.value

    ws_config = await ProviderConfig(pool).get_config(task.workspace_id)
    provider_name = ws_config.get("provider", "local")
    task_provider = get_provider(provider_name)

    provider_settings = ws_config.get("settings") or {}
    if provider_name in ("ollama", "local") and provider_settings.get("ollama_host"):
        task_provider.set_explicit_url(provider_settings["ollama_host"].rstrip("/"))
    if provider_name == "lmstudio" and provider_settings.get("lmstudio_host"):
        task_provider.set_explicit_url(provider_settings["lmstudio_host"].rstrip("/"))

    task_model = model_override or ws_config.get("model", "")
    task_api_key = ws_config.get("api_key", "")

    task_tool_registry = build_tool_registry(
        hands_client=runtime_ctx.hands_client,
        vector_store=runtime_ctx.vector_store,
        pool=pool,
        runtime_policy=runtime_ctx.execution_runtime,
        enabled_bundles=tuple(
            getattr(runtime_ctx, "enabled_tool_bundles", [])
            or enabled_bundles_for_mode(feature_mode)
        ),
        feature_mode=feature_mode.value,
    )
    task_tool_registry = filter_registry_for_profile(task_tool_registry, task_profile, feature_mode)
    evidence_chain = EvidenceChain(task_id=task.id, pool=pool)

    task_working_memory = WorkingMemory(
        redis_client=None,
        vector_store=runtime_ctx.vector_store,
    )
    task_learner = TaskLearner(
        provider=task_provider,
        model=task_model,
        working_memory=task_working_memory,
    )
    task_reflection = None
    if feature_mode.value != "core":
        task_reflection = ReflectionEngine(
            llm_provider=task_provider,
            model=task_model,
        )

    task_simulator = None
    if feature_mode.value == "labs":
        task_simulator = OutcomeSimulator(
            llm_provider=task_provider,
            model=task_model,
        )
    task_verifier = VerifierEngine(
        provider=task_provider,
        model=task_model,
    )

    def custom_provider_checker(name: str) -> bool:
        if name == provider_name and getattr(task_provider, "is_ready", lambda: False)():
            return True
        try:
            return get_provider(name).is_ready()
        except Exception:
            return False

    task_model_router = ModelRouter(
        provider_checker=custom_provider_checker,
        workspace_provider=provider_name,
        workspace_model=task_model,
    )

    task_loop = AgentLoop(
        provider=task_provider,
        tool_registry=task_tool_registry,
        guardrails=Guardrails(),
        persistence=runtime_ctx.agent_persistence,
        model=task_model,
        api_key=task_api_key,
        learner=task_learner,
        checkpoint_manager=getattr(runtime_ctx, "checkpoint_manager", None),
        memory_graph=runtime_ctx.memory_graph,
        evidence_chain=evidence_chain,
        event_callback=event_callback,
        reflection_engine=task_reflection,
        model_router=task_model_router,
        provider_resolver=resolve_provider,
        simulator=task_simulator,
        verifier=task_verifier,
        persona_learner=runtime_ctx.persona_learner,
    )
    return TaskRuntimeBundle(
        task_loop=task_loop,
        provider_name=provider_name,
        model=task_model,
    )
