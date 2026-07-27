# Kestrel Launch Ease P0 Design

**Date:** 2026-07-27
**Status:** Approved product design; awaiting implementation-plan approval
**Baseline:** `main` at `1b46381` (`v0.4.11`)
**Scope:** Single-user, local/private Kestrel on macOS and Linux

## 1. Executive decision

Kestrel's technical depth should remain visible after launch, not become a prerequisite for launch.

P0 adds a small product-facing compatibility facade:

```text
kestrel open
    -> resolve the Kestrel installation
    -> reuse or safely start the loopback service
    -> verify the service API
    -> open the Workbench
```

The same facade exposes:

```text
kestrel start
kestrel stop
kestrel status
kestrel open
kestrel chat
kestrel doctor
```

`kestrel chat` talks to the authoritative server API. It never constructs a second `RunManager` against the server-owned SQLite database. This removes the current repeat-launch collision while keeping one runtime authoritative for runs, approvals, memory promotion, provenance, and safety gates.

The release installer also creates a user-level command launcher and, on macOS, a clickable `Kestrel.app`. The README leads with one install-and-open command. Existing `nest-agent` and `nested-memvid` commands remain supported as the advanced and compatibility surfaces.

## 2. Product outcome

The P0 experience has two layers:

1. **Everyday surface:** install, open, chat, inspect status, diagnose, and stop.
2. **Advanced surface:** direct server configuration, memory maintenance, routines, repair, plugins, MCP, channels, and all existing `nest-agent` subcommands.

A new user should not need to understand virtual environments, working directories, process groups, SQLite ownership, Memvid layer paths, or the server command to open Kestrel. Those details remain inspectable and configurable for advanced users.

### Measurable usability targets

- A release user reaches the Workbench from one documented shell command.
- A returning macOS user launches Kestrel with one app click or `kestrel open`.
- A user can determine service, provider, and next-action state with `kestrel status`.
- A CLI chat started while the Workbench server is running succeeds through the API rather than colliding with the state database.
- Demo mode is never presented as a connected live model.
- A live provider is never presented as connected until the current server process has completed a successful provider request.
- Failure output names the failed layer and gives one concrete recovery action.

## 3. Scope

### 3.1 Included in P0

- A new `kestrel` Python console entry point.
- Idempotent `start`, verified `stop`, informative `status`, start-and-open `open`, server-backed `chat`, and server-aware `doctor`.
- Reuse of the existing detached supervisor and private PID/process-group metadata contract.
- Additive setup-readiness data for `demo`, `model_not_connected`, and `connected`.
- Small Workbench status and recovery-copy changes using those semantics.
- More specific credential, endpoint, model, and provider error classification.
- A user-level command shim from the release installer.
- A per-user macOS application launcher.
- Installer next steps and README quick-start changes.
- Deterministic tests and a local start/status/chat/stop smoke path.

### 3.2 Explicitly deferred

- A broad Workbench settings redesign.
- A new provider marketplace or automatic provider choice.
- Automatic credential discovery or migration.
- Automatic port hopping when an unknown listener owns the configured port.
- Background login-item installation or automatic launch at OS login.
- Windows app/shortcut packaging.
- Hosted or multi-user service management.
- Replacing the existing advanced CLI.
- Any state-schema, memory-layout, approval, policy, provenance, or learning-gate change.

Provider-first onboarding, progressive disclosure of advanced settings, and a broader visual simplification remain P1 candidates. P0 adds only the status and recovery affordances required to make boot and launch truthful.

## 4. Compatibility and safety invariants

P0 must preserve:

- `nested-memvid` and `nest-agent` entry points and their existing argument contracts;
- the `nested_memvid_agent` Python package and public compatibility facades;
- all `NEST_AGENT_*` environment variables;
- `.nest` paths, the current SQLite schema, and current migration behavior;
- Memvid v2 `.mv2` memory only, with one file per nested layer;
- the rule that an existing `.mv2` file is opened, never passed to `create(path)`;
- deterministic mock backend and mock LLM behavior;
- exact-call approval and configured enablement for high-risk tools;
- evidence, provenance, confidence, and validation requirements for memory promotion;
- the single authoritative runtime ownership boundary;
- loopback-only easy launch by default;
- installer rollback and offline-upgrade guarantees.

