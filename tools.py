"""
Libre Bird Tool System
Provides utility tools the LLM can invoke via native function calling.
"""
import json
import logging
import math
import os
import platform
import shutil
import subprocess
import webbrowser
from datetime import datetime, timezone

logger = logging.getLogger("libre_bird.tools")


# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
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
            "name": "web_search",
            "description": "Search the web for information. Use this for looking things up, light research, current events, or when you don't know something.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query",
                    },
                },
                "required": ["query"],
            },
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
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_get_datetime() -> dict:
    """Return current date, time, day of week, and timezone."""
    now = datetime.now()
    return {
        "date": now.strftime("%B %d, %Y"),
        "time": now.strftime("%I:%M %p"),
        "day_of_week": now.strftime("%A"),
        "timezone": datetime.now(timezone.utc).astimezone().tzname(),
        "iso": now.isoformat(),
    }


def tool_web_search(query: str) -> dict:
    """Search the web using DuckDuckGo."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        if not results:
            return {"results": [], "message": "No results found."}
        return {
            "results": [
                {
                    "title": r.get("title", ""),
                    "snippet": r.get("body", ""),
                    "url": r.get("href", ""),
                }
                for r in results
            ]
        }
    except ImportError:
        return {"error": "Search package not installed. Run: pip install ddgs"}
    except Exception as e:
        return {"error": f"Search failed: {str(e)}"}


def tool_calculator(expression: str) -> dict:
    """Safely evaluate a math expression."""
    # Whitelist of safe names
    safe_names = {
        "abs": abs, "round": round, "min": min, "max": max,
        "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos,
        "tan": math.tan, "log": math.log, "log10": math.log10,
        "log2": math.log2, "pi": math.pi, "e": math.e,
        "ceil": math.ceil, "floor": math.floor, "pow": pow,
    }
    try:
        # Only allow safe math operations
        result = eval(expression, {"__builtins__": {}}, safe_names)
        return {"expression": expression, "result": result}
    except Exception as e:
        return {"expression": expression, "error": str(e)}


def tool_get_weather(location: str) -> dict:
    """Get weather from wttr.in (no API key needed)."""
    import urllib.request
    import urllib.parse
    try:
        encoded = urllib.parse.quote(location)
        url = f"https://wttr.in/{encoded}?format=j1"
        req = urllib.request.Request(url, headers={"User-Agent": "LibreBird/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
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
        return {"location": location, "error": f"Could not get weather: {str(e)}"}


def tool_open_url(url: str) -> dict:
    """Open a URL in the default browser."""
    try:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        webbrowser.open(url)
        return {"opened": url, "status": "success"}
    except Exception as e:
        return {"url": url, "error": str(e)}


def tool_search_files(query: str, folder: str = None) -> dict:
    """Search for files using macOS Spotlight (mdfind)."""
    try:
        cmd = ["mdfind"]
        if folder:
            cmd.extend(["-onlyin", folder])
        cmd.append(query)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        files = [f for f in result.stdout.strip().split("\n") if f][:10]  # Limit to 10
        return {"query": query, "files": files, "count": len(files)}
    except Exception as e:
        return {"query": query, "error": str(e)}


def tool_get_system_info() -> dict:
    """Get system information: battery, disk, memory."""
    info = {}

    # Disk space
    total, used, free = shutil.disk_usage("/")
    info["disk"] = {
        "total_gb": round(total / (1024**3), 1),
        "used_gb": round(used / (1024**3), 1),
        "free_gb": round(free / (1024**3), 1),
        "percent_used": round(used / total * 100, 1),
    }

    # Battery (macOS)
    try:
        result = subprocess.run(
            ["pmset", "-g", "batt"], capture_output=True, text=True, timeout=3
        )
        output = result.stdout
        # Parse "100%; charged" or "85%; discharging"
        import re
        match = re.search(r"(\d+)%;\s*(\w+)", output)
        if match:
            info["battery"] = {
                "percent": int(match.group(1)),
                "status": match.group(2),
            }
    except Exception:
        pass

    # Memory (macOS)
    try:
        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=3
        )
        lines = result.stdout.split("\n")
        page_size = 16384  # Apple Silicon default
        stats = {}
        for line in lines:
            if ":" in line:
                key, val = line.split(":", 1)
                val = val.strip().rstrip(".")
                if val.isdigit():
                    stats[key.strip()] = int(val)
        free_pages = stats.get("Pages free", 0)
        active_pages = stats.get("Pages active", 0)
        inactive_pages = stats.get("Pages inactive", 0)
        wired_pages = stats.get("Pages wired down", 0)
        used_mem = (active_pages + wired_pages) * page_size
        info["memory"] = {
            "used_gb": round(used_mem / (1024**3), 1),
            "total_gb": 16,  # Known hardware
        }
    except Exception:
        pass

    # Uptime
    try:
        result = subprocess.run(
            ["uptime"], capture_output=True, text=True, timeout=3
        )
        info["uptime"] = result.stdout.strip()
    except Exception:
        pass

    return info


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

_TOOL_REGISTRY = {
    "get_datetime": lambda args: tool_get_datetime(),
    "web_search": lambda args: tool_web_search(args.get("query", "")),
    "calculator": lambda args: tool_calculator(args.get("expression", "")),
    "get_weather": lambda args: tool_get_weather(args.get("location", "")),
    "open_url": lambda args: tool_open_url(args.get("url", "")),
    "search_files": lambda args: tool_search_files(
        args.get("query", ""), args.get("folder")
    ),
    "get_system_info": lambda args: tool_get_system_info(),
}


def execute_tool(name: str, arguments: dict) -> str:
    """Execute a tool by name and return the JSON result."""
    handler = _TOOL_REGISTRY.get(name)
    if not handler:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        logger.info(f"Executing tool: {name}({arguments})")
        result = handler(arguments)
        logger.info(f"Tool result: {json.dumps(result)[:200]}")
        return json.dumps(result)
    except Exception as e:
        logger.error(f"Tool {name} failed: {e}")
        return json.dumps({"error": f"Tool failed: {str(e)}"})
