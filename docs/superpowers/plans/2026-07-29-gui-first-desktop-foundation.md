# GUI-First Desktop Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a hardened Electron shell and a frozen-runtime-compatible desktop sidecar contract so Kestrel can launch, authenticate, supervise, and stop its authoritative local runtime without Python, Node.js, a terminal, or renderer-held secrets.

**Architecture:** The Electron main process owns the native window and one verified child sidecar. The Python sidecar remains the only authoritative Kestrel runtime and binds an operating-system-assigned loopback port. A private profile lease prevents concurrent writers, a nonce-bound readiness contract proves the launched child, and a minimal preload bridge exposes only reviewed native operations. The existing browser-served Workbench and CLI remain supported, but Desktop never depends on either a shell or a development checkout.

**Tech Stack:** Electron 43.2.0, TypeScript 5.9.3, Zod 4.4.3, Vitest 4.1.6, React 19.2.1 renderer assets, Python 3.11, FastAPI, Uvicorn, PyInstaller-compatible Python entrypoint, existing Kestrel `AgentStateStore`, `ServiceController`, `RuntimeSettingsStore`, `SecretBroker`, and Memvid v2 backends.

## Global Constraints

- Work in a clean feature worktree derived from the integration branch selected in the [program index](2026-07-29-gui-first-flock-program-index.md). Never implement this in the dirty primary checkout.
- Preserve the local/private/single-owner boundary. This plan does not add remote web serving, accounts, tenants, or multi-user authorization.
- Keep FastAPI and the Python runtime authoritative. Electron must not reimplement projects, runs, tools, approvals, routing, memory, or policy.
- Use Memvid v2 `.mv2` files only. Keep exactly one permanent `.mv2` file for each of `working`, `episodic`, `semantic`, `procedural`, `self`, and `policy`, plus separately stored run capsules.
- Reopen an existing `.mv2`; never call `create(path)` when that path exists.
- Keep SQLite as the control plane. Runtime launch records and tokens are ephemeral launcher metadata, not canonical memory.
- Never place the API token, provider secrets, channel secrets, MCP secrets, raw credential values, or signing material in renderer storage, React state, URLs, command-line arguments, logs, support bundles, or Memvid.
- Keep `nodeIntegration: false`, `contextIsolation: true`, `sandbox: true`, a restrictive CSP, blocked navigation/new windows, and a narrow schema-validated preload surface.
- Do not kill a process based only on a PID or port. A managed process must match child handle, launch nonce, executable digest, profile lease, listener, and readiness evidence.
- Preserve CLI compatibility. `kestrel` and `nest-agent` must use the same profile lease and application services.
- Keep mocks deterministic and inject clocks, random token generators, process launchers, and HTTP clients in tests.
- Run focused tests after every red/green step and `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q` after each numbered phase.
- Put Memvid SDK integration checks behind `RUN_MEMVID_INTEGRATION=1`.
- Do not publish installers in this plan. Native packaging, signing, update, rollback, and clean-machine qualification are handled by the packaging plan.

---

## Phase 1: Establish the Desktop Source Boundary

### Task 1: Add a pinned Electron main/preload workspace

**Files:**

- Create: `desktop/package.json`
- Create: `desktop/package-lock.json`
- Create: `desktop/tsconfig.json`
- Create: `desktop/tsconfig.build.json`
- Create: `desktop/vitest.config.ts`
- Create: `desktop/src/contracts.ts`
- Create: `desktop/src/contracts.test.ts`
- Create: `desktop/src/build-boundary.test.ts`
- Modify: `.gitignore`
- Modify: `.github/workflows/ci.yml`
- Modify: `tests/test_packaging_deployment.py`

**Interfaces:**

- Consume: no renderer or Electron runtime payload in this task; Task 5 later
  consumes bundled renderer output from `web/dist`.
- Produce: only compiled `desktop/dist/contracts.js` and
  `desktop/dist/contracts.d.ts`.
- Produce: `DesktopBridge`, `DesktopConnection`, `DesktopLifecycleState`, and `DesktopRecoveryReason` TypeScript contracts.
- Invariant: production dependencies are exact-pinned in `desktop/package-lock.json`; no remote renderer dependency is loaded at runtime.

- [ ] **Step 1: Write the failing metadata and contract tests**

Add a Python repository test:

```python
def test_desktop_workspace_is_exact_pinned_and_stages_entrypoints() -> None:
    package = json.loads((ROOT / "desktop" / "package.json").read_text())
    assert package["private"] is True
    assert "main" not in package
    assert package["dependencies"] == {
        "electron-updater": "6.8.9",
        "zod": "4.4.3",
    }
    assert package["devDependencies"]["electron"] == "43.2.0"
    assert package["devDependencies"]["@electron/fuses"] == "2.1.3"
    assert package["devDependencies"]["vitest"] == "4.1.6"
    assert "react" not in package["dependencies"]
```

Add a build-boundary test that runs `npm run build` from a clean `desktop/dist`
and requires exactly `contracts.js` and `contracts.d.ts`; it must reject an
emitted main entrypoint, preload entrypoint, renderer asset, or React runtime.

Add `desktop/src/contracts.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { desktopConnectionSchema } from "./contracts";

describe("desktopConnectionSchema", () => {
  it("rejects a token-bearing renderer payload", () => {
    expect(() =>
      desktopConnectionSchema.parse({
        state: "ready",
        baseUrl: "http://127.0.0.1:43123",
        apiToken: "must-never-cross"
      })
    ).toThrow();
  });
});
```

