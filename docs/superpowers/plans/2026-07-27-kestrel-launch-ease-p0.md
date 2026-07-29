# Kestrel Launch Ease P0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Kestrel installable, launchable, inspectable, chat-capable, diagnosable, and stoppable through one safe `kestrel` façade while preserving the existing advanced runtime, memory, approval, provenance, and installer guarantees.

**Architecture:** Add three narrowly separated Python modules: `launcher.py` owns presentation and command contracts, `service_control.py` owns loopback process identity and lifecycle, and `server_client.py` owns bounded authenticated HTTP transport. Extend setup readiness with a truthful additive experience mode, then surface it through the launcher and Workbench. Generate transactional per-user launch artifacts from a focused helper and integrate them into the existing release installer without changing its non-starting defaults.

**Tech Stack:** Python 3.11+, argparse, dataclasses, enum, pathlib, urllib, subprocess, existing Kestrel file-lock/private-artifact/platform primitives, pytest, React/TypeScript/Vitest, POSIX shell, plistlib, and macOS ad-hoc codesigning when available.

## Global Constraints

- Work only in the focused worktree and branch created for this feature.
- Keep `nest-agent` and `nested-memvid` fully compatible; `kestrel` is additive.
- Keep schema version 19, `.nest` paths, and every `NEST_AGENT_*` contract unchanged.
- Keep Memvid v2 `.mv2` files, one file per nested layer, and never call `create(path)` for an existing `.mv2`.
- Keep one authoritative server owner for SQLite, approvals, runs, learning, and memory promotion.
- Keep deterministic mock provider behavior and label it **Demo**, never **Ready**.
- Keep high-risk tool enablement, interactive approval, provenance, evidence, confidence, and validation gates unchanged.
- Bind easy launch only to `127.0.0.1`; never auto-adopt, auto-kill, or auto-port-hop around an unknown listener.
- Never print or persist API tokens, credentials, authorization headers, or unsanitized provider payloads.
- Use test-owned temporary paths and processes only in lifecycle tests.
- Add each production behavior by the red-green-refactor sequence: write one focused test, run it and confirm the intended failure, implement the minimum behavior, then rerun the focused test.
- After each task, run the task’s focused suite and `pytest -q` with the project virtual environment and explicit safe `PATH`.
- Keep Memvid integration tests gated behind `RUN_MEMVID_INTEGRATION=1`.
- Do not publish, tag, merge, push, modify credentials, create a login item, or expose a non-loopback service.

---

## Task 1: Add Truthful Experience Modes and Actionable Provider Errors

**Files:**

- Modify: `src/nested_memvid_agent/setup_readiness.py`
- Modify: `src/nested_memvid_agent/llm/resilience.py`
- Modify: `tests/test_product_readiness.py`
- Modify: `tests/test_llm_providers.py`

### 1.1 Add failing readiness-mode tests

- [ ] Extend readiness test fixtures to cover these exact outcomes:

  | Provider state | Expected `experience_mode` |
  | --- | --- |
  | active provider is `mock` | `demo` |
  | non-mock configuration fails | `model_not_connected` |
  | non-mock configuration passes but operational state is unknown, degraded, or open-circuit | `model_not_connected` |
  | non-mock configuration and current-process operational health both pass | `connected` |

- [ ] Assert that `ready` retains its existing prerequisite meaning and that the serialized `/api/product/setup` payload adds, rather than replaces, `experience_mode`.
- [ ] Assert that non-provider failures still win when choosing `next_action`; otherwise Demo gives a Demo-aware action and a disconnected provider gives a Settings recovery action.
- [ ] Run:

  ```bash
  PATH=/Users/tiuni/kestrel/.venv/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src \
  /Users/tiuni/kestrel/.venv/bin/python -m pytest -q \
    tests/test_product_readiness.py
  ```

- [ ] Confirm the tests fail because `experience_mode` does not yet exist.

### 1.2 Implement the additive readiness contract

- [ ] Add:

  ```python
  class ExperienceMode(StrEnum):
      DEMO = "demo"
      MODEL_NOT_CONNECTED = "model_not_connected"
      CONNECTED = "connected"
  ```

