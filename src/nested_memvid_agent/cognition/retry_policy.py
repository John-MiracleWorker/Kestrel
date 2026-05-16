from __future__ import annotations

import json
from difflib import SequenceMatcher

from ..runtime_models import StrategyProposal, ToolCall, ToolExecution
from .models import RetryDecision, StrategyDiff

_NON_RETRYABLE_TOOL_ERRORS = {
    "approval_pending",
    "approval_required",
    "tool_disabled",
}

_WEAK_STRATEGY_MARKERS = {
    "retry",
    "try again",
    "same again",
    "run again",
    "do it again",
    "confidence",
}


class RetryPolicy:
    """Blocks same-action retries unless the next attempt has a real strategy change."""

    def assess_call(
        self,
        call: ToolCall,
        previous_executions: tuple[ToolExecution, ...] | list[ToolExecution],
        *,
        similar_lessons: tuple[str, ...] = (),
    ) -> RetryDecision:
        failed = [
            execution
            for execution in previous_executions
            if not execution.success and execution.error not in _NON_RETRYABLE_TOOL_ERRORS
        ]
        previous = next((execution for execution in reversed(failed) if execution.call.name == call.name), None)
        if previous is None:
            return RetryDecision(True, "No failed same-tool action in this turn.", similar_lessons=similar_lessons)
        previous_action = action_signature(previous.call)
        new_action = action_signature(call)
        require_change = previous_action == new_action
        if not require_change:
            diff = StrategyDiff(
                previous_action=previous_action,
                new_action=new_action,
                difference="Tool arguments changed after the failed attempt.",
                is_meaningfully_different=True,
            )
            return RetryDecision(True, "Tool arguments changed after failure.", strategy_diff=diff, similar_lessons=similar_lessons)
        return self.assess_actions(
            previous_action=previous_action,
            new_action=new_action,
            strategy=call.strategy,
            require_change=True,
            similar_lessons=similar_lessons,
        )

    def assess_actions(
        self,
        *,
        previous_action: str,
        new_action: str,
        strategy: StrategyProposal | None,
        require_change: bool,
        similar_lessons: tuple[str, ...] = (),
    ) -> RetryDecision:
        if not require_change:
            diff = StrategyDiff(
                previous_action=previous_action,
                new_action=new_action,
                difference="Retry policy did not require a changed strategy for this action.",
                is_meaningfully_different=True,
            )
            return RetryDecision(True, "No changed-strategy gate was triggered.", strategy_diff=diff, similar_lessons=similar_lessons)
        if strategy is None or not strategy.changed_strategy.strip():
            return RetryDecision(
                False,
                "Same action failed before and no changed strategy was supplied.",
                required_change="Provide changed_strategy, why_different, expected_signal, and fallback_if_fails before retrying.",
                strategy_diff=StrategyDiff(previous_action, new_action, "No strategy supplied.", False),
                similar_lessons=similar_lessons,
            )
        strategy_text = strategy.changed_strategy.strip()
        ratio = SequenceMatcher(a=previous_action.lower(), b=strategy_text.lower()).ratio()
        weak = _is_weak_strategy(strategy_text)
        meaningful = len(strategy_text) >= 24 and ratio < 0.92 and not weak
        diff_text = (
            strategy.why_different.strip()
            or "A changed strategy was supplied for the repeated action."
            if meaningful
            else "Strategy text is too weak or too similar to the failed action."
        )
        diff = StrategyDiff(
            previous_action=previous_action,
            new_action=new_action,
            difference=diff_text,
            is_meaningfully_different=meaningful,
        )
        if not meaningful:
            return RetryDecision(
                False,
                "Retry denied: strategy is not meaningfully different from the failed attempt.",
                required_change="Describe a concrete changed action, narrower target, new evidence source, or different hypothesis.",
                strategy_diff=diff,
                similar_lessons=similar_lessons,
            )
        return RetryDecision(
            True,
            "Changed strategy supplied; retry may proceed.",
            strategy_diff=diff,
            similar_lessons=similar_lessons,
        )


def action_signature(call: ToolCall) -> str:
    return f"{call.name} {json.dumps(call.arguments, sort_keys=True)}"


def _is_weak_strategy(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in _WEAK_STRATEGY_MARKERS:
        return True
    return any(marker == lowered for marker in _WEAK_STRATEGY_MARKERS)
