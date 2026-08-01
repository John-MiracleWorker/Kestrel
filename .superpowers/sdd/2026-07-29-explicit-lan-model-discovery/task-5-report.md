# Task 5 implementation report

## Identity, milestones, and scope

- Overall Task 5 base SHA: `bf91cb8b31030010cf10f98c79df6c9e6f98dd48`
- Task 5A feature milestone SHA: `da26eea046afb6804df73044c7a95cb1133234b7`
  (`feat: import LAN endpoints as disabled target drafts`)
- Task 5A review-clean final SHA and Task 5B base SHA:
  `424378a2d95a32458a61ca077d5a4f5d9e129f2e`
  (`fix: close LAN draft review gaps`)
- Task 5B feature milestone SHA:
  `dbbc32b1573a743a694f05a6b2de493c1b7d2de1`
  (`feat: harden LAN model runtime transport`)
- Intended bounded review-correction subject:
  `fix: preserve LAN profile authority invariants`
- Worktree: `/Volumes/12.45/Codex-Offload/kestrel-gui-first-integration`

The Task 5B review-correction final commit identity is external to this report by
construction: a commit cannot embed its own resulting SHA. The controller will
record that exact identity in `progress.md` and in the fresh-range review
immediately after commit.

Task 5A changed the 13 authorized source/test files required for strict Task 4
evidence import, durable LAN-managed drafts, review, mutation fences, routing
defense, and the two fixed-owner HTTP routes. The feature milestone contained
11,558 insertions and 123 deletions. Its bounded review correction changed five
of those files with 738 insertions and 32 deletions.

Task 5B's feature-milestone diff from
`424378a2d95a32458a61ca077d5a4f5d9e129f2e`
contains 20 production source files and 11 test files, plus this report. It adds
the neutral internal authority type, coherent durable/live resolver, direct LAN
runtime transport/provider, no-tool agent path, authority propagation and
clearing, routing eligibility corrections, and adversarial coverage. It does
not add scan-manager lifecycle, UI, launchd, automatic activation, learned-route
activation, grant state, TLS, generic Ollama execution, packaging, publishing,
or release qualification.

Two controller-authorized Task 5B scope expansions were required after review:

- `src/nested_memvid_agent/runtime_settings.py` only clears the internal
  nonserializable authority when applying or restoring settings.
- `src/nested_memvid_agent/routing/contracts.py` and
  `tests/test_agent_routing_foundation.py` make the narrow routing-context
  correction described below while preserving all explicit and safety floors.

No other feature scope expansion was accepted. The bounded review correction
changes only `src/nested_memvid_agent/routing/ledger_registry.py`,
`tests/test_lan_discovery_service.py`, and this tracked report; staging and commit
ownership remain with the controller.

## Task 5A: strict durable import and review boundary

Task 5A persists the Task 4 typed evidence without turning discovery into
execution authority:

- The adapter accepts only the exact Task 4 immutable observation, catalog,
  capability, endpoint, receipt, and scan-network shapes. It reconstructs each
  value, recomputes every domain-separated digest, verifies exact receipt
  membership and completion, and rejects public-payload/column disagreement.
- Stable profile, target, and material identities are full domain-separated
  SHA-256 values. The same endpoint/model remains stable across scan IDs and
  timestamps; provider/profile/target domains and different provider shapes do
  not collide.
- Import and review are atomic CAS transactions. First imports are local,
  disabled, unconfirmed, secret-free drafts. No single observation, ordinary
  provider update, or generic HTTP mutation enables them.
- The reviewed immutable preimage includes the pre-write profile and target
  revisions, exact governing observation/receipt/endpoint/material digests,
  privacy acknowledgement, intended roles/task families, stale-reason
  acknowledgement, and review digest. Review cannot claim capabilities stronger
  than the evidence.
- An exact unchanged positive refresh may preserve an existing review. Material
  drift, stale evidence, replacement, or expiry clears the complete review,
  trust, health, role, and authority state required by the affected binding.
