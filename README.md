# Kestrel

<p align="center">
  <strong>Local-first AI engineering agent that learns from its work.</strong>
</p>

<p align="center">
  <a href="https://github.com/John-MiracleWorker/Kestrel/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/John-MiracleWorker/Kestrel/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11 through 3.13" src="https://img.shields.io/badge/Python-3.11--3.13-3776AB?logo=python&logoColor=white">
  <img alt="Memvid v2" src="https://img.shields.io/badge/Memory-Memvid%20v2%20.mv2-6f42c1">
  <img alt="Local first" src="https://img.shields.io/badge/Runtime-local--first-059669">
  <img alt="Status v0.4.0 stable" src="https://img.shields.io/badge/Status-v0.4.0%20stable-059669">
  <img alt="License Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-blue">
</p>

Kestrel is a memory-native agent runtime for developers who want an AI agent they can run locally, inspect deeply, and improve deliberately. It combines a conversational CLI, a local web workbench, durable proactive routines, layered Memvid v2 `.mv2` memory, centralized capability controls, tool approvals, task capsules, behavior-delta learning, managed MCP sessions, provider adapters, and deterministic evals.

It is not a chatbot wrapper and not just a memory library. Kestrel is built around a stricter product promise:

> Repeated engineering work should make the agent safer and more capable through evidence-backed, auditable, reversible learning.

`v0.4.0` is the current stable release for one trusted user, one Kestrel server/worker process,
and one local or privately networked node, not a hosted/team or multi-tenant Internet service.
This source tree contains tag-ready release metadata; the installer, packages, and GHCR tags below
become available only after the exact-tag release workflow publishes them.

## Start Here

The one-shot Bash installer supports Intel/Apple-silicon macOS and Linux x86_64,
including x86_64 Linux inside WSL. After publication, Linux ARM64 uses the release's
`linux/arm64` container image. On native Windows, use the wheel rather than Git Bash or the Windows
`bash.exe` launcher. The exact downloaded universal wheel is release-gated on native
Windows, Linux x86_64, Apple-silicon macOS, and Intel macOS with Python 3.11 through 3.13.
Each macOS lane asserts its actual CPU architecture so
a hosted-runner label change fails closed.

After the `v0.4.0` release workflow publishes its artifacts, install the current stable release,
initialize `.mv2` memory, install the bundled workbench, run a deterministic smoke check, and
explicitly open the localhost app:

```bash
curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.4.0/install.sh | KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash
```

Omit the two launch variables for an install-only run that starts no server.

After Trusted Publishing completes, the same universal wheel is also available from PyPI:

```bash
python -m pip install "nested-memvid-agent[memvid,server,mcp,keyring]==0.4.0"
```

The package name remains `nested-memvid-agent`; the product and CLI are Kestrel and `nest-agent`.
This command is a publication target and is not claimed to work from the pre-publication checkout.

For `v0.4.0`, after the release workflow succeeds, Linux ARM64 uses the same gated
release image through GHCR rather than the Bash installer:

```bash
docker pull --platform linux/arm64 ghcr.io/john-miracleworker/kestrel:v0.4.0
docker run --rm --platform linux/arm64 \
  ghcr.io/john-miracleworker/kestrel:v0.4.0 \
  nest-agent doctor --backend memvid --memory-dir /tmp/kestrel-doctor --provider mock
```

The workflow publishes both `linux/amd64` and `linux/arm64` from the exact scanned
image archives, labels each image with the release source, version, and commit, and
also publishes `sha-<full-GitHub-SHA>`. The multi-platform index is composed from the
registry-returned per-platform push digests, never from the temporary mutable tags.
Registry tags are pointers; deployment automation should resolve and pin the
multi-platform digest and verify its GitHub attestation. This is a released-byte
identity guarantee, not a bit-reproducible-build claim. The documented release URLs and tags are
publication targets and are not claimed to exist from this pre-publication source checkout.

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
- **Conversation continuity with a recall boundary:** Kestrel reconstructs up to the 8 most recent completed user/assistant turns, bounded to 8,000 characters, without duplicating the live turn. Recalled memory derived from tools, web pages, files, or channels is JSON-encoded user-role evidence, never a system-priority instruction.
- **Proactivity with a durable fence:** disabled-by-default UTC routines can create internally scoped runs on a one-shot or fixed interval, with revision checks, leased occurrences, overlap suppression, and the same capability and exact-call approval gates as interactive work.
- **A real operator cockpit:** the FastAPI/web workbench exposes runs, traces, approvals, proactive-routine editing/history/manual launch, memory/context search, Soul/self views, a Settings Capability Center, MCP controls, plugins, skills, channels, scheduler actions, and support diagnostics.
- **Deterministic by default:** the mock backend and mock LLM keep tests and golden evals reproducible, while live provider checks stay behind explicit integration flags.

