# Changelog

All notable changes to Kestrel are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Kestrel uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

No changes yet.

## [0.4.0] - 2026-07-20

### Added

- Bounded exact recent-turn reconstruction so follow-up messages retain explicit user/assistant
  continuity independently of semantic retrieval, while internal runtime turns are excluded from
  native user-dialogue replay.
- Coherent full-agent backup and restore for Memvid layers, SQLite state, run capsules, runtime
  configuration, skills, and plugins, with checksums, pre-restore safety snapshots, and
  cross-component rollback.
- Persisted semantic-plan and evidence-backed reviewer artifacts, with optional provider semantic
  planning/review behind an explicit default-off setting.
- Community contribution, conduct, governance, security-reporting, ownership, and issue/PR templates.
- Credential-free Memvid v2, Memvid golden-evaluation, and MCP fixture coverage in pull-request,
  branch, and release CI as applicable.
- Exact-tag release validation across supported Linux, macOS, and Windows/Python combinations,
  with tag-to-`main` ancestry enforcement and history-aware secret scanning.
- A metadata consistency check that keeps the Python distribution, private web package, stable installer, security support line, and changelog base aligned.
- Disabled-by-default proactive UTC routines with revision-checked owner controls, deterministic leased occurrences, bounded background polling, internally scoped run provenance, CLI/API/workbench editing and history, idempotent manual run-now, and ordinary exact-call tool approvals.
- Digest-pinned OCI execution for executable skills with default-deny scopes and a required real-Docker containment gate in CI and release validation.

### Changed

- Standardized user-facing default branding on Kestrel while retaining the published
  `nested-memvid-agent` distribution, `nested_memvid_agent` import package, and compatibility CLI.
- Made the README documentation and community map directly navigable.
- Made non-secret runtime settings revision-checked and transactional across validation,
  owner-private persistence, live activation, and approval revocation, with rollback on activation
  failure and bounded conflict retry for Telegram admin writes.
- Made deterministic memory snapshots coherent across same-process and cross-process writers, and
  bound lexical retrieval to stable record IDs after an update.
- Made each Memvid v2 file the canonical event timeline, with digest-verified cache reconstruction,
  logical pagination over chunked records, tombstone/correction replay, hash-chain continuity, and
  serialized shared-handle access.
- Advanced the SQLite control-plane to schema version 19. Schema v16 made queued and recovered runs
  retain serialized turn source, origin, and transcript scope; v17 added revisioned routines,
  occurrence leases/generation fencing, and atomic scheduled-run admission; v18 adds durable,
  renewable approval-execution claims and exact scheduler task/subagent continuation bindings; v19
  adds hashed manual-routine idempotency claims and trigger provenance.

### Fixed

- Prevented Memvid run, subagent, scheduler, and manual-endpoint deadlocks by admitting one
  cancellable agent lifecycle per runtime, keeping additional primary runs in the durable FIFO
  queue, and releasing primary layer handles before autonomous scheduler workers start.
- Bounded dense-vector top-k selection to the available corpus so normal retrieval remains valid
  for empty and small memory layers.
- Confined task capsules and configured memory/vector artifacts to validated portable path
  components, including derived lock and sidecar collision checks.
- Made scheduler task/subagent transitions, run-lease fencing, public subagent approval handoff,
  startup worker recovery, and proactive-routine lifecycle admission atomic across normal,
  cancellation, failure, and restart paths.
- Suppressed automatic replay when startup finds an interrupted or stale approved side effect,
  including a durable result whose scheduler continuation is missing or has already advanced.
- Made `nest-agent doctor` return a failing process status whenever its JSON readiness report is
  not healthy, and aligned container smoke checks with the owner-private `/data/memory` volume.
- Made containers compile Memvid `2.0.160` from its hash-verified source distribution in a
  throwaway build stage, validated the native import during image construction, and reported
  installed-but-unloadable SDK failures accurately.
- Rebased the runtime image to digest-pinned Debian Trixie, pinned every Docker build-stage base,
  and required both the Apache license and generated third-party notice in installed image metadata.
- Isolated the Codex CLI response provider from ambient user model and reasoning configuration
  while retaining the user's existing Codex authentication.
- Normalized host-only Ollama endpoints to their OpenAI-compatible API root, bounded native tool
  exposure by relevance, preserved assistant call/result continuity across provider protocols, and
  suppressed only exact successful duplicate calls.
- Aligned CLI completion waits with configured provider retry and summary budgets, made summary
  failure fall back deterministically, and surfaced bounded shutdown failures without tracebacks.
- Made installer upgrades migrate a private state candidate, atomically swap it only after validation,
  preserve original database/WAL/journal bytes through readiness, and reacquire runtime plus Memvid
  locks before rollback.
- Preserved web idempotency keys across ambiguous retryable failures and made explicit server
  validation failures authoritative rather than treating transport success as proof of validity.
- Preserved approved Git patches as exact UTF-8 bytes across subprocess boundaries so Windows
  newline translation cannot corrupt repair or general patch application against LF worktrees.

### Security

- Recalled memory and failure lessons now enter model requests as JSON-encoded, untrusted
  user-role evidence rather than system-priority instructions.
- Soul system context now accepts only the fixed persona preset selected through authenticated
  onboarding; display labels and free-form preferences remain bounded untrusted user-role JSON.
