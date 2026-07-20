# Security Notes

Kestrel's supported security profile is a trusted single-user host running one local or privately networked node. Loopback-only use may omit API authentication; any non-loopback exposure requires API authentication and trusted host/origin policy. Kestrel is not a multi-user or multi-tenant Internet service.

## Default Posture

The defaults keep high-risk behavior off:

```text
NEST_AGENT_ALLOW_SHELL=false
NEST_AGENT_ALLOW_FILE_WRITE=false
NEST_AGENT_ALLOW_POLICY_WRITES=false
NEST_AGENT_ALLOW_CODEX_CLI=false
NEST_AGENT_ALLOW_PLUGIN_INSTALL=false
NEST_AGENT_ALLOW_GIT_COMMIT=false
NEST_AGENT_ALLOW_GIT_PUSH=false
NEST_AGENT_ALLOW_REMOTE_MUTATION=false
NEST_AGENT_GIT_WRITE_MODE=local_branch
NEST_AGENT_PROTECTED_BRANCHES=main,master,release/*
NEST_AGENT_SECRET_STORE_PATH=.nest/secrets/local_vault.json
NEST_AGENT_ALLOW_MEMORY_IMPORT=false
NEST_AGENT_ALLOW_EXECUTABLE_SKILLS=false
NEST_AGENT_ALLOW_MCP_NETWORK_ENDPOINTS=false
NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER=false
NEST_AGENT_MAX_SCHEDULER_TASKS=3
NEST_AGENT_MAX_SCHEDULER_CYCLES=5
NEST_AGENT_ENABLE_PROACTIVE_ROUTINES=false
NEST_AGENT_MAX_ROUTINES_PER_TICK=3
NEST_AGENT_ENABLE_CHANNEL_DELIVERY=false
NEST_AGENT_ENABLE_AUTO_CONSOLIDATION=false
NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN=true
NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=false
NEST_AGENT_MAX_ACTIVE_DELTAS_PER_RUN=8
NEST_AGENT_REQUIRE_API_AUTH=false
NEST_AGENT_TRUSTED_HOSTS=127.0.0.1,localhost,::1,[::1]
NEST_AGENT_CORS_ORIGINS=
```

Do not loosen these in shared deployments without a separate approval and audit story. The local web server also rejects untrusted `Host` headers and cross-site `Origin` hosts by default; set `NEST_AGENT_TRUSTED_HOSTS` and `NEST_AGENT_CORS_ORIGINS` deliberately when running a split frontend or a non-loopback deployment.

## Network Binding

Bind the web/API server to `127.0.0.1` by default:

```bash
nest-agent server --host 127.0.0.1 --port 8765
```

If binding to `0.0.0.0`, enable `NEST_AGENT_REQUIRE_API_AUTH=1`, set `NEST_AGENT_API_TOKEN`, put Kestrel behind an authenticated reverse proxy, and keep dangerous tool flags disabled. Startup fails with `unsafe_bind` when a non-loopback host is requested without API auth and a configured token.

## API Auth

The local API can require a shared token:

```bash
export NEST_AGENT_REQUIRE_API_AUTH=1
export NEST_AGENT_API_TOKEN='replace-with-local-secret'
nest-agent server --host 127.0.0.1 --port 8765
```

Clients may send either `Authorization: Bearer <token>` or `X-Kestrel-API-Key: <token>`. When enabled, the gate covers `/api/*` and `/metrics`. The only public control-plane exception is the exact `POST /api/channels/telegram/webhook` route, which instead fails closed on Telegram's secret-token signature and the configured chat/user allowlists before creating a run or applying an approval. This is a local operator gate, not multi-user authorization.

## Secrets

Use environment variables or the Secret Broker for provider/channel/tool credentials. Do not store secrets in `.mv2` files, `.nest/config`, checked-in fixtures, prompts, chat history, tool arguments visible to the model, traces, or MCP server manifests.

