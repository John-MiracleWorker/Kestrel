from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from scripts.check_project_metadata import _release_mode_errors

ROOT = Path(__file__).resolve().parents[1]
EXTENSION_TEST_IMAGE = (
    "python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3"
)


def test_package_metadata_identifies_kestrel_release() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = pyproject["project"]
    locked_versions = {package["name"]: package["version"] for package in lock["package"]}

    assert project["version"] == "0.4.0"
    assert "pip>=26.1.2" in project["dependencies"]
    assert "setuptools>=83.0.0" in project["dependencies"]
    assert locked_versions["pip"] == "26.1.2"
    assert locked_versions["setuptools"] == "83.0.0"
    assert locked_versions["build"] == "1.5.0"
    assert locked_versions["keyring"] == "25.7.0"
    keyring_deps = project["optional-dependencies"]["keyring"]
    assert any(str(dep).startswith("keyring>=25.6.0") for dep in keyring_deps)
    assert project["description"].startswith("Kestrel:")
    assert project["urls"]["Repository"] == "https://github.com/John-MiracleWorker/Kestrel"
    assert project["urls"]["Issues"] == "https://github.com/John-MiracleWorker/Kestrel/issues"


def test_python_and_web_release_metadata_stay_aligned() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_project_metadata.py")],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "nested-memvid-agent 0.4.0 development" in result.stdout
    assert "published release v0.3.1" in result.stdout
    assert "kestrel-web 0.4.0" in result.stdout


def test_release_metadata_gate_rejects_development_line() -> None:
    errors = _release_mode_errors(
        version="0.4.0",
        release_tag="v0.4.0",
        is_current_release=False,
        changelog="## [Unreleased]\n",
    )

    assert any("unreleased development line" in error for error in errors)
    assert any("dated release section" in error for error in errors)


def test_release_metadata_gate_accepts_exact_published_dated_release() -> None:
    errors = _release_mode_errors(
        version="0.4.0",
        release_tag="v0.4.0",
        is_current_release=True,
        changelog="## [0.4.0] - 2026-07-20\n",
    )

    assert errors == []


def test_release_workflow_strictly_checks_and_smokes_wheel_and_sdist() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "python -m twine check --strict dist/*" in workflow
    assert "Smoke-test built wheel" in workflow
    assert "Smoke-test built source distribution" in workflow
    assert "/tmp/kestrel-release-smoke/bin/python -m pip check" in workflow
    assert "/tmp/kestrel-release-sdist-smoke/bin/python -m pip check" in workflow
    assert '"${sdists[0]}[${RELEASE_EXTRAS}]"' in workflow
    assert workflow.count('joinpath("THIRD_PARTY_NOTICES.txt").is_file()') >= 1
    assert 'probe_name="kestrel-release-readonly-${architecture}"' in workflow
    assert "--read-only" in workflow
    assert "--tmpfs /data:rw,nosuid,nodev" in workflow
    assert "--user 999:999" in workflow
    assert "--runs 4" in workflow
    assert "--response-contract mock-echo" in workflow


def test_cross_platform_workflows_install_and_import_keyring_client() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    exact_wheel = (ROOT / "scripts" / "verify_exact_wheel_install.py").read_text(
        encoding="utf-8"
    )

    assert "config/python-build-bootstrap.txt" in ci
    assert (
        "python -m pip install --require-hashes --only-binary=:all: "
        "-r config/python-build-bootstrap.txt"
    ) in ci
    assert "python -m pip install --no-build-isolation -e '.[dev,keyring]'" in ci
    assert "python -c 'import keyring; assert callable(keyring.get_keyring)'" in ci
    assert "python -m scripts.verify_exact_wheel_install dist" in release
    assert '"--no-deps"' in exact_wheel
    assert "importlib.metadata.version" in exact_wheel
    assert "keyring, memvid_sdk, nested_memvid_agent" in exact_wheel


def test_release_publish_is_isolated_and_provenance_attested() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "permissions:\n  actions: read\n  contents: read" in workflow
    assert "name: Publish exact payload and multi-architecture image" in workflow
    assert "contents: write\n      id-token: write\n      attestations: write" in workflow
    assert "packages: write" in workflow
    assert "Upload validated release payload" in workflow
    assert "Download validated release payload" in workflow
    assert "Verify downloaded payload identity and checksums" in workflow
    assert "verify_release_payload.py dist --expected-version" in workflow
    assert "actions/attest-build-provenance@" in workflow


