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
import subprocess

from agent.types import RiskLevel, ToolDefinition

logger = logging.getLogger("brain.agent.tools.container_control")

PROJECT_ROOT = "/project"
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID", "")

# Services defined in docker-compose.yml
KNOWN_SERVICES = {"postgres", "redis", "gateway", "brain", "hands", "frontend"}

# These services hold persistent state — never stop or restart them autonomously
PROTECTED_SERVICES = {"postgres", "redis"}


def _is_admin(user_id: str = "") -> bool:
    if not ADMIN_USER_ID:
        return True  # Single-user mode: allow all
    return user_id == ADMIN_USER_ID


def _run_compose(args: list[str], timeout: int = 60) -> dict:
    """Run a docker compose command and return structured output."""
    try:
        result = subprocess.run(
            ["docker", "compose"] + args,
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return {
                "success": False,
                "error": result.stderr.strip() or f"docker compose {' '.join(args)} failed",
                "returncode": result.returncode,
            }
        return {
            "success": True,
            "output": result.stdout.strip(),
            "stderr": result.stderr.strip() if result.stderr.strip() else None,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"Command timed out ({timeout}s)"}
    except FileNotFoundError:
        return {
            "success": False,
            "error": (
                "docker CLI not found. "
                "Ensure Docker is installed and the socket is accessible from this container."
            ),
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
                "  rebuild — rebuild a service image and restart it; use this after "
                "code changes to gateway (TypeScript) or frontend (React) since those "
                "are not hot-reloaded. Requires 30 min user inactivity.\n"
                "  stop    — stop a specific service\n"
                "  start   — start a previously stopped service\n"
                "postgres and redis are protected and cannot be stopped or restarted. "
                "Admin-only: restart, rebuild, stop, start."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["status", "logs", "restart", "rebuild", "stop", "start"],
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
        return await _container_rebuild(service)
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


async def _container_rebuild(service: str) -> dict:
    """Rebuild the image for a service and restart it."""
    err = _validate_service(service)
    if err:
        return err
    if service in PROTECTED_SERVICES:
        return {
            "error": (
                f"🛡️ '{service}' is a protected data service and cannot be rebuilt this way."
            )
        }

    # Require 30-min user inactivity (same guard as git deploy)
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

    logger.info(f"container_control: rebuilding {service}")
    result = _run_compose(["up", "--build", "-d", service], timeout=300)
    if result.get("success"):
        return {
            "message": f"✅ Rebuilt and restarted '{service}'",
            "service": service,
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
