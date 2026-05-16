# Security Notes

Kestrel is a local-first alpha runtime. Treat it as trusted-local infrastructure unless a separate authentication and network boundary is added.

## Default Posture

The defaults keep high-risk behavior off:

```text
NEST_AGENT_ALLOW_SHELL=false
NEST_AGENT_ALLOW_FILE_WRITE=false
NEST_AGENT_ALLOW_POLICY_WRITES=false
NEST_AGENT_ALLOW_CODEX_CLI=false
NEST_AGENT_ENABLE_CHANNEL_DELIVERY=false
NEST_AGENT_ENABLE_AUTO_CONSOLIDATION=false
NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN=true
```

Do not loosen these in shared deployments without a separate approval and audit story.

## Network Binding

Bind the web/API server to `127.0.0.1` by default:

```bash
nest-agent server --host 127.0.0.1 --port 8765
```

If binding to `0.0.0.0`, put Kestrel behind an authenticated reverse proxy and keep dangerous tool flags disabled.

## Secrets

Use environment variables for provider keys. Do not store secrets in `.mv2` files, `.nest/config`, checked-in fixtures, or MCP server manifests.

Logs and run events redact common API key, bearer token, password, authorization header, GitHub token, OpenAI key, Anthropic key, OpenRouter key, and private-key shapes. Redaction is defense-in-depth, not permission to log secrets deliberately.

## Tool Risk

High-risk tools require explicit config enablement and approval flow integration. Shell execution, file writes, git commits, Codex CLI execution, channel delivery, and policy memory writes are not production-safe defaults.

## Memory Safety

Memory promotion must carry evidence, provenance, confidence, and validation status. A single ordinary event must not become policy, procedural, or semantic memory.

## Incident Response

If a secret appears in a prompt, tool argument, or log:

1. Rotate the provider credential.
2. Stop the server.
3. Preserve `.nest/logs` for local audit.
4. Verify the redacted log surface with `GET /api/logs` or `nest-agent doctor`.
5. Remove any contaminated local working files outside `.mv2` memory.
