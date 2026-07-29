# Kestrel GUI-First Desktop Product Design

Date: 2026-07-29  
Status: Design approved in dialogue; written specification awaiting owner review  
Target release: first desktop-product release after Kestrel v0.5.0  
Supported security profile: one trusted owner, one local or privately networked node

## 1. Purpose

Kestrel will become a GUI-first desktop product for macOS, Windows, and Linux.
The everyday path will be:

1. download one installer for the current platform;
2. install and open Kestrel;
3. complete a short in-app setup;
4. start a useful mission.

The owner will not need Python, Node.js, a shell, environment-variable
configuration, or separate web-build commands. The existing CLI remains an
advanced and automation surface, but it is no longer the primary product
experience.

This work must preserve Kestrel's existing trust model:

- Memvid v2 `.mv2` files remain canonical memory;
- SQLite remains control-plane state;
- recalled memory remains untrusted evidence;
- dangerous actions remain capability-gated and exact-call approved;
- local branches, validation evidence, provenance, rollback, and reversibility
  remain visible;
- no GUI action becomes a privileged bypass around the Kestrel runtime.

## 2. Goals

### 2.1 Installation and launch

- Ship a self-contained core on macOS, Windows, and Linux.
- Require zero terminal commands for installation, setup, launch, ordinary
  operation, updates, diagnostics, and uninstall.
- Bundle the Electron shell, React assets, frozen Python/Kestrel runtime,
  migrations, Memvid integration, deterministic Demo provider, and all other
  Kestrel-owned runtime dependencies.
- Make core first launch work without network access.
- Preserve the user's memory, state, projects, and settings across upgrades and
  reinstalls.

### 2.2 Product experience

- Make Mission Command the default home and primary task surface.
- Put every existing Kestrel feature into a stable, understandable GUI
  information architecture.
- Replace configuration reconstruction with tunable settings, readiness
  explanations, native pickers, and reversible transactions.
- Keep raw IDs, digests, JSON, and operator diagnostics available under
  Evidence or Advanced without making them prerequisites for ordinary work.
- Give Kestrel a distinctive, warm, expressive visual identity rather than a
  generic gray engineering dashboard.

### 2.3 Safety and truthfulness

- Show configured state, effective state, blockers, authority changes, and when
  a setting takes effect.
- Keep secrets outside renderer state, model context, logs, memory, and visible
  tool output.
- Keep optional providers, local model servers, LAN model servers, containment
  engines, MCP servers, plugins, and channels explicitly reviewed.
- Fail closed when the installed sidecar, state schema, update, or optional
  integration cannot be verified.

## 3. Non-goals

This phase does not:

- add hosted accounts, multi-user authorization, or tenant isolation;
- bundle a production local model;
- bundle Docker, a VM manager, or another platform containment engine;
- make an unqualified model or LAN endpoint eligible automatically;
- remove `kestrel`, `nest-agent`, or compatibility CLIs;
- replace FastAPI or the existing Kestrel runtime with Electron logic;
- allow remote web content to execute inside the desktop renderer;
- add external telemetry by default;
- weaken exact-call approvals or let an Electron IPC call impersonate one.

Cloud providers, local model servers, LAN model servers, and containment
engines are optional integrations configured through the GUI. Demo mode is
immediately usable without them.

## 4. Product architecture

### 4.1 Components

The desktop product has two principal executables:

1. **Hardened Electron shell**
   - owns the desktop window and native lifecycle;
   - renders the bundled React application;
   - starts, authenticates, monitors, and stops the bundled Kestrel sidecar;
   - exposes only narrow, schema-validated native operations;
   - integrates platform file pickers, notifications, updates, and credential
     storage.

2. **Frozen Python Kestrel sidecar**
   - contains the authoritative FastAPI server and agent runtime;
   - owns projects, runs, task graphs, providers, tools, approvals, routing,
     memory, extensions, routines, and evidence;
   - continues to enforce all capability, approval, provenance, containment,
     and rollback gates;
   - binds only to an operating-system-assigned loopback port;
   - is packaged separately for each supported operating system and
     architecture.

The React renderer communicates with the sidecar over authenticated loopback
HTTP and event streams. Native-only operations pass through a minimal Electron
preload bridge with explicit request and response schemas.

### 4.2 Renderer boundary

Every renderer window must:

