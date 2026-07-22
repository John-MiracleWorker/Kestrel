# Kestrel Production Operations Runbook

This runbook defines the supported production profile for Kestrel: a single-user, single-node, local or privately networked agent runtime. It is not a multi-tenant Internet service.

## Operational invariants

- Memvid v2 `.mv2` only; one file per nested memory layer.
- Never call Memvid `create(path)` when that `.mv2` already exists.
- Runs are admitted through bounded worker capacity, hold fenced leases while executing, and heartbeat until terminal or blocked.
- Startup never replays interrupted side effects. A fresh run or approval-execution lease whose PID-bearing local owner is still alive is preserved. An expired/dead approval claimant is recorded as `approval_execution_outcome_unknown`; an expired run lease or one owned by a dead local process is reconciled to `failed`. Pending approvals remain `blocked`.
- Subagent workers record their local manager owner; startup preserves live-owner workers and terminally reconciles dead-owner workers and their task nodes.
- Terminal run states are immutable and cancellation emits one durable cancellation event.
- Runtime settings are versioned, checksummed, source-attributed, and snapshotted into every run.
- Schema v18 retains revisioned proactive routines and adds renewable, claimant-only approval execution plus exact scheduler continuation bindings. Schema v17 introduced deterministic leased routine occurrences and atomic internal run admission.
- Proactive routine polling is disabled by default; when enabled, definitions still start disabled, API owner mutations require authentication, and tick/lease/capacity bounds remain finite.
- High-risk tools require an effective capability decision and every applicable master flag, and still require owner-bound, single-use exact-call approval. Pending approvals expire after 15 minutes by default (`NEST_AGENT_APPROVAL_TTL_SECONDS`).
- The API must use authentication when bound beyond loopback.

## Service health

| Endpoint | Meaning |
|---|---|
| `GET /api/health/live` | Process is serving HTTP. Never use for traffic admission. |
| `GET /api/health/ready` | SQLite is writable and on the supported schema, no orphaned running run exists, capacity and Memvid are healthy, required pollers are healthy, and the active provider has been operationally verified. |
| `GET /api/metrics` | Run counts/capacity, provider state, memory utilization, Telegram poller health, proactive-routine loop health and tick age, and alert conditions. |
| `GET /metrics` | Prometheus exposition for process, run, worker, provider, capacity, memory, poller, and proactive-routine loop metrics. |
| `POST /api/runtime/provider/probe` | Explicitly execute the configured provider health probe; protected by normal mutation auth and rate limits. |
| `GET /api/routines/status` | Report whether proactive polling is launch-enabled plus bounded loop tick/error state. |
| `GET /api/diagnostics` | Redacted metrics, startup recovery report, and a reverse-read recent event tail bounded to 500 lines and 1 MiB. |

Every HTTP response carries `X-Request-ID`. A valid inbound `X-Request-ID` is preserved; otherwise Kestrel generates one. Run traces use the durable `run_id` as their correlation key.

When API auth is enabled, include either `Authorization: Bearer ***` or `X-Kestrel-API-Key`. Never place the token in a URL, command history, plist, log, or support bundle.

For non-mock providers, an unobserved provider is not considered operationally ready. Set persisted `provider_startup_probe=true` when the deployment may spend one minimal provider request at each restart; otherwise readiness remains false until the first real provider call succeeds. Authentication/configuration failures and open/degraded circuits keep readiness false.

## Capability operations

Use Settings → Capabilities for the owner-facing Capability Center, or operate the same server-authoritative API directly:

- `GET /api/capabilities` lists default, configured (owner-desired), and effective state plus blockers, revision, risk, approval, source, and parent metadata.
- `PUT /api/capabilities/{kind}/{capability_id}` accepts `{"enabled": false, "expected_revision": 3}`. On HTTP 409, reload the current row and reconsider the change; do not blindly retry a stale write.
- `GET /api/capabilities/history?kind=tool&capability_id=file.write&limit=100` reads the append-only history. The server bounds the limit to 1–500.

An `On` switch is not sufficient authorization when the row remains blocked by a master config flag, launch allowlist, disabled parent/plugin, or `resource_changed`. Master flags and exact-call approvals remain non-bypassable prerequisites. New discovered skills and new dynamic MCP/skill tools start off. Every API-created MCP server is forced off; enable it afterward through the revisioned capability endpoint.

Changes apply to future invocation attempts. Turning off is checked again at dispatch, so a stale tool registry and a still-active run cannot make a later disabled invocation. The server also denies affected pending approvals; for a parent skill/MCP switch this includes its child tools. Disabling an MCP server closes its manager-owned session and denies later lifecycle/invoke entry points. Do not treat the switch as a universal process-kill primitive: an arbitrary built-in subprocess already past dispatch may continue to its normal timeout/completion path.

