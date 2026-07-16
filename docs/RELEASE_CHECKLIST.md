# Release Candidate Checklist

Use this before tagging or publishing the supported single-user, single-node local/private build. The authoritative acceptance criteria are in `docs/PRODUCTION_OPERATIONS.md`; every command must run against the exact candidate bytes.

## Core Validation

```bash
python -m compileall -q src tests scripts
python -m ruff check scripts src tests
python -m mypy src
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
npm run test --prefix web
npm audit --audit-level=high --prefix web
npm run build --prefix web
bandit -q -r src -lll -iii
shellcheck install.sh scripts/*.sh
bash -n install.sh scripts/*.sh
git diff --check
```

## Optional Integration Validation

Run when dependencies and local credentials are available:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_memory_system.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memory --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memory
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memvid --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memvid
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memory --output-root /tmp/kestrel-live-learning-memory
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
```

## Packaging Validation

```bash
python -m build
# Validate wheel/sdist metadata, built web assets, and an isolated wheel install.
python -m pip check
nest-agent doctor --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "packaging smoke"
bash -n install.sh
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_DRY_RUN=1 KESTREL_START_SERVER=0 bash install.sh
docker build -t kestrel-agent:local .
docker run --rm kestrel-agent:local nest-agent doctor --backend memory --memory-dir /tmp/kestrel-memory --provider mock
```

Optional one-shot installer smoke from a local repo clone:

```bash
RUN_MEMVID_INTEGRATION=1 RUN_INSTALLER_INTEGRATION=1 python -m pytest -q tests/test_install_script.py::test_install_from_local_repo_smoke_with_memvid
```

Run the authenticated mock-provider soak command from `docs/PRODUCTION_OPERATIONS.md`, plus restart recovery and backup/restore drills. Audit the fully pinned release dependency set and require zero known vulnerabilities.

## Documentation Checks

- `.env.example` documents provider keys and safety flags.
- `README.md` exposes the public GitHub curl installer.
- `docs/DEPLOYMENT.md` covers one-shot, local, Docker, Compose, provider, and local model setup.
- `docs/MEMORY_OPERATIONS.md` covers backup, restore, verification, and migration without recreating existing `.mv2` files.
- `docs/SECURITY.md` keeps dangerous tool enablement explicit.
- `docs/CONTROLLED_SELF_MODIFICATION.md` documents behavior-delta gates, review, replay, rollback, and live-learning boundaries.

## Release Gate

Do not tag the release if any of these are true:

- Core validation fails.
- Golden evals or live-learning E2E checks regress on the selected release provider path.
- Memvid verification fails for a production memory directory.
- High-risk tools are enabled by default.
- `.mv2` memory is replaced by another primary memory store.
- Policy memory can be written from one ordinary event.
- The exact candidate has not passed Linux, macOS, and Windows CI on supported Python versions.
- Wheel/sdist metadata, isolated install, packaged web assets, dependency audit, chaos recovery, or bounded soak has not passed.
- Independent review is incomplete, the worktree is dirty, or the tag/version/publication action is not deliberate.
