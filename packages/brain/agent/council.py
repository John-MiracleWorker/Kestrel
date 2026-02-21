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

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger("brain.agent.council")


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
      1. Present proposal to all council members
      2. Each member independently evaluates
      3. Members critique each other's evaluations (optional debate round)
      4. Final vote
      5. Synthesize verdict
    """

    # Minimum members needed for quorum
    QUORUM = 3
    # Consensus requires this fraction of approvals
    CONSENSUS_THRESHOLD = 0.6

    def __init__(
        self,
        llm_provider=None,
        model: str = "",
        roles: list[CouncilRole] = None,
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

    async def deliberate(
        self,
        proposal: str,
        context: str = "",
        include_debate: bool = True,
    ) -> CouncilVerdict:
        """
        Run the full council deliberation process.

        Args:
            proposal: The plan/approach to evaluate
            context: Additional context (task goal, constraints, etc.)
            include_debate: Whether to run a debate round between members

        Returns:
            CouncilVerdict with consensus and synthesized feedback
        """
        verdict = CouncilVerdict(proposal=proposal)

        # ── Phase 1: Independent evaluation ──────────────────────
        opinions = []
        for role in self._roles:
            opinion = await self._get_evaluation(role, proposal, context)
            opinions.append(opinion)

        # ── Phase 2: Cross-critique (debate round) ───────────────
        if include_debate and len(opinions) >= 2:
            opinions = await self._run_debate_round(opinions, proposal)

        verdict.opinions = opinions

        # ── Phase 3: Synthesize verdict ──────────────────────────
        self._synthesize_verdict(verdict)

        logger.info(
            f"Council verdict: consensus={'yes' if verdict.has_consensus else 'no'}, "
            f"votes={[o.vote.value for o in verdict.opinions]}, "
            f"user_review={'yes' if verdict.requires_user_review else 'no'}"
        )

        return verdict

    async def _get_evaluation(
        self,
        role: CouncilRole,
        proposal: str,
        context: str,
    ) -> CouncilMemberOpinion:
        """Get a single council member's evaluation."""
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
            return self._generate_rule_based_opinion(role, proposal)

        try:
            response = await self._llm.generate(
                messages=[{"role": "user", "content": full_prompt}],
                model=self._model,
                temperature=0.4,
                max_tokens=1500,
            )
            content = response.get("content", "")
            return self._parse_opinion(role, content)

        except Exception as e:
            logger.error(f"Council evaluation failed for {role.value}: {e}")
            return self._generate_rule_based_opinion(role, proposal)

    async def _run_debate_round(
        self,
        opinions: list[CouncilMemberOpinion],
        proposal: str,
    ) -> list[CouncilMemberOpinion]:
        """Run a debate round where members can revise opinions after seeing others."""
        # Show each member a summary of other opinions
        opinion_summary = "\n".join(
            f"- **{o.role.value}**: {o.vote.value} (confidence: {o.confidence:.0%}) — "
            + "; ".join(o.concerns[:2])
            for o in opinions
        )

        revised = []
        for opinion in opinions:
            if not self._llm:
                revised.append(opinion)
                continue

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
                response = await self._llm.generate(
                    messages=[{"role": "user", "content": debate_prompt}],
                    model=self._model,
                    temperature=0.3,
                    max_tokens=1000,
                )
                revised_opinion = self._parse_opinion(opinion.role, response.get("content", ""))
                revised.append(revised_opinion)
            except Exception:
                revised.append(opinion)  # Keep original if debate fails

        return revised

    def _synthesize_verdict(self, verdict: CouncilVerdict) -> None:
        """Synthesize individual opinions into a collective verdict."""
        opinions = verdict.opinions
        if not opinions:
            verdict.has_consensus = False
            verdict.requires_user_review = True
            verdict.review_reason = "No council opinions available"
            return

        # Count votes
        vote_counts = {VoteType.APPROVE: 0, VoteType.REJECT: 0,
                       VoteType.CONDITIONAL: 0, VoteType.ABSTAIN: 0}
        for o in opinions:
            vote_counts[o.vote] = vote_counts.get(o.vote, 0) + 1

        total_voting = len(opinions) - vote_counts[VoteType.ABSTAIN]
        if total_voting == 0:
            verdict.has_consensus = False
            verdict.requires_user_review = True
            verdict.review_reason = "All members abstained"
            return

        # Determine majority
        approve_fraction = (vote_counts[VoteType.APPROVE] + vote_counts[VoteType.CONDITIONAL]) / total_voting

        if approve_fraction >= self.CONSENSUS_THRESHOLD:
            if vote_counts[VoteType.CONDITIONAL] > vote_counts[VoteType.APPROVE]:
                verdict.consensus = VoteType.CONDITIONAL
            else:
                verdict.consensus = VoteType.APPROVE
            verdict.has_consensus = True
        elif (vote_counts[VoteType.REJECT] / total_voting) >= self.CONSENSUS_THRESHOLD:
            verdict.consensus = VoteType.REJECT
            verdict.has_consensus = True
        else:
            verdict.has_consensus = False
            verdict.requires_user_review = True
            verdict.review_reason = "Council is divided — no clear consensus"

        # Average confidence
        verdict.consensus_confidence = sum(o.confidence for o in opinions) / len(opinions)

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