Turning on is asymmetric: it does not rewrite an active run's snapshotted launch configuration, enable a parent, satisfy a master flag, or pre-approve a high-risk call. Start a new run when a prior run snapshot omitted the capability. Before resuming an approved call, Kestrel revalidates its exact arguments/tool-call ID, capability revision, and combined policy/tool-spec/parent digest. A tool definition, skill manifest/runtime, MCP endpoint/command/configuration, parent policy, or enablement revision change invalidates the grant. Review `resource_changed` and use the UI's **Reauthorize** action (or submit a fresh revisioned `PUT`) instead of trying to preserve the old grant.

The history actor is `owner` in the supported single-owner profile. Hosted/team operation still requires distinct administrator identities, RBAC for switches and approvals, hardened authenticated sessions, workspace/tenant isolation, and actor-attributed audit export.

## Runtime settings operations

`GET /api/runtime/config` includes the current non-secret persisted-settings snapshot and its content revision. Send that exact revision as `expected_revision` with `PUT /api/runtime/settings`. On HTTP 409, reload and reconsider the entire change; do not replay a stale form over a newer tool-permission decision. Kestrel validates candidate workspace and memory paths before persistence, then atomically applies persistence, live activation, and approval revocation. A failed activation restores both the prior file and live configuration. The owner-only settings file and lock reject symlink, hard-link, non-regular, and foreign-owner aliases before mutation. `require_api_auth` is a launch flag, not a persisted setting.

## Alert meanings

- `provider_degraded`: inspect `/api/runtime/config` operational provider state. Authentication and invalid-request failures require operator correction; retryable failures open the circuit and can use configured fallback.
- `run_queue_saturated`: capacity is exhausted. Do not increase limits until CPU, memory, provider quota, and tool subprocess limits are understood.
- `failed_runs_present`: inspect recent run traces; this is informational unless the count/rate rises.
- `telegram_poller_unhealthy`: the poller heartbeat is corrupt, stale for more than 90 seconds, or reports an error. Inspect the bounded poller log and launchd status.
- `proactive_routine_loop_unhealthy`: polling is enabled but the loop is absent or stopped, its latest tick failed, or its active tick exceeded the stale bound. Inspect `/api/routines/status` and `/api/diagnostics`; disable proactive polling or repair the state, provider, or capacity issue before restart.
- `memory_capacity_high`: at least one layer reached 90% of `NEST_AGENT_MEMORY_MAX_LAYER_BYTES`. Back up, verify, compact working/episodic layers, then reassess capacity.

## Suggested SLOs

For a local single-node deployment:

- Liveness: 99.9% monthly while the user session is active.
- Readiness: 99.5% monthly, excluding provider maintenance.
- Run admission: 99% of accepted runs leave `queued` within 30 seconds under declared capacity.
- Recovery: zero orphaned `running` rows after a completed startup reconciliation.
- Data durability: every retained backup validates all manifest checksums; quarterly restore drill succeeds.
- Secret exposure: zero credential values in diagnostics, logs, config snapshots, or launchd plists.

## Startup and shutdown

Use the hardened launch scripts or launchd unit. They isolate inherited `PYTHONPATH`, `NODE_ENV`, `NEST_AGENT_*`, `NESTED_MEMVID_*`, `KESTREL_*`, provider, and Telegram variables, select the project virtualenv, and bound logs.

Before planned shutdown, stop accepting new work and wait until `/api/metrics` reports `active=0` and `queued=0`. Pending approvals may remain blocked until their configured expiry; routine ticks expire overdue approvals even when no UI is polling and reconcile their occurrences terminally. The FastAPI lifespan stops and joins the proactive-routine loop first, then closes RunManager admission and cancels/joins every owned primary or subagent worker within a bound. Thread start and shutdown admission share the same lifecycle lock, and queued work is never promoted after shutdown begins. Channel and MCP cleanup is always attempted afterward; if either bounded join or dependency cleanup is incomplete, shutdown reports `runtime_shutdown_incomplete` instead of silently claiming a clean stop.

Startup recovery reconstructs a missing initial task graph for an atomically admitted queued routine run before resuming it. The graph itself is persisted all-or-none, so a crash cannot leave a partially initialized run. Scheduler task claims, subagent insertion, worker heartbeats, pending-approval repair, and terminal pair transitions are fenced by the active run owner and generation. A fresh approval-execution claim defers recovery even if an observed parent-run lease looks stale. Dead/expired claims and stale result-bound continuations fail closed; the operator must inspect the external system before choosing any manual retry because the side-effect outcome may be unknown. The one-shot `routines tick` CLI performs graph recovery only for internally scoped scheduled runs whose occurrence is still linked and `running`; it does not execute unrelated queued user work.

