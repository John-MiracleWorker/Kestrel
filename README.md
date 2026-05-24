# Kestrel

Kestrel is a local-first, memory-native agent runtime built around Nested Learning-inspired memory layers and Memvid v2 `.mv2` files.

It is not only a memory library and not only a chatbot wrapper. The current repo contains a conversational CLI, a FastAPI/web control plane, provider adapters, a tool registry with approval gates, managed MCP stdio sessions, skill loading, task capsules, a conservative consolidation pipeline, and a deterministic test path.

Kestrel is still an alpha runtime. It is useful for local development and hardening work, but it is not yet a production multi-user agent platform.

## What Works Now

- CLI chat with in-memory or Memvid `.mv2` memory.
- One-shot GitHub installer for the local Memvid-backed agent runtime; the installer uses `mock` only for deterministic smoke checks.
- Deterministic mock provider for tests and golden evals.
- OpenAI Responses provider with streaming deltas when the SDK stream surface is available.
- OpenAI-compatible chat completions provider for local/model-server endpoints.
- Local Codex CLI provider for using `codex exec` as the response engine.
- Retryable provider fallback wrapper and provider capability metadata.
- One permanent `.mv2` file per memory layer: working, episodic, semantic, procedural, self, and policy.
- MV2 context frames plus a token-aware pseudo-context packer with evidence pointers, conflict warnings, and on-demand raw expansion.
- Run-scoped `complete.mv2` task capsules for reviewable learning signals, plus behavior-delta proposal/replay/ledger/review flows for controlled self-modification.
- Nested Learning kernel with context-flow metadata, optimizer traces, promotion gates, and explicit policy-write constraints.
- Built-in tools for memory, context, repo search and file metadata, patching, tests, linting, git status/diff/log/show/commit, Memvid verify/doctor/stats, diagnosis, repair, Soul/self inspection, gated web context, skills, plugins, MCP registry inspection, project scripts, and Codex CLI delegation.
- Exact-call approval gates for high-risk tools, including shell, file writes, patching, tests/lint, repair mutations, commits, skill installs, plugin review/install, behavior-delta review actions, imports, and Codex CLI delegation.
- SQLite control-plane state for runs, approvals, MCP servers, skills, plugins, task nodes, subagents, and replay-safe terminal transitions. SQLite is not the retrieval memory store.
- Local FastAPI/web workbench with background runs, SSE timeline events, approvals, Soul/self views, gated web search, memory/context tools, behavior-delta review, MCP controls, skills, plugins, subagents, and scheduler actions.
- Managed stdio MCP sessions with lazy connect/disconnect/restart/health, tool discovery, vetting metadata, and approval-by-default risk normalization.
- Experimental plugin registry/CLI/API/web review flow that can inspect GitHub plugin manifests, report dependency/isolation blockers, and materialize plugin-provided skills and MCP server entries.
- Multi-channel ingress for Telegram-shaped, Discord-shaped, webhook, and custom payloads, with outbound delivery disabled by default.
- Optional autonomous scheduler that drains approved ready tasks within bounded task/cycle limits.
- Safe repair primitives with branch isolation, diagnosis-gated validation, reviewer artifacts, and commit gates for repair branches.
- Live-provider validation harnesses: provider integration tests, full golden evals, and isolated live-learning E2E checks validated locally with Ollama Cloud + `gpt-oss:120b` on memory and Memvid backends.

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

One-shot local install:

```bash
curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/main/install.sh | bash
```

The installer clones or updates Kestrel in `${KESTREL_HOME:-$HOME/.kestrel-agent}`, finds Python 3.11 or newer without relying on bare `python`, installs the Memvid/OpenAI/server/MCP/dev extras, builds the web workbench, initializes `.nest/memory/*.mv2`, verifies memory, runs a deterministic `mock` CLI smoke check, starts the localhost server in a detached session, waits for `/api/health`, and opens the web UI at `http://127.0.0.1:8765/`. `mock` is a zero-secret health check, not the intended operating mode. The installer does not ask for secrets or enable high-risk tools.

Useful installer options:

```bash
KESTREL_HOME="$HOME/dev/kestrel" bash install.sh
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_SKIP_WEB=1 bash install.sh
KESTREL_START_SERVER=0 bash install.sh
KESTREL_OPEN_BROWSER=0 KESTREL_PORT=8766 bash install.sh
```

To stop the default detached server:

```bash
kill "$(cat "$HOME/.kestrel-agent/.nest/server.pid")"
screen -S kestrel-agent -X quit 2>/dev/null || true
```

After install, choose a real provider for normal use:

```bash
cd "${KESTREL_HOME:-$HOME/.kestrel-agent}"
.venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider codex-cli --model gpt-5.5
OPENAI_API_KEY=... .venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider openai --model gpt-5.5
.venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider openai-compatible --base-url http://127.0.0.1:1234/v1 --model local-model
```

The one-shot path starts the workbench with the smoke-test provider while you configure a real provider. To start it manually:

```bash
.venv/bin/nest-agent server --backend memvid --memory-dir .nest/memory --provider mock --model mock --host 127.0.0.1 --port 8765
```

Manual development install:

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

## CLI Configuration

`nest-agent` starts from `AgentConfig.from_env()`. Environment variables under `NEST_AGENT_*` provide the baseline config, and CLI flags are sparse overrides for a single command. With no env or flags, the default memory root is `.nest/memory`.

