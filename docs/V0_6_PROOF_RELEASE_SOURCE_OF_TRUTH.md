# Kestrel v0.6 Proof Release — Source of Truth

Last updated: 2026-08-10

This document is the canonical program contract for the Kestrel v0.6 proof
release. It translates the owner-approved release prompt into durable,
reviewable requirements. Plans, pull requests, dashboards, release notes, and
agent summaries may link here, but they must not silently redefine these
requirements or claim a stronger state than the evidence recorded here.

## Status vocabulary

Every requirement and delivery slice uses exactly one status:

- `not_started` — no implementation evidence has been accepted.
- `in_progress` — implementation or qualification is active, but required
  evidence is missing.
- `blocked` — a named external or technical dependency prevents progress.
- `qualified` — the change is merged at an exact SHA and every required local,
  hosted, artifact, review, and owner-controlled gate has a durable receipt.

Focused tests, a locally clean diff, a candidate artifact, a preview, or a
single green hosted run are evidence inputs. None is independently sufficient
to mark a release requirement `qualified`.

## Reflection protocol

Every v0.6 slice must:

1. Read this document before editing.
2. Refresh `origin/main`, relevant issues/PRs, and hosted workflow state.
3. Identify the requirement IDs it changes and avoid unrelated cleanup.
4. Use test-driven development for behavior changes.
5. Record exact commands, subject SHA, merge status, and the merged SHA only
   when merged; also record workflow URLs, artifact/receipt digests, review
   outcome, limitations, and owner gates in the evidence ledger.
6. Run `pytest -q` after the coherent phase, plus the subsystem-specific gates.
7. Update the requirement and slice status only after evaluating the evidence
   against the full acceptance criterion.

## North star

A technically competent developer who has not seen Kestrel before can install
the supported v0.6 artifact, add a repository, enter a meaningful engineering
objective, and observe one coherent bounded mission:

```text
Repo -> Objective -> Plan -> Work -> Proof -> Ship -> Learn
```

Kestrel must understand the repository, compile a bounded task contract, make
its routing authority visible, execute in an isolated boundary, validate and
independently review the change, explain the evidence and uncertainty, request
approval, create an approved local commit or separately approved pull request,
write a durable run capsule, propose only evidence-backed learning, and prove
that a later similar task used an accepted lesson.

## Release priorities

Work follows this hard execution order; a slice may not begin until its listed
predecessor is complete. The foundational release transaction, rehearsal, and
installed-artifact work is reliability infrastructure, not final release
qualification or publication:

1. S0 canonical program record and S1 deterministic runtime reliability.
2. S2 exact-SHA candidate/promotion transaction and S3 repeated release
   rehearsal plus installed-artifact mission matrix.
3. S4 live reviewer separation, S5 zero-authority production shadow
   observation, and S6 shadow comparison evidence.
4. S7 durable mission proof and S8 golden end-to-end engineering journey.
5. S9 benchmark fairness repair and S10 reproducible benchmark breadth.
6. S11 optional constrained Adaptive Flock authority.
7. S12 final exact-artifact v0.6 qualification, owner approval, promotion,
   publication, and post-publication verification.

No large feature family may jump this sequence merely because it is easier to
demo or already partially implemented. S12 is the final gate and is the only
slice that may qualify and publish the v0.6 release.

## Non-negotiable engineering boundaries

- Preserve the local-first, trusted-single-owner/private-node profile.
- Use Memvid v2 `.mv2` files only, normally one permanent file per nested
  memory layer. Never call `create(path)` on an existing `.mv2` file.
- Keep SQLite as control-plane state and Memvid as canonical retrieval memory.
- Keep the conversational CLI and deterministic mock backend/LLM functional.
- UI state never invents server authority.
- High-risk operations require explicit config enablement and then human,
  interactive, exact-call approval before each call.
- Raw secrets never enter model prompts or context, memory, logs, errors,
  renderers, or public APIs; only secret-safe metadata, handles, and receipts
  may appear.
- One ordinary event, one shadow observation, qualification, or activation
  never writes policy memory.
- Every promotion carries evidence, provenance, confidence, validation status,
  and the applicable receipt bindings.
