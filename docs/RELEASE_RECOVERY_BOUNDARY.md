# Release recovery authority and actuation boundary

Kestrel release recovery uses two deliberately separate execution planes. A recovery capsule
interprets and verifies authority without network access. The dispatch-pinned GitHub Actions
workflow performs acquisition and mutation only after the capsule has bound that host plane to
the exact source and tool identities it verified.

This split is part of the v0.6 release-control contract. A recovery capsule is not a general
online interpreter, and activating one does not replace checkout files or grant authority to
ambient code.

## Current implementation boundary

This document describes the intended production boundary and the protocol/staging slice that
implements its fail-closed primitives. It does **not** claim that S2 is production-operable or
qualified. No production controller in this repository currently downloads the staged dependency
artifact by its server artifact ID, constructs the path-specific execution closure from real
authorization evidence, creates and publishes the corresponding recovery capsule, or supplies the
later role-specific authority releases. Those are dependent controller-integration and hosted
qualification gates. The private recovery repository, owner signing material, protected
environments, and scoped credentials are separate external owner gates.

Until that controller slice and its exact-SHA hosted receipts exist, a successful reusable staging
run and capsule smoke can qualify only these implementation primitives. They grant no release
authority, do not make the release transaction runnable, and do not change the `not_started` S2
status in `docs/V0_6_PROOF_RELEASE_SOURCE_OF_TRUTH.md`.

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

The bootstrap freshly creates the virtual environment from the hash-locked capsule wheelhouse,
uses `--no-index`, and runs `pip check`. This slice does not yet record and re-hash the resulting
installed `site-packages` tree before every later launcher invocation. Closing that integrity gap
is a required dependent controller/runtime-hardening gate before S2 qualification; the wheel lock
alone must not be described as later byte-for-byte installed-environment verification.

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
path for the offline dependency closure. It is not yet consumed by the production controller
described in the current implementation boundary above. The workflow accepts one exact
40-character source commit and checks out that commit without persisted credentials.
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
dependent production controller must record both values and download that exact artifact by ID;
that integration is not implemented in this slice. Artifact retention is transport availability,
not authority: the staged receipt and every dependency byte must become immutable capsule assets
before recovery publication. If the transport artifact expires, the controller must restage the
same source commit and create a new pre-publication capsule; it must not substitute ambient files.

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
  a changed Python stdlib/runtime byte stops recovery.
- Offline verification, candidate materialization, and host binding all use the same nested,
  network-unshared execution boundary.
- Role evidence is append-only across artifact handoffs; a later role creates a new binding rather
  than overwriting an earlier receipt.
- Static deterministic release behavior remains the fallback; recovery does not broaden the
  release plan or synthesize missing owner authority.
