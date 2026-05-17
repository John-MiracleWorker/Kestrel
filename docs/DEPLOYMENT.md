# Deployment Guide

Kestrel is a local-first agent runtime. The deployment default is intentionally conservative:
Memvid `.mv2` memory, mock provider, localhost binding, no shell/file-write/policy/Codex high-risk tools enabled, and no automatic consolidation writes.

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
