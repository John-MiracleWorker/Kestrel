# Memory Operations

Kestrel production memory is Memvid v2 `.mv2` storage. Do not replace it with SQLite, Postgres, Chroma, QR/video-frame v1 behavior, or ad hoc JSON as the primary memory database.

## Layout

One permanent `.mv2` file is kept per layer:

```text
.nest/memory/working.mv2
.nest/memory/episodic.mv2
.nest/memory/semantic.mv2
.nest/memory/procedural.mv2
.nest/memory/policy.mv2
```

Run capsules live separately under `.nest/runs/{run_id}/complete.mv2`.

## Verify and Doctor

```bash
nest-agent memory verify --backend memvid --memory-dir .nest/memory
nest-agent memory doctor --backend memvid --memory-dir .nest/memory
```

`doctor` is dry-run by default. Only pass `--repair` after preserving a backup.

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

Working memory can be compacted or promoted through the nested learning pipeline, but semantic/procedural/policy layers require validation evidence. Policy memory requires explicit configuration and explicit user instruction.
