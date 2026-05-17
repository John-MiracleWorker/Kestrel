# Deployment Guide

Kestrel is a local-first agent runtime. The deployment default is intentionally conservative:
Memvid `.mv2` memory, localhost binding, no shell/file-write/policy/Codex high-risk tools enabled, and no automatic consolidation writes. The `mock` provider appears in smoke checks because it needs no secrets and is deterministic; configure a real provider for normal operation.

## One-Shot GitHub Install

For a local Memvid-backed Kestrel install:

```bash
curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/main/install.sh | bash
```

The installer clones or updates `https://github.com/John-MiracleWorker/Kestrel.git` into `${KESTREL_HOME:-$HOME/.kestrel-agent}`, detects Python 3.11 or newer, creates `.venv`, installs `.[memvid,openai,server,mcp,dev]`, runs `npm ci --prefix web`, builds the web workbench, initializes `.nest/memory` with Memvid `.mv2` layers, verifies memory, and runs doctor plus a deterministic `mock` chat smoke check. The smoke check proves the CLI path without requiring secrets; it is not the recommended provider for real use.

Useful options:

```bash
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_HOME="$HOME/dev/kestrel" bash install.sh
KESTREL_REF=main bash install.sh
KESTREL_SKIP_WEB=1 bash install.sh
KESTREL_SKIP_SMOKE=1 bash install.sh
KESTREL_START_SERVER=1 KESTREL_PORT=8765 bash install.sh
```

The installer refuses to overwrite a non-git nonempty target directory. It keeps the same safe runtime defaults as the rest of the repo: Memvid backend, localhost server commands, no provider secrets collected, and high-risk shell/file-write/policy/Codex/plugin/git remote mutation flags disabled.

After install, run with the provider you actually want to use:

```bash
cd "${KESTREL_HOME:-$HOME/.kestrel-agent}"
.venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider codex-cli --model gpt-5.5
OPENAI_API_KEY=... .venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider openai --model gpt-5.5
.venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider openai-compatible --base-url http://127.0.0.1:1234/v1 --model local-model
```

The local workbench can start with any configured provider. `mock` remains useful only as a smoke-test fallback:

```bash
.venv/bin/nest-agent server --backend memvid --memory-dir .nest/memory --provider mock --model mock --host 127.0.0.1 --port 8765
```

## Fresh Clone Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[memvid,openai,server,mcp,dev]'
npm install --prefix web
npm run build --prefix web
```

Smoke check:

```bash
nest-agent doctor --backend memory --provider mock
nest-agent chat --backend memory --provider mock --message "hello"
```

Memvid setup:

```bash
nest-agent init --backend memvid --memory-dir .nest/memory
nest-agent memory verify --backend memvid --memory-dir .nest/memory
```

## Local Server

```bash
nest-agent server \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider mock \
  --host 127.0.0.1 \
  --port 8765
```

Open `http://127.0.0.1:8765/`.

For OpenAI:

```bash
export OPENAI_API_KEY=...
nest-agent server \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider openai \
  --model gpt-5.5 \
  --host 127.0.0.1 \
  --port 8765
```

Use the model name available to the deployment account.

## Docker

Build:

```bash
docker build -t kestrel-agent:local .
```

Run doctor in the image:

```bash
docker run --rm kestrel-agent:local \
  nest-agent doctor --backend memory --memory-dir /tmp/kestrel-memory --provider mock
```

Run the server:

```bash
docker run --rm \
  -p 127.0.0.1:8765:8765 \
  -v kestrel-data:/data \
  -e NEST_AGENT_API_TOKEN='replace-with-local-secret' \
  -e OPENAI_API_KEY \
  kestrel-agent:local
```

The Docker image defaults to:

```text
backend=memvid
memory_dir=/data/memory
log_dir=/data/logs
state_path=/data/state/agent.db
secret_store_path=/data/secrets/local_vault.json
allow_shell=false
allow_file_write=false
allow_policy_writes=false
allow_codex_cli=false
allow_plugin_install=false
allow_git_commit=false
allow_git_push=false
allow_remote_mutation=false
git_write_mode=local_branch
protected_branches=main,master,release/*
allow_memory_import=false
allow_executable_skills=false
allow_mcp_network_endpoints=false
require_api_auth=true
enable_auto_consolidation=false
```

The container command binds to `0.0.0.0` inside Docker, so the image requires API auth by default. Set `NEST_AGENT_API_TOKEN` for `docker run` and `docker compose`; startup fails before serving if a non-loopback bind is requested without a configured token.

When `require_api_auth=true`, the browser shell remains public so operators can load `/`, `/assets/*`, and client-side routes. All `/api/*` routes still require the token. The web app prompts for the token after a 401, stores it in browser local storage, and sends it as `Authorization: Bearer <token>` on API requests.

## Docker Compose

Copy `.env.example` to `.env`, fill provider keys only where needed, then:

```bash
docker compose up --build
```

Compose binds to `127.0.0.1:8765`, requires `NEST_AGENT_API_TOKEN`, and stores memory/state/logs in the `kestrel-data` volume.

## Provider Setup

Mock mode needs no secrets and is the default for tests.

OpenAI:

```bash
export OPENAI_API_KEY=...
nest-agent chat --backend memvid --memory-dir .nest/memory --provider openai --model gpt-5.5 --message "hello"
```

OpenAI-compatible local servers:

```bash
nest-agent chat \
  --backend memvid \
  --memory-dir .nest/memory \
  --provider openai-compatible \
  --base-url http://127.0.0.1:1234/v1 \
  --model local-model \
  --message "hello"
```

If the endpoint requires a key, set it in the environment and pass `--api-key-env NAME`.

## Runtime Checks

Use these before exposing the runtime to a new operator:

```bash
nest-agent doctor --backend memvid --memory-dir .nest/memory
nest-agent memory verify --backend memvid --memory-dir .nest/memory
python scripts/run_golden_evals.py --backend memory --provider mock
```

Integration checks remain opt-in:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
```

Use `python -m pytest` so integration subprocesses inherit the same interpreter, environment, and installed extras.
