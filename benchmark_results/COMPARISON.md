# Kestrel vs Other Agent Frameworks — Benchmark Comparison

**Date:** 2026-05-18
**Kestrel version:** Latest (post-retry-layer)
**Model tested:** kimi-k2.6 via Ollama Cloud (openai-compatible provider)

---

## Executive Summary

| Framework | Type | Benchmark Domain | Score | Key Strength |
|-----------|------|-----------------|-------|--------------|
| **Kestrel** | Local-first agent runtime with layered memory | Error recovery + agent tasks + memory retrieval | **6/6 error recovery, 4/4 agent tasks** | Transparent retry layer, Memvid v2 memory, alias resolution |
| **OpenClaw / Dominion** | Multi-agent coding orchestrator | Coding tasks (JS/Python) | 2/5 (Dominion AI), 4/5 (Opus), 5/5 (GPT 5.5) | Subagent delegation, OAuth integration |
| **Hermes Agent** | Meta-agent orchestration platform | N/A — no published task benchmark | N/A | Multi-tool routing, persistent sessions, gateway architecture |

**Important caveat:** These systems have different goals and no shared benchmark suite exists. This comparison uses the best available data from each system's own benchmarks.

---

## 1. Kestrel — Full Benchmark Results

### Error Recovery & Robustness

| Task | Status | Errors Injected | What Kestrel Did |
|------|--------|-----------------|------------------|
| transient_file_read | ✅ PASS | `transient_error` | Retry layer retried silently, LLM never saw failure |
| file_not_found | ✅ PASS | `not_found` | Correctly reported missing file, no retry |
| empty_search_results | ✅ PASS | `empty_results` | Correctly reported no matches, no retry |
| malformed_tool_name | ✅ PASS | — | Alias `read` → `file.read` resolved correctly |
| strategy_retry | ✅ PASS | `transient_error` | Retry layer handled transparently |
| max_retries_exceeded | ✅ PASS | `transient_error` | After 3 retries, reported graceful failure |

**Score: 6/6 (100%)**

### Agent Tasks

| Task | Status | Tools Used | Latency |
|------|--------|-----------|---------|
| memory_persistence | ✅ PASS | `memory.search`, `self.reflect` | ~61s |
| file_read_qa | ✅ PASS | `file.read` | ~6s |
| repo_search | ✅ PASS | `repo.search` | ~31s |
| git_status | ✅ PASS | `git.status` | ~11s |

**Score: 4/4 (100%), avg 1.25 tools/task**

### Memory Retrieval (vs Flat TF-IDF Baseline)

| Metric | Kestrel (Layered) | Baseline (Flat RAG) | Delta |
|--------|-------------------|---------------------|-------|
| Recall@5 | 0.967 | 1.0 | -0.033 |
| **Precision@5** | **0.331** | 0.2 | **+65%** |
| MRR | 0.858 | 0.878 | -0.02 |
| Avg Latency | **0.073 ms** | 0.124 ms | **1.7× faster** |

Kestrel trades a tiny amount of recall for significantly better precision and lower latency because layered retrieval doesn't pollute results across memory types.

---

## 2. OpenClaw / Dominion — Coding Benchmark

OpenClaw's "Dominion" is a multi-agent coding orchestrator. Its benchmark tests raw coding ability across 5 tasks of increasing difficulty.

### Dominion Coding Benchmark Results

| Task | Difficulty | Dominion AI | GPT 5.5 | Opus/kimi-code |
|------|-----------|-------------|---------|----------------|
| easy-js-duration | 1 | ❌ FAIL (refusal) | ✅ PASS | ✅ PASS |
| medium-js-lru | 2 | ✅ PASS | ✅ PASS | ✅ PASS |
| hard-js-async-pool | 3 | ✅ PASS | ✅ PASS | ✅ PASS |
| expert-js-json-patch | 4 | ❌ FAIL (bug) | ✅ PASS | ✅ PASS |
| expert-py-template-engine | 5 | ❌ MISSING (timeout) | ✅ PASS | ❌ FAIL (if-else bug) |
| **Total** | | **2/5 (40%)** | **5/5 (100%)** | **4/5 (80%)** |

### OpenClaw Architecture Notes

- **Multi-agent orchestration** — Delegates to subagents for different tasks
- **OAuth integration** — Deep GitHub/Codex integration
- **Web app scaffolding** — Can generate full React/Vite apps
- **No published memory benchmark** — No retrieval metrics available
- **No published error recovery benchmark** — No robustness metrics available

---

## 3. Hermes Agent — Platform Context

