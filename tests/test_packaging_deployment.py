from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_identifies_kestrel_release() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    project = pyproject["project"]
    locked_versions = {package["name"]: package["version"] for package in lock["package"]}

    assert project["version"] == "0.3.1"
    assert "pip>=26.1.2" in project["dependencies"]
    assert "setuptools>=83.0.0" in project["dependencies"]
    assert locked_versions["pip"] == "26.1.2"
    assert locked_versions["setuptools"] == "83.0.0"
    assert project["description"].startswith("Kestrel:")
    assert project["urls"]["Repository"] == "https://github.com/John-MiracleWorker/Kestrel"
    assert project["urls"]["Issues"] == "https://github.com/John-MiracleWorker/Kestrel/issues"


def test_package_includes_runtime_prompt_data() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    package_data = pyproject["tool"]["setuptools"]["package-data"]
    dev_deps = pyproject["project"]["optional-dependencies"]["dev"]

    assert "prompts/*.md" in package_data["nested_memvid_agent"]
    assert "web_dist/index.html" in package_data["nested_memvid_agent"]
    assert "web_dist/assets/*" in package_data["nested_memvid_agent"]
    assert any(str(dep).startswith("bandit>=") for dep in dev_deps)
    assert any(str(dep).startswith("build>=") for dep in dev_deps)


def test_dockerfile_keeps_safe_runtime_defaults() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    command = next(line for line in dockerfile.splitlines() if line.startswith("CMD ["))

    assert "FROM node:22-bookworm-slim AS web-build" in dockerfile
    assert "FROM python:3.11-slim AS runtime" in dockerfile
    assert "npm run build" in dockerfile
    assert "pip install -e \".[${INSTALL_EXTRAS}]\"" in dockerfile
    assert "USER kestrel" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "NEST_AGENT_BACKEND=memvid" in dockerfile
    assert "NEST_AGENT_ALLOW_SHELL=false" in dockerfile
    assert "NEST_AGENT_ALLOW_FILE_WRITE=false" in dockerfile
    assert "NEST_AGENT_ALLOW_POLICY_WRITES=false" in dockerfile
    assert "NEST_AGENT_ALLOW_CODEX_CLI=false" in dockerfile
    assert "NEST_AGENT_ALLOW_PLUGIN_INSTALL=false" in dockerfile
    assert "NEST_AGENT_ALLOW_EXECUTABLE_SKILLS=false" in dockerfile
    assert "NEST_AGENT_ALLOW_MCP_NETWORK_ENDPOINTS=false" in dockerfile
    assert "NEST_AGENT_REQUIRE_API_AUTH=true" in dockerfile
    assert "NEST_AGENT_API_TOKEN" in dockerfile
    assert "ARG INSTALL_EXTRAS=server,mcp,memvid,openai,anthropic,gemini" in dockerfile
    assert 'ENTRYPOINT ["/bin/sh", "/app/scripts/docker-entrypoint.sh"]' in dockerfile
    assert "/api/health/ready" in dockerfile
    assert '"--require-api-auth"' in command
    for env_owned_option in ("--backend", "--memory-dir", "--provider", "--model", "--state-path"):
        assert env_owned_option not in command


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
    assert "INSTALL_EXTRAS: server,mcp,memvid,openai,anthropic,gemini" in compose
    assert "kestrel-data:/data" in compose
    assert "NEST_AGENT_BACKEND: memvid" in compose
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

    for target in ["install-dev:", "validate:", "doctor:", "chat-smoke:", "docker-build:", "docker-doctor:"]:
        assert target in makefile
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 $(PYTHON) -m pytest -q" in makefile
    assert "scripts/run_golden_evals.py --backend memory --provider mock" in makefile
    assert "docker run --rm $(DOCKER_IMAGE) nest-agent doctor" in makefile


