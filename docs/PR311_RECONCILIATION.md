# PR #311 reconciliation with production truth (S11)

Date: 2026-08-25
Slice: S11 — Optional constrained authority and truthful PR #311
reconciliation (Task 12 of `docs/V0_6_PROOF_RELEASE_SOURCE_OF_TRUTH.md`).

## What PR #311 is

[PR #311](https://github.com/John-MiracleWorker/Kestrel/pull/311)
(`agent/adaptive-flock-launch`, "docs: launch Adaptive Flock with a
deterministic routing demo") is an **open, never-merged** pull request based
on v0.4.8. It adds 677 insertions across 6 files:

- `.github/ISSUE_TEMPLATE/provider-profile.yml` (+76)
- `.github/ISSUE_TEMPLATE/routing-trace.yml` (+82)
- `README.md` (+29) — a feature-table row plus an "Adaptive Flock: Route the
  Work, Preserve the Guardrails" section
- `docs/ADAPTIVE_FLOCK_DEMO.md` (+97)
- `scripts/demo_adaptive_flock.py` (+302)
- `tests/test_adaptive_flock_demo.py` (+91)

Its body states: "Adaptive Flock is implemented but difficult for a new
evaluator to see and verify quickly. This gives contributors a zero-credential
path to understand the safety boundary..." — no runtime defaults or routing
behavior are changed, and shadow mode remains the non-actionable default.

## What production truth is (verified at S10-qualified main `2764d02`)

- **Learned routing is inert in production by design.** `DurableRoutingCoordinator`
  accepts an `activation_evaluator`, but neither `server.py` nor `cli.py`
  wires one. Every real decision falls back to the deterministic static path
  and records `durable_grant_required` (`src/nested_memvid_agent/routing/coordinator.py:548`).
- **Qualification grants zero authority.** A qualification receipt alone never
  creates a grant; activation is a separate, exact, owner-confirmed
  transaction (`ActivationService.activate_scopes`).
- **v0.6 learned-authority class is narrow.** Per AUTH-002, the only class is
  an exact, owner-activated, low-risk **summarizer** scope.
- The demo script itself runs unchanged against current main and is genuinely
  provider-free, deterministic, and non-authoritative.

## What the reconciliation changed

The demo, docs, and README claims are brought in line with production truth.
The reconciled content lands through this S11 PR; PR #311 is superseded and
closed with a pointer. The specific changes:

1. **`scripts/demo_adaptive_flock.py`** — now emits a `production_truth`
   block in every report: `wired_activation_evaluator: false`,
   `live_grant: false`, `learned_authority_class: v06_low_risk_summarizer_only`,
   `deterministic_fallback_reason: durable_grant_required`, and an explicit
   "contract-level routing exercise only" claim. The `--mode` help and text
   renderer state the same truth. No routing behavior changed.
2. **`tests/test_adaptive_flock_demo.py`** — pins the original routing
   results (roles, targets, reason codes, safety, determinism) **and** the
   new production-truth block, so a future edit cannot silently overclaim.
3. **`docs/ADAPTIVE_FLOCK_DEMO.md`** — adds a "Production-truth
   reconciliation (S11 / PR #311)" section and reframes the mode table as
   contract-level. The "Safe runtime rollout" section no longer implies an
   operator can flip a mode to obtain production authority.
4. **`README.md`** — the feature-table row and the new section state that
   learned routing is inert until an exact owner-activated grant exists and
   that the demo grants no production authority.
5. **Issue forms** — carried forward unchanged (they are truthful and
   useful; both parse as YAML).

## AUTH-001..004 acceptance evidence

Implemented and pinned in `tests/test_v06_authority_class.py` (16 tests),
with supporting changes:

- **AUTH-001** — durable grant tests bind scope and the
  policy/inventory/config/receipt digests and reject stale or mismatched
  evidence (full binding matrix + stale-receipt rejection + scope-mismatch
  rejection). The evaluator's digest checks already existed from the AF
  chain; S11 pins them as AUTH acceptance evidence.
- **AUTH-002** — new `src/nested_memvid_agent/routing/v06_authority.py`
  defines the v0.6 authority class (low-risk summarizer only). Wired as an
  opt-in policy on `ActivationService` (out-of-class activation rejected)
  and `ActivationEvaluator` (out-of-class grants never effective, with a
  non-suspension `v06_authority_class_restricted` reason). Tests pin no
  default grant, owner-only activation, receipt-meets-thresholds, and
  unchanged capability boundary.
- **AUTH-003** — drift/suspension/kill-switch/revocation immediately
  restores deterministic routing for new decisions. The coordinator's
  shadow observation now records the durable
  `deterministic_fallback_after_suspension` authority label when a grant is
  suspended/revoked/kill-switched (previously reserved for AUTH-003 and
  never produced). Tests cover the durable label unit-level and
  end-to-end, plus a threaded concurrent-evaluator convergence test. The
  Workbench RoutingCenter already distinguishes the four states (S6).
- **AUTH-004** — `docs/RELEASE_CHECKLIST.md` gains a "v0.6 Learned
  Authority Qualification (AUTH-004)" section that records the qualification
  outcome without converting lack of activation evidence into a failure or
  weakening thresholds; shadow-only is a valid, expected outcome. Pinned by
  a checklist test.

## Not done and why

- No live grant is created. The task does not require one for release, and
  the evidence supports the shadow-only (inert) production posture.
- The SOT slice status is not flipped here; qualification closure
  (independent review, merge, SOT ledger row) is the orchestrator's
  follow-up, matching the S4–S10 pattern.