```bash
launchctl bootout "gui/$UID/ai.kestrel.daemon"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/ai.kestrel.daemon.plist"
launchctl kickstart -k "gui/$UID/ai.kestrel.daemon"
```

## Agent backup and restore

Stop Kestrel before backup/restore. Full-agent backup and restore acquire the same
primary-runtime ownership lock as the server before inspecting live state, and
hold it through verification, replacement, rollback, and cleanup. The memory-only
CLI path does the same when given the matching `--state-path`. Both paths also
share an OS-level Memvid lock outside the memory directory, so they fail closed
if either the primary runtime or any layer is still open; a clean service stop
remains the supported operational procedure. Use the coherent agent snapshot for
normal recovery so memory, SQLite control-plane state, run capsules, settings,
skills, and plugins stay at the same point in time.

```bash
.venv/bin/nest-agent backup create \
  --backend memvid \
  --memory-dir .nest/memory \
  --state-path .nest/state/agent.db \
  --backup-dir .nest/backups/agent \
  --retain 7

.venv/bin/nest-agent backup list \
  --memory-dir .nest/memory \
  --state-path .nest/state/agent.db \
  --backup-dir .nest/backups/agent

.venv/bin/nest-agent backup verify BACKUP_ID \
  --memory-dir .nest/memory \
  --state-path .nest/state/agent.db \
  --backup-dir .nest/backups/agent
```

Restore is fail-closed, requires `--yes`, rejects traversal, absolute paths, symlinks, hardlinks, duplicate or unlisted targets, checksum mismatches, and invalid SQLite state. It creates a pre-restore safety snapshot, stages and verifies the components, then applies the complete snapshot. If a later component swap fails, Kestrel attempts to roll back every changed component. If rollback itself fails, the command reports the safety-backup ID and preserves any unreinstated rollback artifacts for operator recovery instead of deleting them. Optional components absent from the snapshot are removed so the restored agent identity is exact. A backed-up external layer configuration is installed at `.nest/config/layers.json` by default; pass `--layer-config` only to choose another target. The target may be absent on a clean host. If the snapshot instead stored `layers.json` inside the memory directory, a default clean-host restore also materializes that checksummed configuration at the external target so custom `.mv2` filenames remain usable.

If a completed create or restore returns `maintenance_warnings` with
`retention_prune_failed`, the data transaction is committed; only post-commit removal of older
snapshots failed. Verify the selected/safety backups and resolve retention separately rather than
rerunning the restore as though it had failed.

```bash
.venv/bin/nest-agent backup restore BACKUP_ID \
  --yes \
  --backend memvid \
  --memory-dir .nest/memory \
  --state-path .nest/state/agent.db \
  --backup-dir .nest/backups/agent
```

Raw Secret Broker values, operational logs, and disposable worker worktrees are deliberately excluded. Preserve secrets through an independently encrypted or keychain-backed recovery process. Full-agent backups preserve the current `.nest/repair_receipt_signing.v2.key`, schema-v2 repair validation/review artifacts, and the memory validation key. Each new isolated validation rotates the repair key and intentionally invalidates older review gates. Schema-v1 receipts and `.nest/repair_receipt_signing.key` are never accepted as commit authorization. Restoring a legacy full-agent backup that predates repair-integrity components explicitly removes any live repair signing key and receipt directories; policy evidence therefore fails closed until it is validated and approved again. `nest-agent memory backup` includes `<memory_dir>/.validation-integrity.key`, but deliberately excludes workspace repair receipts and their signing key; policy records that depend on that omitted evidence fail closed after a memory-only migration and must be validated and approved again. Memory-only backups also do not restore run history, ledgers, capsules, settings, skills, or plugins.

Backups and manifests are owner-only. A checksum-valid backup is necessary but not sufficient; perform a quarterly restore into an isolated runtime root and run state, search, inspect, skill, and plugin probes.

## Upgrade and rollback

Default to a dry run:

```bash
scripts/upgrade-kestrel.sh \
  --package-spec 'nested-memvid-agent[server,memvid,mcp]==X.Y.Z' \
  --venv .venv
```

After reviewing paths and target:

```bash
scripts/upgrade-kestrel.sh \
  --package-spec 'nested-memvid-agent[server,memvid,mcp]==X.Y.Z' \
  --venv .venv \
  --apply
```

The script stops launchd before taking snapshots, performs a SQLite backup and integrity check, creates and verifies a Memvid backup when Memvid is configured, records the installed version, installs and checks the target, runs `product setup --check` in fail-closed mode, restarts launchd, and polls the configured health URL (default `http://127.0.0.1:8765/api/health/ready`). Any failed step restores memory and SQLite from the pre-upgrade snapshots, reinstalls the exact previous Python package version, and restarts the service.

Do not delete the prior wheel or memory backup until the new version completes a soak and a restart recovery drill.

## Failure drills

### Owner process killed

