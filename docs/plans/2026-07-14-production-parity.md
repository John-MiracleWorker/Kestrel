# Kestrel Single-Node Production-Parity Plan

**Date:** 2026-07-14
**Scope:** Production-grade, single-user, single-node local/self-hosted agent comparable to the operational profile of Hermes/OpenClaw. Multi-tenant SaaS identity, billing, and horizontal cluster orchestration are explicitly out of scope.

## Frozen baseline

- Branch: `fix/runtime-rescue`
- HEAD: `1a9ce8caec2a8a5df56c5fe8e9a7746e390581c7`
- Existing tracked rescue patch SHA-256: `bda5ca72f8789bc7242d7f9679455c4b13265c292d1ab08462166fa46314fcfe`
- Existing rescue changes are user-approved work and must be preserved.
- Unrelated `tiuni-fun/` remains excluded from edits, tests that mutate it, staging, packaging, and releases.
- No commit, push, merge, tag, credential rotation, or public release is authorized by this plan.

## Completion definition

Kestrel is production-ready for this scope only when all of the following are true:

1. A forced process exit cannot leave a run permanently active; startup deterministically reconciles every non-terminal run and records why.
2. Active execution is owned by an expiring lease with heartbeat, and stale owners cannot complete or overwrite newer ownership.
3. Cancellation is idempotent and late background completion cannot revive a terminal run.
4. Every run records a redacted, versioned effective-configuration snapshot and its revision/provenance.
5. Provider status distinguishes configured, authenticated, healthy, degraded, and unavailable; transient failures are bounded by retry budgets and a circuit breaker.
6. Fallback is explicit, capability-compatible, observable, and never retries unsafe side effects.
7. Non-loopback API exposure fails closed without authentication; channel ingress is authenticated/authorized before event or run creation; abusive request bursts are bounded.
8. Run admission has a configured capacity; excess work receives deterministic backpressure instead of unbounded thread creation.
9. Subprocess tools run in their own process group with timeout, bounded output, termination escalation, and auditable exit metadata.
10. Operators have liveness, readiness, redacted diagnostics, correlated run/provider/tool metrics, and actionable stuck-run/provider/poller signals.
11. All six Memvid v2 layers support verified, atomic backup and restore without calling `create(path)` on an existing `.mv2` file.
12. Memory capacity, retention, integrity, migration/version compatibility, and failure behavior are visible and fail closed.
13. Upgrade preflight, backup, migration, post-upgrade health, and rollback are executable and tested.
14. Restart, timeout, rate-limit, disk/corruption, duplicate-ingress, concurrency, and upgrade/rollback tests pass.
15. Backend, lint, typecheck, compile, golden, Memvid integration, MCP integration, frontend, dependency-audit, shell syntax, security scan, and live smoke gates pass on the final bytes.
16. Independent spec, security, and quality reviewers approve the exact final patch with no critical or important findings.

## Phase 1 — Durable lifecycle and recovery

### Task 1.1: Schema and run ownership

**Files:**
- Modify `src/nested_memvid_agent/state_store.py`
- Modify/add focused lifecycle tests in the existing state-store/server test modules

**Changes:**
- Add a forward-only SQLite schema migration for run ownership fields: lease owner, lease generation/fencing token, lease expiry, heartbeat timestamp, interruption/recovery metadata, and immutable configuration revision reference.
- Add indexed queries for non-terminal and expired runs.
- Add compare-and-swap lease acquire/renew/release APIs.
- Make lease generation monotonic so stale workers are fenced out.

**RED tests:**
- Two owners cannot hold the same run lease.
- Expired lease can be reclaimed with a larger generation.
- Stale generation cannot mutate a reclaimed run.
- Schema migration preserves existing run history.

### Task 1.2: Startup reconciliation

**Files:**
- Modify `src/nested_memvid_agent/run_manager.py`
- Modify `src/nested_memvid_agent/server.py`
- Modify CLI manager construction in `src/nested_memvid_agent/cli.py`
- Add startup/restart tests

**Changes:**
- Reconcile `queued`, `running`, and `blocked` runs before accepting new work.
- Requeue only work proven side-effect-free and resumable; otherwise transition to `interrupted` or terminal `failed/cancelled` with a machine-readable reason.
- Preserve pending approvals but mark their run as blocked/recoverable.
- Emit durable recovery events.

**RED tests:**
- Startup leaves no unowned `running` run.
- Approval-blocked work remains decidable after restart.
- Interrupted provider/tool work is not silently replayed.
- Reconciliation is idempotent across repeated startups.

### Task 1.3: Heartbeats and idempotent cancellation

**Files:**
- Modify `src/nested_memvid_agent/run_manager.py`
- Modify relevant state/event tests

**Changes:**
- Heartbeat while a run is owned.
- Fence every terminal transition by lease generation.
- Make cancel idempotent, release ownership, and prevent late completion from overwriting the terminal state.
- Ensure shutdown marks owned runs interrupted before resource teardown when possible.

