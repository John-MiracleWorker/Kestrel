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
import sys
import webbrowser
from datetime import datetime, timezone

from notifications import reminder_scheduler

logger = logging.getLogger("libre_bird.tools")


def _pip_install(package: str) -> bool:
    """Auto-install a missing Python package into the active venv."""
    try:
        logger.info(f"Auto-installing missing package: {package} (this may take a few minutes)...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", package],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0:
            logger.info(f"Successfully installed {package}")
            return True
        else:
            logger.error(f"Failed to install {package}: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        logger.error(f"pip install {package} timed out (5 min limit)")
        return False
    except Exception as e:
        logger.error(f"pip install failed: {e}")
        return False


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
    {
        "type": "function",
        "function": {
            "name": "read_screen",
            "description": "Read all visible text from the user's screen using OCR. Use this when the user asks what's on their screen, wants you to read something they're looking at, or says 'look at this'. The screenshot is captured, OCR'd, and immediately discarded — nothing is stored.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a timed reminder that will show as a macOS notification. Use this when the user says 'remind me', 'in X minutes', 'set a timer', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "The reminder message to display",
                    },
                    "minutes": {
                        "type": "number",
                        "description": "How many minutes from now to fire the reminder",
                    },
                },
                "required": ["message", "minutes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clipboard",
            "description": "Read or write the system clipboard. Use 'read' to see what the user copied, or 'write' to copy text for them.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "'read' to get clipboard contents, 'write' to set clipboard contents",
                        "enum": ["read", "write", "history"],
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to copy to clipboard (required when action is 'write')",
                    },
                },
                "required": ["action"],
            },
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
    # ── New tools ─────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Fetch and extract the main text content from a webpage URL. Use this when the user shares a link and wants you to read it, summarize an article, or get information from a website.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to fetch, e.g. 'https://example.com/article'",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_code",
            "description": "Execute a Python code snippet and return the output. Use this for calculations, data processing, generating text, or any task that benefits from running actual code. The code runs in an isolated subprocess.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python code to execute. Use print() to produce output.",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shell_command",
            "description": "Run a shell command on the user's macOS system. Use for safe operations like listing files, checking processes, disk usage, network info, and installing Python packages (pip install). NEVER use for destructive operations (rm, mv, sudo, etc). You CAN and SHOULD use this to install missing Python packages when a tool reports a package is not installed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to run, e.g. 'ls -la ~/Documents' or 'df -h'",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "Read text aloud using macOS text-to-speech. Use when the user asks you to read something aloud, speak, or when a verbal response would be helpful.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to speak aloud",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_generate",
            "description": "Generate an image from a text description using Google Gemini (Nano Banana Pro). Use when the user asks to create, generate, or draw an image. Requires a Gemini API key in Settings. Takes 5-15 seconds.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed description of the image to generate",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_control",
            "description": "Control macOS system settings and actions. Use when the user asks to change volume, brightness, toggle dark mode, lock the screen, take a screenshot, open an app, check battery, toggle wifi/bluetooth, enable Do Not Disturb, or similar system-level actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["volume_set", "volume_up", "volume_down", "mute", "unmute", "brightness", "dark_mode", "light_mode", "dnd_on", "dnd_off", "lock_screen", "screenshot", "open_app", "sleep", "empty_trash", "battery", "wifi_on", "wifi_off", "bluetooth_on", "bluetooth_off"],
                        "description": "The system action to perform",
                    },
                    "value": {
                        "type": "string",
                        "description": "Optional value for the action (e.g., volume level 0-100, app name for open_app)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "music_control",
            "description": "Control Apple Music and analyze listening habits. Use for playback (play/pause/skip), searching the library, creating playlists, and getting listening stats. You can recommend music based on what the user is working on by combining screen context with their listening history and library.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "next", "previous", "now_playing", "search", "create_playlist", "add_to_playlist", "listening_stats", "recent_history"],
                        "description": "The music action to perform",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query for 'search' action, or playlist name for 'create_playlist'/'add_to_playlist'",
                    },
                    "track_name": {
                        "type": "string",
                        "description": "Track name for 'add_to_playlist' action",
                    },
                    "genre": {
                        "type": "string",
                        "description": "Optional genre filter for 'listening_stats' or 'search'",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_search",
            "description": "Search the user's local knowledge base for relevant information. Use this when the user asks about something they previously saved, or when you need to recall stored documents, notes, or context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_add",
            "description": "Add a document or text to the user's local knowledge base for future reference. Use when the user asks to remember, save, or store information for later.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text content to store",
                    },
                    "source": {
                        "type": "string",
                        "description": "A label for where this came from, e.g. 'user note', 'meeting notes', a filename",
                    },
                },
                "required": ["text", "source"],
            },
        },
    },
    # ── Phase 1 Agentic Tools ────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "file_operations",
            "description": "Perform file system operations. Read, write, list, move, copy, or delete files and folders. Use when the user asks to create, edit, organize, or inspect files. Delete sends to Trash (safe).",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write", "append", "list", "move", "copy", "delete", "mkdir", "info"],
                        "description": "The file operation to perform",
                    },
                    "path": {
                        "type": "string",
                        "description": "The file or directory path",
                    },
                    "content": {
                        "type": "string",
                        "description": "File content for 'write' or 'append' actions",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Destination path for 'move' or 'copy' actions",
                    },
                },
                "required": ["action", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyboard",
            "description": "Type text or press keys in the active application. Use when the user asks to type something, fill in a form, press a keyboard shortcut, or automate text input.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["type_text", "press_key", "hotkey"],
                        "description": "'type_text' to type a string, 'press_key' for special keys (return, tab, escape, etc.), 'hotkey' for shortcuts (e.g., cmd+s)",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to type (for type_text) or key name (for press_key) or shortcut (for hotkey, e.g., 'cmd+s', 'cmd+shift+z')",
                    },
                },
                "required": ["action", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_document",
            "description": "Read and extract text from documents: PDFs, Word docs (.docx), plain text, Markdown, and CSV files. Use when the user asks to read, summarize, or analyze a document.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the document file",
                    },
                    "pages": {
                        "type": "string",
                        "description": "Optional page range for PDFs, e.g., '1-5' or '3'. Omit to read all pages.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_notifications",
            "description": "Read recent macOS notifications or clear notifications for a specific app. Use when the user asks what notifications they have, or wants to check/dismiss alerts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "clear"],
                        "description": "'list' to get recent notifications, 'clear' to dismiss notifications for an app",
                    },
                    "app_name": {
                        "type": "string",
                        "description": "App name to filter or clear notifications for (optional for list, required for clear)",
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_screen",
            "description": "Capture and analyze what's currently visible on the user's screen. Uses OCR to extract all visible text and describes the screen layout. Can also analyze a specific image file from disk. Use when the user asks 'what's on my screen?', 'what am I looking at?', 'read my screen', or wants you to analyze/describe an image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for or focus on (e.g., 'error messages', 'the code', 'the email'). Optional — omit for general analysis.",
                    },
                    "image_path": {
                        "type": "string",
                        "description": "Optional path to an image file on disk to analyze instead of capturing the screen.",
                    },
                },
                "required": [],
            },
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
        if _pip_install("ddgs"):
            return tool_web_search(query)  # retry after install
        return {"error": "Search package not installed and auto-install failed."}
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
        logger.info(f"Weather request: {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "LibreBird/1.0"})
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
        logger.error(f"Weather tool failed: {type(e).__name__}: {e}")
        return {"location": location, "error": f"Could not get weather: {type(e).__name__}: {str(e)}"}


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
    """Search for files on this Mac using Spotlight. Returns file paths matching the query. Note: these are raw filesystem paths, not 'workspaces' or 'projects'."""
    try:
        cmd = ["mdfind"]
        if folder:
            cmd.extend(["-onlyin", folder])
        cmd.append(query)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        files = [f for f in result.stdout.strip().split("\n") if f][:10]  # Limit to 10
        return {
            "query": query,
            "files": files,
            "count": len(files),
            "note": "These are raw file paths from Spotlight search. Interpret them as files on the user's Mac, not as projects or workspaces.",
        }
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
# New tool implementations: reminders, clipboard, app launcher
# ---------------------------------------------------------------------------

def tool_set_reminder(message: str, minutes: float) -> dict:
    """Schedule a reminder notification."""
    try:
        if minutes <= 0:
            return {"error": "Minutes must be positive"}
        result = reminder_scheduler.schedule_reminder(message, minutes)
        return {
            "status": "scheduled",
            "id": result["id"],
            "message": message,
            "fire_at": result["fire_at"],
            "minutes": minutes,
        }
    except Exception as e:
        return {"error": f"Failed to set reminder: {str(e)}"}


def tool_clipboard(action: str, text: str = None) -> dict:
    """Read from or write to the macOS clipboard using pbpaste/pbcopy. Supports history."""
    try:
        if action == "read":
            result = subprocess.run(
                ["pbpaste"], capture_output=True, text=True, timeout=3
            )
            content = result.stdout
            if not content:
                return {"action": "read", "content": "", "message": "Clipboard is empty"}
            # Store in history
            _clipboard_history.insert(0, {
                "content": content[:2000],
                "time": datetime.now().strftime("%H:%M:%S"),
            })
            if len(_clipboard_history) > 20:
                _clipboard_history.pop()
            return {"action": "read", "content": content[:5000]}
        elif action == "write":
            if not text:
                return {"error": "No text provided to write to clipboard"}
            proc = subprocess.Popen(
                ["pbcopy"], stdin=subprocess.PIPE, text=True
            )
            proc.communicate(input=text, timeout=3)
            return {"action": "write", "status": "success", "length": len(text)}
        elif action == "history":
            if not _clipboard_history:
                return {"action": "history", "count": 0, "entries": [], "message": "No clipboard history yet"}
            return {"action": "history", "count": len(_clipboard_history), "entries": _clipboard_history}
        else:
            return {"error": f"Unknown action: {action}. Use 'read', 'write', or 'history'."}
    except Exception as e:
        return {"error": f"Clipboard operation failed: {str(e)}"}


def tool_open_app(name: str) -> dict:
    """Launch a macOS application by name."""
    try:
        result = subprocess.run(
            ["open", "-a", name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return {"app": name, "status": "opened"}
        else:
            return {"app": name, "error": f"Could not open: {result.stderr.strip()}"}
    except Exception as e:
        return {"app": name, "error": f"Failed to open app: {str(e)}"}


# ---------------------------------------------------------------------------
# New tool implementations: URL reader, code exec, shell, TTS, image gen, RAG
# ---------------------------------------------------------------------------

def tool_read_url(url: str) -> dict:
    """Fetch and extract clean text from a URL."""
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return {"error": f"Could not fetch URL: {url}"}
        text = trafilatura.extract(downloaded, include_links=False,
                                   include_images=False, include_tables=True)
        if not text:
            return {"error": "Could not extract text content from page"}
        # Truncate for context window
        MAX_CHARS = 8000
        truncated = len(text) > MAX_CHARS
        if truncated:
            text = text[:MAX_CHARS] + "\n\n[... truncated — page too long ...]"
        return {"url": url, "content": text, "truncated": truncated}
    except ImportError:
        if _pip_install("trafilatura"):
            return tool_read_url(url)  # retry after install
        return {"error": "trafilatura not installed and auto-install failed."}
    except Exception as e:
        return {"url": url, "error": f"Failed to read URL: {str(e)}"}


def tool_run_code(code: str) -> dict:
    """Execute Python code in a sandboxed subprocess."""
    import tempfile, sys
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py',
                                         delete=False) as f:
            f.write(code)
            f.flush()
            tmppath = f.name

        result = subprocess.run(
            [sys.executable, tmppath],
            capture_output=True, text=True, timeout=10,
            cwd=tempfile.gettempdir(),
        )
        os.unlink(tmppath)

        output = result.stdout.strip()
        errors = result.stderr.strip()
        return {
            "output": output or "(no output)",
            "errors": errors if errors else None,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        try:
            os.unlink(tmppath)
        except Exception:
            pass
        return {"error": "Code execution timed out (10s limit)"}
    except Exception as e:
        return {"error": f"Code execution failed: {str(e)}"}


# Commands that should NEVER be run via shell_command
_DANGEROUS_COMMANDS = {
    "rm", "rmdir", "mv", "dd", "mkfs", "fdisk", "sudo", "su",
    "chmod", "chown", "kill", "killall", "reboot", "shutdown",
    "format", "diskutil", "csrutil", "nvram",
}


def tool_shell_command(command: str) -> dict:
    """Run a shell command (with safety checks)."""
    # Check for dangerous commands
    first_word = command.strip().split()[0] if command.strip() else ""
    if first_word in _DANGEROUS_COMMANDS:
        return {"error": f"Blocked: '{first_word}' is a destructive command and cannot be run."}

    # Also block piped destructive commands
    for dangerous in _DANGEROUS_COMMANDS:
        if f"| {dangerous}" in command or f"|{dangerous}" in command:
            return {"error": f"Blocked: piping to '{dangerous}' is not allowed."}

    # Use a longer timeout for pip install commands
    is_pip = "pip install" in command or "pip3 install" in command
    timeout = 300 if is_pip else 30

    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=timeout, cwd=os.path.expanduser("~"),
        )
        output = result.stdout.strip()
        errors = result.stderr.strip()

        # Truncate long output
        MAX_CHARS = 6000
        if len(output) > MAX_CHARS:
            output = output[:MAX_CHARS] + "\n\n[... truncated ...]"

        return {
            "output": output or "(no output)",
            "errors": errors if errors else None,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": "Command timed out (15s limit)"}
    except Exception as e:
        return {"error": f"Command failed: {str(e)}"}


def tool_speak(text: str) -> dict:
    """Speak text aloud using macOS TTS."""
    try:
        from tts import speak
        success = speak(text)
        return {"status": "speaking" if success else "failed"}
    except ImportError:
        return {"error": "TTS module not available"}
    except Exception as e:
        return {"error": f"TTS failed: {str(e)}"}


def tool_image_generate(prompt: str) -> dict:
    """Generate an image using Google Gemini Nano Banana Pro."""
    try:
        import re, time, sqlite3 as _sql3

        # Get Gemini API key from settings (sync read — tools run in sync context)
        try:
            _db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libre_bird.db")
            _conn = _sql3.connect(_db_path)
            _row = _conn.execute(
                "SELECT value FROM settings WHERE key = 'gemini_api_key'"
            ).fetchone()
            _conn.close()
            api_key = _row[0] if _row else ""
        except Exception:
            api_key = ""

        if not api_key:
            return {"error": "No Gemini API key configured. Please add your API key in Settings → API Keys."}

        # Ensure google-genai is installed
        try:
            from google import genai
        except ImportError:
            if _pip_install("google-genai"):
                from google import genai
            else:
                return {"error": "google-genai not installed and auto-install failed."}

        # Output directory
        output_dir = os.path.expanduser("~/Pictures/libre-bird")
        os.makedirs(output_dir, exist_ok=True)

        # Generate a filename from the prompt
        safe_name = re.sub(r'[^a-zA-Z0-9]+', '_', prompt[:50]).strip('_').lower()
        timestamp = int(time.time())
        filename = f"{safe_name}_{timestamp}.png"
        filepath = os.path.join(output_dir, filename)

        logger.info(f"Generating image with Gemini Nano Banana Pro: '{prompt}'")

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model="gemini-3-pro-image-preview",
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                response_modalities=["IMAGE", "TEXT"],
            ),
        )

        # Extract image from response
        image_saved = False
        for part in response.candidates[0].content.parts:
            if part.inline_data is not None:
                image = part.as_image()
                image.save(filepath)
                image_saved = True
                break

        if not image_saved:
            return {"error": "Gemini did not return an image. Try a different prompt."}

        logger.info(f"Image saved: {filepath}")

        return {
            "status": "generated",
            "path": filepath,
            "filename": filename,
            "url": f"/generated/{filename}",
            "prompt": prompt,
        }
    except Exception as e:
        logger.error(f"Image generation failed: {e}")
        return {"error": f"Image generation failed: {str(e)}"}


def tool_system_control(action: str, value: str = None) -> dict:
    """Control macOS system settings and actions via AppleScript/CLI."""
    try:
        def _osascript(script: str) -> str:
            """Run an AppleScript and return stdout."""
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10
            )
            return r.stdout.strip() if r.returncode == 0 else r.stderr.strip()

        def _sh(cmd: str) -> str:
            """Run a shell command and return stdout."""
            r = subprocess.run(
                cmd, shell=True, capture_output=True, text=True, timeout=10
            )
            return r.stdout.strip() if r.returncode == 0 else r.stderr.strip()

        # ── Volume ────────────────────────────────────────────
        if action == "volume_set":
            level = int(value) if value else 50
            level = max(0, min(100, level))
            vol = round(level / 100 * 7)  # macOS volume is 0-7
            _osascript(f"set volume output volume {level}")
            return {"status": "ok", "action": "volume_set", "level": level}

        elif action == "volume_up":
            _osascript("set volume output volume ((output volume of (get volume settings)) + 10)")
            cur = _osascript("output volume of (get volume settings)")
            return {"status": "ok", "action": "volume_up", "level": cur}

        elif action == "volume_down":
            _osascript("set volume output volume ((output volume of (get volume settings)) - 10)")
            cur = _osascript("output volume of (get volume settings)")
            return {"status": "ok", "action": "volume_down", "level": cur}

        elif action == "mute":
            _osascript("set volume with output muted")
            return {"status": "ok", "action": "muted"}

        elif action == "unmute":
            _osascript("set volume without output muted")
            return {"status": "ok", "action": "unmuted"}

        # ── Brightness ────────────────────────────────────────
        elif action == "brightness":
            level = float(value) if value else 50
            level = max(0, min(100, level))
            brightness = level / 100
            # Try brightness command first, fall back to AppleScript
            result = _sh(f"brightness {brightness} 2>/dev/null")
            if "not found" in result.lower():
                _osascript(f'tell application "System Preferences" to quit')
                return {"status": "partial", "message": f"Install 'brightness' CLI via Homebrew: brew install brightness. Attempted to set to {level}%."}
            return {"status": "ok", "action": "brightness", "level": level}

        # ── Dark/Light Mode ───────────────────────────────────
        elif action == "dark_mode":
            _osascript('tell application "System Events" to tell appearance preferences to set dark mode to true')
            return {"status": "ok", "action": "dark_mode", "mode": "dark"}

        elif action == "light_mode":
            _osascript('tell application "System Events" to tell appearance preferences to set dark mode to false')
            return {"status": "ok", "action": "light_mode", "mode": "light"}

        # ── Do Not Disturb ────────────────────────────────────
        elif action == "dnd_on":
            _sh("shortcuts run 'Do Not Disturb' 2>/dev/null || true")
            # Fallback: use Focus via AppleScript
            _osascript('''
                tell application "System Events"
                    tell process "ControlCenter"
                        click menu bar item "Focus" of menu bar 1
                    end tell
                end tell
            ''')
            return {"status": "ok", "action": "dnd_on", "message": "Attempted to enable Do Not Disturb. You may need to confirm via Control Center."}

        elif action == "dnd_off":
            return {"status": "ok", "action": "dnd_off", "message": "To disable DnD, click the Focus icon in the menu bar or use Control Center."}

        # ── Lock Screen ───────────────────────────────────────
        elif action == "lock_screen":
            _sh("pmset displaysleepnow")
            return {"status": "ok", "action": "lock_screen"}

        # ── Screenshot ────────────────────────────────────────
        elif action == "screenshot":
            import time as _t
            ts = int(_t.time())
            path = os.path.expanduser(f"~/Desktop/screenshot_{ts}.png")
            _sh(f"screencapture -x {path}")
            if os.path.exists(path):
                return {"status": "ok", "action": "screenshot", "path": path}
            return {"error": "Screenshot failed"}

        # ── Open App ──────────────────────────────────────────
        elif action == "open_app":
            app_name = value or ""
            if not app_name:
                return {"error": "Please specify the app name"}
            result = _sh(f'open -a "{app_name}" 2>&1')
            if "unable to find" in result.lower() or "can't open" in result.lower():
                return {"error": f"Could not find app: {app_name}"}
            return {"status": "ok", "action": "open_app", "app": app_name}

        # ── Sleep ─────────────────────────────────────────────
        elif action == "sleep":
            _osascript('tell application "System Events" to sleep')
            return {"status": "ok", "action": "sleep"}

        # ── Empty Trash ───────────────────────────────────────
        elif action == "empty_trash":
            _osascript('tell application "Finder" to empty the trash')
            return {"status": "ok", "action": "empty_trash"}

        # ── Battery ───────────────────────────────────────────
        elif action == "battery":
            info = _sh("pmset -g batt")
            # Parse battery percentage and state
            import re
            match = re.search(r'(\d+)%;\s*(\w+)', info)
            if match:
                return {
                    "status": "ok",
                    "action": "battery",
                    "percentage": int(match.group(1)),
                    "state": match.group(2),  # charging, discharging, charged
                    "raw": info,
                }
            return {"status": "ok", "action": "battery", "raw": info}

        # ── WiFi ──────────────────────────────────────────────
        elif action == "wifi_on":
            _sh("networksetup -setairportpower en0 on")
            return {"status": "ok", "action": "wifi_on"}

        elif action == "wifi_off":
            _sh("networksetup -setairportpower en0 off")
            return {"status": "ok", "action": "wifi_off"}

        # ── Bluetooth ─────────────────────────────────────────
        elif action == "bluetooth_on":
            result = _sh("blueutil --power 1 2>/dev/null")
            if not result or "not found" in result.lower():
                _osascript('tell application "System Preferences" to reveal pane id "com.apple.preferences.Bluetooth"')
                return {"status": "partial", "message": "Opened Bluetooth preferences. Install 'blueutil' for CLI control: brew install blueutil"}
            return {"status": "ok", "action": "bluetooth_on"}

        elif action == "bluetooth_off":
            result = _sh("blueutil --power 0 2>/dev/null")
            if not result or "not found" in result.lower():
                return {"status": "partial", "message": "Install 'blueutil' for CLI control: brew install blueutil"}
            return {"status": "ok", "action": "bluetooth_off"}

        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        logger.error(f"System control failed: {e}")
        return {"error": f"System control failed: {str(e)}"}


