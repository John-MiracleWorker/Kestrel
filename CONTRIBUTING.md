# Contributing to Kestrel

Thanks for helping make Kestrel a safer and more useful personal engineering agent.
Kestrel is currently a maintainer-led project, and focused pull requests are welcome.

## Before You Start

- Search existing issues and pull requests before opening a duplicate.
- Open an issue before a large architectural change so the direction can be discussed first.
- Follow [SECURITY.md](SECURITY.md) and use GitHub private vulnerability reporting for suspected
  vulnerabilities; do not file a public security issue.
- Read [AGENTS.md](AGENTS.md) and [docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md](docs/CODEX_FULL_AGENT_HANDOFF_PROMPT.md) before changing runtime behavior.

## Development Setup

Kestrel requires Python 3.11 or newer and Node.js 22 for the web workbench.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[memvid,openai,anthropic,gemini,server,mcp,dev]'
npm install --prefix web
```

On Windows, activate the virtual environment with the command appropriate for your shell.
The Python runtime is tested on native Windows; the one-shot Bash installer is intended for
macOS, Linux, and Linux inside WSL.

## Architecture Invariants

Contributions must preserve these contracts:

- Use Memvid v2 `.mv2` files only, with one permanent file per nested memory layer.
- Never call `create(path)` on an existing `.mv2` file.
- Keep SQLite in the control plane; do not replace `.mv2` retrieval memory with a database or JSON store.
- Keep the conversational CLI functional independently of the optional web workbench.
- Keep the mock backend and mock LLM deterministic.
- Never promote one ordinary event directly into policy memory.
- Preserve explicit enablement and exact-call approval for high-risk tools.
- Attach evidence, provenance, confidence, and validation status to every memory promotion.

If a change intentionally revises one of these contracts, start with a public design issue and
obtain maintainer agreement before implementation.

## Make a Focused Change

1. Create a branch from the current development base.
2. Add or update tests with the behavior change.
3. Keep generated files, `.nest/` state, credentials, caches, and unrelated local work out of the diff.
4. Update user-facing documentation and [CHANGELOG.md](CHANGELOG.md) when behavior, configuration, compatibility, or installation changes.
5. Run `python -m pytest -q` after each coherent phase.

Use Ruff for formatting and lint-compatible code. Keep public interfaces typed and make test inputs
deterministic. Do not weaken a safety gate to make a test pass.

## Validation

Normal Python changes should pass:

```bash
python -m compileall -q src tests scripts
python -m ruff check scripts src tests
python -m mypy src
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q
python scripts/run_golden_evals.py --backend memory --provider mock
```

Web changes should also pass:

```bash
npm test --prefix web
npm run build --prefix web
```

Memvid, MCP, and executable-skill containment changes should run their credential-free integration fixtures:

```bash
RUN_MEMVID_INTEGRATION=1 python -m pytest -q \
  tests/integration/test_memvid_backend_integration.py \
  tests/integration/test_memvid_memory_system.py \
  tests/integration/test_memvid_context_frames.py
RUN_MCP_INTEGRATION=1 python -m pytest -q tests/integration/test_mcp_stdio_integration.py
docker pull 'python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3'
RUN_EXTENSION_SANDBOX_INTEGRATION=1 \
KESTREL_EXTENSION_TEST_IMAGE='python@sha256:5c34b355088846dddc8afb7442c20b9433dccdc8d66192dc52c616adeaa106a3' \
python -m pytest -q tests/integration/test_extension_container_integration.py
```

Live-provider tests remain opt-in and must never print or persist raw credentials. See
[docs/TESTING.md](docs/TESTING.md) for the complete matrix.

## Pull Requests

A reviewable pull request should include:

- the problem and why the change is needed;
- the implementation and important tradeoffs;
- exact validation commands and results;
- screenshots for visible web changes;
- safety and migration notes for memory, approvals, tools, auth, providers, or persistence;
- a changelog entry when users or operators will notice the change.

Maintainers may ask for a smaller diff, additional evidence, or a design issue before accepting a
large change. Passing CI is necessary but does not guarantee acceptance.

By submitting a contribution, you agree that it may be distributed under the repository's
[Apache License 2.0](LICENSE).
