# Kestrel

<p align="center">
  <strong>A local-first AI engineering agent that remembers what worked, learns from evidence, and keeps you in control.</strong>
</p>

<p align="center">
  <a href="https://github.com/John-MiracleWorker/Kestrel/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/John-MiracleWorker/Kestrel/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python 3.11 through 3.13" src="https://img.shields.io/badge/Python-3.11--3.13-3776AB?logo=python&logoColor=white">
  <img alt="Memvid v2" src="https://img.shields.io/badge/Memory-Memvid%20v2%20.mv2-6f42c1">
  <img alt="Local first" src="https://img.shields.io/badge/Runtime-local--first-059669">
  <img alt="Supported profile" src="https://img.shields.io/badge/Profile-single--user%20local%2Fprivate-0f766e">
  <img alt="License Apache-2.0" src="https://img.shields.io/badge/License-Apache--2.0-blue">
</p>

Kestrel is a **memory-native AI engineering agent runtime** for developers who want more than a stateless coding assistant or a thin chatbot wrapped around tools.

It combines:

- a conversational CLI and local web workbench;
- task-first Mission Control with local project profiles and repository preflight;
- a rebuildable structural repository index with freshness-bound code intelligence;
- structured, layered Memvid v2 memory;
- durable runs, task graphs, approvals, and proactive routines;
- built-in tools, MCP servers, skills, plugins, and channels;
- evidence-backed learning and reversible behavior changes;
- branch-isolated repair workflows with containerized validation; and
- support for cloud models, local model servers, and Codex CLI.

> **Kestrel's core promise:** repeated engineering work should make the agent more useful without making its behavior less understandable.

Kestrel is currently designed for **one trusted user running one local or privately networked node**. It is not yet a hosted, multi-user, or multi-tenant agent platform.

`v0.5.2` is the current stable release for that supported profile: one trusted user, one Kestrel server or worker process, and one local or privately networked node. This source tree contains tag-ready release metadata; the installer and exact-tag artifacts become available once the `v0.5.2` release workflow publishes them.

---

## Install and Open Kestrel

Once the matching `v0.5.2` tag and exact-tag assets have been published, the everyday install-and-open path is:

```bash
curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.5.2/install.sh \
  | KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash
```

That explicit opt-in installs Kestrel, starts one verified loopback-only service, and opens the local Workbench. Returning users can launch or safely reuse that same service with:

```bash
kestrel open
```

The installer installs a `kestrel` command shim in an existing safe writable directory already on `PATH` when one is available. Otherwise, it uses `~/.local/bin` and prints the exact `export PATH=...` step for the current shell. On macOS, it also creates `Kestrel.app` in `~/Applications`. Once the command directory is on `PATH`, either entry point works from any directory. `kestrel status`, `kestrel chat`, `kestrel doctor`, and `kestrel stop` provide the same small everyday surface without requiring the operator to reconstruct server arguments.

Kestrel reports its model state explicitly:

- **Demo** — `Demo uses deterministic responses; no live model connected.` No model credential is required.
- **Model not connected** — a non-mock provider is configured but has not completed a successful response in this server process.
- **Ready** — the configured non-mock provider has completed a successful response in the current server process.

`kestrel` is the everyday command. `nest-agent` remains the advanced operator and automation CLI, `nested-memvid` remains the compatibility CLI, and `nested-memvid-agent` remains the Python distribution name.

On native Windows, download the checksummed `install.ps1` release asset and run its non-mutating
doctor first:

```powershell
.\install.ps1 -Action Doctor
.\install.ps1 -Action Bootstrap -Path Auto
```

