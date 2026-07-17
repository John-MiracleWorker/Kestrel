# Kestrel production-readiness review

Date: 2026-07-16
Branch: `fix/runtime-rescue`
Candidate package: `0.3.0.dev0`
Latest published release: `v0.2.1`

## Verdict

**Supported local/private deployment: PASS.** The current working tree is a production-grade release candidate for one trusted user, one Kestrel server/worker process, and one local or privately networked node. The conversational CLI, authenticated API, React workbench, Memvid v2 memory, deterministic mock path, approvals, MCP boundary, Telegram controls, recovery, backups, observability, bounded admission, packaging, and operator workflows have all been exercised.

**Publication and deployment: HOLD.** The candidate is intentionally uncommitted, untagged, and unpublished. Exact candidate bytes have not run through the repository's Linux/macOS/Windows GitHub matrix, a local Docker daemon was unavailable, and the credentialed live-provider matrix is an external release gate. `v0.2.1` remains the public recommendation.

**Hosted/team product: NOT READY and not claimed.** Kestrel does not yet provide multi-user identity, tenant/workspace isolation, roles, distributed scheduling, or container-grade extension isolation. `nest-agent product readiness --json` deliberately reports against `full_product_including_hosted_team` and remains false. That broader roadmap does not invalidate the narrower supported local/private profile above.

## Final hardening completed

- Added durable run leases, heartbeats, startup reconciliation, cancellation propagation, bounded admission/queueing, task/subagent claim fencing, and immutable effective run configuration.
- Added an explicit per-run publication fence. Terminal or blocked state is not exposed before its timeline event, capsule, resource cleanup, and owner-cycle finalization are published; queued cancellation releases the fence without requiring a worker.
- Added provider circuit isolation, retry/fallback controls, sanitized provider provenance, and operational health/readiness state.
- Centralized credential redaction across streaming, provider errors, final results, events, approvals, MCP discovery, subprocess environments, support bundles, and persistence.
- Made provider-supplied secret-bearing tool calls fail closed before progress reporting, behavior preflight, approval resolution, or registry execution. Sanitized placeholders are never executed.
- Made exact-call approvals atomic, owner-bound, expiring, single-use, and secret-free at rest. Raw exact arguments exist only in volatile process memory and restart fails closed.
- Hardened MCP command/connect approval, mutation-risk inference, discovered metadata, environment handling, and persisted error surfaces.
- Hardened API authentication, trusted hosts/origins, CORS preflight behavior, request-size/rate bounds, Telegram webhook verification, owner allowlists, and callback-query handling.
- Hardened file/shell/process tools against path escape, sensitive-file reads, symlink races, credential inheritance, orphan descendants, unsafe mutation, and implicit high-risk enablement.
- Added Memvid v2 locking, capacity checks, integrity verification, checksummed backup/restore, private staging, rollback, and six-layer recovery coverage.
- Added production operations, launchd supervision, upgrade/rollback, Docker/Compose, CI/release gates, SBOM/checksum workflow, setup readiness, and bounded soak tooling.
- Added a schema-v15 capability control plane for every tool, MCP server, and skill, with revisioned owner decisions, compare-and-swap mutation, append-only history, configured-versus-effective state, parent/master-flag blockers, and resource-digest invalidation.
- Added live dispatch enforcement for stale registries, default-off dynamic tools/skills/API-created MCP servers, pending-approval revocation, managed MCP-session closure, and approval binding to capability revision plus policy/spec/parent digest.
- Added the Settings Capability Center with search, kind/state filters, per-row status and blockers, high-risk confirmation, conflict refresh, inline MCP/skill switches, and explicit reauthorization after a reviewed resource changes.

## Independent adversarial review

The final read-only review returned **APPROVE, high confidence, with no remaining P0/P1 findings**. It independently rechecked:

1. split-token, provider-error, and final-result secret redaction;
2. durable approval secrecy, exact-call safety, restart behavior, and placeholder rejection;
3. custom credential-environment stripping in child processes;
4. MCP mutation-risk inference despite trusted manifest metadata;
5. MCP discovery/schema/error sanitization before ToolSpec construction or persistence; and
6. slow-finalizer and queued-cancellation publication races.

Reviewer verification included 17 focused security tests, two publication-fence regressions, five approval/concurrency tests, Ruff, Mypy, and direct adversarial subprocess/tool probes.