- [ ] Add `experience_mode: ExperienceMode` to `SetupReadinessReport`.
- [ ] Compute it only from the active provider’s configuration and current server-process operational check:

  ```python
  if provider == "mock":
      experience_mode = ExperienceMode.DEMO
  elif provider_configuration.status is CheckStatus.PASS and provider_operational.status is CheckStatus.PASS:
      experience_mode = ExperienceMode.CONNECTED
  else:
      experience_mode = ExperienceMode.MODEL_NOT_CONNECTED
  ```

- [ ] Preserve the existing schema identifier and `ready = fail_count == 0`.
- [ ] Choose `next_action` in this order: first non-provider failure, first non-provider warning, Demo explanation, provider Settings recovery, existing all-ready action.
- [ ] Rerun the focused readiness tests and confirm green.

### 1.3 Add failing provider-classification tests

- [ ] Add parametrized tests proving these sanitized classifications:

  ```text
  "api_key is required"                         -> missing_credential
  "missing api-key for provider"                -> missing_credential
  HTTP 401 / unauthorized                       -> authentication
  HTTP 403 / forbidden                          -> authentication
  connection refused / DNS resolution failure   -> endpoint_unreachable
  request timeout                               -> timeout
  "model llama-x not found" / model-specific 404 -> model_unavailable
  rate limit / HTTP 429                         -> rate_limit
  plain "404 page not found"                    -> invalid_request
  unrecognized provider failure                 -> unavailable or provider_error per existing contract
  ```

- [ ] Assert user-facing details include one concrete recovery action but do not echo a sample token placed in the raw exception.
- [ ] Run the focused provider tests and confirm the new cases fail for the expected missing codes.

### 1.4 Implement the classification ordering

- [ ] Update `classify_provider_error` so specific markers are evaluated before generic HTTP markers.
- [ ] Recognize `api_key`, `api-key`, named missing-secret patterns, authentication rejection, endpoint transport errors, model-specific not-found shapes, and rate limits.
- [ ] Preserve the current plain-404 `invalid_request` behavior.
- [ ] Return generic, redacted messages such as:

  ```text
  Provider credential is missing. Store the named secret in Settings or set the configured environment variable.
  Provider authentication failed. Replace or correct the configured credential.
  Provider endpoint is unreachable. Start or correct the endpoint/base URL, then retry.
  Configured model is unavailable. Select or install a valid model, then retry.
  Provider rate limit reached. Wait, reduce concurrency, or choose another configured provider.
  ```

- [ ] Rerun:

  ```bash
  PATH=/Users/tiuni/kestrel/.venv/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src \
  /Users/tiuni/kestrel/.venv/bin/python -m pytest -q \
    tests/test_product_readiness.py tests/test_llm_providers.py
  ```

- [ ] Run the full Python suite, confirm green, and commit:

  ```bash
  git add src/nested_memvid_agent/setup_readiness.py \
    src/nested_memvid_agent/llm/resilience.py \
    tests/test_product_readiness.py tests/test_llm_providers.py
  git commit -m "feat: report truthful provider experience modes"
  ```

---

## Task 2: Build the Bounded Loopback Server Client

**Files:**

- Create: `src/nested_memvid_agent/server_client.py`
- Create: `tests/test_server_client.py`

### 2.1 Specify transport behavior with real local HTTP tests

- [ ] Use a test-owned `ThreadingHTTPServer` bound to `127.0.0.1` to verify:

  - `/api/health` healthy response;
  - missing client token against HTTP 401 is reported as `locked`, not offline;
  - token from the configured environment-variable name is sent as `Authorization: Bearer …`;
  - no token value appears in exceptions or dataclass representations;
  - `/api/runtime/config` and `/api/product/setup` JSON retrieval;
  - `POST /api/runs` sends only expected run fields;
  - polling terminates on `completed`, `failed`, `blocked`, or `cancelled`;
  - polling timeout reports the durable run ID without issuing cancellation;
  - malformed JSON, non-loopback URLs, transport failures, 401, 404, 409, 429, and 5xx errors normalize to stable codes and recoveries.

- [ ] Inject monotonic-clock and sleep callables into polling tests so timeout coverage is deterministic.
- [ ] Run `tests/test_server_client.py` and confirm collection fails because the module is absent.

