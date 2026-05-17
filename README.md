# Kestrel

Kestrel is a local-first, memory-native agent runtime built around Nested Learning-inspired memory layers and Memvid v2 `.mv2` files.

It is not only a memory library and not only a chatbot wrapper. The current repo contains a conversational CLI, a FastAPI/web control plane, provider adapters, a tool registry with approval gates, managed MCP stdio sessions, skill loading, task capsules, a conservative consolidation pipeline, and a deterministic test path.

Kestrel is still an alpha runtime. It is useful for local development and hardening work, but it is not yet a production multi-user agent platform.

## What Works Now

- CLI chat with in-memory or Memvid `.mv2` memory.
- Deterministic mock provider for tests and golden evals.
- OpenAI Responses provider with streaming deltas when the SDK stream surface is available.
- OpenAI-compatible chat completions provider for local/model-server endpoints.
- Local Codex CLI provider for using `codex exec` as the response engine.
- Retryable provider fallback wrapper and provider capability metadata.
- One permanent `.mv2` file per memory layer: working, episodic, semantic, procedural, self, and policy.
- MV2 context frames plus a token-aware pseudo-context packer with evidence pointers, conflict warnings, and on-demand raw expansion.
- Run-scoped `complete.mv2` task capsules for reviewable learning signals.
- Nested Learning kernel with context-flow metadata, optimizer traces, promotion gates, and explicit policy-write constraints.
- Built-in tools for memory, context, repo search, patching, tests, linting, git status/diff/commit, Memvid verify/doctor/stats, diagnosis, repair, Soul/self inspection, gated web context, skills, and Codex CLI delegation.
- Exact-call approval gates for high-risk tools, including shell, file writes, patching, tests/lint, repair mutations, commits, skill installs, imports, and Codex CLI delegation.
- SQLite control-plane state for runs, approvals, MCP servers, skills, plugins, task nodes, subagents, and replay-safe terminal transitions. SQLite is not the retrieval memory store.
- Local FastAPI/web workbench with background runs, SSE timeline events, approvals, Soul/self views, gated web search, memory/context tools, MCP controls, skills, subagents, and scheduler actions.
- Managed stdio MCP sessions with lazy connect/disconnect/restart/health, tool discovery, vetting metadata, and approval-by-default risk normalization.
- Experimental plugin registry/CLI that can materialize plugin-provided skills and MCP server entries from GitHub plugin manifests.
- Multi-channel ingress for Telegram-shaped, Discord-shaped, webhook, and custom payloads, with outbound delivery disabled by default.
- Optional autonomous scheduler that drains approved ready tasks within bounded task/cycle limits.
- Safe repair primitives with branch isolation, diagnosis-gated validation, reviewer artifacts, and commit gates for repair branches.

See `docs/IMPLEMENTATION_STATUS.md` for the detailed truth table.

## Memory Layout

Kestrel uses Memvid v2 `.mv2` files as the durable memory substrate:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
.nest/memory/self.mv2
.nest/memory/policy.mv2
```

One file per layer is intentional. The layers have different update cadences, trust thresholds, search strategies, and promotion rules.

Run capsules are separate artifacts:

```text
.nest/runs/{run_id}/complete.mv2
```

`complete.mv2` is not a permanent memory layer. It captures run evidence and candidate learning signals for reviewable consolidation.

Important storage rules:

- Use Memvid v2 `.mv2` files only.
- Do not implement QR/video-frame Memvid v1 behavior.
- Never call `create(path)` on an existing `.mv2` file.
- SQLite stores control-plane state only; it is not a memory replacement.
- Policy memory writes require explicit instruction, high validation, repeat evidence, config enablement, and review or equivalent explicit configuration.

## Quick Start

Use Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[memvid,openai,server,mcp,dev]'
npm install --prefix web
npm run build --prefix web
```