The Secret Broker lets a trusted backend/UI flow collect values through `POST /api/secrets` and returns metadata only: name, purpose, `secret://...` reference, configured state, validation status, timestamps, and a salted non-reversible fingerprint. No GET route returns raw values. The default local JSON vault uses cross-process locking and atomic replacement from an owner-only temporary file so a partially written or permissively exposed vault is never the live file. On startup and before reads, an existing vault is validated through a no-follow descriptor and repaired to mode `0600`; symlinked, hard-linked, non-regular, or foreign-owned vault and lock files fail closed before target mutation. Native Windows exposes NTFS ACLs rather than meaningful POSIX mode bits, so deployments requiring strict account separation should select a usable keyring/DPAPI-backed implementation instead of relying on the JSON vault. The JSON vault assumes a non-shared, single-user machine. Set `NEST_AGENT_SECRET_BACKEND=keyring` or `--secret-backend keyring` to store raw values in the OS keyring while keeping only metadata in the JSON vault. Explicit `keyring` selection fails closed when the package or a usable OS keyring backend is unavailable; it never silently downgrades to plaintext JSON. Shared environments should back the same broker contract with an OS keychain or managed vault such as macOS Keychain, Linux Secret Service via `keyring`, Windows DPAPI, or a managed secret store.

Persisted runtime settings are also owner-private state. On POSIX, `runtime_settings.json` and its lock are validated as regular, single-link, current-owner files and repaired to mode `0600`; aliases fail closed before target mutation. Every API write must present the revision returned by the latest settings read. Merge, path/config preflight, atomic persistence, live activation, and capability-approval revocation are serialized as one settings transaction. If activation fails, Kestrel restores the previous file and live configuration; stale writers receive HTTP 409 instead of overwriting a newer safety decision. `require_api_auth` remains launch-controlled and cannot be weakened through persisted settings.

The SQLite control-plane store contains user messages, routine prompts, approvals, and run metadata. On POSIX, an immediate state directory created by Kestrel uses owner-only mode `0700`; an existing custom parent, such as a workspace containing `agent.db`, keeps its current mode. The database, WAL, SHM, and rollback-journal files are always created or repaired as mode `0600`. Use a dedicated state directory when directory-level isolation is required. The state directory and database may not be symlinks, and state files may not be hard-linked, so permission repair cannot be redirected outside the configured state parent. Native Windows keeps its existing ACL behavior because POSIX mode bits are not authoritative there.

The raw JSONL event history can contain redacted but still private turn and routine diagnostics. On POSIX, an immediate log directory created by Kestrel uses mode `0700`, while an existing custom log directory keeps its current mode; `events.jsonl` is created or repaired as `0600`. Descriptor-relative no-follow opens reject symlinked, hard-linked, non-regular, or foreign-owned event files before chmod, read, or append, and advisory locks keep concurrent appends as complete JSON lines. Diagnostic and support tails read backward under a shared lock, cap work at 500 lines and 1 MiB, and discard an incomplete leading line rather than loading an unbounded append-only file. Native Windows retains its existing ACL behavior.

