"""Deterministic qualification replay (Adaptive Flock plan, Task 12).

Replaying a qualification projection means re-evaluating the exact ordered
evidence set plus the snapshotted config digest ``repeats`` times (twenty by
default) and requiring one unique projection digest across every pass.

Invariants:

- The replay inputs are exactly the ordered evidence manifest and the config
  digest; database query order is never implicit.  Evidence is sorted once by
  the stable key ``(scope_digest, case_id, target_id, attempt_ordinal,
  attempt_id)`` and that ordered manifest digest is included in every replay
  projection.
- The reference time is frozen from the run snapshot by the caller: the
  replayer reads its injected clock once per repeat and binds the value into
  the projection, so any drift between repeats (decay, wall clock, or
  otherwise) produces a different digest and blocks qualification with an
  explicit ``replay_drift`` reason instead of silently qualifying.
- A repeat that raises never fabricates a projection: it counts as
  incomplete, and an incomplete replay never passes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from .qualification_digest import canonical_digest
from .qualification_evaluator import (
    ScopeEvaluationInput,
    ScopeQualificationResult,
    evaluate_scope,
)

__all__ = [
    "REPLAY_REASON_DRIFT",
    "REPLAY_REASON_INCOMPLETE",
    "QualificationReplayer",
    "ReplayResult",
]

REPLAY_REASON_DRIFT = "replay_drift"
REPLAY_REASON_INCOMPLETE = "replay_incomplete"


def _require_aware(moment: datetime) -> None:
    if (
        not isinstance(moment, datetime)
        or moment.tzinfo is None
        or moment.tzinfo.utcoffset(moment) is None
    ):
        raise ValueError("replay clock must return an aware datetime")


def _manifest_digest(bundles: tuple[ScopeEvaluationInput, ...]) -> str:
    """Digest of the exact ordered evidence set, sorted by the stable key."""

    entries = sorted(
        (
            {
                "scope_digest": attempt.scope_digest,
                "case_id": attempt.case_id,
                "target_id": attempt.target_id,
                "attempt_ordinal": attempt.attempt_ordinal,
                "attempt_id": attempt.attempt_id,
            }
            for bundle in bundles
            for attempt in bundle.attempts
        ),
        key=lambda entry: (
            entry["scope_digest"],
            entry["case_id"],
            entry["target_id"],
            entry["attempt_ordinal"],
            entry["attempt_id"],
        ),
    )
    return canonical_digest(entries)


def _config_digest(bundles: tuple[ScopeEvaluationInput, ...]) -> str:
    """Digest of the snapshotted scope/static-target/thresholds config."""

    return canonical_digest(
        [
            {
                "scope_digest": bundle.scope.digest,
                "static_target_id": bundle.static_target_id,
                "thresholds_digest": bundle.thresholds.digest,
            }
            for bundle in bundles
        ]
    )


@dataclass(frozen=True)
class ReplayResult:
    """Outcome of replaying one evidence set ``repeats`` times."""

    repeats: int
    completed_repeats: int
    successes_required: int
    projection_digests: tuple[str, ...]
    results: tuple[ScopeQualificationResult, ...]
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.repeats, bool) or not isinstance(self.repeats, int) or self.repeats < 1:
            raise ValueError("repeats must be a positive integer")
        if (
            isinstance(self.completed_repeats, bool)
            or not isinstance(self.completed_repeats, int)
            or not 0 <= self.completed_repeats <= self.repeats
        ):
            raise ValueError("completed_repeats must be between 0 and repeats")
        if (
            isinstance(self.successes_required, bool)
            or not isinstance(self.successes_required, int)
            or not 0 <= self.successes_required <= self.repeats
        ):
            raise ValueError("successes_required must be between 0 and repeats")
        if len(self.projection_digests) != self.completed_repeats:
            raise ValueError("projection_digests must cover every completed repeat")

    @property
    def unique_projection_digests(self) -> int:
        return len(set(self.projection_digests))

    @property
    def passed(self) -> bool:
        return (
            self.completed_repeats >= self.successes_required
            and self.unique_projection_digests == 1
        )


class QualificationReplayer:
    """Replay scope projections and demand byte-identical digests."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        if clock is not None and not callable(clock):
            raise ValueError("clock must be callable")
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))

    def replay(
        self,
        bundles: Sequence[ScopeEvaluationInput],
        *,
        repeats: int = 20,
        successes_required: int | None = None,
    ) -> ReplayResult:
        """Evaluate ``bundles`` ``repeats`` times and compare digests.

        Every repeat reads the injected clock once; the caller freezes that
        clock from the run snapshot so decay cannot change between repeats.
        """

        if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
            raise ValueError("repeats must be a positive integer")
        if successes_required is None:
            successes_required = repeats
        if (
            isinstance(successes_required, bool)
            or not isinstance(successes_required, int)
            or not 0 <= successes_required <= repeats
        ):
            raise ValueError("successes_required must be between 0 and repeats")
        ordered = tuple(bundles)
        if not ordered:
            raise ValueError("replay requires at least one scope evaluation input")
        for bundle in ordered:
            if not isinstance(bundle, ScopeEvaluationInput):
                raise ValueError("bundles must be ScopeEvaluationInput values")
        manifest_digest = _manifest_digest(ordered)
        config_digest = _config_digest(ordered)
        digests: list[str] = []
        results: tuple[ScopeQualificationResult, ...] = ()
        completed = 0
        for _ in range(repeats):
            reference_time = self._clock()
            _require_aware(reference_time)
            try:
                repeat_results = tuple(evaluate_scope(bundle) for bundle in ordered)
            except Exception:  # noqa: BLE001 - a failed repeat never fabricates a projection
                continue
            digests.append(
                canonical_digest(
                    {
                        "config_digest": config_digest,
                        "manifest_digest": manifest_digest,
                        "reference_time": reference_time.isoformat(),
                        "scope_projections": [
                            result.projection_digest for result in repeat_results
                        ],
                    }
                )
            )
            results = repeat_results
            completed += 1
        reasons: list[str] = []
        if completed < successes_required:
            reasons.append(REPLAY_REASON_INCOMPLETE)
        if len(set(digests)) > 1:
            reasons.append(REPLAY_REASON_DRIFT)
        return ReplayResult(
            repeats=repeats,
            completed_repeats=completed,
            successes_required=successes_required,
            projection_digests=tuple(digests),
            results=results,
            reasons=tuple(reasons),
        )