Expected outcome: after supervisor restart, the run is `failed`, `stop_reason=interrupted_by_restart`, lease fields are clear, and no tool/provider side effect is replayed. Covered by `tests/test_chaos_recovery.py`.

### Provider outage

Expected outcome: failures are classified; retryable failures count toward the circuit; the circuit opens at threshold; fallback handles retryable primary failure when configured; authentication/invalid-request failures do not trigger unsafe fallback.

### Queue saturation

Expected outcome: with the in-memory backend, up to `max_concurrent_runs` execute; with Memvid, one run executes because the six `.mv2` writers are exclusive for an agent lifecycle. In both cases, up to `max_queued_runs` wait FIFO, and further admissions return HTTP 429 without creating a durable run. The capacity endpoint reports the effective active limit.

### Corrupt backup

Expected outcome: checksum validation fails and restore makes no changes.

### Memory capacity

Expected outcome: writes that would exceed the per-layer byte cap fail before the Memvid SDK write. Back up and compact; do not raise limits blindly.

## Load and soak

Run against an isolated mock-provider deployment first:

```bash
.venv/bin/python scripts/run-soak.py \
  --base-url http://127.0.0.1:8765 \
  --runs 100 \
  --concurrency 4 \
  --timeout 120 \
  --max-p95 10 \
  --min-throughput 1 \
  --response-contract mock-echo
```

The mock-only response contract requires the exact deterministic echo for each submitted probe, including its request index; it cannot pass on a canned or cross-request response. Real-provider quality runs omit that flag and use the default `exact-ok` contract. The command also requires readiness and all six named memory layers to verify before and after load, exact one-to-one accounting of probe indexes and accepted run IDs, and one `capsule.completed` trace event (with no `capsule.failed` event) for every accepted completion. It exits nonzero for any unexpected failed, blocked, cancelled, timed-out, rate-limited, unavailable, or capsule-unverified run and reports min/median/p95/max latency plus accepted-completion throughput.

A queue-saturation drill must use an explicit saturation gate, for example `--require-overload --min-completed 4 --max-overload-ratio 0.90 --max-p95 10 --min-throughput 0.5`. `--require-overload` implies overload is allowed but also fails unless at least one capacity rejection is observed. The JSON keeps load and saturation acceptance separate, so `--allow-overload` alone is only a tolerant load test and is never evidence that saturation occurred. Only a structured HTTP 429 with `detail=run_capacity_exhausted` counts as expected overload; rate-limit 429s and every 503 remain failures. Raise the isolated deployment's API rate-limit ceiling above the request count and use concurrency greater than `max_concurrent_runs + max_queued_runs`, or deliberately lower the isolated queue limit, so the drill measures admission capacity rather than rate limiting. The default maximum overload ratio is `0.90`, which prevents a nominal saturation pass where nearly every request is rejected. For real providers, reduce concurrency to quota-safe levels, choose an environment-specific throughput floor, and use a non-sensitive prompt.

Before promotion, include the capability control-plane suites in the exact-candidate validation:

```bash
python -m pytest -q tests/test_capability_policy.py tests/test_capability_control_plane.py tests/test_state_store.py
npm run test --prefix web
```

## Supported-boundary limitations

- Exactly one Kestrel server process may schedule a given state database. Durable admission is cross-process atomic, but active worker-slot scheduling is intentionally process-local for the supported single-node supervisor topology.
- Kestrel is not a multi-user or multi-tenant Internet service. Loopback without API auth is supported only for a trusted single-user host; any non-loopback bind requires API authentication and trusted-origin policy.
- Capability administration is single-owner. The shared API token is not a distinct administrator identity and does not provide role-scoped authorization.
- Recovery is fail-stop, not side-effect replay. Interrupted work is reconciled or preserved behind an unexpired live-owner lease; operators retry only after inspecting the durable trace.
- Provider probes may cost tokens or consume quota, so they are explicit persisted policy rather than a side effect of a readiness GET.

## Release gate

A release candidate is acceptable only when all of the following are green on the exact candidate bytes:

1. `make lint`
2. `make typecheck`
3. `make test`
4. `python -m compileall -q src tests scripts`
5. Golden evaluations with mock provider.
6. Frontend tests, audit, and production build.
7. Wheel/sdist build, `twine check --strict`, separate clean wheel and sdist installs against the hash-locked release dependency set, and packaged React/license asset smoke in both environments.
8. Linux/macOS Python 3.11 through 3.13, native Windows Python 3.11, and a successful exact-SHA `main` CI push run before release publication.
9. Chaos recovery test and bounded soak.
10. Memvid integration tests under `RUN_MEMVID_INTEGRATION=1` in a credential-safe environment.
11. Independent security/spec/code review of the exact diff.
12. Deliberate tag/release action; never publish from an unreviewed dirty tree.
