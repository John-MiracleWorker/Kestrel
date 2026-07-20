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

New working and episodic records receive the configured layer TTL when callers do not provide an earlier explicit expiry. Expired records leave normal retrieval immediately but remain available to inactive/audit retrieval until compaction tombstones them. Each compaction pass processes at most 1,000 oldest candidates and caps its deterministic summary at 12,000 characters; reports expose processed and deferred counts so a backlog is visible and can be drained over repeated passes.

Compaction records `never_retrieved` promotion outcomes for promoted records that are summarized away before any `last_retrieved_at` write-back. Normal retrieval updates `last_retrieved_at` at most once per hour per hit, so this is a coarse operator signal rather than a complete retrieval log.

Results from `memory.search`, `memory.inspect`, `memory.export`, `memory.ledger`, `memory.conflicts`, `context.pack`, and `context.expand` are derived from memory rather than new evidence. Kestrel therefore stores only a hash/size/provenance trace for those tool results, excludes the traces from normal retrieval and context-child expansion, and omits copied payloads from turn summaries. Explicit retrieval with `include_retrieval_artifacts=true` keeps the trace/transcript path auditable. Other tool-result memory is capped at 64,000 characters with a content digest; the run result/capsule remains the full execution record.

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

Every `mv2_file` must be a unique, direct filename ending in `.mv2`. Every enabled local `vector.index_path` must likewise be a unique direct filename that does not conflict with a layer container, exact-record artifact, Memvid lock, or another vector database and its WAL/SHM/journal files. Collision checks use Unicode normalization and case folding so a config that is unsafe on default macOS or Windows filesystems is rejected on every host. Absolute paths, directory components, traversal, and duplicate names are rejected before any file is opened or permission is changed.

The deterministic `memory` backend coordinates same-path instances with a shared per-path state/version lock, refreshes stale search indexes before queries, and serializes snapshot seals with an owner-only OS lock. Separate processes merge distinct record IDs under that lock before atomic snapshot replacement, preventing concurrent test/mock runs from silently dropping each other's records.

Inspect and rebuild sidecars with:

```bash
nest-agent memory vector status --backend memvid --memory-dir .nest/memory --layer-config .nest/config/layers.json
nest-agent memory vector rebuild --backend memvid --memory-dir .nest/memory --layer-config .nest/config/layers.json --layer semantic
```

Procedural lesson recall asks for hybrid retrieval when local vector settings are available, then falls back to lexical record iteration when they are not.

## Backup

Stop Kestrel first, then create a coherent agent backup:

```bash
nest-agent backup create \
  --backend memvid \
  --memory-dir .nest/memory \
  --state-path .nest/state/agent.db \
  --backup-dir .nest/backups/agent
```

Backup and restore mutations acquire the primary-runtime ownership lock before
reading live state and retain it through verification and cleanup. A live server
therefore causes a deterministic refusal before a snapshot, safety backup, or
restore staging path is written. For the memory-only commands, pass the active
`--state-path` whenever it is not the default so the same interlock protects the
correct runtime.

The agent backup takes a SQLite-consistent control-plane snapshot and checksums
the six authoritative memory layers together with task capsules, runtime
configuration, installed skills, and installed plugins. Existing exact-record
JSON caches may be included to speed a restore, but schema-v2 caches are
disposable and are rebuilt from digest-verified `.mv2` events on open. It deliberately
excludes raw Secret Broker values, operational logs, and disposable worker
worktrees. Back up secrets separately through an encrypted/keychain-appropriate
process; do not weaken the Secret Broker boundary by copying its raw JSON vault
into an ordinary archive.

List and verify snapshots before relying on them:

```bash
nest-agent backup list --backup-dir .nest/backups/agent
nest-agent backup verify BACKUP_ID --backup-dir .nest/backups/agent
```

`nest-agent memory backup` remains available for a memory-only snapshot. Use it
only when intentionally excluding run history, ledgers, task capsules, runtime
settings, skills, and plugins.

For Docker Compose:

```bash
docker compose stop
docker run --rm -v kestrel-data:/data -v "$PWD:/backup" alpine \
  tar -czf /backup/kestrel-data-backup.tgz /data
docker compose up -d
```

## Restore

