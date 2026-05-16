# Kestrel Production-Ready Agent Build Prompt for Codex

Generated: 2026-05-16  
Repository: `John-MiracleWorker/Kestrel`  
Target file location: `docs/CODEX_PRODUCTION_READY_AGENT_PROMPT.md`

## How to use this document

Hand this entire document to Codex from the root of the Kestrel repository.

This is a production-hardening mission, not a cosmetic cleanup. The goal is to evolve Kestrel from a scaffold into a serious local-first software-engineering agent comparable in operational seriousness to Hermes or OpenClaw, while preserving Kestrel's unique identity: a Nested Learning-inspired memory-native agent runtime using Memvid v2 `.mv2` files as the primary memory substrate.

Codex should work in small, tested, reviewable increments. Prefer boring reliability over flashy demos.

---

# Codex Mission

You are working inside the `John-MiracleWorker/Kestrel` repository.

Your mission is to transform Kestrel into a production-ready local-first agent platform.

Kestrel must become:

> A local-first, memory-native engineering agent that can safely work on codebases, use tools, learn from repeated outcomes, and explain what it remembers and why.

This is not just a chatbot.  
This is not just a RAG backend.  
This is not a simple CLI wrapper around an LLM.  
This is not a toy shell runner with a prompt stapled to it.

The production target is:

```text
LLM provider
+ chat/runtime loop
+ tool registry
+ permission and safety gates
+ nested memory layers
+ context compiler
+ event log
+ consolidation pipeline
+ eval harness
+ CLI/API/web control surface
+ packaging/deployment system
```

Kestrel's key differentiator is:

```text
Nested memory
controlled consolidation
evidence-backed learning
procedure formation from repeated success
honest failure tracking
safe local codebase maintenance
```

---

# Non-Negotiable Design Rules

1. Preserve the Nested Learning memory architecture.
2. Use Memvid v2 `.mv2` files as the primary persistent memory backend.
3. Do not replace `.mv2` memory with Chroma, SQLite, Postgres, FAISS, or a traditional vector DB.
4. JSONL event logs are allowed as audit/debug logs only, not as the primary memory database.
5. Keep the agent local-first by default.
6. Default to safe/read-only behavior unless the user explicitly enables higher-risk capabilities.
7. Do not let a single random event become semantic, procedural, or policy memory.
8. Do not run destructive tools without explicit gating or approval.
9. Do not dump entire raw transcripts into context. Compile relevant cognitive state.
10. Do not start with shiny UI work before the core runtime, memory, tools, and tests are solid.
11. Do not fake test results.
12. Do not claim a feature works unless it is implemented and tested.
13. Do not loosen security to make tests easier.
14. Do not silently swallow errors.

---

# Current Repo Orientation

Before changing code, inspect the repository structure and read the important project files.

Read these files if present:

```text
README.md
PROJECT_MANIFEST.md
AGENTS.md
docs/FULL_AGENT_SPEC.md
docs/RUNTIME_WIRING.md
docs/IMPLEMENTATION_PIPELINE.md
docs/TEST_MATRIX.md
docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md
pyproject.toml
src/nested_memvid_agent/agent.py
src/nested_memvid_agent/cli.py
src/nested_memvid_agent/server.py
src/nested_memvid_agent/app_factory.py
src/nested_memvid_agent/backends/memvid_backend.py
src/nested_memvid_agent/context_compiler.py
src/nested_memvid_agent/consolidation.py
src/nested_memvid_agent/tools/
tests/
```

Then run the baseline:

```bash
python -m compileall -q src tests
pytest -q
nest-agent chat --backend memory --provider mock --message "hello"
```

Do not proceed to new features until the baseline passes or the current failure is understood and fixed.

---

# Codex-Specific Operating Expectations

Codex should treat this like real software engineering work.

When modifying files:

1. Inspect existing code first.
2. Respect any `AGENTS.md` files and their scopes.
3. Make coherent, incremental changes.
4. Add or update tests with each functional change.
5. Run relevant tests after each phase.
6. Fix failures instead of explaining them away.
7. Keep a clean worktree at the end.
8. Summarize changed files, test results, and remaining risks.
9. Commit changes if Codex is configured to commit.
10. Never leave the project in a broken state intentionally.

If the task is too large to complete in one pass, implement the highest-leverage stable slice first and clearly document what remains.

---

# Target Memory Layout

