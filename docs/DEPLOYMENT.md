# Deployment Guide

Kestrel is a local-first agent runtime. The deployment default is intentionally conservative:
Memvid `.mv2` memory, localhost binding, no shell/file-write/policy/Codex high-risk tools enabled, and no automatic consolidation writes. The `mock` provider appears in smoke checks because it needs no secrets and is deterministic; configure a real provider for normal operation.

If a deployment enables agent-invoked test, lint, repair-validation, or `codex.exec` gates, it must
also configure a purpose-built, preloaded `NEST_AGENT_VALIDATION_CONTAINER_IMAGE` using an immutable
`name@sha256:<64 hex>` reference. Those tools execute only against a private networkless,
credential-free, read-only OCI snapshot; there is no host fallback and the runtime uses
`--pull=never`. Run `nest-agent doctor` after changing these gates. Direct operator maintenance
commands are separate from agent tool execution.

`v0.3.1` is the current stable release for the supported local/private deployment profile. This
checkout is the unreleased `v0.4.0` development line.

## One-Shot GitHub Install

The one-shot Bash installer supports Intel/Apple-silicon macOS and Linux x86_64,
including x86_64 Linux inside WSL. It deliberately rejects Linux `aarch64`/`arm64`;
use the published `linux/arm64` container image for `v0.4.0` and later. Until that
workflow succeeds, `v0.3.1` remains current and no `v0.4.0` GHCR artifact is claimed.
Native Windows users should install the wheel instead of using Git Bash or the Windows
`bash.exe` launcher. The exact downloaded wheel is release-gated on native Windows
3.11, Linux x86_64 with Python 3.11, 3.12, and 3.13, and both Apple-silicon and Intel
macOS with every supported Python version. The matrix asserts `platform.machine()` in
each lane so a hosted-runner architecture or label drift cannot silently reduce that
coverage.

For a local Memvid-backed Kestrel install:

```bash
curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.3.1/install.sh | bash
```

The release installer clones or updates `https://github.com/John-MiracleWorker/Kestrel.git` into `${KESTREL_HOME:-$HOME/.kestrel-agent}`, requires Python 3.11, 3.12, or 3.13, creates `.venv`, installs the published release payload, initializes `.nest/memory` with Memvid `.mv2` layers, verifies memory, and runs doctor plus a deterministic `mock` chat smoke check. For a safer first install, it does not start the server or open a browser unless explicitly enabled. The smoke check proves the CLI path without requiring secrets; it is not the recommended provider for real use. The hash-locked wheel, OS-keyring extra, immutable-commit installer binding, and strengthened packaging path described below apply to the unreleased `v0.4.0` candidate and must not be treated as published until its release workflow succeeds.

Production installs should use the immutable GitHub release installer above. The moving `main`
branch and this `v0.4.0` candidate remain development sources, not the published release channel.

The Compose profile runs the image as UID/GID 999 with a read-only root filesystem, all Linux
capabilities dropped, `no-new-privileges`, and a bounded `noexec,nosuid` temporary filesystem. Only
the named `/data` volume is persistent and writable. The image also strips SUID bits from unused
login and mount helpers.

The GitHub release also publishes the universal wheel, source distribution,
version-pinned installer, hash-locked default dependency manifest,
CycloneDX SBOM, and `SHA256SUMS`. The dependency audit and SBOM are generated
from the same isolated wheel environment described by `requirements-release.txt`,
not from the development/test environment.

For `v0.4.0` and later, download the complete payload and verify both its GitHub
provenance and its internal identity before installing it:

```bash
mkdir kestrel-release-v0.4.0
gh release download v0.4.0 --repo John-MiracleWorker/Kestrel --dir kestrel-release-v0.4.0
for artifact in kestrel-release-v0.4.0/*; do
  gh attestation verify "$artifact" --repo John-MiracleWorker/Kestrel
done
python scripts/verify_release_payload.py kestrel-release-v0.4.0 --expected-version v0.4.0
```

`gh attestation verify` binds each downloaded artifact to the repository's GitHub
Actions provenance. The payload verifier then checks complete `SHA256SUMS` coverage,
the expected distribution/version in both filenames and package metadata, and the
matching CycloneDX component. Checksums and attestations identify the released bytes;
Kestrel does not claim bit-for-bit reproducible rebuilds. Debian package-index inputs
and frontend build tooling remain mutable availability/output inputs even though base
images, Python bootstrap artifacts, actions, and JavaScript dependency integrity are pinned.

To install and explicitly launch the localhost workbench in one command:

```bash
curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.3.1/install.sh | KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash
```

Useful options:

```bash
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_HOME="$HOME/dev/kestrel" bash install.sh
# Development/source installer only; staged release installers reject ref overrides:
KESTREL_REF=main bash install.sh
KESTREL_SKIP_WEB=1 bash install.sh
KESTREL_SKIP_SMOKE=1 bash install.sh
KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash install.sh
KESTREL_OPEN_BROWSER=0 KESTREL_PORT=8766 bash install.sh
KESTREL_SERVER_SESSION=kestrel-agent-dev bash install.sh
```