- use `nodeIntegration: false`;
- use `contextIsolation: true`;
- use Chromium process sandboxing;
- load only bundled assets through a private application protocol;
- apply a restrictive Content Security Policy;
- block arbitrary navigation and new-window creation;
- validate the sender and payload of every IPC request;
- avoid exposing generic filesystem, process, shell, or IPC primitives;
- never receive raw provider, channel, MCP, signing, or vault secrets.

The renderer can display an approval request and submit an owner decision to the
existing authenticated API. It cannot construct a completed approval row or
invoke a side-effecting implementation directly.

### 4.3 Sidecar lifecycle

At each launch the Electron main process:

1. verifies the packaged sidecar identity and resource manifest;
2. creates an ephemeral owner-only launch record and API token;
3. starts the sidecar with an operating-system-assigned loopback port;
4. verifies process birth identity and a nonce-bound readiness handshake;
5. opens Mission Command only after connected proof;
6. renews a bounded parent/child liveness lease;
7. requests graceful sidecar shutdown when the app exits.

Kestrel never kills a process based only on a PID or port. It verifies the
managed process identity, listener, launch nonce, executable digest, and parent
relationship. An unrelated process on a candidate port is left untouched.

The supervisor permits one bounded restart after an unexpected sidecar exit.
Further failure opens Recovery instead of entering a crash loop. Interrupted
runs remain durable and require reconciliation; high-risk calls and approvals
are never replayed automatically.

### 4.4 State ownership

Installed application bytes are immutable and separate from owner data.
Platform-appropriate private data directories contain:

- six permanent Memvid v2 files, one per nested memory layer;
- separately stored run capsules;
- SQLite control-plane databases and authenticated rebuildable indexes;
- settings and launcher metadata;
- local diagnostic receipts and support bundles;
- encrypted or OS-backed secret metadata, never renderer-readable values.

Existing `.mv2` files are reopened. The desktop runtime must never call
`create(path)` on an existing `.mv2` file.

The owner can move memory, state, project artifact, and support-bundle
locations through a transactional GUI workflow that validates the destination,
shows impact, stages changes, and offers rollback.

### 4.5 Desktop and CLI service coexistence

One runtime profile has one authoritative service owner. The desktop app and
advanced CLI must never open concurrent writers against the same control-plane
or Memvid paths.

The desktop uses a single-instance lock plus the existing ownership-aware
service identity contract. At launch:

- if no compatible service owns the selected profile, Desktop starts its
  bundled sidecar;
- if a verified compatible Desktop-owned service already exists, the new
  window attaches to it;
- if a verified CLI-managed Kestrel service owns the profile, Desktop explains
  the situation and offers an in-app ownership-aware restart under Desktop;
- if the listener is unrelated, stale, unauthenticated, version-incompatible,
  or identity verification fails, Desktop leaves it untouched and opens a
  recovery choice rather than terminating it.

While Desktop owns the service, bundled `kestrel` client commands discover and
authenticate to that managed profile through owner-only runtime metadata. The
launch token rotates on every service start. Direct advanced server startup
against an already-owned profile fails with a readable lease conflict.

## 5. Cross-platform packaging

Each artifact is built natively from the exact tagged source with locked
dependencies, checksums, an SBOM, and a signed resource manifest.

### 5.1 Canonical artifacts

- **macOS:** signed and notarized DMGs for Apple Silicon and Intel.
- **Windows:** signed per-user installers for x64 and ARM64.
- **Linux:** signed AppImages for x64 and ARM64.
- **Linux secondary formats:** `.deb` and `.rpm`, generated from the same exact
  payload after the AppImage path is qualified.

The default install does not require administrator privileges when the target
platform permits a private per-user installation.

PyInstaller or an equivalently reproducible freezer produces one Kestrel
sidecar per target platform and architecture. It is not cross-compiled:
Windows artifacts build on Windows, macOS artifacts build on macOS, and Linux
artifacts build on the supported Linux baseline.

### 5.2 CLI compatibility

The bundled payload includes the advanced `kestrel` command surface. Installer
integration may expose that shim when the owner opts in, but the desktop
application never depends on `PATH`, shell startup files, or the CLI.

`nest-agent` remains available for advanced operators and automation. Desktop
features call the same application services rather than maintaining a separate
behavior implementation.

## 6. First-run experience

First run contains five short stages.

### 6.1 Welcome and bundled-core check

The app verifies:

