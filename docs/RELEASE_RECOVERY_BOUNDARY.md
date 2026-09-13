# Release recovery authority and actuation boundary

Kestrel release recovery uses two deliberately separate execution planes. A recovery capsule
interprets and verifies authority without network access. The dispatch-pinned GitHub Actions
workflow performs acquisition and mutation only after the capsule has bound that host plane to
the exact source and tool identities it verified.

This split is part of the v0.6 release-control contract. A recovery capsule is not a general
online interpreter, and activating one does not replace checkout files or grant authority to
ambient code.

## Current implementation boundary

This document describes the intended production boundary and the locally implemented
protocol/staging/controller slices. The S2 exact-SHA candidate/promotion transaction is
`qualified` in the source of truth (PR #344 merged as `a29b2e02` on 2026-08-18, reflected by PR
#345). That qualification covers the mechanism and its green hosted gates; it does **not** claim
that S2 is production-operable. `scripts/recovery_capsule_controller.py` is an explicit Ubuntu x86_64 owner-side
controller. It observes and downloads the authorization and dependency artifacts by exact server
artifact ID, binds both server-computed digests, reconstructs the installed environment at the
exact later runner path, creates and publishes the immutable recovery capsule, independently
recaptures and verifies its Release, signs the verification, and publishes the distinct exact
three-asset `release-prepare-authority-{run_id}-1` handoff.

That local implementation is not a hosted receipt. It has not been run against the private
recovery repository, and it does not replace the later per-role `release-preparation-authority`,
`release-commit-authority`, verification, PyPI, and final authority publications. The private
recovery repository, owner signing material, protected environments, scoped credentials, later
role authorities, and exact-SHA hosted qualification are separate external owner gates. Until
those gates produce append-only receipts, the implementation grants no release authority.

The S2 `qualified` status recorded in `docs/V0_6_PROOF_RELEASE_SOURCE_OF_TRUTH.md` (by PR #345,
owner decision 2026-08-18) reflects the merged mechanism plus green hosted gates only. Per the
owner directive, owner-controlled gate receipts are deferred to S12 final qualification; this
boundary is unchanged by the S2 status, and production release authority still requires the
append-only receipts named above.

## Owner-side controller invocation

The controller must run on Ubuntu 24.04 x86_64 under CPython 3.11.14 from the exact clean source
commit with the frozen project dependencies installed. The pinned Gitleaks container image also
requires a working Docker daemon. `--target-workspace-root` must be a separate, empty, real
directory mounted at the exact absolute path that the later Actions job will expose as
`GITHUB_WORKSPACE`; generated virtual-environment scripts and the installed tree are path-bound.
The controller requires the checksum-pinned GitHub CLI named by `--pinned-gh`, an owner mutation
credential in `GH_TOKEN`, the independently scoped reader in
`RELEASE_RECOVERY_READER_TOKEN`, and the owner's private signing identity named by
`--identity-file`. The four `--current-recovery-*` paths are freshly captured, signed-source
envelopes for the bounded owner acknowledgement, sole-writer repository state, immutable Release
policy, and controller context. Credentials are runtime inputs and are never capsule assets.

After recording the exact server IDs and `sha256:` API digests from the authorization and staging
workflow artifacts, the owner-side command is:

```sh
BOOTSTRAP_ROOT=/absolute/new/recovery-controller-bootstrap
BOOTSTRAP_PYTHON="$(bash scripts/bootstrap_recovery_tcb.sh "$BOOTSTRAP_ROOT")"

env -i \
  GH_TOKEN="$GH_TOKEN" \
  RELEASE_RECOVERY_READER_TOKEN="$RELEASE_RECOVERY_READER_TOKEN" \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 LC_ALL=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  "$BOOTSTRAP_PYTHON" -I -S -B scripts/bootstrap_recovery_capsule_controller.py \
  --bootstrap-root "$BOOTSTRAP_ROOT" \
  --pinned-gh /absolute/checksum-pinned/gh \
  --source-root /absolute/clean/kestrel \
  --source-sha "$SOURCE_SHA" \
  --staging-run-id "$STAGING_RUN_ID" \
  --staging-artifact-id "$STAGING_ARTIFACT_ID" \
  --staging-artifact-digest "$STAGING_ARTIFACT_DIGEST" \
  --candidate-manifest-digest "$CANDIDATE_MANIFEST_DIGEST" \
  --promotion-run-id "$PROMOTION_RUN_ID" \
  --authorization-artifact-id "$AUTHORIZATION_ARTIFACT_ID" \
  --authorization-artifact-digest "$AUTHORIZATION_ARTIFACT_DIGEST" \
  --target-workspace-root /exact/later/GITHUB_WORKSPACE \
  --recovery-repository-id "$RECOVERY_REPOSITORY_ID" \
  --identity-file /absolute/private/owner-signing-key \
  --current-recovery-owner-authority-snapshot /absolute/fresh/recovery-owner.json \
  --current-recovery-repository-observation /absolute/fresh/recovery-repository.json \
  --current-recovery-immutable-releases-observation /absolute/fresh/immutable-releases.json \
  --current-recovery-controller-context /absolute/fresh/controller-context.json \
  --work-root /absolute/new/controller-work \
  --output /absolute/new/recovery-controller-receipt.json \
  --prepare-only
```

The first invocation is deliberately non-authoritative. It acquires and validates the two exact
artifacts, creates the deterministic candidate archive, and completes the slow path-bound offline
environment probe. It performs no owner/reader authority capture and no remote mutation. After it
returns, replace the bytes at the same four `--current-recovery-*` slot paths with one newly
captured, internally consistent generation, then rerun the exact command without
`--prepare-only`. The bootstrap root, request paths, server IDs, digests, and all other arguments
remain identical. The outer bootstrap and slow preparation replay exact bytes without transport or
reinstallation.

Each remote mutation rechecks the private signing identity and clean source, obtains a new owner
key observation and independently scoped reader runtime proof, atomically snapshots all four
authority slots, and validates the full sole-writer policy. It then signs an exact stage grant
bound to the request journal, source/candidate/run/repository/transaction identities, release
name/tag/body digest, complete asset inventory, allowed operations, reader scope/token, signing
identity, and authority generation. A grant expires no later than its five-minute source authority
and cannot cross from capsule publication to prepare-authority publication. If the window expires,
replace all four slot bytes with a new generation; completed failed generations remain as evidence.
The immutable capsule retains only its first issuance generation as historical evidence. Later
renewals are sibling controller journals and cannot mutate capsule policy or add capsule authority.

The stdlib-only outer controller validates the exact clean checkout, Actions Python bootstrap tree,
checksum-pinned GitHub CLI, server artifact identity, hash-locked wheels, and installed
`site-packages` tree before it makes project or third-party imports. If it is interrupted after an
atomic artifact acquisition or offline environment install, the next invocation replays the saved
server evidence, archive extraction, dependency receipt, environment build receipt, interpreter,
and installed tree without transport or reinstall; a byte or inventory conflict stops. The
bootstrap receipt itself is fsynced in hidden scratch and atomically installed without replacement;
matching abandoned scratch and a legacy noncanonical partial regular receipt are recoverable, while
a canonical conflict or symlink fails closed. The
authenticated bootstrap archive and pinned content/link inventory are reusable only when the
runtime still has the separately enforced read-only modes. The inner command is crash-safe at
immutable publication boundaries: an exact request journal and exact pre-existing bytes resume;
critical incomplete unpublished capsule, authority-binding, normalized-evidence, prepare-asset,
receipt-temp, and target-runtime scratch is removed only inside its request-bound paths. Other
uniquely named inert staging remnants confer no authority and are ignored. An authority binding
and its atomically staged normalized evidence remain renewable scratch until the complete
capsule directory, closure, authority pair, and directory-identity marker are joined; an interruption
before that marker may renew an expired five-minute generation without rewriting history. Empty or
exact-runtime-only target transaction scratch is recoverable, and every other target entry fails.
A changed journal, Release identity, asset inventory, server ID, server digest, downloaded archive,
signed authority, generation provenance, owner key, or independent-reader result fails closed. Unsigned
verification-source scratch is renewable; once a signed verification exists, its source evidence
is historical and replays at capture time. Every remote mutation reruns the current authority,
reader, source, and exact-stage-grant guard, then reobserves the exact release ID, draft state, and
asset inventory before acting. Running the command without `--prepare-only` performs external
publication and therefore still requires explicit owner authority.

## Bootstrap trust boundary

Recovery does not claim to be offline merely because the capsule archive has been downloaded.
Before capsule authority can run, the dispatch-pinned workflow executes a deliberately narrow
host bootstrap TCB:

- the exact workflow and inline shell at the admitted source commit;
- Bash, `curl`, `tar`, and the host core utilities used to fetch, inventory, and verify bytes;
- the host ELF loader and libraries used for the first authenticated Python invocation; and
- the exact upstream Actions Python 3.11.14 archive named in
  `scripts/bootstrap_recovery_tcb.sh`.

The upstream archive has a frozen byte size and SHA-256. The bootstrap extracts it without owner
preservation, inventories the complete `bin/python3.11` plus `lib` tree, verifies a frozen tree
digest and Python executable digest, makes the verified tree read-only, and only then starts
Python. `/etc/ld.so.preload` is rejected. Python starts with `env -i`, isolated/safe-path flags,
and no workflow credentials.

That first Python process remains inside the named host TCB. It authenticates the capsule archive
digest, manifest, signature-bound owner identity, closure, and every dependency asset; extracts
the capsule-owned Python runtime; and switches bootstrap subprocesses to the capsule's verified
private ELF loader and library path. Kestrel makes no offline-authority claim until full capsule
verification has succeeded, the private-loader dependency preflight admits only capsule-bound
runtime paths, and the requested command enters the network-unshared sandbox. Transport TLS and
GitHub
availability can prevent bootstrap, but they cannot substitute different bytes without a digest
failure.

## Plane 1: offline authority

The independently published immutable recovery capsule owns authority interpretation. After the
bootstrap boundary above has closed, the launcher re-verifies the complete extracted Python
stdlib/runtime tree, interpreter, dependency and wheel locks, private-loader dependency
resolution, capsule assets, and static execution closure. It scrubs the inherited environment and
executes every authority command in the OS sandbox with the network namespace unshared.

The capsule-construction protocol requires its controller to build a reference virtual
environment at the exact target path before capsule creation, with bytecode generation disabled,
and bind a schema-validated `site-packages` tree identity into the immutable execution closure.
The production smoke and owner-side controller share this one closure builder. Hosted execution of
the controller against real authorization evidence remains a separate qualification gate.
Bootstrap freshly creates that same virtual environment
from the hash-locked capsule wheelhouse, uses `--no-index`, runs `pip check`, requires an exact
match to the controller-bound tree, and freezes the installed files read-only. Every later
launcher invocation re-hashes the complete bound `site-packages` tree before authority code can
run. Workflow and nested launcher wrappers enter on `-I -S -B` stdlib paths only; the launcher
removes any capsule/environment paths before its own stdlib imports, verifies the manifest and
tree with its pre-import gate, and only then authorizes capsule and `site-packages` imports. A
wheel lock by itself is not treated as installed-environment verification.

Inside that sandbox, the offline plane may:

- verify signed admission, recovery-repository authority, and transaction authorization;
- verify the immutable capsule publication and exact candidate identity;
- materialize the capsule-bound candidate archive;
- compute a `kestrel.recovery_host_actuator_binding.v1` receipt.

Candidate materialization and binding-receipt creation are mutations, so the production workflow
invokes both through the same nested launcher/bubblewrap boundary. The binding output uses
exclusive creation at a closure-authorized path; it is not produced by shell redirection.

The offline plane may not acquire remote state, publish artifacts, create tags or releases,
upload packages, or expand its endpoint policy. Its closure declares no usable network path for
execution.

## Plane 2: dispatch-pinned host actuation

GitHub Actions remains the network and mutation plane. Before that plane can continue a recovered
transaction, `scripts/recovery_launcher.py bind-host-actuator` verifies all of the following:

- every recovery source file in the checkout is byte-identical to the capsule copy;
- every recovery schema in the checkout is byte-identical to the capsule copy;
- both release workflow files are byte-identical to their capsule-bound versions;
- the host Python executable has the same independently frozen identity as recovery Python;
- the host GitHub CLI has Kestrel's platform-specific pinned digest;
- the host root is an explicitly declared read root in the recovery closure;
- the candidate source SHA comes from the authenticated capsule manifest.

The command emits a canonical, digest-bearing host-actuator binding. The workflow fails before
host actuation if the receipt cannot be produced or validated. Each transaction role preserves
its own binding receipt in role-specific evidence.

The workflow never symlinks `scripts/` to capsule content and never exports capsule Python as the
general transaction interpreter. Inline workflow code is covered by the capsule-bound workflow
digest, while imported host modules are covered by the host source-bundle digest. Host Python and
GitHub CLI executables are copied into a declared read root before binding, so the sandbox cannot
follow a mutable ambient executable path. Network access and credentials remain confined to the
workflow-scoped host plane and are not inherited by capsule commands.

Final reconciliation constructs its host interpreter from the capsule's wheelhouse with
`--no-index`, `--require-hashes`, and binary-only installation. This preserves recovery from a
package-index outage without turning the capsule interpreter into an online process.

## Reproducible recovery dependencies

`.github/workflows/recovery-dependency-staging.yml` is the reusable, production-intended staging
path for the offline dependency closure and is consumed by the outer production controller by
exact workflow run ID, server artifact ID, and server-computed digest. The workflow accepts exactly
one 40-character source commit and checks out that commit without persisted credentials.
`scripts/stage_recovery_dependencies.py` then:

1. downloads the exact Ubuntu Noble bubblewrap package URL and the exact Actions Python 3.11.14
   release asset URL;
2. verifies both pinned package SHA-256 values;
3. extracts and verifies the independently frozen bubblewrap binary and version;
4. derives a link-free, deterministic Python `bin` plus `lib` archive, recording its executable,
   archive, complete-tree, file-count, and total-size identities;
5. reads `config/recovery-requirements.txt`, whose complete dependency set is hash locked;
6. downloads only CPython 3.11 / cp311 / manylinux2014 x86_64 wheels with `--require-hashes`;
7. builds an offline probe environment and resolves the transitive ELF dependencies of the
   frozen Python runtime and installed native extensions;
8. copies those exact dynamic-loader and runtime-library bytes into a sorted, digest-bound
   `kestrel.recovery_runtime.v1` manifest;
9. emits sorted Python-runtime, wheel, and ELF-runtime manifests plus a canonical staging receipt
   bound to the source SHA;
10. atomically publishes the staged directory only after every check passes.

Generate `config/recovery-requirements.txt` with:

```sh
uv export --frozen --only-group recovery --format requirements.txt --no-emit-project --no-header --no-annotate
```

The committed file must be byte-for-byte identical to that output. Changing the recovery
dependency group or lockfile requires regenerating and reviewing it. The staging workflow
bootstraps Kestrel's checksum-pinned `uv` and enforces this comparison before any dependency
acquisition.

The staging receipt records package origins and digests, requirements digest, Python version,
ABI, wheel platform, sandbox identity, Python source/archive identities,
Python-runtime-manifest digest, wheel-manifest digest, wheel count, ELF-runtime-manifest digest,
and runtime-file count. Capsule creation requires `--dependency-root` and rejects a receipt or
dependency byte that differs from the candidate source, closure lock, or frozen production
identities.

Bubblewrap begins with an empty filesystem namespace. The capsule therefore does not expose an
ambient `/usr` or `/lib` tree. Bootstrap re-verifies each staged runtime byte and makes it
read-only; the launcher creates only the necessary parent directories and mounts each
digest-bound file at its recorded ELF loader path. The extracted Python base tree lives in the
capsule's sibling `recovery-runtime/base` directory and the freshly installed environment in
`recovery-runtime/environment`; neither is permitted inside the immutable capsule inventory.
The launcher re-hashes the complete base tree before every command. Runtime-image drift fails
closed instead of silently changing the authority interpreter.

The reusable workflow exposes the server artifact ID and server-computed artifact digest. The
owner-side controller requires both values, independently observes the run, exhaustive artifact
listing, and direct artifact endpoint, downloads only that artifact ID, and rejects any metadata,
size, digest, workflow, attempt, repository, branch, or source-SHA substitution before extraction.
Artifact retention is transport availability, not authority: the staged receipt and every
dependency byte become immutable capsule assets before recovery publication. If the transport
artifact expires, the controller must restage the same source commit and create a new
pre-publication capsule; it must not substitute ambient files.

## Production smoke requirement

A successful staging run uploads its artifact only after a Linux x86_64 smoke has used the real
staged assets to:

- install controller dependencies with `--no-index`;
- create a full recovery capsule through the production capsule command;
- run the pinned secret scan;
- create a deterministic archive;
- extract it with the isolated bootstrap;
- build the hash-locked offline environment;
- verify the complete extracted Python runtime tree and run it through the private loader;
- verify and freeze the exact dynamic-loader/runtime file closure;
- fully verify the capsule and execution closure;
- execute a nested full verification through the real network-unshared bubblewrap profile;
- materialize the candidate through that same nested profile;
- bind copied, pinned host Python and GitHub CLI executables as the separate actuation plane; and
- prove a scrubbed outer secret is absent inside the capsule command.

The schema-validated, self-digesting `kestrel.recovery_capsule_smoke.v1` report is uploaded beside
`recovery/`. A failed smoke prevents the staged artifact from being published.

## Fail-closed invariants

- Dependency staging grants no release or mutation authority.
- Capsule qualification grants no host authority without a binding receipt.
- A source, schema, workflow, Python, GitHub CLI, receipt, or dependency mismatch stops recovery.
- Capsule execution remains network denied; the host plane never imports capsule code directly.
- `/etc/ld.so.preload`, an unexpected inherited environment key, an ambient loader dependency, or
  a changed Python stdlib/runtime or installed `site-packages` byte stops recovery.
- Offline verification, candidate materialization, and host binding all use the same nested,
  network-unshared execution boundary.
- Role evidence is append-only across artifact handoffs; a later role creates a new binding rather
  than overwriting an earlier receipt.
- Static deterministic release behavior remains the fallback; recovery does not broaden the
  release plan or synthesize missing owner authority.