- Learned routing qualification grants zero authority. Owner activation is a
  separate, durable, exact-scope decision.
- High/critical-risk routing remains deterministic for v0.6.
- Drift, suspension, or revocation immediately returns new decisions to the
  deterministic fallback.
- Public claims must be no stronger than reproducible artifacts prove.
- Remote mutation is disabled by default. Local commits are allowed within the
  approved task boundary; only a separately credentialed, human-interactively
  exact-approved PR/release workflow may mutate a remote. Agents never push
  directly to protected `main`.

## Audited baseline

Baseline subject: `f78ef1b4a54d63b0e49787b80a67133ba2ae4268`
(`origin/main`, v0.5.8 snapshot inspected 2026-08-09/10).

| Area | Exists at baseline | Unqualified gap |
| --- | --- | --- |
| Golden determinism | Seeded isolated cases, canonical projection comparison, a 20-repeat Ubuntu/memory workflow, and exact-SHA release receipt checking. | Issue #303 remains open; Memvid and cross-platform repetition do not prove the reported retrieval/settlement failure mode is gone. |
| Windows/channel runtime | Native Windows source CI, monotonic heartbeat polling, publication fences, and truthful channel follow-up behavior. | Issue #308 remains open; installed Windows artifacts do not start the server and exercise channel ingress, and one full local Python 3.13 baseline exposed two host-subprocess test failures. |
| Release mechanics | Exact-SHA prerequisite checks, a local disposable release rehearsal, exact-wheel matrix, artifact digests, and owner-protected PyPI deployment. | Validation still begins from a stable tag; the rehearsal is one local simulation; already artifact-validated exact artifacts are not promoted by a pre-tag transaction. |
| Adaptive Flock | Durable policies/inventory, qualification, replay, grants, drift/suspension/revocation, static fallback, opt-in shadow/constrained/adaptive modes, cost/outcome evidence. | Default runtime is off; ordinary durable tasks create no shadow observation; current shadow rows are not a complete production comparison; live reviewer diversity is not wired. |
| Mission engineering flow | Project/preflight binding checks, durable task DAGs, repair worktrees, validation/review receipts, approvals, commit/PR workflows, capsules, and learning primitives. | No durable server-authored proof aggregate joins the admitted mission to all evidence after reload; no packaged two-task demonstration proves receipt-bound lesson reuse. |
| Memory benchmark | Existing synthetic/unified benchmarks and an open transcript benchmark PR. | PR #328 uses an oracle layer hint, per-layer rather than global top-k, non-standard IDF, one narrow seed/corpus, and outcome-shaped stress data. |

Baseline validation receipt (`BASE-2026-08-10-A`):

