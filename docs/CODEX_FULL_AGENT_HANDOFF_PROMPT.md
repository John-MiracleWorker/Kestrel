# Codex Full Agent Handoff Prompt

Last updated: 2026-05-16

You are working inside the Kestrel repository. Kestrel is a local-first, memory-native agent runtime built around Nested Learning-inspired memory layers and Memvid v2 `.mv2` files.

This is not just a RAG layer. The runtime already includes CLI chat, provider adapters, deterministic mock mode, tools, approvals, state, task capsules, a FastAPI/web control plane, managed MCP stdio sessions, skills, scheduler slices, and safe repair gates. Your job is to harden the next slice without regressing the current contract.

## Read First

Before changing code, inspect:

```text
README.md
AGENTS.md
PROJECT_MANIFEST.md
docs/IMPLEMENTATION_STATUS.md
docs/FULL_AGENT_SPEC.md
docs/RUNTIME_WIRING.md
docs/MEMVID_INTEGRATION.md
docs/TESTING.md
pyproject.toml
src/nested_memvid_agent/agent.py
src/nested_memvid_agent/run_manager.py
src/nested_memvid_agent/cli.py
src/nested_memvid_agent/server.py
src/nested_memvid_agent/backends/memvid_backend.py
src/nested_memvid_agent/tools/builtin.py
src/nested_memvid_agent/state_store.py
tests/
```

## Non-Negotiable Architecture

Kestrel is:

```text
LLM provider
+ chat/runtime loop
+ tool registry
+ approval and permission gates
+ nested .mv2 memory
+ Soul/self runtime model
+ context compiler and pseudo-context packer
+ event/state logs
+ task capsules
+ consolidation/eval harness
+ CLI/API/web control surface
```

Use one permanent Memvid v2 `.mv2` file per nested layer:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
.nest/memory/self.mv2
.nest/memory/policy.mv2
```

Run capsules live separately:

```text
.nest/runs/{run_id}/complete.mv2
```

Do not use Memvid v1 QR/video behavior. Do not build around vector databases. Do not replace `.mv2` memory with SQLite/Postgres/Chroma/FAISS/JSON. SQLite is control-plane state only.

Never call `create(path)` on an existing `.mv2` file.

## Baseline Before Work

Run at least:

```bash
python -m compileall -q src tests scripts
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "hello"
```

If the phase touches web UI or server assets, also run:

```bash
npm run test --prefix web
npm run build --prefix web
```

If the phase touches Memvid or MCP integration, run the matching gated tests:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
```

If the phase touches live provider wiring, run the gated provider harness with the relevant credentials/endpoints configured:

```bash
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
```

Use `python -m pytest` so subprocess fixtures inherit the active interpreter and installed extras.

## Current Working Surface

Treat these as implemented unless current verification proves otherwise:

- CLI chat with in-memory and Memvid backends.
- Deterministic mock provider.
- OpenAI Responses provider.
- OpenAI-compatible local provider, plus OpenRouter/Ollama aliases through the same contract.
- Anthropic Messages and Gemini provider adapters with strict tool-use/function-call normalization and stream support.
- Codex CLI response provider.
- Provider fallback on retryable failures.
- Durable graph runtime above the chat loop: `PlannerNode`, `ExecutorNode`, `ReviewerNode`, `RecoveryNode`, `MemoryPromotionNode`, and `FinalizerNode`.
- MV2 context frames and token-aware pseudo-context packing.
- Soul/self layer, self-model context frames, and CLI/API/web self-inspection surfaces.
- Task capsules and conservative learning-signal extraction.
- `memory.learn`, `memory.consolidate`, and promotion gate metadata.
- `web.search` and `web.fetch` behind `NEST_AGENT_ALLOW_WEB`, with deterministic mock backend support and public-network fetch checks.
- Exact-call approval gates for high-risk tools.
- SQLite state schema version 10, including durable trace spans, persisted run provider, and promotion outcome ledger tables.
- Replay-safe terminal run and approval decisions.
- Managed stdio MCP sessions.
- Skills with manifest validation, provenance hashes, and local runtimes.
- Alpha plugin registry/CLI with GitHub source fetch, manifest parsing, and materialization of plugin-declared skills/MCP server entries.
- API token gate and generic HMAC webhook verification.
- Opt-in autonomous scheduler.
- Branch-isolated repair primitives, diagnosis-gated validation, `repair.review`, and repair-branch commit gate.
- Docker/Compose alpha packaging.

## Current Partial Areas

The next useful hardening work should usually target one of these:

- Credentialed live provider integration runs in CI/local release validation.
- Production-grade auth, user/session isolation, and deployment boundaries.
- MCP SSE/streamable HTTP fixtures and failure-recovery soak tests.
- Container-grade skill isolation and package dependency management.
- Managed plugin dependency installation, real container isolation, executable Hermes compatibility, and broader security review beyond the current review metadata and enable blockers.
- Stronger consolidation validation loops and review UI.
- Richer self-change execution UX beyond approval-gated proposal capture and the existing repair gates.
- Fully dynamic planner/executor/reviewer plan rewriting across worker branches.
- Codex-backed worker fan-out with merge/review handling for isolated worker branches.
- Production bot identity verification and channel-specific rate-limit handling.
- Fully autonomous self-improvement with diff review, tests, rollback, and explicit human approval.

## Tool and Approval Rules