- the signed Electron and sidecar payload;
- packaged resource hashes;
- the private writable state directory;
- the six Memvid layer paths and reopen behavior;
- SQLite schema compatibility;
- loopback lifecycle and authenticated readiness;
- available OS credential storage;
- bundled Demo provider operation.

A failure opens a readable repair screen with retry, choose-location, restore,
and redacted support-bundle actions. It never sends the owner to a command list
as the primary recovery path.

### 6.2 Choose intelligence

The owner can:

- continue immediately with Demo;
- discover model servers on the same computer;
- explicitly scan the selected local network;
- manually enter a compatible local/private endpoint;
- select and configure a cloud provider.

Provider credentials go directly from a native or isolated credential form to
the Secret Broker backend. They do not pass through chat, model context, React
application state, event logs, or Memvid.

### 6.3 Add a project

A native folder picker creates a conservative project draft. The GUI previews:

- canonical repository path;
- allowed path ceiling;
- default branch and current Git state;
- repository-index plan;
- test and build recipes;
- default capability ceiling;
- budget and provider policy;
- rollback strategy.

The owner reviews the draft before saving. Index construction is rebuildable
and cannot become canonical memory.

### 6.4 Review safety defaults

Plain-language cards explain:

- what Kestrel can read;
- which actions always require approval;
- which actions are disabled;
- which features require an optional containment engine;
- where memory and state live;
- how to stop the runtime and revoke authority.

The owner reviews these defaults; setup does not encourage enabling dangerous
capabilities merely to remove warnings.

### 6.5 Start the first mission

The wizard opens Mission Command with:

- the selected project;
- Demo or the verified provider;
- useful goal templates;
- a ready objective field;
- truthful blockers;
- a direct path to run a safe first mission.

The wizard then becomes a permanent Setup Center rather than disappearing.

## 7. LAN model discovery

LAN discovery is manual-only. It begins only after the owner selects **Scan
network** and confirms the network interface or private subnet.

The scan:

- starts with passive mDNS/Bonjour discovery where supported;
- probes only private/link-local addresses in the selected scope;
- uses an allowlist of known model-service ports and bounded compatible API
  probes;
- enforces strict concurrency, host-count, response-size, redirect, and time
  limits;
- never probes public Internet ranges;
- never guesses credentials;
- never performs a broad arbitrary port scan;
- records redacted discovery provenance and timeout/error counts.

The owner can add a host or unusual port manually for routed LANs.

Every discovered server is a disabled target draft. Before enablement Kestrel
shows:

- address and interface;
- provider/API shape;
- models reported;
- transport security;
- generation, streaming, tool, and structured-output probe evidence;
- privacy warning that prompts and code leave the current computer for a LAN
  peer;
- freshness and source provenance.

The owner must assign trust and intended roles before the target becomes
eligible. A later address, certificate, API, model inventory, or capability
change marks the target stale and revokes dependent routing authority.

## 8. Product information architecture

The desktop shell has seven stable top-level destinations.

### 8.1 Mission

Mission Command is the default home:

- objective entry and goal templates;
- project and route preflight;
- editable acceptance plan;
- active run timeline;
- task graph and worker activity;
- approvals and review packets;
- candidate comparison;
- validation, diff, and browser evidence;
- local acceptance and GitHub handoff.

The layout uses:

- stable left navigation;
- a dominant central task surface;
- an optional context rail for project, route, budget, permissions, and
  evidence;
- progressive drill-down instead of parallel raw control panels.

### 8.2 Projects

- repository profiles and folder selection;
- allowed paths, recipes, budgets, policies, and capabilities;
- index freshness and repository-intelligence tools;
- project-scoped history, outcomes, and memory coverage.

### 8.3 Memory

- layer health and storage;
- search and evidence inspection;
- task capsules and promotion history;
- behavior deltas, activation, outcomes, and rollback;
- learning dashboards and consolidation controls.

Self and policy memory retain their stronger gates. The GUI never presents
ordinary learning as policy authority.

### 8.4 Flock

- provider and target inventory;
- local and explicit LAN discovery;
- routing previews and decisions;
- usage, costs, fallback, calibration, and regret;
- qualification runs and immutable receipts;
- scoped activation grants, suspension, and revocation.

### 8.5 Automate

- proactive routines;
- schedules, timezones, occurrences, and history;
- channels and destinations;
- delivery receipts and uncertain-outcome reconciliation.

The GUI says "idempotent admission and connector receipts," not universal
exactly-once delivery.

