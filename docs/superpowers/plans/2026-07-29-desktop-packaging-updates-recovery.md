# Cross-Platform Desktop Packaging, Updates, and Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce self-contained, signed Kestrel desktop installers for macOS, Windows, and Linux—with the complete Kestrel-owned core, safe updates, byte-and-state rollback, uninstall that preserves owner data, and clean-machine qualification.

**Architecture:** Build the Python sidecar natively with PyInstaller on each target platform/architecture, stage it with the exact built React renderer and Electron shell, generate a signed resource manifest plus SBOM, and package native artifacts with electron-builder. Release artifacts are built from one exact tagged commit on native runners and pass source, sidecar, renderer, supply-chain, signing, installed-runtime, update, rollback, and 20-cycle lifecycle gates. Updates use a Kestrel-signed manifest in front of platform updater metadata. Before installation, the sidecar proves the runtime is idle and creates a consistent control-plane snapshot. A separately launched, digest-verified recovery helper retains the previous signed application payload and restores both compatible bytes and the matching SQLite/key snapshot if first-launch health never accepts the new version. Memvid v2 files are preserved and never casually migrated by the updater.

**Tech Stack:** Electron 43.2.0, electron-builder 26.15.3, electron-updater 6.8.9, `@electron/fuses` 2.1.3, PyInstaller 6.21.0, Python 3.11, CycloneDX, Ed25519 signatures through `cryptography`, macOS codesign/notarization, Windows Authenticode/NSIS per-user installers, Linux AppImage plus detached signature, `.deb`/`.rpm` secondary formats, GitHub Actions native runners, and existing Kestrel release provenance/attestation tooling.

## Global Constraints

- Start only after the desktop foundation, Wildflower Workbench, LAN discovery, and Adaptive Flock qualification plans are integrated and green.
- Build every artifact from the exact tagged source commit. Never package a dirty tree, uncommitted generated asset, or sidecar from a different SHA.
- PyInstaller is not a cross-compiler: build Windows on Windows, macOS on macOS, and Linux on the supported Linux baseline.
- Canonical artifacts:
  - macOS: signed/notarized DMG plus updater ZIP for arm64 and x64;
  - Windows: signed per-user NSIS installer for arm64 and x64;
  - Linux: AppImage plus detached signature for arm64 and x64;
  - Linux `.deb` and `.rpm` only after the same payload’s AppImage path is qualified.
- The installed app requires no Python, Node.js, shell, compiler, package manager, Docker, VM engine, or model server for core offline Demo operation.
- Bundle Electron, renderer, frozen Python sidecar, FastAPI/Uvicorn, Memvid v2 integration, all six layer configuration, SQLite migrations, timezone data, deterministic Demo provider, keyring integration, LAN discovery runtime, licenses, and recovery helper.
- Do not bundle a production local model, Docker, a VM manager, or another optional containment engine.
- Installed bytes are immutable and separate from owner data. Upgrades/reinstalls preserve memory, state, projects, settings, credentials, and receipts.
- Existing `.mv2` files are reopened. Never call `create(path)` on an existing `.mv2`; never replace Memvid with a JSON/SQLite recovery copy.
- Update checking is opt-in. Download may be background; install is explicit and only when runtime/approval/migration state is idle.
- Verify update signature, checksum, platform, architecture, version transition, resource manifest, schema compatibility, disk space, and state health before install.
- Application rollback and control-plane rollback are one receipt-bound operation. Never restore SQLite without the matching application version/key manifest.
- Hold the new version in preflight before it can run missions or approvals. Mark accepted only after sidecar, schema, Memvid reopen, Demo, and renderer readiness pass.
- High-risk calls and ambiguous provider requests are never replayed during update or rollback.
- Uninstall removes application bytes and managed launcher integration only. Owner-data deletion is a separate explicitly named destructive action and is not part of the uninstaller.
- Release signing/notarization credentials remain CI secrets and are never placed in source, artifacts, logs, renderer state, support bundles, or agent context.
- A developer bundle is not a release artifact. An unsigned/signature-skipped artifact cannot enter publication jobs.
- Run full `pytest -q`, renderer tests/build, desktop tests/build, and platform artifact verification after every phase.
- Run Memvid integration behind `RUN_MEMVID_INTEGRATION=1`; live provider/containment checks retain their explicit gates.

## Primary References

- Electron’s security checklist requires context isolation, process sandboxing, restrictive CSP, navigation/window limits, sender validation, and avoiding `file://`: <https://www.electronjs.org/docs/latest/tutorial/security>
- electron-builder supports the selected DMG/ZIP, NSIS, AppImage, `.deb`, and `.rpm` targets: <https://www.electron.build/docs/>
- electron-updater supports macOS, Windows NSIS, and Linux AppImage update paths, with macOS signing required: <https://www.electron.build/docs/features/auto-update/>
- PyInstaller explicitly requires native per-platform builds: <https://pyinstaller.org/en/stable/index.html>

---

## Phase 1: Freeze Release Toolchains and Artifact Identity

### Task 1: Pin packaging toolchains and define the desktop release matrix

**Files:**

- Create: `config/desktop-build-bootstrap.txt`
- Create: `config/desktop-release-matrix.json`
- Create: `scripts/check_desktop_release_metadata.py`
- Create: `tests/test_desktop_release_metadata.py`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Modify: `scripts/check_project_metadata.py`
- Modify: `tests/test_packaging_deployment.py`

**Interfaces:**

- Produce one canonical matrix entry per platform/architecture/format.
- All Python build tools are exact and hash-locked.
- All npm build tools are exact and lockfile-bound.
- Version/source identity must match Python package, web package, desktop package, update manifest, sidecar, Electron app, and tag.

- [ ] **Step 1: Write failing release-matrix tests**

```python
def test_desktop_release_matrix_is_complete() -> None:
    matrix = load_release_matrix()
    assert {(item.platform, item.arch, item.primary_format) for item in matrix} == {
        ("darwin", "arm64", "dmg"),
        ("darwin", "x64", "dmg"),
        ("win32", "arm64", "nsis"),
        ("win32", "x64", "nsis"),
        ("linux", "arm64", "appimage"),
        ("linux", "x64", "appimage"),
    }
    assert all(item.python == "3.11" for item in matrix)


def test_desktop_build_dependencies_are_exact() -> None:
    package = load_json(ROOT / "desktop/package.json")
    assert package["devDependencies"]["electron"] == "43.2.0"
    assert package["devDependencies"]["electron-builder"] == "26.15.3"
    assert package["dependencies"]["electron-updater"] == "6.8.9"
    assert locked_python_version("pyinstaller") == "6.21.0"
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_release_metadata.py \
  tests/test_packaging_deployment.py
```

Expected: matrix/bootstrap/checker absent or packaging pins incomplete.

- [ ] **Step 3: Add exact release metadata**