### 2.2 Implement stable client data contracts

- [ ] Implement:

  ```python
  class ServerClientError(RuntimeError):
      def __init__(
          self,
          message: str,
          *,
          code: str,
          status_code: int | None = None,
          recovery: str,
          run_id: str | None = None,
      ) -> None: ...

  @dataclass(frozen=True)
  class ServerProbe:
      reachable: bool
      healthy: bool
      locked: bool
      detail: str | None = None

  @dataclass(frozen=True)
  class KestrelServerClient:
      base_url: str
      request_timeout_seconds: float = 2.0
      token_env_name: str = "NEST_AGENT_API_TOKEN"
      environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
  ```

- [ ] Validate the base URL with `urllib.parse.urlparse` and `ipaddress.ip_address`; accept only `http` with `localhost` or a loopback IP and no username/password/query/fragment.
- [ ] Resolve the actual token environment-variable name from `NEST_AGENT_API_AUTH_TOKEN_ENV`, defaulting to `NEST_AGENT_API_TOKEN`.
- [ ] Add bounded `_request_json`, `probe`, `get_runtime_config`, `get_setup_readiness`, `create_run`, `get_run`, and `wait_for_run`.
- [ ] Sanitize HTTP response details with the existing redaction helper before constructing errors.
- [ ] Never include headers or the environment mapping in `repr`; mark environment/token-bearing fields `repr=False`.
- [ ] Rerun focused tests, then the full Python suite.
- [ ] Commit:

  ```bash
  git add src/nested_memvid_agent/server_client.py tests/test_server_client.py
  git commit -m "feat: add loopback Kestrel server client"
  ```

---

## Task 3: Implement Safe Installation Resolution and Service Discovery

**Files:**

- Create: `src/nested_memvid_agent/service_control.py`
- Create: `tests/test_service_control.py`
- Read/reuse: `src/nested_memvid_agent/private_artifacts.py`
- Read/reuse: `src/nested_memvid_agent/file_lock.py`
- Read/reuse: `src/nested_memvid_agent/platform_primitives.py`
- Read/reuse: `scripts/installer-server-supervisor.sh`

### 3.1 Add failing path-resolution tests

- [ ] Test exact home precedence:

  1. explicit `--home`;
  2. `KESTREL_HOME`;
  3. an installer-embedded home supplied by the command shim;
  4. a current directory containing `pyproject.toml`, `src/nested_memvid_agent`, and `scripts/installer-server-supervisor.sh`;
  5. `~/.kestrel-agent`.

- [ ] Test canonicalization, missing required installation artifacts, unsafe relative values, invalid ports, and `KESTREL_PORT`.
- [ ] Assert easy-launch URLs always use `127.0.0.1`.
- [ ] Run focused tests and confirm the missing module failure.

### 3.2 Implement immutable lifecycle records

- [ ] Add:

  ```python
  class ServiceState(StrEnum):
      RUNNING = "running"
      STOPPED = "stopped"
      STARTING = "starting"
      CONFLICT = "conflict"

  class ServiceManagement(StrEnum):
      MANAGED = "managed"
      EXTERNAL = "external"
      NONE = "none"

  @dataclass(frozen=True)
  class ServicePaths:
      home: Path
      state_path: Path
      memory_dir: Path
      log_path: Path
      pid_path: Path
      supervisor_pid_path: Path
      pgid_path: Path
      lifecycle_lock_path: Path
      supervisor_script: Path
      server_executable: Path
      host: str
      port: int
      url: str

  @dataclass(frozen=True)
  class ProcessSnapshot:
      pid: int
      uid: int
      cwd: Path
      command: tuple[str, ...]
      pgid: int
      state: str

  @dataclass(frozen=True)
  class ServiceStatus:
      state: ServiceState
      management: ServiceManagement
      url: str
      pid: int | None
      supervisor_pid: int | None
      pgid: int | None
      detail: str
      lifecycle_busy: bool = False
  ```

- [ ] Implement `ServiceControlError(code, recovery)` and a narrow `ProcessInspector` protocol.
- [ ] Implement `resolve_kestrel_home`, `resolve_service_paths`, and strict positive port parsing.
- [ ] Rerun path tests and confirm green.

### 3.3 Add failing process-identity and status tests