The new facade is additive. It may call existing internals or server APIs, but it must not fork a second implementation of Kestrel's agent runtime.

## 5. Proposed architecture

### 5.1 Product CLI

Add this packaging entry point:

```toml
[project.scripts]
kestrel = "nested_memvid_agent.launcher:main"
```

Keep the product parser separate from the large advanced parser:

```text
src/nested_memvid_agent/launcher.py
    command parsing, human output, exit codes

src/nested_memvid_agent/service_control.py
    installation resolution, lifecycle metadata, process identity,
    start/reuse/stop decisions

src/nested_memvid_agent/server_client.py
    bounded loopback HTTP calls, optional bearer auth, run polling,
    response/error normalization
```

Names may be adjusted during implementation planning, but the responsibility split is fixed: presentation, operating-system lifecycle, and API transport must not collapse into `cli.py`.

The API client reads only the configured API-auth environment-variable name and value (defaulting to `NEST_AGENT_API_TOKEN`) and sends a bearer header when present. It never persists, logs, or returns the token. An authenticated server with a missing client token is reported as locked rather than misclassified as offline.

### 5.2 Installation resolution

The launcher resolves its home in this order:

1. explicit `--home`;
2. `KESTREL_HOME`;
3. the home embedded by the installer-created command shim;
4. a source checkout in the current directory;
5. `~/.kestrel-agent`.

The resolved home is canonicalized before comparing process identity or paths. A missing or ambiguous installation produces an actionable error; the launcher does not search or mutate arbitrary directories.

The effective loopback address uses `KESTREL_PORT` when set and otherwise port `8765`. Easy launch binds `127.0.0.1`; non-loopback serving remains an advanced `nest-agent server` operation with its existing authentication guard.

### 5.3 Lifecycle ownership

The launcher reuses these existing private files under the resolved home:

```text
.nest/server.log
.nest/server.pid
.nest/server.supervisor.pid
.nest/server.pgid
.nest/server.lifecycle.lock
```

It also reuses `scripts/installer-server-supervisor.sh` so the server child has a dedicated process group and the same cleanup behavior as an installer-started service.

All lifecycle mutations take an exclusive, bounded POSIX file lock through the existing safe file-lock utility. This serializes concurrent `start`, `open`, and `stop` calls without replacing the runtime's own state and memory ownership locks. Read-only status may inspect without waiting indefinitely, but it reports an in-progress lifecycle operation rather than racing it.

Before trusting metadata or signaling a process, lifecycle control verifies:

- the metadata path is a regular, non-symbolic-link file owned by the current user;
- the recorded value is a valid positive PID or process-group ID;
- the process belongs to the current user;
- its canonical working directory is the resolved Kestrel home;
- its command identifies the installed Kestrel supervisor or the expected `.venv/bin/nest-agent server` child;
- the configured port and server arguments match the installation contract;
- the loopback health endpoint identifies a Kestrel service.

An unknown listener, mismatched PID, unsafe metadata file, or unverifiable process is never killed or adopted. The command explains the conflict and points to `kestrel doctor`.

Stale metadata may be removed only after process absence is proven and the metadata file itself passes the private-file checks.

A healthy, current-user `nest-agent server` launched directly from the same canonical Kestrel home may be classified as a **verified external instance** even when it lacks managed PID metadata. `start`, `open`, and `chat` reuse that server without manufacturing ownership metadata. `status` labels it external. `stop` may signal only its exact verified listener PID, never an unowned process group or wrapper. Every other metadata-free listener remains a conflict.

## 6. Command contracts

### 6.1 `kestrel start`

`start` is idempotent:

1. If a healthy matching managed or verified external Kestrel instance already owns the port, report that Kestrel is already running and exit successfully.
2. If startup is already in progress under verified supervisor ownership, wait for the bounded readiness window.
3. If stale, safe metadata refers to absent processes, remove it and continue.
4. If an unrelated process or listener conflicts, refuse to signal it and return an actionable conflict.
5. Otherwise launch the existing supervisor with safe boot defaults and persisted runtime settings, then wait for both verified process metadata and API liveness.

The service command continues to use the Memvid backend, `.nest/memory`, the configured state path, mock as the safe first-run provider, `127.0.0.1`, and the configured port. Persisted runtime settings remain authoritative once the application initializes.

Startup has a bounded readiness wait. On failure, the launcher attempts cleanup only for the process identities it just created and verified. If absence cannot be proven, it reports an indeterminate cleanup state and preserves evidence rather than deleting files or claiming rollback.

Successful output includes the URL, process state, experience mode, provider/model, and next command.

### 6.2 `kestrel stop`

`stop`:

- succeeds without error when Kestrel is already stopped;
- signals only a verified current-user managed supervisor/server group or the exact PID of a verified external listener;
- uses bounded graceful termination before a hard kill;
- re-verifies identity before every escalation;
- verifies the supervisor, server, process group, and owned listener are gone;
- removes only matching safe metadata;
- refuses to touch unknown or reused PIDs.

The raw `kill "$(cat ...)"` instruction is removed from installer next steps.

### 6.3 `kestrel status`

`status` does not start or mutate the service. It reports:

```text
Service:   running | stopped | starting | conflict
Workbench: http://127.0.0.1:8765/
Mode:      Demo | Model not connected | Ready
Provider:  provider / model
Process:   verified PID, when available
Managed:   yes | external | n/a
Next:      one concrete command or Workbench action
```

If the API is reachable but process ownership cannot be verified, the state is `conflict`, not `running`.

Human-readable output is the default. `--json` returns a stable, non-secret structure for tests and future integrations.

Exit status is also stable: `0` means the requested state was achieved (and Demo is operational), `1` means a normal stopped/not-ready/configuration condition, and `2` means invalid usage or a safety conflict whose ownership could not be proven.

### 6.4 `kestrel open`

`open` runs the same idempotent start path, then opens the verified Workbench URL with the platform opener. It never opens a URL until Kestrel liveness succeeds.

If no opener is available or opening fails, the service remains running and the exact URL is printed. `--no-browser` exercises the complete start-and-verify path without launching a browser and supports deterministic tests.

### 6.5 `kestrel chat`

`chat` starts or reuses the service and communicates only through:

- `POST /api/runs`;
- `GET /api/runs/{run_id}` until a terminal state.

It supports:

```text
kestrel chat
kestrel chat "Explain this repository"
kestrel chat --message "Explain this repository"
```

Interactive mode retains one session ID for that process. One-shot mode prints only the assistant result by default. `--json` exposes the run payload without secrets.

Polling is bounded by a configurable wait timeout. Timeout or `Ctrl-C` does not silently cancel durable work; the launcher prints the run ID and Workbench URL so the user can inspect or cancel it deliberately.

For a blocked run or pending approval, the launcher prints the reason and directs the user to the approval surface. Implementing a second CLI approval workflow is out of P0.

This API-only rule is the core state-ownership fix: `kestrel chat` never opens the live SQLite control plane through another `RunManager`.

### 6.6 `kestrel doctor`

When a verified service is running, `doctor` queries the authoritative API and validates lifecycle metadata without opening the state database or Memvid files directly.

When the service is proven stopped, it delegates to the existing offline diagnostic logic with the resolved Kestrel paths.

When ownership is ambiguous, it performs read-only checks, reports the ambiguity, and refuses diagnostics that would contend for runtime ownership.

Doctor groups results into:

- installation and executable;
- service ownership and loopback port;
- API liveness/readiness;
- memory and state paths;
- provider configuration and operational health;
- optional validation/runtime dependencies.

Every failed check includes one recovery action. Secret values are never printed.

## 7. Experience-mode truthfulness

`SetupReadinessReport` gains an additive `experience_mode` field:

```text
demo
model_not_connected
connected
```

The existing `ready` field keeps its current setup-prerequisite meaning for compatibility.

Mode is computed as follows:

- `demo`: the active provider is `mock`;
- `connected`: a non-mock provider has valid configuration and the current server process has recorded a successful live request;
- `model_not_connected`: every other non-mock state, including missing credentials, missing endpoint/model configuration, untested configuration, degraded health, and open circuit.

The Workbench status priority is:

1. API locked or connecting;
2. active approval/run failure/run progress;
3. non-provider setup failure;
4. `Demo`;
5. `Model not connected`;
6. `Ready`.

The Demo detail states that responses are deterministic and no live model is connected. Model-not-connected detail names the provider/model and links to the relevant Settings recovery surface. A small status change is in scope; rearranging the full Settings page is not.

## 8. Error model

Provider error classification and both CLI/UI rendering distinguish at least:

| Class | User-facing meaning | Recovery |
| --- | --- | --- |
| missing credential | Named secret reference is absent | Store it in Settings or set the named environment variable |
| rejected credential | Provider returned an authentication/authorization failure | Replace or correct that provider credential |
| endpoint unreachable | Connection refused, DNS failure, or request timeout | Start/correct the local endpoint or base URL |
| model unavailable | Provider rejected or could not find the configured model | Select/install a valid model |
| rate limited | Provider throttled the request | Wait, reduce concurrency, or choose another configured provider |
| provider failure | Provider returned a specific non-auth failure | Show the sanitized provider detail and retry guidance |
| service conflict | Port/PID belongs to an unverified process | Inspect with `kestrel doctor`; never auto-kill |

Classification recognizes common SDK variants such as `api_key`, `api-key`, HTTP 401/403, connection errors, and model-not-found response shapes. Output includes provider and model names when safe, but never includes request headers, tokens, raw secret values, or unredacted provider payloads.

Generic “Provider request failed” is a last resort, not the response to a recognizable credential, endpoint, or model problem.

## 9. Installer and macOS launcher

### 9.1 Command shim

After the release virtual environment is accepted, the installer creates an owned command shim that:

- embeds the canonical Kestrel home;
- executes `<home>/.venv/bin/kestrel`;
- forwards arguments and exit status exactly;
- contains no credentials;
- is replaced only when its ownership marker identifies it as Kestrel-managed.

The target directory is, in order:

1. `KESTREL_BIN_DIR`, when explicitly configured and safe;
2. a current-user-writable directory already present in `PATH`;
3. `~/.local/bin`.

The installer never uses `sudo` and never overwrites an unrelated command. If the fallback directory is not on `PATH`, it prints one exact shell-profile line while the macOS app and install-and-open flow remain immediately usable.

### 9.2 macOS app

On macOS, install `~/Applications/Kestrel.app` as a per-user launcher. It invokes the managed command shim with `open`, contains no secrets, and is updated only when its Kestrel ownership marker matches.

Double-click behavior:

1. start or reuse Kestrel;
2. wait for verified liveness;
3. open the Workbench;
4. show a concise error with the log path and `kestrel doctor` recovery command if launch fails.

The generated local app is ad-hoc signed and verified when the platform `codesign` tool is available. This is a local integrity check, not a notarization or distributable application-signing claim.

Linux receives the command shim but no desktop-file work in P0.

### 9.3 Installer transaction

Launcher creation participates in installer rollback. A failed candidate install restores any prior Kestrel-managed shim/app and removes a newly created one. Unrelated pre-existing paths cause a safe install failure before mutation.

The installer's existing offline-upgrade, memory/state staging, server cleanup, and release-acceptance boundaries remain intact.