Hermes Agent (the platform I'm running on) is a **meta-agent orchestration system** by Nous Research. It's not a single-task agent like Kestrel or a coding agent like Dominion — it's the infrastructure that hosts and routes between agents.

### Hermes Agent Capabilities

- **Multi-tool routing** — Can load skills, call tools, manage sessions
- **Persistent memory** — Cross-session MEMORY.md and USER.md
- **Gateway architecture** — Telegram, Discord, Slack, SMS integration
- **Cron scheduling** — Autonomous background jobs
- **Subagent delegation** — Can spawn isolated worker agents

### Why No Benchmark?

Hermes Agent doesn't have a published task-success benchmark because it's not designed as a task-completion agent — it's designed as a **host platform** for agents. A fair comparison would require:

- Running Kestrel *inside* Hermes Agent as a skill/plugin
- Or running Hermes Agent's subagent system through Kestrel's benchmark harness

Neither has been done yet.

---

## 4. Architecture Comparison

| Dimension | Kestrel | OpenClaw / Dominion | Hermes Agent |
|-----------|---------|---------------------|--------------|
| **Primary goal** | Local-first memory-native agent runtime | Multi-agent coding/productivity | Meta-agent orchestration platform |
| **Memory system** | Layered Memvid v2 (6 layers) | Not documented / unknown | File-based (MEMORY.md, USER.md) |
| **Tool use** | Native tool registry with aliases | Subagent delegation | Skill-based tool loading |
| **Error handling** | Transparent retry layer + classification | Not benchmarked | Error propagation to user |
| **Retry policy** | Strategy-gated + programmatic | Unknown | No built-in retry layer |
| **Local-first** | ✅ Yes | ⚠️ Hybrid | ✅ Yes |
| **Cloud dependency** | Optional (can use local models) | Requires cloud LLM | Optional |
| **Coding focus** | General agent (can code via tools) | Primary focus | General purpose |

---

## 5. Model Behavior Comparison (Live Testing)

These models were tested against Kestrel's benchmark suite:

| Model | Tool Use | Alias Handling | Retry Behavior | Best For |
|-------|----------|---------------|----------------|----------|
| **kimi-k2.5** | ✅ Excellent | ✅ Correct | Graceful | Reference model for Kestrel |
| **kimi-k2.6** | ✅ Excellent | ✅ Correct | Over-cautious without retry layer | Good with programmatic retry |
| **gemma3:4b** | ❌ Poor | ❌ Hallucinates | N/A | Not usable for agent tasks |
| **gemma3:27b** | ❌ Refuses tools | N/A | N/A | Not usable for agent tasks |
| **qwen3.5:397b** | ⚠️ Partial | ⚠️ Sometimes | Confused | Limited to simple tasks |

---

## 6. What Would a Fair Head-to-Head Look Like?

To truly compare these systems, you'd need a **shared benchmark suite** that tests:

### A. Error Recovery (Kestrel's strength)
- Inject transient failures during tool execution
- Measure: success rate, retry behavior, graceful degradation
- **Kestrel has this** — OpenClaw and Hermes Agent do not publish equivalent metrics

### B. Memory Retrieval (Kestrel's strength)
- Ingest corpus, run queries, measure recall/precision/latency
- **Kestrel has this** — OpenClaw and Hermes Agent do not publish equivalent metrics

### C. Coding Tasks (OpenClaw's strength)
- Hidden-test coding benchmark across difficulty levels
- **OpenClaw has this** — Kestrel has not been tested on this yet

### D. Multi-step Agent Tasks (All could do this)
- File read → search → git status → memory write
- **Kestrel has this** — could be adapted for others

### E. Real-world Integration (Hermes Agent's strength)
- Multi-channel messaging, cron jobs, persistent sessions
- **Hermes Agent has this** — the others don't compete in this space

---

## 7. Recommendations

### If you want the best error recovery + memory system
→ **Kestrel** — The transparent retry layer and layered Memvid memory are unique. No other system tested handles transient failures this gracefully.

### If you want the best coding agent
→ **OpenClaw + GPT 5.5** — 5/5 on coding benchmark. Dominion AI (OpenClaw's default) is weaker (2/5) but has better integration.

### If you want the best orchestration platform
→ **Hermes Agent** — Multi-channel, persistent sessions, cron jobs, subagent delegation. This is infrastructure, not a task agent.

### If you want a single local-first agent that does everything
→ **Kestrel inside Hermes Agent** — Use Hermes as the orchestration layer and Kestrel as the agent runtime. Best of both worlds.

---

## Files Referenced

- Kestrel error recovery: `benchmark_results/error_recovery_ollama_k2.6_retry.json`
- Kestrel agent tasks: `benchmark_results/agent_ollama_k2.6_retry.json`
- Kestrel memory: `benchmark_results/memory_baseline.json`
- OpenClaw dominion: `~/.openclaw/workspace/benchmarks/dominion-coding/results/`
- OpenClaw report: `~/.openclaw/workspace/benchmarks/dominion-coding/REPORT.md`