- [ ] Drive `ServiceController.status()` with a deterministic fake inspector/client and cover:

  - valid private metadata plus matching current-user server and supervisor -> managed running;
  - matching current-user direct server listener from the canonical home -> external running;
  - live matching supervisor but API not ready -> starting;
  - stale safe metadata with absent processes and no listener -> stopped;
  - unsafe symlink/wrong-owner/permissive/non-numeric metadata -> conflict;
  - reused PID with mismatched cwd, uid, command, port, or args -> conflict;
  - API healthy but listener identity unverifiable -> conflict;
  - unknown listener on the configured port -> conflict;
  - no metadata and bindable port -> stopped;
  - lifecycle lock busy -> read-only state with `lifecycle_busy=True`.

- [ ] Assert status never removes metadata, signals a process, or changes the port.
- [ ] Run and confirm failures occur in missing discovery behavior.

### 3.4 Implement read-only process inspection and identity verification

- [ ] Implement `SystemProcessInspector` with argument-array `subprocess.run` calls only:

  - `ps -o uid=,pgid=,state=,command= -p <pid>`;
  - `/proc/<pid>/cwd` when available, otherwise `lsof -a -p <pid> -d cwd -Fn`;
  - `lsof -nP -iTCP:<port> -sTCP:LISTEN -t`;
  - process-group liveness by enumerating `ps`.

- [ ] Read PID metadata with existing owner-only, regular-file validation. Reject symbolic links, wrong owners, modes broader than `0600`, oversized files, non-digits, zero, and negative values.
- [ ] Verify managed server identity by current uid, canonical cwd, executable path, `server` subcommand, Memvid backend, `.nest/memory`, canonical state path, `127.0.0.1`, and configured port.
- [ ] Verify supervisor identity by current uid, canonical cwd, supervisor script path, and exact metadata/log arguments.
- [ ] Treat a directly launched server as external only when the exact server identity and loopback health both match.
- [ ] Do not fabricate managed metadata for external servers.
- [ ] Rerun service discovery tests and the full Python suite.
- [ ] Commit:

  ```bash
  git add src/nested_memvid_agent/service_control.py tests/test_service_control.py
  git commit -m "feat: discover Kestrel services safely"
  ```

---

## Task 4: Add Serialized Idempotent Start and Verified Stop

**Files:**

- Modify: `src/nested_memvid_agent/service_control.py`
- Modify: `tests/test_service_control.py`

### 4.1 Add failing start tests

- [ ] Cover:

  - healthy managed and verified-external services return unchanged;
  - a verified startup in progress waits for bounded API readiness;
  - two concurrent `start()` calls serialize and launch one supervisor;
  - safe stale metadata is removed only after all recorded identities are proven absent;
  - unknown listener and unsafe metadata refuse launch;
  - startup launches the existing supervisor with absolute safe paths and `start_new_session=True`;
  - successful start requires both matching metadata/process identity and API liveness;
  - readiness timeout cleans up only the exact newly launched verified group;
  - indeterminate ownership preserves evidence and reports an explicit cleanup warning.

- [ ] Add one real-process smoke using a test-owned temporary script and port; never use an existing machine listener.
- [ ] Run focused start tests and confirm they fail because `start()` is absent.

### 4.2 Implement lifecycle locking and start

- [ ] Acquire an exclusive bounded lock on `.nest/server.lifecycle.lock` using existing private-file and file-lock primitives.
- [ ] Within the lock, re-run status before every mutation.
- [ ] Create required private `.nest` directories and log/metadata parents with owner-only permissions.
- [ ] Launch:

  ```text
  bash scripts/installer-server-supervisor.sh
    --pid-file <absolute .nest/server.pid>
    --supervisor-pid-file <absolute .nest/server.supervisor.pid>
    --process-group-file <absolute .nest/server.pgid>
    --log-file <absolute .nest/server.log>
    --
    <absolute .venv/bin/nest-agent> server
      --backend memvid
      --memory-dir <absolute .nest/memory>
      --state-path <absolute state path>
      --provider mock
      --model mock
      --host 127.0.0.1
      --port <configured port>
  ```

