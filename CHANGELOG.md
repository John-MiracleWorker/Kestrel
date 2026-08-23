# Changelog

All notable changes to Kestrel are documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Kestrel uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.8] - 2026-08-08

### Fixed

- Recovery release for the v0.5.7 tag, whose release workflow failed before
  publication and whose tag could not be moved under the "Protect v* release
  tags" ruleset (deletion and update both forbidden). Carries the identical
  v0.5.7 feature set (MIT relicensing, Windows vault write retry, IPv6 lease
  URL fixes, SetupCenter focus fix) plus lockfile security fixes for js-yaml
  (CVE-2026-59870) and nanoid (GHSA-2v37-7h3g-55p8).

## [0.5.7] - 2026-08-06

### Changed

- Relicensed Kestrel from Apache-2.0 to MIT. The v0.5.6 tag and its release
  pipeline were superseded before publication; this release carries the
  identical v0.5.6 feature set (Windows vault write retry, IPv6 lease URL
  fixes, SetupCenter focus fix) plus the permissive MIT license. The v0.5.7
  tag was superseded before publication; see 0.5.8.

## [0.5.6] - 2026-08-06

### Fixed

- Recovery release for the v0.5.5 tag, whose release workflow failed at the OCI
  digest binding step because the publish job ran `release_publication_guard.py`
  without installing its dependencies (`cryptography`). The publish job now
  installs the same hash-locked build bootstrap and Python dependencies as the
  build job before validating or publishing anything. Carries the identical
  v0.5.5 feature set.

## [0.5.5] - 2026-08-06

### Fixed

- Windows cross-process vault writes: retry the atomic temp→vault replacement with
  bounded exponential backoff when another process holds the vault open
  (`PermissionError [WinError 5]`), so concurrent `store_secret` callers no longer
  lose records or crash on lock contention. Non-Windows keeps single-attempt
  semantics.
- IPv6 lease URLs: map the `::` wildcard bind to the bracketed IPv6 loopback
  authority `http://[::1]:<port>/` (Uvicorn binds `::` with `IPV6_V6ONLY`, so the
  previous `127.0.0.1` self-URL advertised a dead endpoint), and bracket explicit
  IPv6 host literals per RFC 3986 so `--host ::1` no longer publishes a malformed
  lease URL. IPv4 wildcard binds still map to `127.0.0.1`.
- Setup workbench focus restoration defers to the next animation frame and retries
  briefly until the freshly mounted stage heading exists, ending the recurring
  React Testing Library `toHaveFocus()` CI flake at the wizard stage-transition
  boundary.

## [0.5.4] - 2026-08-04

### Fixed

- Recovery release for the burned `v0.5.2` and `v0.5.3` tags. Both were created on
  commits whose release evidence had not yet satisfied the release workflow's
  ordering gates when the tag push fired: `v0.5.2`'s commit had not finished a
  green main-branch CI run (a Windows `install.ps1` doctor probe that passed an
  empty docker context when the daemon was unreachable, plus two test-only
  flakes), and `v0.5.3`'s tag push raced its commit's release-rehearsal run by
  seconds, failing the rehearsal-before-release ordering check. The release tag
  ruleset makes `v*` tags immutable, so neither could be re-pointed. This release
  carries the identical Adaptive Flock feature set on a commit whose CI and
  release-rehearsal evidence were both green before the tag was pushed.

### Added

- Adaptive Flock Qualification + Activation: owner-run bounded qualification with
  HMAC-signed terminal receipts, per-scope activation grants, drift/decay/stale-target
  suspension with static fallback, terminal revocation, and a deterministic 20-replay
  qualification gate. Learned routing requires a durable grant; qualification alone
  grants no authority.
- Flock GUI workspaces for qualification drafts, run progress, per-scope results,
  and activation management (grant cards with server-verified effectiveness).
- Live-provider qualification evidence runner producing redacted release evidence
  reports bound to source commit and installed artifact digest.

### Security

- Hardened grant creation against in-process forgery (authority-bound ledger API),
  propagated per-attempt evidence kind into live reports, and required checked
  revisions on grant revocation. Independent adversarial review verdict: SOUND.

## [0.5.1] - 2026-07-29

### Fixed

