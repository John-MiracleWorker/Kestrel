# Adaptive Flock v0.6 implementation evidence

Date: 2026-07-28

## Implemented

- Every real provider attempt now emits a redacted receipt with provider,
  model, completion state, normalized token usage, fallback metadata, and
  provider failure code. Task results retain both per-attempt receipts and an
  aggregate coverage record.
- Routing schema v2 adds immutable price snapshots, project/task-family/risk/
  capability scope, durable learned-shadow comparisons, and decayed target
  calibrations. Existing routing schema v1 databases migrate additively.
- Actual route cost is computed only when both token counts and the decision's
  snapshotted input/output rates are known. Missing price or usage data remains
  `null`; it is never interpreted as free.
- Learned examples are limited to actionable, acceptance-validated outcomes
  in the same project, task family, risk band, and capability set. Provider
  outages are measured separately from task-quality failures.
- Every new decision atomically stores static choice, learned counterfactual,
  actual choice, confidence, utility delta, cost coverage, savings estimate,
  activation/abstention reason, and replay configuration digest before model
  execution.
- Learned activation is limited to low/medium-risk contracts, cannot make a
  hard-filtered target eligible, and requires minimum total/per-target support,
  confidence, utility margin, cost coverage, and an explicit replay-verified
  runtime gate. Shadow and constrained modes never activate it.
- Retry routing now distinguishes transport, capability, and contract failure:
  transport retries the same target, capability failure requires a stronger
  eligible target, and contract failure requires replanning.
- Project-bound working, episodic, semantic, and procedural memory records are
  tagged at write time and filtered at the shared retrieval boundary. Global
  self/policy memory remains separate. Cross-project child-frame expansion and
  direct tool lookup are rejected.
- Routing Center now shows pricing inputs and a readable static/learned/actual
  comparison with outcome, confidence, evidence count, cost coverage, savings,
  regret, and scoped calibrations. Raw JSON remains under Advanced disclosure.

## Evidence

- Full Python suite: passed after this phase, with expected platform and opt-in
  integration skips.
- `ruff check .`: passed.
- `mypy src`: passed for 158 source files.
- Web tests: 79/79 passed across six files.
- Web production build: passed. The existing Vite warning for a primary bundle
  above 500 kB remains.
- Focused learned-routing tests prove price-snapshot cost attribution, decayed
  calibration, project isolation, explicit activation gating, hard
  abstention, and the transport/capability/contract retry ladder.
- Context-packing tests prove cross-project semantic recall and child-frame
  expansion are blocked while unscoped reusable knowledge and global self
  memory remain available.

## Qualification boundary

The repository contains the evidence gates needed for learned activation, but
the activation gate defaults off. No production policy was enabled and no
claim is made that a real repeated-task corpus already beats every baseline.
That claim requires a user-owned benchmark corpus with measured provider
pricing and validated outcomes.