The matrix includes:

- OS/architecture and native runner class;
- Python/Node/Electron/PyInstaller versions;
- primary and secondary targets;
- sidecar executable name;
- signing mode;
- updater metadata name;
- installed app identity/bundle ID;
- default per-user install location;
- minimum supported OS;
- state/routing schema range;
- artifact filename pattern.

Use `com.kestrel.agent` as the stable app/bundle identity and `Kestrel` as the display/product name. Update metadata checker rejects any version/SHA drift.

- [ ] **Step 4: Run lock/metadata/full suites**

Run:

```bash
uv lock --check
npm --prefix desktop ci
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_release_metadata.py \
  tests/test_packaging_deployment.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add config/desktop-build-bootstrap.txt \
  config/desktop-release-matrix.json \
  scripts/check_desktop_release_metadata.py \
  tests/test_desktop_release_metadata.py \
  pyproject.toml uv.lock \
  desktop/package.json desktop/package-lock.json \
  scripts/check_project_metadata.py \
  tests/test_packaging_deployment.py
git commit -m "build: pin desktop release toolchains"
```

### Task 2: Make the frozen sidecar complete and reproducible on every target

**Files:**

- Modify: `packaging/kestrel-sidecar.spec`
- Create: `packaging/kestrel-recovery-helper.spec`
- Create: `src/nested_memvid_agent/desktop_recovery_helper.py`
- Create: `tests/test_desktop_recovery_helper.py`
- Modify: `scripts/build_desktop_sidecar.py`
- Create: `scripts/inspect_frozen_sidecar.py`
- Create: `tests/test_frozen_sidecar_contract.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**

- Produce `kestrel-sidecar[.exe]` and `kestrel-recovery[.exe]` for current target.
- Sidecar starts with one bootstrap-path argument.
- Recovery helper starts with one authenticated recovery-instruction path.
- Produce a frozen-module inventory and deterministic staged-file manifest.

- [ ] **Step 1: Write failing collected-module and runtime tests**

```python
def test_frozen_sidecar_contract_contains_required_runtime_roots() -> None:
    inventory = inspect_spec(ROOT / "packaging/kestrel-sidecar.spec")
    for root in (
        "nested_memvid_agent.desktop_sidecar",
        "nested_memvid_agent.server",
        "nested_memvid_agent.routing",
        "memvid_sdk",
        "uvicorn",
        "fastapi",
        "keyring",
        "zeroconf",
        "psutil",
    ):
        assert inventory.contains(root)
    assert not inventory.contains("qrcode")


def test_recovery_helper_has_no_server_or_provider_runtime() -> None:
    inventory = inspect_spec(ROOT / "packaging/kestrel-recovery-helper.spec")
    assert inventory.contains("nested_memvid_agent.desktop_recovery_helper")
    assert not inventory.contains("nested_memvid_agent.server")
    assert not inventory.contains("nested_memvid_agent.llm")
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_frozen_sidecar_contract.py \
  tests/test_desktop_recovery_helper.py
```

Expected: recovery helper/spec absent.

- [ ] **Step 3: Complete the native freezer specs**

Sidecar includes:

- Kestrel Python package and route modules;
- FastAPI/Uvicorn and Pydantic;
- Memvid v2 SDK/native libraries;
- SQLite/Python runtime;
- keyring backends appropriate to the platform;
- LAN discovery dependencies;
- timezone data;
- bundled renderer/license resources needed by compatibility server mode;
- provider adapters included in the full core;
- deterministic Demo assets.

Exclude:

- tests, benchmarks, source maps unless explicitly retained for support;
- `.git`, `.env*`, `.nest`, credentials, local caches;
- QR/video-frame Memvid v1 packages;
- compilers/package managers;
- development servers.

Configure PyInstaller reproducibly: clear environment allowlist, fixed source root, no UPX, clean build, exact bootloader, deterministic archive order where supported, and platform-native dependency inspection.

- [ ] **Step 4: Build and inspect current platform**

Run:

```bash
uv run python scripts/build_desktop_sidecar.py \
  --mode release-candidate \
  --source-commit "$(git rev-parse HEAD)"
uv run python scripts/inspect_frozen_sidecar.py \
  --sidecar build/desktop-sidecar/current/kestrel-sidecar \
  --recovery-helper build/desktop-sidecar/current/kestrel-recovery
```

Then run the binaries against a temporary private profile and verify offline Demo, six Memvid v2 files, settings reopen, routing schemas, and clean shutdown.

- [ ] **Step 5: Run source/full suite**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_frozen_sidecar_contract.py \
  tests/test_desktop_recovery_helper.py \
  tests/test_desktop_sidecar.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add packaging/kestrel-sidecar.spec \
  packaging/kestrel-recovery-helper.spec \
  src/nested_memvid_agent/desktop_recovery_helper.py \
  scripts/build_desktop_sidecar.py \
  scripts/inspect_frozen_sidecar.py \
  tests/test_desktop_recovery_helper.py \
  tests/test_frozen_sidecar_contract.py \
  .github/workflows/ci.yml
git commit -m "build: freeze complete Kestrel desktop core"
```

### Task 3: Generate signed resource manifests and combined SBOMs

**Files:**

- Modify: `scripts/generate_desktop_resource_manifest.py`
- Modify: `scripts/verify_desktop_resource_manifest.py`
- Create: `scripts/generate_desktop_sbom.py`
- Create: `scripts/sign_desktop_manifest.py`
- Create: `scripts/verify_desktop_manifest_signature.py`
- Create: `tests/test_desktop_resource_manifest.py`
- Create: `tests/test_desktop_sbom.py`
- Create: `config/desktop-release-public-key.pem`
- Modify: `desktop/src/main/resource-manifest.ts`
- Modify: `desktop/src/main/resource-manifest.test.ts`

**Interfaces:**

- Resource manifest schema: `kestrel.desktop.resources.v1`.
- SBOM: CycloneDX JSON containing Python, npm/Electron, native sidecar, renderer assets, licenses, and Kestrel component identity.
- Signature: detached Ed25519 over canonical manifest bytes.
- Runtime embeds only release public key; private key is CI-only.

- [ ] **Step 1: Write failing coverage/tamper/key tests**

```python
def test_manifest_covers_every_installed_resource(tmp_path: Path) -> None:
    payload = staged_payload(tmp_path)
    manifest = generate_resource_manifest(payload, source_commit="a" * 40)
    assert set(manifest["files"]) == set(relative_files(payload))
    assert manifest["source_commit"] == "a" * 40
    assert manifest["sbom_digest"] == sha256_file(payload / "sbom.cdx.json")


def test_release_verifier_rejects_test_key_and_tampered_sbom(tmp_path: Path) -> None:
    candidate = signed_candidate(tmp_path, key="developer")
    with pytest.raises(ValueError, match="untrusted manifest signing key"):
        verify_release_candidate(candidate)
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_resource_manifest.py \
  tests/test_desktop_sbom.py
npm --prefix desktop test -- resource-manifest
```