Nested memory and task capsules contain prompts, recalled context, tool evidence, and assistant responses. Server bootstrap repairs the finite configured memory artifact set before the first request, without opening or creating `.mv2` containers; memory-only library/CLI construction does not infer or touch a sibling runs directory. On POSIX, memory-layer and vector-index leaf directories created by Kestrel use mode `0700`; an existing custom memory directory keeps its configured mode after owner/type/no-symlink validation. The explicit full-runtime Kestrel `runs` root and each accessed or newly created run-capsule directory are repaired to `0700` in constant scope. Protecting the root immediately prevents other local accounts from traversing any historical descendants without scanning an unbounded run history; an accessed legacy capsule is then repaired lazily. Capsule run IDs must be one portable path component, so absolute paths, traversal, and nested paths are rejected before any filesystem access or permission repair. Configured layer and vector artifact names must be unique single filenames inside `memory_dir`; absolute, nested, traversal, case-insensitive duplicates, and conflicts with derived index or lock files fail before permission repair. In-memory snapshots (`*.memory.json`), Memvid v2 containers (`*.mv2`), exact-record indexes (`*.mv2.records.json`), capsule marker/metadata files, and rebuildable SQLite vector indexes plus WAL/SHM/journal sidecars are created or repaired as `0600`. Each backend hardens all known layer variants, including stale files left by a backend switch. Existing sensitive files are rejected before chmod, read, connect, or write when they are symlinks, hard-linked, non-regular, or owned by another account, so permission repair cannot mutate an alias target. The deterministic mock backend synchronizes same-path search state and uses a private OS lock plus merge-before-replace snapshot seals to avoid concurrent last-writer loss. Memvid containers are never precreated: the SDK receives a missing path inside the private leaf, and Kestrel requires that the successful create call materialize a regular container before startup can continue, then immediately verifies and tightens it after subsequent writes. Native Windows retains its existing ACL behavior because POSIX modes are not authoritative there.

Support bundles never copy raw event lines. Their bounded event tail preserves only allowlisted operational string fields plus numeric/boolean diagnostics; every other nested string is replaced with `<redacted>`. This includes user and routine prompts, assistant content, proof-of-work objectives, commands, errors, diagnoses, and retry strategies. Raw environment values, Secret Broker vault data, and Memvid files remain excluded.

Secret status checks only report environment-variable configured state for names registered by channel or MCP configuration. They do not probe arbitrary env var names.

MCP manifests must reference secret material through `secret_env`, where each target process variable maps to a host environment variable or `secret://...` reference. Raw secret-looking keys in MCP `env` are rejected, API responses redact `secret_env`, and values are resolved into the child process environment only at launch. Every live stdio server, including manually configured servers, requires explicit connect approval bound to the exact command/argument hash. Shell/proxy launchers and interpreter eval modes are rejected. Dynamically discovered tools remain at least medium risk and approval-required even under `trust_manifest`; only reviewed static manifest metadata can declare an autonomous low-risk tool.

Logs, LLM/tool context, and memory writes share a redaction boundary for common API key, bearer token, password, authorization header, credentialed URL, provider token, private-key, and certificate shapes. `file.read`, content-search, and content-hashing refuse sensitive paths such as `.env*`, key/certificate files, `.npmrc`, `.pypirc`, netrc, `.git/config`, and nested secret/credential directories, including aliases reached through symlinks or traversal. Agent file writes remain approval-gated and cannot modify the broker vault directly. Test, lint, and repair-validation subprocesses inherit a credential-stripped environment. Redaction is defense-in-depth, not permission to log secrets deliberately.

## Tool Risk

High-risk tools require an enabled per-capability decision, explicit config enablement where applicable, and exact-call approval. Neither the UI nor the capability API can bypass a master flag, launch allowlist, parent capability, resource-integrity blocker, or exact-call approval requirement. Approval cannot be disabled by configuration; it is tied to the owner principal, requested tool-call ID, and exact arguments, is single-use, and expires after 15 minutes by default. It is also bound to the capability revision and a digest covering the current tool specification, applicable policy gates, and parent MCP/skill resource. Set `NEST_AGENT_APPROVAL_TTL_SECONDS` to a positive number to change the decision window. Changed or expired arguments, a changed policy/specification/parent digest, or a changed capability revision require a new approval. Shell execution, file writes, patching, repair mutations, git commits, Codex CLI execution, channel delivery, executable skills, memory imports, plugin review/install/update/enable, behavior-delta activate/reject/rollback actions, and policy memory writes are not production-safe defaults.

Approval is authorization, not evidence that a side effect completed. Before dispatch, schema v18 requires an exclusive durable execution claim tied to the active run lease and, when scheduler-owned, the exact running task/subagent continuation. Claims heartbeat during execution and only the matching owner and claim ID can store the result. If startup proves the claimant dead or the claim expired, Kestrel records `approval_execution_outcome_unknown`, fails the bound scheduler pair closed, and suppresses automatic replay. A persisted result with a missing, corrupt, cancelled, or advanced continuation is likewise non-resumable. This provides at-most-once automatic dispatch across recovery; external systems still need idempotency keys when stronger end-to-end guarantees are required.

