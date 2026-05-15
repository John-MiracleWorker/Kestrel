# Full Agent Specification

## Product goal

Build a complete local-first AI agent similar in feel to OpenClaw or Hermes, but designed around a Nested Learning memory model and Memvid `.mv2` files instead of a traditional vector database.

The finished agent must support:

1. Talking to the agent from a CLI.
2. Persistent `.mv2` memory across sessions.
3. Tool use with controlled permissions.
4. Context compilation from nested memory layers.
5. Learning from tool results, failures, decisions, and user corrections.
6. Promotion of memories from fast/noisy layers into slow/trusted layers.
7. Testable behavior with a deterministic mock LLM and in-memory backend.
8. Memvid integration tests once `memvid-sdk` is installed.
9. Optional FastAPI/TUI/web UI after CLI is stable.

## System boundary

The LLM is not the whole agent. The agent is the runtime organism:

```text
agent = LLM provider + tool router + memory system + context compiler + evaluator + consolidation pipeline + UI/API
```

The `.mv2` memory system replaces the normal “dump the full conversation into context” pattern. The LLM still receives a prompt, but that prompt is a compact cognitive state assembled from nested memory.

## Core components

### 1. Agent runtime

Class: `NestedMV2Agent`

Responsibilities:

- receive user input
- write current turn into working memory
- compile relevant nested context
- call LLM provider
- parse tool calls
- execute tools through registry
- write tool results to working memory
- continue tool loop until final answer
- write turn summary to episodic memory
- seal memory layers
- log events

### 2. LLM provider layer

Interfaces:

- `LLMProvider.generate(messages, tools) -> LLMResponse`
- `MockLLMProvider` for tests
- `OpenAIResponsesProvider` for chat-capable production path

Required next hardening:

- native function/tool calling support
- streaming output
- structured output mode
- retry handling
- token accounting
- provider errors mapped to runtime errors

### 3. Tool system

Current built-ins:

- `memory.search`
- `memory.write`
- `file.list`
- `file.read`
- `file.write`
- `shell.run`

Required next tools:

- `patch.apply`
- `test.run`
- `repo.search`
- `repo.map`
- `git.status`
- `git.diff`
- `git.commit` gated by approval
- `web.search` optional
- `memvid.verify`
- `memvid.stats`
- `memory.consolidate`

### 4. Nested memory layers

| Layer | File | Purpose | Write threshold | Promotion behavior |
|---|---|---|---:|---|
| Working | `working.mv2` | current task state, observations, tool results | low | expires quickly; candidates move to episodic |
| Episodic | `episodic.mv2` | session events, failures, decisions, summaries | medium | repeated/validated facts move upward |
| Semantic | `semantic.mv2` | stable facts about projects/users/domains | high | stable factual substrate |
| Procedural | `procedural.mv2` | reusable skills and repair recipes | very high | only after repeated success |
| Policy | `policy.mv2` | slow behavior and safety rules | extreme | manual/strong validation only |

### 5. Context compiler

The compiler must render a prompt with:

- objective
- relevant working memory
- relevant episodic memory
- relevant semantic memory
- relevant procedural memory
- relevant policy memory
- confidence and evidence notes
- conflict warnings
- next-step instruction

It must not blindly stuff raw memory. It ranks by layer, relevancy, confidence, importance, recency, and validation status.

### 6. Consolidation pipeline

The consolidation system is the heart of the Nested Learning design.

Input sources:

- working memory
- tool results
- test outcomes
- user corrections
- repeated patterns
- final turn summaries

Promotion rules:

- Working → Episodic: meaningful event, failure, decision, or summary.
- Episodic → Semantic: repeated or externally verified factual claim.
- Episodic → Procedural: repeated successful workflow.
- Semantic/Procedural → Policy: rare, explicit, high-confidence rule.

Every promoted record must include:

- source layer and source record IDs
- evidence refs
- confidence
- validation method
- promotion reason
- timestamp

### 7. Event log

The current scaffold includes JSONL event logging. This is not a database. It is an audit trail. Codex should optionally mirror session events into `episodic.mv2` and keep JSONL for debugging/crash analysis.

### 8. UI/API

Minimum accepted UI:

```bash
nest-agent chat --backend memvid --provider openai --model <model>
```

Optional after MVP:

- FastAPI `/chat`
- Server-sent events streaming
- Textual/Rich TUI
- Web chat frontend

## Acceptance criteria

The agent is “built” when this works:

```bash
pip install -e '.[memvid,openai,dev]'
pytest -q
RUN_MEMVID_INTEGRATION=1 pytest -q tests/integration
nest-agent init --backend memvid --memory-dir .nest/memory
nest-agent chat --backend memvid --provider openai --model <model>
```

And the agent can:

- answer normal chat messages
- search memory
- remember a user correction across sessions
- read project files safely
- refuse shell/file writes unless enabled
- log tool results
- compile nested context
- promote a validated memory through at least one consolidation path
- verify `.mv2` files