- Profile-latest evidence and a target's governing evidence are deliberately
  distinct. An incomplete catalog may advance the profile's latest evidence and
  add a disabled new target without changing the existing target's governing
  observation, revision, material binding, or freshness.
- Endpoint-wide changes stale the profile and all dependent targets;
  target-specific changes affect only the relevant target. Outage-only evidence
  records no new identity, does not rewrite positive capability authority, and
  cannot automatically rebind an address.
- Replacement requires the exact old profile revision, fingerprint, and material
  digests. A successful replacement atomically stales the old binding; forged,
  omitted, raced, or partially applied replacement state rolls back.
- Reserved LAN IDs and `lan_discovery` metadata are fenced at generic registry,
  inventory, provider, target, and direct mutation surfaces. Ordinary resources
  retain their existing behavior.
- The import/review routes use only the fixed principal
  `owner:local-runtime:v1`. Body/query principal fields, named identity headers,
  raw network/evidence fields, malformed target paths, and query strings are
  rejected. Sensitive query/path material is redacted before access logging.
- Router defense reconstructs managed LAN identity and Task 4 model validity,
  checks enabled/reviewed/fresh/healthy state and evidenced capabilities, and
  rejects direct overrides or corrupt rows. At the Task 5A milestone every LAN
  row remained `lan_runtime_hardening_unavailable`, including reviewed rows.

The review-clean `424378a2d95a32458a61ca077d5a4f5d9e129f2e` correction made
that boundary stricter without enabling runtime use. It canonicalized managed
model identities through `LanProbeModel`, re-derived the profile ID from the
endpoint binding, rejected whitespace-corrupt scan IDs, redacted malformed review
paths/query strings before the real Uvicorn access log, applied the smaller of
the global and 32 KiB LAN body limits to chunked requests, and proved that
unregistered LAN-shaped paths keep ordinary global ingress behavior.

## Task 5B: executable authority and closed LAN runtime

### Internal authority and coherent resolution

- `lan_runtime_authority.py` is a neutral dependency root. Its frozen, slotted
  internal snapshot is not an external config value and has no serialization
  surface.
- `AgentConfig.lan_runtime_authority` is `repr=False` and `compare=False`, is
  excluded explicitly from public mappings and effective settings, and cannot be
  supplied by JSON, environment, CLI, API, snapshot restoration, support bundle,
  capsule, receipt, event, or log surfaces. Only the routing service installs it
  with an in-process `dataclasses.replace()` after current resolution.
- `RoutingRegistry.resolve_lan_runtime_authority()` uses one coherent SQLite read
  transaction to load and revalidate the exact profile, target, governing
  observation, completed terminal receipt, and scan network. It recomputes the
  receipt, membership, protected metadata, review/material/endpoint/interface
  digests, installed adapter marker, and freshness instead of composing public
  getters across separate connections.
- The resolver then enumerates a fresh full interface inventory and requires one
  exact authoritative OS interface identity, attached-address set, confirmed
  network, selected source, endpoint, and numeric ifindex. Display-name drift is
  nonauthoritative; identity/network/source/index drift is fatal.
- The reviewed runtime-interface binding is one centralized domain-separated
  digest over OS identity, source, ifindex, interface/network/endpoint evidence,
  material binding, and review binding. Import/review and runtime resolution use
  the same derivation.
- The runtime constructs one `LanRuntimeAuthorityResolver` object and injects that
  exact object through coordinator, routing service, routing run manager, ordinary
  run manager, agent factory, provider factory, provider, and transport. Preview
  services may omit it but cannot produce an executable LAN assignment.
- A LAN config is executable only when the snapshot and freshly resolved binding
  match provider profile, target, model, strict base URL, API shape, adapter,
  installed hardening version, endpoint, material digest, review digest, source,
  and interface. Missing, duplicate, stale, disabled, re-reviewed, drifted, or
  expired evidence fails before agent construction or request bytes.
- Every non-LAN, fallback, shadow, reuse, failure, delegation, nonactionable,
  settings-apply, settings-restore, and ordinary routing path explicitly clears
  any prior authority. Reconstructed configs and snapshots are authority-free and
  require fresh routing resolution before later LAN execution.