- [ ] **Step 2: Run tests and verify the intended failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q tests/test_packaging_deployment.py
npm --prefix desktop test
```

Expected: Python fails because `desktop/package.json` is absent; npm fails because the desktop workspace is absent.

- [ ] **Step 3: Create the minimal workspace and strict contracts**

Pin these exact packages:

```json
{
  "dependencies": {
    "electron-updater": "6.8.9",
    "zod": "4.4.3"
  },
  "devDependencies": {
    "@electron/fuses": "2.1.3",
    "@types/node": "25.8.0",
    "electron": "43.2.0",
    "typescript": "5.9.3",
    "vitest": "4.1.6"
  }
}
```

Define `desktopConnectionSchema` with only:

```ts
export const desktopConnectionSchema = z.object({
  state: z.enum(["starting", "ready", "recovery"]),
  baseUrl: z.string().url().refine(isLoopbackHttpUrl),
  profileId: z.string().min(1).max(120),
  sidecarVersion: z.string().min(1).max(64),
  recovery: desktopRecoverySchema.nullable()
}).strict();
```

Add npm scripts for `build`, `test`, `test:typecheck`, and `licenses:check`.
Do not set `package.json.main`: Task 5 owns the real Electron main entrypoint.
Keep `desktop/tsconfig.json` as the strict no-emit typecheck configuration for
source and tests. Create `desktop/tsconfig.build.json` extending it with emit
enabled, declarations enabled, `dist` as `outDir`, `src` as `rootDir`, only
`src/contracts.ts` included, and tests excluded. Make `npm run build` use this
emitting configuration. Add `desktop/node_modules`, `desktop/dist`, and
`desktop/release` to `.gitignore`. Extend CI with `npm ci`, typecheck, tests,
audit, and build for this workspace.

- [ ] **Step 4: Run tests and build**

Run:

```bash
npm --prefix desktop ci
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q tests/test_packaging_deployment.py
```

Expected: all pass and `desktop/dist` contains exactly `contracts.js` and
`contracts.d.ts`; no main/preload entrypoint, renderer asset, or runtime
dependency is emitted.

- [ ] **Step 5: Commit**

```bash
git add desktop .gitignore .github/workflows/ci.yml tests/test_packaging_deployment.py
git commit -m "build: establish pinned desktop workspace"
```

### Task 2: Define the bounded sidecar bootstrap and readiness contract

**Files:**

- Create: `src/nested_memvid_agent/desktop_bootstrap.py`
- Create: `src/nested_memvid_agent/server_desktop_routes.py`
- Create: `tests/test_desktop_bootstrap.py`
- Create: `tests/test_server_desktop_routes.py`
- Modify: `src/nested_memvid_agent/server.py`

**Interfaces:**

- Consume: one owner-only bootstrap JSON file whose path is the only bootstrap command-line argument.
- Produce: `DesktopLaunchConfig` and a redacted `DesktopReadiness` payload.
- API: authenticated `GET /api/desktop/readiness`.
- Invariant: public readiness returns `launch_nonce_digest`, never the nonce or API token.
- Invariant: bootstrap input is at most 16 KiB, strict-schema, owner-only, non-symlinked, and deleted after successful read.

- [ ] **Step 1: Write failing bootstrap tests**

```python
def test_bootstrap_consumes_private_file_without_leaking_token(tmp_path: Path) -> None:
    path = write_bootstrap_fixture(
        tmp_path,
        token="desktop-secret-token",
        nonce="launch-nonce",
    )
    launch = consume_desktop_bootstrap(path)

    assert launch.api_token == "desktop-secret-token"
    assert not path.exists()
    assert "desktop-secret-token" not in repr(launch)
    assert "launch-nonce" not in launch.to_public_payload()


def test_bootstrap_rejects_symlink_and_oversized_input(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="symlink"):
        consume_desktop_bootstrap(symlink_bootstrap(tmp_path))
    with pytest.raises(ValueError, match="16 KiB"):
        consume_desktop_bootstrap(oversized_bootstrap(tmp_path))
