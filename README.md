# Kestrel

Kestrel is a self-hosted AI agent platform split into four main services:

- `Gateway`: Node.js/TypeScript API, auth, websocket, and channel routing layer
- `Brain`: Python gRPC reasoning, planning, memory, and task orchestration service
- `Hands`: Python gRPC sandbox executor backed by Docker containers
- `Web`: React/Vite frontend for chat, tasks, settings, and operator workflows

The repo is a monorepo with npm workspaces under `packages/`.

## Architecture

- `packages/gateway`: Fastify + gRPC bridge + channel ingress
- `packages/brain`: Core agent runtime, persistence, providers, and automation
- `packages/hands`: Sandboxed tool execution and audit receipts
- `packages/web`: Browser UI
- `packages/desktop`: Tauri wrapper for the web UI
- `packages/shared`: Shared protobuf definitions and generated code

Default local ports:

- Web UI: `http://localhost:5173`
- Gateway: `http://localhost:8741`
- Brain gRPC: `localhost:50051`
- Hands gRPC: `localhost:50052`

## Quick Start

### Docker-first

1. Copy the environment template:

```bash
cp .env.example .env
```

2. Start the full stack:

```bash
docker compose up -d --build
```

3. Open the web UI:

```text
http://localhost:5173
```

This path brings up PostgreSQL, Redis, Gateway, Brain, Hands, and the frontend together.

## Native Development

Prerequisites:

- Node.js `18+`
- npm `9+`
- Python `3.11+`
- Docker Desktop or Docker Engine
- PostgreSQL `16`
- Redis `7`

Recommended bootstrap:

1. Install workspace dependencies:

```bash
npm install
```

2. Create the repo-local Python environment at the repository root:

```bash
python -m venv venv
```

3. Install Python dependencies into that root environment:

Windows:

```powershell
venv\Scripts\python.exe -m pip install -r packages/brain/requirements.txt
venv\Scripts\python.exe -m pip install -r packages/hands/requirements.txt
```

macOS/Linux:

```bash
venv/bin/python -m pip install -r packages/brain/requirements.txt
venv/bin/python -m pip install -r packages/hands/requirements.txt
```

4. Copy `.env.example` to `.env` and fill in the values you need.

5. Start supporting services. The simplest hybrid path is:

```bash
docker compose up -d postgres redis
```

If you want Hands to stay containerized during native development, start it too:

```bash
docker compose up -d postgres redis hands
```

6. Start the application services in separate terminals:

```bash
npm run dev --workspace=@kestrel/gateway
npm run dev --workspace=@kestrel/web
```

Then start Brain and optionally Hands with the repo venv active, or with the venv interpreter directly.

Example on Windows:

```powershell
cd packages/brain
..\..\venv\Scripts\python.exe server.py
```

```powershell
cd packages/hands
..\..\venv\Scripts\python.exe server.py
```

Example on macOS/Linux:

```bash
cd packages/brain
../../venv/bin/python server.py
```

```bash
cd packages/hands
../../venv/bin/python server.py
```

Notes:

- Hands still requires a working Docker daemon even when the Hands service itself runs natively, because sandbox execution is container-backed.
- The root validation scripts resolve Python tooling from the repo-local `venv/` automatically.
- In PowerShell, `npm` may be blocked by execution policy because it resolves to `npm.ps1`. Use `npm.cmd` instead when needed.

## Validation

Root validation commands:

Other shells:

```bash
npm run test
npm run typecheck
npm run lint
```

PowerShell on Windows:

```powershell
npm.cmd run test
npm.cmd run typecheck
npm.cmd run lint
```

Package-level examples:

```bash
npm run test --workspace=@kestrel/gateway
npm run typecheck --workspace=@kestrel/brain
npm run lint --workspace=@kestrel/hands
```

## Additional Docs

- `SETUP.md`: end-to-end setup details
- `docs/platform-capabilities.md`: current product surface and maturity
- `docs/service-ownership.md`: service boundaries and ownership
- `docs/channel-support-matrix.md`: channel-by-channel support status
- `docs/runtime-flags.md`: feature and runtime toggles
- `docs/desktop-first-migration.md`: native or hybrid startup profile