Use one `.mv2` file per memory layer:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
.nest/memory/policy.mv2
```

Layer definitions:

```text
Working
- current task state
- observations
- recent tool results
- noisy short-term facts
- temporary scratch memory

Episodic
- session summaries
- decisions
- failures
- user corrections
- important events

Semantic
- stable facts about user, projects, repos, environment, preferences
- facts should be repeated, verified, or user-confirmed

Procedural
- reusable workflows
- repair recipes
- coding/debugging procedures
- formed only after repeated verified success

Policy
- slow-changing safety rules
- user-approved operating principles
- hard constraints
- must be very difficult to write
```

Policy memory must require explicit permission or a very strong validation path.

---

# Phase 1: Production Runtime Hardening

The runtime must become dependable.

The agent loop should support:

```text
persistent session IDs
run IDs
resumable/cancellable runs where practical
deterministic mock mode for tests
max tool round enforcement
tool timeout enforcement
provider timeout enforcement
structured runtime errors
graceful provider failure handling
graceful malformed tool-call handling
event logging for every major lifecycle event
memory writes for user input, tool result, failure, and final summary
crash-safe state updates where practical
context budget enforcement
useful stop reasons
clear final reporting
```

Required event types:

```text
turn.start
context.compile
llm.request
llm.response
llm.error
tool.request
tool.execute
tool.result
tool.error
approval.required
approval.resolved
memory.write
memory.promote
turn.end
run.cancelled
runtime.error
```

The runtime should never silently fail. If something fails, it should be visible in:

```text
run result
event log
tool trace
memory trace when relevant
test output
```

Acceptance tests:

```text
one-turn mock chat works
tool loop works
max tool rounds stop correctly
malformed tool call does not crash runtime
tool failure is logged and written to working memory
turn summary is written to episodic memory
context budget is enforced
runtime returns useful stop reason
```

---

# Phase 2: Provider Layer Hardening

The LLM provider layer must support real production use while staying testable.

Current or expected providers:

```text
MockLLMProvider
OpenAIResponsesProvider
OpenAI-compatible provider
local provider for LM Studio/Ollama-style endpoints
```

Implement or harden:

```text
native tool/function calling
JSON-envelope fallback parsing
streaming response support
timeout configuration
retry/backoff for transient failures
structured provider error mapping
token accounting where available
model name passed cleanly from CLI/config
capability detection where practical
deterministic mocked provider tests
```

Provider targets to make easy to add:

```text
OpenAI
OpenRouter
LM Studio
Ollama
Anthropic-compatible APIs
local OpenAI-compatible endpoints
```

Rules:

```text
Do not require real API keys for unit tests.
Real provider tests must be gated by environment variables.
Mock provider behavior must stay deterministic.
```

Suggested gated tests:

```bash
RUN_OPENAI_INTEGRATION=1 pytest -q tests/integration
RUN_LOCAL_LLM_INTEGRATION=1 pytest -q tests/integration
```

Acceptance tests:

```text
mock provider returns deterministic final answer
mock provider can emit a tool call
provider maps API error to structured runtime error
provider respects timeout config
streaming mode can be exercised with a fake stream
tool-call parsing fallback works
```

---

# Phase 3: Memvid `.mv2` Backend Hardening

Harden the Memvid backend until it is reliable enough to trust.

Requirements:

```text
inspect installed memvid-sdk signatures directly
support create/open/use without data loss
never call create(path) on an existing .mv2 file
normalize all search results into internal MemoryHit objects
support put, find, seal, verify, doctor, close if available
support read-only mode if SDK supports it
handle missing files gracefully
handle corrupt files gracefully
add memory verification command
add memory doctor/repair command where supported
add integration tests gated by RUN_MEMVID_INTEGRATION=1
```

Required integration behavior:

```text
create temp .mv2 files
write records
seal memory
verify memory
close backend
reopen backend
search again
confirm persistence
confirm no accidental overwrite
```

Acceptance commands:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
nest-agent memory verify --memory-dir .nest/memory
nest-agent memory doctor --memory-dir .nest/memory
```

---

# Phase 4: Tool System Expansion

The tool system is the agent's hands. It must be powerful but controlled.

Existing or expected tools:

```text
memory.search
memory.write
file.list
file.read
file.write
shell.run
```

Add or harden:

```text
repo.search
repo.map
patch.apply
test.run
lint.run
git.status
git.diff
git.branch
git.commit
memvid.verify
memvid.doctor
memory.consolidate
memory.inspect
memory.export
memory.import
```

