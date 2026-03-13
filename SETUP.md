# Kestrel Setup Guide

This guide covers the supported ways to run the current monorepo. The old `setup.sh`, `start.sh`, `frontend/`, and `server:app` flow is no longer the current project layout.

## Choose a Run Mode

- Docker-first: easiest way to get the full stack running
- Native or hybrid: best for local development on a shared checkout

## Prerequisites

| Requirement | Recommended version | Notes                                                        |
| ----------- | ------------------- | ------------------------------------------------------------ |
| Node.js     | 18+                 | Required for npm workspaces, Gateway, Web, and Desktop       |
| npm         | 9+                  | Root scripts use npm workspaces                              |
| Python      | 3.11+               | Brain and Hands development/runtime                          |
| Docker      | Current             | Required for compose, Hands sandboxing, and hybrid workflows |
| PostgreSQL  | 16                  | Compose uses `pgvector/pgvector:pg16`                        |
| Redis       | 7                   | Required by Gateway and Brain                                |

## 1. Clone And Configure

```bash
git clone https://github.com/John-MiracleWorker/Kestrel.git
cd Kestrel
cp .env.example .env
```

Fill in `.env` with the provider keys and host values you need. The Docker defaults are enough for a local first run.

## 2. Install Dependencies

### Node workspace dependencies

```bash
npm install
```

### Python dependencies

The repository now expects a root virtual environment at `venv/` for validation commands.

Windows:

```powershell
python -m venv venv
venv\Scripts\python.exe -m pip install -r packages/brain/requirements.txt
venv\Scripts\python.exe -m pip install -r packages/hands/requirements.txt
```

macOS/Linux:

```bash
python3 -m venv venv
venv/bin/python -m pip install -r packages/brain/requirements.txt
venv/bin/python -m pip install -r packages/hands/requirements.txt
```

## 3. Start The Stack

### Option A: Docker-first

```bash
docker compose up -d --build
```

Services and default ports:

- Frontend: `http://localhost:5173`
- Gateway: `http://localhost:8741`
- Brain gRPC: `localhost:50051`
- Hands gRPC: `localhost:50052`
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`

### Option B: Native or Hybrid Development

Start the backing services first:

```bash
docker compose up -d postgres redis
```

Recommended service startup:

Terminal 1:

```bash
npm run dev --workspace=@kestrel/gateway
```

Terminal 2:

```bash
npm run dev --workspace=@kestrel/web
```

Terminal 3:

Windows:

```powershell
cd packages/brain
..\..\venv\Scripts\python.exe server.py
```

macOS/Linux:

```bash
cd packages/brain
../../venv/bin/python server.py
```

Terminal 4, optional if you are not running Hands in Docker:

Windows:

```powershell
cd packages/hands
..\..\venv\Scripts\python.exe server.py
```

macOS/Linux:

```bash
cd packages/hands
../../venv/bin/python server.py
```

Important notes:

- Hands requires Docker even when launched natively, because sandbox execution is container-based.
- If you prefer a documented desktop/hybrid profile, use `docs/desktop-first-migration.md`.

## 4. Verify Repo Health

Root checks:

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

Those commands run through the workspace packages and resolve Python tools from the repo-local `venv/`.

## 5. Common Issues

### PowerShell blocks `npm`

If `npm` resolves to `npm.ps1` and execution policy blocks it, use `npm.cmd` instead:

```powershell
npm.cmd run test
```

### Brain tests fail on import

Install the Brain dependencies into the root `venv/`. The validation scripts do not assume global `pytest`, `mypy`, or `ruff`.

### Hands starts but sandbox actions fail

Check that Docker is running and that the configured sandbox image can be built or pulled. Hands itself is not enough; the executor launches Docker containers for each sandboxed action.

### Native services cannot reach each other

Check `.env` and the service host values:

- Gateway talks to Brain on `BRAIN_GRPC_HOST:BRAIN_GRPC_PORT`
- Brain talks to Hands on `HANDS_GRPC_HOST:HANDS_GRPC_PORT`
- Gateway and Brain both need PostgreSQL and Redis

## Reference

- `README.md`
- `docs/platform-capabilities.md`
- `docs/service-ownership.md`
- `docs/channel-support-matrix.md`
- `docs/runtime-flags.md`
- `docs/desktop-first-migration.md`