## Capability Control

The current schema is v19. The capability contract introduced in schema v15 stores durable owner decisions for `tool`, `mcp_server`, and `skill` capabilities separately from discovery metadata, plus an append-only change log. The Settings Capability Center and `GET /api/capabilities` show both configured state (the owner-desired switch) and effective state. `blocked_by` explains unmet prerequisites such as a capability being off, a disabled runtime master flag, a launch allowlist, a disabled/missing parent or plugin, or `resource_changed`.

Mutations use `PUT /api/capabilities/{kind}/{capability_id}` with `enabled` and `expected_revision`. The compare-and-swap revision prevents lost updates; stale writes return HTTP 409 with the current state. `GET /api/capabilities/history` exposes the bounded audit history. These remain single-owner controls under the supported local/private profile: the recorded actor is the owner, not a hosted administrator identity.

Default-off discovery is deliberate. A newly discovered skill is disabled, a dynamic tool supplied by an MCP server or skill has its own disabled default, and every newly created API MCP server is forced disabled until the revisioned capability endpoint records an owner decision. Enabling a dynamic child never enables its parent. High/critical built-ins without a dedicated master flag also default off.

Capability changes apply to future invocation attempts. A live execution gate denies a disabled tool even when the caller holds a registry built before the change. Disabling a tool, skill, or MCP server denies affected pending approvals before they can resume; disabling an MCP server also closes its manager-owned session and blocks connect, test, health, restart, sync-to-live, approval, and invoke paths. This is an invocation boundary, not a general process supervisor: Kestrel does not claim that toggling off always kills an arbitrary built-in subprocess that was already dispatched.

Every enable decision stores a digest of the reviewed tool specification or parent resource. A later tool-schema, skill-manifest/runtime, MCP endpoint/command/configuration, or parent-policy change fails closed as `resource_changed` until the owner records a fresh reviewed decision. Approved continuations are independently revalidated against the current capability revision and combined policy/spec/parent digest immediately before side effects. Deletion removes the override after writing a revocation history row so a recreated resource ID does not inherit the prior authorization.

This control plane does not expand the supported deployment tier. Kestrel remains a local/private, single-owner runtime. Hosted/team use still needs distinct administrator and user identities, role-based authorization for capability changes and approvals, tenant/workspace isolation, hardened authenticated sessions, and actor-attributed audit export.

Self-improvement is local-first. Kestrel may write validated lessons to local `.mv2` memory, prepare local branches/worktrees, create patches, and run validation. `git.create_local_branch` and `git.export_patch` are approval-gated local-only primitives. Remote publishing is a separate lane: direct commits to protected branches, direct pushes to upstream `main`, force pushes, tag pushes, remote rewrites, repo setting edits, GitHub secrets, and workflow enablement are disabled by default. The default tool registry does not include `git.push`; `shell.run` is limited to minimal introspection commands and structurally blocks remote-publishing argv shapes such as `git push`, `git tag`, `git remote set-url`, `gh repo edit`, `gh secret set`, and `gh workflow enable`.

Skill installation is a high-risk file-write action. Uploaded skill capsules are confined to the configured skills directory, validated by manifest shape, and still require approval before installation. Host `python` and `shell` skill runtimes fail closed. Executable skills are always forced to high risk, require exact approval plus `NEST_AGENT_ALLOW_EXECUTABLE_SKILLS=true` / `--allow-executable-skills`, and must declare a digest-pinned OCI image with canonical default-deny scopes. Immediately before launch Kestrel copies the bounded skill tree into an owner-private system-temporary snapshot and verifies its digest. Explicit workspace read grants are independently copied through descriptor-relative, no-follow traversal with hardlink, special-file, mount-crossing, depth, entry, per-file, and total-byte rejection; Docker binds only those private snapshots read-only. Workspace-root, `.git`, `.nest`, writable, network, and secret scopes fail closed. Docker execution has no host fallback, pins the verified local Unix/named-pipe endpoint for launch and cleanup, and enforces a read-only root, nonroot identity, dropped capabilities, no-new-privileges, PID/CPU/memory/ulimit/tmpfs limits, bounded UTF-8 input and output, and supervised timeout cleanup. A terminal result is not returned until I/O workers have stopped and repeated exact-name probes prove the container absent.