### Direct numeric runtime transport and provider

- `LanOpenAICompatibleProvider` is available only for the exact
  `lan-openai-compatible` adapter and shared hardening marker. The factory rejects
  missing authority/resolver, generic provider fallback, stream configuration,
  API keys/secret references, a mismatched model/profile/target/material/review
  binding, and any base URL other than canonical numeric private `http://.../v1`
  on one of Task 1's four known ports.
- Only a new exact positive OpenAI-compatible import or re-import after the
  hardening marker is installed can create an enableable draft. Task 5A drafts
  cannot be upgraded merely by review. Missing/wrong markers and every Ollama
  draft remain disabled; there is no generic-client fallback.
- `DirectLanRuntimeTransport` is a separate closed runtime boundary; it does not
  expose an arbitrary-body escape hatch or mutate Task 4's discovery limits. It
  accepts only an internally canonicalized OpenAI-compatible chat-completions
  request and only `POST /v1/chat/completions`.
- It uses raw numeric `AF_INET`/`AF_INET6` sockets with exact source binding and
  Darwin/Linux interface pinning. It does not use DNS/getaddrinfo, HTTPX, urllib,
  the OpenAI SDK, proxy environment, cookies, redirects, custom headers,
  credentials, or automatic retries.
- The transport re-resolves authority before opening the socket, after connect,
  and immediately before `sendall()`. The final pre-send resolution is the
  authorization linearization point. Only a newer `fresh_until` with every other
  authority value and digest unchanged may continue; disablement, stale import,
  expiry, material drift, or re-review prevents bytes.
- Interface/source authority is freshly rebuilt before each socket. Link-local
  IPv6 uses the authenticated ifindex in source and destination sockaddrs;
  ULA/private IPv6 uses zero scope IDs while remaining interface-pinned.
- Fixed credential-free headers use numeric `Host`, `Accept-Encoding: identity`,
  and `Connection: close`. Exact HTTP 200 is required; redirects are terminal and
  never open a second connection.
- The canonical request is bounded to 1 MiB. The response is bounded to 16 MiB
  plus one sentinel byte. Status/header/chunk framing uses the strict Task 4
  grammar with runtime-specific immutable limits and duplicate-key-free JSON.
- One absolute monotonic deadline is the minimum of requested timeout, 120
  seconds, and remaining authority freshness. All sockets close on success,
  timeout, cancellation, partial parse, protocol error, or policy failure.
- The response must contain exactly one string at
  `choices[0].message.content`. Raw response bodies and response-advertised model
  or provider identity are discarded and never enter errors/logs.
- LAN operational health identity is a full domain-separated SHA-256 over
  provider, model, strict base URL, material digest, and review digest. It exposes
  neither authority digest and prevents a changed material/review binding from
  inheriting circuit state.

### No-tools and routing semantics

- `ProviderCapabilities.supports_tools` now defaults to `True`, preserving
  existing providers. The LAN provider sets tools, native tools, JSON mode, and
  streaming to false.
- For a no-tools provider, the agent supplies an empty catalog and a bounded
  no-tool instruction. The provider independently rejects nonempty direct tool
  lists, tool roles, and tool-call metadata. Streaming fails before network.
- The agent treats a tool call returned by a no-tools provider as a policy
  violation before registry lookup or executor dispatch. Secret detection still
  runs on the raw provider text before sanitized failure handling.
- The context correction is intentionally narrow: a low-risk, final, general,
  simple task with complexity below `0.5` may remain without an invented context
  minimum. Explicit requirements and existing floors for initial/non-final,
  non-low-risk, non-general, complex, or guidance-constrained work are preserved.
  The router remains strict when a context minimum exists.
- Freshness boundary semantics are consistent at equality: an authority is
  expired when `now >= fresh_until`, both in preview/eligibility and execution.

## TDD receipts

### Task 5A RED and GREEN

