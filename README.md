# Kestrel

<p align="center">
  <strong>Local-first AI engineering agent that learns from its work.</strong>
</p>

<p align="center">
  <a href="https://github.com/John-MiracleWorker/Kestrel/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/John-MiracleWorker/Kestrel/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="Memvid v2" src="https://img.shields.io/badge/Memory-Memvid%20v2%20.mv2-6f42c1">
  <img alt="Local first" src="https://img.shields.io/badge/Runtime-local--first-059669">
  <img alt="Status stable local release" src="https://img.shields.io/badge/Status-v0.3.0%20stable-059669">
  <img alt="License Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-blue">
</p>

Kestrel is a memory-native agent runtime for developers who want an AI agent they can run locally, inspect deeply, and improve deliberately. It combines a conversational CLI, a local web workbench, layered Memvid v2 `.mv2` memory, centralized capability controls, tool approvals, task capsules, behavior-delta learning, managed MCP sessions, provider adapters, and deterministic evals.

It is not a chatbot wrapper and not just a memory library. Kestrel is built around a stricter product promise:

> Repeated engineering work should make the agent safer and more capable through evidence-backed, auditable, reversible learning.

Kestrel `v0.3.0` is the stable release for its supported deployment profile: one trusted user, one Kestrel server/worker process, and one local or privately networked node. It is not a hosted/team or multi-tenant Internet service.

## Start Here

Install the latest published release (`v0.3.0`), initialize `.mv2` memory, build the workbench, run a deterministic smoke check, and explicitly open the localhost app:

```bash
curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/v0.3.0/install.sh | KESTREL_REF=v0.3.0 KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash
```

Omit the two launch variables for an install-only run that starts no server.

Then choose a real provider when you are ready to work:

```bash
cd "${KESTREL_HOME:-$HOME/.kestrel-agent}"
.venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider codex-cli --model gpt-5.5
```

The installer uses `mock` only for deterministic health checks. High-risk tools, outbound channels, web access, plugin installs, commits, pushes, and self-modification all stay disabled until explicitly configured and approved.

## Why Kestrel

- **Memory with structure:** working, episodic, semantic, procedural, self, and policy memory live in separate Memvid v2 `.mv2` files with different promotion gates.
- **Learning you can audit:** run-scoped `complete.mv2` capsules, promotion ledgers, validation evidence, behavior deltas, replay, rollback, and activation metrics keep learning inspectable.
- **Actions you control:** every built-in or dynamic tool, MCP server, and skill has a durable on/off decision; shell, patching, file writes, tests, commits, plugin installs, MCP tools, Codex CLI delegation, and self-change requests retain their master flags and exact-call approval gates.
- **A real operator cockpit:** the FastAPI/web workbench exposes runs, traces, approvals, memory/context search, Soul/self views, a Settings Capability Center, MCP controls, plugins, skills, channels, scheduler actions, and support diagnostics.
- **Deterministic by default:** the mock backend and mock LLM keep tests and golden evals reproducible, while live provider checks stay behind explicit integration flags.

## Product Surface

| Surface | What it gives you today |
| --- | --- |
| Conversational agent | `nest-agent chat` with in-memory or Memvid-backed memory, provider selection, tool use, and interactive commands. |
| Local workbench | Browser UI for background runs, SSE timelines, approvals, per-capability controls, tools, memory, behavior deltas, MCP, skills, plugins, channels, scheduler controls, and setup readiness. |
| Durable memory | One `.mv2` file per nested layer, MV2 context frames, token-aware packing, lexical-first retrieval, optional vector sidecars, and explicit policy-write constraints. |
| Controlled learning | Task capsules, promotion gates, validation metadata, behavior-delta proposal/replay/review, low-risk auto-activation behind flags, and rollback paths. |
| Safe tools | Built-in repo, memory, diagnosis, repair, validation, git, web-context, plugin, skill, MCP, and Codex CLI tools with risk classification and approval boundaries. |
| Extensibility | Managed stdio MCP sessions, local skills, and an experimental GitHub plugin review/install flow that reports provenance, risk, dependencies, and enable blockers. |
| Channels | Telegram-shaped, Discord-shaped, generic webhook, and custom JSON ingress, with outbound delivery disabled by default and Telegram admin writes requiring confirmation. |
| Release evidence | `pytest`, `ruff`, `mypy`, web tests/builds, golden evals, Memvid integration tests, provider integration tests, support bundles, and product-readiness reports. |

## How A Run Feels