## Product Surface

| Surface | What it gives you today |
| --- | --- |
| Conversational agent | `nest-agent chat` with in-memory or Memvid-backed memory, provider selection, tool use, and interactive commands. |
| Local workbench | Browser UI for background runs, SSE timelines, approvals, routine controls, per-capability controls, tools, memory, behavior deltas, MCP, skills, plugins, channels, scheduler controls, and setup readiness. |
| Durable memory | One `.mv2` file per nested layer, MV2 context frames, token-aware packing, lexical-first retrieval, optional vector sidecars, and explicit policy-write constraints. |
| Controlled learning | Task capsules, promotion gates, validation metadata, behavior-delta proposal/replay/review, low-risk auto-activation behind flags, and rollback paths. |
| Safe tools | Built-in repo, memory, diagnosis, repair, validation, git, web-context, plugin, skill, MCP, and Codex CLI tools with risk classification and approval boundaries. |
| Extensibility | Managed stdio MCP sessions, local skills, and an experimental GitHub plugin review/install flow that reports provenance, risk, dependencies, and enable blockers. |
| Channels | Telegram-shaped, Discord-shaped, generic webhook, and custom JSON ingress, with outbound delivery disabled by default and Telegram admin writes requiring confirmation. |
| Proactive routines | Durable disabled-by-default one-shot and fixed-interval schedules through CLI/API, with CAS owner controls, bounded dispatch, internal transcript scope, and occurrence history. |
| Release evidence | `pytest`, `ruff`, `mypy`, web tests/builds, golden evals, Memvid integration tests, provider integration tests, support bundles, and product-readiness reports. |

## How A Run Feels

1. Ask Kestrel to inspect, repair, explain, or continue work in a local repository.
2. Watch the plan, task graph, tool calls, traces, and approval waits in the workbench.
3. Approve high-risk actions only when the exact requested call and arguments look right.
4. Review outputs, validation, memory writes, behavior-delta candidates, and rollback evidence.
5. Let validated lessons influence future runs without allowing hidden policy writes or unreviewed self-modification.

## Documentation Map

| Start with | Use it for |
| --- | --- |
| [Implementation status](docs/IMPLEMENTATION_STATUS.md) | The detailed truth table for what is working, partial, or not done. |
| [Architecture](docs/ARCHITECTURE.md) | The local runtime and memory/control-plane split. |
| [Deployment](docs/DEPLOYMENT.md) | Published and development installs, Docker, Compose, providers, and runtime checks. |
| [Production operations](docs/PRODUCTION_OPERATIONS.md) | Health, alerts, backup/restore, upgrade/rollback, failure drills, soak testing, and release gates. |
| [Memory operations](docs/MEMORY_OPERATIONS.md) | `.mv2` backup, restore, verification, and migration. |
| [Runtime security](docs/SECURITY.md) | Local-first boundaries, auth, webhook signatures, secrets, and high-risk tools. |
| [Testing](docs/TESTING.md) | Deterministic core validation plus credential-free and live integration suites. |
| [Product roadmap](docs/PRODUCTIZATION_ROADMAP.md) | The longer-horizon hosted/team product direction, separate from the supported local profile. |
| [v0.4 live stress and release review](docs/reviews/2026-07-20-live-stress-production-readiness.md) | Live-agent, benchmark, stress, browser, package, and exact public-release evidence and residual gates. |
| [Pre-publication v0.3 review](docs/reviews/2026-07-16-production-readiness.md) | Historical candidate evidence that preceded `v0.3.0` and `v0.3.1`; the release workflow is authoritative for published artifacts. |

