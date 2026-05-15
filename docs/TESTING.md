# Testing Guide

## Local tests

```bash
pip install -e .[dev]
pytest
```

These tests use `InMemoryBackend`, so they do not require Memvid.

## Lint/type checks

```bash
ruff check .
mypy src
```

## Memvid integration tests

Install Memvid:

```bash
pip install memvid-sdk
```

Then run:

```bash
python scripts/bootstrap_memory.py --backend memvid --memory-dir ./memory
python scripts/run_validation.py --backend memvid --memory-dir ./memory
nested-memvid search --backend memvid --query "policy promotion"
```

## What Codex should add

- `tests/integration/test_memvid_backend.py`, skipped unless `RUN_MEMVID_INTEGRATION=1`.
- Golden JSON loader.
- Session replay tests once Memvid session APIs are wired.
- Context regression snapshots.
- Latency benchmark script.