The primary documented release command explicitly enables start and browser open. The installer environment defaults remain non-starting for unattended and embedded uses.

Installer completion output uses:

```text
kestrel open
kestrel status
kestrel chat
kestrel doctor
kestrel stop
```

It no longer recommends direct chat against a server-owned state database.

## 10. Documentation

The README begins with a short release path before the architectural tour:

1. one install-and-open command;
2. one returning-user command: `kestrel open`;
3. Demo versus connected-model explanation;
4. a link to source/developer installation;
5. a link to advanced `nest-agent` reference.

The source quick start uses the same facade after editable installation. Detailed direct-server commands remain documented under advanced operation and troubleshooting.

Terminology in the everyday path is consistently:

- product: **Kestrel**;
- everyday command: **`kestrel`**;
- advanced command: **`nest-agent`**;
- compatibility command: **`nested-memvid`**.

## 11. Validation strategy

Implementation follows test-driven phases and runs `pytest -q` after each phase.

### 11.1 Python tests

- parser and exit-code tests for every new command;
- home/path resolution tests;
- concurrent start/open serialization;
- idempotent healthy-service reuse;
- managed and verified-external server reuse;
- stale, unsafe, mismatched, and reused PID cases;
- unrelated port-listener refusal;
- verified supervisor/process-group stop behavior;
- startup failure and cleanup-proof behavior;
- authenticated and unauthenticated API client calls;
- run creation, polling, terminal output, timeout, interruption, and approval-blocked chat;
- proof that server-backed chat never builds a local `RunManager`;
- server-aware versus offline doctor routing;
- experience-mode calculation;
- credential, endpoint, model, rate-limit, and fallback provider errors;
- installer shim/app creation, collision refusal, upgrade, and rollback.

All process tests use temporary paths and test-owned processes. They never signal unrelated machine processes.

### 11.2 Workbench tests

- Demo is displayed instead of Ready for mock;
- an unvalidated live provider displays Model not connected;
- successful current-process provider health displays Ready;
- setup and run-state priorities remain correct;
- recovery copy points to the relevant Settings action.

Run the existing frontend unit tests and production build.

### 11.3 End-to-end smoke

Exercise against a disposable Kestrel home:

```text
kestrel start
kestrel status --json
kestrel chat --message "hello"
kestrel open --no-browser
kestrel stop
kestrel stop
```

Verify that one authoritative server owns the state database throughout, mock chat is deterministic, repeated start/stop is idempotent, and no listener remains.

Memvid integration coverage remains behind `RUN_MEMVID_INTEGRATION=1`.

## 12. Acceptance criteria

P0 is complete only when:

1. The documented release command installs, starts, verifies, and opens Kestrel without requiring a manual `cd`, virtualenv activation, or raw server command.
2. A returning user can use `kestrel open` or the macOS app from a different working directory.
3. Repeated `start`/`open` reuses one healthy service.
4. A directly launched, identity-verified server is reused without unsafe metadata adoption.
5. Concurrent launch commands serialize to one service.
6. `kestrel chat` works while that service is running and does not contend for SQLite ownership.
7. `stop` never signals an unverified PID, process group, or listener.
8. `status` truthfully reports service, management, URL, mode, provider/model, and one next action.
9. Mock is visibly Demo; an unvalidated or broken live provider is visibly Model not connected.
10. Recognizable credential, endpoint, model, and rate-limit failures are actionable and redacted.
11. Installer rollback covers its shim and macOS app without overwriting unrelated paths.
12. Existing `nest-agent`, `nested-memvid`, Memvid v2, schema, safety, approval, and provenance contracts remain compatible.
13. Python tests, frontend tests/build, installer tests, and the disposable-home lifecycle smoke pass.

## 13. Delivery boundary

This design authorizes implementation on the focused feature branch after a separate implementation plan is reviewed. It does not authorize a release publication, tag, merge to `main`, persistent login item, non-loopback exposure, provider purchase, or credential mutation.