### 8.6 Extend

- built-in tools and effective capability state;
- MCP servers and tools;
- skills;
- plugins, provenance, locks, compatibility, and runtime blockers.

Review and enablement are separate actions. Installation never silently grants
authority.

### 8.7 Settings

Settings contains:

- General;
- Models and providers;
- Safety and permissions;
- Storage and memory;
- Containment;
- Appearance;
- Notifications;
- Updates;
- Diagnostics;
- an Advanced operator area for CLI/API/raw reports.

Global search finds both settings and the feature surface that owns them.

## 9. Settings model

Every setting declares:

- current configured value;
- effective value;
- blockers and their source;
- authority or privacy impact;
- whether it applies immediately, to new runs, or after restart;
- revision and last-change provenance;
- whether undo is available.

Writes use optimistic revision checks and transactional persistence. The GUI
must never show a successful toggle while an effective parent gate keeps the
capability blocked.

Default views use friendly controls and examples. Raw JSON is limited to
Advanced diagnostics and export; ordinary setup never requires it.

Advanced detail expands within the current task context. It does not replace
Mission Command with a different expert-only application.

## 10. Visual design

The approved visual direction is **Wildflower Workshop**.

### 10.1 Character

- warm editorial rather than cold enterprise;
- technically confident without looking militaristic;
- playful color used to improve hierarchy, not obscure state;
- tactile borders and offset shadows;
- restrained flight and field-note metaphors;
- gentle motion that communicates state transitions.

### 10.2 Foundation palette

The exact production tokens will be accessibility-adjusted, but the direction
uses:

- warm cream for primary reading surfaces;
- deep plum for navigation and structural anchors;
- coral for owner attention and important action;
- mint/teal for healthy connected state;
- acid lime for affirmative readiness and selected action;
- cornflower blue for contextual information;
- amber for bounded cost, caution, and active progress.

Color is never the only signal for risk, status, or approval.

### 10.3 Typography and geometry

- expressive editorial serif for major headings and selected data stories;
- highly legible sans serif for controls, navigation, evidence, and code;
- strong visible borders;
- modest corner radius;
- offset shadows on major actions and cards;
- organic flight shapes used sparingly as ambient decoration.

### 10.4 Accessibility and motion

- WCAG 2.2 AA contrast for text and interactive states;
- full keyboard navigation and visible focus;
- semantic landmarks and accessible names;
- scalable text without clipped controls;
- status text and icons in addition to color;
- reduced-motion mode that removes ambient movement and preserves functional
  transitions;
- light and dark behavior that retains the Wildflower identity rather than
  reverting to generic gray.

## 11. Update and rollback

Kestrel checks a signed update manifest after owner opt-in. It may download a
verified update in the background, but installation requires confirmation and
waits until runs, approval execution, and state migrations are idle.

Before installation Kestrel verifies:

- update signature and checksum;
- allowed version transition;
- sidecar and resource manifest;
- platform and architecture;
- migration compatibility;
- required disk space;
- current state health.

It then creates a consistent control-plane backup and a rollback receipt.

On first launch of the new version:

1. the new sidecar runs a bounded preflight;
2. schema migrations run transactionally;
3. the desktop verifies readiness and core health;
4. only then is the update marked accepted.

If startup or migration health fails, Kestrel restores compatible application
bytes and the matching control-plane snapshot. Memvid files are not casually
migrated or rewritten by the desktop updater. A future Memvid format
transition requires its own explicit, backup-bound migration contract.

Unsigned packages, incompatible skips, and unsafe downgrades fail closed with a
recovery explanation.

## 12. Failure and recovery behavior

### 12.1 Sidecar or renderer failure

- Renderer reload reconnects to the same authenticated sidecar when possible.
- Sidecar crash preserves durable runs and opens reconciliation after the one
  bounded restart.
- Pending approvals remain pending.
- No high-risk action is retried solely because the UI or sidecar restarted.

### 12.2 Provider and network failure

- Transport outages, capability mismatch, contract failure, and task-quality
  failure remain distinct.
- Partial usage and routing receipts are retained.
- Raw provider errors are redacted before persistence and display.
- Demo remains available even when every optional provider is offline.

### 12.3 State failure

- SQLite writes and settings use transactions and revision checks.
- Rebuildable indexes can be discarded and rebuilt from authenticated source.
- Canonical memory is never replaced with a sidecar index or recovery JSON.
- Corrupt or incompatible state opens read-only inspection and recovery rather
  than automatic destructive initialization.