`Bootstrap` prints reviewed next-step commands; it does not enable WSL2, install Docker Desktop,
Python, Git, or Kestrel. The report distinguishes the native Python, x86_64 WSL2, and Docker
Desktop paths. The native command is explicitly labeled as a version-pinned package-index install,
not a hash-bound local-wheel install. See the
[deployment guide](docs/DEPLOYMENT.md#windows-diagnostic-and-bootstrap-plan) for checksum
verification and path requirements.

Prefer a checkout? Use the [source quick start](#quick-start-from-source). Need full runtime controls? See [advanced `nest-agent` operation](#advanced-nest-agent-operation).

---

## Why Kestrel Exists

Most agents look impressive during the first task. The harder problems arrive later:

- What exactly does the agent remember?
- Can retrieved content quietly become a higher-priority instruction?
- Does the agent learn from failure, or repeat the same broken action?
- Can a permission toggle actually be trusted?
- What changed the agent's behavior, and can that change be rolled back?
- Can the agent repair code without running untrusted changes directly on the host?
- Can scheduled or background work operate under the same safety rules as interactive work?

Kestrel treats those questions as core runtime concerns rather than optional add-ons.

It is built around **structured memory, explicit authority, durable evidence, and reviewable learning**.

---

## What Makes Kestrel Different

| Problem | Kestrel's approach |
| --- | --- |
| Agent memory becomes a flat transcript or opaque vector index | Kestrel separates working, episodic, semantic, procedural, self, and policy memory into distinct `.mv2` layers with different trust and promotion rules. |
| Retrieved text can become prompt injection with a long shelf life | Ordinary recalled memory is passed to the model as untrusted evidence, not silently promoted to system authority. |
| “Learning” means hidden prompt edits | Kestrel uses run capsules, promotion ledgers, validation evidence, typed behavior deltas, activation records, outcome tracking, and rollback. |
| Tool permissions are coarse or easy to bypass | Effective authorization combines owner configuration, master runtime gates, parent capability state, unchanged resource digests, and exact-call approval when required. |
| Agents repeat failed actions without changing strategy | Kestrel records failure episodes, recalls relevant lessons, classifies failures, and can block unchanged retries until the strategy meaningfully changes. |
| Autonomous code repair runs directly against the host workspace | Kestrel separates preparation, patching, validation, review, commit, and rollback. Agent-invoked validation runs in a digest-pinned, networkless, credential-free OCI boundary with no host fallback. |
| Background automation becomes a second, less-safe execution path | Proactive routines create ordinary durable runs and remain subject to the same capability switches and exact-call approval gates. |
| The operator cannot see what the agent is doing | The local workbench exposes runs, task graphs, traces, approvals, memory, learning records, routines, MCP sessions, plugins, skills, channels, and effective capability blockers. |

Kestrel is not trying to maximize unsupervised autonomy at any cost. It is trying to make **useful autonomy inspectable, bounded, and progressively earned**.

---

## Who Should Use Kestrel?

Kestrel is a strong fit when you:

- want a local-first agent that can retain useful project knowledge across sessions;
- use both local models and cloud providers and want a provider-flexible runtime;
- want an agent to learn from repeated engineering work without hidden behavioral drift;
- need dangerous actions to stop at a precise, reviewable approval boundary;
- care about replayable runs, deterministic tests, traces, and evidence-backed claims;
- are experimenting with MCP, skills, plugins, proactive routines, or controlled self-modification;
- want an open-source foundation for building a deeply personal engineering agent.

Kestrel is **not yet the right choice** when you need:

- hosted multi-user workspaces or tenant isolation;
- enterprise identity, roles, or administrator separation;
- a fully autonomous coding swarm that rewrites its own task graph;
- unrestricted self-modification;
- production-grade support for every MCP network transport;
- exactly-once delivery for arbitrary external side effects.

Kestrel tells the truth about those boundaries because an agent runtime should not confuse an ambitious roadmap with a supported contract.

---

## How Kestrel Works

```text
CLI / Web / API / Channels
            │
            ▼
 Run Manager · Event Bus · SQLite Control Plane
            │
            ▼
 Durable Task Graph and Agent Runtime
            │
            ├── Context Compiler
            │      └── Layered Memvid v2 memory
            │
            ├── LLM Provider
            │      └── Cloud, local, or Codex CLI
            │
            └── Tool Registry
                   ├── Built-in tools
                   ├── MCP tools
                   ├── Skills
                   └── Plugin-provided capabilities
            │
            ▼
 Tool results · approvals · traces · validation evidence
            │
            ▼
 Run capsule · promotion ledger · behavior deltas · rollback
```

Kestrel deliberately separates two kinds of state:

- **Memvid v2 `.mv2` files** hold retrieval memory.
- **SQLite** holds control-plane state such as runs, approvals, task nodes, routines, capabilities, plugins, and learning ledgers.

SQLite is not used as a substitute for Kestrel's layered memory model.

---

## Core Capabilities

| Surface | What it provides |
| --- | --- |
| Conversational agent | Interactive and one-shot chat with memory, provider selection, native or portable tool calls, and explicit commands. |
| Local workbench | Browser UI for runs, live events, task graphs, approvals, routines, memory, capabilities, MCP, skills, plugins, channels, learning, and diagnostics. |
| Durable memory | Separate `.mv2` files for working, episodic, semantic, procedural, self, and policy memory. |
| Context compilation | Token-aware retrieval, deduplication, conflict warnings, summary-first packing, and raw evidence expansion. |
| Failure-aware execution | Failure classification, recalled lessons, proof-of-work summaries, and changed-strategy retry gates. |
| Controlled learning | Run capsules, promotion decisions, validation evidence, behavior deltas, activation metrics, outcome records, and rollback. |
| Safe repair | Repair branches or worktrees, approval-gated patching, isolated validation, review artifacts, literal-tree commits, and rollback quarantine. |
| Capability control | Per-tool, MCP-server, and skill switches with configured state, effective state, revision history, and blocker explanations. |
| Proactive routines | Disabled-by-default one-shot and interval routines with revision checks, leased occurrences, overlap suppression, and manual run-now. |
| Extensibility | Built-in tools, managed MCP sessions, local skills, and a review-first GitHub plugin flow. |
| Channels | Telegram-shaped, Discord-shaped, generic webhook, and custom JSON ingress with outbound delivery disabled by default. |
| Operations | Doctor checks, setup readiness, provider certification, support bundles, backups, restores, traces, tests, and golden evals. |

---

## Quick Start From Source

The source checkout is currently the safest universal starting point.

### Requirements

- Python 3.11, 3.12, or 3.13
- Node.js and npm for the web workbench
- Git
- Docker only when enabling OCI-contained validation or executable skills

### Install

```bash
git clone https://github.com/John-MiracleWorker/Kestrel.git
cd Kestrel

python -m venv .venv

# macOS or Linux
source .venv/bin/activate

# Windows PowerShell
# .venv\Scripts\Activate.ps1

python -m pip install --require-hashes --only-binary=:all: \
  -r config/python-build-bootstrap.txt

python -m pip install --no-build-isolation \
  -e '.[memvid,openai,anthropic,gemini,server,mcp,keyring,dev]'

npm ci --prefix web
npm run build --prefix web
```

### Open Kestrel

```bash
kestrel open
```

This initializes the local runtime as needed, starts it in deterministic Demo mode, and opens the Workbench. Run `kestrel chat "hello"` for the same authoritative server-backed conversation from the terminal.

---

## Connect a Real Model

### Codex CLI

Kestrel can use the locally authenticated Codex CLI as its response provider:

```bash
nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider codex-cli \
  --model YOUR_MODEL \
  --message "Inspect this repository and propose the safest next improvement."
```

The Codex CLI response provider is separate from Kestrel's high-risk `codex.exec` tool.

### OpenAI

```bash
export OPENAI_API_KEY="..."

nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider openai \
  --model YOUR_MODEL \
  --message "Help me continue this build."
```

### Local OpenAI-Compatible Server

This works with compatible local servers such as LM Studio:

```bash
nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider openai-compatible \
  --base-url http://127.0.0.1:1234/v1 \
  --model local-model \
  --message "Review the current architecture."
```

Kestrel also includes provider surfaces or aliases for Anthropic, Gemini, OpenRouter, DeepSeek, Kimi, Ollama, Ollama Cloud, Grok, and LM Studio.

Provider implementation, local readiness, and evidence-backed assurance are tracked separately. Use the certification report rather than assuming every provider and model has identical coverage:

```bash
nest-agent product provider-certification \
  --backend memory \
  --provider mock \
  --json
```

---

## Advanced `nest-agent` Operation

The `kestrel` facade is the recommended launch path. Use `nest-agent` when you need to select memory, provider, model, or server options directly.

### Initialize Memory and Verify the Runtime

```bash
nest-agent init --backend memvid --memory-dir .nest/memory

nest-agent doctor \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider mock

nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider mock \
  --message "hello"
```

The `mock` provider is deterministic and requires no credentials. It is intended for health checks, tests, and evaluation—not normal engineering work.

### Start the Server Directly

```bash
nest-agent server \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider mock \
  --model mock \
  --host 127.0.0.1 \
  --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765).

---

## Advanced Installer Controls

The repository contains `v0.5.2` release metadata, but release commands should only be used after the matching Git tag and exact-tag artifacts have been published.

The installer is conservative by default: it does not start the server or open a browser unless explicitly enabled.

```bash
curl -fsSL \
  https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.5.2/install.sh \
  | bash
```

To explicitly start the local workbench and open a browser during installation, pass the opt-in flags (`KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash install.sh` when running a downloaded copy):

```bash
curl -fsSL \
  https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.5.2/install.sh \
  | KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash
```

---

## Memory Is a Runtime, Not a Feature Checkbox

Kestrel stores each permanent memory layer in its own Memvid v2 file:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
.nest/memory/self.mv2
.nest/memory/policy.mv2
```

| Layer | Purpose |
| --- | --- |
| Working | Active task state, recent observations, and current-session continuity |
| Episodic | Events, decisions, failures, outcomes, and run summaries |
| Semantic | Validated facts, stable preferences, and durable project knowledge |
| Procedural | Reusable workflows, repair recipes, and failure playbooks |
| Self | Identity, capability awareness, workflow preferences, and self-change records |
| Policy | Rare, explicit behavior or safety constraints with the strongest promotion gates |

Each run can also produce a separate evidence capsule:

```text
.nest/runs/{run_id}/complete.mv2
```

A run capsule is not automatically permanent memory. It is evidence that can be reviewed and considered for consolidation.

### A Deliberate Trust Boundary

Ordinary recalled memory—including content derived from tools, files, web pages, and channels—is treated as **untrusted evidence** when returned to the model.

It does not become a system-level instruction simply because it was stored and retrieved.

Policy memory has a separate, narrow promotion path requiring structured evidence, explicit enablement, exact-call approval, and durable attestation.

---

## Learning Without Hidden Behavioral Drift

Kestrel does not equate “self-improving” with silently rewriting its system prompt.

Its learning path is explicit:

1. A run produces observations, outcomes, validation results, and a `complete.mv2` capsule.
2. Kestrel records promotion decisions and the evidence behind them.
3. Repeated validated knowledge can move into slower, more trusted memory layers.
4. Candidate behavior changes become typed **behavior deltas**.
5. A mutation gate decides whether a delta may be staged or activated.
6. Active deltas are compiled only when relevant.
7. Activations and later outcomes are recorded.
8. A delta can be reviewed, rejected, or rolled back.

Low-risk auto-activation exists behind an explicit opt-in flag. Policy and high-risk behavior remain separately gated.

This is runtime-level, evidence-backed adaptation—not neural weight training and not unrestricted self-modification.

---

## Permissions That Mean What They Say

Kestrel separates an owner's desired setting from what the runtime will actually authorize.

A capability is effectively enabled only when all applicable conditions pass:

```text
owner capability switch
    ∧ runtime master flag
    ∧ launch allowlist
    ∧ enabled parent skill, MCP server, or plugin
    ∧ unchanged tool and resource digests
    ∧ exact-call approval when required
```

High-risk approvals are:

- bound to one tool call;
- bound to the exact arguments;
- bound to the owner and current capability revision;
- bound to the current tool, policy, and parent-resource digests;
- single-use;
- time-limited; and
- revalidated immediately before execution.

Changing the arguments, policy, capability revision, tool definition, skill, plugin, or MCP server invalidates the approval.

Disabling a capability blocks future invocation attempts, including attempts coming from stale registries. It does not pretend it can magically undo an arbitrary subprocess that already crossed the dispatch boundary.

---

## Failure-Aware Agent Behavior

Kestrel's default agentic cycle is designed to make failure useful.

When an action fails, Kestrel can:

- classify the failure;
- persist a structured failure episode;
- retrieve similar procedural or episodic lessons;
- include relevant lessons before the next planning attempt;
- reject duplicate successful tool calls;
- block unchanged failed retries; and
- require a meaningfully changed strategy before trying again.

Runs also produce a structured proof-of-work summary so success is not reduced to “the model said it finished.”

---

## Safe Repair Pipeline

Kestrel separates code modification into reviewable stages:

```text
inspect
  → prepare isolated branch or worktree
  → apply approved patch
  → validate in isolated OCI snapshot
  → create review artifact
  → commit reviewed tree
  → optionally export patch
```

Agent-invoked test, lint, repair validation, and read-only `codex.exec` do not execute candidate code directly on the host.

When enabled, they require a preloaded, digest-pinned validation image and run with:

- no network;
- no provider credentials;
- no host home;
- no live `.git` or `.nest`;
- a read-only candidate snapshot;
- non-root execution;
- dropped capabilities;
- resource and timeout bounds; and
- no host fallback.

A repair commit requires a current review artifact tied to the successful validation result and current diff. Committing does not imply pushing, and protected branches are refused by default.

Mission Control presents the bounded validation receipt, review summary, risk notes, rollback state, and redacted diff preview together. Patch export is not a snapshot of an arbitrary live `git diff`: it requires the signed review ID and exact candidate digest, revalidates the review, and renders the complete reviewed staged, unstaged, deleted, untracked, and binary candidate into a local `.kestrel/improvements` artifact.

Contained browser proof is separately default-off. It requires
`NEST_AGENT_ALLOW_BROWSER_VALIDATION=true`, a preloaded digest-pinned image
containing `/opt/kestrel/browser-validate`, and exact-call approval for
`browser.validate`; the API route enters that same approval path and has no
direct-execution shortcut.

---

## Proactive Routines Without a Safety Back Door

Kestrel can persist one-shot, fixed-interval, or five-field cron routines in a
named IANA timezone.

Routines:

- are created disabled;
- use revision-checked owner mutations;
- create ordinary durable runs;
- carry internal provenance rather than impersonating a user turn;
- use leased occurrences and overlap suppression;
- bound how much work can be claimed per tick;
- preserve exact-call approvals for dangerous tools; and
- expose history and manual run-now controls in the workbench;
- bind optional channel delivery to a durable destination-specific idempotency
  key and receipt; and
- leave ambiguous delivery in an explicit `uncertain` state until the owner
  reconciles it.

UTC scanning handles daylight-saving gaps and repeated local instants
deterministically. Kestrel does not claim that an arbitrary third-party channel
provides exactly-once effects.

---

## Local Operator Workbench

The FastAPI-based workbench provides a single place to inspect and control the runtime:

- task-first projects, objective templates, preflight, editable plans, and mission timelines;
- repository index freshness, intent-aware structural code evidence, and navigation benchmark state;
- graph amendments, candidate comparison, browser evidence, and GitHub change requests;
- outcome analytics and private benchmark replay;
- runs and live event streams;
- plans, task nodes, subagents, and scheduler state;
- pending and historical approvals;
- memory, context, lessons, and failure episodes;
- Soul and self-model views;
- behavior deltas and learning metrics;
- proactive routine editing, history, and manual launch;
- tool inventory and invocation;
- configured versus effective capability state;
- MCP lifecycle and manual invocation;
- skills and plugin review;
- channel configuration;
- provider and setup readiness;
- traces, diagnostics, and support bundles.

The workbench binds to localhost by default. API authentication can be explicitly required before exposing it beyond a trusted local environment.

---

## Providers and Extensions

### Provider Surfaces

| Provider surface | Notes |
| --- | --- |
| `mock` | Deterministic, credential-free testing and golden evals |
| `openai` | OpenAI Responses adapter |
| `openai-compatible` | Local or remote Chat Completions-compatible endpoints |
| `anthropic` | Native Anthropic Messages adapter |
| `gemini` | Native Gemini adapter |
| `codex-cli` | Uses local Codex authentication as the response provider |
| `lm-studio`, `openrouter`, `deepseek`, `kimi`, `ollama`, `grok` | Provider-specific aliases built on compatible contracts |
| `ollama-cloud` | Ollama's native cloud API |

Adapter availability does not mean every model has equal live validation or release assurance.

### MCP

Kestrel supports managed MCP sessions and adapts discovered MCP tools into the same tool registry and approval model as built-in tools.

Managed stdio sessions are the most hardened path today. SSE and streamable HTTP support still need broader real-transport fixtures and soak testing.

### Skills

Instruction-only skills can provide reusable operating guidance.

Executable skills require explicit enablement, exact-call approval, verified content, a digest-pinned OCI image, default-deny scopes, bounded copied inputs, and contained execution. Host Python or shell execution is intentionally rejected.

### Plugins

Kestrel includes an experimental, review-first GitHub plugin flow with:

- provenance inspection;
- manifest validation;
- risk and dependency reporting;
- enable blockers;
- local skill and MCP materialization; and
- support for Kestrel manifests plus a limited subset of Hermes-style manifests.

The plugin registry is not yet a general-purpose dependency manager.

### Channels

Kestrel can normalize:

- Telegram Bot API updates;
- Discord message or interaction-shaped payloads;
- generic webhooks; and
- custom JSON payloads.

Outbound delivery is disabled by default. External channel hardening remains intentionally narrower than the local runtime.

---

## Conservative by Default

Kestrel starts with dangerous behavior disabled.

By default, it does **not** grant the agent unrestricted access to:

- shell execution;
- arbitrary file writes;
- policy-memory writes;
- code commits or pushes;
- remote repository mutation;
- plugin installation;
- executable skills;
- unrestricted MCP network endpoints;
- web access;
- channel delivery;
- proactive routines;
- behavior-delta activation; or
- self-modification requests.

Enabling a feature does not automatically approve a dangerous call. Capability switches, runtime gates, and exact-call approvals remain separate controls.

Secrets are handled through a metadata-only Secret Broker boundary and are not intended to be pasted into chat.

---

## Current Maturity

Kestrel is a **production candidate for its supported local/private profile**:

- one trusted user;
- one Kestrel server or worker process;
- one local or privately networked node;
- local-first memory and control-plane state;
- conservative defaults and explicit operator approval.

Important unfinished areas include:

- hosted and multi-user authorization;
- tenant isolation and role-scoped permissions;
- learned Adaptive Flock production routing with complete usage/cost attribution;
- fully dynamic provider-written task graphs;
- autonomous patch synthesis and worker-branch fan-out/merge;
- contained browser/visual project validation;
- project-aware GitHub PR/CI feedback;
- production-soaked MCP network transports;
- managed plugin dependency installation;
- broader portable container-engine support;
- exactly-once external delivery semantics; and
- unrestricted autonomous self-improvement.

See the implementation status page for the literal working, partial, and not-yet-implemented breakdown.

---

## Validation

Fast deterministic validation:

```bash
python -m compileall -q src tests scripts
python -m ruff check scripts src tests
python -m mypy src
python -m pytest -q
make golden
python benchmarks/real_agent_learning_benchmark.py --output benchmark_results/agent_learning_gate.json
python scripts/eval_behavior_deltas.py --scenario tests/evals/behavior_deltas/policy_write_requires_approval.json --fail-on-regression
npm run test --prefix web
npm run build --prefix web
```

Useful product checks:

```bash
nest-agent product readiness --json
nest-agent product setup --backend memory --provider mock --json
nest-agent product provider-certification --backend memory --provider mock --json
nest-agent product support-bundle \
  --backend memory \
  --provider mock \
  --output ./kestrel-support.zip \
  --json
```

Live provider, Memvid, MCP, and containment integration suites are opt-in and documented separately.

---

## Documentation

| Document | Purpose |
| --- | --- |
| [Implementation Status](docs/IMPLEMENTATION_STATUS.md) | Literal inventory of what works, what is partial, and what is not done |
| [Architecture](docs/ARCHITECTURE.md) | Runtime shape, memory model, control plane, and trust boundaries |
| [Deployment](docs/DEPLOYMENT.md) | Source installs, release installs, Docker, providers, and runtime setup |
| [Production Operations](docs/PRODUCTION_OPERATIONS.md) | Health, backup, restore, upgrade, rollback, drills, and release gates |
| [Memory Operations](docs/MEMORY_OPERATIONS.md) | Memvid verification, migration, backup, and recovery |
| [Runtime Security](docs/SECURITY.md) | Auth, secrets, approvals, webhooks, tools, MCP, and execution boundaries |
| [Testing](docs/TESTING.md) | Deterministic, integration, provider, and certification test paths |
| [Controlled Self-Modification](docs/CONTROLLED_SELF_MODIFICATION.md) | Behavior deltas, mutation gates, replay, review, and rollback |
| [Productization Roadmap](docs/PRODUCTIZATION_ROADMAP.md) | Longer-horizon hosted and team platform direction |
| [Changelog](CHANGELOG.md) | Version history and security changes |

---

## Contributing

Contributions are welcome.

Before opening a pull request, read:

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
- [GOVERNANCE.md](GOVERNANCE.md)
- [SECURITY.md](SECURITY.md)

Good contribution areas include provider hardening, MCP transport fixtures, evaluation scenarios, workbench UX, documentation, plugin compatibility, containment portability, and adversarial safety testing.

---

## License

Kestrel is licensed under the [Apache License 2.0](LICENSE).
