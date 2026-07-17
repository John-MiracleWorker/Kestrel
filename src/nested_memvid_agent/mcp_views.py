from __future__ import annotations

from typing import Any

from .secret_broker import is_secret_ref


def mcp_public(server: dict[str, Any], secret_broker: Any) -> dict[str, object]:
    """Return an MCP server record with secret material replaced by status metadata."""
    safe = dict(server)
    env = dict(safe.pop("env", {}) or {})
    args = list(safe.pop("args", []) or [])
    secret_env = dict(safe.pop("secret_env", {}) or {})
    safe["env_keys"] = sorted(str(key) for key in env)
    safe["argument_count"] = len(args)
    safe["secret_env_status"] = {
        str(target): {
            "source_env": str(source),
            "secret_ref": str(source) if is_secret_ref(str(source)) else None,
            "configured": bool(secret_broker.resolve(str(source))),
            "validated": bool(secret_broker.status(str(source)).get("validated", False)),
            "last_validated_at": secret_broker.status(str(source)).get("last_validated_at"),
        }
        for target, source in sorted(secret_env.items())
    }
    return safe


def mcp_result_public(result: dict[str, Any], secret_broker: Any) -> dict[str, object]:
    """Redact an MCP operation result that embeds a server record."""
    safe = dict(result)
    if isinstance(safe.get("server"), dict):
        safe["server"] = mcp_public(dict(safe["server"]), secret_broker)
    return safe