The required pre-production RED command was:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q \
  tests/test_lan_http_transport.py \
  tests/test_lan_scanner.py \
  tests/test_lan_discovery_service.py \
  tests/test_lan_discovery_ledger.py \
  tests/test_server_routing_discovery.py \
  tests/test_adaptive_flock_provider_update_semantics.py \
  tests/test_agent_routing_guardrails.py
```

It exited nonzero for the expected missing strict adapter/service,
LAN-specific transactions and mutation fences, import/review routes, and routing
defense. The available durable controller evidence does not retain the exact
numeric failure total for this historical RED run. This report records that
limitation explicitly and does not reconstruct or guess a number.

The Task 5A focused final gate at the review-clean milestone exited 0 with
`561/561` tests passing. The full repeatable Python phase gate at
`424378a2d95a32458a61ca077d5a4f5d9e129f2e` exited 0 with:

```text
3448 passed, 81 skipped, 6 deselected
```

The warning boundary was one known pre-existing Starlette/httpx deprecation
warning. Task 5A had no open P0/P1/P2 review finding when Task 5B began.

### Task 5B frozen RED

The frozen ten-file Task 5B collection contained `873` tests. It established the
absence of the internal authority type, direct runtime transport/provider,
no-tools capability path, resolver propagation, and exact hardening-marker enable
transition while Task 5A still rejected enabled LAN review.

Targeted RED diagnostics recorded during that phase included:

- provider/factory selection: `3 failed, 11 passed`, covering the absent resolver
  injection, insufficient LAN health identity, and shared circuit isolation;
- routing guardrails: `15 failed`, covering authority clearing, resolver/service
  construction, clock/freshness handling, and managed apply semantics;
- provider-update semantics: `2 failed`, covering the absent runtime resolver
  attribute and exact shared resolver propagation.

The frozen tests received only audited fixture corrections before production work:
canonical IDs, explicit preloads/reloads, exact CAS inputs, terminal-trigger
handling, ifindex alignment, correct injected clocks, structurally valid
marker-missing versus forced-invalid legacy rows, provider clock injection, and
ordinary-context fixture repair. These changes made fixtures satisfy the frozen
typed contracts; they did not weaken product assertions or turn an expected RED
into a permissive test.

### Task 5B feature-milestone GREEN gates

Before the final expiry-equality delta, this exact ten-file gate passed
`908/908`. The feature-milestone rerun of the same gate included the added equality
regression and was:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q \
  tests/test_lan_openai_compatible_provider.py \
  tests/test_lan_runtime_transport.py \
  tests/test_lan_discovery_service.py \
  tests/test_agent_runtime.py \
  tests/test_config.py \
  tests/test_full_agent_runtime.py \
  tests/test_llm_providers.py \
  tests/test_server_runtime_routes.py \
  tests/test_adaptive_flock_provider_update_semantics.py \
  tests/test_agent_routing_guardrails.py
exit 0: 909 passed, 1 warning in 78.41s (0:01:18)
```

The warning was the same pre-existing Starlette/httpx deprecation.

The feature-milestone 15-file Task 4/5/routing/provider superset included the added
expiry-equality regression and was:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -q \
  tests/test_lan_http_transport.py \
  tests/test_lan_scanner.py \
  tests/test_lan_discovery_service.py \
  tests/test_lan_openai_compatible_provider.py \
  tests/test_lan_runtime_transport.py \
  tests/test_lan_discovery_ledger.py \
  tests/test_agent_runtime.py \
  tests/test_config.py \
  tests/test_full_agent_runtime.py \
  tests/test_llm_providers.py \
  tests/test_server_routing_discovery.py \
  tests/test_server_runtime_routes.py \
  tests/test_adaptive_flock_provider_update_semantics.py \
  tests/test_agent_routing_guardrails.py \
  tests/test_provider_probe.py