- Worktree: `/Users/tiuni/.codex/worktrees/kestrel-v06-proof-release`
- Branch: `codex/v0.6-proof-release`
- Python: CPython 3.13.12 from the locked `.venv`
- `python -m compileall -q src tests scripts`: exit 0
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q`: exit 1 with two
  failures in `tests/test_tools.py` (`same_public_call_id...` and
  `subprocess_tool_timeout...`). This is a failed baseline, not release
  evidence; root-cause repair and a fresh full run are required.

Baseline follow-up receipt (`BASE-2026-08-10-B`):

- Subject: `9d8bc3d891859a0598350364f3f30e320814157b` (post-baseline
  host-interpreter fixture repair).
- `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`: exit 0.
- OCI-backed memory/mock golden validation with the pinned Python digest: 21/21
  passed; CLI mock chat completed.
- This supersedes neither the failed `BASE-2026-08-10-A` receipt nor the
  remaining hosted/cross-platform repeat requirements. REL/S1 remain
  `in_progress`.

## Requirement register

### Reliability (`REL`)

| ID | Requirement | Acceptance evidence | Status |
| --- | --- | --- | --- |
| REL-001 | Eliminate golden-eval nondeterminism rather than masking it with reruns/timeouts. | Regression reproduces the retrieval/settlement defect; fixed fixture sealing, seeds, clocks, IDs, ordering, and completion; exact-SHA memory and Memvid repeat receipts. | `in_progress` |
| REL-002 | Remove Windows channel/full-runtime timing flakes with explicit synchronization. | Event/state-driven tests and 20 consecutive Windows/macOS/Linux targeted iterations with no rerun; structured failure diagnostics. | `in_progress` |
| REL-003 | Use monotonic elapsed-time logic and explicitly await asynchronous state transitions. | Static/test coverage for every changed timing path; no wall-clock equality or timing-point authority assertion. | `in_progress` |
| REL-004 | Rehearse the release lifecycle repeatedly. | One exact candidate produces 20 consecutive unique-namespace rehearsals, zero flaky failures, and an aggregate receipt digest. | `not_started` |
| REL-005 | Prove fresh artifact install, launch, and first mission on supported platforms. | Windows/macOS/Linux, Python 3.11–3.13 exact-wheel matrix starts the installed entry point, awaits readiness, completes a mock mission, and verifies cleanup. | `not_started` |

### Release transaction (`RELEASE`)

| ID | Requirement | Acceptance evidence | Status |
| --- | --- | --- | --- |
| RELEASE-001 | Qualify an exact full SHA reachable from protected `main` before creating a stable tag. | Candidate manifest binds version, SHA, tree, checks, attestations, and artifact digests; conflicting/stale candidates fail closed. | `not_started` |
| RELEASE-002 | Build once and promote the already artifact-validated exact artifacts. | Owner-protected promotion creates an annotated tag bound to the manifest and publishes the same verified digests without rebuilding. | `not_started` |
| RELEASE-003 | Make promotion rerunnable without burning a new version. | Same-tag/same-digest retries are idempotent; tag, source, artifact, or partial-publication conflicts stop with a reconciliation record. | `not_started` |

### Production shadow routing (`SHADOW`)

| ID | Requirement | Acceptance evidence | Status |
| --- | --- | --- | --- |
| SHADOW-001 | Observe every eligible durable scheduler/subagent planner, executor, reviewer, and summarizer attempt without altering execution. | Compiled contract, at least two policy-admissible targets, explicit eligibility/exclusion reasons, and tests proving byte-identical execution config/no alternate provider call. | `not_started` |
| SHADOW-002 | Persist actual authority, actual target, shadow recommendation, candidates, qualification, constraints, structured reasons, usage/cost/latency, and terminal evidence. | Additive migration with backward-compatible readers and replay-stable payload digests. | `not_started` |
| SHADOW-003 | Answer “would Adaptive Flock differ, and was the evidence favorable?” honestly. | `supported`, `contradicted`, or `inconclusive` observational verdict with evidence basis; mismatched unexecuted targets cannot claim counterfactual proof. | `not_started` |
| SHADOW-004 | Keep shadow telemetry out of policy, calibration authority, grants, and control flow. | No-policy-memory/authority tests and fault injection showing observer failure cannot change the base decision. | `not_started` |
| SHADOW-005 | Make routing evidence and authority inspectable in Workbench/Mission Control. | Accessible UI distinguishes deterministic, shadow, activated, and suspended-fallback states and links evidence to the durable run/task. | `not_started` |

### Golden journey (`JOURNEY`)

| ID | Requirement | Acceptance evidence | Status |
| --- | --- | --- | --- |
| JOURNEY-001 | Persist the accepted launch binding/preflight with the admitted run. | Additive state migration and reload test proving project revision, objective/plan digest, and preflight cannot be substituted. | `not_started` |
| JOURNEY-002 | Expose one server-authored mission proof projection. | `kestrel.mission_proof.v1` aggregates contract, roles, routing, isolation, change, validation, review, risks, approval, shipping, capsule, learning, and explicit missing/stale evidence. | `not_started` |
| JOURNEY-003 | Make Mission Control the coherent command center. | Repo selection through proof/approval/ship/learn works without navigating admin pages; UI never derives authority from presentation state. | `not_started` |
| JOURNEY-004 | Meaningfully separate planner/executor/reviewer responsibility. | Durable role assignments show distinct qualified targets/model families where required, or an explicit non-independent deterministic fallback. | `not_started` |
| JOURNEY-005 | Prove safe shipping. | Owner-rejected path creates no commit; approved flagship creates the exact reviewed local commit; live PR path is separately credentialed, approved, and disposable. | `not_started` |
| JOURNEY-006 | Prove useful learning across tasks. | Task A produces receipt-bound non-policy learning; Task B retrieves the exact record and succeeds within the bound while a no-memory control fails or uses more failed attempts. | `not_started` |

### Benchmark methodology (`BENCH`)

| ID | Requirement | Acceptance evidence | Status |
| --- | --- | --- | --- |
| BENCH-001 | Repair PR #328 before merge. | Standard `log((1+N)/(1+df))+1` IDF; regressions for `df=N-1` and `df=N`; honest backend naming; every review thread resolved. | `not_started` |
| BENCH-002 | Remove oracle information and enforce one global top-k. | No arm receives ground-truth layer labels; same corpus/query/tokenization/k; Kestrel searches all eligible layers and is trimmed deterministically to global top-k. | `not_started` |
| BENCH-003 | Measure breadth and growth reproducibly. | Fixed multi-seed matrix, k values, corpus checkpoints, recency/conflict/update/obsolete/distractor/overlap/common-term scenarios, raw results, manifest/environment digests. | `not_started` |
| BENCH-004 | Report credible metrics and unfavorable results honestly. | Recall@k, Precision@k, MRR, p50/p95/p99 latency, deterministic confidence intervals, recency and growth degradation; methodology gates do not require Kestrel to win. | `not_started` |

### Constrained authority (`AUTH`)

| ID | Requirement | Acceptance evidence | Status |
| --- | --- | --- | --- |
| AUTH-001 | Qualification grants zero authority; exact owner activation remains mandatory. | Durable grant tests bind scope, policy/inventory/config/receipt digests and reject stale or mismatched evidence. | `not_started` |
| AUTH-002 | Initial v0.6 authority is limited to qualified low-risk summarizer selection. | No default grant; current qualification receipt meets existing thresholds; explicit owner action; unchanged capability boundary. | `not_started` |
| AUTH-003 | Drift, suspension, kill switch, or revocation immediately restores deterministic routing for new decisions. | Concurrent/state-transition tests plus durable fallback evidence and Workbench status. | `not_started` |
| AUTH-004 | v0.6 may ship shadow-only when evidence does not support activation. | Release checklist records qualification outcome without converting lack of evidence into a failure or weakening thresholds. | `not_started` |

## Delivery slices and dependencies

| Slice | Deliverable | Depends on | Status |
| --- | --- | --- | --- |
| S0 | Canonical source of truth and audited baseline | — | `in_progress` |
| S1 | Reliability root-cause fixes and 20-repeat platform receipt | S0 | `in_progress` |
| S2 | Exact-SHA candidate/promotion transaction | S1 | `not_started` |
| S3 | 20-release rehearsal and installed-artifact mission matrix | S2 | `not_started` |
| S4 | Live reviewer separation and truthful routing modes | S3 | `not_started` |
| S5 | Default-on zero-authority shadow observation ledger | S4 | `not_started` |
| S6 | Shadow verdict API and Workbench/Mission evidence | S5 | `not_started` |
| S7 | Durable mission proof projection and retrieval references | S6 | `not_started` |
| S8 | Golden Mission Control and two-task flagship | S7 | `not_started` |
| S9 | PR #328 fairness repair | S8 | `not_started` |
| S10 | Benchmark breadth/public artifact | S9 | `not_started` |
| S11 | Optional constrained authority and truthful PR #311 reconciliation | S10 | `not_started` |
| S12 | Final exact-artifact v0.6 qualification and promotion | S0–S11 | `not_started` |

## Executable task briefs

These headings are the executable decomposition of the slice table. They do
not override the requirement register; every task must satisfy the referenced
IDs and reflection protocol.

### Task 1: Establish a portable green baseline and canonical program record

Complete S0 and remove only defects that prevent the documented baseline from
running portably. Preserve production OCI-only validation. For the discovered
macOS host-test failure, prove first that the host-supervised test fixture loses
the absolute interpreter through container normalization, then repair the test
fixture without weakening `_normalize_python_command` or host-fallback policy.
Run the two focused regressions, full `pytest -q`, golden evaluation with the
required digest-pinned validation image, and CLI mock chat. Record all outcomes,
including environmental/owner gates, in the evidence ledger.

### Task 2: Close deterministic runtime reliability gaps

Implement S1 and REL-001 through REL-003. Reproduce the issue #303/#308 failure
modes with deterministic synchronization, replace timing-point assertions with
events/monotonic deadlines, and produce an exact-SHA 20-repeat cross-platform
receipt before closing either issue. Do not begin the shadow workstream until
Tasks 3 and 4 complete the foundational release-infrastructure stages.

### Task 3: Implement the exact-SHA release candidate transaction

Implement S2 and RELEASE-001 through RELEASE-003 as foundational release
infrastructure, not final release qualification or publication. Build and
validate once before tag creation, bind the later promotion to the
manifest/digests, preserve owner approval, and make same-candidate recovery
idempotent and conflicts fail closed.

### Task 4: Validate repeated release and installed artifacts

Implement S3, REL-004, and REL-005 as foundational reliability evidence, not a
claim that the release is `qualified`. Produce 20 unique-namespace rehearsals
and exercise the installed entry point, readiness, channel/mission runtime,
and cleanup on every supported OS/Python matrix row.

### Task 5: Wire truthful production reviewer separation

Implement S4 and JOURNEY-004 only after Tasks 3 and 4 complete. Carry durable
executor context into reviewer routing, enforce configured independence, and
label deterministic fallback truthfully when no independent target exists. Off
mode must abstain.

### Task 6: Add zero-authority production shadow observation

Implement S5 and SHADOW-001 through SHADOW-004. Observation defaults on only as
a non-authoritative side channel; it cannot alter config, execute an alternate
target, influence grants/calibration/control flow, or write Memvid/policy state.

### Task 7: Expose shadow comparison evidence

Implement S6 and SHADOW-003 through SHADOW-005. Add backward-compatible routing
APIs and accessible Workbench/Mission views with explicit authority, evidence
basis, observational verdict, missing data, and fallback state.

### Task 8: Persist and project mission proof

Implement S7 and JOURNEY-001/JOURNEY-002. Persist the admitted launch binding
atomically and expose a read-only `kestrel.mission_proof.v1` reducer that reports
present, missing, stale, and mismatched evidence without UI inference.

### Task 9: Deliver the golden Mission Control journey

Implement S8 and JOURNEY-003 through JOURNEY-006. Integrate existing controls
into one command-center flow, prove rejection and exact reviewed local commit,
keep live PR creation separately gated, and run the deterministic two-task
receipt-bound lesson-reuse/control demonstration.

### Task 10: Repair memory benchmark fairness

Implement S9 and BENCH-001/BENCH-002 on PR #328. Use smoothed non-negative IDF,
remove oracle labels from retrieval, enforce a deterministic global top-k for
all arms, report the actual backend, and resolve all review comments.

### Task 11: Add benchmark breadth and public artifacts

Implement S10 and BENCH-003/BENCH-004. Run the fixed seed/k/corpus/scenario
matrix, publish raw digest-bound data and aggregate metrics, and keep acceptance
independent of whether Kestrel wins.

### Task 12: Qualify constrained Adaptive Flock authority

Implement S11 and AUTH-001 through AUTH-004. Reconcile PR #311 with production
truth. No live grant is required for release; if evidence supports one, the
only v0.6 learned-authority class is an exact owner-activated low-risk
summarizer scope with immediate drift/revocation fallback.

### Task 13: Qualify, promote, and verify v0.6

Implement S12. Audit every explicit requirement against exact current evidence,
run the complete qualification matrix at one SHA, obtain owner approval, promote
the already artifact-validated exact artifacts, and verify post-publication
release surfaces.
Do not mark the program or goal complete while any required evidence is missing.

## Durable interface decisions

- `kestrel.release_candidate.v1` binds the exact version/SHA/tree, source and
  artifact digests, attestations, qualification receipts, approval, tag, and
  publication outcomes.
- Routing observations use an additive ledger schema. Actual authority values
  are `deterministic_static`, `adaptive_activated`,
  `deterministic_fallback_after_suspension`, and `operator_pinned`. Shadow state
  is separate because a shadow recommendation is never execution authority.
- Routing APIs evolve additively to v2. Reasoning metadata is structured reason
  codes and score components, not hidden chain-of-thought.
- `kestrel.mission_proof.v1` is read-only and server-authored. It exposes only
  receipt/handle metadata; raw secrets never appear in the projection, its
  renderers, or public APIs.
- Retrieval-use evidence is bounded to record ID, layer, content hash, and
  evidence reference; it does not expose unnecessary memory content.
- The breadth benchmark uses `kestrel.memory_benchmark.v3`, with raw per-query
  rows, methodology/environment/fixture digests, and aggregate statistics.

## Qualification matrix

Final v0.6 qualification requires all of the following at one exact SHA:

- Source, lockfile, compile, Ruff, mypy, security/audit, full Python, web,
  desktop where applicable, license, and build gates.
- `pytest -q` after each completed phase and again at the final candidate.
- 20/20 golden determinism and 20/20 routing/qualification replay with stable
  projection digests.
- 20/20 disposable release rehearsals with no rerun ritual.
- Exact artifacts on supported Windows/macOS/Linux paths: clean install,
  installed entry point, `kestrel open`, readiness, first mission, and clean
  shutdown.
- Complete golden flagship: isolated patch, independent/truthfully-labelled
  review, validation, rejection/approval, exact commit, capsule, promotion
  proposal, and later retrieval/use proof.
- Fair benchmark artifact with raw results, digest-bound fixtures, environment,
  commands, and truthful losses.
- Memvid v2 integration only behind `RUN_MEMVID_INTEGRATION=1`.
- Final owner review of remaining limitations and all owner-controlled gates.

Passing source tests produces a validated source candidate. Passing installed
artifact tests produces an artifact-validated candidate. Neither candidate is
`qualified` or “shipped”: `qualified` remains reserved for the fully
gate-complete status defined above, including merged exact-SHA evidence and all
required local, hosted, artifact, review, and owner-controlled receipts. The
protected promotion workflow must create the stable tag, publish the same
digests, and complete post-publication verification before the release is
shipped.

## Non-goals for v0.6

Unless a requirement above cannot be completed without them, do not add hosted
multi-user deployment, multi-tenancy, enterprise RBAC/identity, unrestricted
self-modification, broad swarm orchestration, new memory-layer families,
dozens of integrations, speculative agent architectures, or unrelated UI
rewrites. Kestrel already has enough surface area; v0.6 proves coherence and
trust.

## Evidence ledger

| Evidence ID | Subject SHA | Merge status | Merged SHA (when merged) | Evidence | Result and limitation |
| --- | --- | --- | --- | --- | --- |
| BASE-2026-08-10-A | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `merged` | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | Locked Python 3.13 environment; compile; full pytest invocation | Compile passed. Full pytest failed in two host-supervised `test.run` cases. Root cause and fresh full receipt pending. |
| HOSTED-2026-08-09-DET | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `merged` | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | GitHub Actions run `31282412494` | Configured Ubuntu/memory repeat passed; not Memvid/cross-platform proof. |
| HOSTED-2026-08-09-REH | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `merged` | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | GitHub Actions run `31282412490` | One local-simulation rehearsal passed; not a 20-repeat or hosted-publication proof. |
| HOSTED-2026-08-09-REL | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `merged` | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | GitHub Actions run `31283680648` | Candidate/artifact jobs ran; PyPI environment remained owner-gated when inspected. Not v0.6 evidence. |
| BASE-2026-08-10-B | `9d8bc3d891859a0598350364f3f30e320814157b` | `unmerged` | — | Full pytest; OCI-backed memory/mock golden validation; CLI mock chat | Full pytest passed; pinned-image golden passed 21/21; CLI mock chat completed. BASE-A remains failed; hosted/cross-platform repeat receipts are still missing, so REL/S1 remain `in_progress`. |

Append new rows; do not rewrite a failed or superseded receipt into a pass.