```bash
export NEST_AGENT_BACKEND=memory
export NEST_AGENT_MEMORY_DIR=.nest/memory
export NEST_AGENT_PROVIDER=mock
export NEST_AGENT_MODEL=mock
nest-agent doctor
```

The older `NESTED_MEMVID_*` names are not part of the agent runtime config; use the `NEST_AGENT_*` names shown in `.env.example`.

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

Productization and support checks:

```bash
nest-agent product readiness --json
nest-agent product setup --backend memory --provider mock --json
nest-agent product provider-certification --backend memory --provider mock --json
nest-agent product support-bundle --backend memory --provider mock --output /tmp/kestrel-support.zip --json
```

Support bundles are redacted diagnostic archives. They include readiness reports, runtime metadata, git status, state-table counts, log file metadata, and a bounded redacted event-log tail; they do not include raw Secret Broker vault contents, raw environment variable values, or `.mv2` memory files.

Provider certification reports are read-only and redacted. They record per-provider readiness, credential/base-url presence, manual host checks, and the validation commands needed before treating live providers as release-certified.

Plugin registry commands:

```bash
nest-agent plugins list --backend memory
nest-agent plugins review owner/repo --backend memory
nest-agent plugins install owner/repo --backend memory
nest-agent plugins inspect <plugin_id> --backend memory
nest-agent plugins enable <plugin_id> --backend memory
nest-agent plugins disable <plugin_id> --backend memory
```

Plugin review, installation, and updates fetch public GitHub plugin sources, accept `kestrel.plugin.json` plus limited Hermes-style `plugin.yaml`, and materialize plugin-declared skills/MCP servers disabled by default. Review returns provenance, risk, declared dependency, isolation, warning, unsupported-feature, and enable-blocker metadata without installing or executing plugin code. Agent-initiated `plugin.review` and `plugin.install` are high risk: they require `--allow-plugin-install` / `NEST_AGENT_ALLOW_PLUGIN_INSTALL` plus exact-call approval before execution.

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
Provider aliases are also available for `openrouter`, `deepseek`, `kimi`, `ollama`, `ollama-cloud`, `anthropic`, and `gemini`. OpenRouter, DeepSeek, Kimi, and local Ollama use the OpenAI-compatible contract; Ollama Cloud uses Ollama's native cloud API; Anthropic and Gemini use their native surfaces.

DeepSeek:

```bash
export DEEPSEEK_API_KEY=...
nest-agent chat \
  --backend memory \
  --provider deepseek \
  --model deepseek-v4-pro \
  --message "hello"
```

Kimi:

```bash
export MOONSHOT_API_KEY=...
nest-agent chat \
  --backend memory \
  --provider kimi \
  --model kimi-k2.6 \
  --message "hello"
```

Ollama Cloud direct API:

```bash
export OLLAMA_API_KEY=...
nest-agent chat \
  --backend memory \
  --provider ollama-cloud \
  --model gpt-oss:120b \
  --message "hello"
```

The workbench model picker fetches provider model catalogs from `/api/runtime/models?provider=<name>` when a provider is selected. If credentials or a local model server are unavailable, it keeps deterministic fallback suggestions instead of blocking the run form.

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

The workbench exposes runs, live event streams, approvals, tool filters, MCP server health/sync/connect/disconnect/restart, manual MCP invocation, memory/context utilities, skills discovery feedback, subagent/task graph views, and scheduler controls.

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

Telegram can also act as a single-owner admin surface when the Telegram channel includes `settings.admin_user_ids` (or `owner_user_ids`) and Kestrel is started with the run manager/server path. Owner-only commands are deterministic slash commands plus inline buttons: `/status`, `/runs`, `/run <run_id>`, `/cancel <run_id>`, `/approve <approval_id>`, `/deny <approval_id>`, and `/help`. Pending approval buttons and `/approve` use the exact stored approval arguments; non-owner admin commands/callbacks are denied before creating or resuming a run.

Generic/custom webhooks can require HMAC-SHA256 signatures through the channel `settings.signature_secret_env` setting. Telegram webhook deployments should set `settings.signature_provider=telegram` and `settings.signature_secret_env` so the public route verifies Telegram's `X-Telegram-Bot-Api-Secret-Token` header.

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
NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=false
NEST_AGENT_MAX_ACTIVE_DELTAS_PER_RUN=8
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
python scripts/eval_behavior_deltas.py --scenario tests/evals/behavior_deltas/policy_write_requires_approval.json
npm run test --prefix web
npm run build --prefix web
```

Optional integration checks:

```bash
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memory --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memory
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memvid --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memvid
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memory --output-root /tmp/kestrel-live-learning-memory
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
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

- Broader live-provider CI/release coverage beyond the locally validated Ollama Cloud path.
- Richer provider-specific JSON/context/streaming hardening for every native provider surface.
- Production-grade auth, user/session isolation, and deployment boundaries.
- Real MCP SSE/streamable HTTP fixtures and soak testing.
- Container-grade skill isolation and package dependency management.
- Managed plugin dependency installation and container-grade isolation beyond the current review metadata and enable blockers.
- More capable planner/executor/reviewer loops with Codex-backed isolated worker branches or worktrees.
- Production bot identity verification and platform-specific rate-limit handling.
- Fully autonomous self-improvement with diff review, test gates, rollback, and explicit human approval.

The authoritative status page is `docs/IMPLEMENTATION_STATUS.md`.