Expected: signing/SBOM paths incomplete.

- [ ] **Step 3: Implement canonical signed identity**

Manifest includes:

- schema/version/source commit/tag;
- app/bundle identity;
- platform/architecture;
- state/routing schema ranges;
- Python/npm lock digests;
- sidecar/recovery helper/renderer/Electron digests;
- SBOM/license-notice digests;
- build runner/toolchain identity;
- every relative installed file size and SHA-256.

Developer/test signatures use a separate key ID accepted only in developer mode. Release verification has no bypass environment variable. Use established Ed25519 APIs from `cryptography`; do not implement custom signature math.

- [ ] **Step 4: Generate/verify current-platform candidate**

Run:

```bash
uv run python scripts/generate_desktop_sbom.py \
  --staged-root desktop/release/staged \
  --output desktop/release/staged/sbom.cdx.json
uv run python scripts/generate_desktop_resource_manifest.py \
  --staged-root desktop/release/staged \
  --source-commit "$(git rev-parse HEAD)" \
  --output desktop/release/staged/kestrel-resource-manifest.json
uv run python scripts/sign_desktop_manifest.py \
  --developer-key \
  desktop/release/staged/kestrel-resource-manifest.json
uv run python scripts/verify_desktop_resource_manifest.py \
  --allow-developer-key \
  desktop/release/staged
```

- [ ] **Step 5: Run suites**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_resource_manifest.py \
  tests/test_desktop_sbom.py
npm --prefix desktop test -- resource-manifest
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

- [ ] **Step 6: Commit**

```bash
git add scripts/generate_desktop_resource_manifest.py \
  scripts/verify_desktop_resource_manifest.py \
  scripts/generate_desktop_sbom.py \
  scripts/sign_desktop_manifest.py \
  scripts/verify_desktop_manifest_signature.py \
  tests/test_desktop_resource_manifest.py \
  tests/test_desktop_sbom.py \
  config/desktop-release-public-key.pem \
  desktop/src/main/resource-manifest.ts \
  desktop/src/main/resource-manifest.test.ts
git commit -m "build: sign complete desktop resource identity"
```

---

## Phase 2: Package Native Installers

### Task 4: Configure electron-builder for the canonical artifacts

**Files:**

- Create: `desktop/electron-builder.yml`
- Create: `desktop/build/entitlements.mac.plist`
- Create: `desktop/build/entitlements.mac.inherit.plist`
- Create: `desktop/build/linux/kestrel.desktop`
- Create: `desktop/build/icons/README.md`
- Create: `desktop/scripts/build-package.mjs`
- Create: `desktop/scripts/after-pack.mjs`
- Create: `desktop/scripts/after-sign.mjs`
- Create: `desktop/src/main/fuses.ts`
- Create: `desktop/src/main/fuses.test.ts`
- Modify: `desktop/package.json`
- Modify: `desktop/package-lock.json`
- Modify: `tests/test_desktop_release_metadata.py`

**Interfaces:**

- macOS targets: `dmg` and `zip`, one architecture per native job.
- Windows target: `nsis`, per-user, no elevation, one architecture per native job.
- Linux primary: `AppImage`; secondary `deb`/`rpm` from same staged payload.
- Electron fuses disable run-as-node, node options, node CLI inspect, and unsigned archive loading as supported.

- [ ] **Step 1: Write failing builder/fuse tests**

```python
def test_builder_targets_match_release_matrix() -> None:
    config = load_yaml(ROOT / "desktop/electron-builder.yml")
    assert config["mac"]["target"] == ["dmg", "zip"]
    assert config["win"]["target"] == ["nsis"]
    assert config["linux"]["target"] == ["AppImage", "deb", "rpm"]
    assert config["nsis"]["perMachine"] is False
    assert config["nsis"]["allowElevation"] is False
```

```ts
it("locks production Electron fuses", () => {
  expect(PRODUCTION_FUSES).toMatchObject({
    RunAsNode: false,
    EnableNodeOptionsEnvironmentVariable: false,
    EnableNodeCliInspectArguments: false,
    OnlyLoadAppFromAsar: true
  });
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_release_metadata.py
npm --prefix desktop test -- fuses
```

Expected: builder config/fuses absent.

- [ ] **Step 3: Implement exact packaging layout**

Stage:

```text
resources/
  app.asar
  kestrel/
    bin/kestrel-sidecar[.exe]
    bin/kestrel-recovery[.exe]
    web/...
    licenses/...
    sbom.cdx.json
    kestrel-resource-manifest.json
    kestrel-resource-manifest.sig
```

Set `asar: true`; unpack only native executables/libraries that require it. Disable arbitrary protocols/file associations in the first release. Install per user where supported. Add app category/desktop entry and preserve profile data outside app directories.

`after-pack` verifies staged manifest and file coverage before signing. `after-sign` verifies platform signature and records signature identity; it never mutates already signed resource bytes.

- [ ] **Step 4: Build current-platform unsigned/developer artifact**

Run:

```bash
npm --prefix desktop run package:dir
npm --prefix desktop run verify:fuses
uv run python scripts/verify_desktop_resource_manifest.py \
  --allow-developer-key \
  desktop/release/current-platform-unpacked/resources/kestrel
```

Expected: directory bundle passes resource/fuse verification.

- [ ] **Step 5: Run tests**

```bash
npm --prefix desktop run test:typecheck
npm --prefix desktop test
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_release_metadata.py
```

- [ ] **Step 6: Commit**

```bash
git add desktop/electron-builder.yml desktop/build desktop/scripts \
  desktop/src/main/fuses.ts desktop/src/main/fuses.test.ts \
  desktop/package.json desktop/package-lock.json \
  tests/test_desktop_release_metadata.py
git commit -m "build: configure native Kestrel installers"
```

### Task 5: Enforce platform signing, notarization, and Linux detached signatures

**Files:**

- Create: `scripts/verify_macos_desktop_signature.py`
- Create: `scripts/verify_windows_desktop_signature.ps1`
- Create: `scripts/sign_linux_desktop_artifact.py`
- Create: `scripts/verify_linux_desktop_signature.py`
- Create: `tests/test_desktop_artifact_signatures.py`
- Create: `docs/DESKTOP_SIGNING.md`
- Modify: `desktop/scripts/after-sign.mjs`
- Modify: `config/desktop-release-matrix.json`

**Interfaces:**

- macOS: Developer ID Application signature, hardened runtime, notarization ticket/staple, expected team/bundle IDs.
- Windows: Authenticode SHA-256 signature, expected publisher, timestamp, signed sidecar/helper/installer.
- Linux: Ed25519 detached signature over artifact SHA-256 plus signed resource/update manifests and GitHub provenance attestation.
- Invariant: release verification has no “skip signing” path.