To participate, see [Contributing](CONTRIBUTING.md), the [Code of Conduct](CODE_OF_CONDUCT.md),
[Governance](GOVERNANCE.md), the public [Security Policy](SECURITY.md), and the
[Changelog](CHANGELOG.md).

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
- Policy-layer writes require explicit instruction, high validation, repeated evidence, config
  enablement, and exact-call owner approval. System-priority policy recall is narrower: it requires
  the dedicated `memory.policy_promote` path and a durable approval/result attestation.

For normal disaster recovery, stop Kestrel and snapshot the full agent identity together:

```bash
nest-agent backup create --backend memvid --backup-dir .nest/backups/agent
nest-agent backup verify BACKUP_ID --backup-dir .nest/backups/agent
nest-agent backup restore BACKUP_ID --yes --backend memvid --backup-dir .nest/backups/agent
```

This includes memory, SQLite state, capsules, settings, skills, and plugins. Raw Secret Broker
values are intentionally excluded; see [Memory operations](docs/MEMORY_OPERATIONS.md) for the
recovery contract and separate secret-handling requirement.

## Quick Start

One-shot local install, after the exact-tag release workflow has published `v0.4.0`:

```bash
curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.4.0/install.sh | bash
```

Once published, the release installer clones or updates Kestrel in `${KESTREL_HOME:-$HOME/.kestrel-agent}`, binds that checkout to the immutable release commit, rejects repository/ref overrides, requires Python 3.11, 3.12, or 3.13, verifies the published wheel and hash-locked dependencies against `SHA256SUMS`, installs the bundled workbench, initializes `.nest/memory/*.mv2`, verifies memory, and runs a deterministic `mock` CLI smoke check. For a safer first install, it does not start the server or open a browser unless explicitly enabled. `mock` is a zero-secret health check, not the intended operating mode. The installer does not ask for secrets or enable high-risk tools. Use the editable development install below only when working from a source checkout.

Production installs should use the immutable GitHub release installer above; it pins the source, wheel, dependency manifest, and checksum manifest to the same tag. `main` is a development source, not the published release channel.

Install and explicitly launch the localhost workbench in one command:

```bash
curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.4.0/install.sh | KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash
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
python -m pip install --require-hashes --only-binary=:all: -r config/python-build-bootstrap.txt
python -m pip install --no-build-isolation -e '.[memvid,openai,anthropic,gemini,server,mcp,keyring,dev]'
npm ci --prefix web
npm run build --prefix web
```

Fast local validation:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
make golden
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

Background orchestration is deliberately bounded. Kestrel persists a deterministic task skeleton; with `NEST_AGENT_ENABLE_SEMANTIC_ORCHESTRATION=1`, JSON-capable real providers can refine an evidence-checkable semantic plan for existing task IDs without changing dependencies, tools, risk, or approvals. Final completion and approval continuation both pass through a persisted review artifact whose criterion decisions cite concrete runtime evidence. Mock mode remains deterministic and labels unexecuted declared-tool criteria as unverified instead of claiming they passed. Dynamic provider-written DAG changes are not implemented. Semantic orchestration defaults off because it adds up to two model calls per completed run; deterministic evidence review does not add provider calls.

## Proactive Routines

Kestrel can persist a UTC one-shot or fixed-interval prompt and admit its occurrences as ordinary durable runs. Creation always produces a disabled draft; enable, update, disable, and delete operations use an expected revision so a stale owner view cannot overwrite a newer decision. Interval schedules have a 60-second minimum.

```bash
nest-agent routines create \
  --id daily-local-review \
  --name "Daily local review" \
  --prompt "Review unfinished local runs and summarize the next safe action" \
  --schedule-kind interval \
  --interval-seconds 86400 \
  --start-at 2026-07-20T09:00:00-04:00 \
  --backend memory \
  --json

nest-agent routines enable daily-local-review --expected-revision 1 --backend memory --json
NEST_AGENT_ENABLE_PROACTIVE_ROUTINES=1 nest-agent routines tick --backend memory --json
nest-agent routines history daily-local-review --backend memory --json
```

For continuous polling, require API authentication before exposing routine mutation routes, then explicitly enable the launch-controlled loop:

```bash
export NEST_AGENT_REQUIRE_API_AUTH=1
export NEST_AGENT_API_TOKEN='replace-with-local-secret'
export NEST_AGENT_ENABLE_PROACTIVE_ROUTINES=1
nest-agent server --backend memory --provider mock --host 127.0.0.1 --port 8765
```

