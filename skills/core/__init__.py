"""
Core Utilities Skill
Date/time, calculator, weather, URL opener, file search, system info, app launcher.
"""

import json
import math
import os
import platform
import shutil
import subprocess
import webbrowser
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Tool Definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "get_datetime",
            "description": "Get the current date, time, day of week, and timezone. Use this when the user asks what time or date it is.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculator",
            "description": "Evaluate a mathematical expression. Supports arithmetic, exponents, square roots, trig, etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The math expression to evaluate, e.g. '247 * 38' or 'sqrt(144) + 3**2'",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name or location, e.g. 'Detroit' or 'New York'",
                    },
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a URL in the user's default web browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to open",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": "Search for files on the Mac using Spotlight. Finds files by name or content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for finding files, e.g. 'budget spreadsheet' or 'resume.pdf'",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Optional folder to search within, e.g. '/Users/tiuni/Documents'",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_info",
            "description": "Get system information: battery level, disk space, memory usage, and uptime.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open (launch) a macOS application by name. Use this when the user asks to open an app, e.g. 'open Spotify' or 'launch Calculator'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The name of the app to open, e.g. 'Safari', 'Calculator', 'Spotify'",
                    },
                },
                "required": ["name"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_get_datetime() -> dict:
    now = datetime.now()
    return {
        "date": now.strftime("%B %d, %Y"),
        "time": now.strftime("%I:%M %p"),
        "day_of_week": now.strftime("%A"),
        "timezone": datetime.now(timezone.utc).astimezone().tzname(),
        "iso": now.isoformat(),
    }


def tool_calculator(expression: str) -> dict:
    safe_names = {
        "abs": abs, "round": round, "min": min, "max": max,
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
        "tan": math.tan, "log": math.log, "log10": math.log10,
        "log2": math.log2, "pi": math.pi, "e": math.e,
        "ceil": math.ceil, "floor": math.floor, "pow": pow,
    }
    try:
        result = eval(expression, {"__builtins__": {}}, safe_names)
        return {"expression": expression, "result": result}
    except Exception as e:
        return {"expression": expression, "error": str(e)}


def tool_get_weather(location: str) -> dict:
    import urllib.request
    import urllib.parse
    try:
        encoded = urllib.parse.quote(location)
        url = f"https://wttr.in/{encoded}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "Kestrel/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        current = data.get("current_condition", [{}])[0]
        return {
            "location": location,
            "temperature_f": current.get("temp_F", "?"),
            "temperature_c": current.get("temp_C", "?"),
            "feels_like_f": current.get("FeelsLikeF", "?"),
            "condition": current.get("weatherDesc", [{}])[0].get("value", "Unknown"),
            "humidity": current.get("humidity", "?") + "%",
            "wind_mph": current.get("windspeedMiles", "?"),
            "wind_direction": current.get("winddir16Point", "?"),
        }
    except Exception as e:
        return {"location": location, "error": f"Could not get weather: {type(e).__name__}: {str(e)}"}


def tool_open_url(url: str) -> dict:
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        webbrowser.open(url)
        return {"opened": url, "status": "success"}
    except Exception as e:
        return {"url": url, "error": str(e)}


def tool_search_files(query: str, folder: str = None) -> dict:
    try:
        cmd = ["mdfind"]
        if folder:
            cmd.extend(["-onlyin", folder])
        cmd.append(query)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        files = [f for f in result.stdout.strip().split("\n") if f][:10]
        return {
            "query": query,
            "files": files,
            "count": len(files),
            "note": "These are raw file paths from Spotlight search.",
        }
    except Exception as e:
        return {"query": query, "error": str(e)}


def tool_get_system_info() -> dict:
    info = {}
    total, used, free = shutil.disk_usage("/")
    info["disk"] = {
        "total_gb": round(total / (1024**3), 1),
        "used_gb": round(used / (1024**3), 1),
        "free_gb": round(free / (1024**3), 1),
        "percent_used": round(used / total * 100, 1),
    }
    try:
        result = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3)
        import re
        match = re.search(r"(\d+)%;\s*(\w+)", result.stdout)
        if match:
            info["battery"] = {"percent": int(match.group(1)), "status": match.group(2)}
    except Exception:
        pass
    try:
        result = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3)
        lines = result.stdout.split("\n")
        page_size = 16384
        stats = {}
        for line in lines:
            if ":" in line:
                key, val = line.split(":", 1)
                val = val.strip().rstrip(".")
                if val.isdigit():
                    stats[key.strip()] = int(val)
        active_pages = stats.get("Pages active", 0)
        wired_pages = stats.get("Pages wired down", 0)
        used_mem = (active_pages + wired_pages) * page_size
        info["memory"] = {"used_gb": round(used_mem / (1024**3), 1), "total_gb": 16}
    except Exception:
        pass
    try:
        result = subprocess.run(["uptime"], capture_output=True, text=True, timeout=3)
        info["uptime"] = result.stdout.strip()
    except Exception:
        pass
    return info


def tool_open_app(name: str) -> dict:
    try:
        result = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return {"app": name, "status": "opened"}
        else:
            return {"app": name, "error": f"Could not open: {result.stderr.strip()}"}
    except Exception as e:
        return {"app": name, "error": f"Failed to open app: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Handlers (maps tool name → callable(args_dict) → result_dict)
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "get_datetime": lambda args: tool_get_datetime(),
    "calculator": lambda args: tool_calculator(args.get("expression", "")),
    "get_weather": lambda args: tool_get_weather(args.get("location", "")),
    "open_url": lambda args: tool_open_url(args.get("url", "")),
    "search_files": lambda args: tool_search_files(args.get("query", ""), args.get("folder")),
    "get_system_info": lambda args: tool_get_system_info(),
    "open_app": lambda args: tool_open_app(args.get("name", "")),
}
