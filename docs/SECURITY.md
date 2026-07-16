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

The Secret Broker lets a trusted backend/UI flow collect values through `POST /api/secrets` and returns metadata only: name, purpose, `secret://...` reference, configured state, validation status, timestamps, and a salted non-reversible fingerprint. No GET route returns raw values. The default local JSON vault uses cross-process locking and atomic replacement from an owner-only temporary file so a partially written or permissively exposed vault is never the live file. POSIX deployments enforce mode `0600`; native Windows exposes NTFS ACLs rather than meaningful POSIX mode bits, so deployments requiring strict account separation should select a usable keyring/DPAPI-backed implementation instead of relying on the JSON vault. The JSON vault assumes a non-shared, single-user machine. Set `NEST_AGENT_SECRET_BACKEND=keyring` or `--secret-backend keyring` to store raw values in the OS keyring while keeping only metadata in the JSON vault. Explicit `keyring` selection fails closed when the package or a usable OS keyring backend is unavailable; it never silently downgrades to plaintext JSON. Shared environments should back the same broker contract with an OS keychain or managed vault such as macOS Keychain, Linux Secret Service via `keyring`, Windows DPAPI, or a managed secret store.

Secret status checks only report environment-variable configured state for names registered by channel or MCP configuration. They do not probe arbitrary env var names.

MCP manifests must reference secret material through `secret_env`, where each target process variable maps to a host environment variable or `secret://...` reference. Raw secret-looking keys in MCP `env` are rejected, API responses redact `secret_env`, and values are resolved into the child process environment only at launch. Every live stdio server, including manually configured servers, requires explicit connect approval bound to the exact command/argument hash. Shell/proxy launchers and interpreter eval modes are rejected. Dynamically discovered tools remain at least medium risk and approval-required even under `trust_manifest`; only reviewed static manifest metadata can declare an autonomous low-risk tool.

Logs, LLM/tool context, and memory writes share a redaction boundary for common API key, bearer token, password, authorization header, credentialed URL, provider token, private-key, and certificate shapes. `file.read`, content-search, and content-hashing refuse sensitive paths such as `.env*`, key/certificate files, `.npmrc`, `.pypirc`, netrc, `.git/config`, and nested secret/credential directories, including aliases reached through symlinks or traversal. Agent file writes remain approval-gated and cannot modify the broker vault directly. Test, lint, and repair-validation subprocesses inherit a credential-stripped environment. Redaction is defense-in-depth, not permission to log secrets deliberately.

## Tool Risk

High-risk tools require an enabled per-capability decision, explicit config enablement where applicable, and exact-call approval. Neither the UI nor the capability API can bypass a master flag, launch allowlist, parent capability, resource-integrity blocker, or exact-call approval requirement. Approval cannot be disabled by configuration; it is tied to the owner principal, requested tool-call ID, and exact arguments, is single-use, and expires after 15 minutes by default. It is also bound to the capability revision and a digest covering the current tool specification, applicable policy gates, and parent MCP/skill resource. Set `NEST_AGENT_APPROVAL_TTL_SECONDS` to a positive number to change the decision window. Changed or expired arguments, a changed policy/specification/parent digest, or a changed capability revision require a new approval. Shell execution, file writes, patching, repair mutations, git commits, Codex CLI execution, channel delivery, executable skills, memory imports, plugin review/install/update/enable, behavior-delta activate/reject/rollback actions, and policy memory writes are not production-safe defaults.

## Capability Control

Schema v15 stores durable owner decisions for `tool`, `mcp_server`, and `skill` capabilities separately from discovery metadata, plus an append-only change log. The Settings Capability Center and `GET /api/capabilities` show both configured state (the owner-desired switch) and effective state. `blocked_by` explains unmet prerequisites such as a capability being off, a disabled runtime master flag, a launch allowlist, a disabled/missing parent or plugin, or `resource_changed`.

Mutations use `PUT /api/capabilities/{kind}/{capability_id}` with `enabled` and `expected_revision`. The compare-and-swap revision prevents lost updates; stale writes return HTTP 409 with the current state. `GET /api/capabilities/history` exposes the bounded audit history. These remain single-owner controls under the supported local/private profile: the recorded actor is the owner, not a hosted administrator identity.

Default-off discovery is deliberate. A newly discovered skill is disabled, a dynamic tool supplied by an MCP server or skill has its own disabled default, and every newly created API MCP server is forced disabled until the revisioned capability endpoint records an owner decision. Enabling a dynamic child never enables its parent. High/critical built-ins without a dedicated master flag also default off.