The loop defaults to a 30-second poll, a 120-second occurrence lease, and at most three claims per tick. Polls and leases are limited to 1-3,600 seconds, and one tick may claim 1-100 occurrences. Owner-authored fixed intervals are limited to 60-31,536,000 seconds (one year), with misfire grace limited to 0-604,800 seconds (seven days). Missed intervals do not create an unbounded backlog, and a running or approval-blocked occurrence suppresses overlap. Each run persists `scheduled_routine` / `internal` provenance plus routine ID, revision, occurrence ID, scheduled time, and lease generation. Atomic initial-task creation plus scoped startup/CLI recovery closes the crash window between durable admission and execution without resuming unrelated queued user work. Routine ticks also expire overdue approvals when no UI is open. A routine being enabled never pre-approves a tool: high-risk calls still stop for their exact current arguments.

The workbench now provides revision-checked routine editing, enable/pause/delete, selected history, and **Run now**. The manual action sends a client UUID to `POST /api/routines/{routine_id}/actions/run-now`; Kestrel hashes and transactionally claims that key, replays the same admitted run after an ambiguous retry, reclaims abandoned claims, preserves the routine schedule, and still applies overlap and proactive-routine enablement gates. It does not include cron expressions, named-timezone/DST calendar rules, or automatic outbound connector delivery. Occurrence/run admission is deterministic and duplicate-fenced; Kestrel does not claim exactly-once behavior for arbitrary external side effects after a tool crosses its dispatch boundary.

Productization and support checks:

```bash
nest-agent product readiness --json
nest-agent product setup --backend memory --provider mock --json
nest-agent product provider-certification --backend memory --provider mock --json
nest-agent product support-bundle --backend memory --provider mock --output /tmp/kestrel-support.zip --json
```

`product readiness` is the static long-horizon roadmap report for the full product, including hosted/team capabilities. It is not an exact-build deployment verdict. Use `product setup --check`, live health checks, and the release gate in `docs/PRODUCTION_OPERATIONS.md` for the supported local/private profile.

Support bundles are redacted diagnostic archives. They include readiness reports, runtime metadata, git status, state-table counts, log file metadata, and a bounded redacted event-log tail; they do not include raw Secret Broker vault contents, raw environment variable values, or `.mv2` memory files.

