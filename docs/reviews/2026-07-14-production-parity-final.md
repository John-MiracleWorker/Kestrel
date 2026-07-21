# Kestrel production-parity final review

Date: 2026-07-14
Branch: `fix/runtime-rescue`
Scope: production-grade, single-user, single-node, local/self-hosted deployment
Published recommendation: `v0.2.1` remains the only published/recommended release

> **Historical pre-publication snapshot:** This review predates the `v0.3.0` and `v0.3.1`
> releases published on 2026-07-16. See the [changelog](../../CHANGELOG.md) for the current release
> history; the findings below remain unchanged as evidence of the state reviewed on 2026-07-14.

## Verdict

**Local deployment acceptance: PASS.** The current working tree satisfies the production-parity plan for the declared single-user, single-node topology on the verified macOS host. Durable ownership, startup recovery, cancellation, bounded admission, immutable effective configuration, provider resilience, API/channel controls, operational telemetry, Memvid recovery, packaging, launchd, Codex CLI, and Telegram paths were exercised with real outputs.

**Release acceptance: HOLD.** The production-parity work is intentionally uncommitted and unreleased. The exact final bytes have not run in GitHub's Linux/macOS/Windows matrix because publishing a branch or pull request was outside the authorized scope. Do not move the public recommendation from `v0.2.1` until a deliberate commit/review/CI/release action is requested and passes. Historical Telegram credentials should also be rotated before a new release is treated as fully remediated.

## Independent review ledger

Three independent pre-final reviews covered specification compliance, application security, and code quality/concurrency. Their high-value findings were remediated as follows:

| Finding | Resolution | Evidence |
|---|---|---|
| Stale workers could mutate after lease expiry | Owner/generation checks now also require an unexpired durable lease; replacement ownership advances generation | State-store expiry/reclaim regressions |
| Startup could poison a healthy owner | PID-bearing live owners with fresh leases are preserved; expired or provably dead owners are reconciled terminally | Startup live-owner and killed-owner chaos tests |
| Concurrent cancellation could duplicate effects or strand queue capacity | Cancellation is idempotent, emits one durable cancellation transition, removes queued reservations, and cancels run-owned subprocess groups | Concurrent cancellation and queued-capacity tests |
| Approval could race callbacks or cancellation | Approval decisions are atomic; only the winning callback resumes; cancelled/failed runs never execute a late approved tool | Concurrent approval and cancelled-run approval tests |
| Run admission was process-local | Durable nonterminal admission is checked under `BEGIN IMMEDIATE`; local reservations are unwound on every failed creation/scheduling path | Backpressure, cleanup, and concurrent durable-admission tests |
| Effective configuration snapshots were partial | Every `AgentConfig` field is frozen in a schema-versioned, revision-hashed per-run snapshot and reconstructed for resume/retry | All-field immutable snapshot regression |
| Runtime settings evolution could invalidate older revision hashes | Stored documents retain raw-document revision compatibility while new saves adopt the complete schema | Legacy runtime-settings revision test |
| Provider health keyed only by provider/model | Circuit identity now includes endpoint and credential-reference identity; readiness distinguishes configured, unverified, degraded, and healthy | Provider resilience and readiness tests; live startup probe |
| Fallback could reuse the primary credential reference or lose provenance | Fallback resolves its own/default credential reference, validates capability compatibility, and persists sanitized fallback metadata | Provider factory/resilience regressions |
| Half-open streaming could remain wedged if iteration was abandoned | Probe ownership is released on generator close or nonstandard interruption | Half-open abandoned-stream regression |
| Readiness could use stale config or ignore expired leases | Routes use a live config accessor; expired/dead-owner running leases fail readiness | Operational-metrics regressions |
| Telegram allowlists could fail open | Telegram ingress now rejects absent, empty, malformed, wrong-chat, and wrong-user allowlists before run creation | Channel tests and live HTTP 400/no-run verification |
| Request limits trusted `Content-Length` and rate keys were unbounded | Middleware authenticates first, bounds actual streamed bytes, and caps tracked client cardinality | Server-support and full-runtime tests |
| Secret-shaped dictionary values and exception text could leak | Central redaction now handles semantic secret keys while preserving nonsecret fields such as `token_configured`; durable exception text is sanitized | Event-log and observability regressions |
| File writes had a symlink race | POSIX writes walk directories with `dir_fd`/`O_NOFOLLOW` and atomically replace; Windows uses checked atomic replacement; secret-store paths remain forbidden | Workspace-tool tests and static gates |
| CLI/process tools killed only direct processes or lacked Windows groups | Codex and tool subprocesses run in process groups; timeout, explicit cancellation, and lease loss terminate descendants; Windows uses process groups plus `taskkill /T` | Real descendant timeout test and cancellation regressions |
| Memvid backup/restore could race writers or partially replace files | Cross-process shared/exclusive memory locks, checksum/path/hardlink validation, private staging, whole-directory swap, safety backup, rollback, fsync, and retention | Malicious-manifest, hardlink, injected-failure, and six-layer restore drills |
| Schema initialization could race | Schema migration uses serialized `BEGIN IMMEDIATE`, rejects future schemas, and enables WAL after migration | Concurrent fresh-database and future-schema tests |
| Startup/provider readiness could remain unknown forever | Optional persisted startup probe records actual provider success/failure; deployed Codex CLI has the probe enabled | Live restart reported provider `healthy` |
| Shell scripts lacked a real ShellCheck gate | ShellCheck 0.11.0 installed and passed; CI now includes ShellCheck | Local ShellCheck plus `bash -n` |