- Policy system context now requires a durable owner-approved `memory.policy_promote` receipt,
  structured repeated evidence, an exact argument digest, and a matching recorded result.
- Scheduler, subagent, and approval-continuation turns now carry internal transcript scope and
  cannot replay later as native user messages; approved tool output is JSON-wrapped as untrusted data.
- Imported memory is stripped of runtime transcript-authority fields and stamped as untrusted data;
  replay also requires the current turn's exact primary/channel scope and origin.
- Channel session keys preserve existing safe IDs while collision-prone normalized, truncated, empty,
  or separator-ambiguous identifiers receive a versioned tuple digest.
- Trusted onboarding and policy candidates are authenticated before bounded selection, preventing
  untrusted high-ranking records from crowding authenticated persona or policy context out.
- Test/lint/pass criteria require validation-producing evidence; unrelated successful tools cannot
  satisfy either deterministic or provider semantic review.
- Semantic planner and reviewer requests recursively redact credentials before calling a provider.
- Outbound channel delivery now rejects redirects, validates and pins public DNS results through
  connection setup, fails closed on rebinding, and avoids echoing token-bearing webhook URLs.
- Docker build contexts now exclude every local `.env*` file and unrelated workspace trees.
- Default installer, release, and container dependency graphs now include the optional OS-keyring
  client, while keyring selection still fails closed without a usable host credential service and
  populated JSON vaults cannot be reinterpreted in place.
- Assistant transcript frames are redacted before persistence, and coherent backups deliberately
  exclude raw Secret Broker values.
- Agent restore preserves recovery artifacts and surfaces its safety snapshot if rollback itself
  cannot be completed; retention never prunes unrelated directories in a shared backup root.
- Agent restore binds the requested backup ID, directory name, and manifest ID and rejects symlink
  aliases; canonical embedded layer maps remain portable to a clean host.
- Sensitive SQLite, event-log, memory, vector-sidecar, capsule, settings, and Secret Broker
  artifacts now use owner-only POSIX permissions and reject symlink, hard-link, non-regular, and
  foreign-owner aliases before reads, writes, or permission repair.
- Support-bundle event tails now use default-deny string redaction, bounded reverse reads, and omit
  prompts, messages, commands, errors, and other arbitrary nested text.
- Approved side effects now require an exclusive durable execution claim before dispatch. Only the
  exact claimant may record the result; dead or expired claim recovery records an unknown outcome,
  fails the bound scheduler pair closed, and never retries the side effect automatically.
- Repair validation and review artifacts are create-once, durably signed, redacted, and bound to an
  unchanged candidate snapshot, exact proposal, run, and session. Signing keys and validation
  evidence survive coherent backup/restore, while incompatible legacy restores fail closed.
  Literal-tree commits bypass filters/hooks/signing, while rollback requires the approved current
  diff and quarantines overwritten files before raw restoration.
- Stable-memory promotion now revalidates evidence and durable receipts at recall, rejects
  cross-claim and cross-run replay, caps caller-asserted human evidence, and permits ordinary
  semantic/procedural promotion without weakening the owner-approved policy path.
- Executable skills cannot run Python or shell code on the host. The OCI path requires explicit
  enablement and approval, a pinned image and unchanged tree/scope digests, no network, a read-only
  root filesystem, nonroot execution, dropped capabilities, resource bounds, and timeout cleanup.
- Tool timeouts now use bounded cancellation and settlement, retain resources for unsettled workers,
  return nonretryable reconciliation-required outcomes when quiescence is unknown, and quarantine the
  affected tool within its owning runtime. Windows subprocesses enter kill-on-close Job Objects while
  suspended before execution resumes.
- Full-agent backup and restore bind descriptor identities, reject undeclared or aliased components,
  stream into owner-private exclusive stages, and roll back across `BaseException` interruptions.
- Release publication verifies the complete immutable GitHub payload before registry mutation, binds
  OCI digests into checksummed evidence, and permits PyPI recovery only for exact filename/SHA matches
  from the already verified release artifact.

## [0.3.1] - 2026-07-16

### Security

- Pinned and audited release bootstrap dependencies so clean installs use the secured package set.
- Added isolated install and shipped-environment audit evidence to the release path.

## [0.3.0] - 2026-07-16

### Added

- Production hardening for the supported single-user, single-node local/private profile.
- Durable run leases, recovery, bounded scheduling, capability controls, provider resilience,
  support diagnostics, release evidence, and packaged web-workbench validation.
- Cross-platform Python CI, credential-free Memvid/MCP integration, dependency audit, SBOM,
  checksums, and clean release-install smoke coverage.

### Security

- Strengthened secret redaction, exact-call approvals, API ingress controls, webhook verification,
  subprocess boundaries, MCP lifecycle controls, and default-off dynamic capabilities.

## [0.2.1] - 2026-07-13

### Fixed

- Hardened one-shot installation and isolated dependency verification across the supported platforms.

## [0.2.0] - 2026-07-13

### Added

- First tagged Kestrel-branded local alpha release with the conversational runtime, layered Memvid v2
  memory, workbench, tools and approvals, deterministic mock path, installer, and release artifacts.

[Unreleased]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/John-MiracleWorker/Kestrel/releases/tag/v0.2.0