exit 0: 1232/1232, 1 warning (the same pre-existing Starlette/httpx deprecation)
```

After the final expiry-equality correction, the focused regression receipts were:

```text
tests/test_agent_routing_guardrails.py: 47/47
tests/test_agent_routing_foundation.py: 20/20
reviewer-focused equality set: 6/6
```

The full routing test suite also exited 0 at the feature milestone.

## Static and repository-wide verification

Feature-milestone static receipts:

```text
ruff check: exit 0, full src/nested_memvid_agent tree and authorized tests
mypy: exit 0, 20 changed production source files, no issues
git diff --check: exit 0
```

The repository-wide feature-milestone command used the handoff PATH and only the six
exact documented host-sensitive deselections:

```text
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest -o addopts='' -q \
  --deselect tests/test_install_script.py::test_failed_server_health_stops_tracked_supervisor_and_child \
  --deselect tests/test_install_script.py::test_failed_child_pid_publication_still_cleans_via_supervisor_identity \
  --deselect tests/test_install_script.py::test_failed_health_kills_term_ignoring_server_and_grandchild_group \
  --deselect tests/test_service_control.py::test_system_process_inspector_and_signaler_touch_only_test_owned_process \
  --deselect tests/test_tools.py::test_same_public_call_id_across_runs_keeps_process_tracking_isolated \
  --deselect tests/test_tools.py::test_subprocess_tool_timeout_kills_child_process_and_caps_requested_timeout
