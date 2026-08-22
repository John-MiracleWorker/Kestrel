"""S4 / JOURNEY-004 — live reviewer separation in the graph runtime.

The truthful-routing-modes half (PR #350) made the *resolver* produce a durable
review-authority label.  This file proves the second half: the LIVE graph
runtime's reviewer (``evaluate_turn_review`` and ``ReviewerNode``) actually
routes the reviewer through the resolver's label instead of the pre-S4
``_provider_review_enabled`` boolean.

Three authority outcomes are pinned at the live-runtime boundary:

  * ``independent_target``  — the reviewer role is satisfied by routing the
    provider review through a SEPARATE reviewer agent (a distinct qualified
    target / model family), never the executor's own agent.
  * ``deterministic_fallback`` — the resolver found no independent reviewer
    target; the deterministic evidence gate is the review authority and the
    rejection reason codes are preserved.
  * ``off_mode_abstained`` — routing mode is ``off``; the reviewer is not
    routed and the abstention is durably labeled, never mis-claimed as a
    fallback.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.graph_runtime import evaluate_turn_review
from nested_memvid_agent.llm.base import ProviderCapabilities
from nested_memvid_agent.llm.mock import MockLLMProvider
from nested_memvid_agent.routing.models import (
    AgentTaskContract,
    ModelTarget,
    ProviderProfile,
    RoutePolicy,
)
from nested_memvid_agent.routing.role_resolver import (
    GraphRoleAssignment,
    RoleAssignmentResolver,
)
from nested_memvid_agent.runtime_models import (
    AgentTurnResult,
    ChatMessage,
    LLMOptions,
    LLMResponse,
    ToolSpec,
)
from nested_memvid_agent.state_store import TaskNodeRecord


class _StructuredMockProvider(MockLLMProvider):
    @property
    def capabilities(self) -> ProviderCapabilities:
        return replace(super().capabilities, name="structured-test")

    def generate(
        self,
        messages: list[ChatMessage],
        tools: list[ToolSpec],
        options: LLMOptions | None = None,
    ) -> LLMResponse:
        return super().generate(messages, tools, options)


class _FakeAgent:
    """Minimal reviewer/executor agent double for the live review boundary."""

    def __init__(self, provider: Any, config: AgentConfig) -> None:
        self.llm = provider
        self.config = config


def _agent(provider: Any, config: AgentConfig) -> Any:
    return _FakeAgent(provider, config)


def _turn(result: str = "The executor produced a concrete answer.") -> AgentTurnResult:
    return AgentTurnResult(
        session_id="s",
        user_message="Solve it.",
        assistant_message=result,
        tool_executions=(),
        context_chars=0,
        memory_writes=(),
        stop_reason="complete",
    )


def _config(provider: str = "mock") -> AgentConfig:
    return AgentConfig(
        name="Kestrel",
        provider=provider,
        model="exec-model" if provider != "mock" else "mock",
        backend="memory",
        enable_semantic_orchestration=provider != "mock",
    )


def _root() -> TaskNodeRecord:
    return TaskNodeRecord(
        task_id="root",
        run_id="r",
        title="Plan",
        goal="Return a concrete answer.",
        profile="planner",
        status="running",
        acceptance_criteria=("The answer is concrete.",),
    )


_PROFILES = (
    ProviderProfile(
        profile_id="prov-a",
        display_name="A",
        adapter="openai_compatible",
        base_url="https://example.com",
        secret_ref="S",
        locality="cloud",
    ),
    ProviderProfile(
        profile_id="prov-b",
        display_name="B",
        adapter="openai_compatible",
        base_url="https://example.com",
        secret_ref="S",
        locality="cloud",
    ),
)


def _target(
    tid: str,
    pid: str = "prov-a",
    *,
    model: str = "m",
    model_family: str | None = None,
    role_affinities: tuple[str, ...] = ("executor",),
) -> ModelTarget:
    metadata: dict[str, object] = {}
    if model_family is not None:
        metadata["model_family"] = model_family
    return ModelTarget(
        target_id=tid,
        provider_profile_id=pid,
        provider="openai_compatible",
        model=model,
        quality_tier=3,
        role_affinities=role_affinities,
        locality="cloud",
        supports_json=True,
        metadata=metadata,
    )


def _contract(role: str) -> AgentTaskContract:
    return AgentTaskContract(
        task_id="root",
        run_id="r",
        role=role,
        task_family="review" if role == "reviewer" else "general",
        objective="Review the answer.",
        complexity=0.3,
        ambiguity=0.2,
        risk="low",
        structured_output_required=role == "reviewer",
    )


def _assignment(
    *,
    mode: str = "constrained",
    targets: tuple[ModelTarget, ...],
    policy: RoutePolicy | None = None,
) -> GraphRoleAssignment:
    resolver = RoleAssignmentResolver(
        profiles=_PROFILES,
        targets=targets,
        policy=policy or RoutePolicy(),
        mode=mode,  # type: ignore[arg-type]
    )
    return resolver.resolve(_contract("executor"), _contract("planner"), _contract("reviewer"))


# ---------------------------------------------------------------------------
# 1. independent_target — the live reviewer routes through a SEPARATE agent.
# ---------------------------------------------------------------------------


class TestIndependentTargetRouting:
    def test_reviewer_uses_separate_agent_and_labels_independent(self):
        independent = (
            _target("tgt-exec", "prov-a", model="exec-model", model_family="family-a"),
            _target(
                "tgt-review",
                "prov-b",
                model="review-model",
                model_family="family-b",
                role_affinities=("reviewer",),
            ),
        )
        assignment = _assignment(targets=independent)
        assert assignment.review_authority.value == "independent_target"

        executor_agent = _agent(_StructuredMockProvider(), _config("mock"))
        reviewer_agent = _agent(
            _StructuredMockProvider(
                canned=[
                    LLMResponse(
                        content=(
                            '{"verdict":"pass","summary":"ok","criteria":['
                            '{"criterion":"The answer is concrete.","status":"satisfied",'
                            '"evidence_refs":["assistant_response"],"reason":"evident"}],'
                            '"remaining_risks":[],"confidence":0.9}'
                        )
                    )
                ]
            ),
            _config("openai_compatible"),
        )

        review = evaluate_turn_review(
            message="Solve it.",
            config=_config("mock"),
            result=_turn(),
            root_task=_root(),
            agent=executor_agent,
            review_assignment=assignment,
            reviewer_agent=reviewer_agent,
        )

        assert review["review_authority"] == "independent_target"
        assert review["review_fallback"] is False
        assert review["off_mode_abstained"] is False
        assert review["gate"] == "provider_semantic_review"
        # The provider review ran through the separate reviewer target's model.
        assert review["artifact"]["provider"] == {
            "name": "structured-test",
            "model": "exec-model",
        }

    def test_independent_without_reviewer_agent_truthfully_falls_back(self):
        independent = (
            _target("tgt-exec", "prov-a", model="exec-model", model_family="family-a"),
            _target(
                "tgt-review",
                "prov-b",
                model="review-model",
                model_family="family-b",
                role_affinities=("reviewer",),
            ),
        )
        assignment = _assignment(targets=independent)
        assert assignment.review_authority.value == "independent_target"

        # No reviewer agent was built: independence must NOT be silently
        # downgraded to a same-agent review — it is a truthful fallback.
        review = evaluate_turn_review(
            message="Solve it.",
            config=_config("mock"),
            result=_turn(),
            root_task=_root(),
            agent=_agent(_StructuredMockProvider(), _config("mock")),
            review_assignment=assignment,
            reviewer_agent=None,
        )

        assert review["review_authority"] == "deterministic_fallback"
        assert review["review_fallback"] is True
        assert review["off_mode_abstained"] is False
        assert review["review_rejection_reasons"] == ["reviewer_agent_unavailable"]


# ---------------------------------------------------------------------------
# 2. deterministic_fallback — reason codes preserved through the live gate.
# ---------------------------------------------------------------------------


class TestDeterministicFallbackPreservesReasons:
    def test_rejection_reason_codes_are_preserved(self):
        # A single target with reviewer affinity but the independence policy
        # requires a different target -> resolver rejects it truthfully.
        single = (_target("tgt-only", "prov-a", role_affinities=("executor", "reviewer")),)
        policy = RoutePolicy(require_different_target_for_review=True)
        assignment = _assignment(targets=single, policy=policy)
        assert assignment.review_authority.value == "deterministic_fallback"

        review = evaluate_turn_review(
            message="Solve it.",
            config=_config("mock"),
            result=_turn(),
            root_task=_root(),
            agent=_agent(_StructuredMockProvider(), _config("mock")),
            review_assignment=assignment,
        )

        assert review["review_authority"] == "deterministic_fallback"
        assert review["review_fallback"] is True
        assert review["off_mode_abstained"] is False
        assert "review_target_not_independent" in review["review_rejection_reasons"]
        assert review["artifact"]["evaluator"] == "deterministic_runtime_evidence"


# ---------------------------------------------------------------------------
# 3. off_mode_abstained — the reviewer abstains and is never mis-claimed.
# ---------------------------------------------------------------------------


class TestOffModeAbstains:
    def test_off_mode_labels_abstention_not_fallback(self):
        independent = (
            _target("tgt-exec", "prov-a", model="exec-model", model_family="family-a"),
            _target(
                "tgt-review",
                "prov-b",
                model="review-model",
                model_family="family-b",
                role_affinities=("reviewer",),
            ),
        )
        assignment = _assignment(mode="off", targets=independent)
        assert assignment.review_authority.value == "off_mode_abstained"

        review = evaluate_turn_review(
            message="Solve it.",
            config=_config("mock"),
            result=_turn(),
            root_task=_root(),
            agent=_agent(_StructuredMockProvider(), _config("mock")),
            review_assignment=assignment,
        )

        assert review["review_authority"] == "off_mode_abstained"
        assert review["off_mode_abstained"] is True
        # Abstention is never mis-claimed as a deterministic fallback.
        assert review["review_fallback"] is False
        assert review["review_rejection_reasons"] == []
        assert review["provider_review_status"] == "abstained"

    def test_off_mode_does_not_run_provider_review_even_with_independent_target(self):
        independent = (
            _target("tgt-exec", "prov-a", model="exec-model", model_family="family-a"),
            _target(
                "tgt-review",
                "prov-b",
                model="review-model",
                model_family="family-b",
                role_affinities=("reviewer",),
            ),
        )
        assignment = _assignment(mode="off", targets=independent)
        assert assignment.review_authority.value == "off_mode_abstained"

        # A reviewer agent that would pass if invoked — but off mode must not
        # invoke it.
        reviewer_agent = _agent(
            _StructuredMockProvider(
                canned=[
                    LLMResponse(
                        content=(
                            '{"verdict":"pass","summary":"ok","criteria":['
                            '{"criterion":"The answer is concrete.","status":"satisfied",'
                            '"evidence_refs":["assistant_response"],"reason":"evident"}],'
                            '"remaining_risks":[],"confidence":0.9}'
                        )
                    )
                ]
            ),
            _config("openai_compatible"),
        )

        review = evaluate_turn_review(
            message="Solve it.",
            config=_config("mock"),
            result=_turn(),
            root_task=_root(),
            agent=_agent(_StructuredMockProvider(), _config("mock")),
            review_assignment=assignment,
            reviewer_agent=reviewer_agent,
        )

        assert review["review_authority"] == "off_mode_abstained"
        assert review["artifact"]["evaluator"] == "deterministic_runtime_evidence"
        assert review["provider_review_status"] == "abstained"


# ---------------------------------------------------------------------------
# 4. No resolver wired — legacy behaviour unchanged (backward compatibility).
# ---------------------------------------------------------------------------


class TestNoResolverWired:
    def test_missing_assignment_keeps_legacy_deterministic_review(self):
        review = evaluate_turn_review(
            message="Solve it.",
            config=_config("mock"),
            result=_turn(),
            root_task=_root(),
            agent=_agent(_StructuredMockProvider(), _config("mock")),
        )

        assert review["artifact"]["evaluator"] == "deterministic_runtime_evidence"
        assert "review_authority" not in review


# ---------------------------------------------------------------------------
# 5. ReviewerNode — the live graph node invokes the resolver and the separate
#    reviewer-agent builder when wired through GraphRuntimeServices.
# ---------------------------------------------------------------------------


class _Span:
    def __init__(self) -> None:
        self.result: dict[str, Any] = {}
        self.span_id = "span-1"

    def __enter__(self) -> _Span:
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def set_result(self, **kwargs: Any) -> None:
        self.result.update(kwargs)


class _Spans:
    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []

    def start(self, **kwargs: Any) -> _Span:
        self.started.append(kwargs)
        return _Span()


class _Events:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, dict[str, Any]]] = []

    def publish(self, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        self.published.append((run_id, event_type, payload))


class _State:
    def __init__(self, root: TaskNodeRecord) -> None:
        self.root = root
        self.updated: list[tuple[str, dict[str, Any]]] = []

    def list_task_nodes(self, run_id: str) -> list[TaskNodeRecord]:
        return [self.root]

    def update_task_node(self, task_id: str, **kwargs: Any) -> None:
        self.updated.append((task_id, kwargs))


def _node_services(
    *,
    resolver: Any,
    builder: Any | None,
    state: _State,
) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(
        spans=_Spans(),
        state=state,
        events=_Events(),
        review_authority_resolver=resolver,
        build_reviewer_agent=builder,
    )


class TestReviewerNodeWiring:
    def test_node_routes_off_mode_abstention_through_resolver(self):
        from nested_memvid_agent.graph_runtime import GraphRunState, ReviewerNode

        independent = (
            _target("tgt-exec", "prov-a", model="exec-model", model_family="family-a"),
            _target(
                "tgt-review",
                "prov-b",
                model="review-model",
                model_family="family-b",
                role_affinities=("reviewer",),
            ),
        )
        assignment = _assignment(mode="off", targets=independent)
        assert assignment.review_authority.value == "off_mode_abstained"

        resolver = lambda ctx: assignment  # noqa: E731
        state = _State(_root())
        services = _node_services(resolver=resolver, builder=None, state=state)
        ctx = GraphRunState(
            run_id="r",
            config=_config("mock"),
            message="Solve it.",
            session_id="s",
            agent=_agent(_StructuredMockProvider(), _config("mock")),
            result=_turn(),
        )

        ReviewerNode().run(ctx, services, "span-0")

        assert ctx.review["review_authority"] == "off_mode_abstained"
        assert ctx.review["off_mode_abstained"] is True
        assert ctx.review["review_fallback"] is False

    def test_node_builds_separate_reviewer_agent_for_independent_target(self):
        from nested_memvid_agent.graph_runtime import GraphRunState, ReviewerNode

        independent = (
            _target("tgt-exec", "prov-a", model="exec-model", model_family="family-a"),
            _target(
                "tgt-review",
                "prov-b",
                model="review-model",
                model_family="family-b",
                role_affinities=("reviewer",),
            ),
        )
        assignment = _assignment(targets=independent)
        assert assignment.review_authority.value == "independent_target"

        reviewer_agent = _agent(
            _StructuredMockProvider(
                canned=[
                    LLMResponse(
                        content=(
                            '{"verdict":"pass","summary":"ok","criteria":['
                            '{"criterion":"The answer is concrete.","status":"satisfied",'
                            '"evidence_refs":["assistant_response"],"reason":"evident"}],'
                            '"remaining_risks":[],"confidence":0.9}'
                        )
                    )
                ]
            ),
            replace(_config("openai_compatible"), model="review-model"),
        )
        built: list[tuple[Any, Any]] = []

        def resolver(ctx: Any) -> Any:
            return assignment

        def builder(ctx: Any, resolved: Any) -> Any:
            built.append((ctx, resolved))
            return reviewer_agent

        state = _State(_root())
        services = _node_services(resolver=resolver, builder=builder, state=state)
        ctx = GraphRunState(
            run_id="r",
            config=_config("mock"),
            message="Solve it.",
            session_id="s",
            agent=_agent(_StructuredMockProvider(), _config("mock")),
            result=_turn(),
        )

        ReviewerNode().run(ctx, services, "span-0")

        assert ctx.review["review_authority"] == "independent_target"
        assert built and built[0][1] is assignment
        assert ctx.review["artifact"]["provider"]["model"] == "review-model"

