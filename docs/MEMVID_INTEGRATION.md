# Memvid Integration

Last updated: 2026-07-20

Kestrel uses Memvid v2 `.mv2` files as its durable retrieval-memory substrate. Do not build against deprecated QR/video-frame Memvid v1 behavior, and do not replace `.mv2` memory with SQLite, Postgres, Chroma, FAISS, or JSON logs.

SQLite is used for control-plane state such as runs, approvals, MCP servers, skills, task nodes, subagent records, promotion ledgers, and behavior-delta ledgers. The only retrieval exception is an explicitly configured rebuildable vector sidecar per layer. A sidecar is keyed to `.mv2` record IDs and content hashes, stores embeddings instead of raw memory text, and can be deleted/rebuilt without losing memory.

## Current Adapter

The executable adapter is `src/nested_memvid_agent/backends/memvid_backend.py`.

Current behavior:

- Imports `memvid_sdk` lazily so unit tests can run without the Memvid extra.
- Uses `use("basic", path, ...)` for existing `.mv2` files.
- Calls `create(path, enable_vec=False, enable_lex=True)` only for missing files.
- Requires a successful SDK create call to materialize the `.mv2` container, then validates and hardens that exact file; a non-materializing create fails closed.
- Defaults to lexical-first writes (`enable_vec=False`, `enable_lex=True`) to avoid accidental embedding/API-key requirements.
- Writes records with `text`, digest-addressed `uri`, `tags`, `labels`, `track`, `kind`, and nested-memory metadata.
- Embeds a schema-versioned, SHA-256-verified canonical event envelope in each `.mv2` logical commit so exact record content, evidence, timestamps, corrections, and tombstones can be replayed without JSON state. New envelopes carry a monotonic commit sequence and previous-event digest, making missing or out-of-order canonical commits fail closed.
- Passes `enable_embedding=self.enable_vec` on writes so embedding use is explicit.
- Normalizes SDK `find()` result shapes into `MemoryHit`.
- Avoids scope assumptions that can trigger lexical-index errors in installed SDKs.
- Supports context-frame round trips through `put_frame()` and `find_frames()`.
- Replays `.mv2` timeline/frame metadata on every production open and refreshes a disposable exact-record cache for `get_record()`, `iter_records()`, exports, and conflict checks. Pagination follows descending logical commit IDs until origin frame `0`; SDK `frame_count`/`active_frame_count` are physical chunk counts and are not treated as record totals.
- Serializes operations on each live SDK handle so one agent cannot race Memvid or its exact-record cache internally.
- Admits one Memvid-backed agent lifecycle per `RunManager`. Additional primary runs remain in the bounded, cancellable durable queue; subagents and manual memory/tool endpoints wait on the same shutdown-cancellable fence. Primary and approval-continuation agents release their layer handles before autonomous scheduler workers start. This avoids opening the same six exclusive `.mv2` writers twice without weakening the one-file-per-layer layout.
- Exposes `seal()`, `verify()`, `doctor(dry_run=True)`, `stats()`, and `close()`.

The adapter was hardened against `memvid_sdk 2.0.160`. Re-check SDK signatures before upgrading the dependency.

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

Each `.mv2` file may have a companion exact-record sidecar:

```text
.nest/memory/semantic.mv2.records.json
```

The sidecar is a disposable startup/cache artifact containing reconstructed `MemoryRecord` snapshots, inactive IDs, a cache checksum, and the observed Memvid fingerprint. It is never trusted as startup truth for schema-v2 containers: Kestrel replays digest-verified canonical envelopes from `.mv2`, then repairs or recreates the JSON. Deleting or coherently tampering with this cache must not change active records or resurrect tombstones.

Containers created by older Kestrel builds have frames without canonical envelopes. Their version-one exact-record sidecar is required exactly once: the first writable open appends a canonical snapshot event for every cached record and inactive state, then writes the disposable schema-v2 cache. Do not delete a legacy sidecar before that migration. A legacy `.mv2` with no usable cache fails closed with migration guidance because exact content cannot be reconstructed from old SDK search snippets.