def test_deployment_docs_cover_release_and_memory_operations() -> None:
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    memory_ops = (ROOT / "docs" / "MEMORY_OPERATIONS.md").read_text(encoding="utf-8")
    security = (ROOT / "docs" / "SECURITY.md").read_text(encoding="utf-8")
    checklist = (ROOT / "docs" / "RELEASE_CHECKLIST.md").read_text(encoding="utf-8")

    assert (
        "curl -fsSL https://github.com/John-MiracleWorker/Kestrel/releases/download/"
        "v0.3.1/install.sh | bash"
    ) in deployment
    assert "/Kestrel/main/install.sh" not in deployment
    assert "KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash" in deployment
    assert "does not start the server" in deployment
    assert "KESTREL_DRY_RUN=1 bash install.sh" in checklist
    assert "python -m pip install -e '.[memvid,openai,anthropic,gemini,server,mcp,dev]'" in deployment
    assert "docker run --rm kestrel-agent:local" in deployment
    assert "OpenAI-compatible local servers" in deployment
    assert "`Authorization: Bearer REDACTED` on API requests." in deployment
    assert "Never call `create(path)` on an existing `.mv2` file." in memory_ops
    assert "NEST_AGENT_ALLOW_SHELL=false" in security
    assert "RUN_MEMVID_INTEGRATION=1 python scripts/run_golden_evals.py --backend memvid" in checklist


def test_one_shot_docs_match_safe_opt_in_server_defaults() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    for document in (readme, deployment):
        assert "does not start the server or open a browser unless explicitly enabled" in document
        assert "KESTREL_START_SERVER=1 KESTREL_OPEN_BROWSER=1 bash install.sh" in document


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
    assert "npm run build" in workflow
    assert "Stage web workbench in Python package" in workflow
    assert "src/nested_memvid_agent/web_dist" in workflow
    assert "python -m build" in workflow
    assert "release wheel smoke" in workflow
    assert "curl -fsS http://127.0.0.1:8878/" in workflow
    assert "Verify tag matches package version" in workflow
    assert 'test "$GITHUB_REF_NAME" = "v$VERSION"' in workflow
    assert "uv lock --check" in workflow
    assert "uv export --frozen --no-dev --no-emit-local" in workflow
    assert "--require-hashes" in workflow
    assert "requirements-release.txt" in workflow
    assert "python -m pip_audit --path" in workflow
    assert "cyclonedx-bom==7.3.0" in workflow
    assert "pip-audit==2.10.1" in workflow
    assert "cyclonedx-py environment /tmp/kestrel-release-smoke/bin/python" in workflow
    assert '"nested-memvid-agent"' in workflow
    assert '"google-genai"' in workflow
    assert "Stage version-pinned installer" in workflow
    assert 'os.environ["GITHUB_REF_NAME"]' in workflow
    assert 'os.environ["RELEASE_EXTRAS"]' in workflow
    assert 'DEFAULT_REQUIREMENTS_URL=""' in workflow
    assert 'DEFAULT_WHEEL_URL=""' in workflow
    assert 'DEFAULT_CHECKSUMS_URL=""' in workflow
    assert "{release_base}/install.sh" in workflow
    assert "${{KESTREL_REF" not in workflow
    assert "Defaults to {tag}." in workflow
    assert "Validate staged release installer plan" in workflow
    assert "verify SHA256SUMS" in workflow
    assert 'sha256sum install.sh requirements-release.txt "${wheels[@]}"' in workflow
    assert "gh release create \"$GITHUB_REF_NAME\" dist/*" in workflow
    assert "gh release create" in workflow
    assert 'extra_args+=(--extra "$extra")' in workflow


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
    assert "run: npm test" in ci
    assert "run: npm run build" in ci
    assert "run: docker build -t kestrel-agent:ci ." in ci


def test_runtime_artifacts_are_not_tracked() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "runs", "memory", "logs"],
        check=True,
        cwd=ROOT,
        text=True,
        capture_output=True,
    ).stdout.splitlines()

    assert tracked == []