**Acceptance gate:** focused lifecycle tests plus `pytest -q` pass.

## Phase 2 — Versioned effective configuration

### Task 2.1: Config envelope and migration

**Files:**
- Modify `src/nested_memvid_agent/runtime_settings.py`
- Modify `src/nested_memvid_agent/config.py`
- Modify `src/nested_memvid_agent/server_runtime_routes.py`
- Add runtime-settings migration/provenance tests

**Changes:**
- Replace unversioned persisted settings with a schema-versioned envelope containing revision, updated-at, mutable settings, and redacted source provenance.
- Preserve launch-time security policy as immutable from runtime APIs.
- Validate unknown fields and support migration from the current flat JSON shape.
- Keep atomic temp-file replacement and add restrictive permissions.

### Task 2.2: Per-run immutable snapshot

**Files:**
- Modify `src/nested_memvid_agent/state_store.py`
- Modify `src/nested_memvid_agent/run_manager.py`
- Modify run API/trace serialization tests

**Changes:**
- Hash a canonical redacted effective configuration.
- Persist the snapshot/revision when creating a run.
- Reconstruct a resumed run from its snapshot rather than mutable global state.
- Never persist secret values.

**Acceptance gate:** changing runtime settings after run creation cannot alter that run's provider, model, permissions, workspace, or recovery behavior.

## Phase 3 — Provider resilience

### Task 3.1: Health state and error taxonomy

**Files:**
- Modify provider abstraction under `src/nested_memvid_agent/llm/`
- Modify `src/nested_memvid_agent/setup_readiness.py`
- Modify provider/runtime routes and tests

**Changes:**
- Add redacted provider health records with state, last probe, latency, consecutive failures, retryability, and capability metadata.
- Classify authentication, rate-limit, timeout, transport, server, invalid-request, and capability errors.
- Use bounded probes that do not expose prompts or credentials.

### Task 3.2: Circuit breaker and safe fallback

**Files:**
- Modify provider construction/execution and `src/nested_memvid_agent/run_manager.py`
- Add deterministic fake-provider tests

**Changes:**
- Implement closed/open/half-open circuit states with injectable monotonic clock.
- Retry only classified transient pre-side-effect failures within total attempt/time budgets.
- Permit fallback only when explicitly configured and capability-compatible.
- Record selected provider, fallback reason, attempt count, and circuit state in durable events/spans.

**Acceptance gate:** deterministic tests cover 401, 429, timeout, recovery probe, open circuit, successful half-open close, incompatible fallback, and no duplicate side effect.

## Phase 4 — Security and trust boundaries

### Task 4.1: API exposure and authentication

**Files:**
- Modify `src/nested_memvid_agent/server.py` and `server_support.py`
- Add security middleware/tests

**Changes:**
- Refuse non-loopback startup unless API auth is enabled and a resolvable token exists.
- Keep health/liveness disclosure minimal; protect mutating and sensitive read endpoints.
- Compare credentials in constant time and make token rotation/reload behavior explicit.
- Add bounded token-bucket request admission keyed by authenticated principal and source.

### Task 4.2: Channels, secrets, and capabilities

**Files:**
- Review/modify channel manager/routes/adapters, secret broker, tool registry, plugin/MCP boundaries
- Add direct-ingress, callback, webhook, secret-redaction, and capability tests

**Changes:**
- Apply provider-specific signature/secret checks and sender/workspace allowlists before durable events.
- Separate channel read/admin scopes.
- Ensure high-risk tool approval is exact-call, single-use, expiry-bound, principal-bound, and run-bound.
- Ensure logs, diagnostics, snapshots, and errors redact known secrets.
- Add explicit filesystem/network/secret capability declarations for executable extensions; default deny.

**Acceptance gate:** unauthorized traffic creates no event, approval, session, or run; rate-limited traffic cannot exhaust workers.

## Phase 5 — Admission, workers, and process isolation

### Task 5.1: Bounded run admission

**Files:**
- Modify `src/nested_memvid_agent/config.py`
- Modify `src/nested_memvid_agent/run_manager.py`
- Modify run routes/models and tests

**Changes:**
- Add max active runs and max queued runs.
- Replace unbounded per-run thread creation with a bounded executor/worker loop.
- Persist queue order and expose admission rejection/retry guidance.
- Ensure restart reconciliation feeds the same queue.

### Task 5.2: Subprocess containment

**Files:**
- Modify tool subprocess helpers and Codex/MCP process launchers
- Add process-tree timeout/cancellation/output-limit tests

**Changes:**
- Start subprocesses in isolated process groups/sessions.
- Enforce wall timeout, output byte cap, graceful terminate, and forced kill escalation.
- Capture redacted exit metadata and reap descendants.
- Preserve existing git worktree isolation and enforce cleanup ownership.

**Acceptance gate:** load test proves configured queue bound; cancellation/timeout leaves no test child process alive.

