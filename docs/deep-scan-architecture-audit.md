# Deep Scan Architecture Audit

Date: 2026-02-22
Scope: `packages/brain`, `packages/hands`, `packages/gateway`, `packages/web`, `packages/cli`, `packages/shared`, and top-level `tests`.

## Plan and reasoning

1. **Inventory modules and hotspots** by file size and role to find likely architectural bottlenecks first.
2. **Inspect core execution paths** (task loop, tool dispatch, channel routing, sandbox execution, chat UI streaming) to identify systemic speed and reliability risks.
3. **Evaluate security and correctness controls** around auth, cross-channel deduplication, isolation boundaries, and persistence durability.
4. **Prioritize major improvements** with highest impact and lowest migration risk.

Why this approach: this repo is a multi-service autonomous agent platform where bottlenecks are usually in orchestration code, boundary layers, and long-lived stateful components.

## Observations (evidence summary)

- The Brain service has multiple monolithic files (`server.py`, `agent/loop.py`, `tools/self_improve.py`) that combine orchestration, I/O, policy, and transport concerns.
- Tooling and council execution are broad and powerful, but many workflows are serialized where controlled concurrency would reduce latency.
- Some reliability controls are present (timeouts, sandboxing, retries), but there are in-memory/session-local data structures and `/tmp` persistence that can break under restarts or horizontal scaling.
- Gateway and Web code have large “god files” and relatively low test coverage in proportion to surface area.

## Highest-impact improvement opportunities

## 1) Break up monolithic service entry points into bounded domains (Architecture + Reliability)

### Evidence

- `packages/brain/server.py` is very large and owns transport, runtime globals, auth/workspace operations, task lifecycle, and prompt/runtime assembly in one place.
- `packages/web/src/components/Settings/SettingsPanel.tsx` and `packages/web/src/components/Chat/ChatView.tsx` are very large UI components.
- `packages/gateway/src/channels/telegram.ts` and `packages/gateway/src/channels/discord.ts` are large integration adapters with many responsibilities.

### Improvement

- Split by vertical capability with strict interfaces:
    - Brain: `task_api`, `chat_api`, `memory_api`, `automation_api`, `provider_api` modules.
    - Gateway: shared middleware + per-channel transport adapters + a channel orchestration core.
    - Web: container/presenter split + typed state machines for streaming chat and settings.

### Expected impact

- Faster onboarding and safer refactors.
- Better unit-testability and lower blast radius for production incidents.

## 2) Replace process-local state with durable/shared coordination (Reliability + Scale)

### Evidence

- Running tasks are tracked in-memory; event stream reconnection is explicitly unimplemented.
- Notification preference cache is in-memory in the router.
- Self-improvement proposals are persisted in `/tmp/kestrel_proposals.json`.

### Improvement

- Move live task/event fan-out to Redis streams or pub/sub + cursor replay.
- Keep per-user/channel prefs in Redis as source-of-truth and invalidate local cache via pub/sub.
- Replace `/tmp` proposal storage with Postgres table including status transitions and audit fields.

### Expected impact

- Restarts no longer lose critical state.
- Horizontal scaling without sticky sessions.

## 3) Introduce explicit concurrency control for slow parallelizable work (Speed)

### Evidence

- Council deliberation performs member evaluation in sequence.
- Message routing in all-channel mode sends each channel sequentially.
- Some identity fetch loops and graph upsert logic execute many DB operations one-by-one.

### Improvement

- Use bounded `asyncio.gather`/`Promise.allSettled` with per-target timeout budgets.
- Batch DB reads/writes for graph operations (`UNNEST`, CTE upserts, conflict handling).
- Add a small concurrency utility with global limits to avoid thundering herds.

### Expected impact

- Lower p95/p99 latency on multi-agent tasks and multi-channel notification paths.

## 4) Harden memory and graph pipelines with batch semantics + quality gates (Speed + Quality)

### Evidence

- Embedding worker is single-queue, single-item processing.
- Graph node/edge ingestion performs many per-item queries in loops.
- Retrieval truncation and fixed thresholds are static and may not adapt to model context size.

### Improvement

- Add batch embedding (N items / time window), retry-with-backoff, and dead-letter handling.
- Use set-based SQL for graph updates; add uniqueness constraints for entity normalization.
- Add adaptive retrieval policy (workspace size, query type, model context budget).