Fast local validation:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "hello"
```

The mock provider and in-memory backend are deterministic and are the default fast path for tests.

## CLI Chat

One-shot chat:

```bash
nest-agent chat --backend memory --provider mock --message "hello"
```

Interactive chat:

```bash
nest-agent chat --backend memory --provider mock --session-id local-dev
```

Useful interactive commands:

```text
/tools
/plugins
/self
/soul
/capabilities
/web <query>
/context <query>
/memory <query>
/doctor
/session
/exit
```

Background run and approval flow:

```bash
nest-agent run --backend memory --provider mock --json --events "inspect this repo"
nest-agent approvals --backend memory --json
nest-agent approve <approval_id> --backend memory --json
nest-agent status <run_id> --backend memory --json --events
```

Plugin registry commands:

```bash
nest-agent plugins list --backend memory
nest-agent plugins install owner/repo --backend memory
nest-agent plugins inspect <plugin_id> --backend memory
nest-agent plugins enable <plugin_id> --backend memory
nest-agent plugins disable <plugin_id> --backend memory
```

Plugin installation and updates fetch public GitHub plugin sources, accept `kestrel.plugin.json` plus limited Hermes-style `plugin.yaml`, and materialize plugin-declared skills/MCP servers disabled by default. Agent-initiated `plugin.install` is high risk: it requires `--allow-plugin-install` / `NEST_AGENT_ALLOW_PLUGIN_INSTALL` plus exact-call approval before execution.

## Memvid Backend

Initialize and verify layer files:

```bash
nest-agent init --backend memvid --memory-dir .nest/memory
nest-agent memory verify --backend memvid --memory-dir .nest/memory
nest-agent memory doctor --backend memvid --memory-dir .nest/memory
```

Run with Memvid and OpenAI:

```bash
export OPENAI_API_KEY=...
nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider openai \
  --model gpt-5.5 \
  --message "What do you remember about this project?"
```

If your account exposes a different model, pass that model name instead.

The Memvid adapter is lexical-first by default (`enable_vec=False`, `enable_lex=True`) so local writes do not accidentally require embeddings. Embeddings can be enabled deliberately where needed.

## Local Providers

OpenAI-compatible local/model-server endpoints:

```bash
nest-agent chat \
  --backend memory \
  --provider openai-compatible \
  --base-url http://127.0.0.1:1234/v1 \
  --model local-model \
  --message "hello"
```

Use `--api-key-env NAME` when the endpoint needs a non-default API key environment variable.
Provider aliases are also available for `openrouter`, `ollama`, `anthropic`, and `gemini`. OpenRouter and Ollama use the OpenAI-compatible contract; Anthropic and Gemini use their native SDK surface when installed with the matching optional extras.

Codex CLI as the response provider:

```bash
nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider codex-cli \
  --model gpt-5.5 \
  --timeout-seconds 600 \
  --message "Help me continue this build"
```

The provider runs `codex exec` in read-only, ephemeral mode by default. Write-capable Codex work belongs behind the separate high-risk `codex.exec` tool approval path.

## Web Workbench

Build the web assets, then start the local server:

```bash
npm run build --prefix web
nest-agent server --backend memory --provider mock --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/`.

The workbench exposes runs, live event streams, approvals, MCP server health/sync/connect/disconnect/restart, manual MCP invocation, memory/context utilities, skills, subagent/task graph views, and scheduler controls.

The Soul tab surfaces Kestrel's non-secret self model: identity, memory layers, available tools, skills, plugins, MCP state, validated self-memory capture, and gated web search.

To require a local bearer/API-key token:

```bash
export NEST_AGENT_REQUIRE_API_AUTH=1
export NEST_AGENT_API_TOKEN='replace-with-local-secret'
nest-agent server --backend memory --provider mock --host 127.0.0.1 --port 8765
```

Clients can send `Authorization: Bearer <token>` or `X-Kestrel-API-Key: <token>`.

## Channels

Kestrel can normalize Telegram Bot API updates, Discord message/interaction-shaped payloads, generic webhooks, and custom JSON into the same run loop:

```bash
nest-agent channel \
  --backend memory \
  --provider mock \
  telegram \
  --payload-file telegram-update.json