Configured non-policy layers may also have a disposable vector sidecar:

```text
.nest/memory/semantic.mv2.vector.sqlite
```

Vector sidecars are local SQLite indexes of embedding blobs keyed by record ID and content hash. They do not store raw memory text. If a vector sidecar is stale or missing, rebuild exact records from `.mv2` and then rebuild embeddings from those records; do not treat either sidecar as backup memory.

## Data-Loss Rules

- Never call `create(path)` when `path` already exists.
- Open existing `.mv2` files through the SDK's safe open/use path.
- Copy `.mv2` files for backup or migration; do not recreate them in place.
- Seal changed memories after writes.
- Verify important memory directories after migration, restore, or SDK upgrade.
- After schema-v2 migration, `.mv2` alone preserves exact enumeration, tombstones, corrections, provenance, and conflict metadata; `.mv2.records.json` may be copied as an optional warm cache but must be rebuildable.
- Before the one-time migration of a legacy container, preserve its version-one `.mv2.records.json` because old frames do not contain canonical envelopes.

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
- Episodic: auto by default; hybrid only with explicit local vector layer config.
- Semantic: auto by default; hybrid only with explicit local vector layer config.
- Procedural: auto by default; hybrid only with explicit local vector layer config.
- Policy: lexical exactness preferred.

Hybrid retrieval rank-fuses lexical `.mv2` hits with vector-sidecar hits at the layered memory router. `mode=lex` bypasses vector sidecars, and policy memory is forced lexical even if vector fields are present. The Memvid adapter still uses the SDK's returned hits directly and normalizes metadata instead of forcing layer-specific scope filtering.

## CLI and Tools

CLI commands:

```bash
nest-agent init --backend memvid --memory-dir .nest/memory
nest-agent memory verify --backend memvid --memory-dir .nest/memory
nest-agent memory doctor --backend memvid --memory-dir .nest/memory
nest-agent memory inspect --backend memvid --memory-dir .nest/memory "policy promotion"
nest-agent memory vector status --backend memvid --memory-dir .nest/memory --layer-config .nest/config/layers.json
nest-agent memory vector rebuild --backend memvid --memory-dir .nest/memory --layer-config .nest/config/layers.json --layer semantic
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
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_memory_system.py tests/integration/test_memvid_context_frames.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memvid --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memvid
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
```

The integration suite covers:

- write -> seal -> verify -> close -> reopen -> search
- exact enumeration across more than the SDK's default 100-frame timeline page after deleting `.mv2.records.json`
- exact replay of a 256 KiB record represented by one logical commit and hundreds of physical Memvid chunks
- active/tombstoned state after cache deletion, including legacy sidecar migration
- repair of a self-consistent but tampered JSON cache from authoritative `.mv2` envelopes
- context-frame metadata round trip
- run-capsule `complete.mv2` summary reads, preferring exact indexed records before search snippets
- isolated memory directories for golden eval cases to avoid `.mv2` lock contention

Use `python -m pytest` so fixture subprocesses inherit the same interpreter and installed extras.

## Upgrade Checklist

Before changing the Memvid SDK version:

1. Inspect `create`, `use`, `put`, `find`, `timeline`, `frame`, `seal`, `verify`, `doctor`, `stats`, and `close` signatures.
2. Confirm existing files are opened with `use(...)`, not recreated.
3. Confirm lexical-first writes do not require an embedding API key.
4. Confirm `frame(uri)` returns the full canonical event metadata and `timeline()` can enumerate beyond 100 frames without URI ambiguity.
5. Confirm `find()` result metadata is still normalized into `MemoryHit`.
6. Run the gated Memvid integration tests, including sidecar deletion and tombstone replay.
7. Run golden evals with both memory and Memvid backends when possible, plus the live-learning Memvid harness for release-provider validation.
