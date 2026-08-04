"""Transactional hard-cap admission for Flock qualification (Task 7).

Admission is atomic, capped exactly, and fail-closed:

- reserve projection and the attempt ``pending -> reserved`` transition
  happen in one SQLite transaction (see
  :meth:`QualificationLedger.admit_attempt_with_budget`);
- admission requires ``known_actual_spend + unresolved_cost_reserve +
  admitted_inflight_reserve + projected_attempt_reserve <=
  min(immutable_max_spend, effective_stop_cap)``;
- unknown prices reject admission before any provider contact and are never
  converted to zero; an explicit non-billed local price is a known zero;
- missing usage keeps the full reserve as unresolved cost; ambiguous
  transport outcomes do the same; only a provider-accepted-proof zero-token
  transport failure releases the reserve;
- an actual above the reserve charges the actual, records
  ``budget_projection_overrun``, and stops admissions fail-closed.

Cost coverage is the share of terminal live (``real_project``) attempt
units with attributable known cost. Synthetic fixtures never increase
production cost coverage.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .qualification_ledger import QualificationLedger
from .qualification_models import MoneyMicros, PriceSnapshot
from .qualification_records import (
    TERMINAL_ATTEMPT_STATES,
    QualificationAttempt,
    QualificationAttemptDraft,
    QualificationRun,
)

__all__ = [
    "DEFAULT_IMMUTABLE_MAX_SPEND_MICROS",
    "AttemptTokenCeilings",
    "BudgetAdmissionRejected",
    "QualificationBudget",
    "QualificationBudgetState",
]

DEFAULT_IMMUTABLE_MAX_SPEND_MICROS = 50_000_000

_MICROS_PER_MILLION_TOKENS = 1_000_000


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def _usage_tokens(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"usage {key} must be a non-negative integer")
    return value


class BudgetAdmissionRejected(RuntimeError):
    """Admission failed closed before any provider contact."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(reason if not detail else f"{reason}: {detail}")


