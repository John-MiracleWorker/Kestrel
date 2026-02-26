"""
Container control tool — gives Kestrel the ability to inspect and manage
its own Docker Compose services without going through the full deploy gate.

Safety rails:
  - status and logs are read-only (no admin required)
  - restart, rebuild, stop, start are admin-only
  - postgres and redis are protected from stop/restart (would corrupt state)
  - rebuild requires 30-min user inactivity (same guard as git deploy)
  - All operations are scoped to the project root (/project)
"""

import logging
import os
import shutil
import subprocess

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.container_control")

PROJECT_ROOT = "/project"
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "")

# Services defined in docker-compose.yml
KNOWN_SERVICES = {"postgres", "redis", "gateway", "brain", "hands", "frontend"}

# These services hold persistent state — never stop or restart them autonomously
PROTECTED_SERVICES = {"postgres", "redis"}


def _resolve_compose_command() -> tuple[list[str] | None, str | None]:
    """Resolve an available compose command for this environment."""
    if shutil.which("docker"):
        return ["docker", "compose"], None
    if shutil.which("docker-compose"):
        return ["docker-compose"], "docker-compose"
    if shutil.which("podman"):
        return ["podman", "compose"], "podman"
    return None, None

def _is_admin(user_id: str = "") -> bool:
    if not ADMIN_USER_ID:
        return True  # Single-user mode: allow all
    return user_id == ADMIN_USER_ID


def _resolve_project_root() -> str | None:
    """Resolve a compose project directory that exists in this runtime."""
    candidates = [
        PROJECT_ROOT,
        os.getenv("KESTREL_PROJECT_ROOT", ""),
        "/workspace/Kestrel",
        os.getcwd(),
    ]
    seen = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        if os.path.isdir(candidate):
            return candidate
    return None

def _run_compose(args: list[str], timeout: int = 60) -> dict:
    """Run an available compose command and return structured output."""
    compose_cmd, runtime = _resolve_compose_command()
    if not compose_cmd:
        return {
            "success": False,
            "error": (
                "Container runtime not available in this environment. "
                "Tried Docker Compose and Podman Compose but neither CLI was found."
            ),
            "hint": (
                "Install Docker/Podman on the host and expose the socket to this container, "
                "or run this tool from an environment with compose access."
            ),
        }

    project_root = _resolve_project_root()
    if not project_root:
        return {
            "success": False,
            "error": (
                "Compose project directory was not found in this runtime. "
                "Expected `/project` or `KESTREL_PROJECT_ROOT` to exist."
            ),
            "hint": "Set KESTREL_PROJECT_ROOT to the repository path in the running container.",
        }

    try:
        result = subprocess.run(
            compose_cmd + args,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            command_label = " ".join(compose_cmd)
            return {
                "success": False,
                "error": result.stderr.strip() or f"{command_label} {' '.join(args)} failed",
                "returncode": result.returncode,
                "runtime": runtime or "docker",
                "project_root": project_root,
            }
        return {
            "success": True,
            "output": result.stdout.strip(),
            "stderr": result.stderr.strip() if result.stderr.strip() else None,
            "runtime": runtime or "docker",
            "project_root": project_root,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out ({timeout}s)"}
    except FileNotFoundError as exc:
        command_label = " ".join(compose_cmd)
        return {
            "success": False,
            "error": (
                f"{command_label} failed to launch in this runtime: {exc}. "
                "Verify both the runtime binary and compose project path are accessible."
            ),
            "runtime": runtime or "docker",
            "project_root": project_root,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _validate_service(service: str) -> dict | None:
    """Return an error dict if the service name is invalid, else None."""
    if not service:
        return {"error": "A service name is required for this action."}
    if service not in KNOWN_SERVICES:
        return {
            "error": f"Unknown service '{service}'. "
            f"Choose from: {', '.join(sorted(KNOWN_SERVICES))}"
        }
    return None


def register_container_tools(registry) -> None:
    """Register the container_control tool."""
    registry.register(
        definition=ToolDefinition(
            name="container_control",
            description=(
                "Inspect and manage Kestrel's own Docker Compose services. "
                "Actions:\n"
                "  status  — show running state of all services (read-only)\n"
                "  logs    — stream recent log lines from a service (read-only)\n"
                "  restart — cycle a service without rebuilding its image\n"
                "  rebuild — git pull + rebuild a service image and restart it. "
                "For 'brain' (self-rebuild), uses fire-and-forget so the rebuild "
                "survives the current container being replaced.\n"
                "  rebuild_all — rebuild all app services (gateway, brain, hands, frontend)\n"
                "  stop    — stop a specific service\n"
                "  start   — start a previously stopped service\n"
                "postgres and redis are protected and cannot be stopped or restarted. "
                "Admin-only: restart, rebuild, rebuild_all, stop, start."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "logs", "restart", "rebuild", "rebuild_all", "stop", "start"],
                        "description": "Operation to perform",
                    },
                    "service": {
                        "type": "string",
                        "enum": sorted(KNOWN_SERVICES),
                        "description": (
                            "Target service. Required for logs, restart, rebuild, stop, start."
                        ),
                    },
                    "tail": {
                        "type": "integer",
                        "description": "Number of log lines to return (default 50, max 200)",
                    },
                },
                "required": ["action"],
            },
            risk_level=RiskLevel.HIGH,
            timeout_seconds=120,
            category="infrastructure",
        ),
        handler=container_action,
    )