### Expected impact

- Higher throughput and fewer duplicate/low-signal memories.

## 5) Strengthen auth/authorization consistency and service boundaries (Security + Reliability)

### Evidence

- Gateway memory route directly queries DB and stitches graph-like payloads, bypassing Brain ownership boundaries.
- API key validation logic constructs Redis client internally in middleware path.
- Some endpoints have TODOs or partial wiring between gateway and brain features.

### Improvement

- Move all domain reads/writes for agent memory/evidence behind Brain APIs.
- Centralize auth/token/API-key validation in a single dependency-injected auth service.
- Add explicit contract tests for gateway ↔ brain authorization and workspace scoping.

### Expected impact

- Fewer data consistency bugs and auth drift across services.

## 6) Add production-grade observability and SLO instrumentation (Reliability + Operability)

### Evidence

- Logging exists, but cross-service traces and end-to-end latency budget visibility are limited.
- Several broad exception handlers return generic failures without structured error taxonomy.

### Improvement

- Add OpenTelemetry tracing for request/task IDs across gateway → brain → hands.
- Standardize error envelopes and classify retryable vs terminal errors.
- Define SLOs: task start latency, tool call success rate, chat token stream startup latency, sandbox failure rate.

### Expected impact

- Faster incident triage and measurable reliability improvements.

## 7) Expand automated test pyramid where risk is highest (Reliability)

### Evidence

- Current tests are sparse compared to total module surface (especially web and gateway integration flows).

### Improvement

- Add:
    - Contract tests for gateway routes + brain stubs.
    - Property-style tests for dedup hashing/window behavior.
    - Replay tests for task stream reconnect semantics.
    - Web component tests for streaming state transitions.

### Expected impact

- Prevent regressions while enabling aggressive refactors.

## 8) Optimize sandbox execution lifecycle and audit fidelity (Speed + Security)

### Evidence

- Container execution is synchronous in executor threadpool with per-run spin-up/teardown.
- Audit payload includes placeholders for file/system events that are not deeply populated.

### Improvement

- Add warm container pools (or lightweight runner reuse) for trusted low-risk operations.
- Enrich audit stream with normalized event schema and policy decisions.
- Add per-workspace quota + circuit-breaker policies (rate, CPU-minutes, failure bursts).

### Expected impact

- Faster tool execution and stronger forensic capability.

## 9) Frontend state architecture modernization (Speed + Reliability)

### Evidence

- Chat and settings flows maintain many local states and imperative effects in large components/hooks.

### Improvement

- Introduce a typed state machine (e.g., XState or reducer-driven finite states) for chat streaming lifecycle.
- Normalize server state with query caching and stale-while-revalidate semantics.
- Virtualize long message/task streams to reduce render cost.

### Expected impact

- Fewer UI race conditions and smoother performance under long sessions.

## 10) Define an explicit platform contract layer (Architecture)

### Evidence

- Shared protocol exists (`packages/shared/proto`), but some features route via direct DB access or partial route TODOs.

### Improvement

- Expand protobuf/typed contracts for memory graph, evidence, and feature management.
- Ban ad-hoc cross-service DB reads from gateway where a Brain contract should exist.

### Expected impact

- Clear ownership boundaries and cleaner future decomposition.

## Suggested execution roadmap

### Phase 1 (2–3 weeks)

- Event stream durability + reconnect.
- Extract Brain task API from monolith.
- Add contract tests for task lifecycle and auth.

### Phase 2 (3–5 weeks)

- Council/router concurrency upgrades with budgets.
- Memory/graph batching improvements.
- Observability baseline with tracing and SLO dashboards.

### Phase 3 (4–6 weeks)

- Web state machine migration for chat/settings.
- Sandbox pooling + audit schema uplift.
- Complete contract-first boundary cleanup.

## Quick wins (can start immediately)

1. Implement task event replay backend and remove `UNIMPLEMENTED` reconnect path.
2. Replace `/tmp` proposal store with database-backed workflow state.
3. Parallelize council member evaluation with bounded concurrency.
4. Add gateway-memory route contract path through Brain instead of direct DB stitching.
5. Add tests around dedup collision window and router fallback behavior.
