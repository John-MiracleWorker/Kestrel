# Everyday Kestrel v0.5 Implementation Review

Date: 2026-07-28

Branch: `feat/everyday-kestrel-v05`

Profile reviewed: single-user, single-node, local/private

## Decision

This branch implements the integrated “first useful hour” slice of the
Usefulness Roadmap. It does not claim that the v0.6, v0.7, or ecosystem backlog
is complete.

The delivered slice connects project setup, repository intelligence, provider
bootstrap APIs, mission preflight, durable launch, repair review, exact local
acceptance actions, determinism evidence, Windows diagnostics, and release
rehearsal without weakening Kestrel's existing approval, isolation, provenance,
or rollback boundaries.

No remote push, pull request, release tag, package publication, or production
deployment was performed as part of this implementation review.

## Implemented

### Task-first Mission Control

- The default workbench begins with a project and one engineering objective.
- Goal templates cover repository explanation, failing tests, features, safe
  refactors, security review, and documentation.
- Server-produced preflight binds the project revision, objective, editable
  plan, routing inventory and policy, budget, Git branch/HEAD/tree/worktree
  state, repository-index baseline, required capabilities, validation recipes,
  and rollback strategy.
- Launch revalidates those bindings. Project, plan, Git, routing, index, or
  capability drift fails closed before a run is admitted.
- The run timeline and repair review remain in the same task-oriented surface;
  older control planes remain under Advanced and Diagnostics.

### Projects and repository intelligence

- SQLite schema v20 adds revisioned local Project records and nullable
  run-to-project bindings while preserving legacy runs.
- Project records include a canonical repository, default branch, allowed-path
  ceiling, narrowing capability ceiling, provider policy, budget, privacy
  class, validation/build recipes, and baseline-index digest.
- Import/export is reviewable and redacted. Project authority can narrow, but
  cannot expand, the owner-level runtime configuration.
- A rebuildable SQLite index remains separate from canonical Memvid v2 memory.
- Authenticated generation receipts and content fingerprints reject stale,
  incomplete, rolled-back, or tampered index evidence.
- Bounded Python, TypeScript/JavaScript, Go, Rust, Java/Kotlin, Swift, and text
  adapters record files, symbols, imports, lexical references, and test
  relationships.
- `repo.symbols`, `repo.references`, `repo.dependencies`, `repo.tests_for`,
  `repo.impact`, and `repo.context_pack` return exact path/line/digest/freshness
  evidence inside the project path ceiling.

### Provider bootstrap

- Discovery supports Ollama, LM Studio, and generic OpenAI-compatible model
  catalogs.
- Bounded probes record generation, streaming, tool, structured-output,
  latency, freshness, and evidence provenance where supported.
- Discovered targets are drafts and remain disabled until the owner confirms
  trust and role. Removed discovery-managed models become stale.
- Local Only, Balanced, Cheapest Validated, Fastest, Frontier Review, and
  Privacy First presets constrain existing eligibility rather than granting
  capabilities.

### Review and local acceptance

- The review UI accepts only bounded current-schema validation and review
  projections; raw task JSON cannot impersonate approval authority.
- Acceptance criteria map to recorded validation evidence. The panel displays a
  redacted unified/split diff, changed files, review summary, risk, rollback,
  and exact-call local commit/export preparation.
- `git.export_patch` requires the current signed review ID and candidate digest,
  revalidates under the repair lock, and renders the authenticated manifest in
  a private Git index.
- Exact export covers staged, unstaged, deleted, untracked, binary, spaced, and
  Unicode paths. It is bounded, refuses registered credential material, obeys
  project artifact paths, and can be applied with `git apply --binary`.
- Repair commit/export still require exact-call approval. Protected branches,
  stale reviews, and missing validation remain blocked.

### Reliability

- Golden reports use canonical ordering and seeded inputs. A 20-repeat
  determinism runner compares exact projections and writes a fail-closed
  aggregate receipt.
- The aggregate receipt binds seed, repeat count, source commit, case and
  iteration timeouts, and the configured maximum-case latency gate. Release
  verification requires the release-qualified values.
- A repeated determinism CI lane publishes the receipt instead of treating one
  lucky pass as evidence.
- Timing-sensitive paths use monotonic deadlines or poll-with-deadline behavior.
- The PowerShell bootstrap/doctor is non-mutating and produces actionable native
  Windows prerequisite diagnostics.
- Exact-patch subprocesses use Windows Job Objects. Assignment and resume
  failures directly reap a suspended leader instead of leaving an orphan.
- Release rehearsal exercises the finalized tag, archive, wheel, isolated
  install, and conflict behavior in a disposable namespace before the
  production release workflow may publish an immutable tag.

## Rendered qualification

The production web bundle was served by the local authenticated Kestrel server
with a temporary state store and mock provider.

- Desktop viewport: objective, project, templates, plan, and preflight rendered
  as one task-first workspace with no horizontal overflow.
- Mobile viewport at 390 by 844: the three-column surface collapsed into a
  readable single-column flow with a 352-pixel objective editor and no
  horizontal overflow.
- Selecting “Fix failing test” populated the objective and enabled inspection.
- Inspection showed the real branch and dirty-tree warning, routing and budget,
  stale-index warning, mock-provider limitation, validation recipes, rollback,
  missing capability blockers, and a disabled Run mission action.
- Editing a plan produced labeled title and acceptance-criteria fields and
  triggered a new preflight before launch could be considered.
- Browser console warnings/errors: zero.

Fidelity ledger:

1. One dominant engineering-objective action: matched.
2. Project and repository context visible before launch: matched.
3. Plain-language plan with rationale and acceptance criteria: matched.
4. Route, budget, permissions, validation, index, and rollback preflight:
   matched.
5. Progressive disclosure for non-mission controls: matched.
6. Fail-closed blockers explained before launch: matched.
7. Desktop and narrow/mobile layout without horizontal overflow: matched.

## Deliberately deferred

These roadmap items are not represented as complete:

- production Adaptive Flock shadow integration, provider usage/cost attribution,
  per-family calibration, route regret, escalation ladders, and
  evidence-gated learned authority;
- project-scoped Memvid retrieval and learned-routing history;
- dynamic DAG amendments and isolated multi-candidate fan-out/selection;
- a governed Playwright project-validation container with screenshot, DOM,
  network, console, accessibility, and visual-diff evidence;
- plan-time approval packets;
- outcome analytics, private benchmark replay, and policy A/B dashboards;
- project-aware GitHub pull-request creation and CI/review-comment re-entry;
- dependency-locked extension distribution, portable containment parity, rich
  schedule semantics, and idempotent external routine delivery;
- a provider-discovery onboarding screen and fresh live-provider certification;
- native Windows execution evidence beyond the Windows CI/doctor contracts.

## Qualification boundary

Local tests and rendered browser evidence qualify this implementation branch,
not a production release. Live provider calls, OCI repair validation, native
Windows/WSL2 execution, GitHub mutation, hosted CI, package publication, and
installation on a fresh external workstation require separate receipts.