- [ ] Use `stdin/stdout/stderr=DEVNULL`, canonical `cwd`, `start_new_session=True`, and no shell interpolation.
- [ ] Poll with bounded monotonic deadlines, verify process identity before accepting health, and return the resulting `ServiceStatus`.
- [ ] On timeout, terminate only the exact process/group created by this call after re-verification.
- [ ] Rerun focused start tests.

### 4.3 Add failing stop tests

- [ ] Cover:

  - stopped is idempotent success;
  - managed stop signals verified supervisor/server group with TERM, waits, re-verifies, then uses KILL only if still matching;
  - external stop signals only the exact verified listener PID;
  - PID reuse between TERM and KILL prevents escalation;
  - unknown listener, mismatched process, unsafe metadata, and ambiguous ownership are refused;
  - metadata is removed only after supervisor, server, group, and listener absence are proven;
  - concurrent stop/open serialization cannot race identity checks.

- [ ] Run focused stop tests and confirm they fail because `stop()` is absent.

### 4.4 Implement verified termination

- [ ] Use existing platform signal helpers and bounded waits.
- [ ] Re-read the snapshot immediately before TERM and immediately before KILL.
- [ ] For managed services, prefer the verified managed process group; for external services, signal only the listener PID.
- [ ] After termination, verify process, group, supervisor, and listener absence before removing matching safe metadata.
- [ ] Preserve metadata and return conflict if absence cannot be proven.
- [ ] Rerun all service-control tests and the full Python suite.
- [ ] Commit:

  ```bash
  git add src/nested_memvid_agent/service_control.py tests/test_service_control.py
  git commit -m "feat: manage Kestrel lifecycle safely"
  ```

---

## Task 5: Add the Product CLI and Packaging Entry Point

**Files:**

- Create: `src/nested_memvid_agent/launcher.py`
- Create: `tests/test_launcher.py`
- Modify: `pyproject.toml`
- Modify: `tests/test_packaging_metadata.py` if present; otherwise add packaging assertions to `tests/test_launcher.py`

### 5.1 Add failing parser and status tests

- [ ] Test `kestrel --help` and parsers for `start`, `stop`, `status`, `open`, `chat`, and `doctor`.
- [ ] Test global `--home` and `--port`, `status --json`, `open --no-browser`, chat positional/`--message` exclusivity, `chat --json`, and bounded wait timeout.
- [ ] Test exit codes: `0` achieved including Demo, `1` normal stopped/not-ready/configuration, `2` invalid usage or ownership conflict.
- [ ] Test stable JSON keys:

  ```json
  {
    "service": "running",
    "management": "managed",
    "url": "http://127.0.0.1:8765/",
    "mode": "demo",
    "provider": "mock",
    "model": "mock",
    "pid": 123,
    "next_action": "kestrel chat"
  }
  ```

- [ ] Test authenticated-but-locked status as running/locked with provider and mode unavailable, never offline or Ready.
- [ ] Run and confirm the launcher module/entry point is absent.

### 5.2 Implement presentation and dependency boundaries

- [ ] Implement a testable `LauncherApplication` that receives controller, client factory, browser opener, offline-doctor callable, environment, clock/sleep, and I/O streams.
- [ ] Implement `build_parser`, `run(argv, application_factory=...) -> int`, and `main() -> NoReturn`.
- [ ] Add to `pyproject.toml` without changing existing entries:

  ```toml
  kestrel = "nested_memvid_agent.launcher:main"
  ```

- [ ] Render human status in the fixed fields `Service`, `Workbench`, `Mode`, `Provider`, `Process`, `Managed`, and `Next`.
- [ ] Fetch runtime config/readiness only after service identity is verified; map 401 to access locked.
- [ ] Rerun parser/status tests.

### 5.3 Add failing start/open tests

- [ ] Test `start` idempotent output, conflict recovery, and mode/provider/next-command output.
- [ ] Test `open` calls start, waits for verified health, and only then invokes the platform browser opener.
- [ ] Test opener failure leaves the verified service running and prints the exact URL.
- [ ] Test `--no-browser` performs full start/health verification but never invokes an opener.
- [ ] Implement these command handlers and rerun.

### 5.4 Add failing API-only chat tests

