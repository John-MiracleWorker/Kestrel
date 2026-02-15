"""
Server SSH / FTP Skill
Connect to remote servers via SSH, execute commands, transfer files via SFTP.
Inspired by PyGPT's SSH/FTP plugin.

Dependencies: paramiko (auto-installed on first use).
"""

import json
import logging
import os
import subprocess
import sys
from typing import Optional, Dict

logger = logging.getLogger("libre_bird.skills.ssh_ftp")

_paramiko = None
_connections: Dict[str, object] = {}  # host:port -> SSHClient


def _ensure_paramiko():
    global _paramiko
    if _paramiko is not None:
        return _paramiko
    try:
        import paramiko
    except ImportError:
        logger.info("Installing paramiko...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "paramiko"])
        import paramiko
    _paramiko = paramiko
    return paramiko


def _get_connection(host: str, port: int = 22, username: str = "",
                    password: str = "", key_path: str = ""):
    """Get or create an SSH connection."""
    paramiko = _ensure_paramiko()
    conn_id = f"{username}@{host}:{port}"

    if conn_id in _connections:
        client = _connections[conn_id]
        if client.get_transport() and client.get_transport().is_active():
            return client
        del _connections[conn_id]

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs = {
        "hostname": host,
        "port": port,
        "username": username,
        "timeout": 10,
    }

    if key_path:
        key_path = os.path.expanduser(key_path)
        if os.path.exists(key_path):
            connect_kwargs["key_filename"] = key_path
    elif password:
        connect_kwargs["password"] = password
    else:
        # Try default SSH key
        default_key = os.path.expanduser("~/.ssh/id_rsa")
        if os.path.exists(default_key):
            connect_kwargs["key_filename"] = default_key

    client.connect(**connect_kwargs)
    _connections[conn_id] = client
    return client


def tool_ssh_connect(args: dict) -> dict:
    """Test SSH connection to a remote server."""
    host = args.get("host", "").strip()
    port = int(args.get("port", 22))
    username = args.get("username", os.environ.get("USER", "root"))
    password = args.get("password", "")
    key_path = args.get("key_path", "")

    if not host:
        return {"error": "host is required"}

    try:
        client = _get_connection(host, port, username, password, key_path)
        transport = client.get_transport()
        return {
            "success": True,
            "host": host,
            "port": port,
            "username": username,
            "server_banner": transport.remote_version if transport else "unknown",
            "message": f"Connected to {host}:{port} as {username}",
        }
    except Exception as e:
        return {"error": str(e), "hint": "Check host, port, username, and that your SSH key or password is correct."}


def tool_ssh_execute(args: dict) -> dict:
    """Execute a command on a remote server via SSH."""
    host = args.get("host", "").strip()
    command = args.get("command", "").strip()
    port = int(args.get("port", 22))
    username = args.get("username", os.environ.get("USER", "root"))
    password = args.get("password", "")
    key_path = args.get("key_path", "")
    timeout = int(args.get("timeout", 30))

    if not host:
        return {"error": "host is required"}
    if not command:
        return {"error": "command is required"}

    try:
        client = _get_connection(host, port, username, password, key_path)
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)

        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")

        # Truncate large output
        if len(out) > 10000:
            out = out[:10000] + "\n... [truncated]"
        if len(err) > 3000:
            err = err[:3000] + "\n... [truncated]"

        return {
            "host": host,
            "command": command,
            "exit_code": exit_code,
            "stdout": out,
            "stderr": err if err else None,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_sftp_upload(args: dict) -> dict:
    """Upload a local file to a remote server via SFTP."""
    host = args.get("host", "").strip()
    local_path = os.path.expanduser(args.get("local_path", ""))
    remote_path = args.get("remote_path", "")
    port = int(args.get("port", 22))
    username = args.get("username", os.environ.get("USER", "root"))
    password = args.get("password", "")
    key_path = args.get("key_path", "")

    if not host:
        return {"error": "host is required"}
    if not local_path or not os.path.exists(local_path):
        return {"error": f"Local file not found: {local_path}"}
    if not remote_path:
        remote_path = f"/tmp/{os.path.basename(local_path)}"

    try:
        client = _get_connection(host, port, username, password, key_path)
        sftp = client.open_sftp()
        sftp.put(local_path, remote_path)
        file_size = os.path.getsize(local_path)
        sftp.close()

        return {
            "success": True,
            "local_path": local_path,
            "remote_path": remote_path,
            "size_bytes": file_size,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_sftp_download(args: dict) -> dict:
    """Download a file from a remote server via SFTP."""
    host = args.get("host", "").strip()
    remote_path = args.get("remote_path", "")
    local_path = os.path.expanduser(args.get("local_path", ""))
    port = int(args.get("port", 22))
    username = args.get("username", os.environ.get("USER", "root"))
    password = args.get("password", "")
    key_path = args.get("key_path", "")

    if not host:
        return {"error": "host is required"}
    if not remote_path:
        return {"error": "remote_path is required"}
    if not local_path:
        local_path = os.path.join(os.path.expanduser("~/Downloads"), os.path.basename(remote_path))

    try:
        client = _get_connection(host, port, username, password, key_path)
        sftp = client.open_sftp()
        sftp.get(remote_path, local_path)
        file_size = os.path.getsize(local_path)
        sftp.close()

        return {
            "success": True,
            "remote_path": remote_path,
            "local_path": local_path,
            "size_bytes": file_size,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_sftp_list(args: dict) -> dict:
    """List files in a remote directory via SFTP."""
    host = args.get("host", "").strip()
    remote_path = args.get("remote_path", "/")
    port = int(args.get("port", 22))
    username = args.get("username", os.environ.get("USER", "root"))
    password = args.get("password", "")
    key_path = args.get("key_path", "")

    if not host:
        return {"error": "host is required"}

    try:
        client = _get_connection(host, port, username, password, key_path)
        sftp = client.open_sftp()
        entries = sftp.listdir_attr(remote_path)

        files = []
        for entry in entries[:50]:  # Cap at 50
            import stat as stat_mod
            is_dir = stat_mod.S_ISDIR(entry.st_mode) if entry.st_mode else False
            files.append({
                "name": entry.filename,
                "size": entry.st_size,
                "is_directory": is_dir,
                "modified": str(entry.st_mtime),
            })
        sftp.close()

        return {"path": remote_path, "files": files, "count": len(files)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "ssh_connect",
            "description": "Test an SSH connection to a remote server. Uses SSH keys by default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP address"},
                    "port": {"type": "integer", "description": "SSH port (default 22)"},
                    "username": {"type": "string", "description": "SSH username"},
                    "password": {"type": "string", "description": "SSH password (if not using key)"},
                    "key_path": {"type": "string", "description": "Path to SSH private key (default ~/.ssh/id_rsa)"},
                },
                "required": ["host"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_execute",
            "description": "Execute a command on a remote server via SSH and return stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "port": {"type": "integer", "description": "SSH port (default 22)"},
                    "username": {"type": "string", "description": "SSH username"},
                    "password": {"type": "string", "description": "SSH password"},
                    "key_path": {"type": "string", "description": "Path to SSH private key"},
                    "timeout": {"type": "integer", "description": "Command timeout in seconds (default 30)"},
                },
                "required": ["host", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sftp_upload",
            "description": "Upload a local file to a remote server via SFTP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "local_path": {"type": "string", "description": "Path to local file"},
                    "remote_path": {"type": "string", "description": "Destination path on server"},
                    "port": {"type": "integer", "description": "SSH port (default 22)"},
                    "username": {"type": "string", "description": "SSH username"},
                    "password": {"type": "string", "description": "SSH password"},
                    "key_path": {"type": "string", "description": "Path to SSH private key"},
                },
                "required": ["host", "local_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sftp_download",
            "description": "Download a file from a remote server via SFTP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "remote_path": {"type": "string", "description": "Path to file on server"},
                    "local_path": {"type": "string", "description": "Local destination path (default ~/Downloads/)"},
                    "port": {"type": "integer", "description": "SSH port (default 22)"},
                    "username": {"type": "string", "description": "SSH username"},
                    "password": {"type": "string", "description": "SSH password"},
                    "key_path": {"type": "string", "description": "Path to SSH private key"},
                },
                "required": ["host", "remote_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sftp_list",
            "description": "List files in a remote directory via SFTP.",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Hostname or IP"},
                    "remote_path": {"type": "string", "description": "Directory to list (default /)"},
                    "port": {"type": "integer", "description": "SSH port (default 22)"},
                    "username": {"type": "string", "description": "SSH username"},
                    "password": {"type": "string", "description": "SSH password"},
                    "key_path": {"type": "string", "description": "Path to SSH private key"},
                },
                "required": ["host"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "ssh_connect": tool_ssh_connect,
    "ssh_execute": tool_ssh_execute,
    "sftp_upload": tool_sftp_upload,
    "sftp_download": tool_sftp_download,
    "sftp_list": tool_sftp_list,
}