1. Ask Kestrel to inspect, repair, explain, or continue work in a local repository.
2. Watch the plan, task graph, tool calls, traces, and approval waits in the workbench.
3. Approve high-risk actions only when the exact requested call and arguments look right.
4. Review outputs, validation, memory writes, behavior-delta candidates, and rollback evidence.
5. Let validated lessons influence future runs without allowing hidden policy writes or unreviewed self-modification.

## Documentation Map

- `docs/IMPLEMENTATION_STATUS.md` is the detailed truth table for what is working, partial, or not done.
- `docs/ARCHITECTURE.md` explains the local runtime and memory/control-plane split.
- `docs/PRODUCTION_OPERATIONS.md` defines health, alerts, backup/restore, upgrade/rollback, failure drills, soak testing, and the release gate.
- `docs/reviews/2026-07-16-production-readiness.md` records the pre-publication supported-profile verification matrix and external gate inventory; the GitHub release workflow is authoritative for published artifact validation.
- `docs/MEMORY_OPERATIONS.md` covers `.mv2` backup, restore, verification, and migration.
- `docs/SECURITY.md` documents local-first safety boundaries, auth, webhook signatures, secrets, and high-risk tools.
- `docs/PRODUCTIZATION_ROADMAP.md` is the long-horizon hosted/team product roadmap.
- `docs/DEPLOYMENT.md` covers local installs, Docker, Compose, providers, and runtime checks.

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
curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/v0.3.0/install.sh | KESTREL_REF=v0.3.0 bash
```

The installer clones or updates Kestrel in `${KESTREL_HOME:-$HOME/.kestrel-agent}`, finds Python 3.11 or newer without relying on bare `python`, installs the Memvid/OpenAI/server/MCP extras, builds the web workbench, initializes `.nest/memory/*.mv2`, verifies memory, and runs a deterministic `mock` CLI smoke check. For a safer first install, it does not start the server or open a browser unless explicitly enabled. `mock` is a zero-secret health check, not the intended operating mode. The installer does not ask for secrets or enable high-risk tools.

Production installs must pin both the installer URL and `KESTREL_REF` to an immutable published tag. `main` is a development source, not the published release channel.

Install and explicitly launch the localhost workbench in one command:

```bash
curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/v0.3.0/install.sh | KESTREL_REF=v0.3.0 KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash
```

Useful installer options:

```bash
KESTREL_HOME="$HOME/dev/kestrel" bash install.sh
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_SKIP_WEB=1 bash install.sh
KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash install.sh
KESTREL_OPEN_BROWSER=0 KESTREL_PORT=8766 bash install.sh
```

To stop a detached server started by the opt-in installer launch:

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

When installer launch is enabled, the workbench starts with the smoke-test provider while you configure a real provider. To start it manually:

```bash
.venv/bin/nest-agent server --backend memvid --memory-dir .nest/memory --provider mock --model mock --host 127.0.0.1 --port 8765
```

Manual development install:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[memvid,openai,anthropic,gemini,server,mcp,dev]'
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

`product readiness` is the static long-horizon roadmap report for the full product, including hosted/team capabilities. It is not an exact-build deployment verdict. Use `product setup --check`, live health checks, and the release gate in `docs/PRODUCTION_OPERATIONS.md` for the supported local/private profile.

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

Every live MCP stdio command, including a manually configured server, is bound to a command/argument hash and requires explicit connect approval before Kestrel starts the process. Shell/proxy launchers and interpreter eval modes such as `python -c` or `node --eval` are rejected. Tools discovered dynamically from a live server always remain at least medium risk with exact-call approval, even when static manifest metadata is trusted.

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

The workbench exposes runs, live event streams, approvals, tool filters, MCP server health/sync/connect/disconnect/restart, manual MCP invocation, memory/context utilities, skills discovery feedback, subagent/task graph views, scheduler controls, and a Capability Center under Settings.

The Capability Center lists every built-in or dynamic tool, MCP server, and skill. Its switch records the owner-desired state; the adjacent effective state and `Blocked by` reasons show whether runtime master flags, launch allowlists, a disabled parent/plugin, or a changed resource still prevent use. New discovered skills and new dynamic MCP/skill tools start off. Every MCP server created through the API is forced off until the owner enables it through the revisioned capability endpoint. Enabling a child tool does not enable its parent, satisfy a master flag, or remove an exact-call approval requirement.

The same control plane is available through `GET /api/capabilities`, revision-checked `PUT /api/capabilities/{kind}/{capability_id}`, and `GET /api/capabilities/history`. A `PUT` body contains `enabled` and `expected_revision`; stale writes return HTTP 409 so clients can reload instead of overwriting a newer owner decision. Changes apply to future invocation attempts. Turning a capability off also denies later attempts from stale registries, revokes affected pending approvals, and closes a disabled MCP server's managed session. It does not promise to terminate an arbitrary built-in subprocess that already crossed the dispatch boundary.

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

For a private Telegram agent, set both `settings.allowed_conversation_ids` and `settings.allowed_user_ids` to your numeric Telegram IDs. The polling stack also accepts `TELEGRAM_ALLOWED_CHAT_IDS` and `TELEGRAM_ALLOWED_USER_IDS` as comma-separated fallbacks when those channel settings are omitted. Messages outside either configured allowlist are rejected before a run is created.

Telegram can also act as a single-owner admin surface when the Telegram channel explicitly includes `settings.admin_enabled=true` plus exactly one `settings.owner_user_ids` entry and Kestrel is started with the run manager/server path. `TELEGRAM_ALLOWED_USER_IDS` grants conversation access only and never grants admin ownership. The older `admin_user_ids`/`telegram_owner_ids` setting names remain accepted for configuration compatibility, but still require explicit admin enablement and exactly one owner. Owner-only admin supports deterministic slash commands and bounded natural-language requests such as "show status" or "increase max tool calls to 12." Write actions return an inline confirmation preview before mutation; raw secrets are never accepted through Telegram and should be entered through the local UI/CLI Secret Broker.

The server exposes Telegram setup helpers for webhook deployments:

```text
GET  /api/channels/{channel_id}/telegram/webhook-info
POST /api/channels/{channel_id}/telegram/set-webhook
POST /api/channels/{channel_id}/telegram/delete-webhook
POST /api/channels/{channel_id}/telegram/test-message
```

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

High-risk tools need an enabled per-capability decision, every applicable master flag, and exact-call approval before execution. Capability switches cannot bypass those prerequisites. Approval cannot be disabled by configuration; it is owner-bound, single-use, bound to the requested tool call ID and exact arguments, and expires after 15 minutes by default. It is also bound to the capability revision and a digest of the current tool specification, policy gates, and parent MCP/skill resource. Changed arguments, policy, specification, parent resource, capability revision, or expiry require a new approval.

Secrets stay out of chat. The Secret Broker stores channel/MCP/tool credentials through backend API/UI flows and returns only metadata such as `secret://...` handles, configured state, validation state, timestamps, and fingerprints. No public GET route returns raw secret values; MCP `secret_env` can point to host env names or broker refs, and channel status checks use the same metadata-only boundary.

Self-improvement is local-first: Kestrel can learn into local `.mv2` memory, create approval-gated local branches with `git.create_local_branch`, export patch artifacts with `git.export_patch`, and run tests without publishing. Remote mutation is a separate gated lane. `git.commit` never pushes, refuses protected branches by default, and repair branch commits also require a current `repair.review` artifact tied to a successful validation result and the current diff hash. The shell tool blocks common remote-publishing escape routes unless a future publishing mode explicitly gates them.

Web access is read-only context gathering. `web.search` and `web.fetch` stay disabled unless `--allow-web` / `NEST_AGENT_ALLOW_WEB=1` is set; fetches reject private, local, link-local, multicast, reserved, and unspecified addresses. Self-change requests stay behind `--allow-self-modification` / `NEST_AGENT_ALLOW_SELF_MODIFICATION=1`, exact-call approval, and the existing repair/commit path.

## Validation

Core validation:

```bash
python -m compileall -q src tests scripts
python -m ruff check scripts src tests
python -m mypy src
python -m pytest -q tests/test_capability_policy.py tests/test_capability_control_plane.py tests/test_state_store.py
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
- `docs/RELEASE_CHECKLIST.md` lists release-candidate validation commands.

## Broader and Unsupported Product Gaps

These capabilities are outside the supported single-user, single-node profile or remain optional-surface hardening work; they do not imply hosted/team support:

- Broader live-provider CI/release coverage beyond the locally validated Ollama Cloud path.
- Richer provider-specific JSON/context/streaming hardening for every native provider surface.
- Hosted/team identity, distinct administrator principals, hardened sessions, workspace ownership, role-scoped capability policy, and tenant isolation.
- Real MCP SSE/streamable HTTP fixtures and soak testing.
- Container-grade skill isolation and package dependency management.
- Managed plugin dependency installation and container-grade isolation beyond the current review metadata and enable blockers.
- More capable planner/executor/reviewer loops with Codex-backed isolated worker branches or worktrees.
- Production bot identity verification and platform-specific rate-limit handling.
- Fully autonomous self-improvement with diff review, test gates, rollback, and explicit human approval.

The authoritative status page is `docs/IMPLEMENTATION_STATUS.md`.