### 12.4 Uninstall

Uninstall removes application bytes and managed launcher integration. It
preserves owner state by default. Memory/state deletion is a separate
explicitly named action that previews exact paths and warns that the deletion
is not recoverable without a backup.

## 13. Security requirements

- The desktop remains loopback-only for its private sidecar.
- Per-launch API tokens are owner-only, short-lived, nonce-bound, and never
  persisted in renderer storage.
- Native dialogs return bounded selected paths, not generic filesystem access.
- The Electron main process never exposes a generic shell execution API.
- Remote URLs open only through an allowlisted external-browser action after
  owner intent.
- Update metadata and packages are signature- and checksum-verified.
- Platform credential adapters implement the Secret Broker contract:
  - macOS Keychain;
  - Windows Credential Manager;
  - Linux Secret Service when available;
  - when no Linux Secret Service exists, either session-only credentials or a
    passphrase-unlocked encrypted vault implemented with an established
    authenticated-encryption library and KDF rather than custom cryptography.
- No insecure raw-vault fallback is silently selected for cloud credentials.
- Local/LAN discovery responses are untrusted, bounded, and schema-validated.
- Support bundles remain redacted and owner-reviewed before export.

## 14. Verification and acceptance

### 14.1 Source and contract gates

- full `pytest -q` after every implementation phase;
- Python compilation, Ruff, strict mypy, security/static gates, and lockfile
  checks;
- full React tests and production build;
- deterministic mock provider behavior;
- Memvid integration behind `RUN_MEMVID_INTEGRATION=1`;
- provider, MCP, and containment integrations behind their explicit gates.

### 14.2 Desktop security gates

- renderer sandbox and context-isolation assertions;
- no Node integration;
- restrictive CSP;
- blocked navigation/new-window behavior;
- strict preload surface snapshot;
- sender validation and schema/fuzz tests for IPC;
- no secret values in renderer, API payload, events, logs, support bundles, or
  memory;
- sidecar identity, nonce, token, listener, and parent-process adversarial
  tests.

### 14.3 Installed-artifact gates

The exact signed artifacts must pass clean-environment tests on macOS, Windows,
and Linux:

- install without Python, Node, or terminal setup;
- launch offline into working Demo mode;
- create or reopen exactly six permanent `.mv2` layer files;
- initialize/migrate SQLite safely;
- complete a deterministic first mission;
- persist settings and project selection;
- coexist safely with the advanced CLI and reject concurrent profile writers;
- restart without orphaned processes or listeners;
- uninstall while preserving owner data;
- reject tampered sidecars, resources, manifests, and update packages.

Twenty repeated launch/stop/update cycles must complete without process,
listener, state, or launcher residue.

### 14.4 GUI and accessibility gates

- Mission Command, Setup Center, Flock, and Settings rendered tests;
- keyboard-only completion of first-run setup and first mission;
- screen-reader names and landmarks;
- WCAG 2.2 AA contrast checks;
- reduced-motion checks;
- no horizontal overflow at supported desktop and narrow window sizes;
- snapshot coverage for light and dark Wildflower themes;
- visible configured/effective/blocker state for authority controls.

### 14.5 LAN discovery gates

- discovery never runs before explicit owner action;
- only the confirmed private scope and allowlisted service ports are probed;
- host count, concurrency, response size, redirect, and deadline limits hold;
- public ranges are rejected;
- manual host entry remains bounded;
- discovered targets are disabled drafts;
- stale address, model, certificate, or capability evidence blocks enablement;
- no secret or unrestricted network authority is inferred from discovery.

## 15. Definition of done

This design is complete when:

1. clean users on macOS, Windows, and Linux can install, open, set up, and run a
   Demo mission without a terminal or external runtime;
2. Mission Command is the primary product surface;
3. every currently supported Kestrel feature is reachable through the seven
   stable GUI destinations;
4. settings are tunable, truthful, revisioned, and reversible;
5. local and explicit LAN model discovery create disabled evidence-backed
   drafts;
6. the Electron renderer cannot bypass the authoritative Kestrel runtime;
7. secrets remain behind the Secret Broker;
8. updates and rollbacks bind application bytes and control-plane state;
9. exact installed artifacts pass the cross-platform and security gates;
10. the CLI remains available without being required for everyday use.