## Final verification matrix

| Area | Result |
|---|---|
| Backend tests | **PASS** — 632 collected; 601 passed and 31 live/integration-gated tests skipped by design |
| Ruff | **PASS** — `scripts`, `src`, and `tests` |
| Mypy | **PASS** — 104 source files |
| Compile | **PASS** — source, tests, and scripts |
| Bandit high-severity gate | **PASS** |
| ShellCheck / shell syntax | **PASS** — ShellCheck 0.11.0 and `bash -n` |
| Golden evaluations | **PASS** — 21/21, zero false promotions |
| Frontend tests | **PASS** — 37/37 |
| Frontend production build | **PASS** |
| npm audit | **PASS** — zero vulnerabilities at the high gate |
| Diff hygiene | **PASS** — `git diff --check`; no staged files or Git operation markers |
| Wheel/sdist | **PASS** — both built; isolated current-wheel install passed; packaged React assets and schema-13 state store present |
| Hostile environment | **PASS** — isolated CLI and server ignored hostile provider/path environment and ran with explicit mock settings |
| API soak/saturation | **PASS** — bounded concurrent run accepted overloads explicitly; no unexpected failures; p95 below gate; no stranded active records |
| Durable crash recovery | **PASS** — killed PID owner reconciled without replaying side effects; fresh live owner preserved |
| Upgrade/rollback compatibility | **PASS** — public `v0.2.1` schema 11 upgraded to current schema 13; state backup restored and public `v0.2.1` reopened it |
| Memvid recovery | **PASS** — all six `.mv2` layers backed up, mutated, restored, integrity-checked, and reopened |
| launchd | **PASS** — private mode-0600 credential-free plist, exact checkout loaded, restart healthy |
| Liveness/readiness | **PASS** — live and ready; durable store writable; provider operational; schema 13 |
| Provider | **PASS** — real `codex-cli` startup probe and exact-response run completed |
| SQLite | **PASS** — schema 13; zero queued/running/blocked records after verification |
| Telegram ingress | **PASS** — unauthorized ingress HTTP 400 and durable run count unchanged |
| Telegram egress | **PASS** — real test message returned HTTP 200 with `ok=true` |
| Telegram poller | **PASS** — operational metrics reported `healthy` |
| Public release integrity | **PASS** — `v0.2.1` still resolves to commit `1a9ce8caec2a8a5df56c5fe8e9a7746e390581c7` and retains the expected installer, wheel, and sdist |

## Declared boundaries and remaining release actions

1. **Supported topology:** one Kestrel server/worker process on one node. Multi-node scheduling, distributed consensus, and multi-tenant isolation are explicitly unsupported.
2. **Authentication:** unauthenticated HTTP is allowed only for an intentional loopback-only deployment. Non-loopback exposure requires bearer authentication and host/origin policy.
3. **Provider probe:** an operational readiness result may consume a minimal provider request when `provider_startup_probe` is enabled.
4. **External credential lifecycle:** rotate the historical Telegram token and replace the obsolete Ollama credential before relying on those credentials for a new release.
5. **Exact-byte CI:** commit the intended diff, exclude unrelated `tiuni-fun/`, open a review branch/PR, and require Linux/macOS/Windows Python, web, Docker, ShellCheck, package, chaos, and Memvid integration gates.
6. **Publication:** only after item 5 passes should a reviewed version bump, immutable tag, release artifacts, clean install, upgrade, rollback, and post-publish smoke be performed.

No commit, push, merge, tag, or release was performed by this review.
