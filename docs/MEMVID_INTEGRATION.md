# Memvid Integration

Last updated: 2026-05-16

Kestrel uses Memvid v2 `.mv2` files as its durable retrieval-memory substrate. Do not build against deprecated QR/video-frame Memvid v1 behavior, and do not replace `.mv2` memory with SQLite, Postgres, Chroma, FAISS, or JSON logs.

SQLite is used only for control-plane state such as runs, approvals, MCP servers, skills, task nodes, and subagent records.

## Current Adapter

The executable adapter is `src/nested_memvid_agent/backends/memvid_backend.py`.

Current behavior:

- Imports `memvid_sdk` lazily so unit tests can run without the Memvid extra.
- Uses `use("basic", path, ...)` for existing `.mv2` files.
- Calls `create(path, enable_vec=False, enable_lex=True)` only for missing files.
- Defaults to lexical-first writes (`enable_vec=False`, `enable_lex=True`) to avoid accidental embedding/API-key requirements.
- Writes records with `text`, `uri`, `tags`, `labels`, `track`, `kind`, and nested-memory metadata.
- Passes `enable_embedding=self.enable_vec` on writes so embedding use is explicit.
- Normalizes SDK `find()` result shapes into `MemoryHit`.
- Avoids scope assumptions that can trigger lexical-index errors in installed SDKs.
- Supports context-frame round trips through `put_frame()` and `find_frames()`.
- Exposes `seal()`, `verify()`, `doctor(dry_run=True)`, `stats()`, and `close()`.

The adapter was hardened against `memvid_sdk 2.0.159`. Re-check SDK signatures before upgrading the dependency.

## Storage Layout

Kestrel keeps one permanent `.mv2` file per nested layer:

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

`complete.mv2` is a run evidence bundle, not a permanent memory layer.

## Data-Loss Rules

- Never call `create(path)` when `path` already exists.
- Open existing `.mv2` files through the SDK's safe open/use path.
- Copy `.mv2` files for backup or migration; do not recreate them in place.
- Seal changed memories after writes.
- Verify important memory directories after migration, restore, or SDK upgrade.

## Metadata Contract

Every stored record should carry nested-memory metadata such as:

```json
{
  "id": "mem_xxx",
  "nested_layer": "semantic",
  "nested_kind": "fact",
  "nested_confidence": 0.91,
  "nested_importance": 0.75,
  "content_hash": "sha256...",
  "source": "tool.memory.write",
  "frame_type": "section_summary",
  "parent_ids": ["raw_parent"],
  "child_ids": ["raw_child"],
  "context_flow_id": "semantic_fact_consolidation",
  "validation_status": "validated",
  "created_at": "2026-05-16T..."
}
```

For context frames, preserve frame ID, frame type, parent/child links, source URI/span, content hash, confidence, importance, provenance, and validation metadata.

## Search Modes

Layer defaults should stay conservative:

- Working: lexical, because active state often contains exact paths, errors, and command text.
- Episodic: auto/hybrid where available, because events and failures benefit from exact and semantic recall.
- Semantic: auto/hybrid where available.
- Procedural: auto/hybrid where available.
- Policy: lexical exactness preferred.

The current adapter uses the SDK's returned hits directly and normalizes metadata instead of forcing layer-specific scope filtering.

## CLI and Tools

CLI commands:

```bash
nest-agent init --backend memvid --memory-dir .nest/memory
nest-agent memory verify --backend memvid --memory-dir .nest/memory
nest-agent memory doctor --backend memvid --memory-dir .nest/memory
nest-agent memory inspect --backend memvid --memory-dir .nest/memory "policy promotion"
```

Built-in tools:

- `memvid.verify`
- `memvid.doctor`
- `memvid.stats`
- `memory.inspect`
- `memory.export`
- `memory.import`
- `context.pack`
- `context.expand`
- `memory.conflicts`

`memory.import` is high-risk and approval-gated. Policy writes remain gated separately by `allow_policy_writes`.

## Integration Tests

Memvid tests are opt-in:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

The integration suite covers:

- write -> seal -> verify -> close -> reopen -> search
- context-frame metadata round trip
- run-capsule `complete.mv2` summary reads
- isolated memory directories for golden eval cases to avoid `.mv2` lock contention

Use `python -m pytest` so fixture subprocesses inherit the same interpreter and installed extras.

## Upgrade Checklist

Before changing the Memvid SDK version:

1. Inspect `create`, `use`, `put`, `find`, `seal`, `verify`, `doctor`, `stats`, and `close` signatures.
2. Confirm existing files are opened with `use(...)`, not recreated.
3. Confirm lexical-first writes do not require an embedding API key.
4. Confirm `find()` result metadata is still normalized into `MemoryHit`.
5. Run the gated Memvid integration tests.
6. Run golden evals with both memory and Memvid backends when possible.