- [ ] **Step 1: Write failing signature-policy tests**

```python
def test_release_matrix_requires_signature_for_every_primary_artifact() -> None:
    for target in load_release_matrix():
        assert target.signature_required is True
        assert target.expected_signer


def test_linux_signature_binds_filename_digest_version_and_architecture() -> None:
    receipt = sign_linux_fixture()
    tampered = replace(receipt, architecture="arm64")
    with pytest.raises(ValueError, match="signature"):
        verify_linux_signature(tampered)
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_artifact_signatures.py
```

Expected: scripts/policy absent.

- [ ] **Step 3: Implement verifiers and secret contract**

Document only secret names and CI trust boundaries, never values. Signing secrets enter only native signing jobs with least permissions. Verification output includes public signer identity, timestamp/notarization status, artifact digest, version, architecture, and result.

Validate inner sidecar/recovery binaries as well as outer app/installer. A platform-signed outer artifact with a mismatched signed resource manifest still fails.

- [ ] **Step 4: Run deterministic signature tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_artifact_signatures.py
```

Actual codesign/Authenticode/notarization runs only on signing CI runners; local tests use generated ephemeral Ed25519 fixtures and parsed command outputs.

- [ ] **Step 5: Commit**

```bash
git add scripts/verify_macos_desktop_signature.py \
  scripts/verify_windows_desktop_signature.ps1 \
  scripts/sign_linux_desktop_artifact.py \
  scripts/verify_linux_desktop_signature.py \
  tests/test_desktop_artifact_signatures.py \
  docs/DESKTOP_SIGNING.md \
  desktop/scripts/after-sign.mjs \
  config/desktop-release-matrix.json
git commit -m "build: require signed desktop artifacts"
```

### Task 6: Verify credential behavior in packaged environments

**Files:**

- Create: `src/nested_memvid_agent/desktop_credentials.py`
- Create: `tests/test_desktop_credentials.py`
- Modify: `src/nested_memvid_agent/secret_broker.py`
- Modify: `src/nested_memvid_agent/server_product_routes.py`
- Modify: `desktop/e2e/credentials.spec.ts`
- Modify: `docs/SECURITY.md`

**Interfaces:**

- macOS uses Keychain; Windows uses Credential Manager; Linux uses Secret Service when available.
- Linux without Secret Service offers session-only credentials in the first release.
- No JSON/raw-vault persistent fallback is silently chosen for Desktop cloud credentials.

- [ ] **Step 1: Write failing platform policy tests**

```python
@pytest.mark.parametrize(
    ("platform", "backend", "expected"),
    [
        ("darwin", "macOS Keychain", "persistent"),
        ("win32", "Windows Credential Locker", "persistent"),
        ("linux", "Secret Service", "persistent"),
        ("linux", None, "session_only"),
    ],
)
def test_desktop_credential_policy(platform: str, backend: str | None, expected: str) -> None:
    assert detect_desktop_credential_mode(platform, backend).mode == expected


def test_linux_missing_secret_service_never_uses_json_secret_vault() -> None:
    mode = detect_desktop_credential_mode("linux", None)
    assert mode.mode == "session_only"
    assert mode.persistent_backend is None
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_credentials.py \
  tests/test_secret_broker.py
```

Expected: Desktop policy module absent.

- [ ] **Step 3: Implement explicit backend selection**

Desktop bootstrap supplies `credential_mode=desktop`; the sidecar then requires a usable platform keyring for persistent storage. Session-only secrets live in process memory, are redaction-registered, disappear on shutdown, and are clearly labeled. Existing advanced CLI JSON broker compatibility remains opt-in and cannot be selected silently by Desktop.

- [ ] **Step 4: Run credential leak and E2E tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_credentials.py \
  tests/test_secret_broker.py \
  tests/test_server_secret_routes.py \
  tests/test_security_boundary.py
npm --prefix desktop run e2e -- credentials
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/desktop_credentials.py \
  src/nested_memvid_agent/secret_broker.py \
  src/nested_memvid_agent/server_product_routes.py \
  tests/test_desktop_credentials.py \
  desktop/e2e/credentials.spec.ts \
  docs/SECURITY.md
git commit -m "feat: enforce safe packaged credential backends"
```

---

## Phase 3: Build Signed Update Preflight and State Snapshots

### Task 7: Define and verify the Kestrel update manifest

**Files:**

- Create: `desktop/src/main/update-manifest.ts`
- Create: `desktop/src/main/update-manifest.test.ts`
- Create: `scripts/generate_desktop_update_manifest.py`
- Create: `scripts/verify_desktop_update_manifest.py`
- Create: `tests/test_desktop_update_manifest.py`
- Modify: `config/desktop-release-public-key.pem`

**Interfaces:**

- Manifest schema: `kestrel.desktop.update.v1`.
- Bind version, source commit, channel, platform, architecture, artifact names/digests/sizes/signatures, resource manifest digest, SBOM digest, state/routing schema compatibility, minimum current version, blocked transitions, and release-note URL.
- Signature: Ed25519 over canonical bytes.
- Invariant: electron-updater metadata is accepted only after this manifest verifies and points to the exact artifact.

- [ ] **Step 1: Write failing transition/tamper tests**

```ts
it("rejects unsafe skip and architecture drift", () => {
  expect(() =>
    verifyUpdateManifest(manifest({ current: "0.5.0", next: "0.8.0" }), context)
  ).toThrow("version_transition_not_allowed");
  expect(() =>
    verifyUpdateManifest(manifest({ architecture: "arm64" }), x64Context)
  ).toThrow("architecture_mismatch");
});
```

```python
def test_update_manifest_rejects_artifact_digest_mismatch() -> None:
    with pytest.raises(ValueError, match="artifact digest"):
        verify_update_manifest(tampered_artifact_manifest())
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix desktop test -- update-manifest
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_update_manifest.py
```

Expected: modules absent.

- [ ] **Step 3: Implement signed-manifest verification**

Allow only HTTPS production feeds and a test-only local feed in developer mode. Use exact URL parsing and allowlisted release origin. Reject expired manifest, downgrade, disallowed skip, unknown channel, schema incompatibility, unsigned artifact, or mismatch with electron-updater metadata.

- [ ] **Step 4: Run tests**

```bash
npm --prefix desktop test -- update-manifest
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_update_manifest.py
```

- [ ] **Step 5: Commit**

```bash
git add desktop/src/main/update-manifest.ts \
  desktop/src/main/update-manifest.test.ts \
  scripts/generate_desktop_update_manifest.py \
  scripts/verify_desktop_update_manifest.py \
  tests/test_desktop_update_manifest.py
git commit -m "feat: verify signed desktop update manifests"
```