```

Add route assertions:

```python
def test_desktop_readiness_is_auth_and_nonce_digest_bound(client: TestClient) -> None:
    response = client.get(
        "/api/desktop/readiness",
        headers={"Authorization": "Bearer desktop-token"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "schema": "kestrel.desktop.readiness.v1",
        "ready": True,
        "profile_id": "default",
        "launch_nonce_digest": sha256(b"launch-nonce").hexdigest(),
        "sidecar_version": "0.5.0",
        "state_schema_version": 21,
        "routing_schema_version": 2,
        "memory_layers": [
            "working", "episodic", "semantic", "procedural", "self", "policy"
        ],
    }
    assert "desktop-token" not in response.text
    assert "launch-nonce" not in response.text
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_bootstrap.py \
  tests/test_server_desktop_routes.py
```

Expected: import/route failures.

- [ ] **Step 3: Implement the strict dataclasses and route**

Define:

```python
@dataclass(frozen=True)
class DesktopLaunchConfig:
    profile_id: str
    profile_root: Path
    state_path: Path
    memory_dir: Path
    runtime_settings_path: Path
    launch_nonce: str = field(repr=False)
    api_token: str = field(repr=False)
    parent_pid: int
    parent_birth_marker: str
    resource_manifest_digest: str
```

Validate resolved paths under `profile_root`; require exactly the six default `DEFAULT_LAYER_SPECS`; reject extra keys; use constant-time comparisons for nonce-bound verification. Register desktop routes only when `create_app` receives a desktop launch context. Browser/CLI server mode must not claim Desktop ownership or expose a fake Desktop readiness result.

- [ ] **Step 4: Run focused and full Python tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_bootstrap.py \
  tests/test_server_desktop_routes.py \
  tests/test_server_security_headers.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/desktop_bootstrap.py \
  src/nested_memvid_agent/server_desktop_routes.py \
  src/nested_memvid_agent/server.py \
  tests/test_desktop_bootstrap.py \
  tests/test_server_desktop_routes.py
git commit -m "feat: add nonce-bound desktop bootstrap contract"
```

### Task 3: Add a portable, single-writer runtime profile lease

**Files:**

- Create: `src/nested_memvid_agent/runtime_profile_lease.py`
- Create: `tests/test_runtime_profile_lease.py`
- Modify: `src/nested_memvid_agent/cli.py`
- Modify: `src/nested_memvid_agent/service_control.py`
- Modify: `src/nested_memvid_agent/server_client.py`
- Modify: `tests/test_runtime_ownership.py`
- Modify: `tests/test_service_control.py`
- Modify: `tests/test_server_client.py`

**Interfaces:**

- Produce: `RuntimeProfileLease.acquire()`, `.inspect()`, `.release()`, and `RuntimeLeaseConflict`.
- Lease payload: schema, profile ID, management kind (`desktop` or `cli`), owner UID/SID digest, PID, process birth marker, executable digest, launch nonce digest, base URL, version, and created time.
- Invariant: the OS file lock is authority; JSON metadata is evidence only.
- Invariant: stale or unverifiable metadata never authorizes process termination.

- [ ] **Step 1: Write failing race and coexistence tests**

```python
def test_only_one_writer_can_acquire_profile_lease(tmp_path: Path) -> None:
    first = RuntimeProfileLease.acquire(tmp_path, fixture_identity("desktop"))
    with pytest.raises(RuntimeLeaseConflict) as raised:
        RuntimeProfileLease.acquire(tmp_path, fixture_identity("cli"))
    assert raised.value.current.management == "desktop"
    first.release()


def test_stale_metadata_is_reported_but_not_treated_as_kill_authority(
    tmp_path: Path,
) -> None:
    write_unlocked_lease_metadata(tmp_path, pid=4242)
    state = RuntimeProfileLease.inspect(tmp_path, inspector=missing_process_inspector)
    assert state.status == "stale_unverified"
    assert state.can_terminate is False
```

Add a CLI server test that a Desktop-held profile exits with `profile_owned_by_desktop`, and a Desktop attach test that a verified compatible Desktop lease is attachable.

- [ ] **Step 2: Verify tests fail**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_runtime_profile_lease.py \
  tests/test_runtime_ownership.py \
  tests/test_service_control.py \
  tests/test_server_client.py
```

Expected: missing lease implementation and old concurrent-start behavior.

- [ ] **Step 3: Implement the lease and wire every server entrypoint**

Use the existing cross-process `file_lock` primitives. Persist metadata with the existing owner-only private artifact helpers. Acquire before `create_app()` opens SQLite or Memvid and release only after lifespan shutdown closes all writers.

Add these compatibility results:

```python
LeaseDisposition = Literal[
    "available",
    "attach_desktop",
    "offer_desktop_takeover",
    "version_conflict",
    "stale_unverified",
    "foreign_or_unrelated",
]
```

`ServiceController.stop()` may stop only its existing verified managed service. It must not use the new lease metadata as a substitute for its current process/listener verification. `KestrelServerClient` gains a read-only compatibility probe that checks profile ID and version without exposing the token.

- [ ] **Step 4: Exercise revision/race behavior**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_runtime_profile_lease.py \
  tests/test_runtime_ownership.py \
  tests/test_service_control.py \
  tests/test_launcher.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: one writer, safe attach/takeover explanations, and no regressions in launcher ownership tests.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/runtime_profile_lease.py \
  src/nested_memvid_agent/cli.py \
  src/nested_memvid_agent/service_control.py \
  src/nested_memvid_agent/server_client.py \
  tests/test_runtime_profile_lease.py \
  tests/test_runtime_ownership.py \
  tests/test_service_control.py \
  tests/test_server_client.py
git commit -m "feat: enforce portable runtime profile leases"
```

---

## Phase 2: Build the Sidecar Entrypoint and Lifecycle

### Task 4: Add a frozen-runtime-compatible sidecar entrypoint with port zero

**Files:**

- Create: `src/nested_memvid_agent/desktop_sidecar.py`
- Create: `tests/test_desktop_sidecar.py`
- Modify: `pyproject.toml`
- Modify: `src/nested_memvid_agent/server.py`
- Modify: `tests/test_packaging_deployment.py`

**Interfaces:**

- Executable entrypoint: `kestrel-desktop-sidecar <bootstrap-path>`.
- Consume: strict bootstrap path only; no token, nonce, project path, state path, or memory path on the command line.
- Produce: atomic owner-only readiness record with PID, birth marker, port, profile ID, version, executable digest, manifest digest, and nonce digest.
- Invariant: bind a pre-created socket to `127.0.0.1:0`; never race by selecting a port and closing it before Uvicorn starts.

- [ ] **Step 1: Write failing socket and artifact tests**

```python
def test_sidecar_serves_on_the_same_os_assigned_socket(tmp_path: Path) -> None:
    harness = DesktopSidecarHarness(tmp_path)
    result = harness.start()
    assert result.host == "127.0.0.1"
    assert 1 <= result.port <= 65535
    assert result.uvicorn_socket_fileno == result.bound_socket_fileno


def test_sidecar_reopens_existing_six_mv2_files(tmp_path: Path) -> None:
    memory_dir = seed_six_mv2_files(tmp_path)
    backend = RecordingMemvidBackend()
    run_desktop_sidecar_preflight(memory_dir, backend_factory=lambda _: backend)
    assert backend.opened == expected_six_layer_paths(memory_dir)
    assert backend.created == []
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_sidecar.py \
  tests/test_memvid_backend_contract.py
```

Expected: entrypoint and harness imports fail.

- [ ] **Step 3: Implement the entrypoint**

Use:

```python
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", 0))
sock.listen(config.backlog)
server = uvicorn.Server(uvicorn.Config(app, access_log=False, lifespan="on"))
await server.serve(sockets=[sock])
```

Perform, in order: consume bootstrap; verify parent identity and packaged manifest binding; acquire profile lease; prepare private directories; distinguish missing from existing memory files; build `create_app(config, desktop_context=...)`; bind the socket; write readiness atomically; serve; delete readiness on clean shutdown; release lease last.

Add the console script:

```toml
kestrel-desktop-sidecar = "nested_memvid_agent.desktop_sidecar:main"
```

- [ ] **Step 4: Run deterministic and gated Memvid tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_sidecar.py \
  tests/test_memvid_backend_contract.py \
  tests/test_private_artifact_permissions.py
RUN_MEMVID_INTEGRATION=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/integration/test_memvid_backend_integration.py \
  tests/integration/test_memvid_memory_system.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass; the integration test creates only missing `.mv2` files and reopens them on the second start.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/desktop_sidecar.py \
  src/nested_memvid_agent/server.py \
  pyproject.toml \
  tests/test_desktop_sidecar.py \
  tests/test_packaging_deployment.py
git commit -m "feat: add desktop sidecar entrypoint"
```

### Task 5: Create the secure Electron window and private app protocol

**Files:**

- Create: `desktop/src/main.ts`
- Create: `desktop/src/main/window.ts`
- Create: `desktop/src/main/security.ts`
- Create: `desktop/src/main/protocol.ts`
- Create: `desktop/src/main/window.test.ts`
- Create: `desktop/src/main/security.test.ts`
- Create: `desktop/src/main/protocol.test.ts`
- Create: `desktop/resources/csp.txt`
- Modify: `desktop/src/build-boundary.test.ts`
- Modify: `desktop/package.json`
- Modify: `desktop/tsconfig.build.json`
- Modify: `desktop/src/contracts.ts`

**Interfaces:**

- Produce: one single-instance `BrowserWindow` loading `kestrel://app/index.html`.
- Consume: packaged `web/dist` directory resolved relative to `process.resourcesPath`.
- Build ownership: add `src/main.ts` and `src/main/**/*.ts` to the emitting
  build, then set `package.json.main` to `dist/main.js` only after the hardened
  main process exists.
- Invariant: no `file://`, remote page, `<webview>`, arbitrary navigation, arbitrary external URL, or unreviewed permission.

- [ ] **Step 1: Write failing security assertions**

```ts
it("constructs every renderer with the mandatory boundary", () => {
  expect(windowOptions().webPreferences).toMatchObject({
    nodeIntegration: false,
    contextIsolation: true,
    sandbox: true,
    webSecurity: true
  });
  expect(windowOptions().webPreferences).not.toHaveProperty("webviewTag", true);
});

it("blocks navigation and new windows", () => {
  expect(navigationDecision("kestrel://app/mission")).toBe("allow");
  expect(navigationDecision("https://example.com")).toBe("deny");
  expect(windowOpenDecision("https://example.com")).toEqual({ action: "deny" });
});

it("serves only normalized files beneath the renderer root", async () => {
  await expect(resolveAppAsset("../secrets.json", rendererRoot)).rejects.toThrow();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix desktop test -- window security protocol
```

Expected: missing modules.

- [ ] **Step 3: Implement the minimum secure shell**

Register `kestrel` as a privileged standard/secure scheme before `app.ready`. Resolve paths with a containment check after URL decoding. Return a restrictive CSP equivalent to:

```text
default-src 'none';
script-src 'self';
style-src 'self';
font-src 'self';
img-src 'self' data: blob:;
connect-src http://127.0.0.1:*;
object-src 'none';
base-uri 'none';
form-action 'none';
frame-ancestors 'none';
```

Deny every session permission request. Prevent navigation except same-app routes. Keep external URL opening behind a later explicit, allowlisted IPC action; do not call `shell.openExternal` from clicked content.

Extend `desktop/tsconfig.build.json` to emit the reviewed main-process source
alongside contracts, and add `package.json.main` only for the real emitted
`dist/main.js`; do not introduce a placeholder entrypoint.

- [ ] **Step 4: Test and typecheck**

Run:

```bash
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run build
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/main.ts desktop/src/main desktop/resources/csp.txt \
  desktop/src/contracts.ts desktop/package.json desktop/tsconfig.build.json
git commit -m "feat: harden desktop renderer boundary"
```

### Task 6: Implement resource verification and child supervision

**Files:**

- Create: `desktop/src/main/resource-manifest.ts`
- Create: `desktop/src/main/resource-manifest.test.ts`
- Create: `desktop/src/main/sidecar-supervisor.ts`
- Create: `desktop/src/main/sidecar-supervisor.test.ts`
- Create: `desktop/src/main/private-files.ts`
- Create: `desktop/src/main/private-files.test.ts`
- Create: `desktop/src/testing/fake-sidecar.ts`
- Modify: `desktop/src/main.ts`
- Modify: `desktop/src/contracts.ts`

**Interfaces:**

- Consume: `resources/kestrel-resource-manifest.json`, detached manifest signature, embedded verification public key, frozen sidecar executable, and renderer assets.
- Produce: lifecycle states `verifying`, `starting`, `ready`, `stopping`, `restarting`, `recovery`.
- Produce: exactly one bounded automatic restart after an unexpected exit.
- Invariant: no restart after an ambiguous high-risk call; the authoritative sidecar reports reconciliation blockers before the renderer resumes.

- [ ] **Step 1: Write failing manifest and crash-loop tests**

```ts
it("refuses a sidecar whose digest differs from the signed manifest", async () => {
  await expect(verifier.verify(tamperedResources)).rejects.toMatchObject({
    code: "resource_digest_mismatch"
  });
  expect(spawn).not.toHaveBeenCalled();
});

it("restarts once and then enters recovery", async () => {
  await supervisor.start();
  fakeChildren[0].exit(17);
  await eventually(() => expect(spawn).toHaveBeenCalledTimes(2));
  fakeChildren[1].exit(17);
  await eventually(() => expect(supervisor.state.kind).toBe("recovery"));
  expect(spawn).toHaveBeenCalledTimes(2);
});

it("never kills a conflicting listener it did not spawn", async () => {
  await expect(supervisor.startWith(conflictingLease)).rejects.toThrow(
    "foreign_or_unrelated"
  );
  expect(kill).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix desktop test -- resource-manifest sidecar-supervisor private-files
```

Expected: missing supervisor implementation.

- [ ] **Step 3: Implement the supervisor state machine**

The start sequence must be:

1. verify manifest signature and every required resource digest;
2. resolve and validate the private profile directory;
3. inspect the profile lease;
4. create a mode-`0600`/owner-ACL bootstrap file with a 256-bit token and nonce;
5. spawn the sidecar with only the bootstrap path;
6. wait for the bounded readiness file;
7. validate PID, child handle, executable digest, manifest digest, profile, and nonce digest;
8. call authenticated `/api/desktop/readiness`;
9. transition to `ready`.

Use `child_process.spawn` with `shell: false`, `detached: false`, an explicit environment allowlist, piped stdio, and no inherited secrets. Redact and bound stdout/stderr lines before logging. On normal exit, request authenticated graceful shutdown, wait a bounded interval, then terminate only the retained verified child handle.

- [ ] **Step 4: Run adversarial tests**

Run:

```bash
npm --prefix desktop test
npm --prefix desktop run test:typecheck
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_bootstrap.py \
  tests/test_desktop_sidecar.py \
  tests/test_runtime_profile_lease.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: tampering, stale files, nonce mismatch, PID reuse simulation, token leak fixtures, crash loops, and unrelated listeners all fail closed.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/main desktop/src/testing desktop/src/contracts.ts desktop/src/main.ts
git commit -m "feat: supervise verified desktop sidecar"
```

---

## Phase 3: Connect the Renderer Without Exposing Authority

### Task 7: Inject loopback authentication in the main process

**Files:**

- Create: `desktop/src/main/api-session.ts`
- Create: `desktop/src/main/api-session.test.ts`
- Create: `web/src/platform/runtimeTransport.ts`
- Create: `web/src/platform/runtimeTransport.test.ts`
- Modify: `web/src/api.ts`
- Modify: `web/src/auth.ts`
- Modify: `web/src/types.ts`
- Modify: `web/src/App.test.tsx`

**Interfaces:**

- Consume: sidecar base URL and token held only by the Electron main process.
- Produce: authenticated fetch/SSE from the bundled renderer to the exact sidecar origin.
- Browser fallback: retain explicit session token support for advanced browser-served Workbench use.
- Invariant: Desktop mode never writes or reads the sidecar token through `sessionStorage`, `localStorage`, DOM, URL, preload result, or renderer logs.

- [ ] **Step 1: Write failing Desktop transport tests**

```ts
it("desktop transport never reads browser token storage", async () => {
  installDesktopRuntime({ baseUrl: "http://127.0.0.1:43123" });
  const getItem = vi.spyOn(Storage.prototype, "getItem");
  await getJson("/api/health");
  expect(getItem).not.toHaveBeenCalled();
  expect(fetch).toHaveBeenCalledWith(
    "http://127.0.0.1:43123/api/health",
    expect.not.objectContaining({
      headers: expect.objectContaining({ Authorization: expect.anything() })
    })
  );
});
```

Main-process tests must prove that authorization is injected only when all of these match: the registered renderer `webContents.id`, initiator `kestrel://app`, exact loopback origin, and active launch generation.

- [ ] **Step 2: Run tests and verify failures**

Run:

```bash
npm --prefix desktop test -- api-session
npm --prefix web test -- runtimeTransport App
```

Expected: Desktop transport and header injection do not exist.

- [ ] **Step 3: Implement the dual transport**

Keep browser token behavior in a `BrowserRuntimeTransport`. Add `DesktopRuntimeTransport` selected only when the frozen preload marker is present. In Electron main, attach the bearer header through the exact session request hook and strip any renderer-supplied `Authorization` header first. Rotate the transport generation whenever the sidecar restarts so old requests cannot authenticate to the new process.

Do not expose a generic Electron IPC fetch primitive. The renderer continues to call the authoritative HTTP API; the main process supplies only connection routing and the hidden header.

- [ ] **Step 4: Run renderer, desktop, and server tests**

Run:

```bash
npm --prefix web test
npm --prefix web run build
npm --prefix desktop test
npm --prefix desktop run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_security_headers.py \
  tests/test_server_client.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: both browser and Desktop transports pass; no token appears in snapshots.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/main/api-session.ts \
  desktop/src/main/api-session.test.ts \
  web/src/platform \
  web/src/api.ts \
  web/src/auth.ts \
  web/src/types.ts \
  web/src/App.test.tsx
git commit -m "feat: authenticate desktop renderer without token exposure"
```

### Task 8: Add a minimal, schema-validated preload bridge

**Files:**

- Create: `desktop/src/preload.ts`
- Create: `desktop/src/preload.test.ts`
- Create: `desktop/src/main/ipc.ts`
- Create: `desktop/src/main/ipc.test.ts`
- Create: `web/src/platform/desktopBridge.ts`
- Create: `web/src/platform/desktopBridge.test.ts`
- Create: `web/src/global.d.ts`
- Modify: `desktop/src/build-boundary.test.ts`
- Modify: `desktop/tsconfig.build.json`
- Modify: `desktop/src/contracts.ts`
- Modify: `desktop/src/main.ts`

**Interfaces:**

- Expose exactly: connection status, select project folder, select storage folder, save support bundle, request credential dialog, request external URL open, app version, update status, and lifecycle/recovery actions.
- Every request and response has a strict Zod schema, a size limit, a sender check, and a stable error code.
- Build ownership: add `src/preload.ts` to the emitting build only after the
  reviewed bridge and schema-validation tests pass.
- Invariant: no generic `invoke(channel, payload)`, filesystem path read/write, process spawn, shell command, environment access, or raw IPC object crosses preload.

- [ ] **Step 1: Write the failing bridge snapshot and fuzz tests**

```ts
it("exports only the reviewed bridge methods", () => {
  expect(Object.keys(exposedBridge).sort()).toEqual([
    "chooseProjectFolder",
    "chooseStorageFolder",
    "connection",
    "exportSupportBundle",
    "getAppVersion",
    "getUpdateStatus",
    "openCredentialDialog",
    "openExternalUrl",
    "performRecoveryAction",
    "subscribeLifecycle",
    "subscribeUpdateStatus"
  ]);
});

it.each(malformedIpcPayloads)("rejects malformed IPC payload %#", async (payload) => {
  await expect(invokeHandler(payload)).rejects.toMatchObject({
    code: "invalid_desktop_request"
  });
});
```

- [ ] **Step 2: Run tests and verify failures**

Run:

```bash
npm --prefix desktop test -- preload ipc
npm --prefix web test -- desktopBridge
```

Expected: bridge modules absent.

- [ ] **Step 3: Implement explicit handlers**

Use `contextBridge.exposeInMainWorld("kestrelDesktop", Object.freeze(...))`. Validate `event.senderFrame.url === "kestrel://app/"` or a normalized same-app route. Native picker results are canonical absolute paths plus a display label; they do not grant generic directory traversal. External URLs require an exact `https:` URL and a host allowlist supplied by the owning feature.

The credential dialog returns only Secret Broker metadata such as `secret://provider-key`, validation state, and fingerprint. The raw value flows from the isolated credential window to Electron main and then directly to the authenticated secret endpoint; it never returns to the primary renderer.

Extend `desktop/tsconfig.build.json` with the reviewed preload entrypoint only
after these tests pass; the resulting `dist/preload.js` is the first preload
output in the sequence.

- [ ] **Step 4: Run security and build gates**

Run:

```bash
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run build
npm --prefix web test
npm --prefix web run build
```

Expected: strict bridge snapshot passes and malformed payloads never reach handler bodies.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/preload.ts desktop/src/preload.test.ts \
  desktop/src/main/ipc.ts desktop/src/main/ipc.test.ts \
  desktop/src/contracts.ts desktop/src/main.ts \
  desktop/tsconfig.build.json \
  web/src/platform/desktopBridge.ts \
  web/src/platform/desktopBridge.test.ts \
  web/src/global.d.ts
git commit -m "feat: expose narrow desktop preload bridge"
```

### Task 9: Implement the isolated credential flow and platform keyring readiness

**Files:**

- Create: `desktop/src/main/credential-window.ts`
- Create: `desktop/src/main/credential-window.test.ts`
- Create: `desktop/src/credential/preload.ts`
- Create: `desktop/src/credential/index.html`
- Create: `desktop/src/credential/form.ts`
- Create: `desktop/src/credential/form.test.ts`
- Modify: `src/nested_memvid_agent/server_secret_routes.py`
- Modify: `src/nested_memvid_agent/secret_broker.py`
- Modify: `src/nested_memvid_agent/server_product_routes.py`
- Modify: `tests/test_server_secret_routes.py`
- Modify: `tests/test_secret_broker.py`
- Modify: `tests/test_product_readiness.py`

**Interfaces:**

- Produce: metadata-only platform credential readiness: `available`, `session_only`, `locked_vault_required`, or `unavailable`.
- Consume: one credential value in a separate sandboxed modal and immediately forward it to the Secret Broker.
- Invariant: raw value is zeroed from owned buffers where practical, never logged, never returned, and never stored in browser storage.
- Linux fallback in this phase is session-only when Secret Service is absent. The passphrase-encrypted persistent fallback belongs to the packaging/recovery plan and must use established authenticated encryption and KDF primitives.

- [ ] **Step 1: Write failing secret-isolation tests**

```ts
it("returns metadata and never the submitted credential", async () => {
  const result = await submitCredential({
    name: "OPENAI_API_KEY",
    purpose: "provider",
    value: "raw-secret"
  });
  expect(result.secretRef).toBe("secret://openai_api_key");
  expect(JSON.stringify(result)).not.toContain("raw-secret");
  expect(primaryRendererMessages()).not.toContainEqual(
    expect.objectContaining({ value: expect.anything() })
  );
});
```

Add Python readiness tests for usable macOS Keychain, Windows Credential Manager, Linux Secret Service, and missing Linux Secret Service.

- [ ] **Step 2: Run tests and verify failures**

Run:

```bash
npm --prefix desktop test -- credential
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_secret_routes.py \
  tests/test_secret_broker.py \
  tests/test_product_readiness.py
```

Expected: no isolated credential window/readiness contract.

- [ ] **Step 3: Implement the bounded flow**

Create the modal only on explicit owner action. Give it the same sandbox/context-isolation/navigation controls as the main window and no access to the main preload. Submit once, close on success, and discard on cancel. Extend setup readiness with metadata-only backend state and remediation. Do not silently choose the existing JSON secret backend for Desktop cloud credentials.

- [ ] **Step 4: Run leak scans and suites**

Run:

```bash
npm --prefix desktop test
npm --prefix desktop run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_server_secret_routes.py \
  tests/test_secret_broker.py \
  tests/test_security_boundary.py \
  tests/test_product_readiness.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: tests pass and sentinel secrets are absent from captured IPC, renderer messages, logs, HTTP responses, and support bundle fixtures.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/main/credential-window.ts \
  desktop/src/main/credential-window.test.ts \
  desktop/src/credential \
  src/nested_memvid_agent/server_secret_routes.py \
  src/nested_memvid_agent/secret_broker.py \
  src/nested_memvid_agent/server_product_routes.py \
  tests/test_server_secret_routes.py \
  tests/test_secret_broker.py \
  tests/test_product_readiness.py
git commit -m "feat: isolate desktop credential entry"
```

---

## Phase 4: Recovery Projection and Developer Artifact Smoke

### Task 10: Add recovery state and reconciliation APIs

**Files:**

- Create: `src/nested_memvid_agent/desktop_recovery.py`
- Create: `src/nested_memvid_agent/server_desktop_recovery_routes.py`
- Create: `tests/test_desktop_recovery.py`
- Create: `tests/test_server_desktop_recovery_routes.py`
- Modify: `src/nested_memvid_agent/server.py`
- Modify: `desktop/src/main/sidecar-supervisor.ts`
- Modify: `desktop/src/contracts.ts`
- Modify: `web/src/types.ts`

**Interfaces:**

- API: `GET /api/desktop/recovery`, `POST /api/desktop/recovery/retry`, and read-only support-bundle preview.
- Recovery reasons: payload verification, profile conflict, state incompatibility, state corruption, Memvid reopen failure, sidecar crash loop, pending ambiguous provider request, and unavailable credential backend.
- Invariant: retry is bounded; restore/move/delete actions are not added until their transactional contracts exist.

- [ ] **Step 1: Write failing recovery tests**

```python
def test_crash_recovery_never_replays_high_risk_or_ambiguous_attempts(
    recovery_service: DesktopRecoveryService,
) -> None:
    report = recovery_service.inspect()
    assert report.can_auto_resume is False
    assert report.blockers == (
        "pending_high_risk_approval",
        "ambiguous_provider_attempt",
    )
    assert report.actions == ("inspect", "export_support_bundle", "retry_readiness")
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_recovery.py \
  tests/test_server_desktop_recovery_routes.py
npm --prefix desktop test -- sidecar-supervisor
```

Expected: recovery service/routes absent.

- [ ] **Step 3: Implement read-only recovery projection**

Compose existing state health, run status, approval status, routing attempt state, memory health, profile lease evidence, and sidecar manifest evidence. Return stable reason codes and friendly remediation. No route may initialize over corrupt state, delete state, or reissue an ambiguous external request.

- [ ] **Step 4: Run recovery and chaos suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_recovery.py \
  tests/test_server_desktop_recovery_routes.py \
  tests/test_chaos_recovery.py \
  tests/test_run_backpressure.py
npm --prefix desktop test
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/desktop_recovery.py \
  src/nested_memvid_agent/server_desktop_recovery_routes.py \
  src/nested_memvid_agent/server.py \
  desktop/src/main/sidecar-supervisor.ts \
  desktop/src/contracts.ts \
  web/src/types.ts \
  tests/test_desktop_recovery.py \
  tests/test_server_desktop_recovery_routes.py
git commit -m "feat: project safe desktop recovery state"
```

### Task 11: Build an unsigned developer bundle and run lifecycle smoke

**Files:**

- Create: `packaging/kestrel-sidecar.spec`
- Create: `scripts/build_desktop_sidecar.py`
- Create: `scripts/generate_desktop_resource_manifest.py`
- Create: `scripts/verify_desktop_resource_manifest.py`
- Create: `tests/test_desktop_build_scripts.py`
- Create: `desktop/scripts/stage-resources.mjs`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**

- Produce: an unsigned, non-release developer app directory containing Electron, `web/dist`, the platform sidecar, license notices, and a test-signed resource manifest.
- Invariant: built output is derived from the exact source tree and is never committed.
- Invariant: developer signatures are rejected by release publication tooling.

- [ ] **Step 1: Write failing build-script tests**

```python
def test_sidecar_spec_collects_memvid_v2_and_all_server_routes() -> None:
    spec = load_sidecar_spec(ROOT / "packaging" / "kestrel-sidecar.spec")
    assert "nested_memvid_agent.desktop_sidecar" in spec.entrypoints
    assert "nested_memvid_agent.web_dist" in spec.datas
    assert "memvid_sdk" in spec.hidden_import_roots
    assert "qrcode" not in spec.hidden_import_roots


def test_resource_manifest_covers_every_staged_file(tmp_path: Path) -> None:
    staged = stage_fixture(tmp_path)
    manifest = generate_manifest(staged, source_commit="a" * 40)
    assert set(manifest["files"]) == set(relative_files(staged))
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q tests/test_desktop_build_scripts.py
```

Expected: build scripts/spec absent.

- [ ] **Step 3: Implement deterministic staging**

Pin `pyinstaller==6.21.0` in the release dependency group and lock it. The sidecar spec must collect FastAPI/Uvicorn, all Kestrel route modules, Memvid v2, keyring backends, deterministic Demo provider assets, timezone data, and bundled web notices. Exclude tests, benchmarks, credentials, `.env*`, local `.nest`, QR/video v1 modules, and development servers.

The manifest contains source commit, app version, platform, architecture, file sizes, SHA-256 digests, Python lock digest, npm lock digests, and SBOM digest. A test-only key may sign developer manifests; production trusts only the release key configured in the packaging plan.

- [ ] **Step 4: Build and smoke the current platform**

Run:

```bash
uv run python scripts/build_desktop_sidecar.py --mode developer
npm --prefix web ci
npm --prefix web run build
npm --prefix desktop ci
npm --prefix desktop run stage:resources
npm --prefix desktop run build:dir
uv run python scripts/verify_desktop_resource_manifest.py \
  desktop/release/current-platform-unpacked/resources
npm --prefix desktop run smoke:dir
```

The smoke must start offline Demo mode, verify six Memvid v2 layer paths, make an authenticated health request, stop cleanly, start again against the same state, and confirm no orphan listener or child remains.

- [ ] **Step 5: Run the phase gate**

Run:

```bash
npm --prefix web test
npm --prefix web run build
npm --prefix desktop test
npm --prefix desktop run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass. This proves source-level Desktop lifecycle only; it does not qualify signed installers or updates.

- [ ] **Step 6: Commit**

```bash
git add packaging/kestrel-sidecar.spec \
  scripts/build_desktop_sidecar.py \
  scripts/generate_desktop_resource_manifest.py \
  scripts/verify_desktop_resource_manifest.py \
  tests/test_desktop_build_scripts.py \
  desktop/scripts/stage-resources.mjs \
  desktop/package.json \
  desktop/package-lock.json \
  pyproject.toml uv.lock .github/workflows/ci.yml
git commit -m "build: produce verifiable desktop developer bundle"
```

---

## Final Verification

- [ ] Run formatting/static/security gates:

```bash
uv run python -m compileall -q src tests scripts
uv run ruff check scripts src tests
uv run mypy src
uv run bandit -q -r src -lll -iii
npm --prefix web run licenses:check
npm --prefix web run test:typecheck
npm --prefix web test
npm --prefix web run build
npm --prefix desktop run licenses:check
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run build
```

- [ ] Run full deterministic Python tests:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

- [ ] Run gated Memvid v2 integration:

```bash
RUN_MEMVID_INTEGRATION=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/integration/test_memvid_backend_integration.py \
  tests/integration/test_memvid_memory_system.py \
  tests/integration/test_memvid_context_frames.py
```

- [ ] Run developer bundle lifecycle smoke twice and verify:

  - the second process attaches or reopens safely;
  - exactly six permanent `.mv2` files exist;
  - no `.mv2` was recreated;
  - no child/listener remains after stop;
  - no raw token or sentinel secret appears in renderer storage, logs, readiness, events, or support-bundle fixtures;
  - an unrelated listener and a CLI-owned profile are left untouched;
  - one crash restarts once, a second crash enters Recovery;
  - pending approvals and ambiguous provider attempts are not replayed.

- [ ] Inspect the diff and ensure the Desktop shell contains no authoritative business logic:

```bash
git diff --check
rg -n "child_process|shell\\.open|nodeIntegration|contextIsolation|sandbox|Authorization" \
  desktop/src web/src
rg -n "create\\(" src/nested_memvid_agent/backends src/nested_memvid_agent/layers.py
git status --short
```

- [ ] Commit any final test-only corrections, rerun the exact full gate at final `HEAD`, and record the commit SHA in the program index.

## Completion Criteria

- Opening the unsigned developer bundle starts one verified sidecar on an OS-assigned loopback port and reaches Mission Command without a shell.
- Desktop and CLI cannot concurrently own the same profile.
- The renderer never receives the API token or raw credential values.
- The sidecar remains the sole implementation of Kestrel authority.
- Existing Memvid v2 files reopen and exactly six permanent layer files remain.
- A tampered sidecar, renderer asset, manifest, bootstrap, lease, nonce, listener, or readiness response fails closed.
- Shutdown is ownership-aware and leaves unrelated processes untouched.
- Source-level tests pass on macOS, Windows, and Linux CI.
- No claim is made yet that a signed installer, updater, or clean-machine artifact is production-qualified.