A follow-on capability-control review added 11 adversarial end-to-end tests covering catalog/PUT/CAS behavior, aliases and stale registries, default-off high-risk tools, restart persistence, plugin resync, MCP lifecycle closure and entry-point denial, approval revocation, policy/resource-bound approval invalidation, legacy skill endpoint auditing, and MCP configuration-route bypass resistance. All 11 passed. Rendered browser validation exercised tool, MCP-server, and skill switches against an isolated local runtime on desktop and at a 390 px responsive viewport; no horizontal overflow or browser console warnings/errors were observed.

## Verification matrix

| Area | Result |
|---|---|
| Python tests | **PASS** — 798 collected; 766 passed, 32 live/integration-gated skips; one third-party Starlette deprecation warning |
| Release lint | **PASS** — Ruff over `scripts`, `src`, and `tests` |
| Types / compile | **PASS** — Mypy over 107 source files; compileall over source, tests, and scripts |
| Security/static | **PASS** — Bandit high-severity/high-confidence gate, ShellCheck, Bash syntax, and `git diff --check` |
| Golden evaluations | **PASS** — 21/21; zero false promotions; maximum latency 220.72 ms |
| Memvid v2 integration | **PASS** — 5/5 with `RUN_MEMVID_INTEGRATION=1` |
| MCP stdio integration | **PASS** — 1/1 with `RUN_MCP_INTEGRATION=1` |
| Frontend | **PASS** — 43/43 tests, production Vite build, rendered desktop/mobile control-plane validation, npm audit with zero vulnerabilities |
| Setup readiness | **PASS** — authenticated Memvid/mock/worker-isolated profile: 9 pass, 0 warn, 0 fail |
| Authenticated Memvid soak | **PASS** — 25/25, concurrency 4, zero overload/failure, p95 7.093 s, max 7.386 s |
| Installed-wheel soak | **PASS** — 30/30, concurrency 8, zero overload/failure, p95 1.291 s, max 1.322 s |
| Package metadata | **PASS** — wheel and sdist built with release-workflow web staging; both passed Twine checks |
| Package contents | **PASS** — compiled React assets present in wheel/sdist; no `.env*`, `.git`, `.nest`, or `tiuni-fun` content |
| Isolated package smoke | **PASS** — fresh wheel install, doctor, mock chat, bundled-UI server, capability catalog, and API run lifecycle |
| Dependency integrity | **PASS** — lock resolves 75 packages, `pip check` clean, npm audit clean, and the Python environment audit found no known vulnerabilities after raising the setuptools build floor and refreshing build tools |
| Docker configuration | **PASS** — Compose renders with placeholder configuration; image/runtime verification remains external because the local daemon was unavailable |

The candidate artifact checksums from this review were:

```text
cefbe13173dc393cf5f336a65f0e99aa771fd176f249506b07fafb5364006666  nested_memvid_agent-0.3.0.dev0-py3-none-any.whl
c66af0d8fd50fe52be5b8aa35ea4b817cdedb8fc07942333977bcd8ad984d263  nested_memvid_agent-0.3.0.dev0.tar.gz
```

These are review artifacts, not published releases. A direct unstaged `uv build` is not the release procedure because it omits the separately built workbench; the release workflow's **Stage web workbench in Python package** step is mandatory.

## Required release sequence

1. Curate and commit only the intended Kestrel changes. Explicitly exclude `tiuni-fun/`, local env files, runtime state, credentials, caches, and temporary review artifacts.
2. Obtain exact-diff review and run the committed bytes through required Linux/macOS/Windows Python, frontend, ShellCheck, package, Memvid, chaos, and Docker CI gates.
3. Run credential-safe live-provider certification for every provider claimed by the release. Mock validation proves determinism and product wiring, not third-party availability or quota behavior.
4. Rotate any historical Telegram or obsolete provider credentials before relying on them for the release; never move secret values into chat, commits, artifacts, or CI logs.
5. Restore comfortable disk headroom above the current approximately 6 GiB free before Docker builds, large provider caches, or release rollback drills.
6. Promote `0.3.0.dev0` to a deliberate final version, verify tag/version equality, generate SBOM and checksums, sign/tag, publish, then perform clean-install, upgrade, rollback, and post-publish smoke checks.

No commit, push, pull request, merge, tag, deployment, credential rotation, or release was performed by this review. The unrelated `tiuni-fun/` tree was neither edited nor packaged.
