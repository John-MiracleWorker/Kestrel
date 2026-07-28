# Kestrel Usefulness Roadmap Implementation Review

Date: 2026-07-28

Branch: `feat/everyday-kestrel-v05`

Supported profile: single-user, single-node, local/private

## Decision

This branch implements the roadmap as three integrated, default-safe product
slices plus the bounded extension/routine follow-through:

- v0.5 makes the first useful hour task-first;
- v0.6 closes the measured Adaptive Flock shadow/constrained-learning loop;
- v0.7 adds bounded replanning, isolated candidate comparison, browser proof,
  approval packets, outcome benchmarks, and review-bound GitHub shipping;
- the later extension/routine slice adds provenance, reproducible review
  receipts, named-timezone cron, durable delivery idempotency, and explicit
  reconciliation.

The implementation preserves Memvid v2 as canonical layered memory, keeps the
SQLite control plane separate, treats recalled content as untrusted, retains
exact-call approval, and does not add host fallback to candidate or browser
execution.

No remote push, pull request, release tag, package publication, production
deployment, or live third-party delivery was performed for this review.

## Implemented

### Task-first product and repository intelligence

- Mission Control joins project selection, objective templates, immutable
  preflight, editable acceptance plans, one run timeline, repair review, and
  exact local acceptance actions.
- Project profiles scope paths, capability ceilings, budgets, provider policy,
  validation recipes, repository-index binding, memory retrieval, and routing
  outcomes.
- The rebuildable structural index covers Python, TypeScript/JavaScript, Go,
  Rust, Java/Kotlin, Swift, and text without becoming canonical memory.
- `repo.context_pack` now extracts high-signal terms from natural-language
  navigation questions and deterministically ranks definitions, references,
  imports, and test ownership. Every returned snippet is path/line/digest bound;
  stale, mixed-generation, changed-during-read, and out-of-scope evidence fails
  closed.
- A machine-readable eleven-case, seven-language navigation benchmark gates
  recall@5, evidence coverage, and identical replay in CI.

### Adaptive Flock

- Durable route outcomes carry provider/model identity, usage, latency,
  fallback/retry evidence, decision-snapshotted token pricing, and attributable
  actual cost.
- Static, learned-shadow, and actual choices are persisted together with
  confidence, utility delta, regret, eligibility, and abstention evidence.
- Calibration is scoped by project, task family, risk, capability requirements,
  and target. Transport, capability, and contract failures follow different
  escalation paths.
- Learned routing remains default-off and can activate only for low/medium-risk
  work after minimum support, confidence, utility, cost-coverage, eligibility,
  and deterministic replay gates pass. High-risk routing remains deterministic.

### Bounded parallel engineering and shipping

- Graph amendments support add, split, dependency replacement, cancellation,
  and evidence requests with deterministic cycle, node, tool, risk, cost,
  acceptance, scope, and approval validation. Payloads reject registered
  secrets and non-finite JSON before hashing or persistence.
- Candidate fan-out preserves one immutable task contract, uses isolated
  worktrees, records trusted validation/review/cost/latency evidence, and never
  selects solely from model preference. Candidate results and review
  provenance reject registered secrets before durable persistence.
- `browser.validate` is separately default-off, exact-call approved, bound to
  the durable candidate workspace/digest, and OCI-only with no host fallback.
  The repository includes a version-aligned Playwright/axe image, networkless
  fixture interception, bounded screenshots/DOM/console/network/accessibility
  evidence, registered-secret rejection before execution, and a hardened CI
  self-test. Every requested assertion and interaction must have an
  identity-matched boolean result; omitted or substituted evidence fails
  closed.
- Every engineering control-plane mutation requires configured owner API
  authentication; this does not replace the independent exact-call gate for
  side-effecting tools.
- Approval packets reduce interruptions while retaining an individual
  single-use call digest, arguments, capability revision, resource binding, and
  decision for every exact call; both call arguments and display text reject
  registered secrets before persistence.
- The Mission timeline renders amendment, candidate, browser, approval, and
  GitHub evidence without requiring raw event JSON.