Stop Kestrel, verify the selected snapshot, then restore it explicitly:

```bash
nest-agent backup verify BACKUP_ID --backup-dir .nest/backups/agent
nest-agent backup restore BACKUP_ID \
  --yes \
  --backend memvid \
  --memory-dir .nest/memory \
  --state-path .nest/state/agent.db \
  --backup-dir .nest/backups/agent
nest-agent doctor --backend memvid --memory-dir .nest/memory
```

Restore stages and validates every component before replacement, creates a
pre-restore safety snapshot, and attempts to roll all changed components back if
a later component swap fails. If rollback itself fails, the command reports the
safety-backup ID and preserves unreinstated rollback artifacts for operator
recovery. If the snapshot used an external layer configuration, restore installs
it at `.nest/config/layers.json` by default; pass `--layer-config` only to choose
another target. The target may be absent on a clean host. When an older or custom
layout stored the active `layers.json` inside the memory directory, a default
clean-host restore also materializes that checksummed file at the external target
so custom `.mv2` filenames remain usable. Secret Broker values are preserved
from the live installation rather than restored from the snapshot.

If only schema-v2 `.mv2` files are restored with `nest-agent memory restore`,
Kestrel rebuilds exact-record caches on open. Run a fresh `doctor` and expect run
history, learning ledgers, capsules, configuration, and extension views to be
incomplete. Preserve a legacy version-one records cache until its first writable
open completes the canonical-envelope migration.

## Migration

For a path migration, copy files without recreating them:

```bash
mkdir -p /new/kestrel/memory
cp .nest/memory/*.mv2 /new/kestrel/memory/
nest-agent memory verify --backend memvid --memory-dir /new/kestrel/memory
```

Copy the matching `.mv2.records.json` too only when the source is a legacy,
not-yet-migrated container. Never call `create(path)` on an existing `.mv2` file.
The backend must use existing files through the safe open/use path.

## Retention Notes

Working memory can be compacted or promoted through the nested learning pipeline, but semantic/procedural/self/policy layers require structured validation evidence. Policy memory requires explicit configuration, repeated evidence, high validation, and explicit user instruction.

## Safe Write Paths

`memory.write` is a direct-write tool for volatile layers only: `working` and `episodic`.

Stable layers must use paths that preserve validation, provenance, confidence, and approval metadata:

- Use `memory.learn` for validated semantic/procedural promotion. It cannot write policy memory.
- Use approval-gated `memory.policy_promote` for policy promotion. First call it with `stage_proposal=true` to create the exact episodic proposal. Run `test.run`, `lint.run`, `repair.validate` (or `repair.orchestrate_validate`), and `repair.review` with that proposal ID as `subject_record_id`; collect at least five distinct authenticated receipt IDs in `task_refs`. A separately approved promotion call must name the proposal as `source_record_id`. Raw repair artifact locators are audit evidence only and cannot be rematerialized to authorize a later policy claim.
- Use `self.remember` for validated Soul/self memory.
- Use `memory.correct` to correct existing stable records without overwriting history.
- Use approval-gated `memory.import` or an admin path for migrations and bulk restoration.

Policy writes are stricter than other stable writes. They remain disabled unless `allow_policy_writes` is explicitly enabled, and even then direct `memory.write`, `memory.learn`, and `memory.consolidate` do not write policy records. Both proposal staging and final `memory.policy_promote` execution require owner approval for their exact arguments. Final evidence receipts are HMAC-authenticated, bound to the proposal and current run, and checked against their declared objective bucket; repair/review receipts additionally retain signed disk-artifact backing. The final approval attestation is cross-checked against durable control-plane state before the record can receive system-role priority. Imported or legacy policy-shaped records remain untrusted recall data unless that check succeeds.

Soul onboarding has a matching prompt-trust boundary. Only the internal onboarding route can add authenticated onboarding provenance. Only its fixed persona preset may affect system-role voice; display names, working style, goals, interests, and communication notes are bounded JSON at user-role priority. A caller cannot obtain system priority by passing `user_confirmed` or forged onboarding source strings to `self.remember`.

Near-miss promotions use `promotion_status: provisional`. Provisional records are visible to retrieval, have half retention, and cannot be promoted further until later full-threshold evidence confirms them.
