# Release Candidate Checklist

Use this before tagging or publishing the supported single-user, single-node local/private build. The authoritative acceptance criteria are in `docs/PRODUCTION_OPERATIONS.md`; every command must run against the exact candidate bytes.

## Core Validation

```bash
python -m compileall -q src tests scripts
python scripts/check_project_metadata.py
python -m ruff check scripts src tests
python -m mypy src
python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
python -m ruff check benchmarks/real_agent_learning_benchmark.py tests/test_real_agent_learning_benchmark.py
MYPYPATH=src python -m mypy --strict benchmarks/real_agent_learning_benchmark.py
BENCHMARK_OUTPUT="$(mktemp -d)/agent-learning-gate.json"
python benchmarks/real_agent_learning_benchmark.py --output "$BENCHMARK_OUTPUT"
npm ci --prefix web
npm run test --prefix web
npm run licenses:check --prefix web
npm audit --audit-level=high --prefix web
npm run build --prefix web
bandit -q -r src -lll -iii
test -z "$(git status --short)"
RELEASE_COMMIT_SHA="$(git rev-parse 'HEAD^{commit}')"
GITLEAKS_IMAGE='zricethezav/gitleaks@sha256:c00b6bd0aeb3071cbcb79009cb16a60dd9e0a7c60e2be9ab65d25e6bc8abbb7f'
CANDIDATE_SOURCE="$(mktemp -d)"
git archive --format=tar "$RELEASE_COMMIT_SHA" | tar -xf - -C "$CANDIDATE_SOURCE"
docker pull "$GITLEAKS_IMAGE"
docker run --rm -v "$CANDIDATE_SOURCE:/repo:ro" -w /repo "$GITLEAKS_IMAGE" dir --redact=100 --no-banner .
test "$(gh api 'repos/John-MiracleWorker/Kestrel/secret-scanning/alerts?state=open&per_page=100' --jq 'length')" -eq 0
shellcheck install.sh scripts/*.sh
bash -n install.sh scripts/*.sh
git diff --check
```

## Foundational Integration Validation

These Memvid v2, stdio MCP, and executable-skill OCI fixtures require no provider credentials. They are required before tagging and run in pull-request/branch CI. The OCI gate requires Docker and the exact pre-pulled image:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q tests/integration/test_memvid_backend_integration.py tests/integration/test_memvid_memory_system.py tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid --provider mock --memory-dir /tmp/kestrel-memvid-golden
docker pull 'python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
RUN_EXTENSION_SANDBOX_INTEGRATION=1 \
KESTREL_EXTENSION_TEST_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3' \
python -m pytest -q tests/integration/test_extension_container_integration.py
```

## Optional Live-Provider Validation

Run the cases for each provider claimed by the release when credentials and endpoints are available:

```bash
RUN_PROVIDER_INTEGRATION=1 python -m pytest -q tests/integration/test_provider_live_integration.py
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memory --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memory
OLLAMA_API_KEY=... python scripts/run_golden_evals.py --backend memvid --provider ollama-cloud --model gpt-oss:120b --memory-dir /tmp/kestrel-live-golden-memvid
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memory --output-root /tmp/kestrel-live-learning-memory
python scripts/run_live_learning_eval.py --provider ollama-cloud --model gpt-oss:120b --backend memvid --output-root /tmp/kestrel-live-learning-memvid
```

## Packaging Validation

```bash
npm ci --prefix web
npm run licenses:check --prefix web
npm run build --prefix web
python scripts/stage_web_release.py
SOURCE_ROOT="$PWD"
RELEASE_TMP="$(mktemp -d)"
DIST_DIR="$RELEASE_TMP/dist"
VERSION="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
RELEASE_EXTRAS='memvid,openai,anthropic,gemini,server,mcp,keyring'
python -m pip install --require-hashes --only-binary=:all: -r config/release-build-bootstrap.txt
uv export --frozen --no-dev --no-emit-local \
  --extra memvid --extra openai --extra anthropic --extra gemini \
  --extra server --extra mcp --extra keyring \
  --format requirements.txt --output-file "$RELEASE_TMP/requirements-release.txt"
