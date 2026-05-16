# Nested MV2 Agent

A buildable scaffold for a full conversational agent runtime designed around a Nested Learning-inspired memory architecture and Memvid `.mv2` memory files.

This is **not just a RAG backend**. It includes the pieces required for a real agent:

- interactive CLI chat runtime
- provider abstraction for LLMs
- OpenAI Responses API adapter scaffold
- OpenAI-compatible chat completions adapter for local/model-server endpoints
- deterministic mock LLM for tests
- tool registry and built-in tools
- safety gates for shell and file writes
- layered nested memory system
- Memvid `.mv2` backend adapter
- in-memory backend for tests and Codex iteration
- context compiler
- token-aware MV2 pseudo-context packer
- run-scoped task capsules
- multi-channel ingress for Telegram, Discord, and generic webhooks
- paper-guided nested learning kernel
- consolidation and learning-signal tools
- event logging
- validation plan and golden test pipeline

The previous memory scaffold becomes the memory subsystem. This package is the full agent scaffold.

## Target architecture

```text
User / CLI / API / Channels
        ↓
NestedMV2Agent runtime
        ↓
Context compiler ← layered memory retrieval
        ↓
MV2 pseudo-context packer ← summary-first retrieval + on-demand raw expansion
        ↓
LLM provider
        ↓
Tool router / executor
        ↓
Tool results + observations
        ↓
Working memory + event log
        ↓
Nested learning kernel / consolidation pipeline
        ↓
Episodic → Semantic → Procedural → Policy .mv2 layers
```

## Memory files

By default, the agent creates one Memvid file per layer:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
.nest/memory/policy.mv2
```

One file per layer is intentional. The layers have different update cadences, trust thresholds, search strategies, and promotion rules.

## Pseudo-context windows

Kestrel does not remove LLM context limits and does not claim neural weight-level learning. It builds an on-demand pseudo-context window from `.mv2` memory by retrieving compact summaries first, expanding raw evidence only when needed, deduplicating repeated content, warning on conflicts, and packing selected material under a token budget.

The packer emits sections for policy constraints, procedures, stable facts, episodic/task state, working memory, conflict warnings, evidence pointers, telemetry, and the next-step instruction. Summaries carry parent/child pointers back to raw chunks so exact evidence can be expanded with `context.expand` instead of dumping full transcripts into the prompt.

See `docs/MV2_CONTEXT_PACKING.md` for the frame and packing contract.

## Task capsules

Completed runs can write a temporary capsule at:

```text
.nest/runs/{run_id}/complete.mv2
```

`complete.mv2` is a run artifact, not a sixth permanent layer. It captures the objective, selected context, tool calls/results, tests, errors, final response, unresolved questions, reusable lessons, and candidate facts/procedures/corrections/policy items. `capsule.summarize` is preview-only. `capsule.apply` can write accepted signals only when auto-consolidation is explicitly enabled and the high-risk approval gate is satisfied. Policy memory still requires explicit instruction, strong validation, repeat evidence, config enablement, and human review or equivalent explicit configuration.

See `docs/TASK_CAPSULES.md` for the capsule lifecycle.

## Quick start with in-memory backend

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
pytest -q
nest-agent chat --backend memory --provider mock --message "hello"
```

## Quick start with Memvid

```bash
pip install -e '.[memvid,openai]'
export OPENAI_API_KEY=...
nest-agent init --backend memvid --memory-dir .nest/memory
nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider openai \
  --model gpt-5.5 \
  --message "What do you remember about this project?"
```

If your model name differs, pass the model available in your account. The scaffold keeps provider wiring isolated so Codex can swap in your preferred model/provider without touching the runtime.

## OpenAI-compatible local providers

For local servers that expose an OpenAI-compatible chat completions API:

```bash
nest-agent chat \
  --backend memory \
  --provider openai-compatible \
  --base-url http://127.0.0.1:1234/v1 \
  --model local-model \
  --message "hello"
```

Use `--api-key-env NAME` when the endpoint needs a non-default API key environment variable. The runtime also accepts `--stream`, which streams token events through the CLI and web run event bus; providers without native streaming use the compatibility stream wrapper.

## Codex CLI as response provider