Capability changes apply to future invocation attempts. A live execution gate denies a disabled tool even when the caller holds a registry built before the change. Disabling a tool, skill, or MCP server denies affected pending approvals before they can resume; disabling an MCP server also closes its manager-owned session and blocks connect, test, health, restart, sync-to-live, approval, and invoke paths. This is an invocation boundary, not a general process supervisor: Kestrel does not claim that toggling off always kills an arbitrary built-in subprocess that was already dispatched.

Every enable decision stores a digest of the reviewed tool specification or parent resource. A later tool-schema, skill-manifest/runtime, MCP endpoint/command/configuration, or parent-policy change fails closed as `resource_changed` until the owner records a fresh reviewed decision. Approved continuations are independently revalidated against the current capability revision and combined policy/spec/parent digest immediately before side effects. Deletion removes the override after writing a revocation history row so a recreated resource ID does not inherit the prior authorization.

This control plane does not expand the supported deployment tier. Kestrel remains a local/private, single-owner runtime. Hosted/team use still needs distinct administrator and user identities, role-based authorization for capability changes and approvals, tenant/workspace isolation, hardened authenticated sessions, and actor-attributed audit export.

Self-improvement is local-first. Kestrel may write validated lessons to local `.mv2` memory, prepare local branches/worktrees, create patches, and run validation. `git.create_local_branch` and `git.export_patch` are approval-gated local-only primitives. Remote publishing is a separate lane: direct commits to protected branches, direct pushes to upstream `main`, force pushes, tag pushes, remote rewrites, repo setting edits, GitHub secrets, and workflow enablement are disabled by default. The default tool registry does not include `git.push`; `shell.run` is limited to minimal introspection commands and structurally blocks remote-publishing argv shapes such as `git push`, `git tag`, `git remote set-url`, `gh repo edit`, `gh secret set`, and `gh workflow enable`.

Skill installation is a high-risk file-write action. Uploaded skill capsules are confined to the configured skills directory, validated by manifest shape, and still require approval before installation. Executable skill runtimes such as `python`, `shell`, and future `container` runtimes are always forced to high risk, require exact approval, and require `NEST_AGENT_ALLOW_EXECUTABLE_SKILLS=true` or `--allow-executable-skills`; a manifest cannot downgrade that policy.

Plugin installation is high risk: it fetches public GitHub repositories and materializes skills/MCP entries. CLI/API review, install, update, enable, and sync/materialization routes are disabled unless `NEST_AGENT_ALLOW_PLUGIN_INSTALL=true` or `--allow-plugin-install` is set. Agent-initiated `plugin.review` and `plugin.install` have the same enablement gate and still require exact-call approval before execution. Review fetches and normalizes manifests without installing or executing plugin code, and reports declared dependencies, isolation requirements, warnings, unsupported features, and enable blockers. Installed plugins are not enabled by default unless explicitly requested; plugins that declare unmanaged dependencies or required unavailable isolation can be installed disabled but cannot be enabled until those blockers are resolved. Plugin updates reject manifest ID drift. Plugin-provided MCP stdio servers are restricted to conservative launchers (`npx`, `uvx`, `python -m`, `node`, `bunx`, `deno`) with validated args, and Kestrel stores a command/args hash so later tampering is refused before connect. Plugin-provided MCP servers also require explicit connect approval through `POST /api/mcp/servers/{server_id}/approve-connect` before the first connect/test/sync/invoke path may start the process.

Autonomous scheduling is disabled by default. When enabled, it is bounded by per-cycle task and cycle limits, and it stops at task approval or exact-call tool approval boundaries instead of silently crossing into high-risk work.

Repair branch commits require a current `repair.review` artifact tied to a successful validation result and the current diff hash. `git.commit` never pushes and refuses protected branches from `NEST_AGENT_PROTECTED_BRANCHES`.

## Webhooks

Public channel webhook endpoints reject unsigned payloads by default. Set channel `settings.unsigned_allowed=true` only for deliberately private or already-authenticated generic ingress. It never disables signature verification for the public Telegram webhook route. The Telegram launch scripts enable API auth by default, require a non-empty control-plane token, and trust only the exact hostname from `PUBLIC_URL`; they do not expose the rest of `/api` as an unauthenticated tunnel surface.

Generic/custom channel endpoints can require HMAC-SHA256 signatures by setting channel `settings.signature_secret_env`. The signature is computed over the raw HTTP body bytes and sent in `X-Kestrel-Signature` as either a hex digest or `sha256=<digest>`. GitHub uses `X-Hub-Signature-256`; Stripe uses `Stripe-Signature` with timestamp tolerance; Discord uses Ed25519 and requires `settings.discord_public_key` plus optional PyNaCl support, not an HMAC secret.

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
