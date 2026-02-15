"""
System Monitor Skill — CPU, memory, disk, battery, network, and process info.
Uses only stdlib (subprocess to call macOS system_profiler & sysctl).
"""

import json
import logging
import os
import subprocess
import platform

logger = logging.getLogger("libre_bird.skills.system_monitor")


def _run(cmd: list) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    return result.stdout.strip()


def tool_system_stats(args: dict) -> dict:
    """Get comprehensive system stats: CPU, memory, disk, uptime."""
    stats = {}

    # CPU info
    try:
        cpu_brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
        cpu_cores = _run(["sysctl", "-n", "hw.ncpu"])
        stats["cpu"] = {"model": cpu_brand, "cores": int(cpu_cores)}
    except Exception:
        stats["cpu"] = {"error": "unavailable"}

    # Memory
    try:
        mem_bytes = int(_run(["sysctl", "-n", "hw.memsize"]))
        mem_gb = round(mem_bytes / (1024**3), 1)

        # Parse vm_stat for usage
        vm_stat = _run(["vm_stat"])
        pages = {}
        for line in vm_stat.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                val = val.strip().rstrip(".")
                try:
                    pages[key.strip()] = int(val)
                except ValueError:
                    pass

        page_size = 16384  # Apple Silicon default
        try:
            page_size = int(_run(["sysctl", "-n", "vm.pagesize"]))
        except Exception:
            pass

        free_pages = pages.get("Pages free", 0)
        active_pages = pages.get("Pages active", 0)
        inactive_pages = pages.get("Pages inactive", 0)
        wired_pages = pages.get("Pages wired down", 0)

        used_gb = round((active_pages + wired_pages) * page_size / (1024**3), 1)
        free_gb = round((free_pages + inactive_pages) * page_size / (1024**3), 1)

        stats["memory"] = {
            "total_gb": mem_gb,
            "used_gb": used_gb,
            "available_gb": free_gb,
            "percent_used": round(used_gb / mem_gb * 100, 1) if mem_gb else 0
        }
    except Exception:
        stats["memory"] = {"error": "unavailable"}

    # Disk
    try:
        df_out = _run(["df", "-h", "/"])
        lines = df_out.split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            stats["disk"] = {
                "total": parts[1],
                "used": parts[2],
                "available": parts[3],
                "percent_used": parts[4]
            }
    except Exception:
        stats["disk"] = {"error": "unavailable"}

    # Uptime
    try:
        uptime = _run(["uptime"])
        stats["uptime"] = uptime.strip()
    except Exception:
        pass

    # macOS version
    stats["os"] = {
        "system": platform.system(),
        "version": platform.mac_ver()[0],
        "machine": platform.machine()
    }

    return stats


def tool_battery_status(args: dict) -> dict:
    """Get battery level and charging status."""
    try:
        raw = _run(["pmset", "-g", "batt"])
        lines = raw.split("\n")

        result = {"raw": raw}
        for line in lines:
            if "%" in line:
                # Parse "... 85%; charging; 0:30 remaining"
                parts = line.split("\t")
                if len(parts) >= 2:
                    info = parts[1].strip()
                    pct = ""
                    status = ""
                    remaining = ""
                    for chunk in info.split(";"):
                        chunk = chunk.strip()
                        if "%" in chunk:
                            pct = chunk
                        elif "charging" in chunk.lower() or "charged" in chunk.lower() or "discharging" in chunk.lower():
                            status = chunk
                        elif "remaining" in chunk.lower() or "present" in chunk.lower():
                            remaining = chunk
                    result = {
                        "percentage": pct,
                        "status": status,
                        "time_remaining": remaining
                    }
        return result
    except Exception as e:
        return {"error": str(e)}


def tool_top_processes(args: dict) -> dict:
    """Get top processes by CPU or memory usage."""
    sort_by = args.get("sort_by", "cpu").lower()
    count = min(int(args.get("count", 10)), 20)

    if sort_by == "memory":
        sort_flag = "-o mem"
    else:
        sort_flag = "-o cpu"

    try:
        raw = _run(["ps", "aux"])
        lines = raw.split("\n")
        header = lines[0] if lines else ""
        processes = lines[1:] if len(lines) > 1 else []

        # Parse and sort
        parsed = []
        for line in processes:
            parts = line.split(None, 10)
            if len(parts) >= 11:
                parsed.append({
                    "user": parts[0],
                    "pid": parts[1],
                    "cpu_percent": float(parts[2]),
                    "mem_percent": float(parts[3]),
                    "command": parts[10][:80]
                })

        if sort_by == "memory":
            parsed.sort(key=lambda p: p["mem_percent"], reverse=True)
        else:
            parsed.sort(key=lambda p: p["cpu_percent"], reverse=True)

        return {"processes": parsed[:count], "sort_by": sort_by}
    except Exception as e:
        return {"error": str(e)}


def tool_network_info(args: dict) -> dict:
    """Get network interface info and current connections."""
    info = {}

    # Active interface and IP
    try:
        route = _run(["route", "-n", "get", "default"])
        for line in route.split("\n"):
            line = line.strip()
            if line.startswith("interface:"):
                info["default_interface"] = line.split(":")[1].strip()
            elif line.startswith("gateway:"):
                info["gateway"] = line.split(":")[1].strip()
    except Exception:
        pass

    # IP addresses
    try:
        ifconfig = _run(["ifconfig"])
        ips = []
        current_iface = ""
        for line in ifconfig.split("\n"):
            if not line.startswith("\t") and ":" in line:
                current_iface = line.split(":")[0]
            if "inet " in line and "127.0.0.1" not in line:
                ip = line.strip().split()[1]
                ips.append({"interface": current_iface, "ip": ip})
        info["ip_addresses"] = ips
    except Exception:
        pass

    # Wi-Fi SSID
    try:
        airport = _run([
            "/System/Library/PrivateFrameworks/Apple80211.framework/Versions/Current/Resources/airport",
            "-I"
        ])
        for line in airport.split("\n"):
            line = line.strip()
            if line.startswith("SSID:"):
                info["wifi_ssid"] = line.split(":")[1].strip()
            elif line.startswith("link auth:"):
                info["wifi_security"] = line.split(":")[1].strip()
    except Exception:
        pass

    return info


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "system_stats",
            "description": "Get comprehensive system stats: CPU, memory usage, disk space, uptime, macOS version.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "battery_status",
            "description": "Get battery level, charging status, and estimated time remaining.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "top_processes",
            "description": "Get top processes sorted by CPU or memory usage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sort_by": {"type": "string", "description": "'cpu' or 'memory' (default 'cpu')"},
                    "count": {"type": "integer", "description": "Number of processes to return (default 10, max 20)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "network_info",
            "description": "Get network information: IP addresses, Wi-Fi SSID, default gateway.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
]

TOOL_HANDLERS = {
    "system_stats": tool_system_stats,
    "battery_status": tool_battery_status,
    "top_processes": tool_top_processes,
    "network_info": tool_network_info,
}