Every tool must have:

```text
name
description
JSON schema
risk level
permission requirement
workspace boundary enforcement
timeout
structured result
event log entry
unit tests
success path
failure path
abuse/path escape tests
```

Risk levels:

```text
LOW
- read-only
- safe introspection
- examples: file.list, file.read inside workspace, git.status

MEDIUM
- writes inside workspace
- tests
- patch application
- examples: file.write, patch.apply, test.run

HIGH
- shell commands
- git commits
- network access
- examples: shell.run, git.commit

CRITICAL
- destructive actions
- secret access
- external publishing
- policy memory writes
```

Default behavior:

```text
LOW tools may run by default.
MEDIUM tools require explicit config enablement.
HIGH tools require explicit config enablement and approval.
CRITICAL tools require explicit approval every time.
```

Security rules:

```text
shell access must be denied by default
file writes must be restricted to configured workspace
path traversal must be blocked
no tool should access outside workspace unless explicitly configured
tool results must be structured
tool failures must be honest and visible
```

Acceptance tests:

```text
tool schemas validate arguments
path traversal is blocked
file read outside workspace is blocked
file write outside workspace is blocked
shell is blocked when disabled
git commit requires approval
tool timeout is enforced
tool failure is logged
```

---

# Phase 5: Coding-Agent Capability

Kestrel should be able to maintain codebases.

Implement codebase-oriented workflows.

## `repo.map`

`repo.map` should produce a structured map:

```text
languages
top-level directories
entry points
test framework
package manager
important config files
detected services
risk areas
likely build/test commands
detected AGENTS.md instructions
```

## `repo.search`

`repo.search` should:

```text
search filenames and content safely inside workspace
support text search
support extension filters where practical
return file path, line numbers, and snippets
enforce workspace boundaries
avoid binary files unless requested
```

## `patch.apply`

`patch.apply` should:

```text
accept unified diffs
preview changed files
enforce workspace boundaries
apply patch safely
return structured result
log changed paths
fail cleanly on conflicts
```

## `test.run`

`test.run` should:

```text
run configured test commands
support timeout
capture stdout/stderr
return exit code
summarize failures
write failures to working memory
write successful verified fixes as candidate evidence
```

## Git tools

Add or harden:

```text
git.status
git.diff
git.branch
git.commit
```

Rules:

```text
git.commit must be approval-gated
git.push should not exist unless added later as CRITICAL
no automatic pushing
do not hide dirty worktree state
```

Acceptance scenario:

```text
agent maps repo
agent identifies test command
agent applies a patch
agent runs tests
agent reports pass/fail honestly
agent refuses to claim success if tests fail
```

---

# Phase 6: Nested Learning Consolidation

This is Kestrel's defining feature. Build it carefully.

The consolidation pipeline should identify useful learning candidates from:

```text
working memory
episodic memory
tool results
test failures
test successes
user corrections
repeated procedures
final turn summaries
manual memory writes
```

Candidate types:

```text
fact
preference
decision
failure
fix
procedure
policy_candidate
conflict
```

Promotion rules:

```text
Working → Episodic
- meaningful event
- user correction
- failure
- decision
- session summary

Episodic → Semantic
- repeated fact
- externally verified fact
- user-confirmed stable preference

Episodic → Procedural
- repeated successful workflow
- fix verified by tests
- stable debugging recipe

Semantic/Procedural → Policy
- rare
- explicit user approval
- very high confidence
- strong evidence
```

Every promoted record must include:

```text
source record IDs
source layer
destination layer
evidence refs
confidence
importance
validation method
promotion reason
timestamp
```

Implement conflict detection.

If two memories conflict, do not silently merge them. Create a conflict record or surface the conflict in context compilation.

Memory philosophy:

```text
Remember outcomes, not vibes.
Prefer evidence over assumptions.
Prefer repeated verified behavior over single anecdotes.
Failure memories are useful but must not become procedures unless a fix is verified.
Policy memory is sacred and should be difficult to write.
```

Required tests:

```text
one success does not become procedure
one correction does not become policy
repeated validated success becomes procedural memory
validated user preference becomes semantic memory
conflicting facts are flagged
low-confidence memory is not promoted
policy write requires explicit permission
failed tool result does not become trusted procedure
duplicate memories are deduped or linked
```

---

# Phase 7: Context Compiler Hardening

The context compiler should behave like the agent's working cognitive state.

