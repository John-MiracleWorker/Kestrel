# Desktop-first startup profile (native/hybrid)

This guide adds a **desktop-first** path for Kestrel while preserving Docker compatibility.

## What this profile does

- Selects native runtime mode metadata (`KESTREL_RUNTIME_MODE=native`).
- Enables `screen-agent` by default (host-run process).
- Keeps Docker-heavy subsystems optional via `KESTREL_ENABLE_DOCKER_SUBSYSTEMS` + `KESTREL_DOCKER_SUBSYSTEMS`.
- Defaults host-native write/exec tools to **disabled** until policy is configured:
    - `KESTREL_ENABLE_HOST_WRITE=false`
    - `KESTREL_ENABLE_HOST_EXEC=false`

The profile files are:

- `config/startup/native-hybrid.env.example`
- `scripts/startup/native-hybrid.sh`

## Quick start

```bash
cp config/startup/native-hybrid.env.example config/startup/native-hybrid.env
./scripts/startup/native-hybrid.sh check
./scripts/startup/native-hybrid.sh up
```

## Safety model for native write/exec tools

Native write/exec registration in the Brain now follows startup-policy flags:

- `KESTREL_ENABLE_HOST_WRITE=true` registers `host_write`.
- `KESTREL_ENABLE_HOST_EXEC=true` registers `host_shell` and `host_python`.
- If either flag is true, `KESTREL_NATIVE_POLICY_FILE` must exist.

Recommended policy location:

```text
~/.kestrel/native-tools-policy.yml
```

Example allowlist seed:

```yaml
allowed_commands:
    - '^git status$'
    - '^git diff'
    - '^npm run test'
```

## Preflight checks performed by startup script

`./scripts/startup/native-hybrid.sh check` validates:

1. Host dependencies: `python3`, `node`, `npm` (and `docker` when hybrid mode enabled).
2. Version guards: minimum Python and Node major versions from the profile.
3. Policy requirements when host write/exec are enabled.
4. Repo writability and host permission reminders for screen automation.

## Migration from Docker-first to desktop-first

### 1) Keep your existing Docker flow intact

Your current Docker-first command remains valid:

```bash
docker compose up -d --build
```

### 2) Introduce the desktop-first profile

- Copy `config/startup/native-hybrid.env.example` to `config/startup/native-hybrid.env`.
- Keep default safe flags (`KESTREL_ENABLE_HOST_WRITE=false`, `KESTREL_ENABLE_HOST_EXEC=false`).

### 3) Choose runtime style per machine

- **Pure native desktop:** set `KESTREL_ENABLE_DOCKER_SUBSYSTEMS=false`.
- **Hybrid desktop:** keep `KESTREL_ENABLE_DOCKER_SUBSYSTEMS=true` and run only heavy subsystems (default: `postgres,redis,hands`).

### 4) Enable native write/exec intentionally

Only after you establish your policy file:

- Create `~/.kestrel/native-tools-policy.yml`.
- Set `KESTREL_ENABLE_HOST_WRITE=true` and/or `KESTREL_ENABLE_HOST_EXEC=true`.
- Re-run `./scripts/startup/native-hybrid.sh check`.

## Compatibility matrix

| OS                      | Native core services (brain/gateway/web) | Screen-agent                        | Hybrid Docker subsystems          | Notes                                                                                         |
| ----------------------- | ---------------------------------------- | ----------------------------------- | --------------------------------- | --------------------------------------------------------------------------------------------- |
| macOS                   | ✅ Supported                             | ✅ Best supported                   | ✅ Supported                      | Grant Screen Recording + Accessibility permissions to terminal/app running `screen-agent`.    |
| Linux (desktop session) | ✅ Supported                             | ⚠️ Supported with environment setup | ✅ Supported                      | Requires GUI session and compatible screenshot/input backends for `pyautogui`/display server. |
| Windows (native shell)  | ⚠️ Experimental                          | ⚠️ Experimental                     | ✅ Recommended via Docker Desktop | Prefer WSL2 or Docker-backed hybrid mode for predictable service orchestration.               |

## Operational recommendation

For teams migrating gradually, start with **hybrid mode** (`postgres,redis,hands` in Docker; brain/gateway/web/screen-agent native), then disable Docker subsystems one-by-one once host dependencies and policies are stable.
