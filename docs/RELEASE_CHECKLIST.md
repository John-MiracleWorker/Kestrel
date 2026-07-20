# Release Candidate Checklist

Use this before tagging or publishing the supported single-user, single-node local/private build. The authoritative acceptance criteria are in `docs/PRODUCTION_OPERATIONS.md`; every command must run against the exact candidate bytes.

## Core Validation

```bash
python -m compileall -q src tests scripts
python scripts/check_project_metadata.py
python -m ruff check scripts src tests
python -m mypy src
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
python -m ruff check benchmarks/real_agent_learning_benchmark.py tests/test_real_agent_learning_benchmark.py
MYPYPATH=src python -m mypy --strict benchmarks/real_agent_learning_benchmark.py
python benchmarks/real_agent_learning_benchmark.py --output benchmark_results/agent_learning_gate.json
npm run test --prefix web
npm run licenses:check --prefix web
npm audit --audit-level=high --prefix web
npm run build --prefix web
bandit -q -r src -lll -iii
gitleaks git --redact=100 .
shellcheck install.sh scripts/*.sh
bash -n install.sh scripts/*.sh
git diff --check
```

## Foundational Integration Validation

These Memvid v2, stdio MCP, and executable-skill OCI fixtures require no provider credentials. They are required before tagging and run in pull-request/branch CI. The OCI gate requires Docker and the exact pre-pulled image:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_memory_system.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
docker pull 'python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
RUN_EXTENSION_SANDBOX_INTEGRATION=1 \
KESTREL_EXTENSION_TEST_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3' \
python -m pytest -q tests/integration/test_extension_container_integration.py
```

## Optional Live-Provider Validation

Run the cases for each provider claimed by the release when credentials and endpoints are available:

```bash
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memory --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memory
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memvid --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memvid
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memory --output-root /tmp/kestrel-live-learning-memory
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
```

## Packaging Validation

```bash
npm ci --prefix web
npm run licenses:check --prefix web
npm run build --prefix web
python scripts/stage_web_release.py
rm -rf dist
python -m build --outdir dist
# Require THIRD_PARTY_NOTICES.txt in both the packaged web_dist and license metadata.
python -m zipfile -l dist/*.whl | grep 'nested_memvid_agent/web_dist/THIRD_PARTY_NOTICES.txt'
tar -tzf dist/*.tar.gz | grep '/web/public/THIRD_PARTY_NOTICES.txt'
# Validate wheel/sdist metadata, built web assets, and an isolated wheel install.
python -m pip check
nest-agent doctor --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "packaging smoke"
bash -n install.sh
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_DRY_RUN=1 KESTREL_START_SERVER=0 bash install.sh
docker build -t kestrel-agent:local .
docker run --rm kestrel-agent:local nest-agent doctor --backend memvid --memory-dir /data/memory --provider mock
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
- Root `SECURITY.md` names an enabled confidential vulnerability-reporting channel.
- GitHub secret scanning and push protection are enabled for the public repository.
- `main` branch protection requires the cross-platform CI checks and disallows force pushes and
  deletion outside an explicit incident-recovery procedure.
- `docs/CONTROLLED_SELF_MODIFICATION.md` documents behavior-delta gates, review, replay, rollback, and live-learning boundaries.

## Release Gate

Do not tag the release if any of these are true:

- Core validation fails.
- Credential-free Memvid v2, stdio MCP, or executable-skill OCI containment integration fails or skips in enabled mode.
- Python and private web release metadata do not agree.
- The deterministic end-to-end agent learning gate, golden evals, or live-learning E2E checks regress.
- Memvid verification fails for a production memory directory.
- High-risk tools are enabled by default.
- `.mv2` memory is replaced by another primary memory store.
- Policy memory can be written from one ordinary event.
- The exact candidate has not passed Linux, macOS, and Windows CI on supported Python versions.
- The release tag is not on `main`, or the exact tag bytes have not passed the release workflow's
  cross-platform matrix before publication.
- Wheel/sdist metadata, isolated install, packaged web assets, dependency audit, chaos recovery, or bounded soak has not passed.
- The generated production-web third-party notice is stale or absent from either release artifact.
- No enabled confidential vulnerability-reporting channel is available to security researchers.
- A history-aware secret scan reports anything other than explicitly fingerprinted synthetic test
  fixtures, or repository secret scanning/push protection is disabled.
- `main` has no required-check branch protection for the exact candidate workflow.
- Independent review is incomplete, the worktree is dirty, or the tag/version/publication action is not deliberate.