It must compile:

```text
objective
relevant working memory
relevant episodic memory
relevant semantic memory
relevant procedural memory
relevant policy memory
conflict warnings
confidence notes
tool availability
next-step instruction
```

Ranking should consider:

```text
layer
relevance
confidence
importance
recency
validation status
evidence strength
conflict status
```

Rules:

```text
enforce a context budget
do not blindly stuff raw memory
include memory confidence/evidence where useful
surface conflicts
favor policy/procedural memory when relevant
make context inspectable for debugging
```

Add CLI/API support for inspecting compiled context:

```bash
nest-agent context "query here"
nest-agent chat ... /context query here
```

Acceptance tests:

```text
context stays under budget
relevant semantic memory appears
irrelevant memory is excluded
conflict warning appears when needed
procedural recipe appears for matching task
policy memory appears only when relevant
```

---

# Phase 8: Security Model

Implement a serious security posture.

Default mode should be safe and local.

Required protections:

```text
workspace jail
path traversal prevention
shell disabled by default
network disabled by default unless explicitly enabled
git commit approval
destructive action approval
policy memory approval
MCP server allowlist
tool timeout
secret redaction in logs
safe event logging
prompt-injection warnings for untrusted files
read-only mode
emergency stop/cancel
```

Secret redaction should detect:

```text
API keys
bearer tokens
passwords
private keys
.env values
authorization headers
GitHub tokens
OpenAI keys
Anthropic keys
OpenRouter keys
```

Add tests for:

```text
attempt to read outside workspace
attempt to write outside workspace
attempt to run shell while disabled
attempt to commit without approval
attempt to write policy memory without approval
tool timeout
malformed tool arguments
secret redaction in logs
```

Security acceptance principle:

```text
The agent should be useful while constrained.
Unsafe convenience is not production readiness.
```

---

# Phase 9: CLI Productization

The CLI should be genuinely usable.

Commands to support or harden:

```bash
nest-agent init
nest-agent chat
nest-agent server
nest-agent memory search
nest-agent memory verify
nest-agent memory doctor
nest-agent memory consolidate
nest-agent memory inspect
nest-agent tools
nest-agent context
nest-agent doctor
nest-agent run
nest-agent eval
```

Chat slash commands:

```text
/exit
/help
/tools
/context <query>
/memory <query>
/doctor
/session
/approve
/deny
/status
```

CLI should show:

```text
session ID
run ID
stop reason
tool calls
approval required
memory writes
test result summary
error summary
```

Keep output useful but not obnoxious.

Acceptance tests:

```text
nest-agent --help works
nest-agent doctor works
nest-agent chat --provider mock works
slash commands work in chat
memory search command works
context command works
invalid command gives helpful error
```

---

# Phase 10: API and Web Dashboard

Only after the CLI and runtime are stable, harden the API/web dashboard.

API routes should include:

```text
GET  /api/health
POST /api/runs
GET  /api/runs
GET  /api/runs/{run_id}
POST /api/runs/{run_id}/cancel
GET  /api/runs/{run_id}/events
GET  /api/tools
POST /api/tools/{tool_name}/invoke
GET  /api/approvals
POST /api/approvals/{approval_id}/decision
GET  /api/memory/search
GET  /api/memory/verify
POST /api/memory/consolidate
GET  /api/context
GET  /api/sessions
```

Dashboard should include:

```text
chat/run console
live event timeline
tool approval inbox
memory browser
compiled context viewer
tool registry viewer
MCP server manager
skills/plugins viewer
settings/config page
logs viewer
run replay
```

Use SSE for live run events.

Rules:

```text
do not expose dangerous actions without local auth or explicit trusted-local-only mode
show approvals clearly
show tool risk levels clearly
show failed runs honestly
```

---

# Phase 11: Eval Harness

Build a serious eval harness.

Add:

```text
scripts/run_golden_evals.py
golden/
golden/*.json
```

Required eval scenarios:

```text
1. Agent remembers a user correction across sessions.
2. Agent retrieves prior failure before repeating it.
3. Agent uses procedural recipe after repeated successful fixes.
4. Agent refuses workspace path escape.
5. Agent blocks shell when shell is disabled.
6. Agent verifies .mv2 files after memory writes.
7. Agent compiles useful context under budget.
8. Agent avoids writing policy from ordinary event.
9. Agent can map a repository.
10. Agent can apply a patch and run tests.
11. Agent reports failure honestly when tests fail.
12. Agent does not claim success without evidence.
```