```

Outbound delivery is disabled by default. To send real replies, configure `.nest/config/channels.json` from `config/channels.example.json`, set the relevant secret environment variable, enable that channel's `send_enabled` or `auto_reply`, and start with `--enable-channel-delivery`.

Generic/custom webhooks can require HMAC-SHA256 signatures through the channel `settings.signature_secret_env` setting.

## Safety Model

Kestrel defaults to local, conservative behavior:

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
NEST_AGENT_ALLOW_WEB=false
NEST_AGENT_ALLOW_SELF_MODIFICATION=false
NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER=false
NEST_AGENT_ENABLE_CHANNEL_DELIVERY=false
NEST_AGENT_ENABLE_AUTO_CONSOLIDATION=false
NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN=true
NEST_AGENT_REQUIRE_API_AUTH=false
```

High-risk tools need capability enablement where applicable and exact-call approval before execution. Approval is bound to the requested tool call ID and arguments; changed arguments require a new approval.

Secrets stay out of chat. The Secret Broker stores channel/MCP/tool credentials through backend API/UI flows and returns only metadata such as `secret://...` handles, configured state, validation state, timestamps, and fingerprints. No public GET route returns raw secret values; MCP `secret_env` can point to host env names or broker refs, and channel status checks use the same metadata-only boundary.

Self-improvement is local-first: Kestrel can learn into local `.mv2` memory, create approval-gated local branches with `git.create_local_branch`, export patch artifacts with `git.export_patch`, and run tests without publishing. Remote mutation is a separate gated lane. `git.commit` never pushes, refuses protected branches by default, and repair branch commits also require a current `repair.review` artifact tied to a successful validation result and the current diff hash. The shell tool blocks common remote-publishing escape routes unless a future publishing mode explicitly gates them.

Web access is read-only context gathering. `web.search` and `web.fetch` stay disabled unless `--allow-web` / `NEST_AGENT_ALLOW_WEB=1` is set; fetches reject private, local, link-local, multicast, reserved, and unspecified addresses. Self-change requests stay behind `--allow-self-modification` / `NEST_AGENT_ALLOW_SELF_MODIFICATION=1`, exact-call approval, and the existing repair/commit path.

## Validation

Core validation:

```bash
python -m compileall -q src tests scripts
python -m ruff check scripts src tests
python -m mypy src
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
npm run test --prefix web
npm run build --prefix web
```

Optional integration checks:

```bash
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

Use `python -m pytest` for optional integration tests so fixture subprocesses inherit the same interpreter, environment, and installed extras.

## Packaging and Deployment

Common commands:

```bash
make install-dev
make validate
make docker-build
make docker-doctor
```

Operational docs:

- `docs/DEPLOYMENT.md` covers local installs, Docker, Compose, providers, and runtime checks.
- `docs/MEMORY_OPERATIONS.md` covers `.mv2` backup, restore, verification, and migration.
- `docs/SECURITY.md` documents the local-first posture, API token gate, webhook signatures, and high-risk tool gates.
- `docs/RELEASE_CHECKLIST.md` lists alpha release validation commands.

## Current Gaps

Kestrel is not yet production-complete. The main remaining hardening areas are:

- Native tool-calling parity across providers beyond the portable JSON envelope.
- Streaming parity for OpenAI-compatible/local providers.
- Production-grade auth, user/session isolation, and deployment boundaries.
- Real MCP SSE/streamable HTTP fixtures and soak testing.
- Container-grade skill isolation and package dependency management.
- Plugin install gate enforcement, approval UX, dependency isolation, and security review.
- More capable planner/executor/reviewer loops with isolated worker branches or worktrees.
- Production bot identity verification and platform-specific rate-limit handling.
- Fully autonomous self-improvement with diff review, test gates, rollback, and explicit human approval.

The authoritative status page is `docs/IMPLEMENTATION_STATUS.md`.