### Task 8: Add authoritative update preflight and control-plane backup

**Files:**

- Create: `src/nested_memvid_agent/desktop_update.py`
- Create: `src/nested_memvid_agent/server_desktop_update_routes.py`
- Create: `tests/test_desktop_update.py`
- Create: `tests/test_server_desktop_update_routes.py`
- Modify: `src/nested_memvid_agent/server.py`
- Modify: `src/nested_memvid_agent/state_store.py`
- Modify: `src/nested_memvid_agent/agent_backup.py`
- Modify: `src/nested_memvid_agent/memory_backup.py`

**Interfaces:**

- `GET /api/desktop/update/preflight`
- `POST /api/desktop/update/prepare`
- `POST /api/desktop/update/accept`
- Preflight checks active runs, approval execution, migrations, state integrity, routing receipt integrity, Memvid health/reopen readiness, disk, and target schema compatibility.
- Prepare creates one update receipt plus consistent SQLite/control-key/settings snapshot. Memvid files are backed up/checkpointed by the existing explicit backup contract but not rewritten.

- [ ] **Step 1: Write failing idle/snapshot tests**

```python
def test_update_preflight_blocks_nonterminal_work(update_service: DesktopUpdateService) -> None:
    report = update_service.preflight(target_manifest())
    assert report.ready is False
    assert set(report.blockers) == {
        "active_run",
        "approval_execution_in_progress",
        "ambiguous_provider_attempt",
    }


def test_prepare_snapshot_binds_state_key_and_app_versions(tmp_path: Path) -> None:
    receipt = update_service(tmp_path).prepare(target_manifest())
    assert receipt.from_version == "0.5.0"
    assert receipt.to_version == "0.6.0"
    assert receipt.state_snapshot_digest
    assert receipt.routing_integrity_key_digest
    assert receipt.memory_manifest_digest
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_update.py \
  tests/test_server_desktop_update_routes.py
```

Expected: update service/routes absent.

- [ ] **Step 3: Implement transactional preflight/prepare**

Use SQLite online backup while the runtime is quiesced. Include state DB, routing integrity key, settings, profile lease metadata template, and application/resource digests. Verify the backup by opening it read-only and checking both schemas/receipt authentication before returning prepared.

For Memvid, acquire the normal memory-system quiescence boundary, seal/verify layers, and write a backup manifest/checkpoint through existing `MemoryBackupManager`. Do not migrate or create over existing layer files.

`accept` is callable only from the nonce-bound new Desktop launch after complete preflight and before Mission opens. It records acceptance and unlocks ordinary execution.

- [ ] **Step 4: Run update, backup, Memvid, and full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_update.py \
  tests/test_server_desktop_update_routes.py \
  tests/test_agent_backup.py \
  tests/test_memory_backup.py \
  tests/test_chaos_recovery.py
RUN_MEMVID_INTEGRATION=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/integration/test_memvid_backend_integration.py \
  tests/integration/test_memvid_memory_system.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/desktop_update.py \
  src/nested_memvid_agent/server_desktop_update_routes.py \
  src/nested_memvid_agent/server.py \
  src/nested_memvid_agent/state_store.py \
  src/nested_memvid_agent/agent_backup.py \
  src/nested_memvid_agent/memory_backup.py \
  tests/test_desktop_update.py \
  tests/test_server_desktop_update_routes.py
git commit -m "feat: prepare consistent desktop update snapshots"
```

### Task 9: Implement opt-in download and owner-confirmed install

**Files:**

- Create: `desktop/src/main/updater.ts`
- Create: `desktop/src/main/updater.test.ts`
- Create: `desktop/src/main/update-state.ts`
- Create: `desktop/src/main/update-state.test.ts`
- Create: `web/src/settings/updates/UpdateSettings.tsx`
- Create: `web/src/settings/updates/UpdateSettings.test.tsx`
- Modify: `desktop/src/main/ipc.ts`
- Modify: `desktop/src/preload.ts`
- Modify: `desktop/src/contracts.ts`
- Modify: `web/src/settings/SettingsWorkspace.tsx`

**Interfaces:**

- Update states: `disabled`, `checking`, `available`, `downloading`, `downloaded`, `preflight_blocked`, `ready_to_install`, `installing`, `awaiting_acceptance`, `accepted`, `rollback_required`, `failed`.
- Check only after owner opt-in.
- Install only after explicit confirmation and successful server preflight/prepare.
- Invariant: no silent install and no install while work is non-idle.

- [ ] **Step 1: Write failing opt-in/install tests**

```ts
it("never contacts update feed before opt-in", async () => {
  const updater = createUpdater({ enabled: false });
  await updater.onAppReady();
  expect(updateClient.check).not.toHaveBeenCalled();
});

