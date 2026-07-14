from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_identifies_kestrel_release() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]

    assert project["version"] == "0.2.0"
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
    assert "--backend\", \"memvid\"" in dockerfile
    assert "\"--require-api-auth\"" in dockerfile


def test_compose_binds_localhost_and_persists_data_volume() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

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

    assert "curl -fsSL https://raw.githubusercontent.com/John-MiracleWorker/Kestrel/main/install.sh | bash" in deployment
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

    assert 'tags: ["v*"]' in workflow
    assert "npm run build" in workflow
    assert "Stage web workbench in Python package" in workflow
    assert "src/nested_memvid_agent/web_dist" in workflow
    assert "python -m build" in workflow
    assert "release wheel smoke" in workflow
    assert "curl -fsS http://127.0.0.1:8878/" in workflow
    assert "Verify tag matches package version" in workflow
    assert 'test "$GITHUB_REF_NAME" = "v$VERSION"' in workflow
    assert "Stage version-pinned installer" in workflow
    assert 'os.environ["GITHUB_REF_NAME"]' in workflow
    assert "gh release create \"$GITHUB_REF_NAME\" dist/*" in workflow
    assert "gh release create" in workflow


def test_ci_runs_isolated_python_tests_and_web_build() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD: \"1\"" in ci
    assert "setup-node@v4" in ci
    assert 'node-version: "22"' in ci
    assert "cache-dependency-path: web/package-lock.json" in ci
    assert "run: npm ci" in ci
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
