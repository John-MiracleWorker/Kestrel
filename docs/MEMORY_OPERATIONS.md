# Memory Operations

Kestrel production memory is Memvid v2 `.mv2` storage. Do not replace it with SQLite, Postgres, Chroma, QR/video-frame v1 behavior, or ad hoc JSON as the primary memory database.

## Layout

One permanent `.mv2` file is kept per layer:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
.nest/memory/self.mv2
.nest/memory/policy.mv2
```

Run capsules live separately under `.nest/runs/{run_id}/complete.mv2`.

## Verify and Doctor

```bash
nest-agent memory verify --backend memvid --memory-dir .nest/memory
nest-agent memory doctor --backend memvid --memory-dir .nest/memory
```

`doctor` is dry-run by default. Only pass `--repair` after preserving a backup.

## Corrections

Use correction frames instead of overwriting durable facts in place:

```bash
nest-agent memory correct <target_record_id> "Corrected memory text" --backend memvid --memory-dir .nest/memory
```

The command writes a `correction` frame, links it to the target record, tombstones the superseded record, and leaves normal retrieval filtering inactive records out by default. Use inspect/search with inactive/audit options when reviewing tombstones.

## Compaction

Compaction is dry-run unless `--apply` is passed:

```bash
nest-agent memory compact --layer working --backend memvid --memory-dir .nest/memory
nest-agent memory compact --layer episodic --apply --backend memvid --memory-dir .nest/memory
```

TTL compaction only targets working and episodic layers by default. Stable layers are skipped except for correction-driven tombstones. Automatic compaction is off unless `NEST_AGENT_ENABLE_AUTO_COMPACT=1`; it still runs dry-run unless `NEST_AGENT_AUTO_COMPACT_APPLY=1`.

Compaction records `never_retrieved` promotion outcomes for promoted records that are summarized away before any `last_retrieved_at` write-back. Normal retrieval updates `last_retrieved_at` at most once per hour per hit, so this is a coarse operator signal rather than a complete retrieval log.

## Promotion Ledger

Use the ledger to inspect whether past promotion decisions held up:

```bash
nest-agent memory ledger
nest-agent memory ledger --since 7d --layer procedural
nest-agent memory ledger --outcome corrected
nest-agent memory ledger --json
```

The ledger lives in the existing AgentStateStore SQLite database at `.nest/state/agent.db` by default. It records the promotion decision separately from `.mv2` memory content and appends outcomes for corrections, contradictions, tombstones, supersession, useful confirmations, and never-retrieved compaction.

Recommendations in this command are deterministic heuristics only. Kestrel never edits thresholds automatically.

## Layer Config And Hybrid Search

`--layer-config` / `NEST_AGENT_LAYER_CONFIG` can load a JSON layer spec file. Hybrid/vector retrieval is only enabled when the layer explicitly provides local vector settings:

```json
{
  "semantic": {
    "search_mode": "hybrid",
    "vector": {
      "enabled": true,
      "embedding_provider": "local",
      "embedding_model": "all-MiniLM-L6-v2",
      "index_path": "semantic.mv2.vector.sqlite"
    }
  }
}
```

Hybrid retrieval rank-fuses exact `.mv2` lexical hits with local vector-sidecar hits. `mode=lex` bypasses vectors. `mode=vector` searches only the sidecar. Policy memory remains lexical-only even if vector fields are present.

Local embeddings require the optional `sentence-transformers` dependency, plus `vector.embedding_provider: "local"` and a `vector.index_path` in the layer config. Sidecars are SQLite files stored beside the `.mv2` layers, contain embeddings keyed by `.mv2` record ID and content hash, and do not store raw memory text.

Inspect and rebuild sidecars with:

```bash
nest-agent memory vector status --backend memvid --memory-dir .nest/memory --layer-config .nest/config/layers.json
nest-agent memory vector rebuild --backend memvid --memory-dir .nest/memory --layer-config .nest/config/layers.json --layer semantic
```

Procedural lesson recall asks for hybrid retrieval when local vector settings are available, then falls back to lexical record iteration when they are not.

## Backup

Stop the server or pause writes first, then copy the complete runtime directory:

```bash
tar -czf kestrel-backup-$(date +%Y%m%d-%H%M%S).tgz .nest/memory .nest/state .nest/logs .nest/config
```

For Docker Compose:

```bash
docker compose stop
docker run --rm -v kestrel-data:/data -v "$PWD:/backup" alpine \
  tar -czf /backup/kestrel-data-backup.tgz /data
docker compose up -d
```

## Restore

Restore the full memory directory and state database together when possible:

```bash
tar -xzf kestrel-backup.tgz
nest-agent memory verify --backend memvid --memory-dir .nest/memory
nest-agent doctor --backend memvid --memory-dir .nest/memory
```

If only `.mv2` files are restored, run a fresh `doctor` and expect run history/state views to be incomplete.

## Migration

For a path migration, copy files without recreating them:

```bash
mkdir -p /new/kestrel/memory
cp .nest/memory/*.mv2 /new/kestrel/memory/
nest-agent memory verify --backend memvid --memory-dir /new/kestrel/memory
```

Never call `create(path)` on an existing `.mv2` file. The backend must use existing files through the safe open/use path.

## Retention Notes

Working memory can be compacted or promoted through the nested learning pipeline, but semantic/procedural/self/policy layers require structured validation evidence. Policy memory requires explicit configuration, repeated evidence, high validation, and explicit user instruction.

## Safe Write Paths

`memory.write` is a direct-write tool for volatile layers only: `working` and `episodic`.

Stable layers must use paths that preserve validation, provenance, confidence, and approval metadata:

- Use `memory.learn` for validated semantic/procedural promotion.
- Use `self.remember` for validated Soul/self memory.
- Use `memory.correct` to correct existing stable records without overwriting history.
- Use approval-gated `memory.import` or an admin path for migrations and bulk restoration.

Policy writes are stricter than other stable writes. They remain disabled unless `allow_policy_writes` is explicitly enabled, and even then direct `memory.write` does not write policy records; use the nested-learning or admin path so policy evidence, repeat count, explicit instruction, and approval gates stay intact.

Near-miss promotions use `promotion_status: provisional`. Provisional records are visible to retrieval, have half retention, and cannot be promoted further until later full-threshold evidence confirms them.