Executable-skill writeback is intentionally unsupported until Kestrel has a quota-bounded staging area plus a reviewed, no-follow host-side commit protocol. The supported single-user threat model also trusts the owning host account and its local container socket; a same-user process that can replace or proxy that socket is outside this containment boundary.

Plugin installation is high risk: it fetches public GitHub repositories and materializes skills/MCP entries. CLI/API review, install, update, enable, and sync/materialization routes are disabled unless `NEST_AGENT_ALLOW_PLUGIN_INSTALL=true` or `--allow-plugin-install` is set. Agent-initiated `plugin.review` and `plugin.install` have the same enablement gate and still require exact-call approval before execution. Review fetches and normalizes manifests without installing or executing plugin code, and reports declared dependencies, isolation requirements, warnings, unsupported features, and enable blockers. Installed plugins are not enabled by default unless explicitly requested; plugins that declare unmanaged dependencies or required unavailable isolation can be installed disabled but cannot be enabled until those blockers are resolved. Plugin updates reject manifest ID drift. Plugin-provided MCP stdio servers are restricted to conservative launchers (`npx`, `uvx`, `python -m`, `node`, `bunx`, `deno`) with validated args, and Kestrel stores a command/args hash so later tampering is refused before connect. Plugin-provided MCP servers also require explicit connect approval through `POST /api/mcp/servers/{server_id}/approve-connect` before the first connect/test/sync/invoke path may start the process.

Autonomous scheduling is disabled by default. When enabled, it is bounded by per-cycle task and cycle limits, and it stops at task approval or exact-call tool approval boundaries instead of silently crossing into high-risk work.

Proactive routine polling is independently disabled by default. New routine definitions are always disabled. Polling and claim leases are limited to 1-3,600 seconds, each tick to 1-100 claims, fixed intervals to 60-31,536,000 seconds (one year), and misfire grace to 0-604,800 seconds (seven days). Create/update/enable/disable/delete API calls fail closed unless shared-token API authentication is configured, and routine mutation bodies use strict boolean and integer types rather than coercing strings or booleans. Definition fields reject registered raw secret values; use `secret://...` references only where a later tool explicitly resolves them. Owner mutations use revision compare-and-swap, deletion tombstones the ID, and each due occurrence is bound to its routine revision and UTC instant.

A routine occurrence uses a claim owner, lease generation, and expiry. The same SQLite transaction revalidates that claim and the live routine revision before inserting its deterministic internally scoped run. Disabling, revising, or deleting before admission fences the stale worker. Once a run is admitted, it follows normal run cancellation and approval semantics; a routine switch is not a universal process-kill primitive. An approval-blocked run counts as active for overlap suppression, and routine enablement never grants tool approval.

Kestrel guarantees duplicate-fenced occurrence/run admission, not exactly-once arbitrary side effects. External tools and future channel delivery need their own idempotency keys and recovery contracts. Automatic connector delivery is not part of the current routine slice.

Manual routine launch is subject to the same owner API-auth and proactive-routine master gates. The request includes the current definition revision and a client UUID; Kestrel stores only its hash, claims/replays/reclaims it transactionally, preserves the schedule, and rejects overlap. This makes an ambiguous HTTP retry idempotent at run admission, not at arbitrary downstream connector side effects.