- [ ] Test one-shot positional and `--message` chat.
- [ ] Test interactive chat reuses one generated session ID until EOF.
- [ ] Test only `POST /api/runs` and `GET /api/runs/{id}` are used after service start/reuse.
- [ ] Patch `nested_memvid_agent.run_manager.RunManager` to raise if constructed, proving the product chat never opens local runtime state.
- [ ] Test completed output, JSON output, failed output, blocked/approval recovery, timeout, and `KeyboardInterrupt`; timeout/interruption must print run ID and Workbench URL without cancellation.
- [ ] Implement API-only chat and rerun.

### 5.5 Add failing server-aware doctor tests

- [ ] Test running managed/external service uses API plus lifecycle metadata only and never invokes offline diagnostics.
- [ ] Test proven stopped service delegates to existing `cli._doctor_runtime` with resolved paths.
- [ ] Test conflict performs read-only reporting and never opens SQLite or Memvid.
- [ ] Implement doctor routing and grouped output for installation, ownership/port, API/readiness, paths, provider, and optional dependencies.
- [ ] Rerun:

  ```bash
  PATH=/Users/tiuni/kestrel/.venv/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src \
  /Users/tiuni/kestrel/.venv/bin/python -m pytest -q \
    tests/test_launcher.py tests/test_service_control.py tests/test_server_client.py
  ```

- [ ] Run the full Python suite and commit:

  ```bash
  git add pyproject.toml src/nested_memvid_agent/launcher.py tests/test_launcher.py
  git add tests/test_packaging_metadata.py
  git commit -m "feat: add the Kestrel launch facade"
  ```

  If `tests/test_packaging_metadata.py` does not exist, keep the packaging assertion in `tests/test_launcher.py` and omit that single `git add` command.

---

## Task 6: Generate Transactional User Launch Artifacts

**Files:**

- Create: `scripts/manage_user_launchers.py`
- Create: `tests/test_user_launchers.py`

### 6.1 Add failing command-shim tests

- [ ] Test target selection in order: explicit safe `KESTREL_BIN_DIR`, safe writable PATH directory, then `~/.local/bin`.
- [ ] Reject relative, symlinked, temporary, virtualenv, app-bundle, wrong-owner, and non-directory targets.
- [ ] Test generated shim:

  - contains a stable Kestrel ownership marker;
  - embeds canonical `KESTREL_HOME`;
  - executes `<home>/.venv/bin/kestrel`;
  - forwards `"$@"` and exit status;
  - is mode `0755`;
  - contains no environment credential values.

- [ ] Test unrelated existing `kestrel` paths fail before mutation; Kestrel-managed predecessors are safely upgradeable.
- [ ] Run and confirm the helper is absent.

### 6.2 Add failing macOS-app and transaction tests

- [ ] Under a temporary fake home, test:

  - `Kestrel.app/Contents/Info.plist` parses with `plistlib`;
  - the executable invokes the managed shim with `open`;
  - launch failure invokes a static `osascript` error containing log path and `kestrel doctor`, never raw stderr secrets;
  - the ownership marker gates replacement;
  - `prepare` creates a manifest and backups;
  - `rollback` removes newly created artifacts and restores managed predecessors byte-for-byte;
  - `commit` removes backups and manifest;
  - a simulated failure between shim and app creation rolls back both.

- [ ] Inject platform/tool discovery so Linux tests validate command shim behavior without pretending a macOS app was installed.
- [ ] Run and confirm the missing implementation failure.

### 6.3 Implement the focused artifact manager

- [ ] Implement subcommands:

  ```text
  manage_user_launchers.py prepare
    --kestrel-home <canonical home>
    --user-home <home>
    --manifest <private manifest path>
    [--bin-dir <explicit directory>]
    [--platform darwin|linux]

  manage_user_launchers.py commit --manifest <path>
  manage_user_launchers.py rollback --manifest <path>
  ```

- [ ] Use `os.open` with no-follow/exclusive flags, atomic same-directory replacement, `lstat`, current-user ownership checks, and private manifest/backups.
- [ ] Store only artifact paths, prior-managed status, backup paths, and created flags in the manifest.
- [ ] Generate the plist with `plistlib`; use a fixed shell executable with fully quoted static paths.
- [ ] On Darwin, run `codesign --force --deep --sign -` and `codesign --verify --deep --strict` when available; distinguish skipped tooling from failed signing.
- [ ] Do not use `sudo`, modify shell profiles, or overwrite unrelated artifacts.
- [ ] Rerun focused tests and the full Python suite.
- [ ] Commit:

  ```bash
  git add scripts/manage_user_launchers.py tests/test_user_launchers.py
  git commit -m "feat: generate user Kestrel launchers"
  ```