- Recovery release for the burned `v0.5.0` tag. Its tag-triggered workflow
  completed the release candidate, multi-platform package, Memvid v2, web,
  container, and security gates, then failed before any public GitHub Release,
  PyPI, or GHCR publication because the staged installer dry run used the
  root-owned `/tmp` directory. The installer ownership guard correctly failed
  closed. Release validation now uses GitHub Actions' runner-owned
  `${RUNNER_TEMP}` while preserving that ownership boundary.

## [0.5.0] - 2026-07-29

### Added

- A unified engineering-run surface for Mission Control covering bounded graph amendments,
  isolated same-contract candidate fan-out and evidence-ranked selection, digest-bound OCI browser
  validation evidence, per-call approval packets, and review-bound GitHub pull-request/CI feedback.
- Project/time-filtered outcome analytics with explicit evidence coverage and missing-versus-zero
  metrics, redacted private benchmark fixtures, deterministic replay records, and redacted export.
- Five-field proactive-routine cron schedules in named IANA timezones, including deterministic DST
  gap/fold handling, plus destination-bound delivery receipts and explicit ambiguous-outcome
  reconciliation.
- Extension-manifest dependency locks, compatibility ranges, Ed25519/source-pinned provenance,
  authority-delta review, and reproducible install receipts.
- Intent-aware repository context packs that deterministically blend definitions, references,
  imports, and test ownership, plus a seven-language recall@5 and evidence-freshness CI gate.

### Changed

- Advanced the SQLite control plane to schema version 21 for cron schedules and durable routine
  deliveries; v20 introduced local project records and nullable run-to-project bindings.
- Replaced hard-coded provider/model suggestions in the workbench with discovered catalogs and
  the deterministic mock default.
- Defined the strongest-model baseline from the declared target quality tier rather than observed
  hindsight success, and separated browser evidence coverage from browser pass rate.

### Security

- Approval packets preserve an individual exact-call digest, capability revision, resource digest,
  and decision for every call; grouping does not broaden authority.
- GitHub publication requires a current signed repair review, unchanged reviewed commit tree,
  remote-mutation/push enablement, and exact-call approval. Feedback ingestion cannot mutate a
  reviewed request and recovery re-enters through a bounded graph amendment.
- GitHub publication accepts only canonical credential-free `https://github.com/<owner>/<repo>/pull/<number>`
  receipt URLs; query strings, fragments, user info, ports, and non-PR paths fail closed.
- Enabled plugin updates fail closed when a new manifest adds authority, and signed manifests fail
  closed when signature verification is unavailable or invalid.
- Plugin manifests reject raw registered secrets, and install receipts distinguish reproducible
  reviewed source from a runnable dependency environment; unmanaged dependencies cannot claim
  runtime reproducibility.
- Ambiguous external routine delivery is recorded as `uncertain` and never automatically retried;
  reconciliation is an explicit owner action tied to the original idempotency key.
- Provider and operator routine-delivery receipts are bounded, finite, and secret-safe before
  durable persistence; provider receipt fields are redacted and raw reconciliation secrets fail
  closed.
- Engineering mutation APIs require configured owner authentication, and registered secrets are
  refused before browser-validation commands or deterministic fixtures can enter containment.
- Graph-amendment payloads, candidate results/review provenance, and approval-packet display fields
  reject registered secrets before hashing or durable persistence; graph and candidate JSON also
  reject non-finite values.
- Browser evidence must return one schema-valid result for every exact requested assertion and
  interaction; missing, reordered, renamed, or non-boolean proof cannot produce a passing record.

## [0.4.11] - 2026-07-26

### Fixed

- Recovery release for the burned `v0.4.10` tag — the third burned tag in a
  row. `v0.4.10`'s tag-triggered release workflow published its GHCR images
  and attestations successfully, but the GitHub Release step failed: under
  immutable releases, `gh release create --draft` produces an *untagged*
  release, and the workflow then queried `releases/tags/<tag>`, which 404s
  on untagged drafts. Nothing for `v0.4.10` was ever fully published as a
  GitHub Release or uploaded to PyPI (the leftover empty draft release was
  deleted), and repository rulesets block tag deletion, so `v0.4.10` must be
  treated as a burned tag. This `v0.4.11` release ships the same content plus
  the query-by-`databaseId` immutable-draft fix (commit 9a3e778), and carries
  forward the `v0.4.8` publish-gate fix (PR #312: the immutable-releases gate
  is read via a PAT stored as `RELEASE_GUARD_TOKEN`) and the `v0.4.9`
  GET-not-HEAD GHCR manifest existence check fix (commit 5126c16).

