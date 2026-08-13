# Kestrel v0.6 Proof Release — Source of Truth

Last updated: 2026-08-13

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

For dependency purposes, a predecessor is **complete** only when its slice is
`qualified`. A dependent slice may be prepared early only on an isolated,
stacked branch after the predecessor's local implementation and independent
review are complete. That preparation may be `in_progress`, but it may not be
accepted, merged, or used as qualification evidence until every listed
predecessor is `qualified`.

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

Work follows this hard acceptance and merge order. The limited stacked-branch
preparation described in the status vocabulary is the only permitted overlap;
it grants no authority and cannot satisfy a dependency. The foundational
release transaction, rehearsal, and installed-artifact work is reliability
infrastructure, not final release qualification or publication:

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
  Conversational CLI readiness is a prerequisite: no optional UI work may
  start, and no optional UI slice may be accepted or merged, unless the exact
  predecessor SHA has a durable CLI launch-and-chat receipt.
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

### Current S0/S1 reconciliation (2026-08-11)

The audited baseline above remains historical and append-only. In particular,
neither failed `BASE-2026-08-10-A` nor incomplete `BASE-2026-08-10-B` is
rewritten as a pass.

- S0 implementation from [PR #331](https://github.com/John-MiracleWorker/Kestrel/pull/331)
  head `fc964b6a1fb6cd2cf6abc8b6156b775f4e8a9b39` merged as
  `4bd4937a20d575d28803293b5c717120000957b4`; its automatic protected-main
  [CI](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31360437434)
  and [determinism](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31360437361)
  runs completed successfully on attempt 1. A fresh local receipt on
  protected-main qualification subject
  `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` supplies the previously missing
  full-test output, pinned-OCI 21-case golden report, conversational CLI output,
  exact identity, and clean-checkout proof. Its owner-local receipt root,
  command record, immutable S0-only manifest, and review disposition are
  recorded in `MAIN-2026-08-11-S0`. That receipt completes the technical S0
  evidence without changing either baseline receipt's original result. At
  this receipt's 2026-08-11 boundary, S0 remained `in_progress` until PR #338
  merged and a later append-only receipt bound its then-unknown merge SHA.
- S1 implementation provenance spans backend-bound determinism
  [PR #332](https://github.com/John-MiracleWorker/Kestrel/pull/332), Windows wait
  stabilization [PR #333](https://github.com/John-MiracleWorker/Kestrel/pull/333),
  cross-platform runtime qualification
  [PR #334](https://github.com/John-MiracleWorker/Kestrel/pull/334), partial
  executor-rejection synchronization
  [PR #335](https://github.com/John-MiracleWorker/Kestrel/pull/335), and final
  five-cell aggregation
  [PR #337](https://github.com/John-MiracleWorker/Kestrel/pull/337), followed by
  the scheduler-sensitive Windows reliability repair
  [PR #339](https://github.com/John-MiracleWorker/Kestrel/pull/339). PR #337 head
  `59ebcc6e90e2b120fddeceb6d0a2578213f4ef51` merged normally as
  `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` with tree
  `8395483245d48af48feeb3521f65f22785fe30ee`. Exact-main attempt-1 CI and
  determinism passed, the five-cell aggregate artifact was independently
  safety-checked and replayed against the same run/attempt/SHA, and exact-head
  review found no major issue.
- Later qualification preserved two additional failed candidate heads rather
  than rewriting them as passes. PR #338 head
  `b5d914813f4308efc73acd0d35319a7d23e03747`
  exposed a one-second Windows scheduler wait and unhandled worker warning.
  PR #339 intermediate head
  `8b72ae875d1c44339cd5aa9babf785c79bae04ca` then exposed independent
  15-second terminal-event and two-second execution-completion wait failures
  even though its targeted determinism receipt passed. The final seven-test
  contract uses a 900-second per-iteration deadline. PR #339 final head
  `9023a7c04cd7ccaa87fa49deef0fbbf14261473b`, tree
  `854d534160f76adaa1e4039dcd291f154f4cb5e6`, passed exact-head qualification
  and merged normally as `94556eaa98ec153729d0d063346c8e64ba2575e1`.
  Fresh protected-main CI, determinism, release rehearsal, the complete local
  5,298-test suite, and a local 20-by-7 receipt then passed at that merge SHA.
  The technical acceptance evidence for REL-001 through REL-003 passed, but
  those requirements remain `in_progress` pending the dependency and process
  gates below.
- Process exception: PRs #332 through #337 and PR #339 were merged by the
  repository owner while no merged canonical record had established S0 as
  `qualified`. The later exact-main S0 receipt cannot retroactively make those
  merges dependency-compliant. PR #338 is this reconciliation record, is not
  part of that exception set, and its merge does not grant the distinct owner
  exception. At this 2026-08-11 reconciliation boundary, its future merge
  also could not itself qualify S0: a later append-only receipt still had to
  bind PR #338's merged SHA before a follow-up could move S0 to `qualified`.
  S0 and S1 therefore remained `in_progress`, and S2 remained unavailable.
  After S0 qualifies, a distinct later owner-acceptance change
  may durably grant a one-time exception and move S1 to `qualified`; no such
  exception has been granted. That later change must preserve the violation as
  history and does not waive dependency ordering for S2 or any future slice.
  Issues #303 and #308 remain open until that later change qualifies S1; any
  eventual closure must be scoped to REL-001 through REL-003 and must not
  imply REL-004/REL-005 or installed-artifact qualification.
- The first exact-main S0 command used a non-native global `TMPDIR` and a pytest
  base resolved under `/private/tmp`. Of the five preserved failures, one was a
  harness-precondition failure and four exercised a real, separately triaged P3
  `/tmp` versus `/private/tmp` launcher-path behavior. A native-temp focused
  check passed 5/5 before the fresh full receipt passed. The invalid run is not
  qualifying evidence; the focused pass does not erase the P3 installer/final-
  release hardening finding, which does not block S0 or the REL-001–REL-003
  technical evidence.

### S0 qualification follow-up (2026-08-12)

PR #338 reviewed head `d73f5c54be0739787612bfa03df50f7942cd598e`,
tree `8f8e09bf4c4226df67255df79d8004f1b5d75cc1`, merged normally as signed
protected-main commit `eb7f26628e40e3a840dccb7ebfb9dd67e0bb7ac9` with
the same tree and ordered parents
`94556eaa98ec153729d0d063346c8e64ba2575e1` and
`d73f5c54be0739787612bfa03df50f7942cd598e`. The append-only
`MAIN-2026-08-12-S0-QUAL` receipt below binds that exact merge to the already
accepted `MAIN-2026-08-11-S0` technical receipt. Exact-head CI, determinism,
review, and exact-main attempt-1 CI and determinism all passed without rerun or
cancellation. This closes only the remaining canonical-record gate and moves
S0 to `qualified`.

REL-001 through REL-003 and S1 remain `in_progress`; the distinct S1 owner
exception has not been granted. S2 remains unavailable and `not_started`, and
issues #303 and #308 remain open. This follow-up does not satisfy REL-004,
REL-005, S2 or any later slice, installed-artifact qualification, owner
promotion, stable-tag creation, publication, post-publication verification, or
final v0.6 release qualification. The successful single protected-main
rehearsal is ancillary and is not REL-004.

These receipts do not satisfy REL-004, REL-005, any `RELEASE` requirement, S2
or a later slice, installed-artifact qualification, owner promotion, stable-tag
creation, publication, post-publication verification, or final v0.6 release
qualification.

### S1 one-time owner-acceptance exception (2026-08-12)

On 2026-08-12 the repository owner, Trent Iuni, explicitly selected **Grant the
narrow S1 exception** in response to this exact question:

> May I create the one-time S1 owner-acceptance record that preserves that
> historical dependency-order violation and waives no S2+, release, promotion,
> or publication gate?

This decision's immutable source ID is
`OWNER-DECISION-2026-08-12-S1-EXC-V1`. Its canonical
`kestrel.owner_decision.v1` record is the UTF-8, line-feed-terminated sequence
`schema=kestrel.owner_decision.v1`,
`source_id=OWNER-DECISION-2026-08-12-S1-EXC-V1`, `owner=Trent Iuni`,
`date=2026-08-12`, the exact `question` text above on one line, and
`selection=Grant the narrow S1 exception`; its SHA-256 is
`fe9e3bc2bdcf93f76632d63fb55198884503446169b51c45ea5fe4124d753c60`.

This durably records the owner's one-time acceptance of the S1 process
exception anticipated by the 2026-08-11 reconciliation above. The exception:

- Is narrow and nonprecedential. It applies only to accepting the already
  complete REL-001/REL-002/REL-003 technical evidence for S1 (receipts
  `MAIN-2026-08-11-S1`, `MAIN-2026-08-11-S1-REQUAL`, and `PR339-2026-08-11-S1`).
- Preserves as history that PRs #332 through #337 and PR #339 were merged
  before a merged canonical record had qualified S0. Those merges remain a
  dependency-order violation and are never rewritten as dependency-compliant.
- Grants no waiver, authority, or evidence for REL-004, REL-005, S2 through
  S12, installed artifacts, stable-tag creation, promotion, publication,
  post-publication verification, issue closure beyond the scoped reliability
  issues (#303/#308, scoped to REL-001 through REL-003 when closure later
  occurs), or any future dependency-order violation.

This change does not qualify S1 on this unmerged branch. REL-001 through
REL-003 and S1 remain `in_progress` until this owner-acceptance change itself
is normally merged to protected main and a later append-only receipt binds the
exact protected-main merge SHA. Only then may S1 move to `qualified`, and only
then may issues #303/#308 be closed, scoped to REL-001 through REL-003. S2
remains `not_started` and unavailable for acceptance, merge, or qualification;
any S2 preparation is limited to an isolated stacked branch and grants no
authority. The existing S0 qualification and every failed or incomplete
receipt above remain unchanged.

### S1 owner-acceptance merge and uncovered channel correlation gap (2026-08-13)

[PR #341](https://github.com/John-MiracleWorker/Kestrel/pull/341) merged the
owner-acceptance record above as protected-main commit
`deeb7138c755af7427e3ee11f6244bb1cf2dbf94`. That merge satisfies the narrow
decision-record dependency, but its preserved CI history exposed a separate
REL-002 coverage gap that prevents S1 qualification:

- PR CI run
  [31582333551](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31582333551)
  attempt 1 failed on Windows in
  `test_public_channel_webhook_allows_explicit_unsigned_channel`. The endpoint
  truthfully returned `Kestrel accepted the request and is still working.`
  while the test expected the eventual mock response at that timing point.
- The same head was rerun without a source change and attempt 2 passed. That
  rerun does not erase or qualify the failed first attempt.
- Exact-main CI
  [31588036325](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31588036325),
  determinism
  [31588036297](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31588036297),
  and the ancillary rehearsal
  [31588036357](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31588036357)
  passed on attempt 1. However, the runtime reliability contract still selected
  only seven tests and omitted the failing public-webhook path. One later full
  suite pass therefore does not prove that scheduler timing can no longer
  choose either legitimate response state.

The durable run already had a server-authored run ID, but
`ChannelProcessResult.to_public_dict()` omitted it. A caller receiving the
accepted/still-working response therefore had no public correlation key with
which to inspect the eventual durable result. The bounded repair is to expose
that existing ID additively as `run_id: string | null`, force the accepted
state with an explicit worker barrier, release the worker, and await
`GET /api/runs/{run_id}` under one monotonic deadline with observed-status
diagnostics. The test must join the cross-platform reliability contract,
raising its qualified total from seven tests/420 executions to eight tests/480
executions. This changes neither webhook signature policy, response timing,
follow-up delivery, nor execution authority.

REL-001 through REL-003 and S1 remain `in_progress`; S2 remains `not_started`,
and issues #303/#308 remain open until the repaired exact-head and later
protected-main receipts pass without rerun.

### S1 repaired candidate and LAN test scheduling gap (2026-08-13)

The first [PR #342](https://github.com/John-MiracleWorker/Kestrel/pull/342)
candidate, `cf5ab301180340fc5a8ae323aa5d29928e0eee45`, implemented the public
channel correlation repair and passed its attempt-1 five-cell determinism run
[31683321383](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31683321383).
Independent replay of aggregate artifact `9174792983` proved 5/5 cells,
100/100 repeats, 480 runtime test executions, 840 golden-case executions, and
zero failures, flakes, or cleanup failures. The broader attempt-1 push CI run
[31683286186](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31683286186)
nevertheless exposed another Windows scheduling dependency in
`test_manual_confirm_requires_exact_consent_and_cached_authority_without_writes[nonzero-cas]`:

- Windows job
  [94393448052](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31683286186/job/94393448052)
  failed after `_wait_for_terminal` exhausted a fixed three-second polling
  window. The injected manual scanner itself is immediate and deterministic.
- The concurrent PR Windows copy completed the same full-test step and the
  separately repeated runtime cells remained green. That contrast does not
  erase the failed push receipt; it demonstrates that the assertion depended
  on incidental thread-pool scheduling.
- The test is about exact consent, compare-and-swap rejection, cached address
  authority, and absence of rejected writes. It does not need a live scheduler
  race. Its worker submission is therefore changed to a controlled executor:
  rejected calls must enqueue nothing, the one accepted call must enqueue
  exactly one controller, and the test explicitly releases and joins that
  controller before inspecting the durable terminal record.

The exact failed parameter now joins the runtime reliability contract. The
qualified contract consequently rises from eight tests/480 executions to nine
tests/540 executions across Linux, macOS, and Windows. REL-001 through REL-003
and S1 remain `in_progress`; S2 remains `not_started`; no rerun of the failed
receipt may substitute for a fresh repaired candidate and later protected-main
evidence.

### S1 qualification closure (2026-08-13)

[PR #342](https://github.com/John-MiracleWorker/Kestrel/pull/342) repaired both
remaining coverage gaps and merged normally as signed protected-main commit
`dbe9313c8671e2ba7507f73cc434569a59ebf785`, tree
`e7cf79be8d95b5b307827ba694e00a7dda63c90b`, with ordered parents
`deeb7138c755af7427e3ee11f6244bb1cf2dbf94` and
`894b31ce1d3b6353a4257948bbcd3e9912ceda2f`. The exact repaired PR head passed
attempt-1 push CI, PR CI, and five-cell determinism; the aggregate was
independently replayed, and current-head review found no major issue or
unresolved thread. The append-only failed receipts above remain failures and
were not rerun or rewritten.

The later exact protected-main receipt `MAIN-2026-08-13-S1-QUAL` below passed
on first attempts at the merge SHA. Its independently replayed 146-file
aggregate proved 5/5 cells and 100/100 repetitions: 20 each for Linux, macOS,
Windows, memory, and real Memvid; 540 runtime-test executions; 840 golden-case
executions; and zero failures, observed flakes, cleanup failures, missing
cells, duplicate cells, stale cells, or mismatched cells. The ordinary
protected-main CI passed all 14 jobs, including the complete Windows suite,
the separate LAN adversarial suite, Docker privilege/license controls, and the
container vulnerability policy. A fresh local full suite from a clean detached
worktree at the same merge SHA also passed all 5,298 collected tests. The exact
command, subject identity, pre/post status, collection, output, and exit status
are retained under owner-local receipt root
`/Users/tiuni/.codex/evidence/kestrel-v06-s1-channel-reliability/dbe9313c8671e2ba7507f73cc434569a59ebf785/local-full-suite-2026-08-13`;
`RECEIPT.json` SHA-256 is
`08f27d84a5d16726f698b6c632ea50e8ec127959d97efa8422071c1e988164d8`,
and the immutable file-manifest SHA-256 is
`ab35957d79a30fd5823e2ebd5dc58fe40495089ce39a9f3ddf449c8cfdb6453a`.

Together with S0 qualification and the narrow one-time owner decision already
merged by PR #341, these exact-head and protected-main receipts satisfy
REL-001, REL-002, REL-003, and S1. Those four statuses therefore move to
`qualified`, and S2 becomes available to begin under its separately approved
plan. This closure grants no evidence or authority for REL-004, REL-005, S2 or
later slices, installed-artifact qualification, release promotion,
publication, or final v0.6 qualification. The one exact-main release rehearsal
is recorded as ancillary only and does not satisfy repeated-rehearsal REL-004.

## Requirement register

### Reliability (`REL`)

| ID | Requirement | Acceptance evidence | Status |
| --- | --- | --- | --- |
| REL-001 | Eliminate golden-eval nondeterminism rather than masking it with reruns/timeouts. | Regression reproduces the retrieval/settlement defect; fixed fixture sealing, seeds, clocks, IDs, ordering, and completion; exact-SHA memory and Memvid repeat receipts. | `qualified` |
| REL-002 | Remove Windows channel/full-runtime timing flakes with explicit synchronization. | Event/state-driven tests and 20 consecutive targeted iterations on Windows, macOS, and Linux with no rerun; structured failure diagnostics. | `qualified` |
| REL-003 | Use monotonic elapsed-time logic and explicitly await asynchronous state transitions. | Static/test coverage for every changed timing path; no wall-clock equality or timing-point authority assertion. | `qualified` |
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
| S0 | Canonical source of truth and audited baseline | — | `qualified` |
| S1 | Reliability root-cause fixes and 20-repeat platform receipt | S0 | `qualified` |
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
- A conversational CLI launch-and-chat receipt must pass at the exact
  predecessor SHA before optional UI work starts or any optional UI slice is
  accepted or merged; UI success cannot substitute for this CLI gate.
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
| BASE-2026-08-10-A | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `merged` | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `uv sync --locked --all-extras --group release --python 3.13`; `python -m compileall -q src tests scripts`; `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q` | Compile passed. Full pytest failed in two host-supervised `test.run` cases. The exact initial environment-output digest was not captured, so this failed receipt remains explicitly incomplete. |
| HOSTED-2026-08-09-DET | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `merged` | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | [Actions run 31282412494](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31282412494); workflow command `python scripts/run_determinism_evals.py --repeats 20 --seed 1729 --source-commit "${GITHUB_SHA}" --run-root "${RUNNER_TEMP}/kestrel-determinism-runs" --output "${RUNNER_TEMP}/kestrel-determinism-report.json" --workspace . --case-timeout-seconds 60 --iteration-timeout-seconds 1500 --max-case-latency-ms 45000` | Configured Ubuntu/memory repeat passed; not Memvid/cross-platform proof. The artifact digest was not captured during the audit, so this historical receipt remains incomplete. |
| HOSTED-2026-08-09-REH | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `merged` | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | [Actions run 31282412490](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31282412490); workflow command `python scripts/run_release_rehearsal.py --source-root . --sandbox-root "${RUNNER_TEMP}/kestrel-release-rehearsal" --namespace "kestrel-rehearsal-${GITHUB_REPOSITORY_ID}-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" --commit "$GITHUB_SHA" --output "${RUNNER_TEMP}/kestrel-release-rehearsal-report.json"` | One local-simulation rehearsal passed; not a 20-repeat or hosted-publication proof. The artifact digest was not captured during the audit, so this historical receipt remains incomplete. |
| HOSTED-2026-08-09-REL | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | `merged` | `f78ef1b4a54d63b0e49787b80a67133ba2ae4268` | [Actions run 31283680648](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31283680648); exact commands remain encoded in `.github/workflows/release.yml` at this SHA | Candidate/artifact jobs ran; PyPI remained owner-gated. The expanded command list and artifact digests were not captured during the audit, so this historical receipt is explicitly incomplete and is not v0.6 evidence. |
| BASE-2026-08-10-B | `9d8bc3d891859a0598350364f3f30e320814157b` | `unmerged` | — | Receipt root: `TASK1_RECEIPT_ROOT=/tmp/kestrel-v06-task1-review.oBrMFT`; focused RED/GREEN: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q tests/test_tools.py::test_same_public_call_id_across_runs_keeps_process_tracking_isolated tests/test_tools.py::test_subprocess_tool_timeout_kills_child_process_and_caps_requested_timeout`; full: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q`; golden: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 NEST_AGENT_VALIDATION_CONTAINER_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3' .venv/bin/python scripts/run_golden_evals.py --backend memory --provider mock --model mock --workspace . --memory-dir "$TASK1_RECEIPT_ROOT/golden-memory" --seed 1729 --output "$TASK1_RECEIPT_ROOT/golden-report.json" --case-timeout-seconds 120`; CLI: `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m nested_memvid_agent.cli chat --backend memory --memory-dir "$TASK1_RECEIPT_ROOT/cli-receipt-memory" --provider mock --model mock --workspace . --log-dir "$TASK1_RECEIPT_ROOT/cli-receipt-logs" --state-path "$TASK1_RECEIPT_ROOT/cli-receipt-state/agent.db" --message 'Return a deterministic v0.6 CLI readiness acknowledgement.' --session-id v06_task1_cli_receipt > "$TASK1_RECEIPT_ROOT/cli-output.txt"` | Focused RED failed 2/2 at pre-repair subject `f78ef1b4a54d63b0e49787b80a67133ba2ae4268`; focused GREEN passed 2/2 at this `9d8bc3d` subject. Full pytest passed, but its output digest was not captured, so that local full-test receipt remains incomplete and non-qualifying. A fresh 2026-08-10 golden rerun passed 21/21 with report SHA-256 `a89cbf9e5c1c4cc1b45d0eaa6d6b76fc19c3b21604ca0280c6501c28ce0b3ea4`; the exact CLI command exited 0 with completed mock output SHA-256 `eaacc7efee11acef5f876681943a0260db2aa778bbdd2ec9e5195e71b1746b9e`. BASE-A remains failed; hosted/cross-platform repeat receipts are still missing, so REL/S1 remain `in_progress`. |
| PR331-2026-08-10-DET | `96d265ef92b4a82ff8cbf815021af5035a1f195b` (synthetic PR merge) | `unmerged` | — | [Actions run 31356774284](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31356774284); determinism artifact SHA-256 `b65c4ea253c800c06e26f9ded7bd31d2f0b73b87d8b587b87260063d5af8d656`; workflow command is the exact HOSTED-DET command above | First-attempt Ubuntu/memory determinism and Flock qualification jobs passed for the PR merge candidate. This is review evidence only: it is not a protected-main SHA, Memvid proof, or cross-platform S1 qualification. |
| MAIN-2026-08-11-S0-INVALID | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | `merged` | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | Preserved output SHA-256 `112e85976f56173cf2b1dc6e71937585d3c3614eaef11052d06e33b21614f21b`; command globally set `TMPDIR=/tmp/kestrel-s0-exact-main.Zh6pp6/tmp` and `--basetemp /tmp/kestrel-s0-exact-main.Zh6pp6/pytest-basetemp`, which resolved under `/private/tmp`; native-temp correction output SHA-256 `4d2df7a96ce4a667da3ccf295f2d16c728a65ed0e861a3baa919f7bb469b02b5` | Non-qualifying receipt: one failure was a harness-precondition failure and four exercised the real, triaged P3 lexical `/tmp` versus `/private/tmp` launcher-path behavior. The focused native-temp correction passed 5/5 but does not erase that P3 finding. Neither this run nor the correction replaces the separately qualifying full-test receipt. |
| MAIN-2026-08-11-S0 | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | `merged` | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | [PR #331](https://github.com/John-MiracleWorker/Kestrel/pull/331) head `fc964b6a1fb6cd2cf6abc8b6156b775f4e8a9b39` merged by the repository owner as `4bd4937a20d575d28803293b5c717120000957b4`; [main CI 31360437434](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31360437434); [main determinism 31360437361](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31360437361); owner-local receipt root `/Users/tiuni/.codex/worktrees/kestrel-v06-reliability/.superpowers/sdd/V0_6_PROOF_RELEASE_SOURCE_OF_TRUTH/receipts/659c313ac164c4f62dbd38876f1caf0bc02ab0a2`; exact command record `COMMANDS.md` SHA-256 `6bd55537f723f8d37a1f87ff081095d196ee5dbba4ce6e4acf5454b092ba6d72`; fresh exact-main full pytest 5,298-test output SHA-256 `782070378d979a98b63a81ea96e2d8820b6a966cd31227a096965c1be80cfef8`; collection output SHA-256 `112141130834476fcac037bfbe94fff9bfacc3373154d81c5c6202ca24ecccc6`; pinned-OCI golden 21/21 report SHA-256 `712f5cc135517c7ae8f924b211e4e553f6d1cdd0232e2c08abc30a6cd4a5b3b4`; CLI completed output SHA-256 `d5109080017edbe9908699d15f1371b8ec47904da027d39f557aab0a78e108f5`; local summary SHA-256 `063893054c27f483ef72022d58563e0218108fbee5808dee817e279925b4298f`; immutable 35-entry `S0_SHA256SUMS` manifest SHA-256 `d6460a6743291494cc331b8e7dc515873eae6ca98d7956a2db53f90660d15658`; pinned uv 0.11.16, Python 3.13.12, OCI `python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3`; exact identity and clean checkout | The technical S0 evidence is complete after independent receipt-integrity review. This fresh receipt supplies the output digest absent from BASE-B while preserving BASE-A/B exactly as failed/incomplete history. It does not qualify S0 because `659c313` still records S0 as `in_progress`; S0 remains `in_progress` until PR #338 merges and a later receipt records that merge SHA. Only then may a follow-up move S0 to `qualified`. The receipt is retained owner-locally; collaborators must not treat the path alone as remotely retrievable evidence. |
| PR337-2026-08-11-S1 | `59ebcc6e90e2b120fddeceb6d0a2578213f4ef51` | `merged` | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | Root-cause provenance: [PR #332](https://github.com/John-MiracleWorker/Kestrel/pull/332), [PR #333](https://github.com/John-MiracleWorker/Kestrel/pull/333), [PR #334](https://github.com/John-MiracleWorker/Kestrel/pull/334), and [PR #335](https://github.com/John-MiracleWorker/Kestrel/pull/335); final qualification boundary [PR #337](https://github.com/John-MiracleWorker/Kestrel/pull/337); exact-head attempt-1 [push CI 31497683668](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31497683668), [PR CI 31497687124](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31497687124), and [determinism 31497687171](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31497687171); artifact `9104182385` ZIP SHA-256 `13ea1caa196b54fab82f4521b025d0a25ca8138f1925e43cb9a1998fc1bf29f4`; exact verifier replay; [hosted Codex review](https://github.com/John-MiracleWorker/Kestrel/pull/337#issuecomment-5254042760) reviewed `59ebcc6e90` with no major issue; original PR and exact-main ZIPs retained at the owner-local receipt root in `S1_ARTIFACT_SHA256SUMS` (manifest SHA-256 `a03546203dc4654e3de1dd4a8eeb914bc3380752482bd4ea82743f83b74e6415`) with preservation record `S1_ARTIFACTS.md` SHA-256 `04725be56412e150f0c85fa914e38d5547409d21cd90eb6396cc9f6ee261aeef` | Exact-head candidate evidence passed 5/5 cells, 100/100 repeats, 240 runtime and 840 golden executions, zero failures/flakes/cleanup failures, plus CI/security/review. This row is branch evidence and does not substitute for the protected-main row below. Hosted copies expire on 2026-08-25; the original ZIP bytes are retained owner-locally, not published. |
| MAIN-2026-08-11-S1 | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | `merged` | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | [CI 31508994971](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31508994971): attempt 1, 14/14 jobs, 156 executed steps successful and 35 expected conditional skips; [determinism 31508994974](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31508994974): attempt 1, 7/7 jobs; exact aggregate artifact `9108661459`, 248,519 bytes, ZIP SHA-256 `c274b4b41a4063bc8d7e0161484b485bebf398b5c4b6a7df89cfdb83f4624951`; safe 146-file archive audit; exact verifier replay; aggregate raw/canonical SHA-256 `39c9fbe60ee00ebe9f28352ac3f415a40cc2a658593fdfc4a789291c2326ec80` / `4c4d6b3387be4cd6f27b2e6a17aad2f5d9601181390d7f11678b79e217572b63`; exact-main CodeQL analysis `1602548479` fixed alert #126 with no new alert | REL-001, REL-002, and REL-003 technical acceptance evidence passed: 5/5 cells, 100/100 repeats (60 runtime/40 golden), 240 runtime and 840 golden executions, zero failures/flakes/cleanup failures, exact-main CI/security, independent artifact replay, and owner-local preservation of the original ZIP. The requirements and S1 remain `in_progress` because the dependency and process gates described above are still open. This does not satisfy REL-004/005, S2+, installed-artifact, promotion, publication, or final release gates. |
| MAIN-2026-08-11-REH | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | `merged` | `659c313ac164c4f62dbd38876f1caf0bc02ab0a2` | [Release rehearsal 31508995280](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31508995280), attempt 1; artifact `9108248907`, 955 bytes, ZIP SHA-256 `2e51ad05b33b8dda54a55bde6538461aa083298962a1d13d9d6466d576a826ac`; downloaded receipt JSON SHA-256 `e58f341b3497cefe2c8b4f34acf7909da9bdd555f71091ba2b63c4bd645dd421`; exact source/finalization SHA; wheel `264c92c1585bf4264484d7cb5b46ee9f0acbe573d7d0871e4e016c2d8f2a323d`; sdist `dfa0466570e6cda123a69e18e32406bb2d63e6b62b31117a12be07bda7fa0920` | One rehearsal passed with production targets blocked, exact replays `already_exact`, and conflicting mutation rejected. This rehearsal created no production tag, did not run the production release workflow, and created no GitHub release. It is not REL-004, S2/S3, promotion, publication, or release qualification. The current selector is post-tag and does not satisfy the required pre-tag transaction. Before S2 or RELEASE-001 can be qualified, and before any stable-tag creation, the selector must bind attempt 1, prove uniqueness, and download and verify the selected receipt. |
| PR338-2026-08-11-CI-FAIL | `b5d914813f4308efc73acd0d35319a7d23e03747` | `unmerged at receipt time; superseded on PR #338` | — | Attempt-1 [push CI 31517030595](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31517030595), Windows job `93864630113`; concurrent [PR CI 31517057727](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31517057727) and [determinism 31517057680](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31517057680) | Push Windows failed `test_cancelling_queued_run_finishes_publication_fence_without_worker` at `active_started.wait(timeout=1)` and emitted an unhandled worker warning; downstream Docker was skipped. The concurrent PR CI and determinism copies passed, proving a real scheduler-sensitive REL-002 flake. The attempt was neither rerun nor cancelled, and later green runs do not erase it. |
| PR339-2026-08-11-CI-FAIL | `8b72ae875d1c44339cd5aa9babf785c79bae04ca` | `failed receipt; PR #339 merged later` | `94556eaa98ec153729d0d063346c8e64ba2575e1` | Attempt-1 [push CI 31525914516](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31525914516), Windows `93894066757`; [PR CI 31525917233](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31525917233), Windows `93894076076`; successful [determinism 31525917155](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31525917155) | Push failed `test_approved_repair_scheduler_flow_binds_real_validation_and_review_receipts` after its fixed 15-second terminal wait; PR CI independently failed `test_approval_heartbeat_delayed_renewal_cannot_cancel_after_finalization` at its fixed two-second completion wait. Both Docker jobs were skipped. This exact `8b72ae8` receipt remained failed and non-qualifying; PR #339 merged later as `94556eaa` only after later repairs produced qualified head `9023a7c`. The attempt was not rerun, and the later merge does not rewrite it as a pass. |
| PR339-2026-08-11-S1 | `9023a7c04cd7ccaa87fa49deef0fbbf14261473b` | `merged` | `94556eaa98ec153729d0d063346c8e64ba2575e1` | Exact tree `854d534160f76adaa1e4039dcd291f154f4cb5e6`; attempt-1 [push CI 31542384239](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31542384239) 14/14, [PR CI 31542387278](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31542387278) 14/14, and [determinism 31542387290](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31542387290) 7/7; aggregate artifact `9121666980`, API digest `f89e5118ce6979de81dd8404825546ef2b19c57ad04cfe29a8663432930da098`, JSON SHA-256 `44ddaa20d8a9e9a05a7e9164ebd52eb6393bb00d36da50e2f7a31111d9423ff9`; clean local 20-by-7 report SHA-256 `7df5cbe83414b170910d519871f0c2099c808607db34d61e0a4b81fcb76edbd2`; [hosted Codex review](https://github.com/John-MiracleWorker/Kestrel/pull/339#issuecomment-5259594915) and current-head CodeRabbit status | Five of five cells, 100/100 repeats, 420 runtime and 840 golden executions, ordinary Windows and downstream Docker all passed with zero failures, flakes, cleanup failures, deadline overruns, or receipt diagnostics. This qualifies the branch candidate only; it grants no owner exception and does not satisfy S2, installed-artifact, repeated rehearsal, publication, promotion, or release qualification. |
| MAIN-2026-08-11-S1-REQUAL | `94556eaa98ec153729d0d063346c8e64ba2575e1` | `merged` | `94556eaa98ec153729d0d063346c8e64ba2575e1` | Exact tree `854d534160f76adaa1e4039dcd291f154f4cb5e6`; attempt-1 [CI 31545460395](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31545460395) 14/14, Windows `93956853439`, Docker `93962276821`; [determinism 31545460478](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31545460478) 7/7, aggregate artifact `9122565218`, API digest `04c7342fe9bb7166ffb48ea279e01ba3b09093aaae07b0b3297b988bb9e751dc`, JSON SHA-256 `b4a91cddd150f492be3a2fdf6346a51091ad604cbff94579318bde9aca3c41a8`; complete 5,298-test/local-qualification receipt SHA-256 `49291fb4ba6695d6c6eff49dfafb1385092704f07e2e27a4d2424ad9b8b93a1a`; local 20-by-7 report SHA-256 `45b2d18a6f361914b6b3dc16b53d1bd1512915d93b96e4e3b4857b8469cbf292`; ancillary [release rehearsal 31545460452](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31545460452), artifact `9122289710`, report SHA-256 `c4289de984cbefa21f48e9c8bfedb7b14300039a3fdb5d28cf279bdd643e2631` | Protected-main technical requalification passed: five of five cells, 100/100 repeats, 420 runtime and 840 golden executions, zero failures, flakes, cleanup failures, deadline overruns, or diagnostics; Windows and strict Docker passed. REL-001 through REL-003 remain `in_progress`: S0 is `in_progress` pending the later PR #338 merged-SHA receipt, and after S0 qualifies the distinct S1 process exception will still be ungranted. S1 remains `in_progress`; S2 remains unavailable and `not_started`; #303/#308 remain open. One disposable rehearsal is ancillary and does not satisfy REL-004, S2, promotion, publication, or release qualification. |
| MAIN-2026-08-12-S0-QUAL | `eb7f26628e40e3a840dccb7ebfb9dd67e0bb7ac9` | `merged` | `eb7f26628e40e3a840dccb7ebfb9dd67e0bb7ac9` | Prior technical receipt `MAIN-2026-08-11-S0`; [PR #338](https://github.com/John-MiracleWorker/Kestrel/pull/338) reviewed head `d73f5c54be0739787612bfa03df50f7942cd598e`, tree `8f8e09bf4c4226df67255df79d8004f1b5d75cc1`, and validly signed merge with ordered parents `94556eaa98ec153729d0d063346c8e64ba2575e1` / `d73f5c54be0739787612bfa03df50f7942cd598e`; one-file patch SHA-256 `7f8c79d40f05120047d60e9dd334c5a1c485f6d4429a5b13f475f80d351e992f`; exact-head attempt-1 [push CI 31557390723](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31557390723) 14/14, [PR CI 31557393069](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31557393069) 14/14, and [determinism 31557393068](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31557393068) 7/7; CodeRabbit SUCCESS; hosted Codex comment `5261496325` reviewed exact `d73f5c54be` with no major issue; 5/5 review threads resolved; exact-main attempt-1 [CI 31560773372](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31560773372) 14/14, Windows `94002397462`, Docker `94008639366`; [determinism 31560773367](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31560773367) 7/7, aggregate artifact `9127839468`, API digest `22ecaa60a25015cefce7c95033a693f72775f4818ce85fa574c7ff86599dab8c`, JSON SHA-256 `6f486d29b639c98f93da8ca40cee0730d39ad712933f0709789808d22bd2edf3` | The appended merged-SHA binding closes only the remaining S0 canonical-record gap and, together with `MAIN-2026-08-11-S0`, qualifies S0. It does not rewrite failed/incomplete history, qualify REL-001 through REL-003 or S1, grant the distinct owner exception, close #303/#308, make S2 available, or satisfy REL-004/REL-005, release qualification, promotion, publication, or post-publication verification. PR #338's owner merge is merge evidence, not the S1 exception. |
| OWNER-2026-08-12-S1-EXC | `15768229811db675de169ecfa1a619dc70e4124c` (unmerged branch candidate; docs-only) | `unmerged` | — | Immutable source ID `OWNER-DECISION-2026-08-12-S1-EXC-V1`, canonical-record SHA-256 `fe9e3bc2bdcf93f76632d63fb55198884503446169b51c45ea5fe4124d753c60`; owner decision by repository owner Trent Iuni on 2026-08-12 explicitly selecting **Grant the narrow S1 exception** in response to the exact question recorded in the section "S1 one-time owner-acceptance exception (2026-08-12)" of this document; one-time owner-acceptance record added on branch `agent/v06-s1-owner-exception-draft` based on protected-main SHA `f60a65f156437c968be78a83d0a9db29a4d8389a` | One-time, narrow, nonprecedential exception accepting only the already complete REL-001/REL-002/REL-003 technical evidence for S1. It preserves the PR #332–#337/#339 dependency-order violation as history and grants no waiver, authority, or evidence for REL-004/REL-005, S2–S12, installed artifacts, stable-tag creation, promotion, publication, post-publication verification, issue closure beyond the scoped reliability issues, or future dependency-order violations. S1 and REL-001 through REL-003 remain `in_progress` and S2 remains `not_started` and unavailable until this change is normally merged to protected main and a later append-only receipt binds the exact merge SHA; no merge SHA, workflow URL, or review receipt exists yet and none is invented here. |
| PR341-2026-08-12-CI-FAIL | `c4201981cb0fa474a29ed32117a254cbfd6457b2` | `merged after a no-code rerun` | `deeb7138c755af7427e3ee11f6244bb1cf2dbf94` | PR CI [31582333551](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31582333551) attempt 1; failed Windows job [94068071202](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31582333551/job/94068071202); the same run's no-code attempt 2 later passed | Windows observed the truthful accepted/still-working channel response while the test asserted the eventual mock response. This is a preserved failed receipt and a missing durable-correlation regression, not an execution failure. The rerun and later merge do not rewrite it as a pass. |
| MAIN-2026-08-12-S1-COVERAGE-GAP | `deeb7138c755af7427e3ee11f6244bb1cf2dbf94` | `merged` | `deeb7138c755af7427e3ee11f6244bb1cf2dbf94` | Owner-exception [PR #341](https://github.com/John-MiracleWorker/Kestrel/pull/341); exact-main attempt-1 [CI 31588036325](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31588036325), [determinism 31588036297](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31588036297), and ancillary [release rehearsal 31588036357](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31588036357) | The narrow owner decision is merged and exact-main checks passed, but the seven-test runtime contract omitted the public webhook failure exposed by PR #341. This receipt is explicitly insufficient to qualify REL-001/002/003 or S1. The repair requires an additive public `run_id`, deterministic accepted-to-terminal API correlation, an eight-test/480-execution contract, fresh exact-head evidence, and later exact-main evidence without rerun. |
| PR342-2026-08-13-LAN-CI-FAIL | `cf5ab301180340fc5a8ae323aa5d29928e0eee45` | `unmerged; superseded on PR #342` | — | Attempt-1 [push CI 31683286186](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31683286186), failed Windows job [94393448052](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31683286186/job/94393448052); concurrent attempt-1 [PR CI 31683321361](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31683321361) 14/14; successful attempt-1 [determinism 31683321383](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31683321383), aggregate artifact `9174792983`, API digest `671de541a63542fdb548dd3e7923f3e517b5832323801508b2b84ec9b22c2c2c`, aggregate JSON SHA-256 `749006e320fbe0443c1a194af1c4da87bb8976e4ef2ce2c7e1be771404954e37` | The five-cell eight-test contract passed 100/100 repeats with 480 runtime and 840 golden executions and zero failures/flakes/cleanup failures, but it omitted the LAN test that then failed the broad Windows suite at a fixed three-second terminal poll. The concurrent PR Windows copy passed the same full-test step, confirming scheduler sensitivity. This receipt remains failed and cannot qualify S1; the repair uses an explicitly controlled executor and expands the repeated contract to nine tests/540 executions. |
| PR342-2026-08-13-S1 | `894b31ce1d3b6353a4257948bbcd3e9912ceda2f` | `merged` | `dbe9313c8671e2ba7507f73cc434569a59ebf785` | Exact tree `e7cf79be8d95b5b307827ba694e00a7dda63c90b`; attempt-1 [push CI 31687437405](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31687437405) 14/14, [PR CI 31687440512](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31687440512) 14/14, and [determinism 31687440395](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31687440395) 7/7; aggregate artifact `9176317110`, API digest `9577de368e35a1480c8599612d25bb3b7718d199eaa47f947e8a63c55e2c1edf`, aggregate JSON SHA-256 `56efe738552d2dda94c7613a11a814c1f86db3d4f636d1d492545b01d89e71a5`; clean local 20-by-9 report SHA-256 `09610a24c64191858469835b544c0eaf1b58097e8361d998b39d9093f9ab9360`; [hosted Codex review](https://github.com/John-MiracleWorker/Kestrel/pull/342#issuecomment-5278658094) reviewed `894b31ce1d` with no major issue; zero unresolved review threads | Five of five cells, 100/100 repeats, 540 runtime and 840 golden executions, ordinary Windows, downstream Docker, and full local tests passed with zero failures, flakes, cleanup failures, or deadline overruns. This is exact-head candidate evidence; protected-main binding is recorded separately below. |
| MAIN-2026-08-13-S1-QUAL | `dbe9313c8671e2ba7507f73cc434569a59ebf785` | `merged` | `dbe9313c8671e2ba7507f73cc434569a59ebf785` | Signed [PR #342](https://github.com/John-MiracleWorker/Kestrel/pull/342) merge, tree `e7cf79be8d95b5b307827ba694e00a7dda63c90b`, parents `deeb7138c755af7427e3ee11f6244bb1cf2dbf94` / `894b31ce1d3b6353a4257948bbcd3e9912ceda2f`; exact-main attempt-1 [CI 31690911759](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31690911759) 14/14, Windows `94417759252`, Docker `94426161166`; [determinism 31690911872](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31690911872) 7/7, aggregate artifact `9177709996`, API digest `c2fa169d0b69020b0243f277ca6fded03f762f61699f8372da87f0bc66ce0beb`, aggregate JSON SHA-256 `aa705501806927d73fa25d94ac702240e9a6319bfd0b4f73f42e0cbdfdeecb3f`; safe 146-file download and exact verifier replay; fresh clean detached-worktree command `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /Users/tiuni/.codex/worktrees/kestrel-v06-s1-channel-reliability/.venv/bin/python -m pytest -q` exited 0 with 5,298 tests, output SHA-256 `94094d68e15e8c90632114b1057c272051fca3bae62d8ac94b1a329832e0cc6a`, collection-output SHA-256 `10395c2052657482089ccfef26418bc73e0a183ad26b5266911347c50c6c6874`, command-record SHA-256 `49e40d97ce3f7144f7de354d70ddf072f60efc2185e02cc2cbee0409076350dd`, structured-receipt SHA-256 `08f27d84a5d16726f698b6c632ea50e8ec127959d97efa8422071c1e988164d8`, and owner-local receipt root `/Users/tiuni/.codex/evidence/kestrel-v06-s1-channel-reliability/dbe9313c8671e2ba7507f73cc434569a59ebf785/local-full-suite-2026-08-13`; ancillary attempt-1 [release rehearsal 31690911883](https://github.com/John-MiracleWorker/Kestrel/actions/runs/31690911883), artifact `9177349607`, API digest `b56c9ae721cac7ff05842e810eb5846d9861945659ec847758a61f9c056cc480`, report SHA-256 `d5564d46a8907299e0b2943ebadb26cd5db6b905320b8c096e797c4b4d122da2` | Protected-main closure passed 5/5 cells, 100/100 repeats, 540 runtime and 840 golden executions, zero failures/flakes/cleanup failures/diagnostics, plus ordinary Windows, LAN adversarial, Docker security controls, and local full regression. The owner-local path is not remotely retrievable; its recorded digests integrity-bind the retained receipt. Together with qualified S0 and the merged narrow owner exception, this qualifies only REL-001, REL-002, REL-003, and S1. The one rehearsal is ancillary: REL-004/REL-005, S2+, installed artifacts, promotion, publication, and final release qualification remain unsatisfied. |

Append new rows; do not rewrite a failed or superseded receipt into a pass.