---

## Task 7: Integrate Launchers into the Release Installer

**Files:**

- Modify: `install.sh`
- Modify: `tests/test_install_script.py`

### 7.1 Add failing installer behavior tests

- [ ] Extend the test harness to install into a temporary release home and assert:

  - accepted release virtualenv contains functional `kestrel --help`;
  - launcher `prepare` runs after candidate state commit and before optional server start;
  - active transaction rollback restores/removes launch artifacts;
  - successful finalize commits launch artifacts;
  - dry run reports target shim/app without mutation;
  - unrelated shim/app collision aborts safely;
  - defaults still do not start or open a browser;
  - `KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1` invokes `kestrel open` behavior;
  - completion output lists `kestrel open`, `status`, `chat`, `doctor`, and `stop`;
  - completion output does not recommend raw `kill` or direct state-owning chat.

- [ ] Prefer executing installer functions in the existing isolated harness; use source assertions only for trap wiring that cannot be triggered safely.
- [ ] Run focused installer tests and confirm expected failures.

### 7.2 Integrate transaction state

- [ ] Add installer variables for `KESTREL_BIN_DIR` and a private launcher manifest under the active transaction directory.
- [ ] During runtime verification, run candidate `.venv/bin/kestrel --help` in addition to the preserved advanced entry-point checks.
- [ ] Call artifact `prepare` after `commit_staged_state` and before optional server start/browser open.
- [ ] Wire rollback into every active-transaction error trap before the previous accepted release is restored.
- [ ] Call artifact `commit` before clearing the accepted transaction.
- [ ] Preserve existing offline upgrade, state/memory staging, supervisor cleanup, and release acceptance sequencing.

### 7.3 Replace completion and launch behavior

- [ ] Keep `KESTREL_START_SERVER=0` and `KESTREL_OPEN_BROWSER=0` as defaults.
- [ ] When start/open are requested, invoke the candidate managed `kestrel open` path rather than duplicate a direct server/browser sequence.
- [ ] Print an exact PATH profile line only when the chosen shim directory is not already on PATH.
- [ ] Replace raw server/chat/kill instructions with the everyday façade and retain one link to advanced `nest-agent`.
- [ ] Rerun focused installer tests, full Python tests, and `bash -n install.sh`.
- [ ] Commit:

  ```bash
  git add install.sh tests/test_install_script.py
  git commit -m "feat: install Kestrel launch facade transactionally"
  ```

---

## Task 8: Surface Experience Modes in the Workbench

**Files:**

- Modify: `web/src/types.ts`
- Modify: `web/src/App.tsx`
- Modify: `web/src/App.test.tsx`

### 8.1 Make the setup fixture configurable

- [ ] Add:

  ```typescript
  export type SetupExperienceMode =
    | "demo"
    | "model_not_connected"
    | "connected";

  export type SetupReadinessReport = {
    // existing fields
    experience_mode: SetupExperienceMode;
  };
  ```

- [ ] Replace the fixed `/api/product/setup` test response with a per-test mutable typed fixture initialized to a Demo-ready report in `beforeEach`.
- [ ] Keep existing onboarding and non-provider failure tests semantically unchanged.

### 8.2 Add failing rendered-mode tests

- [ ] Render and assert:

  - mock provider + `demo` shows **Demo** and “deterministic responses; no live model connected”;
  - non-mock + `model_not_connected` shows **Model not connected**, provider/model, and a Settings recovery action;
  - non-mock + `connected` shows **Ready**;
  - approval, run failure/progress, auth/connecting, and non-provider setup failures retain higher priority;
  - provider-only setup failures do not hide the more useful model-not-connected state.

- [ ] Run:

  ```bash
  npm test --prefix web -- --run
  ```

- [ ] Confirm the new assertions fail with the current Ready/missing-workspace logic.