## [0.4.10] - 2026-07-26

### Fixed

- Recovery release for the burned `v0.4.9` tag. `v0.4.9`'s tag-triggered release
  workflow failed deterministically at the publish step (twice) with
  `curl: (18) transfer closed with 70 bytes remaining`: its GHCR manifest
  existence check used `curl --request HEAD`, and GHCR answers HEAD with a
  `content-length` header but no body, so curl aborts with exit 18 under
  `set -e` on the not-yet-published manifest. Nothing for `v0.4.9` was ever
  published — no GitHub Release, no PyPI upload, no GHCR image — and
  repository rulesets block tag deletion, so `v0.4.9` must be treated as a
  burned tag. This `v0.4.10` release ships the same content plus the
  GET-not-HEAD GHCR manifest existence check fix (commit 5126c16), and
  carries forward the `v0.4.8` publish-gate fix (PR #312): the
  immutable-releases gate is read via a PAT stored as `RELEASE_GUARD_TOKEN`,
  and immutable releases are enabled on the repository.

## [0.4.9] - 2026-07-26

### Fixed

- Recovery release for the burned `v0.4.8` tag. `v0.4.8` was tagged at a pre-fix
  commit, and its tag-triggered release workflow failed at the publish gate with a
  403 reading the repository's immutable-releases admin setting: the default
  `GITHUB_TOKEN` cannot read admin settings. Nothing for `v0.4.8` was ever
  published — no GitHub Release, no PyPI upload, no GHCR image — and repository
  rulesets block tag deletion, so `v0.4.8` must be treated as a burned tag. This
  `v0.4.9` release ships the same content plus the publish-gate workflow fix
  (PR #312): the immutable-releases gate is now read via a PAT stored as
  `RELEASE_GUARD_TOKEN`, and immutable releases are enabled on the repository.

## [0.4.8] - 2026-07-26

### Fixed

- Wheel-smoke soak throughput floor given shared-runner headroom (1.0 → 0.75
  runs/s) after v0.4.7's release run missed by 1.7% on runner noise (#309).

## [0.4.7] - 2026-07-26

### Fixed

- Re-tag of the v0.4.6 content: the `v0.4.6` tag was inadvertently pushed pointing
  at the v0.4.5 commit before its release PR merged, and repository rulesets block
  tag deletion. `v0.4.6` should be treated as a burned tag; this release carries the
  identical code that was reviewed and merged as PR #306 (Intel macOS support
  removal across the release matrix, installer, and docs).

## [0.4.6] - 2026-07-25

### Removed

- Dropped the release workflow's `macos-15-intel` exact-wheel verification rows. The
  `cryptography` dependency stopped shipping Intel-macOS wheels in 49.0.0, so pip cannot
  resolve a binary distribution on that platform. Kestrel never claimed Intel-mac support
  (docs and CI only cover arm64 macOS); the release gate now matches reality.

## [0.4.5] - 2026-07-25

### Fixed

- Fixed the release workflow's OCI label verification: the `docker image inspect --format`
  Go templates wrapped label keys in `\"` escapes that YAML block scalars pass through
  literally, so Go template parsing failed (`unexpected "\" in operand`) after the
  container scan had already passed. Label keys are now quoted correctly (verified live
  against a labeled image).

## [0.4.4] - 2026-07-25

### Fixed

- Fixed the release probe's minimum-throughput acceptance check under QEMU emulation: the
  soak measures throughput against wall-clock load time, so 4 emulated runs (~30s each)
  landed at 0.066 runs/s against the 0.1 floor despite 4/4 completion, p95 within budget,
  and exact request accounting. The throughput floor is now per-architecture (0.1 runs/s
  native amd64, 0.01 runs/s emulated arm64).
- Fixed `uv.lock` corruption from the 0.4.3 version bump: the string replace also rewrote
  two upstream packages at 0.4.2 (`pyasn1-modules`, `typing-inspection`), desyncing their
  version fields from wheel URLs and breaking the hash-locked container build.

## [0.4.3] - 2026-07-25

### Fixed

- Fixed the release workflow's read-only container probe soak budget: the arm64 probe runs
  under QEMU emulation (~20x slower than native), so a shared 30s p95 budget was unreachable
  despite 4/4 runs completing with zero failures. The budget is now per-architecture (30s
  native amd64, 120s emulated arm64) with identical functional assertions for both.

## [0.4.2] - 2026-07-25

### Fixed

- Fixed the release workflow's smoke/doctor memory-dir parents: `MemvidBackend` places its
  memory lock file in the memory directory's parent, and the v0.4.x lock hardening validates
  parent ownership — so any doctor or smoke run whose memory dir sat directly under
  root-owned `/tmp` failed with `Sensitive artifacts must be owned by the current user: /tmp`.
  Container doctors now use `/data/doctor-memory*` (owned by the runtime user in the image)
  and runner-side wheel/sdist/server smokes use `$RUNNER_TEMP`. Verified live in the built
  release image: doctor green with all six memory layers verified.

## [0.4.1] - 2026-07-25

### Fixed

- Fixed the release workflow's container validation step: `nest-agent doctor` runs as the
  image's non-root runtime user (uid 999), but the Memvid memory directory was created under
  root-owned `/tmp`, tripping the sensitive-artifact ownership guard and aborting the release
  before artifacts published. The workflow now creates the directory inside the container as
  the runtime user before running doctor.
- Made `temperature` optional across every provider adapter, the agent config, runtime
  settings, planner/reviewer diversity calls, and learning evaluation. When unset, no
  temperature field is sent on the wire and each provider applies its own default. This fixes
  hard failures on providers that pin temperature (e.g. Kimi requiring exactly 1), including
  the planner/reviewer sub-calls that previously hardcoded `0.0`.
- Sanitized dotted tool names on the OpenAI-compatible wire (dots to underscores) with
  canonical-name restoration on responses, fixing HTTP 400 rejections from providers that
  disallow `.` in function names.

## [0.4.0] - 2026-07-24

### Added

- Evidence-backed provider certification reports with explicit generation, streaming, native-tool,
  tool-normalization, learning-E2E, tested-model, freshness, exact-subject, and missing-requirement
  fields across every implemented provider surface.
- A deterministic `collect` / `build` / `check` provider-certification runner that consumes exact
  JSON evidence schemas and conservatively scoped provider-test JUnit XML, binds canonical receipt
  IDs to every source digest, keeps credentials redacted, and fails closed for missing or
  insufficient evidence.

### Changed

- Advanced the provider-certification schema from v1 to v2 while retaining the existing command,
  API route, and legacy readiness fields. Implementation, current-machine readiness, and
  evidence-backed assurance are now reported as separate concepts.

### Fixed

- Made behavior-delta activation logs report only trigger predicates that actually matched,
  including path-glob and risk-tag matches, without inventing a semantic-context fallback.
- Sanitized dotted tool names (`tool.registry` → `tool_registry`) on the OpenAI-compatible wire
  and restored canonical names on the response path, fixing native tool calling against providers
  that reject dots in function names (e.g. Kimi).
- Made temperature optional end to end and omitted it from provider requests unless explicitly
  configured via `NEST_AGENT_TEMPERATURE`, fixing planner/reviewer and eval calls against
  providers that reject pinned temperature values (e.g. Kimi, which requires exactly 1).

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

[Unreleased]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.5.8...HEAD
[0.5.8]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.5.7...v0.5.8
[0.5.7]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.5.6...v0.5.7
[0.5.6]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.5.5...v0.5.6
[0.5.5]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.5.1...v0.5.4
[0.5.1]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.11...v0.5.0
[0.4.11]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.10...v0.4.11
[0.4.10]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.9...v0.4.10
[0.4.9]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.8...v0.4.9
[0.4.8]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.7...v0.4.8
[0.4.7]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.5...v0.4.7
[0.4.6]: https://github.com/John-MiracleWorker/Kestrel/releases/tag/v0.4.6
[0.4.5]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.4...v0.4.5
[0.4.4]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.3...v0.4.4
[0.4.3]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/John-MiracleWorker/Kestrel/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/John-MiracleWorker/Kestrel/releases/tag/v0.2.0