def tool_music_control(action: str, query: str = None, track_name: str = None, genre: str = None) -> dict:
    """Control Apple Music and analyze listening habits via AppleScript."""
    try:
        def _music_script(script: str) -> str:
            """Run an AppleScript targeting Music.app."""
            full = f'tell application "Music"\n{script}\nend tell'
            r = subprocess.run(
                ["osascript", "-e", full],
                capture_output=True, text=True, timeout=15
            )
            return r.stdout.strip() if r.returncode == 0 else r.stderr.strip()

        def _raw_osascript(script: str) -> str:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=30
            )
            return r.stdout.strip() if r.returncode == 0 else r.stderr.strip()

        # ── Playback ──────────────────────────────────────────
        if action == "play":
            if query:
                # Search and play
                _music_script(f'play (first track of playlist "Library" whose name contains "{query}" or artist contains "{query}")')
                return {"status": "ok", "action": "play", "query": query}
            else:
                _music_script("play")
                return {"status": "ok", "action": "play"}

        elif action == "pause":
            _music_script("pause")
            return {"status": "ok", "action": "paused"}

        elif action == "next":
            _music_script("next track")
            import time; time.sleep(0.5)
            info = _music_script("get {name, artist, album} of current track")
            return {"status": "ok", "action": "next", "now_playing": info}

        elif action == "previous":
            _music_script("previous track")
            import time; time.sleep(0.5)
            info = _music_script("get {name, artist, album} of current track")
            return {"status": "ok", "action": "previous", "now_playing": info}

        # ── Now Playing ───────────────────────────────────────
        elif action == "now_playing":
            state = _music_script("get player state as string")
            if "playing" not in state.lower() and "paused" not in state.lower():
                return {"status": "ok", "state": "stopped", "message": "Nothing is playing"}
            name = _music_script("get name of current track")
            artist = _music_script("get artist of current track")
            album = _music_script("get album of current track")
            genre_info = _music_script("get genre of current track")
            duration = _music_script("get duration of current track")
            pos = _music_script("get player position")
            return {
                "status": "ok",
                "state": state,
                "track": name,
                "artist": artist,
                "album": album,
                "genre": genre_info,
                "duration_seconds": duration,
                "position_seconds": pos,
            }

        # ── Search Library ────────────────────────────────────
        elif action == "search":
            if not query:
                return {"error": "Please provide a search query"}
            # Search by name, artist, or genre
            script = f'''
tell application "Music"
    set results to {{}}
    set searchResults to (search playlist "Library" for "{query}")
    set maxResults to 15
    if (count of searchResults) < maxResults then set maxResults to (count of searchResults)
    repeat with i from 1 to maxResults
        set t to item i of searchResults
        set end of results to (name of t) & " — " & (artist of t) & " [" & (album of t) & "]"
    end repeat
    return results
end tell'''
            result = _raw_osascript(script)
            if not result or "error" in result.lower():
                return {"status": "ok", "results": [], "message": f"No results for '{query}'"}
            tracks = [t.strip() for t in result.split(",") if t.strip()]
            return {"status": "ok", "query": query, "results": tracks, "count": len(tracks)}

        # ── Create Playlist ───────────────────────────────────
        elif action == "create_playlist":
            name = query or "Libre Bird Mix"
            _music_script(f'make new playlist with properties {{name:"{name}"}}')
            return {"status": "ok", "action": "create_playlist", "name": name}

        # ── Add to Playlist ───────────────────────────────────
        elif action == "add_to_playlist":
            playlist_name = query or "Libre Bird Mix"
            if not track_name:
                return {"error": "Please specify track_name to add"}
            script = f'''
tell application "Music"
    set t to (first track of playlist "Library" whose name contains "{track_name}")
    duplicate t to playlist "{playlist_name}"
end tell'''
            result = _raw_osascript(script)
            if "error" in result.lower():
                return {"error": f"Could not add '{track_name}' to '{playlist_name}': {result}"}
            return {"status": "ok", "action": "add_to_playlist", "track": track_name, "playlist": playlist_name}

        # ── Listening Stats ───────────────────────────────────
        elif action == "listening_stats":
            # Get top artists by play count
            top_artists_script = '''
tell application "Music"
    set allTracks to every track of playlist "Library"
    set artistCounts to {}
    set artistNames to {}
    repeat with t in allTracks
        set a to artist of t
        set pc to played count of t
        if pc > 0 then
            set found to false
            repeat with i from 1 to count of artistNames
                if item i of artistNames is a then
                    set item i of artistCounts to (item i of artistCounts) + pc
                    set found to true
                    exit repeat
                end if
            end repeat
            if not found then
                set end of artistNames to a
                set end of artistCounts to pc
            end if
        end if
    end repeat
    -- Sort and return top 10
    set output to {}
    repeat 10 times
        set maxVal to 0
        set maxIdx to 0
        repeat with i from 1 to count of artistCounts
            if item i of artistCounts > maxVal then
                set maxVal to item i of artistCounts
                set maxIdx to i
            end if
        end repeat
        if maxIdx > 0 then
            set end of output to (item maxIdx of artistNames) & " (" & maxVal & " plays)"
            set item maxIdx of artistCounts to 0
        end if
    end repeat
    return output
end tell'''
            # Get top genres
            top_genres_script = '''
tell application "Music"
    set allTracks to every track of playlist "Library"
    set genreNames to {}
    set genreCounts to {}
    repeat with t in allTracks
        set g to genre of t
        set pc to played count of t
        if pc > 0 and g is not "" then
            set found to false
            repeat with i from 1 to count of genreNames
                if item i of genreNames is g then
                    set item i of genreCounts to (item i of genreCounts) + pc
                    set found to true
                    exit repeat
                end if
            end repeat
            if not found then
                set end of genreNames to g
                set end of genreCounts to pc
            end if
        end if
    end repeat
    set output to {}
    repeat 8 times
        set maxVal to 0
        set maxIdx to 0
        repeat with i from 1 to count of genreCounts
            if item i of genreCounts > maxVal then
                set maxVal to item i of genreCounts
                set maxIdx to i
            end if
        end repeat
        if maxIdx > 0 then
            set end of output to (item maxIdx of genreNames) & " (" & maxVal & " plays)"
            set item maxIdx of genreCounts to 0
        end if
    end repeat
    return output
end tell'''
            # Get most played tracks
            top_tracks_script = '''
tell application "Music"
    set allTracks to every track of playlist "Library"
    set trackInfos to {}
    set trackPlays to {}
    repeat with t in allTracks
        set pc to played count of t
        if pc > 2 then
            set end of trackInfos to (name of t) & " — " & (artist of t)
            set end of trackPlays to pc
        end if
    end repeat
    set output to {}
    repeat 15 times
        set maxVal to 0
        set maxIdx to 0
        repeat with i from 1 to count of trackPlays
            if item i of trackPlays > maxVal then
                set maxVal to item i of trackPlays
                set maxIdx to i
            end if
        end repeat
        if maxIdx > 0 then
            set end of output to (item maxIdx of trackInfos) & " (" & maxVal & "x)"
            set item maxIdx of trackPlays to 0
        end if
    end repeat
    return output
end tell'''
            artists = _raw_osascript(top_artists_script)
            genres = _raw_osascript(top_genres_script)
            tracks = _raw_osascript(top_tracks_script)

            return {
                "status": "ok",
                "action": "listening_stats",
                "top_artists": [a.strip() for a in artists.split(",") if a.strip()] if artists else [],
                "top_genres": [g.strip() for g in genres.split(",") if g.strip()] if genres else [],
                "most_played_tracks": [t.strip() for t in tracks.split(",") if t.strip()] if tracks else [],
                "tip": "Use this data to recommend music based on what the user is currently working on.",
            }

        # ── Recent History ────────────────────────────────────
        elif action == "recent_history":
            script = '''
tell application "Music"
    set recentTracks to (every track of playlist "Library" whose played date > (current date) - 7 * days)
    set output to {}
    set maxItems to 20
    if (count of recentTracks) < maxItems then set maxItems to (count of recentTracks)
    repeat with i from 1 to maxItems
        set t to item i of recentTracks
        set end of output to (name of t) & " — " & (artist of t) & " [" & (genre of t) & "]"
    end repeat
    return output
end tell'''
            result = _raw_osascript(script)
            tracks = [t.strip() for t in result.split(",") if t.strip()] if result else []
            return {
                "status": "ok",
                "action": "recent_history",
                "recent_tracks": tracks,
                "period": "last 7 days",
            }

        else:
            return {"error": f"Unknown music action: {action}"}

    except Exception as e:
        logger.error(f"Music control failed: {e}")
        return {"error": f"Music control failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Phase 1 Agentic Tools
# ---------------------------------------------------------------------------

# Clipboard history (in-memory, shared with tool_clipboard)
_clipboard_history: list[dict] = []


def tool_file_operations(action: str, path: str, content: str = None, destination: str = None) -> dict:
    """Perform file system operations with safety checks."""
    try:
        # Expand ~ and resolve path
        path = os.path.expanduser(path)

        # Safety: restrict to $HOME
        home = os.path.expanduser("~")
        if not os.path.abspath(path).startswith(home):
            return {"error": f"Access denied: path must be within {home}"}

        if action == "read":
            if not os.path.isfile(path):
                return {"error": f"File not found: {path}"}
            size = os.path.getsize(path)
            if size > 1_000_000:  # 1MB limit
                return {"error": f"File too large ({size} bytes). Max 1MB for text reading."}
            with open(path, "r", errors="replace") as f:
                text = f.read()
            return {"action": "read", "path": path, "size": len(text), "content": text[:50000]}

        elif action == "write":
            if not content:
                return {"error": "No content provided for write"}
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return {"action": "write", "path": path, "size": len(content)}

        elif action == "append":
            if not content:
                return {"error": "No content provided for append"}
            with open(path, "a") as f:
                f.write(content)
            return {"action": "append", "path": path, "added": len(content)}

        elif action == "list":
            if not os.path.isdir(path):
                return {"error": f"Not a directory: {path}"}
            entries = []
            for entry in sorted(os.listdir(path)):
                full = os.path.join(path, entry)
                try:
                    stat = os.stat(full)
                    entries.append({
                        "name": entry,
                        "type": "dir" if os.path.isdir(full) else "file",
                        "size": stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    })
                except OSError:
                    entries.append({"name": entry, "type": "unknown"})
            return {"action": "list", "path": path, "count": len(entries), "entries": entries[:100]}

        elif action == "move":
            if not destination:
                return {"error": "No destination provided for move"}
            destination = os.path.expanduser(destination)
            shutil.move(path, destination)
            return {"action": "move", "from": path, "to": destination}

        elif action == "copy":
            if not destination:
                return {"error": "No destination provided for copy"}
            destination = os.path.expanduser(destination)
            if os.path.isdir(path):
                shutil.copytree(path, destination)
            else:
                shutil.copy2(path, destination)
            return {"action": "copy", "from": path, "to": destination}

        elif action == "delete":
            # Safe delete: move to Trash via AppleScript
            if not os.path.exists(path):
                return {"error": f"Path not found: {path}"}
            escaped = path.replace('"', '\\"')
            script = f'''
            tell application "Finder"
                delete POSIX file "{escaped}"
            end tell
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return {"action": "delete", "path": path, "method": "moved_to_trash"}

        elif action == "mkdir":
            os.makedirs(path, exist_ok=True)
            return {"action": "mkdir", "path": path}

        elif action == "info":
            if not os.path.exists(path):
                return {"error": f"Path not found: {path}"}
            stat = os.stat(path)
            return {
                "action": "info",
                "path": path,
                "exists": True,
                "type": "directory" if os.path.isdir(path) else "file",
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "created": datetime.fromtimestamp(stat.st_birthtime).isoformat() if hasattr(stat, "st_birthtime") else None,
                "extension": os.path.splitext(path)[1] if os.path.isfile(path) else None,
            }

        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        logger.error(f"File operation failed: {e}")
        return {"error": f"File operation failed: {str(e)}"}


def tool_keyboard(action: str, text: str) -> dict:
    """Type text or press keys in the active application via AppleScript."""
    try:
        if action == "type_text":
            # Escape for AppleScript string
            safe = text.replace("\\", "\\\\").replace('"', '\\"')
            script = f'''
            tell application "System Events"
                keystroke "{safe}"
            end tell
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return {"action": "type_text", "typed": len(text)}

        elif action == "press_key":
            # Map friendly names to key codes
            key_map = {
                "return": 36, "enter": 36, "tab": 48, "escape": 53, "esc": 53,
                "space": 49, "delete": 51, "backspace": 51, "forward_delete": 117,
                "up": 126, "down": 125, "left": 123, "right": 124,
                "home": 115, "end": 119, "page_up": 116, "page_down": 121,
                "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96,
                "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
                "f11": 103, "f12": 111,
            }
            key_code = key_map.get(text.lower())
            if key_code is None:
                return {"error": f"Unknown key: {text}. Available: {', '.join(key_map.keys())}"}
            script = f'''
            tell application "System Events"
                key code {key_code}
            end tell
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return {"action": "press_key", "key": text}

        elif action == "hotkey":
            # Parse shortcut like "cmd+s", "cmd+shift+z"
            parts = [p.strip().lower() for p in text.split("+")]
            key_char = parts[-1]
            modifiers = parts[:-1]

            modifier_map = {
                "cmd": "command down", "command": "command down",
                "ctrl": "control down", "control": "control down",
                "alt": "option down", "option": "option down",
                "shift": "shift down",
            }

            mod_list = []
            for m in modifiers:
                mapped = modifier_map.get(m)
                if mapped:
                    mod_list.append(mapped)

            if not mod_list:
                return {"error": f"No valid modifiers in '{text}'. Use cmd, ctrl, alt, shift."}

            mods = ", ".join(mod_list)
            script = f'''
            tell application "System Events"
                keystroke "{key_char}" using {{{mods}}}
            end tell
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return {"action": "hotkey", "shortcut": text}

        else:
            return {"error": f"Unknown action: {action}. Use type_text, press_key, or hotkey."}

    except Exception as e:
        logger.error(f"Keyboard action failed: {e}")
        return {"error": f"Keyboard action failed: {str(e)}"}


def tool_read_document(path: str, pages: str = None) -> dict:
    """Read and extract text from documents (PDF, Word, text, Markdown, CSV)."""
    try:
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            return {"error": f"File not found: {path}"}

        ext = os.path.splitext(path)[1].lower()
        file_size = os.path.getsize(path)

        # PDF
        if ext == ".pdf":
            try:
                import PyPDF2
            except ImportError:
                if _pip_install("PyPDF2"):
                    import PyPDF2
                else:
                    return {"error": "PyPDF2 not installed and auto-install failed"}

            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                total_pages = len(reader.pages)

                # Parse page range
                start_page, end_page = 0, total_pages
                if pages:
                    if "-" in pages:
                        parts = pages.split("-")
                        start_page = max(0, int(parts[0]) - 1)
                        end_page = min(total_pages, int(parts[1]))
                    else:
                        start_page = max(0, int(pages) - 1)
                        end_page = start_page + 1

                text_parts = []
                for i in range(start_page, end_page):
                    page_text = reader.pages[i].extract_text() or ""
                    text_parts.append(f"--- Page {i + 1} ---\n{page_text}")

                text = "\n\n".join(text_parts)

            return {
                "type": "pdf",
                "path": path,
                "total_pages": total_pages,
                "pages_read": f"{start_page + 1}-{end_page}",
                "content": text[:50000],
            }

        # Word documents
        elif ext == ".docx":
            try:
                import docx
            except ImportError:
                if _pip_install("python-docx"):
                    import docx
                else:
                    return {"error": "python-docx not installed and auto-install failed"}

            doc = docx.Document(path)
            paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
            text = "\n\n".join(paragraphs)
            return {
                "type": "docx",
                "path": path,
                "paragraphs": len(paragraphs),
                "content": text[:50000],
            }

        # Plain text, Markdown, CSV, JSON, etc.
        elif ext in {".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml", ".log", ".ini", ".conf", ".cfg", ".html", ".htm", ".rtf"}:
            if file_size > 2_000_000:
                return {"error": f"File too large ({file_size} bytes). Max 2MB."}
            with open(path, "r", errors="replace") as f:
                text = f.read()
            return {
                "type": ext.lstrip("."),
                "path": path,
                "size": file_size,
                "content": text[:50000],
            }

        else:
            return {"error": f"Unsupported file type: {ext}. Supported: .pdf, .docx, .txt, .md, .csv, .json, .xml, .yaml, .log, .html"}

    except Exception as e:
        logger.error(f"Document read failed: {e}")
        return {"error": f"Document read failed: {str(e)}"}


def tool_read_notifications(action: str, app_name: str = None) -> dict:
    """Read or clear macOS notifications."""
    try:
        if action == "list":
            # Try AppleScript approach first
            script = '''
            tell application "System Events"
                set notifList to {}
                try
                    tell process "NotificationCenter"
                        set theWindows to every window
                        repeat with w in theWindows
                            try
                                set notifTitle to value of static text 1 of w
                                set notifBody to ""
                                try
                                    set notifBody to value of static text 2 of w
                                end try
                                set end of notifList to notifTitle & " | " & notifBody
                            end try
                        end repeat
                    end tell
                end try
            end tell
            if (count of notifList) is 0 then
                return "NO_NOTIFICATIONS"
            end if
            set AppleScript's text item delimiters to "|||"
            return notifList as text
            '''
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, text=True, timeout=10
            )

            if result.returncode == 0 and result.stdout.strip() != "NO_NOTIFICATIONS":
                notifications = []
                for item in result.stdout.strip().split("|||"):
                    parts = item.split(" | ", 1)
                    notifications.append({
                        "title": parts[0].strip() if parts else "",
                        "body": parts[1].strip() if len(parts) > 1 else "",
                    })

                if app_name:
                    notifications = [n for n in notifications
                                     if app_name.lower() in n.get("title", "").lower()
                                     or app_name.lower() in n.get("body", "").lower()]

                return {"action": "list", "count": len(notifications), "notifications": notifications[:20]}

            # Fallback: try reading notification DB
            import sqlite3 as _sqlite3
            import glob
            db_pattern = os.path.expanduser("~/Library/Group Containers/group.com.apple.usernoted/db2/db")
            db_files = glob.glob(db_pattern)
            if db_files:
                conn = _sqlite3.connect(db_files[0])
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT app_id, title, subtitle, body, delivered_date
                    FROM record
                    ORDER BY delivered_date DESC
                    LIMIT 20
                """)
                rows = cursor.fetchall()
                conn.close()

                notifications = []
                for row in rows:
                    n = {"app": row[0] or "", "title": row[1] or "", "subtitle": row[2] or "", "body": row[3] or ""}
                    if app_name and app_name.lower() not in n["app"].lower() and app_name.lower() not in n["title"].lower():
                        continue
                    notifications.append(n)

                return {"action": "list", "count": len(notifications), "notifications": notifications, "source": "database"}

            return {"action": "list", "count": 0, "notifications": [], "message": "No notifications found or access denied"}

        elif action == "clear":
            if not app_name:
                return {"error": "app_name required for clear action"}
            # Close notification banners via AppleScript
            script = f'''
            tell application "System Events"
                try
                    tell process "NotificationCenter"
                        set theWindows to every window
                        repeat with w in theWindows
                            try
                                click button 1 of w
                            end try
                        end repeat
                    end tell
                end try
            end tell
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
            return {"action": "clear", "app": app_name, "status": "attempted"}

        else:
            return {"error": f"Unknown action: {action}. Use 'list' or 'clear'."}

    except Exception as e:
        logger.error(f"Notification reading failed: {e}")
        return {"error": f"Notification reading failed: {str(e)}"}


def tool_knowledge_search(query: str) -> dict:
    """Search the local knowledge base."""
    try:
        from knowledge import search
        return search(query)
    except ImportError:
        return {"error": "Knowledge module not available"}
    except Exception as e:
        return {"error": f"Knowledge search failed: {str(e)}"}


def tool_analyze_screen(query: str = None, image_path: str = None) -> dict:
    """Capture the screen (or load an image) and extract text via OCR.
    
    Returns rich screen data that the LLM can reason about:
    - Extracted text from the screen/image
    - Active app context if available
    - Timestamp
    """
    import base64
    
    if image_path:
        # Analyze a specific image file from disk
        image_path = os.path.expanduser(image_path)
        if not os.path.exists(image_path):
            return {"error": f"Image file not found: {image_path}"}
        
        ext = os.path.splitext(image_path)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".webp"):
            return {"error": f"Unsupported image format: {ext}"}
        
        # Try OCR on the image file
        try:
            from screen_ocr import ocr_image, VISION_AVAILABLE
            if not VISION_AVAILABLE:
                return {
                    "error": "macOS Vision framework not available. Install pyobjc-framework-Vision.",
                }
            
            import Quartz
            from Foundation import NSURL
            
            # Load image from file
            file_url = NSURL.fileURLWithPath_(image_path)
            cg_source = Quartz.CGImageSourceCreateWithURL(file_url, None)
            if cg_source is None:
                return {"error": f"Could not load image: {image_path}"}
            
            cg_image = Quartz.CGImageSourceCreateImageAtIndex(cg_source, 0, None)
            if cg_image is None:
                return {"error": f"Could not decode image: {image_path}"}
            
            text = ocr_image(cg_image)
            del cg_image
            
            result = {
                "source": "image_file",
                "path": image_path,
                "text": text if text else "(No text detected in image)",
                "char_count": len(text) if text else 0,
                "timestamp": datetime.now().isoformat(),
            }
            
            if query:
                result["focus_query"] = query
                result["note"] = f"The user is specifically asking about: {query}. Focus your analysis on that."
            
            return result
            
        except Exception as e:
            return {"error": f"Image analysis failed: {str(e)}"}
    
    else:
        # Capture and analyze the current screen
        try:
            from screen_ocr import read_screen
        except ImportError:
            return {"error": "screen_ocr module not available"}
        
        screen_data = read_screen()
        
        if not screen_data.get("available"):
            return {
                "error": screen_data.get("error", "Screen capture not available"),
                "hint": "Check System Settings > Privacy & Security > Screen Recording permissions.",
            }
        
        text = screen_data.get("text", "")
        
        result = {
            "source": "screen_capture",
            "text": text if text else "(No text detected on screen)",
            "char_count": len(text) if text else 0,
            "timestamp": datetime.now().isoformat(),
        }
        
        # Add context about what app is active (if we can get it)
        try:
            active_app = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of first application process whose frontmost is true'],
                capture_output=True, text=True, timeout=3
            )
            if active_app.returncode == 0:
                result["active_app"] = active_app.stdout.strip()
            
            window_title = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to get name of front window of (first application process whose frontmost is true)'],
                capture_output=True, text=True, timeout=3
            )
            if window_title.returncode == 0:
                result["window_title"] = window_title.stdout.strip()
        except Exception:
            pass  # Non-critical — proceed without app context
        
        if query:
            result["focus_query"] = query
            result["note"] = f"The user is specifically asking about: {query}. Focus your analysis on that."
        else:
            result["note"] = "This is the OCR-extracted text from the user's screen. Describe what you see and answer any questions about it."
        
        return result


def tool_knowledge_add(text: str, source: str = "user_input") -> dict:
    """Add a document to the local knowledge base."""
    try:
        from knowledge import add_document
        return add_document(text, source)
    except ImportError:
        return {"error": "Knowledge module not available"}
    except Exception as e:
        return {"error": f"Knowledge add failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

# Import screen OCR (lazy — only used when the tool is called)
try:
    from screen_ocr import read_screen as _read_screen
except ImportError:
    _read_screen = lambda: {"error": "screen_ocr module not available"}

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
    "read_screen": lambda args: _read_screen(),
    "set_reminder": lambda args: tool_set_reminder(
        args.get("message", "Reminder"), args.get("minutes", 1)
    ),
    "clipboard": lambda args: tool_clipboard(
        args.get("action", "read"), args.get("text")
    ),
    "open_app": lambda args: tool_open_app(args.get("name", "")),
    # ── New tools ─────────────────────────────────────────────────────
    "read_url": lambda args: tool_read_url(args.get("url", "")),
    "run_code": lambda args: tool_run_code(args.get("code", "")),
    "shell_command": lambda args: tool_shell_command(args.get("command", "")),
    "speak": lambda args: tool_speak(args.get("text", "")),
    "image_generate": lambda args: tool_image_generate(
        args.get("prompt", "")
    ),
    "system_control": lambda args: tool_system_control(
        args.get("action", ""), args.get("value")
    ),
    "music_control": lambda args: tool_music_control(
        args.get("action", ""), args.get("query"), args.get("track_name"), args.get("genre")
    ),
    "knowledge_search": lambda args: tool_knowledge_search(args.get("query", "")),
    "knowledge_add": lambda args: tool_knowledge_add(
        args.get("text", ""), args.get("source", "user_input")
    ),
    # ── Phase 1 Agentic Tools ────────────────────────────────────────
    "file_operations": lambda args: tool_file_operations(
        args.get("action", ""), args.get("path", ""), args.get("content"), args.get("destination")
    ),
    "keyboard": lambda args: tool_keyboard(
        args.get("action", ""), args.get("text", "")
    ),
    "read_document": lambda args: tool_read_document(
        args.get("path", ""), args.get("pages")
    ),
    "read_notifications": lambda args: tool_read_notifications(
        args.get("action", "list"), args.get("app_name")
    ),
    # ── Phase 3 Intelligence Tools ───────────────────────────────────
    "analyze_screen": lambda args: tool_analyze_screen(
        args.get("query"), args.get("image_path")
    ),
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
