# Kestrel Production Operations Runbook

This runbook defines the supported production profile for Kestrel: a single-user, single-node, local or privately networked agent runtime. It is not a multi-tenant Internet service.

## Operational invariants

- Memvid v2 `.mv2` only; one file per nested memory layer.
- Never call Memvid `create(path)` when that `.mv2` already exists.
- Runs are admitted through bounded worker capacity, hold fenced leases while executing, and heartbeat until terminal or blocked.
- Startup never replays interrupted side effects. A fresh lease whose PID-bearing local owner is still alive is preserved; an expired lease or a lease owned by a dead local process is reconciled to `failed` with `interrupted_by_restart`. Pending approvals remain `blocked`.
- Subagent workers record their local manager owner; startup preserves live-owner workers and terminally reconciles dead-owner workers and their task nodes.
- Terminal run states are immutable and cancellation emits one durable cancellation event.
- Runtime settings are versioned, checksummed, source-attributed, and snapshotted into every run.
- Schema v15 capability overrides are revisioned, and every mutation appends a capability change row.
- High-risk tools require an effective capability decision and every applicable master flag, and still require owner-bound, single-use exact-call approval. Pending approvals expire after 15 minutes by default (`NEST_AGENT_APPROVAL_TTL_SECONDS`).
- The API must use authentication when bound beyond loopback.

## Service health

| Endpoint | Meaning |
|---|---|
| `GET /api/health/live` | Process is serving HTTP. Never use for traffic admission. |
| `GET /api/health/ready` | SQLite is writable and on the supported schema, no orphaned running run exists, capacity and Memvid are healthy, required pollers are healthy, and the active provider has been operationally verified. |
| `GET /api/metrics` | Run counts/capacity, provider state, memory utilization, Telegram poller health, and alert conditions. |
| `GET /metrics` | Prometheus exposition for process, run, worker, provider, capacity, memory, and poller metrics. |
| `POST /api/runtime/provider/probe` | Explicitly execute the configured provider health probe; protected by normal mutation auth and rate limits. |
| `GET /api/diagnostics` | Redacted metrics, startup recovery report, and bounded recent event log. |

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

## Alert meanings

- `provider_degraded`: inspect `/api/runtime/config` operational provider state. Authentication and invalid-request failures require operator correction; retryable failures open the circuit and can use configured fallback.
- `run_queue_saturated`: capacity is exhausted. Do not increase limits until CPU, memory, provider quota, and tool subprocess limits are understood.
- `failed_runs_present`: inspect recent run traces; this is informational unless the count/rate rises.
- `telegram_poller_unhealthy`: the poller heartbeat is corrupt, stale for more than 90 seconds, or reports an error. Inspect the bounded poller log and launchd status.
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

Before planned shutdown, stop accepting new work and wait until `/api/metrics` reports `active=0` and `queued=0`. Pending approvals may remain blocked.

```bash
launchctl bootout "gui/$UID/ai.kestrel.daemon"
launchctl bootstrap "gui/$UID" "$HOME/Library/LaunchAgents/ai.kestrel.daemon.plist"
launchctl kickstart -k "gui/$UID/ai.kestrel.daemon"
```

## Memory backup and restore

Stop Kestrel before backup/restore. The CLI and Memvid backend also share an OS-level lock outside the memory directory, so a backup or restore fails closed while any layer is open; the service stop remains the supported operational procedure.

```bash
.venv/bin/nest-agent memory backup \
  --backend memvid \
  --memory-dir .nest/memory \
  --state-path .nest/state/agent.db \
  --backup-dir .nest/backups/memory \
  --retain 7

.venv/bin/nest-agent memory backup-list \
  --memory-dir .nest/memory \
  --backup-dir .nest/backups/memory

.venv/bin/nest-agent memory backup-verify BACKUP_ID \
  --memory-dir .nest/memory \
  --backup-dir .nest/backups/memory
```

Restore is fail-closed, requires `--yes`, rejects traversal, absolute paths, symlinks, hardlinks, duplicate targets, and checksum mismatches, creates a pre-restore safety backup, stages a complete owner-only memory directory, swaps the closed directory, then reopens and deeply verifies all six layers. A failed in-process swap restores the prior directory.

```bash
.venv/bin/nest-agent memory restore BACKUP_ID \
  --yes \
  --backend memvid \
  --memory-dir .nest/memory \
  --state-path .nest/state/agent.db \
  --backup-dir .nest/backups/memory
```

Backups and manifests are owner-only. A checksum-valid backup is necessary but not sufficient; perform a quarterly restore into an isolated directory and run search/inspect probes.

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

Expected outcome: up to `max_concurrent_runs` execute, up to `max_queued_runs` wait FIFO, and further admissions return HTTP 429 without creating a durable run.

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
  --timeout 120
```

The command exits nonzero for any unexpected failed, blocked, cancelled, or timed-out run and reports min/median/p95/max latency. A saturation drill may add `--allow-overload --min-completed 1 --max-p95 10`; HTTP 429/503 admissions then count as expected overload rather than failures, while the command still requires the declared number of completions and zero unexpected failures. For real providers, reduce concurrency to quota-safe levels and use a non-sensitive prompt.

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
7. Wheel/sdist build, `twine check` equivalent metadata validation, isolated wheel install, packaged React asset smoke.
8. Linux/macOS/Windows and supported Python CI matrix.
9. Chaos recovery test and bounded soak.
10. Memvid integration tests under `RUN_MEMVID_INTEGRATION=1` in a credential-safe environment.
11. Independent security/spec/code review of the exact diff.
12. Deliberate tag/release action; never publish from an unreviewed dirty tree.