- Pull-request creation requires a current signed repair review, the unchanged
  reviewed tree, explicit remote-mutation/push configuration, and exact-call
  approval. CI/review feedback can re-enter only as a bounded recovery
  amendment.

### Outcomes, extensions, and routines

- Outcome analytics distinguish missing evidence from zero and group validated
  completion, time, cost, retries, interventions, approval wait, patch
  acceptance, rollback, route regret, and evidence coverage by project/task
  family/target/policy/time. Browser evidence coverage is not conflated with
  pass rate, and strongest-model comparison uses the declared quality tier
  rather than choosing the historically luckiest target.
- Private benchmark fixtures are redacted, acceptance criteria are immutable,
  replay links are durable, and exports remain machine-readable and redacted.
- Plugin manifests now bind dependency locks, compatibility ranges,
  source/commit digests, optional Ed25519 signatures, authority deltas, and
  reproducible install receipts. Raw registered secrets are rejected, and
  receipts distinguish reproducible source from a runnable dependency
  environment so unmanaged dependencies cannot claim runtime reproducibility.
  Enabled updates that add authority fail closed for fresh review.
- Proactive routines support five-field cron in named IANA timezones with
  deterministic gap/fold handling. Optional outbound delivery has one durable
  destination-bound idempotency key, receipts, leases, and explicit
  reconciliation; an ambiguous effect becomes `uncertain` and is never
  silently retried. Provider receipts are recursively redacted, and all
  persisted delivery receipts must be bounded, finite, and secret-safe.

## Local verification

- Full deterministic backend suite: exit `0`; `2539` tests collected. Explicit
  live-provider, Memvid, MCP, Windows, and Docker integration cases remain
  opt-in and were skipped where their flags/environments were absent.
- Python byte compilation: `benchmarks`, `src`, `tests`, and `scripts` pass.
- Ruff: all benchmark, script, source, and test files pass.
- Mypy: all `170` source files pass strict project typing; the new navigation
  benchmark also passes standalone strict typing.
- Project metadata and lockfile alignment pass.
- Repository navigation benchmark: recall@5 `1.000`, precision@5 `0.764`, MRR
  `0.955`, authoritative evidence coverage `1.000`, identical replay `true`.
- Web: `8` test files and `81` tests pass; dependency notice check and
  high-severity audit pass; the production Vite build succeeds.
- Workflow/shell/security preflight: actionlint, ShellCheck, and the
  high-severity Bandit gate pass.
- Browser image source: JavaScript syntax and high-severity npm audit pass.
- Rendered desktop workbench: real server APIs populated project preflight,
  task plan, durable run timeline, engineering/shipping evidence, and explicit
  outcome missing-data states. At `1280 x 720`, the document width equals the
  viewport and has no horizontal overflow.
- Rendered mobile workbench: at `390 x 844`, Mission Control collapses to a
  readable task-first flow with document width `390` and no horizontal
  overflow.

## Qualification boundaries

- The local Docker daemon was not responsive, so this exact tree's browser
  image build/networkless self-test and existing Docker-backed containment
  integrations require hosted CI or a working local daemon. The source,
  immutable base, contract tests, and CI gate are present; that is not a runtime
  image qualification claim.
- The twenty-repeat determinism workflow, ten-run native Windows streak, and
  disposable release rehearsal are implemented as required gates, but this
  unpushed tree has no hosted run evidence.
- No live provider, GitHub pull request, external channel delivery, or
  credentialed provider-certification run was attempted. Deterministic mocks
  and local state exercise the contracts without claiming third-party effects.
- Structural repository navigation now has a deterministic quality floor.
  Optional embeddings, larger real-repository relevance studies, and deeper
  language-specific type/call resolution remain precision improvements, not
  hidden completion claims.
- Dependency locks are reviewed and receipt-bound; Kestrel does not silently
  install arbitrary third-party packages on the host. Docker remains the
  qualified OCI engine, and no weaker portable-engine fallback was introduced.
