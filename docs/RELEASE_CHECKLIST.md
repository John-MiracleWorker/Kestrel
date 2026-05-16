# Alpha Release Checklist

Use this before tagging or publishing an alpha build.

## Core Validation

```bash
python -m compileall -q src tests scripts
python -m ruff check scripts src tests
python -m mypy src
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
npm run test --prefix web
npm run build --prefix web
```

## Optional Integration Validation

Run when dependencies and local credentials are available:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
```

## Packaging Validation

```bash
python -m pip install -e '.[memvid,openai,server,mcp,dev]'
nest-agent doctor --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "packaging smoke"
docker build -t kestrel-agent:local .
docker run --rm kestrel-agent:local nest-agent doctor --backend memory --memory-dir /tmp/kestrel-memory --provider mock
```

## Documentation Checks

- `.env.example` documents provider keys and safety flags.
- `docs/DEPLOYMENT.md` covers local, Docker, Compose, provider, and local model setup.
- `docs/MEMORY_OPERATIONS.md` covers backup, restore, verification, and migration without recreating existing `.mv2` files.
- `docs/SECURITY.md` keeps dangerous tool enablement explicit.

## Release Gate

Do not tag the release if any of these are true:

- Core validation fails.
- Golden evals regress.
- Memvid verification fails for a production memory directory.
- High-risk tools are enabled by default.
- `.mv2` memory is replaced by another primary memory store.
- Policy memory can be written from one ordinary event.
