from __future__ import annotations

from . import native_chat_tools as _native_chat_tools

globals().update({name: value for name, value in vars(_native_chat_tools).items() if not name.startswith("__")})

def sync_markdown_memory(paths: KestrelPaths, vector_store: VectorMemoryStore) -> dict[str, Any]:
    indexed = 0
    namespaces: set[str] = set()
    for file_path in sorted(paths.memory_dir.rglob("*.md")):
        if not file_path.is_file():
            continue
        relative_parent = file_path.parent.relative_to(paths.memory_dir)
        namespace = str(relative_parent).replace("\\", "/") or "root"
        doc_id = str(file_path.relative_to(paths.home)).replace("\\", "/")
        content = file_path.read_text(encoding="utf-8")
        vector_store.upsert_text(
            doc_id=doc_id,
            namespace=namespace,
            content=content,
            metadata={
                "path": doc_id,
                "mtime_ns": file_path.stat().st_mtime_ns,
            },
        )
        namespaces.add(namespace)
        indexed += 1
    return {
        "indexed_files": indexed,
        "namespaces": sorted(namespaces),
        "synced_at": _now_iso(),
    }


def build_doctor_report(
    *,
    paths: KestrelPaths,
    config: dict[str, Any],
    runtime_profile: dict[str, Any],
    model_runtime: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    checks.append(
        {
            "name": "platform",
            "status": "ok" if platform.system() == "Darwin" else "warning",
            "detail": f"Running on {platform.system()}",
        }
    )
    checks.append(
        {
            "name": "control_socket",
            "status": "ok" if control_socket_available(paths) else "warning",
            "detail": (
                f"tcp://{paths.control_host}:{paths.control_port}"
                if os.name == "nt"
                else str(paths.control_socket)
            ),
        }
    )
    checks.append(
        {
            "name": "sqlite_state",
            "status": "ok" if paths.sqlite_db.exists() else "warning",
            "detail": str(paths.sqlite_db),
        }
    )
    checks.append(
        {
            "name": "keychain",
            "status": "ok" if platform.system() == "Darwin" else "warning",
            "detail": "macOS Keychain available" if platform.system() == "Darwin" else "Keychain not available",
        }
    )
    model_ready = any(info.get("ready") for info in model_runtime.get("providers", {}).values())
    checks.append(
        {
            "name": "local_models",
            "status": "ok" if model_ready else "warning",
            "detail": (
                f"default={model_runtime.get('default_provider')}:{model_runtime.get('default_model')}"
                if model_ready
                else "No local model runtime detected"
            ),
        }
    )

    warnings = sum(1 for item in checks if item["status"] == "warning")
    errors = sum(1 for item in checks if item["status"] == "error")
    return {
        "timestamp": _now_iso(),
        "summary": {
            "warnings": warnings,
            "errors": errors,
            "healthy": errors == 0,
        },
        "checks": checks,
        "paths": {
            "home": str(paths.home),
            "control_socket": str(paths.control_socket),
            "control_tcp": f"{paths.control_host}:{paths.control_port}",
            "sqlite_db": str(paths.sqlite_db),
        },
        "runtime_profile": runtime_profile,
        "model_runtime": model_runtime,
        "permissions": config.get("permissions", {}),
    }


def install_daemon_service(
    *,
    daemon_path: str,
    python_executable: str,
    paths: KestrelPaths,
) -> dict[str, Any]:
    plat = platform.system()
    if plat == "Darwin":
        plist_dir = Path.home() / "Library" / "LaunchAgents"
        plist_dir.mkdir(parents=True, exist_ok=True)
        plist_path = plist_dir / "ai.kestrel.daemon.plist"
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>ai.kestrel.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_executable}</string>
        <string>{daemon_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>WorkingDirectory</key>
    <string>{paths.home}</string>
    <key>StandardOutPath</key>
    <string>{paths.logs_dir / "daemon.stdout.log"}</string>
    <key>StandardErrorPath</key>
    <string>{paths.logs_dir / "daemon.stderr.log"}</string>
</dict>
</plist>
"""
        plist_path.write_text(plist_content, encoding="utf-8")
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True, text=True)
        result = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "launchctl load failed")
        return {
            "manager": "launchd",
            "service_path": str(plist_path),
        }
    if plat == "Linux":
        service_dir = Path.home() / ".config" / "systemd" / "user"
        service_dir.mkdir(parents=True, exist_ok=True)
        service_path = service_dir / "kestrel-daemon.service"
        service_text = f"""[Unit]
Description=Kestrel Native Agent OS Daemon
After=network.target

[Service]
ExecStart={python_executable} {daemon_path}
WorkingDirectory={paths.home}
Restart=always
StandardOutput=append:{paths.logs_dir / "daemon.stdout.log"}
StandardError=append:{paths.logs_dir / "daemon.stderr.log"}

[Install]
WantedBy=default.target
"""
        service_path.write_text(service_text, encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", "kestrel-daemon"], check=True)
        return {
            "manager": "systemd",
            "service_path": str(service_path),
        }
    if plat == "Windows":
        task_name = "KestrelDaemon"
        command = f'"{python_executable}" "{daemon_path}"'
        result = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/F",
                "/SC",
                "ONLOGON",
                "/TN",
                task_name,
                "/TR",
                command,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "schtasks create failed")
        return {
            "manager": "scheduled-task",
            "service_path": task_name,
        }
    raise RuntimeError(f"Native daemon install is not implemented for {plat}")