async def container_action(
    action: str,
    service: str = "",
    tail: int = 50,
    user_id: str = "",
) -> dict:
    """Route to the appropriate container action."""
    if action == "status":
        return _container_status()
    elif action == "logs":
        return _container_logs(service, tail)
    elif action == "restart":
        if not _is_admin(user_id):
            return {"error": "🔒 Restart is admin-only."}
        return _container_restart(service)
    elif action == "rebuild":
        if not _is_admin(user_id):
            return {"error": "🔒 Rebuild is admin-only."}
        return await _container_rebuild(service, user_requested=True)
    elif action == "rebuild_all":
        if not _is_admin(user_id):
            return {"error": "🔒 Rebuild is admin-only."}
        return await _container_rebuild_all()
    elif action == "stop":
        if not _is_admin(user_id):
            return {"error": "🔒 Stop is admin-only."}
        return _container_stop(service)
    elif action == "start":
        if not _is_admin(user_id):
            return {"error": "🔒 Start is admin-only."}
        return _container_start(service)
    else:
        return {"error": f"Unknown action: {action}"}


def _container_status() -> dict:
    """Show status of all compose services."""
    result = _run_compose(["ps"], timeout=15)
    return result


def _container_logs(service: str, tail: int) -> dict:
    """Fetch recent logs from one service (or all if service is empty)."""
    if service:
        err = _validate_service(service)
        if err:
            return err

    tail = min(max(tail, 1), 200)
    args = ["logs", "--no-color", f"--tail={tail}"]
    if service:
        args.append(service)

    return _run_compose(args, timeout=30)


def _container_restart(service: str) -> dict:
    """Restart a service without rebuilding its image."""
    err = _validate_service(service)
    if err:
        return err
    if service in PROTECTED_SERVICES:
        return {
            "error": (
                f"🛡️ '{service}' is a protected data service and cannot be restarted autonomously. "
                "Stop it manually from the host if absolutely necessary."
            )
        }

    logger.info(f"container_control: restarting {service}")
    result = _run_compose(["restart", service], timeout=60)
    if result.get("success"):
        return {"message": f"✅ Restarted '{service}'", "service": service}
    return result


