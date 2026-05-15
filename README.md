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
- paper-guided nested learning kernel
- consolidation and learning-signal tools
- event logging
- validation plan and golden test pipeline

The previous memory scaffold becomes the memory subsystem. This package is the full agent scaffold.

## Target architecture

```text
User / CLI / API
        ↓
NestedMV2Agent runtime
        ↓
Context compiler ← layered memory retrieval
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

## Local web agent

```bash
pip install -e '.[memvid,openai,server,mcp,dev]'
npm install --prefix web
npm run build --prefix web
nest-agent server --backend memory --provider mock --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765/` to use the local workbench. It exposes background runs, SSE timeline events, human approvals, built-in tools, MCP servers, skills, and memory search.

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

The scaffold is runnable with the in-memory backend, mock provider, local web UI, approval-gated built-in tools, MCP/skill registry surfaces, Codex CLI bridge, Memvid `.mv2` backend, and OpenAI/OpenAI-compatible provider adapters.

It is **not yet a complete Hermes/OpenClaw agent**. Native provider tool-calling, durable multi-step planning, production auth, full MCP session execution, richer skill sandboxing, and autonomous self-improvement controls still need hardening. See `docs/IMPLEMENTATION_STATUS.md` for the current truth table.

The nested learning pass now records context-flow and optimizer-trace metadata for validated memory updates via `memory.learn` and `memory.consolidate`. This is the runtime-memory analogue of the paper’s nested context-flow idea, not a claim of neural weight-level HOPE/self-modifying model training.

## Critical next Codex task

Give Codex `docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md`. That prompt tells it exactly how to harden this into a complete OpenClaw/Hermes-style agent with `.mv2` memory.