### 8.3 Implement narrow status rendering

- [ ] Add a helper that finds failing/warning checks excluding `provider_configuration` and `provider_operational`.
- [ ] Apply status priority exactly:

  1. API auth/connecting;
  2. approval/run state;
  3. non-provider setup failure;
  4. Demo;
  5. Model not connected;
  6. Ready.

- [ ] Add only the small Settings recovery action needed for the status; do not redesign Settings.
- [ ] Rerun frontend tests and:

  ```bash
  npm run build --prefix web
  ```

- [ ] Confirm the production bundle succeeds.
- [ ] Run the full Python suite to catch package/static-contract regressions.
- [ ] Commit:

  ```bash
  git add web/src/types.ts web/src/App.tsx web/src/App.test.tsx
  git commit -m "feat: show truthful Kestrel connection modes"
  ```

---

## Task 9: Document and Verify the Complete Launch Path

**Files:**

- Modify: `README.md`
- Modify if generated bundle is versioned: `src/nested_memvid_agent/web_dist/**`
- Add or modify focused smoke test only if needed: `tests/test_launcher_smoke.py`

### 9.1 Add documentation contract checks if the repository already tests README/install commands

- [ ] Assert the release section leads with:

  ```bash
  curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/v0.4.11/install.sh \
    | KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash
  ```

- [ ] Assert returning users see `kestrel open`, Demo versus connected-model truthfulness, source installation, and advanced `nest-agent` links.
- [ ] Assert the editable/source quick start ends with `kestrel open`.
- [ ] Confirm direct server commands remain only in advanced/troubleshooting sections.
- [ ] Run the focused documentation test and confirm failure before editing README.

### 9.2 Update everyday documentation

- [ ] Put the release install-and-open path before the architectural tour.
- [ ] Explain:

  - Demo is deterministic and needs no credential;
  - Ready means a non-mock provider succeeded in the current server process;
  - `kestrel` is everyday, `nest-agent` is advanced, and `nested-memvid` is compatibility;
  - returning users can launch from any directory with the shim or macOS app.

- [ ] Keep exact current release URL/version and do not imply a new release was published.
- [ ] Rerun the documentation test.

### 9.3 Run a disposable-home lifecycle smoke

- [ ] Build/install the current branch into a temporary home without mutating the developer checkout or accepted release.
- [ ] Choose a test-owned free loopback port and set `KESTREL_HOME`/`KESTREL_PORT`.
- [ ] Execute, capturing process/listener identity before and after:

  ```text
  kestrel start
  kestrel status --json
  kestrel chat --message "hello"
  kestrel open --no-browser
  kestrel stop
  kestrel stop
  ```

- [ ] Verify:

  - one verified server owns the configured port;
  - status reports Demo, mock/mock, URL, and management accurately;
  - deterministic mock chat completes through the HTTP API;
  - repeated start/open reuse the same authoritative server PID;
  - both stops succeed;
  - no listener or test-owned service process remains;
  - no Memvid integration is run unless `RUN_MEMVID_INTEGRATION=1`.

- [ ] If the smoke reveals a defect, add a focused failing test before repairing it.

### 9.4 Perform final verification

- [ ] Run:

  ```bash
  git diff --check
  bash -n install.sh scripts/installer-server-supervisor.sh
  PATH=/Users/tiuni/kestrel/.venv/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src \
  /Users/tiuni/kestrel/.venv/bin/python -m pytest -q
  npm test --prefix web -- --run
  npm run build --prefix web
  ```

- [ ] Verify `git status --short`, inspect the full branch diff, and confirm no unrelated primary-checkout files, credentials, runtime state, `.mv2` files, PID files, logs, or build caches are staged.
- [ ] Commit documentation and any required versioned frontend bundle:

  ```bash
  git add README.md
  git add src/nested_memvid_agent/web_dist
  git commit -m "docs: make Kestrel install and launch immediate"
  ```

  If `src/nested_memvid_agent/web_dist` is not versioned, omit that single `git add` command.

- [ ] Use `superpowers:verification-before-completion` against fresh command output.
- [ ] Use `superpowers:finishing-a-development-branch` to present the verified branch without merging, pushing, tagging, or releasing unless the user explicitly authorizes one of those actions.