Each eval should output JSON:

```json
{
  "name": "remember_correction_across_sessions",
  "passed": true,
  "latency_ms": 123,
  "context_chars": 4200,
  "tool_count": 2,
  "memory_hits": 4,
  "memory_writes": 3,
  "reason": "..."
}
```

Aggregate summary should include:

```text
pass count
fail count
latency stats
context size stats
tool count stats
promotion precision
false promotion count
```

Acceptance commands:

```bash
python scripts/run_golden_evals.py --backend memory
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid
```

---

# Phase 12: Observability

Production agents need introspection.

Add:

```text
structured JSONL logs
run timeline
tool trace
memory trace
context trace
provider trace
test/eval trace
error trace
```

Do not log secrets.

Add `nest-agent doctor` command that checks:

```text
Python version
package install
optional extras
Memvid availability
memory directory
memory file verification
provider config
workspace config
tool config
test command config
server availability
```

Acceptance tests:

```text
doctor detects missing memvid extra
doctor detects missing memory dir
doctor verifies memory files
logs redact fake API keys
run trace includes tool execution
run trace includes memory writes
```

---

# Phase 13: Packaging and Deployment

Make Kestrel easy to install and run.

Add or harden:

```text
.env.example
Dockerfile
docker-compose.yml
Makefile or task runner
GitHub Actions CI
release checklist
configuration docs
memory backup docs
memory migration docs
provider setup docs
local model setup docs
security docs
```

CI should run:

```bash
python -m compileall -q src tests
ruff check .
mypy src
pytest -q
```

Optional integration jobs should be gated by secrets/env vars.

Packaging acceptance:

```text
fresh clone can install dev extras
fresh clone can run mock chat
Docker image can run doctor
CI passes core checks
.env.example documents provider and safety options
```

---

# Final Acceptance Criteria

The project is production-ready enough for an alpha release when these pass:

```bash
python -m compileall -q src tests
ruff check .
mypy src
pytest -q
python scripts/run_golden_evals.py --backend memory
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid
```

And these user flows work:

```bash
pip install -e '.[memvid,openai,server,mcp,dev]'

nest-agent init --backend memvid --memory-dir .nest/memory

nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider openai \
  --model <available-model> \
  --message "Remember that I prefer concise answers."

nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider openai \
  --model <available-model> \
  --message "What do you remember about my answer style?"

nest-agent memory verify --memory-dir .nest/memory

nest-agent doctor
```

Expected behavior:

```text
memory persists across turns
memory persists across process restarts
context compiler retrieves relevant memory
agent does not dump full transcript
.mv2 files verify successfully
tool calls are logged
high-risk tools are gated
path escapes are blocked
shell is disabled unless enabled
test failures are reported honestly
procedural memory only forms after repeated verified success
policy memory requires explicit permission
```

---

# Implementation Strategy

Work in small, verifiable increments.

For each phase:

```text
1. Inspect current code.
2. Make the smallest coherent implementation.
3. Add or update tests.
4. Run the relevant tests.
5. Fix failures.
6. Update docs.
7. Continue to the next phase.
```

Priority order:

```text
1. Baseline passing state
2. Runtime reliability
3. Provider/tool-call correctness
4. Memvid backend hardening
5. Tool safety and coding-agent tools
6. Consolidation pipeline
7. Context compiler quality
8. Eval harness
9. CLI polish
10. API/web dashboard
11. Packaging and deployment
```

Do not skip directly to UI before core correctness.

---

# What Not to Do

Do not:

```text
replace .mv2 memory with a vector DB
make shell enabled by default
allow file writes outside workspace
write policy memory from one ordinary event
claim tests pass without running them
silently ignore failed tools
stuff full transcripts into context
build a pretty dashboard while core runtime is unstable
remove deterministic mock testing
make unit tests depend on real API keys
hide failures behind vague success messages
```

---

# Final Product Identity

Kestrel should become the memory-native engineering agent.

OpenClaw's lane is broad personal-assistant infrastructure.  
Hermes' lane is skill/self-improvement oriented.  
Kestrel's lane should be:

```text
local-first codebase maintenance
safe tool use
memory-backed learning
evidence-based consolidation
honest failure tracking
procedural improvement over time
```

Kestrel should feel less like:

```text
chatbot with tools
```

and more like:

```text
a local apprentice that stops making the same mistake twice
```

Build toward that.
