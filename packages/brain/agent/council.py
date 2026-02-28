from __future__ import annotations
"""
Agent Council — multi-agent swarm debate and consensus.

Instead of a single agent making all decisions, the Council pattern
assembles multiple specialist perspectives that:
  1. Independently propose approaches
  2. Critique each other's proposals
  3. Vote on the best path forward
  4. Reach consensus (or flag disagreement for user review)

This produces higher-quality decisions than any single agent,
because diverse perspectives catch each other's blind spots.

Council roles:
  - Architect: system design, scalability, patterns
  - Implementer: practical coding, edge cases, testing
  - Security Reviewer: vulnerabilities, data protection, access control
  - Devil's Advocate: challenges assumptions, finds weaknesses
  - User Advocate: UX impact, simplicity, user experience
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.council")


def _council_max_tokens(env_name: str, default: int) -> int:
    """Read council token caps from env with sane bounds."""
    raw = os.getenv(env_name, str(default))
    try:
        val = int(raw)
    except ValueError:
        val = default
    return max(256, min(val, 4096))


class CouncilRole(str, Enum):
    """Roles that council members can take."""
    ARCHITECT = "architect"
    IMPLEMENTER = "implementer"
    SECURITY = "security"
    DEVILS_ADVOCATE = "devils_advocate"
    USER_ADVOCATE = "user_advocate"


class VoteType(str, Enum):
    """Voting outcomes."""
    APPROVE = "approve"
    REJECT = "reject"
    ABSTAIN = "abstain"
    CONDITIONAL = "conditional"  # Approve with conditions


ROLE_PROMPTS = {
    CouncilRole.ARCHITECT: """You are the **Architect** on this council. Your focus is:
- System design and architecture patterns
- Scalability and maintainability
- Component boundaries and dependencies
- Technical debt implications

Evaluate this proposal from an architectural perspective. Be specific about design concerns.""",

    CouncilRole.IMPLEMENTER: """You are the **Implementer** on this council. Your focus is:
- Practical coding feasibility
- Edge cases and error handling
- Testing strategy
- Performance implications
- Implementation effort estimate

Evaluate this proposal from a practical implementation perspective.""",

    CouncilRole.SECURITY: """You are the **Security Reviewer** on this council. Your focus is:
- Authentication and authorization gaps
- Data exposure risks
- Input validation and sanitization
- Dependency vulnerabilities
- Compliance implications

Evaluate this proposal for security concerns. Flag anything that could be exploited.""",

    CouncilRole.DEVILS_ADVOCATE: """You are the **Devil's Advocate** on this council. Your job is to:
- Challenge every assumption
- Find the weakest points in the proposal
- Suggest what could go wrong
- Question whether simpler alternatives exist
- Push back on complexity

Be constructively critical. Your goal is to strengthen the proposal, not kill it.""",

    CouncilRole.USER_ADVOCATE: """You are the **User Advocate** on this council. Your focus is:
- User experience impact
- Simplicity and intuitiveness
- Breaking changes for existing users
- Documentation needs
- Accessibility and usability

Evaluate this proposal from the end user's perspective.""",
}

VOTE_PROMPT = """Based on your review, cast your vote:

Respond in JSON:
{{
  "vote": "approve" | "reject" | "conditional" | "abstain",
  "confidence": 0.0 to 1.0,
  "summary": "one-line summary of your position",
  "concerns": ["list of specific concerns"],
  "conditions": ["if conditional, what needs to change"],
  "suggestions": ["specific improvements"]
}}
"""


@dataclass
class CouncilMemberOpinion:
    """A council member's evaluation and vote."""
    role: CouncilRole
    analysis: str
    vote: VoteType
    confidence: float
    concerns: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "role": self.role.value,
            "vote": self.vote.value,
            "confidence": round(self.confidence, 2),
            "analysis": self.analysis[:500],
            "concerns": self.concerns,
            "conditions": self.conditions,
            "suggestions": self.suggestions,
        }