Provider reporting deliberately separates three different claims: an adapter can be implemented,
the current machine can be configured to use it, and a particular provider/model can have
evidence-backed assurance. `product provider-certification` is a read-only, redacted
`kestrel.provider_certification.v2` matrix. Without authenticated evidence receipts it reports the
implementation and local-readiness baseline only; credentials, an installed executable, or a
configured base URL never upgrade certification. See
[Provider certification evidence](docs/TESTING.md#provider-certification-evidence) for the
`collect`, `build`, and fail-closed `check` workflow used to create an exact-subject release
artifact.

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

Every live MCP stdio command, including a manually configured server, requires explicit connect approval bound to the resolved executable and entry-artifact bytes. Kestrel launches an owner-only content-addressed snapshot; installed plugin scripts bind and snapshot the complete source tree so imported sibling drift revokes approval. Registry package runners (`npx`, `uvx`, `bunx`), mutable `python -m`, shell/proxy launchers, and interpreter eval modes such as `python -c` or `node --eval` are rejected. Secret-bearing standalone interpreter scripts, interpreter aliases, and direct shebang executables are rejected; supported installed-plugin script launchers require a verified private tree snapshot. Tools discovered dynamically from a live server always remain at least medium risk with exact-call approval, even when static manifest metadata is trusted.

Executable skills never run directly on the host. They require explicit executable-skill enablement, exact-call approval, a digest-pinned OCI image, a verified skill-tree snapshot, and canonical default-deny scopes. Declared read grants are copied with bounded, no-follow traversal into owner-private temporary snapshots before launch; the container never receives a live workspace bind. Workspace-root, `.git`, `.nest`, writable, secret, and network scopes fail closed. The Docker runner pins a verified local engine endpoint and uses a read-only root filesystem, nonroot execution, dropped capabilities, no-new-privileges, PID/CPU/memory/ulimit/tmpfs and input/output bounds, supervised timeout cleanup, and no host fallback. Instruction-only skills remain available without executing code.

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

Being listed here means that Kestrel implements an adapter; it does not mean every provider or
model has equal live or release assurance. The certification matrix records the models and profile
actually tested, evidence-backed results for each required dimension, the latest exact-scoped
receipt time, and any missing requirements.

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
Provider names are also available for `lm-studio`, `openrouter`, `deepseek`, `kimi`, `ollama`,
`ollama-cloud`, `anthropic`, `grok`, and `gemini`. LM Studio, OpenRouter, DeepSeek, Kimi, Grok,
and local Ollama use the OpenAI-compatible contract; Ollama Cloud uses Ollama's native cloud API;
Anthropic and Gemini use their native surfaces.

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

The response provider runs host `codex exec` in read-only, ephemeral mode by default and ignores
ambient Codex user configuration while retaining the user's Codex authentication. This keeps
unrelated global model and reasoning settings from overriding Kestrel's explicit provider
configuration. Because read-only mode is not same-account credential isolation, the provider fails
closed when Kestrel has raw-vault bytes, keyring secret records, or repair trust material.

The separate high-risk `codex.exec` tool does not reuse that host process or authentication. It
rejects workspace-write mode and runs only inside the configured networkless, credential-free OCI
validation image, so remote-model use is unavailable there. Write-capable code changes must use the
staged repair prepare/apply/validate/review pipeline.

## Web Workbench

Build the web assets, then start the local server:

```bash
npm run build --prefix web
nest-agent server --backend memory --provider mock --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/`.

The workbench exposes runs, live event streams, approvals, routine definitions/history/manual run-now, tool filters, MCP server health/sync/connect/disconnect/restart, manual MCP invocation, memory/context utilities, skills discovery feedback, subagent/task graph views, scheduler controls, and a Capability Center under Settings.

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
NEST_AGENT_VALIDATION_CONTAINER_IMAGE=
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
NEST_AGENT_ENABLE_SEMANTIC_ORCHESTRATION=false
NEST_AGENT_ENABLE_AUTONOMOUS_SCHEDULER=false
NEST_AGENT_ENABLE_PROACTIVE_ROUTINES=false
NEST_AGENT_ENABLE_CHANNEL_DELIVERY=false
NEST_AGENT_ENABLE_AUTO_CONSOLIDATION=false
NEST_AGENT_AUTO_CONSOLIDATION_DRY_RUN=true
NEST_AGENT_ENABLE_BEHAVIOR_DELTAS=false
NEST_AGENT_MAX_ACTIVE_DELTAS_PER_RUN=8
NEST_AGENT_REQUIRE_API_AUTH=false
```

High-risk tools need an enabled per-capability decision, every applicable master flag, and exact-call approval before execution. Capability switches cannot bypass those prerequisites. Approval cannot be disabled by configuration; it is owner-bound, single-use, bound to the requested tool call ID and exact arguments, and expires after 15 minutes by default. It is also bound to the capability revision and a digest of the current tool specification, policy gates, and parent MCP/skill resource. Changed arguments, policy, specification, parent resource, capability revision, or expiry require a new approval.

Secrets stay out of chat. The Secret Broker stores channel/MCP/tool credentials through backend API/UI flows and returns only metadata such as `secret://...` handles, configured state, validation state, timestamps, and fingerprints. No public GET route returns raw secret values; MCP `secret_env` can point to host env names or broker refs, and channel status checks use the same metadata-only boundary. Local stdio fails closed whenever the configured raw JSON/file/local vault exists, keyring metadata has secret records, or repair receipt/signing material exists, because an approved same-user process is not an OS sandbox. Secret and repair-key publication also serialize against stdio launch and first quiesce all registered local stdio sessions; unverified teardown aborts publication. The default installer includes the `keyring` extra, but keyring is an at-rest backend rather than process isolation. Use remote authenticated MCP or separate containment for hostile server code or independent runtimes. Do not point the keyring backend at a populated JSON vault: use a new metadata path, rotate/re-enter the secrets, and remove the old raw vault only after validating the replacement. A missing raw-vault path and an empty keyring metadata file are not expanded or enumerated by MCP lifecycle. The stock headless container intentionally stays on the owner-private JSON backend because installing the Python client does not create an OS keychain service.

Agent-invoked `test.run`, `lint.run`, `repair.validate`, `repair.orchestrate_validate`, and read-only `codex.exec` never execute candidate code on the host. Configure a preloaded digest-pinned validation image, for example `NEST_AGENT_VALIDATION_CONTAINER_IMAGE='registry.example/kestrel-validation@sha256:…'`, containing every requested command and project dependency. Kestrel copies the exact Git candidate into a bounded private snapshot and runs it in a networkless, secret-free, read-only, nonroot OCI boundary; there is no host fallback and Kestrel never pulls the image during a tool call. `nest-agent doctor --allow-shell --validation-container-image "$VALIDATION_IMAGE"` validates the configuration shape, while local image availability and command dependencies are checked at execution. Each isolated repair validation also records a schema-v2 attestation and rotates `.nest/repair_receipt_signing.v2.key`; legacy schema-v1 receipts and the former workspace key cannot authorize review or commit.

The validation image must be purpose-built for the repository: the small pinned Python image used by the containment fixture is not a general Kestrel test image. Build and publish the required dependencies to a trusted registry, pull the resulting immutable `name@sha256:<64 hex>` reference ahead of time, and then configure that exact reference. `codex.exec` additionally requires a Codex binary in the image, but the current boundary intentionally grants neither provider credentials nor network access; remote-model Codex delegation is therefore unavailable through this tool. The separate `codex-cli` response provider and local stdio MCP remain host-process surfaces and fail closed when raw-vault bytes, keyring secret records, or repair trust material exist.

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
make golden
python benchmarks/real_agent_learning_benchmark.py --output benchmark_results/agent_learning_gate.json
python scripts/eval_behavior_deltas.py --scenario tests/evals/behavior_deltas/policy_write_requires_approval.json --fail-on-regression
npm run test --prefix web
npm run build --prefix web
```

Credential-free release-gated integration checks:

```bash
VALIDATION_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
docker pull "$VALIDATION_IMAGE"
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_memory_system.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden --validation-container-image "$VALIDATION_IMAGE"
RUN_EXTENSION_SANDBOX_INTEGRATION=1 \
KESTREL_EXTENSION_TEST_IMAGE="$VALIDATION_IMAGE" \
python -m pytest -q tests/integration/test_extension_container_integration.py
```

Optional live-provider checks:

```bash
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memory --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memory --validation-container-image "$VALIDATION_IMAGE"
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memvid --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memvid --validation-container-image "$VALIDATION_IMAGE"
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memory --output-root /tmp/kestrel-live-learning-memory
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
```

Use `python -m pytest` for gated integration tests so fixture subprocesses inherit the same interpreter, environment, and installed extras.

## Packaging and Deployment

Common commands:

```bash
make install-dev
make validate
make docker-build
make docker-doctor
```

Operational docs:

- [Deployment](docs/DEPLOYMENT.md) covers local installs, Docker, Compose, providers, and runtime checks.
- [Memory operations](docs/MEMORY_OPERATIONS.md) covers `.mv2` backup, restore, verification, and migration.
- [Runtime security](docs/SECURITY.md) documents the local-first posture, API token gate, webhook signatures, and high-risk tool gates.
- [Release checklist](docs/RELEASE_CHECKLIST.md) lists release-candidate validation commands.

## Broader and Unsupported Product Gaps

These capabilities are outside the supported single-user, single-node profile or remain optional-surface hardening work; they do not imply hosted/team support:

- Fresh authenticated live-provider CI/release evidence across the full provider/model matrix.
- Richer provider-specific JSON/context/streaming hardening for every native provider surface.
- Hosted/team identity, distinct administrator principals, hardened sessions, workspace ownership, role-scoped capability policy, and tenant isolation.
- Real MCP SSE/streamable HTTP fixtures and soak testing.
- Quota-bounded staged extension writeback with reviewed host-side commit semantics; executable-skill workspace write scopes currently fail closed.
- Portable container-engine support, managed skill package dependencies, and richer explicitly granted network policies beyond the current digest-pinned Docker/OCI runner.
- Managed plugin dependency installation beyond the current review metadata, enable blockers, and contained executable-skill path.
- Dynamic provider-written task DAG changes and Codex-backed fan-out/merge/review across isolated worker branches or worktrees.
- Production bot identity verification and platform-specific rate-limit handling.
- Fully autonomous self-improvement with diff review, test gates, rollback, and explicit human approval.

The authoritative status page is [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md).