exit 0: 3816 passed, 81 skipped, 6 deselected, 1 warning in 424.87s (0:07:04)
```

The one warning is the same known pre-existing Starlette/httpx deprecation. A
first otherwise-identical quiet-double run also exited 0, but repository addopts
suppressed its numerical summary; the `-o addopts=''` repeat above exposed the
authoritative exact counts without changing source.

## Review findings closed

Central implementation review found and closed the following issues before the
final gates:

- Replaced dynamically assembled factory kwargs with an explicit typed provider
  construction call so MyPy validates resolver/authority threading.
- Closed the no-tools executor bypass: even a malicious/nonconforming provider
  response cannot reach tool registry lookup or execution. Secret classification
  occurs on raw provider text before sanitized failure reporting.
- Removed overcoupling between profile-latest evidence and a target's governing
  observation. Only their exact shared binding tuple must agree.
- Added the missing reviewed runtime-interface digest revalidation for OS identity,
  source, ifindex, interface/network/endpoint, material, and review state.
- Cleared authority on runtime settings apply/restore and on every non-LAN,
  ordinary, failure, reuse, delegation, nonactionable, shadow, and fallback path.
- Corrected the context compiler's universal 16k invention for low-risk final
  simple general work while preserving explicit and safety-related floors.
- Kept secret detection ahead of response sanitization so the no-tools policy
  path cannot mask a secret-bearing response as a harmless format error.
- Aligned managed-LAN expiration at exact equality from `now > fresh_until` to
  `now >= fresh_until`.

A transport/authority-specific audit also exercised 183 runtime-transport tests,
34 resolver regressions, and 37 provider tests and reported no P0/P1 issue. After
the central fixes and equality delta, two independent final auditors both returned
**APPROVE** with no open P0, P1, or P2 finding.

Those pre-commit approvals did not replace the mandatory fresh committed-range
review. The exact review of
`424378a2d95a32458a61ca077d5a4f5d9e129f2e..dbbc32b1573a743a694f05a6b2de493c1b7d2de1`
returned **REQUEST CHANGES** for one P1 authority-family defect: an incomplete
catalog observed after an installed hardening marker changed from `v1` to absent
could downgrade only the mentioned target while leaving an omitted enabled
sibling executable, or could discover the inconsistency only after writes.

The original implementer froze five marker-downgrade regressions. RED was four
failures plus one same-marker control pass. The correction fans every enabled
sibling into the exact affected revision set, preserves each omitted sibling's
remote evidence, clears its marker/review/trust/routing/interface authority,
invalidates the prior reviewed material, recomputes the unconfirmed material,
and validates every current/replacement family inside the transaction before the
commit hook.

The first scoped re-review then found a second P1 in that same correction round:
when the last enabled target expired during an outage while profile evidence was
still fresh, the target could become disabled without deriving and writing the
profile projection. Five outage-family regressions recorded RED as one failure
plus four controls passing. The correction now overlays planned target mutations
over the complete family, derives profile enablement/trust from that post-plan
family, CAS-bumps the profile only when its projection or metadata changes,
returns only affected targets, and uses the shared whole-family validator before
the caller's commit hook.

All ten regressions passed after the bounded correction. Two independent
post-fix audits returned **APPROVE** with high confidence and no P0, P1, or P2
finding. They independently passed all `251` discovery-service tests, Ruff,
MyPy, and `git diff --check`; the second audit rehashed unchanged final bytes as:

```text
src/nested_memvid_agent/routing/ledger_registry.py: 2abb5bcdc222714fef1eb098d04282b74ea3346f38edfb34d8be26d4806e3844
tests/test_lan_discovery_service.py: 288dc71285a867a66cbff02b3703be42e4d311dc708c3ffac87937c1378028b6
```

## Review-corrected final verification

The unchanged corrected bytes passed the same exact ten-file and 15-file commands
recorded above with the ten new regressions included:

```text
ten-file Task 5 gate: 919 passed, 1 known warning
15-file Task 4/5/routing/provider superset: 1242 passed, 1 known warning
```

Final scoped static receipts were Ruff clean for both changed source/test files,
MyPy clean for `src/nested_memvid_agent/routing/ledger_registry.py`, and
`git diff --check` clean. The
repository-wide command recorded above was then repeated against the correction
with the same six exact deselections and exited 0:

```text
3826 passed, 81 skipped, 6 deselected, 1 warning in 383.99s (0:06:23)
```

The warning remained the known pre-existing Starlette/httpx deprecation.

## Deliberate policy decisions and residual boundaries

- Authority defaults off. Discovery, import, review, health, or a learned route
  never self-activates a LAN provider.
- A Task 5A-era row cannot be review-upgraded into runtime hardening. Only an
  exact new positive OpenAI-compatible import/re-import under the installed
  marker can become enableable. Ollama remains disabled pending a separate
  hardened native runtime.
- Five-minute evidence freshness and five-second future-skew allowance are fixed
  Task 5 policy constants. Changing them is a policy/digest/race review, not a UI
  preference.
- Two earlier Task 4 append-seam residuals were controller-classified as Task 4
  concerns, not Task 5 defects: that seam accepts an observation timestamp more
  than five seconds in the future and accepts IPv4 network/broadcast endpoint
  literals. Task 5's strict reconstruction rejects both, so neither can become a
  Task 5 durable or executable authority. They remain documented for a separate
  Task 4 decision.
- Plain private-LAN HTTP is the only qualified wire shape. There is no TLS or
  certificate/provider-identity proof, credentialed access, pricing/usage proof,
  tools, JSON mode, streaming, vision, reasoning inference, or generic provider
  fallback.
- `LLMOptions` has no run cancellation token. Deterministic cancellation is proven
  at the direct transport seam; ordinary synchronous generation is bounded by the
  shared freshness-aware absolute deadline of at most 120 seconds, not qualified
  for immediate RunManager cancellation.
- Tests use deterministic mocked interface inventories, clocks, sockets, durable
  rows, and transport responses. This host did not provide a qualifying same-host
  eligible-interface fixture. There is no controlled two-machine/live-provider,
  installed/frozen-sidecar, signed-artifact, packaging, rollback, or production
  qualification.
- The import/review routes intentionally use one fixed authenticated local-owner
  label. They make no per-user attribution claim. Task 6 later owns bounded scan
  lifecycle/orchestration.
- Flock schema v4 must still add exact grant-to-target/material-binding linkage
  before later idempotent suspension transitions. Task 5 returns no grant IDs and
  does not depend on future transition storage to make stale targets ineligible.

## Handoff

The Task 5B bounded correction is review-clean in its uncommitted final bytes
under the focused, superset, static, and repository-wide gates recorded above.
Before finalizing the milestone, the controller must stage only
`src/nested_memvid_agent/routing/ledger_registry.py`,
`tests/test_lan_discovery_service.py`, and this already tracked report; commit;
record the resulting exact identity in `progress.md`;
and run a fresh exact review of `dbbc32b..correction`. This report is not an
activation, release, or production qualification record.