Repair artifacts live beneath a no-follow, owner-only `.nest` boundary and are create-once HMAC receipts signed by the durable workspace key at `.nest/repair_receipt_signing.key`. The key is created atomically as a single-link regular file, repaired to `0600`, rejected when symlinked, hard-linked, or owned by another account, and reused after restart. Full-agent backups include the signing key plus signed validation/review receipts; memory-only backups deliberately do not, so restoring policy memory without the corresponding workspace evidence fails closed at policy recall until the operator validates and approves it again. Validation output and evidence are redacted before persistence, and a passing receipt requires identical pre/post branch, HEAD, and content digest. Repair branch commits derive literal blobs and a temporary index from that signed manifest, so repository filters, hooks, signing config, and caller-index races cannot alter the approved tree. `repair.rollback` requires an approval-bound current diff digest, writes a plan journal first, and quarantines overwritten/untracked files before raw HEAD restoration. `git.commit` never pushes and refuses protected branches from `NEST_AGENT_PROTECTED_BRANCHES`.

Stable-learning runtime receipts use a separate owner-only key at `<memory_dir>/.validation-integrity.key`. That key is also create-once, `0600`, single-link, and included in both memory-only and full-agent backups so restored `.mv2` validation envelopes retain their identity. Receipts are bound to the exact candidate record ID, the canonical title/content/kind digest, and the originating run (or session when no run exists); replay against another claim or run is rejected.

## Webhooks

Public channel webhook endpoints reject unsigned payloads by default. Set channel `settings.unsigned_allowed=true` only for deliberately private or already-authenticated generic ingress. It never disables signature verification for the public Telegram webhook route. The Telegram launch scripts enable API auth by default, require a non-empty control-plane token, and trust only the exact hostname from `PUBLIC_URL`; they do not expose the rest of `/api` as an unauthenticated tunnel surface.

Generic/custom channel endpoints can require HMAC-SHA256 signatures by setting channel `settings.signature_secret_env`. The signature is computed over the raw HTTP body bytes and sent in `X-Kestrel-Signature` as either a hex digest or `sha256=<digest>`. GitHub uses `X-Hub-Signature-256`; Stripe uses `Stripe-Signature` with timestamp tolerance; Discord uses Ed25519 and requires `settings.discord_public_key` plus optional PyNaCl support, not an HMAC secret.

Outbound channel delivery accepts HTTPS public endpoints only. Kestrel rejects local, private,
link-local, and metadata addresses; rejects redirects; and pins the validated DNS address set through
connection establishment so DNS rebinding cannot redirect a vetted webhook into the local network.
Delivery remains disabled until the owner configures the channel explicitly.

Telegram admin mode is single-owner only until production auth/workspaces land. Configure `settings.admin_enabled=true` and exactly one explicit `settings.owner_user_ids` entry; ordinary chat allowlists never confer ownership. Missing, disabled, or multi-owner admin configuration fails closed, and non-owner messages and callback actions are denied before creating or resuming runs. Natural-language admin writes create inline confirmation previews bound to the initiating owner and expiring after five minutes. Raw secrets are refused in Telegram because Telegram chat history is not a local secret-entry surface.

An explicit unknown `channel_id` is rejected instead of being treated as an ephemeral local channel. This keeps signed webhook configuration from being bypassed by choosing a new ID.

## Memory and Behavior-Delta Safety

Memory promotion must carry evidence, provenance, confidence, and validation status. A single ordinary event must not become policy, procedural, or semantic memory. Behavior deltas are default-off runtime instructions: proposed/staged deltas do not run, active deltas require evidence and rollback metadata, MutationGate adjudicates activation, exact-call approval protects review actions, and tool-aware preflight must not bypass capability flags or exact-call approval gates.

## Incident Response

If a secret appears in a prompt, tool argument, or log:

1. Rotate the provider credential.
2. Stop the server.
3. Preserve `.nest/logs` for local audit.
4. Verify the redacted log surface with `GET /api/logs` or `nest-agent doctor`.
5. Remove any contaminated local working files outside `.mv2` memory.