python -m build --no-isolation --outdir "$DIST_DIR"
python -m twine check --strict "$DIST_DIR"/*
WHEELS=("$DIST_DIR"/*.whl)
SDISTS=("$DIST_DIR"/*.tar.gz)
test "${#WHEELS[@]}" -eq 1
test "${#SDISTS[@]}" -eq 1
# Require THIRD_PARTY_NOTICES.txt in both the packaged web_dist and license metadata.
python -m zipfile -l "$DIST_DIR"/*.whl | grep 'nested_memvid_agent/web_dist/THIRD_PARTY_NOTICES.txt'
tar -tzf "$DIST_DIR"/*.tar.gz | grep '/web/public/THIRD_PARTY_NOTICES.txt'

# Validate the wheel from outside the checkout in a clean environment.
python -m venv "$RELEASE_TMP/wheel-venv"
WHEEL_PY="$RELEASE_TMP/wheel-venv/bin/python"
"$WHEEL_PY" -m pip install --require-hashes --only-binary=:all: \
  -r "$RELEASE_TMP/requirements-release.txt"
"$WHEEL_PY" -m pip install --no-deps "${WHEELS[0]}[$RELEASE_EXTRAS]"
"$WHEEL_PY" -m pip check
(
  cd "$RELEASE_TMP"
  "$WHEEL_PY" -I -c 'import importlib.metadata, sys; actual=importlib.metadata.version("nested-memvid-agent"); assert actual == sys.argv[1], (actual, sys.argv[1])' "$VERSION"
  "$WHEEL_PY" -I -m nested_memvid_agent.cli doctor --backend memvid --memory-dir "$RELEASE_TMP/wheel-doctor-memory" --provider mock --model mock
  "$WHEEL_PY" -I -m nested_memvid_agent.cli chat --backend memvid --memory-dir "$RELEASE_TMP/wheel-chat-memory" --provider mock --model mock --message "clean wheel smoke"
  "$WHEEL_PY" -I "$SOURCE_ROOT/scripts/verify_installed_memvid.py" --source-root "$SOURCE_ROOT" --memory-dir "$RELEASE_TMP/wheel-memvid"
)

# Validate the sdist separately with the exact build bootstrap and no build isolation.
python -m venv "$RELEASE_TMP/sdist-venv"
SDIST_PY="$RELEASE_TMP/sdist-venv/bin/python"
"$SDIST_PY" -m pip install --require-hashes --only-binary=:all: \
  -r config/release-build-bootstrap.txt
"$SDIST_PY" -m pip install --require-hashes --only-binary=:all: \
  -r "$RELEASE_TMP/requirements-release.txt"
"$SDIST_PY" -m pip install --no-build-isolation --no-deps \
  "${SDISTS[0]}[$RELEASE_EXTRAS]"
"$SDIST_PY" -m pip check
(
  cd "$RELEASE_TMP"
  "$SDIST_PY" -I -c 'import importlib.metadata, sys; from importlib.resources import files; actual=importlib.metadata.version("nested-memvid-agent"); assert actual == sys.argv[1], (actual, sys.argv[1]); web=files("nested_memvid_agent").joinpath("web_dist"); assert web.joinpath("index.html").is_file(); assert web.joinpath("THIRD_PARTY_NOTICES.txt").is_file()' "$VERSION"
  "$SDIST_PY" -I -m nested_memvid_agent.cli doctor --backend memvid --memory-dir "$RELEASE_TMP/sdist-doctor-memory" --provider mock --model mock
)
bash -n install.sh
KESTREL_DRY_RUN=1 bash install.sh
KESTREL_DRY_RUN=1 KESTREL_START_SERVER=0 bash install.sh
docker build -t kestrel-agent:local .
docker run --rm kestrel-agent:local nest-agent doctor --backend memvid --memory-dir /data/memory --provider mock
```

The tag workflow first proves that the exact tag SHA already has a successful `main` push run of
the complete CI workflow. It then builds the wheel once and installs that identical downloaded
wheel plus its hash-locked dependency payload on Linux x86_64, Apple-silicon macOS, Intel
macOS, and native Windows x86_64 with Python 3.11, 3.12, and 3.13. The macOS
lanes use `macos-latest` (arm64) and `macos-15-intel` (x86_64); every lane asserts
`platform.machine()` against its declared architecture before installing the candidate. Every
lane also asserts `importlib.metadata` version, imports Memvid, runs Kestrel doctor/chat from
outside the checkout, and performs a real Memvid v2 write/seal/verify/reopen/search integration.
Keyring client import is required, but a usable OS keychain is not claimed on headless runners.
The workflow also builds, executes, and Trivy-scans
both `linux/amd64` and `linux/arm64` images under pinned Buildx/QEMU tooling; a native-only local
image does not replace that release gate. After the image checks pass, CI archives those exact
images and transfers them to the publication job instead of rebuilding them. Only that final job
has `packages: write`; it runs after the wheel matrix and every candidate gate. Before its first
registry write it queries the tag's GitHub release. A rerun against an already published immutable
release verifies the checksum-bound OCI digest record, both public index refs, and both stable
per-architecture refs, then performs no GHCR or GitHub Release mutation. Draft/partial runs push
each architecture through a run-attempt
candidate ref, then publish the stable per-architecture, immutable-SHA, and version refs only when
the current ref is absent or already has the exact candidate digest; a different current digest is
a hard failure. It publishes a two-platform GHCR index under `sha-<full-GitHub-SHA>`. Each
per-platform `docker push` result is
parsed for the registry-returned manifest digest, the index is composed from those immutable
`IMAGE@sha256:...` references rather than the temporary architecture tags, and the published
platform descriptors must exactly equal the two captured digests. CI then anonymously pulls each
platform digest and runs doctor. It resolves the current remote tag with `git ls-remote` (including
annotated-tag peeling) without fetching it into a local tag ref before attestations, and repeats
that full resolution after attestations immediately before the version image. Publication then
requires repository-level immutable releases, creates a draft, rejects any pre-existing asset name
outside the exact local payload, uploads only that payload, and downloads every asset to compare
the final filename set and SHA-256 bytes. It then revalidates both the remote tag and current `main`
after the upload. Only then does it publish the draft and verify that GitHub reports the release as
immutable. Every check requires the peeled
commit to equal `GITHUB_SHA`; all full checks also require it to remain an ancestor of the current
remote `main`.

The workflow serializes runs per release ref, but repository administrators must also prevent
release-tag updates/deletion and concurrent external package writers during publication. GitHub
release immutability locks the published tag and assets after the final draft-to-published
transition. The repeated remote-tag checks and digest-addressed image composition fail closed on
observable drift before that transition; they do not make a mutable registry tag transactionally
immutable.

The GHCR package must be public so Linux ARM64 users can pull the documented fallback without a
credential. A first publication that leaves the package private is expected to fail at the
anonymous post-publish pull. Set the linked package visibility to public, then use GitHub's
**Re-run failed jobs** action within the one-day artifact-retention window so publication reuses
the exact uploaded container archives. Do not restart all jobs, rebuild the images, or weaken the
anonymous-pull gate.

PyPI publication uses Trusted Publishing rather than a long-lived repository secret. Configure a
pending publisher for project `nested-memvid-agent`, owner `John-MiracleWorker`, repository
`Kestrel`, workflow `release.yml`, and environment `pypi`. The `pypi` environment must require
manual approval by a trusted reviewer. Its least-privilege job starts only after immutable GitHub
release and GHCR publication, downloads the already validated payload, verifies `SHA256SUMS`,
isolates exactly one wheel and one sdist, and invokes the SHA-pinned PyPA publishing action with
job-scoped `id-token: write`. Before uploading, it queries the exact PyPI project/version file set;
an already present file is skipped only when its filename and SHA-256 match, while unknown,
mismatched, duplicate, or yanked files fail closed. A partial exact upload resumes with only the
missing distribution. It receives no long-lived PyPI token.

After publication, download the complete release and verify repository provenance plus payload
identity (replace the version as appropriate):

```bash
gh release download v0.4.0 --repo John-MiracleWorker/Kestrel --dir "$RELEASE_TMP/published"
for artifact in "$RELEASE_TMP"/published/*; do
  gh attestation verify "$artifact" --repo John-MiracleWorker/Kestrel
done
python scripts/verify_release_payload.py "$RELEASE_TMP/published" --expected-version v0.4.0
docker buildx imagetools inspect ghcr.io/john-miracleworker/kestrel:v0.4.0
docker pull --platform linux/amd64 ghcr.io/john-miracleworker/kestrel:v0.4.0
docker pull --platform linux/arm64 ghcr.io/john-miracleworker/kestrel:v0.4.0
```

Require the image inspection to contain exactly `linux/amd64` and `linux/arm64`, verify the image
attestation against its index digest, and check the source/version/revision labels on both child
manifests. The version and `sha-<full-GitHub-SHA>` references must resolve to the same index
digest. This verifies the released bytes and source provenance; it is not a bit-for-bit rebuild
claim. Debian package-index inputs and frontend build tooling remain mutable build inputs.

Optional one-shot installer smoke from a local repo clone:

```bash
RUN_MEMVID_INTEGRATION=1 RUN_INSTALLER_INTEGRATION=1 python -m pytest -q tests/test_install_script.py::test_install_from_local_repo_smoke_with_memvid
```

Run the authenticated mock-provider soak command from `docs/PRODUCTION_OPERATIONS.md`, plus restart recovery and backup/restore drills. Audit the fully pinned release dependency set and require zero known vulnerabilities.

## Documentation Checks

- `.env.example` documents provider keys and safety flags.
- `README.md` exposes the public GitHub curl installer.
- `docs/DEPLOYMENT.md` covers one-shot, local, Docker, Compose, provider, and local model setup.
- `docs/MEMORY_OPERATIONS.md` covers backup, restore, verification, and migration without recreating existing `.mv2` files.
- `docs/SECURITY.md` keeps dangerous tool enablement explicit.
- Root `SECURITY.md` names an enabled confidential vulnerability-reporting channel.
- GitHub secret scanning and push protection are enabled for the public repository.
- `main` branch protection requires the cross-platform CI checks and disallows force pushes and
  deletion outside an explicit incident-recovery procedure.
- `docs/CONTROLLED_SELF_MODIFICATION.md` documents behavior-delta gates, review, replay, rollback, and live-learning boundaries.

## Release Gate

Do not tag the release if any of these are true:

- Core validation fails.
- Credential-free Memvid v2, stdio MCP, or executable-skill OCI containment integration fails or skips in enabled mode.
- Python and private web release metadata do not agree.
- The deterministic end-to-end agent learning gate, golden evals, or live-learning E2E checks regress.
- Memvid verification fails for a production memory directory.
- High-risk tools are enabled by default.
- `.mv2` memory is replaced by another primary memory store.
- Policy memory can be written from one ordinary event.
- The source candidate has not passed repository CI, including the native Windows source lane.
- The exact release tag SHA has no successful complete CI `push` run on `main`.
- The exact single built wheel and hash-locked dependency payload have not passed Linux x86_64,
  Apple-silicon macOS, and Intel macOS on every supported Python version (3.11 through 3.13),
  plus native Windows x86_64 on every supported Python version, with each runner architecture
  asserted explicitly.
- The release tag is not on `main`, or the exact tag bytes have not passed the release workflow's
  cross-platform matrix before publication.
- Strict Twine metadata validation; matching distribution/version across wheel/sdist filenames,
  wheel `METADATA`, sdist `PKG-INFO`, and CycloneDX; separate isolated wheel and sdist installs;
  packaged web assets; dependency audit; chaos recovery; or bounded soak has not passed.
- The generated production-web third-party notice is stale or absent from either release artifact.
- Either `linux/amd64` or `linux/arm64` fails its executable container smoke or enforced Trivy policy.
- The exact scanned container archives are rebuilt in the publication job, their GHCR index does
  not contain exactly `linux/amd64` plus `linux/arm64`, their source/version/revision labels drift,
  their public post-publish pull/doctor fails, or the version and immutable-SHA index digests differ.
- The multi-platform index is composed from mutable architecture tags instead of the exact
  registry-returned per-platform push digests, or either published platform descriptor differs
  from its captured digest.
- A rerun can mutate GHCR before inspecting an existing GitHub release, treats a published
  immutable release as mutable work, lacks the checksum-bound OCI digest record, or can replace a
  per-architecture, immutable-SHA, or version ref whose current digest differs from the candidate.
- The current remote release tag cannot be peeled unambiguously to `GITHUB_SHA` immediately before
  attestations and again immediately before release publication, the release commit is no longer
  an ancestor of the current remote `main`, or release-tag immutability is not administratively
  enforced.
- The workflow cannot create a draft, reject unknown assets, upload the exact local payload,
  download and SHA-256-verify the final asset set, revalidate tag and `main`, publish the draft,
  and verify GitHub's immutable-release state in that order.
- The validated release payload lacks GitHub build-provenance attestation before publication.
- The published multi-platform image digest lacks GitHub build-provenance attestation.
- No enabled confidential vulnerability-reporting channel is available to security researchers.
- Candidate-source scanning reports any non-fixture finding, GitHub has any open secret alert,
  repository secret scanning/push protection is disabled, or any historical credential lacks
  confirmed provider revocation/rotation and incident documentation.
- The exact PyPI pending Trusted Publisher or approval-protected `pypi` GitHub environment is
  absent, the OIDC job receives broader permissions or repository secrets, or PyPI does not expose
  the expected release with Trusted Publisher provenance after publication.
- PyPI partial-upload recovery trusts `skip-existing`, an existing filename without an exact
  SHA-256 match, or a version containing an unknown or yanked distribution.
- `main` has no required-check branch protection for the exact candidate workflow.
- Independent review is incomplete, the worktree is dirty, or the tag/version/publication action is not deliberate.