Kestrel can use the local Codex CLI as its normal response engine while keeping Kestrel in charge of memory, approvals, tools, MCP, and file writes:

```bash
nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider codex-cli \
  --model gpt-5.5 \
  --timeout-seconds 600 \
  --message "Help me continue this build"
```

The provider runs `codex exec` with `--sandbox read-only`, `--ephemeral`, and `--output-last-message` by default. If Codex needs Kestrel to run a tool, it should return the existing Kestrel JSON tool envelope. Keep write-capable Codex work behind the separate approval-gated `codex.exec` tool.

## Multi-channel gateway

Kestrel can normalize external channel payloads into the same agent turn loop used by CLI/API chat. Telegram Bot API updates, Discord message/interaction-shaped payloads, and generic webhooks are supported without adding external dependencies.

Dry-run local handling:

```bash
nest-agent channel \
  --backend memory \
  telegram \
  --payload-file telegram-update.json
```

Server routes:

```text
GET  /api/channels
POST /api/channels/ingest
POST /api/channels/{provider}/webhook
```

Outbound delivery is disabled by default. To send real replies, configure `.nest/config/channels.json` from `config/channels.example.json`, set the provider secret environment variable, set that channel's `send_enabled` or `auto_reply`, and start the runtime with `--enable-channel-delivery`.

## Local web agent

```bash
pip install -e '.[memvid,openai,server,mcp,dev]'
npm install --prefix web
npm run build --prefix web
nest-agent server --backend memory --provider mock --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/` to use the local workbench. It exposes background runs, SSE timeline events, human approvals, built-in tools, MCP server health/sync/connect/disconnect/restart controls, manual MCP JSON invocation, local subagent runs, skills, and memory search.

## Packaging and deployment

Operational docs:

- `docs/DEPLOYMENT.md` covers fresh installs, Docker, Compose, provider setup, local model setup, and runtime checks.
- `docs/MEMORY_OPERATIONS.md` covers `.mv2` backup, restore, verification, and migration.
- `docs/SECURITY.md` documents the default local-only security posture and high-risk tool gates.
- `docs/RELEASE_CHECKLIST.md` lists the alpha release validation commands.

Common local packaging commands:

```bash
make install-dev
make validate
make docker-build
make docker-doctor
```

## Codex CLI connection

The runtime exposes the local Codex CLI as a high-risk built-in tool named `codex.exec`.

Example tool arguments:

```json
{
  "prompt": "Review the current repository and summarize the riskiest files.",
  "sandbox": "read-only",
  "model": "gpt-5.5",
  "timeout": 600
}
```

By default, `codex.exec` requires the web approval flow before it runs. To allow it without per-call approval in a trusted local session:

```bash
nest-agent server --backend memory --provider openai --allow-codex-cli
```

The alternate MCP route is also available through the MCP server registry by configuring a stdio server with command `codex` and args `["mcp-server"]`.

## Current build status

The scaffold is runnable with the in-memory backend, mock provider, local web UI, approval-gated built-in tools, managed MCP stdio sessions, MCP/skill registry surfaces, Codex CLI bridge, Memvid `.mv2` backend, OpenAI/OpenAI-compatible provider adapters, MV2 context packing, run-scoped task capsules, and dry-run multi-channel ingress.

It is **not yet a complete Hermes/OpenClaw agent**. Native provider tool-calling, durable multi-step planning, production auth, MCP SSE/streamable HTTP soak testing, richer skill sandboxing, and autonomous self-improvement controls still need hardening. See `docs/IMPLEMENTATION_STATUS.md` for the current truth table.

The nested learning pass records context-flow and optimizer-trace metadata for validated memory updates via `memory.learn`, `memory.consolidate`, and capsule summaries. The MCP/subagent pass adds durable MCP health metadata, normalized tool lifecycle events, task graph records, and in-process planner/worker/reviewer subagents. This is still the runtime-memory analogue of the paper’s nested context-flow idea, not a claim of neural weight-level HOPE/self-modifying model training.

## Critical next Codex task

Give Codex `docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md`. That prompt tells it exactly how to harden this into a complete OpenClaw/Hermes-style agent with `.mv2` memory.