The installer refuses to overwrite a non-git nonempty target directory or update an existing Kestrel checkout with tracked or untracked changes. Development/source mode can fetch an explicitly requested source while preserving the operator's `origin` remote. A staged release installer rejects `KESTREL_REPO` and `KESTREL_REF` overrides, checks the fetched commit against the immutable embedded release SHA before any checkout script runs, and fails on a moved tag or mismatched repository. Both modes use a non-forced detached checkout that refuses to overwrite ignored files and retain the safe runtime defaults: Memvid backend, localhost server commands, no provider secrets collected, and high-risk shell/file-write/policy/Codex/plugin/git remote mutation flags disabled.

An opt-in detached server writes logs to `${KESTREL_HOME:-$HOME/.kestrel-agent}/.nest/server.log`. Stop it with:

```bash
kill "$(cat "$HOME/.kestrel-agent/.nest/server.pid")"
screen -S kestrel-agent -X quit 2>/dev/null || true
```

After install, run with the provider you actually want to use:

```bash
cd "${KESTREL_HOME:-$HOME/.kestrel-agent}"
.venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider codex-cli --model gpt-5.5
OPENAI_API_KEY=... .venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider openai --model gpt-5.5
.venv/bin/nest-agent chat --backend memvid --memory-dir .nest/memory --provider openai-compatible --base-url http://127.0.0.1:1234/v1 --model local-model
```

When installer launch is enabled, the local workbench starts with `mock`. The workbench can also start manually with any configured provider; `mock` remains useful only as a smoke-test fallback:

```bash
.venv/bin/nest-agent server --backend memvid --memory-dir .nest/memory --provider mock --model mock --host 127.0.0.1 --port 8765
```

## Fresh Clone Install

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes --only-binary=:all: -r config/python-build-bootstrap.txt
python -m pip install --no-build-isolation -e '.[memvid,openai,anthropic,gemini,server,mcp,keyring,dev]'
npm ci --prefix web
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

The release and development commands above include the dedicated `keyring` extra; custom minimal installs must add `.[keyring]` explicitly. That extra installs the cross-platform client, not an OS credential service. macOS and Windows normally supply a native keychain. Linux needs an unlocked Secret Service implementation in the Kestrel user's session. Before storing secrets, start Kestrel with `NEST_AGENT_SECRET_BACKEND=keyring` (or `--secret-backend keyring`); it fails closed if no usable backend is available. Never switch a populated JSON vault in place. Choose a new empty metadata path, rotate and re-enter every secret into the keyring backend, validate those references, and only then securely retire the old raw vault.

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

For a published `v0.4.0` or later release, the Linux ARM64 installer fallback is the
public GHCR image. The same tag is a two-platform index for `linux/amd64` and
`linux/arm64`:

```bash
docker pull --platform linux/arm64 ghcr.io/john-miracleworker/kestrel:v0.4.0
docker run --rm --platform linux/arm64 \
  ghcr.io/john-miracleworker/kestrel:v0.4.0 \
  nest-agent doctor --backend memvid --memory-dir /tmp/kestrel-doctor --provider mock
```

Release CI also publishes `ghcr.io/john-miracleworker/kestrel:sha-<full-GitHub-SHA>`.
It transfers the already executed and vulnerability-scanned per-platform images into
the publication job, verifies their source/version/revision labels, publishes the
multi-platform index only after every release gate, and composes that index from the
registry-returned per-platform push digests rather than mutable architecture tags. It
then anonymously pulls both platform digests and runs `nest-agent doctor` after
publication. The version and commit tags must resolve to the same attested index
digest. For unattended deployments, inspect that index and pin
`ghcr.io/john-miracleworker/kestrel@sha256:<index-digest>` rather than relying on a
mutable tag. This binds deployment to the released bytes and source commit; it does
not assert that a separate rebuild will be bit-for-bit identical.

Run doctor in the image:

```bash
docker run --rm kestrel-agent:local \
  nest-agent doctor --backend memvid --memory-dir /data/memory --provider mock
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
secret_backend=json
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

The container command binds to `0.0.0.0` inside Docker, so the image requires API auth by default. Set `NEST_AGENT_API_TOKEN` for `docker run` and `docker compose`; startup fails before serving if a non-loopback bind is requested without a configured token. Provider, model, backend, and storage paths come from the `NEST_AGENT_*` environment values instead of being overridden by the image command. The entrypoint initializes only missing Memvid v2 layers before starting the server, and the container health check uses `/api/health/ready` for traffic admission. The image contains the `keyring` client for package parity but deliberately defaults to the owner-private JSON vault. A stock headless container has no OS keychain service, so `NEST_AGENT_SECRET_BACKEND=keyring` fails closed unless the operator explicitly provides a compatible, authenticated external keyring session; package installation alone is not sufficient.

Docker builds validate a real `memvid_sdk` import before producing an image. Every Docker architecture compiles Memvid v2 from its exact hash-verified source distribution in a throwaway build stage; this also avoids an upstream `2.0.160` ARM64 wheel that cannot currently be loaded safely in a standard Python container. A separate build stage uses the same pinned `uv==0.11.16` toolchain as release CI to export the frozen `uv.lock` runtime graph, and the final stage installs that graph with `pip --require-hashes` before installing Kestrel without dependency resolution. The final image contains the resulting native wheel and locked runtime dependencies but none of the Rust or `uv` build toolchain, and image construction fails if the native runtime is not usable.

When `require_api_auth=true`, the browser shell remains public so operators can load `/`, `/assets/*`, and client-side routes. All `/api/*` routes still require the token. The web app prompts for the token after a 401, stores it in browser local storage, and sends it as `Authorization: Bearer REDACTED` on API requests.

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