def test_release_workflow_executes_and_scans_amd64_and_arm64_images() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )

    assert "docker/setup-qemu-action@" in workflow
    assert "docker/setup-buildx-action@" in workflow
    assert 'for architecture in amd64 arm64; do' in workflow
    assert '--platform "linux/${architecture}"' in workflow
    assert '-t "kestrel-agent:release-${architecture}"' in workflow
    assert "kestrel-release-container-trivy-${architecture}.json" in workflow
    assert '("linux", "amd64"): amd64_digest_path.read_text' in workflow
    assert '("linux", "arm64"): arm64_digest_path.read_text' in workflow
    assert '"$IMAGE_NAME@$amd64_digest"' in workflow
    assert '"$IMAGE_NAME@$arm64_digest"' in workflow


def test_default_user_facing_branding_is_kestrel() -> None:
    web_index = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    example_config = (ROOT / "config" / "agent.example.json").read_text(encoding="utf-8")

    assert AgentConfig().name == "Kestrel"
    assert "<title>Kestrel</title>" in web_index
    assert '"name": "Kestrel"' in example_config


def test_community_policy_links_enabled_private_vulnerability_reporting() -> None:
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    issue_config = (ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text(
        encoding="utf-8"
    )
    bug_template = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(
        encoding="utf-8"
    )
    governance = (ROOT / "GOVERNANCE.md").read_text(encoding="utf-8")

    private_reporting_url = (
        "https://github.com/John-MiracleWorker/Kestrel/security/advisories/new"
    )
    assert private_reporting_url in security
    assert private_reporting_url in issue_config
    assert "private reporting channel linked in SECURITY.md" in bug_template
    assert "private process in SECURITY.md" not in bug_template
    assert "No separate project-lead appointment is currently recorded" in governance


def test_package_includes_runtime_prompt_data() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    package_data = pyproject["tool"]["setuptools"]["package-data"]
    dev_deps = pyproject["project"]["optional-dependencies"]["dev"]

    assert "prompts/*.md" in package_data["nested_memvid_agent"]
    assert "web_dist/index.html" in package_data["nested_memvid_agent"]
    assert "web_dist/assets/*" in package_data["nested_memvid_agent"]
    assert "web_dist/THIRD_PARTY_NOTICES.txt" in package_data["nested_memvid_agent"]
    assert "web/public/THIRD_PARTY_NOTICES.txt" in pyproject["project"]["license-files"]
    assert any(str(dep).startswith("bandit>=") for dep in dev_deps)
    assert "build==1.5.0" in dev_deps


def test_sdist_manifest_excludes_partial_tests_and_local_evidence() -> None:
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")

    assert "prune tests" in manifest
    assert "prune benchmark_results" in manifest
    assert "prune tiuni-fun" in manifest
    assert "exclude .env*" in manifest


def test_dockerfile_keeps_safe_runtime_defaults() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    command = next(line for line in dockerfile.splitlines() if line.startswith("CMD ["))

    assert (
        "FROM node:22-bookworm-slim@sha256:"
        "6c74791e557ce11fc957704f6d4fe134a7bc8d6f5ca4403205b2966bd488f6b3 "
        "AS web-build" in dockerfile
    )
    assert (
        "FROM rust:1.89-bookworm@sha256:"
        "948f9b08a66e7fe01b03a98ef1c7568292e07ec2e4fe90d88c07bb14563c84ff "
        "AS memvid-wheels" in dockerfile
    )
    pinned_python = (
        "python:3.11-slim-trixie@sha256:"
        "db3ff2e1800a8581e2c48a27c3995339d47bdf046da21c7627accd3d51053a93"
    )
    assert f"FROM {pinned_python} AS dependency-lock" in dockerfile
    assert f"FROM {pinned_python} AS runtime" in dockerfile
    assert "MEMVID_SDK_VERSION=2.0.160" in dockerfile
    assert (
        "MEMVID_SDK_SDIST_URL=https://files.pythonhosted.org/packages/7e/1a/"
        "709899b6757e1d1fde0bbe7e97fd814411931ab6c8c68b876305404b7a83/"
        "memvid_sdk-2.0.160.tar.gz" in dockerfile
    )
    assert "MEMVID_SDK_SDIST_SHA256=8eab5aec9a30eb459f553ed091038b6916d02a2f33569b32a7aee1b556820243" in dockerfile
    assert "UV_VERSION=0.11.16" in dockerfile
    assert "uv --version | cut -d' ' -f1-2" in dockerfile
    assert 'test "$(uv --version)" = "uv ${UV_VERSION}"' not in dockerfile
    assert "COPY pyproject.toml uv.lock ./" in dockerfile
    assert "COPY pyproject.toml README.md LICENSE ./" in dockerfile
    assert (
        "COPY web/public/THIRD_PARTY_NOTICES.txt "
        "./web/public/THIRD_PARTY_NOTICES.txt" in dockerfile
    )
    assert "uv export" in dockerfile
    assert "--frozen" in dockerfile
    assert "--no-emit-local" in dockerfile
    assert "requirements-runtime.txt" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "urllib.request.urlretrieve" in dockerfile
    assert "sha256sum -c -" in dockerfile
    assert "import memvid_sdk; assert callable(memvid_sdk.create)" in dockerfile
    assert "test -f /app/LICENSE" in dockerfile
    assert "chmod a-s /usr/bin/mount /usr/bin/umount /usr/bin/su" in dockerfile
    assert "test ! -u /usr/bin/mount" in dockerfile
    assert "test ! -u /usr/bin/umount" in dockerfile
    assert "test ! -u /usr/bin/su" in dockerfile
    assert ".dist-info/licenses/LICENSE" in dockerfile
    assert ".dist-info/licenses/web/public/THIRD_PARTY_NOTICES.txt" in dockerfile
    assert "npm run build" in dockerfile
    assert "pip install --no-deps --no-build-isolation -e \".[${INSTALL_EXTRAS}]\"" in dockerfile
    assert "USER kestrel" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "NEST_AGENT_BACKEND=memvid" in dockerfile
    assert "NEST_AGENT_SECRET_BACKEND=json" in dockerfile
    assert "NEST_AGENT_PLUGINS_DIR=/data/plugins" in dockerfile
    assert "/data/skills /data/plugins /data/config" in dockerfile
    assert "NEST_AGENT_ALLOW_SHELL=false" in dockerfile
    assert "NEST_AGENT_ALLOW_FILE_WRITE=false" in dockerfile
    assert "NEST_AGENT_ALLOW_POLICY_WRITES=false" in dockerfile
    assert "NEST_AGENT_ALLOW_CODEX_CLI=false" in dockerfile
    assert "NEST_AGENT_ALLOW_PLUGIN_INSTALL=false" in dockerfile
    assert "NEST_AGENT_ALLOW_EXECUTABLE_SKILLS=false" in dockerfile
    assert "NEST_AGENT_ALLOW_MCP_NETWORK_ENDPOINTS=false" in dockerfile
    assert "NEST_AGENT_REQUIRE_API_AUTH=true" in dockerfile
    assert "NEST_AGENT_API_TOKEN" in dockerfile
    assert "--extra keyring" in dockerfile
    assert "import keyring; assert callable(keyring.get_keyring)" in dockerfile
    assert "ARG INSTALL_EXTRAS=server,mcp,memvid,openai,anthropic,gemini,keyring" in dockerfile
    assert 'ENTRYPOINT ["/bin/sh", "/app/scripts/docker-entrypoint.sh"]' in dockerfile
    assert "/api/health/ready" in dockerfile
    assert '"--require-api-auth"' in command
    for env_owned_option in ("--backend", "--memory-dir", "--provider", "--model", "--state-path"):
        assert env_owned_option not in command


def test_docker_build_context_excludes_local_secrets_and_unrelated_trees() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".env*" in patterns
    assert ".env" not in patterns
    assert "tiuni-fun" in patterns


def test_docker_entrypoint_initializes_memvid_before_default_server() -> None:
    entrypoint = ROOT / "scripts" / "docker-entrypoint.sh"
    script = entrypoint.read_text(encoding="utf-8")
    syntax = subprocess.run(
        ["sh", "-n", str(entrypoint)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert syntax.returncode == 0, syntax.stderr
    assert 'if [ "$#" -ge 2 ] && [ "$1" = "nest-agent" ] && [ "$2" = "server" ]' in script
    assert "nest-agent init" in script
    assert 'exec "$@"' in script


def test_compose_binds_localhost_and_persists_data_volume() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    command = compose.split("    command:\n", maxsplit=1)[1].split("    healthcheck:\n", maxsplit=1)[0]

    assert "127.0.0.1:8765:8765" in compose
    assert "INSTALL_EXTRAS: server,mcp,memvid,openai,anthropic,gemini,keyring" in compose
    assert "kestrel-data:/data" in compose
    assert 'user: "999:999"' in compose
    assert "read_only: true" in compose
    assert "cap_drop:\n      - ALL" in compose
    assert "security_opt:\n      - no-new-privileges:true" in compose
    assert "/tmp:rw,noexec,nosuid,size=64m" in compose
    assert "NEST_AGENT_BACKEND: memvid" in compose
    assert "NEST_AGENT_SECRET_BACKEND: json" in compose
    assert "NEST_AGENT_PLUGINS_DIR: /data/plugins" in compose
    assert 'NEST_AGENT_ALLOW_SHELL: "false"' in compose
    assert 'NEST_AGENT_ALLOW_FILE_WRITE: "false"' in compose
    assert 'NEST_AGENT_ALLOW_POLICY_WRITES: "false"' in compose
    assert 'NEST_AGENT_ALLOW_PLUGIN_INSTALL: "false"' in compose
    assert 'NEST_AGENT_ALLOW_EXECUTABLE_SKILLS: "false"' in compose
    assert 'NEST_AGENT_ALLOW_MCP_NETWORK_ENDPOINTS: "false"' in compose
    assert 'NEST_AGENT_REQUIRE_API_AUTH: "true"' in compose
    assert "NEST_AGENT_API_TOKEN: ${NEST_AGENT_API_TOKEN:?Set NEST_AGENT_API_TOKEN for Kestrel API auth}" in compose
    assert "--require-api-auth" in compose
    assert "/api/health/ready" in compose
    for env_owned_option in ("--backend", "--memory-dir", "--provider", "--model", "--state-path"):
        assert env_owned_option not in command
    assert "env_file:" not in compose


def test_makefile_exposes_packaging_validation_targets() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    for target in [
        "install-dev:",
        "validate:",
        "doctor:",
        "chat-smoke:",
        "release-build:",
        "docker-build:",
        "docker-doctor:",
    ]:
        assert target in makefile
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PYTHON) -m pytest -q" in makefile
    assert "scripts/run_golden_evals.py --backend memory --provider mock" in makefile
    assert "docker run --rm $(DOCKER_IMAGE) nest-agent doctor" in makefile
    assert "--backend memvid --memory-dir /data/memory" in makefile


def test_deployment_docs_cover_release_and_memory_operations() -> None:
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    memory_ops = (ROOT / "docs" / "MEMORY_OPERATIONS.md").read_text(encoding="utf-8")
    security = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    checklist = (ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

    assert (
        "curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/"
        "v0.3.1/install.sh | bash"
    ) in deployment
    assert "`v0.3.1` is the current stable release" in deployment
    assert "unreleased `v0.4.0` development line" in deployment
    assert "releases/download/v0.3.1/install.sh" in deployment
    assert "releases/download/v0.4.0/install.sh" not in deployment
    assert "/Kestrel/main/install.sh" not in deployment
    assert "KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash" in deployment
    assert "does not start the server" in deployment
    assert "KESTREL_DRY_RUN=1 bash install.sh" in checklist
    assert "python -m pip install --no-build-isolation -e '.[memvid,openai,anthropic,gemini,server,mcp,keyring,dev]'" in deployment
    assert "stock headless container has no OS keychain service" in deployment
    assert "Never switch a populated JSON vault in place" in deployment
    assert "docker run --rm kestrel-agent:local" in deployment
    assert "OpenAI-compatible local servers" in deployment
    assert "`Authorization: Bearer REDACTED` on API requests." in deployment
    assert "Never call `create(path)` on an existing `.mv2` file." in memory_ops
    assert "NEST_AGENT_ALLOW_SHELL=false" in security
    assert "RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid" in checklist
    assert "Every Docker architecture" in deployment
    assert "hash-verified source distribution" in deployment


def test_one_shot_docs_match_safe_opt_in_server_defaults() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    for document in (readme, deployment):
        assert "does not start the server or open a browser unless explicitly enabled" in document
        assert "KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash install.sh" in document


def test_readme_behavior_delta_validation_fails_on_regression() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert (
        "python scripts/eval_behavior_deltas.py --scenario "
        "tests/evals/behavior_deltas/policy_write_requires_approval.json "
        "--fail-on-regression"
    ) in readme


def test_release_workflow_builds_and_publishes_tagged_artifacts() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    installer = (ROOT / "install.sh").read_text(encoding="utf-8")
    default_extras_match = re.search(r'^DEFAULT_EXTRAS="([^"]+)"$', installer, flags=re.MULTILINE)
    release_extras_match = re.search(r'^\s+RELEASE_EXTRAS: "([^"]+)"$', workflow, flags=re.MULTILINE)

    assert default_extras_match is not None
    assert release_extras_match is not None
    assert release_extras_match.group(1) == default_extras_match.group(1)
    assert not re.search(r"uses:\s+[^\s#]+@v\d+", workflow)
    assert 'tags: ["v*"]' in workflow
    assert "npm audit --audit-level=high" in workflow
    assert "npm run licenses:check" in workflow
    assert "npm run build" in workflow
    assert "Stage web workbench in Python package" in workflow
    assert "python scripts/stage_web_release.py" in workflow
    assert "python -m build" in workflow
    assert "Build, execute, and scan release containers" in workflow
    assert "check_container_vulnerabilities.py" in workflow
    assert "container_vulnerability_exceptions.json" in workflow
    assert 'raise SystemExit("sdist contains a partial test bundle")' in workflow
    assert "release wheel smoke" in workflow
    assert "curl -fsS http://127.0.0.1:8878/" in workflow
    assert "export NEST_AGENT_REQUIRE_API_AUTH=1" in workflow
    assert "export NEST_AGENT_API_TOKEN" in workflow
    assert 'test "$unauthenticated_code" = 401' in workflow
    assert 'Authorization: Bearer $NEST_AGENT_API_TOKEN' in workflow
    assert "Verify tag matches package version" in workflow
    assert 'test "$GITHUB_REF_NAME" = "v$VERSION"' in workflow
    assert 'python scripts/check_project_metadata.py --release-tag "$GITHUB_REF_NAME"' in workflow
    assert "scripts/run_golden_evals.py --backend memvid --provider mock" in workflow
    assert workflow.count("--max-case-latency-ms 45000") == 2
    assert "--response-contract mock-echo" in workflow
    assert "--min-throughput 1" in workflow
    assert "--require-overload" in workflow
    assert "--min-completed 4" in workflow
    assert "--max-overload-ratio 0.90" in workflow
    assert "--min-throughput 0.5" in workflow
    assert "NEST_AGENT_API_RATE_LIMIT_REQUESTS=100000" in workflow
    assert "NEST_AGENT_MAX_CONCURRENT_RUNS=1" in workflow
    assert "NEST_AGENT_MAX_QUEUED_RUNS=4" in workflow
    assert 'RUN_EXTENSION_SANDBOX_INTEGRATION: "1"' in workflow
    assert f'KESTREL_EXTENSION_TEST_IMAGE: "{EXTENSION_TEST_IMAGE}"' in workflow
    assert 'docker pull "$KESTREL_EXTENSION_TEST_IMAGE"' in workflow
    assert "tests/integration/test_extension_container_integration.py" in workflow
    assert "uv lock --check" in workflow
    assert "uv export --frozen --no-dev --no-emit-local" in workflow
    assert "--require-hashes" in workflow
    assert "requirements-release.txt" in workflow
    assert "python -m pip_audit --path" in workflow
    assert "--group release" in workflow
    assert "cyclonedx-py environment /tmp/kestrel-release-smoke/bin/python" in workflow
    assert '"nested-memvid-agent"' in workflow
    assert '"google-genai"' in workflow
    assert "Stage version-pinned installer" in workflow
    assert 'os.environ["GITHUB_REF_NAME"]' in workflow
    assert 'os.environ["RELEASE_EXTRAS"]' in workflow
    assert 'DEFAULT_REQUIREMENTS_URL=""' in workflow
    assert 'DEFAULT_WHEEL_URL=""' in workflow
    assert 'DEFAULT_CHECKSUMS_URL=""' in workflow
    assert 'DEFAULT_RELEASE_SHA=""' in workflow
    assert 'DEFAULT_RELEASE_VERSION=""' in workflow
    assert 'os.environ["RELEASE_COMMIT_SHA"]' in workflow
    assert "immutable release commit: $RELEASE_COMMIT_SHA" in workflow
    assert "KESTREL_REF=main bash dist/install.sh" in workflow
    assert "KESTREL_REPO=https://example.invalid/fork.git bash dist/install.sh" in workflow
    assert "{release_base}/install.sh" in workflow
    assert "${{KESTREL_REF" not in workflow
    assert "Defaults to {tag}." in workflow
    assert "Validate staged release installer plan" in workflow
    assert "bash < dist/install.sh" in workflow
    assert "verify SHA256SUMS" in workflow
    assert 'sha256sum install.sh requirements-release.txt "${wheels[@]}"' in workflow
    assert "gh release create \"$GITHUB_REF_NAME\" dist/*" in workflow
    assert "gh release create" in workflow
    assert 'extra_args+=(--extra "$extra")' in workflow
    assert '"keyring"' in workflow
    assert "import keyring; assert callable(keyring.get_keyring)" in workflow


def test_ci_runs_isolated_python_tests_and_web_build() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD: \"1\"" in ci
    assert "actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020" in ci
    assert "go run github.com/rhysd/actionlint/cmd/actionlint@v1.7.7" in ci
    assert "permissions:\n  contents: read" in ci
    assert 'node-version: "22"' in ci
    assert "cache-dependency-path: web/package-lock.json" in ci
    assert "run: npm ci" in ci
    assert "run: npm audit --audit-level=high" in ci
    assert "run: npm run licenses:check" in ci
    assert "run: npm test" in ci
    assert "run: npm run build" in ci
    assert "codeql:" in ci
    assert "name: CodeQL" in ci
    assert "github/codeql-action/init@b7351df727350dca84cb9d725d57dcf5bc82ba26" in ci
    assert "github/codeql-action/analyze@b7351df727350dca84cb9d725d57dcf5bc82ba26" in ci
    assert "security-events: write" in ci
    assert "foundational-integrations:" in ci
    assert 'RUN_MEMVID_INTEGRATION: "1"' in ci
    assert 'RUN_MCP_INTEGRATION: "1"' in ci
    assert "tests/integration/test_memvid_memory_system.py" in ci
    assert "tests/integration/test_mcp_stdio_integration.py" in ci
    assert "extension-sandbox:" in ci
    assert 'RUN_EXTENSION_SANDBOX_INTEGRATION: "1"' in ci
    assert f'KESTREL_EXTENSION_TEST_IMAGE: "{EXTENSION_TEST_IMAGE}"' in ci
    assert 'docker pull "$KESTREL_EXTENSION_TEST_IMAGE"' in ci
    assert "tests/integration/test_extension_container_integration.py" in ci
    assert "--backend memvid" in ci
    assert '"${RUNNER_TEMP}/kestrel-memvid-golden"' in ci
    assert "- foundational-integrations" in ci
    assert "- extension-sandbox" in ci
    assert "- codeql" in ci
    assert "run: python scripts/check_project_metadata.py" in ci
    assert "run: docker build -t kestrel-agent:ci ." in ci
    assert "Verify container privilege and license controls" in ci
    assert "check_container_vulnerabilities.py" in ci
    assert "cffe3f5161a47a6823fbd23d985795b3ed72a4c806da4c4df16266c02accdd6f" in ci


def test_generated_web_third_party_notices_are_present_and_complete() -> None:
    package_json = (ROOT / "web" / "package.json").read_text(encoding="utf-8")
    notice = (ROOT / "web" / "public" / "THIRD_PARTY_NOTICES.txt").read_text(
        encoding="utf-8"
    )

    assert '"licenses:check"' in package_json
    assert "Production packages: 106" in notice
    assert "lucide-react@0.561.0" in notice
    assert "The MIT License (MIT) (for portions derived from Feather)" in notice
    assert "@ungap/structured-clone@1.3.1" in notice
    assert "Copyright (c) 2021, Andrea Giammarchi, @WebReflection" in notice


def test_runtime_artifacts_are_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runs", "memory", "logs"],
        check=True,
        cwd=ROOT,
        text=True,
        capture_output=True,
    ).stdout.splitlines()

    assert tracked == []
