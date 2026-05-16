# Security Notes

Kestrel is a local-first alpha runtime. Treat it as trusted-local infrastructure unless a separate authentication and network boundary is added.

## Default Posture

The defaults keep high-risk behavior off:

```text
NEST_AGENT_ALLOW_SHELL=false
NEST_AGENT_ALLOW_FILE_WRITE=false
NEST_AGENT_ALLOW_POLICY_WRITES=false
NEST_AGENT_ALLOW_CODEX_CLI=false
NEST_AGENT_ALLOW_PLUGIN_INSTALL=false
NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER=false
NEST_AGENT_MAX_SCHEDULER_TASKS=3
NEST_AGENT_MAX_SCHEDULER_CYCLES=5
NEST_AGENT_ENABLE_CHANNEL_DELIVERY=false
NEST_AGENT_ENABLE_AUTO_CONSOLIDATION=false
NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN=true
NEST_AGENT_REQUIRE_API_AUTH=false
```

Do not loosen these in shared deployments without a separate approval and audit story.

## Network Binding

Bind the web/API server to `127.0.0.1` by default:

```bash
nest-agent server --host 127.0.0.1 --port 8765
```

If binding to `0.0.0.0`, enable `NEST_AGENT_REQUIRE_API_AUTH=1`, set `NEST_AGENT_API_TOKEN`, put Kestrel behind an authenticated reverse proxy, and keep dangerous tool flags disabled.

## API Auth

The local API can require a shared token:

```bash
export NEST_AGENT_REQUIRE_API_AUTH=1
export NEST_AGENT_API_TOKEN='replace-with-local-secret'
nest-agent server --host 127.0.0.1 --port 8765
```

Clients may send either `Authorization: Bearer <token>` or `X-Kestrel-API-Key: <token>`. This is a local operator gate, not multi-user authorization.

## Secrets

Use environment variables for provider keys. Do not store secrets in `.mv2` files, `.nest/config`, checked-in fixtures, or MCP server manifests.

Logs and run events redact common API key, bearer token, password, authorization header, GitHub token, OpenAI key, Anthropic key, OpenRouter key, and private-key shapes. Redaction is defense-in-depth, not permission to log secrets deliberately.

## Tool Risk

High-risk tools require explicit config enablement where applicable and exact-call approval. Approval is tied to the requested tool-call ID and arguments; changed arguments require a new approval. Shell execution, file writes, patching, repair mutations, git commits, Codex CLI execution, channel delivery, and policy memory writes are not production-safe defaults.

Skill installation is a high-risk file-write action. Uploaded skill capsules are confined to the configured skills directory, validated by manifest shape, and still require approval before installation.

Plugin installation is newer and should be treated as high risk: it fetches GitHub repositories and materializes skills/MCP entries. Keep `NEST_AGENT_ALLOW_PLUGIN_INSTALL=false` in shared or exposed runtimes until the install path has the same explicit enforcement and approval guarantees as other high-risk mutation paths.

Autonomous scheduling is disabled by default. When enabled, it is bounded by per-cycle task and cycle limits, and it stops at task approval or exact-call tool approval boundaries instead of silently crossing into high-risk work.

Repair branch commits require a current `repair.review` artifact tied to a successful validation result and the current diff hash. `git.commit` never pushes.

## Webhooks

Generic/custom channel endpoints can require HMAC-SHA256 signatures by setting channel `settings.signature_secret_env`. The signature is computed over canonical JSON and sent in `X-Kestrel-Signature` as either a hex digest or `sha256=<digest>`.

## Memory Safety

Memory promotion must carry evidence, provenance, confidence, and validation status. A single ordinary event must not become policy, procedural, or semantic memory.

## Incident Response

If a secret appears in a prompt, tool argument, or log:

1. Rotate the provider credential.
2. Stop the server.
3. Preserve `.nest/logs` for local audit.
4. Verify the redacted log surface with `GET /api/logs` or `nest-agent doctor`.
5. Remove any contaminated local working files outside `.mv2` memory.
