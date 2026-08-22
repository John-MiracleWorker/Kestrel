"""Zero-authority production shadow observation ledger (S5 / SHADOW-001..004).

This module is the *observational side channel* for Adaptive Flock routing.  It
records, for every eligible durable scheduler/subagent planner, executor,
reviewer, and summarizer attempt, what *actually* executed (authority + target)
alongside what Adaptive Flock *would have* recommended (the shadow target), plus
the candidates, qualification evidence, constraints, structured reasons, and
terminal evidence needed to answer the honest question:

    "Would Adaptive Flock differ, and was the evidence favorable?"

The observation is **zero authority by construction**:

* it never selects or executes a target (it only reads the already-made
  decision and the learned-router shadow evaluation);
* it never writes policy memory, grants, calibration, or control flow — only an
  append-only observation row in the control-plane SQLite ledger;
* every public entry point here is best-effort: a failure is swallowed so the
  base routing decision is byte-identical whether or not observation runs
  (SHADOW-004).

Actual authority values (the SOT durable-interface vocabulary) are closed:

    ``deterministic_static``               ordinary v0.6 static routing;
    ``adaptive_activated``                 learned routing under a durable grant;
    ``deterministic_fallback_after_suspension``  fail-closed return to static;
    ``operator_pinned``                    an explicit operator target override.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from .models import AgentTaskContract, ModelTarget, RouteDecision
from .qualification_digest import canonical_digest


class _ShadowObservationStore(Protocol):
    def record_shadow_observation(
        self, draft: ShadowObservationDraft, *, observation_id: str | None = None
    ) -> ShadowObservationEntry: ...

    def resolve_shadow_observation(
        self,
        observation_id: str,
        *,
        validation_passed: bool,
        actual_cost_usd: float | None = None,
        actual_latency_seconds: float | None = None,
    ) -> ShadowObservationEntry: ...


class ActualAuthority(StrEnum):
    """Closed, durable label for *how* the executed target was chosen."""

    DETERMINISTIC_STATIC = "deterministic_static"
    ADAPTIVE_ACTIVATED = "adaptive_activated"
    DETERMINISTIC_FALLBACK_AFTER_SUSPENSION = "deterministic_fallback_after_suspension"
    OPERATOR_PINNED = "operator_pinned"


class ShadowVerdict(StrEnum):
    """Observational verdict for "would Adaptive Flock differ, favorably?"."""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    INCONCLUSIVE = "inconclusive"


class ShadowRole(StrEnum):
    """The closed set of observed graph/scheduler roles."""

    PLANNER = "planner"
    EXECUTOR = "executor"
    REVIEWER = "reviewer"
    SUMMARIZER = "summarizer"


@dataclass(frozen=True)
class ShadowVerdictResult:
    """A verdict plus the structured reason and evidence basis behind it."""

    verdict: ShadowVerdict
    reason: str
    counterfactual_proven: bool
    evidence_basis: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "reason": self.reason,
            "counterfactual_proven": self.counterfactual_proven,
            "evidence_basis": list(self.evidence_basis),
        }


@dataclass(frozen=True)
class ShadowObservationDraft:
    """Everything the ledger needs to persist one shadow observation.

    ``usage`` and the terminal fields (``validation_passed`` / ``actual_cost_usd``
    / ``actual_latency_seconds``) are optional at record time and filled in by
    :meth:`~nested_memvid_agent.routing.ledger.RoutingLedger.resolve_shadow_observation`
    once the executed attempt reaches a terminal state.
    """

    run_id: str
    task_id: str
    subagent_id: str | None
    attempt: int
    role: ShadowRole
    actual_authority: ActualAuthority
    actual_target_id: str | None
    actual_provider: str
    actual_model: str
    shadow_target_id: str | None
    shadow_provider: str
    shadow_model: str
    shadow_executed: bool
    static_target_id: str | None
    candidates: tuple[dict[str, Any], ...]
    constraints: dict[str, Any]
    qualification: dict[str, Any]
    reason_codes: tuple[str, ...]
    usage: dict[str, Any]
    verdict: ShadowVerdict
    verdict_reason: str
    evidence_basis: tuple[str, ...]
    counterfactual_proven: bool
    payload_digest: str
    validation_passed: bool | None = None
    actual_cost_usd: float | None = None
    actual_latency_seconds: float | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "subagent_id": self.subagent_id,
            "attempt": self.attempt,
            "role": self.role.value,
            "actual_authority": self.actual_authority.value,
            "actual_target_id": self.actual_target_id,
            "actual_provider": self.actual_provider,
            "actual_model": self.actual_model,
            "shadow_target_id": self.shadow_target_id,
            "shadow_provider": self.shadow_provider,
            "shadow_model": self.shadow_model,
            "shadow_executed": self.shadow_executed,
            "static_target_id": self.static_target_id,
            "candidates": [dict(item) for item in self.candidates],
            "constraints": dict(self.constraints),
            "qualification": dict(self.qualification),
            "reason_codes": list(self.reason_codes),
            "usage": dict(self.usage),
            "verdict": self.verdict.value,
            "verdict_reason": self.verdict_reason,
            "evidence_basis": list(self.evidence_basis),
            "counterfactual_proven": self.counterfactual_proven,
            "payload_digest": self.payload_digest,
            "validation_passed": self.validation_passed,
            "actual_cost_usd": self.actual_cost_usd,
            "actual_latency_seconds": self.actual_latency_seconds,
        }


def compute_shadow_verdict(
    *,
    shadow_target_id: str | None,
    actual_target_id: str | None,
    shadow_executed: bool,
    abstention_reason: str | None,
    favorable: bool | None,
    actual_validation_passed: bool | None,
) -> ShadowVerdictResult:
    """Answer "would Adaptive Flock differ, and was the evidence favorable?" honestly.

    The three-valued verdict is a pure function of the already-resolved facts:

    * ``shadow_target_id is None`` — the learned router abstained, so Adaptive
      Flock has no differing recommendation: ``inconclusive``.
    * ``shadow_target_id == actual_target_id`` — Adaptive Flock would NOT
      differ.  The verdict then reports whether the shared choice was borne out
      by terminal evidence (``supported`` on pass, ``contradicted`` on fail),
      or ``inconclusive`` before any terminal evidence exists.
    * ``shadow_target_id != actual_target_id`` — Adaptive Flock WOULD differ.

      - If the shadow target actually executed (``shadow_executed``), its own
        terminal outcome is direct evidence: ``supported`` on pass,
        ``contradicted`` on fail, ``inconclusive`` while still running.
      - If the shadow target was **never executed**, only prior learned evidence
        may speak, and there is *no counterfactual proof*: ``supported`` means
        "prior evidence favors the recommendation" (``counterfactual_proven`` is
        always ``False`` here), ``contradicted`` means prior evidence opposes it,
        and ``inconclusive`` covers insufficient/absent evidence.  A mismatched
        unexecuted target can never be reported as proven better or worse.
    """

    if shadow_target_id is None:
        reason = abstention_reason or "no_shadow_recommendation"
        return ShadowVerdictResult(
            verdict=ShadowVerdict.INCONCLUSIVE,
            reason=reason,
            counterfactual_proven=False,
            evidence_basis=("shadow_abstained",),
        )

    if actual_target_id is None:
        # Nothing executed: no basis for any comparison.
        return ShadowVerdictResult(
            verdict=ShadowVerdict.INCONCLUSIVE,
            reason="no_actual_execution",
            counterfactual_proven=False,
            evidence_basis=("no_actual_target",),
        )

    if shadow_target_id == actual_target_id:
        # Adaptive Flock agrees with what actually ran.
        if actual_validation_passed is True:
            return ShadowVerdictResult(
                verdict=ShadowVerdict.SUPPORTED,
                reason="shadow_agrees_and_actual_succeeded",
                counterfactual_proven=True,
                evidence_basis=("terminal_validation_passed",),
            )
        if actual_validation_passed is False:
            return ShadowVerdictResult(
                verdict=ShadowVerdict.CONTRADICTED,
                reason="shadow_agrees_and_actual_failed",
                counterfactual_proven=True,
                evidence_basis=("terminal_validation_failed",),
            )
        return ShadowVerdictResult(
            verdict=ShadowVerdict.INCONCLUSIVE,
            reason="no_terminal_evidence",
            counterfactual_proven=False,
            evidence_basis=(),
        )

    # Adaptive Flock would differ.
    if shadow_executed:
        if actual_validation_passed is True:
            return ShadowVerdictResult(
                verdict=ShadowVerdict.SUPPORTED,
                reason="shadow_executed_and_passed",
                counterfactual_proven=True,
                evidence_basis=("shadow_executed", "terminal_validation_passed"),
            )
        if actual_validation_passed is False:
            return ShadowVerdictResult(
                verdict=ShadowVerdict.CONTRADICTED,
                reason="shadow_executed_and_failed",
                counterfactual_proven=True,
                evidence_basis=("shadow_executed", "terminal_validation_failed"),
            )
        return ShadowVerdictResult(
            verdict=ShadowVerdict.INCONCLUSIVE,
            reason="shadow_executed_no_terminal_evidence",
            counterfactual_proven=False,
            evidence_basis=("shadow_executed",),
        )

    # Mismatched, unexecuted shadow target: prior evidence only, no proof.
    if favorable is True:
        return ShadowVerdictResult(
            verdict=ShadowVerdict.SUPPORTED,
            reason="shadow_favored_by_prior_evidence",
            counterfactual_proven=False,
            evidence_basis=("prior_evidence_favorable", "target_unexecuted"),
        )
    if favorable is False:
        return ShadowVerdictResult(
            verdict=ShadowVerdict.CONTRADICTED,
            reason="shadow_contradicted_by_prior_evidence",
            counterfactual_proven=False,
            evidence_basis=("prior_evidence_unfavorable", "target_unexecuted"),
        )
    return ShadowVerdictResult(
        verdict=ShadowVerdict.INCONCLUSIVE,
        reason="insufficient_prior_evidence",
        counterfactual_proven=False,
        evidence_basis=("target_unexecuted",),
    )


def shadow_favorability(
    *,
    learned_target_id: str | None,
    utility_delta: float,
    confidence: float,
    abstention_reason: str | None,
    confidence_threshold: float = 0.70,
) -> bool | None:
    """Map a learned-router shadow evaluation onto a favorability signal.

    ``True`` when the learned recommendation has positive utility over the
    static target and meets the confidence floor; ``False`` when it is present
    but fails those floors; ``None`` when there is no recommendation at all.
    """

    if learned_target_id is None:
        return None
    if abstention_reason is not None:
        # Sparse / high-risk / low-confidence abstention is not favorable
        # evidence, but a present recommendation that failed the utility
        # margin is still an *unfavorable* signal.
        if confidence < confidence_threshold or utility_delta <= 0.0:
            return False
    if utility_delta > 0.0 and confidence >= confidence_threshold:
        return True
    return False


def stable_shadow_observation_id(
    *,
    run_id: str,
    task_id: str,
    subagent_id: str | None,
    attempt: int,
    role: ShadowRole,
    payload_digest: str,
) -> str:
    """Deterministic observation id: replay of the same attempt is idempotent."""

    import hashlib
    import json

    encoded = json.dumps(
        [run_id, task_id, subagent_id, attempt, role.value, payload_digest],
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "shadow_obs_" + hashlib.sha256(encoded).hexdigest()[:40]


def shadow_observation_payload_digest(draft: ShadowObservationDraft) -> str:
    """Replay-stable digest over the observation's *content* fields.

    Volatile fields (``validation_passed`` / ``actual_cost_usd`` /
    ``actual_latency_seconds`` and any id/timestamp) are excluded so that the
    same attempt observed with the same routing facts always produces the same
    digest, regardless of when the terminal evidence was recorded.
    """

    payload: dict[str, Any] = {
        "run_id": draft.run_id,
        "task_id": draft.task_id,
        "subagent_id": draft.subagent_id,
        "attempt": draft.attempt,
        "role": draft.role.value,
        "actual_authority": draft.actual_authority.value,
        "actual_target_id": draft.actual_target_id,
        "actual_provider": draft.actual_provider,
        "actual_model": draft.actual_model,
        "shadow_target_id": draft.shadow_target_id,
        "shadow_provider": draft.shadow_provider,
        "shadow_model": draft.shadow_model,
        "shadow_executed": draft.shadow_executed,
        "static_target_id": draft.static_target_id,
        "candidates": list(draft.candidates),
        "constraints": draft.constraints,
        "qualification": draft.qualification,
        "reason_codes": list(draft.reason_codes),
        "usage": draft.usage,
        "verdict": draft.verdict.value,
        "verdict_reason": draft.verdict_reason,
        "evidence_basis": list(draft.evidence_basis),
        "counterfactual_proven": draft.counterfactual_proven,
    }
    return canonical_digest(payload)


def build_shadow_observation_draft(
    *,
    run_id: str,
    task_id: str,
    subagent_id: str | None,
    attempt: int,
    role: ShadowRole,
    actual_authority: ActualAuthority,
    decision: RouteDecision,
    contract: AgentTaskContract,
    shadow_target_id: str | None,
    shadow_targets: dict[str, ModelTarget],
    shadow_executed: bool,
    static_target_id: str | None,
    qualification: dict[str, Any],
    reason_codes: tuple[str, ...],
    usage: dict[str, Any] | None = None,
    abstention_reason: str | None = None,
    utility_delta: float = 0.0,
    confidence: float = 0.0,
    validation_passed: bool | None = None,
    actual_cost_usd: float | None = None,
    actual_latency_seconds: float | None = None,
) -> ShadowObservationDraft:
    """Assemble a replay-stable draft from the resolved routing facts.

    ``decision`` is the *actual* executed decision (never altered); the shadow
    recommendation is looked up from ``shadow_targets`` so the observation
    records provider/model identity without ever building or calling an
    alternate provider.
    """

    actual_target = decision.selected_target
    shadow_target = shadow_targets.get(shadow_target_id) if shadow_target_id else None
    shadow_provider = shadow_target.provider if shadow_target is not None else ""
    shadow_model = shadow_target.model if shadow_target is not None else ""

    favorable = shadow_favorability(
        learned_target_id=shadow_target_id,
        utility_delta=utility_delta,
        confidence=confidence,
        abstention_reason=abstention_reason,
    )
    verdict = compute_shadow_verdict(
        shadow_target_id=shadow_target_id,
        actual_target_id=actual_target.target_id,
        shadow_executed=shadow_executed,
        abstention_reason=abstention_reason,
        favorable=favorable,
        actual_validation_passed=validation_passed,
    )

    constraints = _contract_constraints(contract)

    base_usage = usage if usage is not None else _empty_usage()

    draft = ShadowObservationDraft(
        run_id=run_id,
        task_id=task_id,
        subagent_id=subagent_id,
        attempt=attempt,
        role=role,
        actual_authority=actual_authority,
        actual_target_id=actual_target.target_id,
        actual_provider=actual_target.provider,
        actual_model=actual_target.model,
        shadow_target_id=shadow_target_id,
        shadow_provider=shadow_provider,
        shadow_model=shadow_model,
        shadow_executed=shadow_executed,
        static_target_id=static_target_id,
        candidates=tuple(
            _bounded_candidate_payload(candidate) for candidate in decision.candidates[:64]
        ),
        constraints=constraints,
        qualification=qualification,
        reason_codes=reason_codes,
        usage=base_usage,
        verdict=verdict.verdict,
        verdict_reason=verdict.reason,
        evidence_basis=verdict.evidence_basis,
        counterfactual_proven=verdict.counterfactual_proven,
        payload_digest="",
        validation_passed=validation_passed,
        actual_cost_usd=actual_cost_usd,
        actual_latency_seconds=actual_latency_seconds,
    )
    digest = shadow_observation_payload_digest(draft)
    return ShadowObservationDraft(
        **{**draft.__dict__, "payload_digest": digest},
    )


def _bounded_candidate_payload(candidate: Any) -> dict[str, Any]:
    payload = candidate.to_payload()
    return {
        "target_id": str(payload.get("target_id", ""))[:256],
        "provider_profile_id": str(payload.get("provider_profile_id", ""))[:256],
        "provider": str(payload.get("provider", ""))[:128],
        "model": str(payload.get("model", ""))[:256],
        "eligible": bool(payload.get("eligible")),
        "score": payload.get("score"),
        "reason_codes": [str(item)[:128] for item in list(payload.get("reason_codes", []))[:32]],
        "components": {
            str(key)[:128]: float(value)
            for key, value in dict(payload.get("components", {})).items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        },
    }


def _contract_constraints(contract: AgentTaskContract) -> dict[str, Any]:
    return {
        "task_family": contract.task_family,
        "risk": contract.risk,
        "privacy_class": contract.privacy_class,
        "local_required": contract.local_required,
        "local_preferred": contract.local_preferred,
        "required_capabilities": sorted(contract.required_capabilities),
        "required_tools": sorted(contract.required_tools),
        "required_modalities": sorted(contract.required_modalities),
        "structured_output_required": contract.structured_output_required,
        "maximum_cost_usd": contract.maximum_cost_usd,
        "allowed_target_ids": sorted(contract.allowed_target_ids),
        "forbidden_target_ids": sorted(contract.forbidden_target_ids),
        "allowed_provider_profiles": sorted(contract.allowed_provider_profiles),
        "forbidden_provider_profiles": sorted(contract.forbidden_provider_profiles),
    }


def _empty_usage() -> dict[str, Any]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "actual_cost_usd": None,
        "latency_seconds": None,
    }


def empty_qualification() -> dict[str, Any]:
    """A neutral qualification payload for attempts with no learned evidence."""

    return {
        "evidence_count": 0,
        "target_example_count": 0,
        "confidence": 0.0,
        "cost_coverage": 0.0,
        "utility_delta": 0.0,
        "abstention_reason": None,
    }


def shadow_role_for(role: str) -> ShadowRole:
    """Map a scheduler task profile onto the closed observation-role vocabulary.

    Unknown profiles fall back to ``EXECUTOR`` (the ordinary scheduler/subagent
    worker) so observation stays total without ever inventing a role.
    """

    normalized = str(role or "").strip().lower()
    if normalized in {"planner"}:
        return ShadowRole.PLANNER
    if normalized in {"reviewer", "review"}:
        return ShadowRole.REVIEWER
    if normalized in {"summarizer", "summary", "summarization"}:
        return ShadowRole.SUMMARIZER
    return ShadowRole.EXECUTOR


def actual_authority_for(
    *,
    selection_kind: str,
    activation_effective: bool,
    operator_pinned: bool,
) -> ActualAuthority:
    """Derive the durable authority label for one executed routing decision.

    ``operator_pinned`` wins (an explicit override is always authoritative),
    then a genuinely effective learned grant (``adaptive_activated``), and
    otherwise ordinary deterministic static routing.  The suspension-fallback
    value is reserved for AUTH-003; this coordinator path never produces it
    because suspension/revocation is not yet a live state.
    """

    if operator_pinned or selection_kind == "operator_override":
        return ActualAuthority.OPERATOR_PINNED
    if activation_effective or selection_kind == "learned_constrained":
        return ActualAuthority.ADAPTIVE_ACTIVATED
    return ActualAuthority.DETERMINISTIC_STATIC


@dataclass(frozen=True)
class ShadowObservationEntry:
    """A persisted shadow observation row (additive schema v5)."""

    observation_id: str
    run_id: str
    task_id: str
    subagent_id: str | None
    attempt: int
    role: str
    actual_authority: str
    actual_target_id: str | None
    actual_provider: str
    actual_model: str
    shadow_target_id: str | None
    shadow_provider: str
    shadow_model: str
    shadow_executed: bool
    static_target_id: str | None
    candidates: tuple[dict[str, Any], ...]
    constraints: dict[str, Any]
    qualification: dict[str, Any]
    reason_codes: tuple[str, ...]
    usage: dict[str, Any]
    verdict: str
    verdict_reason: str
    evidence_basis: tuple[str, ...]
    counterfactual_proven: bool
    payload_digest: str
    created_at: str
    resolved_at: str | None = None
    validation_passed: bool | None = None
    actual_cost_usd: float | None = None
    actual_latency_seconds: float | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "subagent_id": self.subagent_id,
            "attempt": self.attempt,
            "role": self.role,
            "actual_authority": self.actual_authority,
            "actual_target_id": self.actual_target_id,
            "actual_provider": self.actual_provider,
            "actual_model": self.actual_model,
            "shadow_target_id": self.shadow_target_id,
            "shadow_provider": self.shadow_provider,
            "shadow_model": self.shadow_model,
            "shadow_executed": self.shadow_executed,
            "static_target_id": self.static_target_id,
            "candidates": [dict(item) for item in self.candidates],
            "constraints": dict(self.constraints),
            "qualification": dict(self.qualification),
            "reason_codes": list(self.reason_codes),
            "usage": dict(self.usage),
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
            "evidence_basis": list(self.evidence_basis),
            "counterfactual_proven": self.counterfactual_proven,
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
            "resolved_at": self.resolved_at,
            "validation_passed": self.validation_passed,
            "actual_cost_usd": self.actual_cost_usd,
            "actual_latency_seconds": self.actual_latency_seconds,
        }


class ShadowObservationRecorder:
    """Best-effort, zero-authority side channel that persists observations.

    ``record`` and ``resolve`` swallow all exceptions and return ``None`` on
    failure — an observer that cannot write must never change the base routing
    decision or the run's terminal truth (SHADOW-004).
    """

    def __init__(self, ledger: _ShadowObservationStore) -> None:
        self.ledger = ledger

    def record(self, draft: ShadowObservationDraft) -> ShadowObservationEntry | None:
        try:
            return self.ledger.record_shadow_observation(draft)
        except Exception:  # noqa: BLE001 - observation is a side channel
            return None

    def resolve(
        self,
        observation_id: str,
        *,
        validation_passed: bool,
        actual_cost_usd: float | None = None,
        actual_latency_seconds: float | None = None,
    ) -> ShadowObservationEntry | None:
        try:
            return self.ledger.resolve_shadow_observation(
                observation_id,
                validation_passed=validation_passed,
                actual_cost_usd=actual_cost_usd,
                actual_latency_seconds=actual_latency_seconds,
            )
        except Exception:  # noqa: BLE001 - observation is a side channel
            return None


__all__ = [
    "ActualAuthority",
    "ShadowRole",
    "ShadowVerdict",
    "ShadowVerdictResult",
    "ShadowObservationDraft",
    "ShadowObservationEntry",
    "ShadowObservationRecorder",
    "actual_authority_for",
    "build_shadow_observation_draft",
    "compute_shadow_verdict",
    "empty_qualification",
    "shadow_favorability",
    "shadow_observation_payload_digest",
    "shadow_role_for",
    "stable_shadow_observation_id",
]