@dataclass
class CouncilVerdict:
    """The council's collective decision."""
    proposal: str
    opinions: list[CouncilMemberOpinion] = field(default_factory=list)
    consensus: Optional[VoteType] = None
    consensus_confidence: float = 0.0
    has_consensus: bool = False
    dissenting_roles: list[str] = field(default_factory=list)
    synthesized_concerns: list[str] = field(default_factory=list)
    synthesized_suggestions: list[str] = field(default_factory=list)
    requires_user_review: bool = False
    review_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "proposal": self.proposal[:300],
            "has_consensus": self.has_consensus,
            "consensus": self.consensus.value if self.consensus else None,
            "consensus_confidence": round(self.consensus_confidence, 2),
            "votes": {o.role.value: o.vote.value for o in self.opinions},
            "opinions": [o.to_dict() for o in self.opinions],
            "dissenting_roles": self.dissenting_roles,
            "concerns": self.synthesized_concerns,
            "suggestions": self.synthesized_suggestions,
            "requires_user_review": self.requires_user_review,
            "review_reason": self.review_reason,
        }


class CouncilSession:
    """
    Orchestrates a multi-agent debate on a proposal.

    Flow:
      1. Classify proposal to determine role weights
      2. Present proposal to all council members
      3. Each member independently evaluates
      4. Members critique each other's evaluations (optional debate round)
      5. Final weighted vote using adaptive expertise
      6. Synthesize verdict

    Enhancement: Adaptive expertise weighting. When a proposal is security-
    related, the Security Reviewer's vote carries more weight. For architecture
    proposals, the Architect weighs more. This produces better decisions by
    amplifying the most relevant expertise.
    """

    # Minimum members needed for quorum
    QUORUM = 3
    # Consensus requires this fraction of approvals
    CONSENSUS_THRESHOLD = 0.6
    # Max parallel LLM calls to avoid overloading providers.
    MAX_PARALLEL_EVALUATIONS = max(1, int(os.getenv("COUNCIL_MAX_PARALLEL", "3")))

    # Expertise weight multipliers by proposal category
    # Keys are categories detected from proposal content; values map roles to weight multipliers
    EXPERTISE_WEIGHTS: dict[str, dict[CouncilRole, float]] = {
        "security": {
            CouncilRole.SECURITY: 2.0,
            CouncilRole.ARCHITECT: 1.2,
            CouncilRole.IMPLEMENTER: 1.0,
            CouncilRole.DEVILS_ADVOCATE: 1.0,
            CouncilRole.USER_ADVOCATE: 0.7,
        },
        "architecture": {
            CouncilRole.ARCHITECT: 2.0,
            CouncilRole.SECURITY: 1.2,
            CouncilRole.IMPLEMENTER: 1.3,
            CouncilRole.DEVILS_ADVOCATE: 1.0,
            CouncilRole.USER_ADVOCATE: 0.8,
        },
        "performance": {
            CouncilRole.IMPLEMENTER: 1.8,
            CouncilRole.ARCHITECT: 1.5,
            CouncilRole.SECURITY: 0.8,
            CouncilRole.DEVILS_ADVOCATE: 1.0,
            CouncilRole.USER_ADVOCATE: 0.7,
        },
        "ux": {
            CouncilRole.USER_ADVOCATE: 2.0,
            CouncilRole.IMPLEMENTER: 1.0,
            CouncilRole.ARCHITECT: 0.8,
            CouncilRole.SECURITY: 0.7,
            CouncilRole.DEVILS_ADVOCATE: 1.0,
        },
        "general": {
            CouncilRole.ARCHITECT: 1.0,
            CouncilRole.IMPLEMENTER: 1.0,
            CouncilRole.SECURITY: 1.0,
            CouncilRole.DEVILS_ADVOCATE: 1.0,
            CouncilRole.USER_ADVOCATE: 1.0,
        },
    }

    # Keywords used to classify proposal category
    _CATEGORY_KEYWORDS: dict[str, list[str]] = {
        "security": [
            "security", "auth", "password", "token", "encryption", "vulnerability",
            "injection", "xss", "csrf", "secret", "credential", "permission", "access control",
        ],
        "architecture": [
            "architecture", "refactor", "design pattern", "module", "dependency",
            "abstraction", "coupling", "scalab", "migration", "schema",
        ],
        "performance": [
            "performance", "latency", "throughput", "cache", "optimize", "slow",
            "bottleneck", "memory leak", "n+1", "batch", "concurrent",
        ],
        "ux": [
            "user experience", "ui", "ux", "interface", "usability", "accessibility",
            "responsive", "design", "frontend", "onboarding",
        ],
    }

    # Max cached opinions before eviction (LRU)
    _OPINION_CACHE_MAX = 50

    def __init__(
        self,
        llm_provider=None,
        model: str = "",
        roles: list[CouncilRole] = None,
        event_callback=None,
        semantic_classifier=None,
    ):
        self._llm = llm_provider
        self._model = model
        self._roles = roles or [
            CouncilRole.ARCHITECT,
            CouncilRole.IMPLEMENTER,
            CouncilRole.SECURITY,
            CouncilRole.DEVILS_ADVOCATE,
            CouncilRole.USER_ADVOCATE,
        ]
        self._event_callback = event_callback
        self._semantic_classifier = semantic_classifier  # Optional SemanticClassifier instance
        # LRU opinion cache: keyed on (role + category + proposal fingerprint)
        self._opinion_cache: OrderedDict[str, CouncilMemberOpinion] = OrderedDict()

    def _classify_proposal(self, proposal: str) -> str:
        """Classify a proposal into a category for expertise weighting.

        Tries semantic (embedding-based) classification first if available,
        then falls back to keyword matching.
        """
        # Try semantic classification first (async-to-sync bridge for cached results)
        if self._semantic_classifier and hasattr(self._semantic_classifier, '_exemplar_embeddings'):
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # We're inside an async context — schedule as a task
                    # For now, fall through to keyword matching (semantic is async)
                    pass
            except RuntimeError:
                pass

        # Keyword-based fallback
        proposal_lower = proposal.lower()
        scores: dict[str, int] = {}
        for category, keywords in self._CATEGORY_KEYWORDS.items():
            scores[category] = sum(1 for kw in keywords if kw in proposal_lower)
        best = max(scores, key=scores.get) if scores else "general"
        return best if scores.get(best, 0) > 0 else "general"

    async def _classify_proposal_async(self, proposal: str) -> str:
        """Async version of proposal classification with semantic support."""
        if self._semantic_classifier:
            result = await self._semantic_classifier.classify(proposal)
            if result:
                return result
        return self._classify_proposal(proposal)

    def _get_role_weight(self, role: CouncilRole, category: str) -> float:
        """Get the expertise weight multiplier for a role given the proposal category."""
        weights = self.EXPERTISE_WEIGHTS.get(category, self.EXPERTISE_WEIGHTS["general"])
        return weights.get(role, 1.0)

    async def _emit(self, activity_type: str, data: dict):
        """Emit an agent activity event to the UI."""
        if self._event_callback:
            await self._event_callback(activity_type, data)

    async def deliberate_lite(
        self,
        proposal: str,
        context: str = "",
        top_n: int = 3,
    ) -> CouncilVerdict:
        """
        Lightweight council deliberation using only the top-N most expertise-relevant
        members for this proposal category. No debate round.

        Use for moderate complexity tasks (5.0 < complexity ≤ 7.0) to save ~40–60%
        of tokens versus a full deliberate() call while still getting meaningful
        multi-perspective coverage.
        """
        verdict = CouncilVerdict(proposal=proposal)
        category = self._classify_proposal(proposal)

        # Pick top-N roles by expertise weight for this proposal category
        weights = self.EXPERTISE_WEIGHTS.get(category, self.EXPERTISE_WEIGHTS["general"])
        sorted_roles = sorted(self._roles, key=lambda r: weights.get(r, 1.0), reverse=True)
        selected_roles = sorted_roles[:top_n]

        logger.info(
            f"Council lite: proposal='{category}', "
            f"members={[r.value for r in selected_roles]}"
        )

        await self._emit("council_started", {
            "topic": proposal[:200],
            "roles": [r.value for r in selected_roles],
            "member_count": len(selected_roles),
            "category": category,
            "mode": "lite",
        })

        # Parallel evaluation — no debate round
        semaphore = asyncio.Semaphore(self.MAX_PARALLEL_EVALUATIONS)

        async def evaluate_role(role: CouncilRole) -> CouncilMemberOpinion:
            async with semaphore:
                opinion = await self._get_evaluation(role, proposal, context)
            await self._emit("council_opinion", {
                "role": role.value,
                "vote": opinion.vote.value,
                "confidence": opinion.confidence,
                "analysis": opinion.analysis[:200],
            })
            return opinion

        opinions = await asyncio.gather(*(evaluate_role(role) for role in selected_roles))
        verdict.opinions = list(opinions)

        self._synthesize_verdict(verdict, category=category)

        await self._emit("council_verdict", {
            "consensus": verdict.consensus.value if verdict.consensus else "none",
            "confidence": verdict.consensus_confidence,
            "has_consensus": verdict.has_consensus,
            "requires_user_review": verdict.requires_user_review,
            "mode": "lite",
        })

        logger.info(
            f"Council lite verdict: consensus={'yes' if verdict.has_consensus else 'no'}, "
            f"votes={[o.vote.value for o in verdict.opinions]}"
        )
        return verdict

    async def deliberate(
        self,
        proposal: str,
        context: str = "",
        include_debate: bool = True,
    ) -> CouncilVerdict:
        """
        Run the full council deliberation process with adaptive expertise weighting.
        """
        verdict = CouncilVerdict(proposal=proposal)

        # Classify proposal to determine expertise weights
        category = self._classify_proposal(proposal)
        logger.info(f"Council: proposal classified as '{category}'")

        await self._emit("council_started", {
            "topic": proposal[:200],
            "roles": [r.value for r in self._roles],
            "member_count": len(self._roles),
            "category": category,
            "weights": {r.value: self._get_role_weight(r, category) for r in self._roles},
        })

        # ── Phase 1: Independent evaluation (bounded concurrency) ───────────
        semaphore = asyncio.Semaphore(self.MAX_PARALLEL_EVALUATIONS)

        async def evaluate_role(role: CouncilRole) -> CouncilMemberOpinion:
            async with semaphore:
                opinion = await self._get_evaluation(role, proposal, context)
            await self._emit("council_opinion", {
                "role": role.value,
                "vote": opinion.vote.value,
                "confidence": opinion.confidence,
                "analysis": opinion.analysis[:300],
                "concerns": opinion.concerns[:3],
            })
            return opinion

        opinions = await asyncio.gather(*(evaluate_role(role) for role in self._roles))

        # ── Phase 2: Cross-critique (debate round) ───────────────
        if include_debate and len(opinions) >= 2:
            await self._emit("council_debate", {
                "phase": "cross_critique",
                "message": "Members reviewing each other's positions...",
            })
            opinions = await self._run_debate_round(opinions, proposal)

        verdict.opinions = opinions

        # ── Phase 3: Synthesize verdict with expertise weighting ─
        self._synthesize_verdict(verdict, category=category)

        await self._emit("council_verdict", {
            "consensus": verdict.consensus.value if verdict.consensus else "none",
            "confidence": verdict.consensus_confidence,
            "has_consensus": verdict.has_consensus,
            "requires_user_review": verdict.requires_user_review,
            "concerns": verdict.synthesized_concerns[:3],
            "suggestions": verdict.synthesized_suggestions[:3],
        })

        logger.info(
            f"Council verdict: consensus={'yes' if verdict.has_consensus else 'no'}, "
            f"votes={[o.vote.value for o in verdict.opinions]}, "
            f"user_review={'yes' if verdict.requires_user_review else 'no'}"
        )

        return verdict

    def _opinion_cache_key(self, role: CouncilRole, proposal: str) -> str:
        """Generate a cache key for a council opinion based on role + proposal fingerprint."""
        category = self._classify_proposal(proposal)
        content = f"{role.value}:{category}:{proposal[:200]}"
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _cache_opinion(self, key: str, opinion: CouncilMemberOpinion) -> None:
        """Store an opinion in the LRU cache, evicting oldest if full."""
        if key in self._opinion_cache:
            self._opinion_cache.move_to_end(key)
        self._opinion_cache[key] = opinion
        while len(self._opinion_cache) > self._OPINION_CACHE_MAX:
            self._opinion_cache.popitem(last=False)

    async def _get_evaluation(
        self,
        role: CouncilRole,
        proposal: str,
        context: str,
    ) -> CouncilMemberOpinion:
        """Get a single council member's evaluation (with LRU caching)."""
        # Check opinion cache first
        cache_key = self._opinion_cache_key(role, proposal)
        if cache_key in self._opinion_cache:
            logger.debug(f"Council opinion cache hit: {role.value}")
            self._opinion_cache.move_to_end(cache_key)
            return self._opinion_cache[cache_key]

        role_prompt = ROLE_PROMPTS.get(role, "Evaluate this proposal.")

        full_prompt = f"""{role_prompt}

## Proposal
{proposal}

## Context
{context}

## Instructions
Provide your analysis, then {VOTE_PROMPT}
"""

        if not self._llm:
            opinion = self._generate_rule_based_opinion(role, proposal)
            self._cache_opinion(cache_key, opinion)
            return opinion

        try:
            response = await self._llm.generate(
                messages=[{"role": "user", "content": full_prompt}],
                model=self._model,
                temperature=0.4,
                max_tokens=_council_max_tokens("COUNCIL_EVAL_MAX_TOKENS", 800),
            )
            # generate() may return str or dict depending on provider
            content = response.get("content", "") if isinstance(response, dict) else str(response)
            opinion = self._parse_opinion(role, content)
            self._cache_opinion(cache_key, opinion)
            return opinion

        except Exception as e:
            logger.error(f"Council evaluation failed for {role.value}: {e}")
            opinion = self._generate_rule_based_opinion(role, proposal)
            self._cache_opinion(cache_key, opinion)
            return opinion

    async def _run_single_debate_round(
        self,
        opinions: list[CouncilMemberOpinion],
        proposal: str,
    ) -> list[CouncilMemberOpinion]:
        """Run one debate round where members can revise opinions after seeing others."""
        opinion_summary = "\n".join(
            f"- **{o.role.value}**: {o.vote.value} (confidence: {o.confidence:.0%}) — "
            + "; ".join(o.concerns[:2])
            for o in opinions
        )

        if not self._llm:
            return opinions

        semaphore = asyncio.Semaphore(self.MAX_PARALLEL_EVALUATIONS)

        async def revise(opinion: CouncilMemberOpinion) -> CouncilMemberOpinion:
            debate_prompt = f"""You previously voted **{opinion.vote.value}** on this proposal.

## Other Council Members' Positions
{opinion_summary}

## Your Original Concerns
{json.dumps(opinion.concerns)}

## Instructions
After seeing your colleagues' perspectives, would you change your vote or analysis?
If yes, provide updated analysis. If no, confirm your position.

{VOTE_PROMPT}
"""
            try:
                async with semaphore:
                    response = await self._llm.generate(
                        messages=[{"role": "user", "content": debate_prompt}],
                        model=self._model,
                        temperature=0.3,
                        max_tokens=_council_max_tokens("COUNCIL_DEBATE_MAX_TOKENS", 500),
                    )
                content = response.get("content", "") if isinstance(response, dict) else str(response)
                return self._parse_opinion(opinion.role, content)
            except Exception:
                return opinion

        return await asyncio.gather(*(revise(opinion) for opinion in opinions))

    async def _run_debate_round(
        self,
        opinions: list[CouncilMemberOpinion],
        proposal: str,
        max_rounds: int = 3,
    ) -> list[CouncilMemberOpinion]:
        """Run debate rounds with convergence detection.

        Runs up to max_rounds of debate but stops early when no votes change
        (convergence). This avoids wasting LLM calls on proposals where members
        already agree, while allowing more debate on contentious ones.
        """
        for round_num in range(max_rounds):
            previous_votes = [o.vote.value for o in opinions]

            opinions = await self._run_single_debate_round(opinions, proposal)

            current_votes = [o.vote.value for o in opinions]

            # Count vote changes
            changes = sum(1 for p, c in zip(previous_votes, current_votes) if p != c)

            await self._emit("council_debate_round", {
                "round": round_num + 1,
                "vote_changes": changes,
                "votes": current_votes,
            })

            if changes == 0:
                logger.info(f"Council debate converged after {round_num + 1} round(s)")
                break

            logger.info(f"Debate round {round_num + 1}: {changes} vote change(s)")

        return opinions

    def _synthesize_verdict(self, verdict: CouncilVerdict, category: str = "general") -> None:
        """
        Synthesize individual opinions into a collective verdict using
        adaptive expertise weighting.

        Each role's vote is weighted by their expertise relevance to the
        proposal category. A Security Reviewer's rejection on a security
        proposal carries 2x the weight of a User Advocate's approval.
        """
        opinions = verdict.opinions
        if not opinions:
            verdict.has_consensus = False
            verdict.requires_user_review = True
            verdict.review_reason = "No council opinions available"
            return

        # Weighted vote tallying
        weighted_approve = 0.0
        weighted_reject = 0.0
        weighted_conditional = 0.0
        total_weight = 0.0

        for o in opinions:
            if o.vote == VoteType.ABSTAIN:
                continue
            weight = self._get_role_weight(o.role, category)
            total_weight += weight
            if o.vote == VoteType.APPROVE:
                weighted_approve += weight
            elif o.vote == VoteType.REJECT:
                weighted_reject += weight
            elif o.vote == VoteType.CONDITIONAL:
                weighted_conditional += weight

        if total_weight == 0:
            verdict.has_consensus = False
            verdict.requires_user_review = True
            verdict.review_reason = "All members abstained"
            return

        # Determine majority using weighted fractions
        approve_fraction = (weighted_approve + weighted_conditional) / total_weight
        reject_fraction = weighted_reject / total_weight

        if approve_fraction >= self.CONSENSUS_THRESHOLD:
            if weighted_conditional > weighted_approve:
                verdict.consensus = VoteType.CONDITIONAL
            else:
                verdict.consensus = VoteType.APPROVE
            verdict.has_consensus = True
        elif reject_fraction >= self.CONSENSUS_THRESHOLD:
            verdict.consensus = VoteType.REJECT
            verdict.has_consensus = True
        else:
            verdict.has_consensus = False
            verdict.requires_user_review = True
            verdict.review_reason = "Council is divided — no clear consensus"

        # Weighted confidence average
        weighted_conf_sum = sum(
            o.confidence * self._get_role_weight(o.role, category)
            for o in opinions
        )
        total_all_weight = sum(self._get_role_weight(o.role, category) for o in opinions)
        verdict.consensus_confidence = weighted_conf_sum / total_all_weight if total_all_weight > 0 else 0.0

        # Find dissenters
        if verdict.consensus:
            for o in opinions:
                if o.vote != verdict.consensus and o.vote != VoteType.ABSTAIN:
                    verdict.dissenting_roles.append(o.role.value)

        # Synthesize concerns and suggestions (deduplicate)
        seen_concerns = set()
        seen_suggestions = set()
        for o in opinions:
            for c in o.concerns:
                key = c.lower().strip()[:50]
                if key not in seen_concerns:
                    seen_concerns.add(key)
                    verdict.synthesized_concerns.append(c)
            for s in o.suggestions:
                key = s.lower().strip()[:50]
                if key not in seen_suggestions:
                    seen_suggestions.add(key)
                    verdict.synthesized_suggestions.append(s)

        # Escalate to user if security concerns or critical rejection
        security_opinion = next((o for o in opinions if o.role == CouncilRole.SECURITY), None)
        if security_opinion and security_opinion.vote == VoteType.REJECT:
            verdict.requires_user_review = True
            verdict.review_reason = "Security reviewer rejected — user review required"

        if verdict.consensus == VoteType.REJECT:
            verdict.requires_user_review = True
            verdict.review_reason = "Council rejected the proposal"

    def _parse_opinion(self, role: CouncilRole, content: str) -> CouncilMemberOpinion:
        """Parse an LLM response into a CouncilMemberOpinion."""
        # Try to extract JSON
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(content[start:end])
                vote_str = data.get("vote", "abstain").lower()
                vote_map = {
                    "approve": VoteType.APPROVE,
                    "reject": VoteType.REJECT,
                    "conditional": VoteType.CONDITIONAL,
                    "abstain": VoteType.ABSTAIN,
                }
                return CouncilMemberOpinion(
                    role=role,
                    analysis=content[:start].strip() or data.get("summary", ""),
                    vote=vote_map.get(vote_str, VoteType.ABSTAIN),
                    confidence=float(data.get("confidence", 0.5)),
                    concerns=data.get("concerns", []),
                    conditions=data.get("conditions", []),
                    suggestions=data.get("suggestions", []),
                )
        except (json.JSONDecodeError, ValueError, KeyError):
            pass

        # Fallback: infer from text
        content_lower = content.lower()
        if "reject" in content_lower:
            vote = VoteType.REJECT
        elif "conditional" in content_lower:
            vote = VoteType.CONDITIONAL
        elif "approve" in content_lower or "looks good" in content_lower:
            vote = VoteType.APPROVE
        else:
            vote = VoteType.ABSTAIN

        return CouncilMemberOpinion(
            role=role,
            analysis=content[:500],
            vote=vote,
            confidence=0.5,
        )

    def _generate_rule_based_opinion(
        self,
        role: CouncilRole,
        proposal: str,
    ) -> CouncilMemberOpinion:
        """Generate a rule-based opinion when LLM is unavailable."""
        proposal_lower = proposal.lower()
        concerns = []
        suggestions = []
        vote = VoteType.CONDITIONAL

        if role == CouncilRole.SECURITY:
            if any(kw in proposal_lower for kw in ["password", "secret", "key", "token"]):
                concerns.append("Proposal involves sensitive data — ensure encryption")
            if "user input" in proposal_lower or "external" in proposal_lower:
                concerns.append("External input handling — validate and sanitize")
            if not concerns:
                vote = VoteType.APPROVE

        elif role == CouncilRole.IMPLEMENTER:
            if len(proposal) > 2000:
                concerns.append("Complex proposal — break into smaller increments")
                suggestions.append("Implement in stages with validation between stages")
            else:
                vote = VoteType.APPROVE
                suggestions.append("Add unit tests for new functionality")

        elif role == CouncilRole.ARCHITECT:
            if "database" in proposal_lower and "migration" in proposal_lower:
                concerns.append("Schema changes — ensure backward compatibility")
            suggestions.append("Document architectural decisions")
            vote = VoteType.CONDITIONAL

        elif role == CouncilRole.DEVILS_ADVOCATE:
            concerns.append("What's the simplest possible approach?")
            concerns.append("What if requirements change next week?")
            vote = VoteType.CONDITIONAL

        elif role == CouncilRole.USER_ADVOCATE:
            if not any(kw in proposal_lower for kw in ["ui", "ux", "user", "interface"]):
                concerns.append("No mention of user-facing impact")
            suggestions.append("Consider documenting changes for end users")
            vote = VoteType.APPROVE

        return CouncilMemberOpinion(
            role=role,
            analysis=f"Rule-based evaluation from {role.value} (LLM unavailable)",
            vote=vote,
            confidence=0.4,
            concerns=concerns,
            suggestions=suggestions,
        )
