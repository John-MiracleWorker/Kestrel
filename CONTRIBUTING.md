# Contributing To Kestrel

Kestrel is a multi-service monorepo. Good changes are small, explicit about boundaries, and validated against the package or runtime surface they touch.

## Before You Change Code

1. Read the current setup and capability docs before changing behavior:
    - `README.md`
    - `SETUP.md`
    - `docs/platform-capabilities.md`
    - `docs/service-ownership.md`
2. Pick the narrowest package surface that matches the change.
3. If you are changing runtime behavior, confirm whether the change affects Docker-first, native or hybrid startup, or the CLI path.

## Development Environment

Recommended baseline:

- Node.js `18+`
- npm `9+`
- Python `3.11+`
- Docker
- PostgreSQL `16`
- Redis `7`

Install workspace dependencies:

```bash
npm install
python3 -m venv venv
venv/bin/python -m pip install -r packages/brain/requirements.txt
venv/bin/python -m pip install -r packages/hands/requirements.txt
```

## Architectural Boundaries

Keep service ownership clean:

- `Gateway` should own ingress, auth context, routing, delivery semantics, and webhooks.
- `Brain` should own reasoning, memory, policy, providers, approvals, and task lifecycle.
- `Hands` should own explicit execution, sandbox policy, artifacts, and audit output.
- `Web` and `Desktop` should render state and user workflows without taking on backend business rules.
- `CLI` should stay aligned with the native companion and local runtime model.

If a change crosses service boundaries, document why and update the affected docs.

## Validation

Run the narrowest checks that prove the change:

```bash
npm run test
npm run typecheck
npm run lint
```

Common package-level checks:

```bash
npm run test --workspace=@kestrel/gateway
npm run typecheck --workspace=@kestrel/gateway
npm run typecheck --workspace=@kestrel/web
cd packages/brain && ../../venv/bin/python -m pytest tests -v
cd packages/hands && ../../venv/bin/python -m pytest tests -v
```

For documentation-only changes, verify links, commands, and paths against the repository layout.

## Pull Requests

Each PR should include:

- A clear summary of what changed
- The package or runtime surfaces affected
- Validation that was run, or an explicit note if no automated checks were needed
- Screenshots, logs, or CLI output when the change affects UX or runtime behavior
- Follow-up work called out separately instead of being hidden inside the PR

Keep PRs focused. Avoid bundling unrelated refactors with behavior changes.

## Documentation Changes

If you change any of the following, keep the public docs in sync:

- Startup commands
- Ports, environment variables, or package names
- Runtime modes
- Feature maturity or supported channels
- Security or contribution expectations

At minimum, re-check `README.md`, `SETUP.md`, and the relevant file under `docs/`.