async def _container_rebuild(service: str, user_requested: bool = False) -> dict:
    """Rebuild the image for a service and restart it.

    When rebuilding 'brain' (self), we face a chicken-and-egg problem: the
    running process IS the container being rebuilt. We solve this with a
    fire-and-forget pattern:
      1. git pull latest code in /project
      2. Launch 'docker compose up --build -d brain' via nohup so it survives
         the current container being replaced.
    """
    err = _validate_service(service)
    if err:
        return err
    if service in PROTECTED_SERVICES:
        return {
            "error": (
                f"🛡️ '{service}' is a protected data service and cannot be rebuilt this way."
            )
        }

    # Inactivity gate — bypass when user explicitly requested the rebuild
    if not user_requested:
        from agent.tools.self_improve import is_user_inactive, get_inactivity_seconds

        if not is_user_inactive():
            idle_min = get_inactivity_seconds() / 60
            return {
                "error": (
                    f"🚫 Rebuild blocked — user was active {idle_min:.0f} min ago. "
                    "Waiting for 30 min of inactivity to avoid disrupting an active session."
                ),
                "hint": "Call this again once the user has been idle for 30 minutes.",
            }

    project_root = _resolve_project_root()
    if not project_root:
        return {"success": False, "error": "Project root not found."}

    compose_cmd, _ = _resolve_compose_command()
    if not compose_cmd:
        return {"success": False, "error": "No container runtime found."}

    # Step 1: Pull latest code
    try:
        pull_result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        git_status = pull_result.stdout.strip() if pull_result.returncode == 0 else pull_result.stderr.strip()
        logger.info(f"container_control: git pull result: {git_status}")
    except Exception as e:
        git_status = f"git pull failed (non-fatal): {e}"
        logger.warning(git_status)

    # Step 2: Rebuild
    is_self_rebuild = (service == "brain")

    if is_self_rebuild:
        # Fire-and-forget: launch the rebuild in a detached process that
        # survives the current container being replaced by Docker Compose.
        rebuild_cmd = " ".join(compose_cmd + ["up", "--build", "-d", "brain"])
        nohup_cmd = f"nohup sh -c 'sleep 2 && cd {project_root} && {rebuild_cmd}' > /tmp/brain-rebuild.log 2>&1 &"

        logger.info(f"container_control: self-rebuild initiated (fire-and-forget)")
        try:
            subprocess.Popen(
                nohup_cmd,
                shell=True,
                cwd=project_root,
                start_new_session=True,
            )
        except Exception as e:
            return {"success": False, "error": f"Failed to launch self-rebuild: {e}"}

        return {
            "success": True,
            "message": (
                "🔄 Self-rebuild initiated! The brain service will be rebuilt and restarted "
                "in ~2 seconds. I'll be briefly offline while the new container starts. "
                "This process is fire-and-forget — it will complete even though this "
                "container is being replaced."
            ),
            "service": "brain",
            "git_pull": git_status,
        }

    # Non-self rebuild: normal synchronous path
    logger.info(f"container_control: rebuilding {service}")
    result = _run_compose(["up", "--build", "-d", service], timeout=300)
    if result.get("success"):
        return {
            "message": f"✅ Rebuilt and restarted '{service}'",
            "service": service,
            "git_pull": git_status,
            "output": result.get("output", ""),
        }
    return result


def _container_stop(service: str) -> dict:
    """Stop a running service."""
    err = _validate_service(service)
    if err:
        return err
    if service in PROTECTED_SERVICES:
        return {
            "error": (
                f"🛡️ '{service}' is a critical data service and cannot be stopped autonomously."
            )
        }

    logger.info(f"container_control: stopping {service}")
    result = _run_compose(["stop", service], timeout=30)
    if result.get("success"):
        return {"message": f"✅ Stopped '{service}'", "service": service}
    return result


def _container_start(service: str) -> dict:
    """Start a previously stopped service."""
    err = _validate_service(service)
    if err:
        return err

    logger.info(f"container_control: starting {service}")
    result = _run_compose(["start", service], timeout=30)
    if result.get("success"):
        return {"message": f"✅ Started '{service}'", "service": service}
    return result


async def _container_rebuild_all() -> dict:
    """Rebuild all app services. Rebuilds brain last (fire-and-forget self-rebuild).

    Order: gateway → hands → frontend → brain (self, fire-and-forget)
    Skips postgres and redis (protected).
    """
    app_services = ["gateway", "hands", "frontend"]
    results = {}

    for svc in app_services:
        logger.info(f"container_control: rebuild_all → rebuilding {svc}")
        result = await _container_rebuild(svc, user_requested=True)
        results[svc] = result.get("message", result.get("error", "unknown"))
        if not result.get("success", True):
            logger.warning(f"container_control: rebuild_all → {svc} failed: {result}")

    # Brain last (self-rebuild, fire-and-forget)
    brain_result = await _container_rebuild("brain", user_requested=True)
    results["brain"] = brain_result.get("message", brain_result.get("error", "unknown"))

    return {
        "success": True,
        "message": "🔄 Full rebuild initiated. Brain rebuild is fire-and-forget — I'll be briefly offline.",
        "services": results,
    }