@dataclass(frozen=True)
class AttemptTokenCeilings:
    """Immutable per-attempt token ceilings used for reserve projection."""

    max_input_tokens: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        for name in ("max_input_tokens", "max_output_tokens"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class QualificationBudgetState:
    """Exact budget accounting snapshot for one qualification run."""

    run_id: str
    max_spend_micros: int
    effective_stop_cap_micros: int
    known_actual_spend_micros: int
    unresolved_cost_reserve_micros: int
    admitted_inflight_reserve_micros: int
    cost_coverage: float
    admissions_open: bool


class QualificationBudget:
    """Hard-cap admission service bound to one live qualification run."""

    def __init__(
        self,
        ledger: QualificationLedger,
        run_id: str,
        *,
        prices: Mapping[str, PriceSnapshot],
        token_ceilings: AttemptTokenCeilings,
    ) -> None:
        if not isinstance(ledger, QualificationLedger):
            raise ValueError("ledger must be a QualificationLedger")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("run_id is required")
        if ledger.get_run(run_id) is None:
            raise ValueError(f"unknown qualification run: {run_id}")
        for target_id, price in prices.items():
            if not isinstance(price, PriceSnapshot):
                raise ValueError("prices must be PriceSnapshot values")
            if price.target_id != target_id:
                raise ValueError("price snapshot must belong to its target key")
        if not isinstance(token_ceilings, AttemptTokenCeilings):
            raise ValueError("token_ceilings must be an AttemptTokenCeilings value")
        self._ledger = ledger
        self._run_id = run_id
        self._prices = dict(prices)
        self._token_ceilings = token_ceilings

    @property
    def ledger(self) -> QualificationLedger:
        return self._ledger

    @property
    def run_id(self) -> str:
        return self._run_id

    # -- reserve projection -------------------------------------------------

    def estimate_attempt_reserve(self, target_id: str) -> MoneyMicros:
        """Rounded-up reserve from snapshotted prices and token ceilings.

        An explicit non-billed local price is a known zero reserve; an
        unknown price rejects before provider contact.
        """

        price = self._trustworthy_price(target_id)
        if price.is_known_zero:
            return MoneyMicros(0)
        assert price.input_per_million is not None
        assert price.output_per_million is not None
        input_reserve = _ceil_div(
            price.input_per_million.micros * self._token_ceilings.max_input_tokens,
            _MICROS_PER_MILLION_TOKENS,
        )
        output_reserve = _ceil_div(
            price.output_per_million.micros * self._token_ceilings.max_output_tokens,
            _MICROS_PER_MILLION_TOKENS,
        )
        return MoneyMicros(input_reserve + output_reserve)

    # -- admission ------------------------------------------------------------

    def admit_attempt(self, draft: QualificationAttemptDraft) -> QualificationAttempt:
        """Admit an attempt atomically under the exact hard-cap condition."""

        if not isinstance(draft, QualificationAttemptDraft):
            raise ValueError("draft must be a QualificationAttemptDraft")
        self._trustworthy_price(draft.target_id)
        return self._ledger.admit_attempt_with_budget(draft)

    # -- settlement -----------------------------------------------------------

    def settle_attempt(
        self,
        attempt_id: str,
        *,
        usage: Mapping[str, Any] | None = None,
        actual_cost: MoneyMicros | None = None,
        transport_failure: bool = False,
        zero_tokens_confirmed: bool = False,
        validation_passed: bool = True,
        validation_codes: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
        guardrail_violated: bool = False,
    ) -> QualificationBudgetState:
        """Settle one admitted attempt and return the reconciled state."""

        attempt = self._ledger.get_attempt(attempt_id)
        if attempt is None:
            raise ValueError(f"unknown qualification attempt: {attempt_id}")
        if attempt.run_id != self._run_id:
            raise ValueError("attempt does not belong to this budget run")
        priced_actual: MoneyMicros | None = None
        final_validation_passed = validation_passed
        final_codes = tuple(validation_codes)
        if transport_failure:
            outcome = (
                "transport_confirmed_zero"
                if zero_tokens_confirmed
                else "ambiguous"
            )
        elif usage is None:
            outcome = "missing_usage"
        else:
            priced_actual = self._priced_actual(attempt.target_id, usage, actual_cost)
            if priced_actual.micros > attempt.reservation.micros:
                outcome = "overrun"
                final_validation_passed = False
                final_codes = tuple(
                    sorted(set(final_codes) | {"budget_projection_overrun"})
                )
            else:
                outcome = "completed"
        self._ledger.settle_attempt_with_budget(
            attempt_id,
            outcome=outcome,
            usage=usage,
            actual_cost=priced_actual,
            validation_passed=final_validation_passed,
            validation_codes=final_codes,
            evidence_refs=evidence_refs,
            guardrail_violated=guardrail_violated,
        )
        return self.state()

    # -- stop cap ---------------------------------------------------------------

    def lower_effective_stop_cap(self, new_cap: MoneyMicros) -> QualificationBudgetState:
        """Tighten the effective stop cap; it can never be raised after start."""

        if not isinstance(new_cap, MoneyMicros):
            raise ValueError("new_cap must be a MoneyMicros value")
        run = self._require_run()
        if new_cap.micros > run.effective_stop_cap.micros:
            raise ValueError(
                "effective stop cap cannot be raised after the run started"
            )
        if new_cap.micros < run.effective_stop_cap.micros:
            self._ledger.update_effective_stop_cap(
                self._run_id,
                expected_revision=run.revision,
                new_cap=new_cap,
            )
        return self.state()

    # -- state ------------------------------------------------------------------

    def state(self) -> QualificationBudgetState:
        run = self._require_run()
        covered, total = self._cost_coverage_units(run)
        return QualificationBudgetState(
            run_id=self._run_id,
            max_spend_micros=run.max_spend.micros,
            effective_stop_cap_micros=run.effective_stop_cap.micros,
            known_actual_spend_micros=run.actual_spend.micros,
            unresolved_cost_reserve_micros=run.unresolved_reserve.micros,
            admitted_inflight_reserve_micros=run.inflight_reserve.micros,
            cost_coverage=0.0 if total == 0 else covered / total,
            admissions_open=run.effective_stop_cap.micros > 0 and not run.is_terminal,
        )

    # -- internals ----------------------------------------------------------------

    def _require_run(self) -> QualificationRun:
        run = self._ledger.get_run(self._run_id)
        if run is None:
            raise ValueError(f"unknown qualification run: {self._run_id}")
        return run

    def _trustworthy_price(self, target_id: str) -> PriceSnapshot:
        price = self._prices.get(target_id)
        if price is None or not price.is_trustworthy:
            raise BudgetAdmissionRejected(
                "price_unknown",
                f"target {target_id} has no trustworthy price snapshot; "
                "unknown is not zero",
            )
        return price

    def _priced_actual(
        self,
        target_id: str,
        usage: Mapping[str, Any],
        actual_cost: MoneyMicros | None,
    ) -> MoneyMicros:
        """Exact rounded-up actual cost from reported usage and the snapshot."""

        price = self._trustworthy_price(target_id)
        if price.is_known_zero:
            computed = MoneyMicros(0)
        else:
            assert price.input_per_million is not None
            assert price.output_per_million is not None
            input_cost = _ceil_div(
                price.input_per_million.micros * _usage_tokens(usage, "input_tokens"),
                _MICROS_PER_MILLION_TOKENS,
            )
            output_cost = _ceil_div(
                price.output_per_million.micros
                * _usage_tokens(usage, "output_tokens"),
                _MICROS_PER_MILLION_TOKENS,
            )
            computed = MoneyMicros(input_cost + output_cost)
        if actual_cost is not None and actual_cost != computed:
            raise ValueError(
                "actual_cost does not match the priced usage from the snapshot"
            )
        return computed

    def _cost_coverage_units(self, run: QualificationRun) -> tuple[int, int]:
        """Terminal live attempt units with/without attributable known cost.

        Live units are attempts on ``real_project`` corpus items; synthetic
        fixtures are excluded from both numerator and denominator.
        """

        corpus = json.loads(run.corpus_json)
        items = corpus.get("items", []) if isinstance(corpus, dict) else []
        live_item_ids = {
            str(item["item_id"])
            for item in items
            if isinstance(item, dict) and item.get("evidence_kind") == "real_project"
        }
        terminal = ", ".join(f"'{status}'" for status in TERMINAL_ATTEMPT_STATES)
        with self._ledger.state._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT a.actual_cost_micros AS actual_cost_micros,
                       c.item_id AS item_id
                FROM routing_qualification_attempts a
                JOIN routing_qualification_cases c ON c.case_id = a.case_id
                WHERE a.run_id = ? AND a.status IN ({terminal})
                """,
                (self._run_id,),
            ).fetchall()
        total = 0
        covered = 0
        for row in rows:
            if str(row["item_id"]) not in live_item_ids:
                continue
            total += 1
            if row["actual_cost_micros"] is not None:
                covered += 1
        return covered, total