it("does not call quitAndInstall when preflight has blockers", async () => {
  const updater = createUpdater({ preparedDownload: true });
  server.preflightResult = { ready: false, blockers: ["active_run"] };
  await updater.installAfterOwnerConfirmation();
  expect(autoUpdater.quitAndInstall).not.toHaveBeenCalled();
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
npm --prefix desktop test -- updater update-state
npm --prefix web test -- UpdateSettings
```

Expected: modules absent.

- [ ] **Step 3: Implement guarded updater**

Verify Kestrel manifest first, then configure/validate electron-updater’s exact target metadata. Set `autoDownload` according to owner setting, but always set `autoInstallOnAppQuit=false`. Before install:

1. refetch/verify signed manifest;
2. verify downloaded bytes and platform signature;
3. call server preflight;
4. show exact blockers/authority impact;
5. obtain owner confirmation;
6. call server prepare;
7. stage recovery instruction/helper;
8. request graceful sidecar stop;
9. invoke platform install.

- [ ] **Step 4: Run updater/UI/full gates**

Run:

```bash
npm --prefix desktop test -- updater update-state ipc preload
npm --prefix web test -- UpdateSettings Settings
npm --prefix desktop run build
npm --prefix web run build
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_update.py \
  tests/test_server_desktop_update_routes.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add desktop/src/main/updater.ts \
  desktop/src/main/updater.test.ts \
  desktop/src/main/update-state.ts \
  desktop/src/main/update-state.test.ts \
  desktop/src/main/ipc.ts desktop/src/preload.ts desktop/src/contracts.ts \
  web/src/settings/updates web/src/settings/SettingsWorkspace.tsx
git commit -m "feat: require owner-confirmed verified desktop updates"
```

---

## Phase 4: Restore Matching Application Bytes and State

### Task 10: Implement authenticated recovery instructions and acceptance watchdog

**Files:**

- Modify: `src/nested_memvid_agent/desktop_recovery_helper.py`
- Create: `src/nested_memvid_agent/desktop_recovery_instruction.py`
- Create: `tests/test_desktop_recovery_instruction.py`
- Modify: `tests/test_desktop_recovery_helper.py`
- Create: `desktop/src/main/recovery-watchdog.ts`
- Create: `desktop/src/main/recovery-watchdog.test.ts`
- Modify: `desktop/src/main/updater.ts`

**Interfaces:**

- Recovery instruction binds from/to versions, app paths, previous signed artifact, current/new resource digests, state snapshot, routing key, memory manifest, acceptance marker, deadline, parent identity, and one-time authentication.
- Helper is launched before app exit, verifies instruction/paths/artifacts, waits for parent exit, and monitors acceptance.
- Invariant: helper may touch only exact staged application and snapshot paths beneath validated roots.

- [ ] **Step 1: Write failing path/auth/watchdog tests**

```python
def test_recovery_instruction_rejects_broad_or_escaping_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="outside recovery root"):
        parse_instruction(instruction(app_path=Path("/")))
    with pytest.raises(ValueError, match="outside recovery root"):
        parse_instruction(instruction(snapshot_path=tmp_path / ".." / "agent.db"))


def test_tampered_instruction_never_changes_app_or_state(tmp_path: Path) -> None:
    helper = recovery_helper(tmp_path)
    tamper(helper.instruction_path)
    assert helper.run() == RECOVERY_INSTRUCTION_INVALID
    assert helper.fs.mutations == []
```

```ts
it("marks accepted only after every new-version health gate passes", async () => {
  const watchdog = createWatchdog();
  server.readiness = { sidecar: true, schemas: true, memvid: true, demo: false };
  await watchdog.evaluate();
  expect(acceptanceMarker.exists()).toBe(false);
});
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_recovery_instruction.py \
  tests/test_desktop_recovery_helper.py
npm --prefix desktop test -- recovery-watchdog updater
```

Expected: instruction/watchdog incomplete.

- [ ] **Step 3: Implement the one-time recovery contract**

Use an owner-only one-time secret generated by Electron main and supplied to the helper through a private file opened before launch. The helper validates file owner/mode/non-symlink, exact path roots, resource/update signatures, previous artifact signature, and snapshot manifest before waiting.

New app remains in `awaiting_acceptance`; Mission and all mutations are blocked. It runs sidecar/resource verification, schema migrations transactionally, six-layer Memvid reopen/health, routing receipt verification, Demo smoke, and renderer handshake. Only then call server accept and atomically publish the marker.

- [ ] **Step 4: Run recovery/full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_recovery_instruction.py \
  tests/test_desktop_recovery_helper.py \
  tests/test_desktop_update.py \
  tests/test_chaos_recovery.py
npm --prefix desktop test -- recovery-watchdog updater
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/desktop_recovery_helper.py \
  src/nested_memvid_agent/desktop_recovery_instruction.py \
  tests/test_desktop_recovery_instruction.py \
  tests/test_desktop_recovery_helper.py \
  desktop/src/main/recovery-watchdog.ts \
  desktop/src/main/recovery-watchdog.test.ts \
  desktop/src/main/updater.ts
git commit -m "feat: bind desktop update acceptance to recovery watchdog"
```

### Task 11: Add platform-specific byte restoration

**Files:**

- Create: `src/nested_memvid_agent/desktop_recovery_macos.py`
- Create: `src/nested_memvid_agent/desktop_recovery_windows.py`
- Create: `src/nested_memvid_agent/desktop_recovery_linux.py`
- Create: `tests/test_desktop_recovery_macos.py`
- Create: `tests/test_desktop_recovery_windows.py`
- Create: `tests/test_desktop_recovery_linux.py`
- Modify: `src/nested_memvid_agent/desktop_recovery_helper.py`
- Modify: `desktop/electron-builder.yml`

**Interfaces:**

- macOS: retain previous signed updater ZIP/app bundle; verify codesign/notarization identity; stage sibling app; atomic swap in user-writable install location.
- Windows: retain previous signed NSIS installer; verify Authenticode publisher/digest; run per-user silent reinstall only after new app exits/fails acceptance.
- Linux: retain versioned signed AppImage; atomically switch owner-managed current pointer/desktop entry; `.deb`/`.rpm` recovery instructs reinstall of retained signed previous package when permissions permit.
- Restore matching SQLite/routing-key/settings snapshot before relaunching the previous version; leave Memvid files in place after verifying the manifest.

- [ ] **Step 1: Write failing per-platform rollback tests**

```python
def test_macos_rollback_validates_then_atomically_swaps(fake_macos: FakeMacOS) -> None:
    recover_macos(valid_instruction(), fake_macos)
    assert fake_macos.operations == [
        "verify_previous_zip",
        "extract_to_sibling_stage",
        "verify_codesign",
        "verify_bundle_manifest",
        "move_failed_app_to_quarantine",
        "rename_previous_app_into_place",
        "restore_matching_state",
        "launch_previous_app_recovery",
    ]


def test_windows_bad_publisher_never_runs_previous_installer(fake_windows: FakeWindows) -> None:
    with pytest.raises(RecoveryBlocked, match="publisher"):
        recover_windows(instruction_with_bad_publisher(), fake_windows)
    assert fake_windows.executed_installers == []


def test_linux_appimage_pointer_switch_is_atomic(fake_linux: FakeLinux) -> None:
    recover_linux(valid_instruction(), fake_linux)
    assert fake_linux.current_target == "Kestrel-0.5.0.AppImage"
    assert fake_linux.partial_links == []
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_recovery_macos.py \
  tests/test_desktop_recovery_windows.py \
  tests/test_desktop_recovery_linux.py
```

Expected: platform modules absent.

- [ ] **Step 3: Implement adapters behind injected OS protocols**

Use recoverable sibling staging/quarantine paths, not broad recursive deletion. Validate every target before mutation. If byte restoration succeeds but state restore fails, do not launch either version for mutation; open read-only Recovery with both copies preserved.

Memvid files are never replaced automatically. Verify their recorded paths/digests/format; if incompatible, stop and require the separate explicit Memvid migration contract.

- [ ] **Step 4: Run rollback/backup/full suites**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_recovery_macos.py \
  tests/test_desktop_recovery_windows.py \
  tests/test_desktop_recovery_linux.py \
  tests/test_desktop_recovery_helper.py \
  tests/test_agent_backup.py \
  tests/test_memory_backup.py
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/desktop_recovery_macos.py \
  src/nested_memvid_agent/desktop_recovery_windows.py \
  src/nested_memvid_agent/desktop_recovery_linux.py \
  src/nested_memvid_agent/desktop_recovery_helper.py \
  tests/test_desktop_recovery_macos.py \
  tests/test_desktop_recovery_windows.py \
  tests/test_desktop_recovery_linux.py \
  desktop/electron-builder.yml
git commit -m "feat: restore matching desktop bytes and state"
```

### Task 12: Add uninstall and explicit owner-data deletion boundaries

**Files:**

- Create: `src/nested_memvid_agent/desktop_uninstall.py`
- Create: `tests/test_desktop_uninstall.py`
- Create: `web/src/settings/storage/UninstallDataPanel.tsx`
- Create: `web/src/settings/storage/UninstallDataPanel.test.tsx`
- Modify: `desktop/electron-builder.yml`
- Modify: `desktop/src/main/ipc.ts`
- Modify: `docs/DEPLOYMENT.md`

**Interfaces:**

- Platform uninstaller removes app bytes, shortcuts, desktop entries, and managed launcher integration.
- It preserves profile/state/memory/projects/settings/backups by default.
- Separate data-deletion preview enumerates exact paths, sizes, state class, backup status, and irreversibility; execution is not part of normal uninstall.

- [ ] **Step 1: Write failing preservation tests**

```python
def test_uninstall_plan_never_contains_owner_data(default_paths: DesktopPaths) -> None:
    plan = plan_uninstall(default_paths)
    assert default_paths.application_path in plan.remove
    assert default_paths.profile_root not in plan.remove
    assert default_paths.memory_dir not in plan.remove
    assert default_paths.state_path not in plan.remove
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q tests/test_desktop_uninstall.py
npm --prefix web test -- UninstallDataPanel
```

Expected: uninstall boundary absent.

- [ ] **Step 3: Implement preservation-first behavior**

The GUI may preview owner-data deletion but requires exact typed confirmation and a fresh backup warning. Do not invoke deletion automatically in this release plan unless separately authorized and implemented with exact-path tests.

- [ ] **Step 4: Run tests**

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q tests/test_desktop_uninstall.py
npm --prefix web test -- UninstallDataPanel
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/nested_memvid_agent/desktop_uninstall.py \
  tests/test_desktop_uninstall.py \
  web/src/settings/storage \
  desktop/electron-builder.yml desktop/src/main/ipc.ts \
  docs/DEPLOYMENT.md
git commit -m "feat: preserve owner data during uninstall"
```

---

## Phase 5: Native CI, Clean-Machine Qualification, and Publication

### Task 13: Add native desktop rehearsal jobs

**Files:**

- Create: `.github/workflows/desktop-rehearsal.yml`
- Create: `scripts/verify_desktop_release_payload.py`
- Create: `tests/test_desktop_release_workflow.py`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release-rehearsal.yml`
- Modify: `scripts/release_publication_guard.py`

**Interfaces:**

- Native matrix builds each primary artifact on its platform/architecture.
- Rehearsal uploads internal artifacts only; no public release.
- Verify exact source SHA, locks, source tests, sidecar smoke, renderer/desktop tests, signatures in signing-capable protected run, SBOM, resource manifest, updater manifest, and artifact inventory.

- [ ] **Step 1: Write failing workflow-policy tests**

```python
def test_desktop_rehearsal_builds_natively_and_never_publishes() -> None:
    workflow = load_workflow(".github/workflows/desktop-rehearsal.yml")
    assert native_targets(workflow) == expected_primary_targets()
    assert "release" not in workflow["permissions"]["contents"]
    assert "actions/upload-artifact" in workflow_text(workflow)
    assert "gh release create" not in workflow_text(workflow)
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_release_workflow.py
```

Expected: workflow absent.

- [ ] **Step 3: Implement native jobs**

Each job:

1. checks exact SHA/clean tree;
2. installs pinned Python/Node build bootstrap;
3. validates locks;
4. runs relevant source suites;
5. builds renderer, sidecar, helper, SBOM, resource manifest;
6. packages current target;
7. signs in protected signing context or marks rehearsal unsigned with non-release key;
8. verifies inner/outer identity;
9. runs unpacked/installed smoke;
10. uploads one-SHA artifact plus verification receipt.

Keep protected release signing out of pull requests. Rehearsal proves all non-secret paths with developer signing and rejects publication.

- [ ] **Step 4: Run workflow/static tests**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_release_workflow.py \
  tests/test_release_supply_chain.py
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/desktop-rehearsal.yml \
  .github/workflows/ci.yml \
  .github/workflows/release-rehearsal.yml \
  scripts/verify_desktop_release_payload.py \
  scripts/release_publication_guard.py \
  tests/test_desktop_release_workflow.py
git commit -m "ci: rehearse native desktop release artifacts"
```

### Task 14: Add clean-machine install/update/rollback/uninstall qualification

**Files:**

- Create: `scripts/run_desktop_artifact_qualification.py`
- Create: `tests/test_desktop_artifact_qualification.py`
- Create: `desktop/e2e/installed-artifact.spec.ts`
- Create: `docs/DESKTOP_ARTIFACT_QUALIFICATION.md`
- Modify: `.github/workflows/desktop-rehearsal.yml`
- Modify: `docs/TEST_MATRIX.md`

**Interfaces:**

- Run in clean macOS, Windows, and Linux environments with no Python/Node.
- Test install, offline launch, setup, Demo mission, persistence, CLI coexistence, restart, update accept, forced update failure/rollback, uninstall preservation, tamper rejection, and 20 lifecycle cycles.
- Produce receipt `kestrel.desktop.artifact_qualification.v1`.

- [ ] **Step 1: Write failing receipt-verifier tests**

```python
def test_artifact_receipt_requires_twenty_clean_cycles() -> None:
    report = valid_artifact_report()
    report["lifecycle"]["completed_cycles"] = 19
    with pytest.raises(ValueError, match="20 lifecycle cycles"):
        verify_artifact_qualification(report)


def test_artifact_receipt_binds_signed_bytes_and_source_sha() -> None:
    report = valid_artifact_report()
    report["artifact_digest"] = "b" * 64
    with pytest.raises(ValueError, match="artifact digest"):
        verify_artifact_qualification(
            report,
            expected_artifact_digest="a" * 64,
        )
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_artifact_qualification.py
```

Expected: runner/verifier absent.

- [ ] **Step 3: Implement artifact-driven harness**

The harness must install the exact artifact, not run source or unpacked Electron. It records:

- source/tag/artifact/resource/SBOM/signature digests;
- OS/build/architecture;
- absence of Python/Node prerequisites;
- first-launch readiness;
- six Memvid paths and reopen result;
- state/routing schema migrations;
- Demo mission receipt;
- settings/project persistence;
- CLI/profile lease coexistence;
- child/listener residue;
- update snapshot/acceptance;
- forced sidecar/schema/renderer failure rollback;
- uninstall preserved-data paths/digests;
- tamper cases;
- 20 cycle timings and residues.

Use disposable VM snapshots or equivalent clean hosted runners. Preserve no credentials after the job.

- [ ] **Step 4: Run local verifier and hosted artifact jobs**

Run locally:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_desktop_artifact_qualification.py
```

Trigger `desktop-rehearsal.yml` and require all six primary target receipts. `.deb`/`.rpm` remain non-canonical until their installed qualification receipts pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_desktop_artifact_qualification.py \
  tests/test_desktop_artifact_qualification.py \
  desktop/e2e/installed-artifact.spec.ts \
  docs/DESKTOP_ARTIFACT_QUALIFICATION.md \
  .github/workflows/desktop-rehearsal.yml \
  docs/TEST_MATRIX.md
git commit -m "test: qualify exact installed desktop artifacts"
```

### Task 15: Integrate desktop assets into the protected release workflow

**Files:**

- Modify: `.github/workflows/release.yml`
- Modify: `scripts/release_publication_guard.py`
- Modify: `scripts/verify_release_payload.py`
- Modify: `tests/test_release_supply_chain.py`
- Modify: `tests/test_desktop_release_workflow.py`
- Modify: `docs/RELEASE_CHECKLIST.md`
- Modify: `CHANGELOG.md`

**Interfaces:**

- Protected release consumes exact-SHA successful CI, determinism, Python release rehearsal, desktop rehearsal, and installed-artifact qualification receipts completed before tag workflow publication.
- Publication assets include installers, updater metadata, Kestrel update manifest/signature, resource manifest/signature, SBOM, checksums, platform signature receipts, provenance attestations, and qualification receipts.
- Existing Python/OCI artifacts remain supported.

- [ ] **Step 1: Write failing release-gate tests**

```python
def test_release_requires_exact_sha_desktop_receipts_before_build() -> None:
    workflow = release_workflow_text()
    assert "Require successful exact-SHA desktop rehearsal" in workflow
    assert "Require exact signed desktop artifact qualification receipts" in workflow
    assert workflow.index("Require successful exact-SHA desktop rehearsal") < workflow.index(
        "Publish exact payload"
    )


def test_publication_guard_rejects_missing_architecture() -> None:
    with pytest.raises(ValueError, match="missing desktop targets"):
        validate_release_assets(release_assets_without_windows_arm64())
```

- [ ] **Step 2: Run and verify failures**

Run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/test_release_supply_chain.py \
  tests/test_desktop_release_workflow.py
```

Expected: release workflow does not yet require Desktop.

- [ ] **Step 3: Add protected native build/sign/publish fan-in**

Do not rebuild artifacts in the publish job. Download exact signed outputs from native protected jobs, reverify SHA/signatures/manifests/receipts, assemble one release manifest, attest provenance, then publish.

The release job must fail if:

- any target/architecture is absent;
- any artifact or inner resource digest drifts;
- signature/notarization is missing;
- source/version/commit mismatch;
- updater metadata points elsewhere;
- clean-machine receipt is missing/failing;
- 20 cycles are incomplete;
- qualification evidence belongs to an unsigned/different artifact.

- [ ] **Step 4: Run all source/workflow gates**

Run:

```bash
uv run python -m compileall -q src tests scripts
uv run ruff check scripts src tests
uv run mypy src
uv run bandit -q -r src -lll -iii
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
npm --prefix web run licenses:check
npm --prefix web test
npm --prefix web run build
npm --prefix desktop run licenses:check
npm --prefix desktop test
npm --prefix desktop run build
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/release.yml \
  scripts/release_publication_guard.py \
  scripts/verify_release_payload.py \
  tests/test_release_supply_chain.py \
  tests/test_desktop_release_workflow.py \
  docs/RELEASE_CHECKLIST.md CHANGELOG.md
git commit -m "release: gate signed Kestrel desktop artifacts"
```

---

## Final Verification

- [ ] Run exact final source gates:

```bash
uv lock --check
uv run python -m compileall -q benchmarks src tests scripts
uv run ruff check scripts src tests
uv run mypy src
uv run bandit -q -r src -lll -iii
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q
npm --prefix web ci
npm --prefix web run licenses:check
npm --prefix web run test:typecheck
npm --prefix web test
npm --prefix web run build
npm --prefix desktop ci
npm --prefix desktop run licenses:check
npm --prefix desktop run test:typecheck
npm --prefix desktop test
npm --prefix desktop run build
go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7
```

- [ ] Run gated integrations:

```bash
RUN_MEMVID_INTEGRATION=1 RUN_MCP_INTEGRATION=1 \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 uv run pytest -q \
  tests/integration/test_memvid_backend_integration.py \
  tests/integration/test_memvid_memory_system.py \
  tests/integration/test_memvid_context_frames.py \
  tests/integration/test_mcp_stdio_integration.py
```

- [ ] Require successful exact-SHA hosted receipts for:

  - macOS arm64 signed/notarized DMG/ZIP;
  - macOS x64 signed/notarized DMG/ZIP;
  - Windows arm64 signed NSIS;
  - Windows x64 signed NSIS;
  - Linux arm64 signed AppImage;
  - Linux x64 signed AppImage;
  - `.deb`/`.rpm` only after their own qualification;
  - sidecar/helper inner signatures and manifests;
  - SBOM/license coverage;
  - offline clean install;
  - six Memvid reopen behavior;
  - Demo first mission;
  - profile lease/CLI coexistence;
  - update acceptance;
  - forced update rollback of matching bytes/state;
  - uninstall owner-data preservation;
  - tamper rejection;
  - 20 clean launch/stop/update cycles.

- [ ] Inspect final artifact identity:

```bash
uv run python scripts/check_desktop_release_metadata.py \
  --release-tag "$EXPECTED_TAG" \
  --source-commit "$(git rev-parse HEAD)"
uv run python scripts/verify_desktop_release_payload.py \
  --release-root "$DESKTOP_RELEASE_ROOT" \
  --expected-commit "$(git rev-parse HEAD)"
git diff --check
git status --short
```

- [ ] Confirm no publication until explicit release authorization. Successful build/rehearsal creates internal artifacts only; a production tag/release changes persistent public state and remains a separate owner-authorized operation.

## Completion Criteria

- A clean owner on macOS, Windows, or Linux installs and opens Kestrel without Python, Node.js, or terminal setup.
- Offline Demo mode works from fully bundled core.
- Exactly six permanent Memvid v2 layer files initialize/reopen safely and persist across upgrades/reinstalls.
- Desktop, CLI, settings, projects, and memory coexist without concurrent profile writers.
- Every release artifact is signed, manifest/SBOM-bound, exact-SHA, and installed-artifact-qualified.
- Update checks are opt-in; install is owner-confirmed and idle-gated.
- A failed first launch or migration restores the previous verified application bytes and matching control-plane snapshot.
- Memvid is not automatically rewritten or downgraded by updater recovery.
- Uninstall preserves owner data by default.
- Twenty lifecycle/update cycles leave no orphan process, listener, state, launcher, or partial update residue.
- Python/OCI release paths remain intact.
- No public release is claimed until protected signing, exact installed-artifact gates, and explicit publication authorization all complete.