High-risk tools require capability enablement where applicable and exact-call approval before execution. Approval is tied to the tool-call ID and exact arguments.

Examples:

- `shell.run` requires shell enablement and approval.
- `file.write`, `patch.apply`, `skill.install`, and repair patch tools require file-write enablement and approval.
- `codex.exec` requires Codex CLI enablement and approval.
- `capsule.apply` requires auto-consolidation enablement, write mode, and approval.
- `self.propose_change` requires self-modification enablement and approval, and must not patch directly.
- `web.search` and `web.fetch` require web enablement and must remain read-only.
- `memory.import` requires approval and still respects policy-write gating.
- `git.commit` requires approval and never pushes.
- Repair branch commits require a fresh `repair.review` artifact tied to successful validation and the current diff hash.

Do not weaken gates to make tests pass.

## Secure Secret Handling

Kestrel must never ask users to paste secrets into chat. Raw secret values must not land in prompts, chat history, tool outputs visible to the model, trace logs, error text, `.mv2` memory, MCP manifests, or public API payloads.

The Secret Broker is the trusted backend boundary. The agent may request a secret by name and purpose, but a backend/UI flow collects, stores, validates, and injects the value outside LLM context.

The LLM may see metadata only:

- secret name
- purpose
- configured true/false
- validation status
- last validated timestamp
- `secret://...` handle
- optional non-reversible fingerprint

The LLM must never see:

- raw secret values
- full env files
- unredacted process environments
- unredacted tool errors
- unredacted logs or traces

Secrets should be treated as capabilities, not strings. Channels, MCP servers, subprocesses, and provider adapters should receive secret values only through backend runtime injection. Public surfaces should expose handles and configuration status, as in channel `env_status` and MCP `secret_env_status`.

Current local-first implementation:

- `POST /api/secrets` stores values through the backend and returns metadata only.
- `GET /api/secrets` and `GET /api/secrets/{id}` never return raw values.
- `NEST_AGENT_SECRET_STORE_PATH` points at the local Secret Broker vault.
- `NEST_AGENT_SECRET_BACKEND=json` uses the owner-only local vault; `keyring` stores raw values through the optional OS keyring backend and keeps only metadata in JSON.
- MCP `secret_env` values can point to host env names or `secret://...` refs.
- Channel env status checks consult the broker as well as host env vars.

The local file vault is a development substrate with owner-only permissions. Production deployments should keep the same broker contract but back it with OS keychain, platform secret storage, or a managed vault.

## Local-Only Self-Improvement and Git Safety

Kestrel may improve itself locally by writing memory, creating local branches, producing patches, and running tests. Kestrel must not push to upstream `main`. Remote publishing requires explicit human approval and should happen through PRs or forks, never direct main mutation.

Keep three lanes separate:

- Self-learning memory: validated lessons, workflow preferences, repair notes, failures, and procedural memories stay in local `.mv2` memory.
- Local code experiments: Kestrel may create local branches/worktrees, patch files, run validation, and show diffs on the user's machine.
- Remote publishing: pushing to GitHub, opening PRs, changing remote branches, tags, releases, or repo settings is disabled by default and requires a separate explicit approval story.

Protected branch policy:

```text
main = protected
agent direct push to main = forbidden
agent direct commit to main = forbidden
agent force push = forbidden
agent push tags = forbidden
agent remote rewrite = forbidden
agent can create local branches = allowed
agent can create patch files = allowed
agent can open PR from branch/fork = explicit approval only
```

Default config keeps remote publishing disabled:

```text
NEST_AGENT_ALLOW_GIT_COMMIT=0
NEST_AGENT_ALLOW_GIT_PUSH=0
NEST_AGENT_ALLOW_REMOTE_MUTATION=0
NEST_AGENT_GIT_WRITE_MODE=local_branch
NEST_AGENT_PROTECTED_BRANCHES=main,master,release/*
```

Do not add a generic `git.push` tool to the default registry. Safer git tools are read/status/diff, local branch creation through `git.create_local_branch`, commit through `git.commit`, and local patch export through `git.export_patch`. Shell execution must not be usable as a remote-publishing escape route; commands such as `git push`, `git tag`, `git remote set-url`, `gh repo edit`, `gh secret set`, `gh workflow enable`, `rm -rf .git`, or writes to `.git/config` must stay blocked unless a future remote-publishing mode deliberately gates them.

A local improvement candidate should be represented as reviewer artifacts under `.kestrel/improvements/` or an equivalent local queue, with at least a summary, plan, diff/patch, validation output, risk notes, and status. The user then chooses whether to keep it local, export a patch, open a PR, or discard it.

## Memory Promotion Rules

Every accepted promotion must carry:

- source evidence or record IDs
- provenance
- target layer
- confidence
- validation status
- validation score
- repeat count where relevant
- promotion/rejection reason
- context-flow and optimizer-trace metadata when using `NestedLearningKernel`

Do not write policy memory from a single ordinary event. Policy writes require explicit instruction or reviewed rule, high validation, repeat evidence, config enablement, and review or equivalent explicit configuration.

## Expected Work Style

1. Pick one coherent hardening slice.
2. Inspect the existing implementation first.
3. Keep changes scoped.
4. Add or update focused tests.
5. Run `python -m pytest -q` after the phase, plus any relevant targeted validations.
6. Update docs when behavior or commands change.
7. Report what changed, what was verified, and what remains risky.

Do not present roadmap items as done. Keep partial work marked partial.