## Phase 6 — Observability and operations

### Task 6.1: Metrics and correlated telemetry

**Files:**
- Modify observability/tracing/event modules and routes
- Add metrics tests

**Changes:**
- Add dependency-light in-process counters/gauges/histograms for runs, queue, provider attempts, tools, approvals, memory, and channels.
- Correlate logs/events/spans by run/session/tool/provider identifiers.
- Provide Prometheus text export and bounded JSON summaries without labels derived from arbitrary user text.

### Task 6.2: Liveness, readiness, and diagnostics

**Files:**
- Modify health/readiness/support-bundle routes and launch scripts
- Add redaction and degraded-state tests

**Changes:**
- Separate process liveness from dependency readiness.
- Readiness includes state DB, run reconciler/worker heartbeat, configured provider state, memory integrity/capacity, and channel poller freshness when enabled.
- Add machine-readable alerts for stale leases, open provider circuits, queue saturation, memory threshold, backup age, and poller staleness.
- Keep support bundles bounded and secret-free.

**Acceptance gate:** simulated failures flip only the correct readiness/alert signals and never leak secret fixtures.

## Phase 7 — Memvid lifecycle guarantees

### Task 7.1: Atomic backup and verified restore

**Files:**
- Modify memory operations/backend/CLI/routes
- Add mock-backend tests and `RUN_MEMVID_INTEGRATION=1` tests

**Changes:**
- Quiesce/seal each layer, snapshot manifest plus six `.mv2` files, checksum, fsync where supported, and atomically finalize.
- Restore into a new temporary location, verify all layers and manifest, then atomically swap only after success.
- Never call `create(path)` on an existing `.mv2` file.
- Reject path traversal, incomplete sets, checksum mismatch, incompatible schema, and active-write restore.

### Task 7.2: Retention, capacity, migration, and deletion

**Files:**
- Modify retention/compaction/verification/status modules and tests

**Changes:**
- Expose per-layer size/capacity/headroom and configurable thresholds.
- Add backup retention without deleting the newest valid backup.
- Validate migration compatibility before mutation and preserve rollback copy.
- Add auditable record/session deletion semantics consistent with Memvid capabilities.
- Keep evidence/provenance/confidence/validation requirements for every promotion.

**Acceptance gate:** corrupted backup and failed restore leave the active six layers byte-identical and readable.

## Phase 8 — Upgrade, rollback, and failure validation

### Task 8.1: Operational scripts and release gates

**Files:**
- Add/update scripts under `scripts/`
- Modify `install.sh`, Makefile, Docker/Compose, release workflow, deployment docs
- Add installer/upgrade contract tests

**Changes:**
- Add preflight, backup, stop, install, migrate, start, post-health, and rollback workflow.
- Preserve config/secrets/state permissions.
- Generate checksums and SBOM; verify packaged React assets and public wheel in isolation.
- Test supported macOS/Linux service paths without assuming an interactive shell.

### Task 8.2: Chaos, concurrency, and soak harnesses

**Files:**
- Add deterministic failure harnesses and opt-in live/soak tests

**Changes:**
- Cover forced restart, provider faults, queue saturation, SQLite contention, duplicate ingress, process timeout, memory corruption, disk-capacity guard, upgrade failure, and rollback.
- Add bounded soak mode that reports machine-readable counts and exits nonzero on leaked workers, stale runs, failed memory verification, or unbounded resource growth.

**Acceptance gate:** release workflow cannot certify artifacts without migration/backup/restore/restart/rollback smoke evidence.

## Phase 9 — Final exact-byte review and verification

1. Re-run `git status`, operation-marker checks, and diff inspection; exclude `tiuni-fun/`.
2. Run current-source and history-aware redacted secret scans without reading values into reports.
3. Run:
   - `make test`
   - `make lint`
   - `make typecheck`
   - `make compile`
   - `make golden`
   - `RUN_MEMVID_INTEGRATION=1 .venv/bin/python -m pytest -q tests/integration`
   - MCP stdio integration
   - `npm test && npm run build && npm audit --audit-level=high` in `web/`
   - shell syntax and ShellCheck when installed
   - high-severity Bandit gate
   - upgrade/rollback and chaos smoke harnesses
   - live loopback API, real provider, and authorized/unauthorized Telegram smoke tests without exposing content or credentials
4. Freeze an explicit-path staged patch only if the user later authorizes commit/release; never stage `tiuni-fun/` or local secrets.
5. Compute patch digest and obtain independent spec, security, and quality reviews against that exact digest.
6. Any subsequent byte change invalidates review and affected test evidence.

## Stop/abort conditions

- Stop before any destructive migration, credential rotation, public exposure, commit, push, merge, tag, release, or modification of `tiuni-fun/` unless explicitly authorized.
- Fail closed if a migration cannot preserve existing state or a backup cannot be verified.
- Do not claim production readiness while any completion criterion or critical/important independent-review finding remains open.
