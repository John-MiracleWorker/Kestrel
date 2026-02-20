"""
Productivity Skill
Clipboard, reminders, file operations, keyboard automation, document reader, notifications.

Several functions require macOS — will raise a clear error on Linux/Docker.
"""

import os
import platform
import subprocess
import shutil
from datetime import datetime


def _check_macos():
    """Raise a clear error if not running on macOS."""
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        raise RuntimeError(
            "This function requires macOS with osascript. "
            "It cannot run in a Linux/Docker environment."
        )


# Clipboard history (in-memory ring buffer)
_clipboard_history: list[dict] = []


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": "Set a timed reminder that will show as a macOS notification. Use this when the user says 'remind me', 'in X minutes', 'set a timer', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "The reminder message to display"},
                    "minutes": {"type": "number", "description": "How many minutes from now to fire the reminder"},
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
                    "text": {"type": "string", "description": "Text to copy to clipboard (required when action is 'write')"},
                },
                "required": ["action"],
            },
        },
    },
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
                    "path": {"type": "string", "description": "The file or directory path"},
                    "content": {"type": "string", "description": "File content for 'write' or 'append' actions"},
                    "destination": {"type": "string", "description": "Destination path for 'move' or 'copy' actions"},
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
                    "path": {"type": "string", "description": "Path to the document file"},
                    "pages": {"type": "string", "description": "Optional page range for PDFs, e.g., '1-5' or '3'. Omit to read all pages."},
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
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_set_reminder(message: str, minutes: float) -> dict:
    try:
        if minutes <= 0:
            return {"error": "Minutes must be positive"}
        from notifications import reminder_scheduler
        result = reminder_scheduler.schedule_reminder(message, minutes)
        return {
            "status": "scheduled", "id": result["id"],
            "message": message, "fire_at": result["fire_at"], "minutes": minutes,
        }
    except Exception as e:
        return {"error": f"Failed to set reminder: {str(e)}"}


def tool_clipboard(action: str, text: str = None) -> dict:
    try:
        if action == "read":
            result = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=3)
            content = result.stdout
            if not content:
                return {"action": "read", "content": "", "message": "Clipboard is empty"}
            _clipboard_history.insert(0, {"content": content[:2000], "time": datetime.now().strftime("%H:%M:%S")})
            if len(_clipboard_history) > 20:
                _clipboard_history.pop()
            return {"action": "read", "content": content[:5000]}
        elif action == "write":
            if not text:
                return {"error": "No text provided to write to clipboard"}
            proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE, text=True)
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


def tool_file_operations(action: str, path: str, content: str = None, destination: str = None) -> dict:
    path = os.path.expanduser(path)
    try:
        if action == "read":
            if not os.path.isfile(path):
                return {"error": f"File not found: {path}"}
            with open(path, "r", errors="replace") as f:
                text = f.read(50000)
            return {"action": "read", "path": path, "content": text, "size": os.path.getsize(path)}
        elif action == "write":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w") as f:
                f.write(content or "")
            return {"action": "write", "path": path, "bytes_written": len(content or "")}
        elif action == "append":
            with open(path, "a") as f:
                f.write(content or "")
            return {"action": "append", "path": path, "bytes_appended": len(content or "")}
        elif action == "list":
            if not os.path.isdir(path):
                return {"error": f"Not a directory: {path}"}
            entries = []
            for entry in sorted(os.listdir(path))[:50]:
                full = os.path.join(path, entry)
                entries.append({
                    "name": entry,
                    "type": "dir" if os.path.isdir(full) else "file",
                    "size": os.path.getsize(full) if os.path.isfile(full) else None,
                })
            return {"action": "list", "path": path, "entries": entries, "count": len(entries)}
        elif action == "move":
            if not destination:
                return {"error": "destination is required for move"}
            shutil.move(path, os.path.expanduser(destination))
            return {"action": "move", "from": path, "to": destination}
        elif action == "copy":
            if not destination:
                return {"error": "destination is required for copy"}
            dest = os.path.expanduser(destination)
            if os.path.isdir(path):
                shutil.copytree(path, dest)
            else:
                shutil.copy2(path, dest)
            return {"action": "copy", "from": path, "to": destination}
        elif action == "delete":
            _check_macos()
            trash_cmd = ["osascript", "-e",
                         f'tell application "Finder" to delete POSIX file "{path}"']
            result = subprocess.run(trash_cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return {"action": "delete", "path": path, "status": "moved to Trash"}
            else:
                return {"error": f"Delete failed: {result.stderr.strip()}"}
        elif action == "mkdir":
            os.makedirs(path, exist_ok=True)
            return {"action": "mkdir", "path": path, "status": "created"}
        elif action == "info":
            if not os.path.exists(path):
                return {"error": f"Path not found: {path}"}
            stat = os.stat(path)
            return {
                "action": "info", "path": path,
                "type": "dir" if os.path.isdir(path) else "file",
                "size": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "created": datetime.fromtimestamp(stat.st_birthtime).isoformat() if hasattr(stat, 'st_birthtime') else None,
            }
        else:
            return {"error": f"Unknown action: {action}"}
    except Exception as e:
        return {"error": f"File operation failed: {str(e)}"}


def tool_keyboard(action: str, text: str) -> dict:
    _check_macos()
    try:
        if action == "type_text":
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            script = f'tell application "System Events" to keystroke "{escaped}"'
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return {"action": "type_text", "typed": text, "status": "success"}
            else:
                return {"error": f"Keystroke failed: {result.stderr.strip()}"}
        elif action == "press_key":
            key_map = {
                "return": "return", "enter": "return", "tab": "tab",
                "escape": "escape", "esc": "escape", "space": "space",
                "delete": "delete", "backspace": "delete",
                "up": "up arrow", "down": "down arrow",
                "left": "left arrow", "right": "right arrow",
            }
            key_name = key_map.get(text.lower(), text.lower())
            script = f'tell application "System Events" to key code (key code of {key_name})'
            script = f'tell application "System Events" to keystroke return' if key_name == "return" else \
                     f'tell application "System Events" to keystroke tab' if key_name == "tab" else \
                     f'tell application "System Events" to key code 53' if key_name == "escape" else \
                     f'tell application "System Events" to keystroke " "' if key_name == "space" else \
                     f'tell application "System Events" to keystroke (ASCII character 8)' if key_name == "delete" else \
                     f'tell application "System Events" to keystroke "{text}"'
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return {"action": "press_key", "key": text, "status": "success"}
            else:
                return {"error": f"Key press failed: {result.stderr.strip()}"}
        elif action == "hotkey":
            parts = [p.strip().lower() for p in text.split("+")]
            modifiers = []
            key_char = parts[-1]
            for mod in parts[:-1]:
                if mod in ("cmd", "command"):
                    modifiers.append("command down")
                elif mod in ("shift",):
                    modifiers.append("shift down")
                elif mod in ("opt", "option", "alt"):
                    modifiers.append("option down")
                elif mod in ("ctrl", "control"):
                    modifiers.append("control down")
            mod_str = ", ".join(modifiers)
            if mod_str:
                script = f'tell application "System Events" to keystroke "{key_char}" using {{{mod_str}}}'
            else:
                script = f'tell application "System Events" to keystroke "{key_char}"'
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return {"action": "hotkey", "shortcut": text, "status": "success"}
            else:
                return {"error": f"Hotkey failed: {result.stderr.strip()}"}
        else:
            return {"error": f"Unknown keyboard action: {action}. Use 'type_text', 'press_key', or 'hotkey'."}
    except Exception as e:
        return {"error": f"Keyboard operation failed: {str(e)}"}


def tool_read_document(path: str, pages: str = None) -> dict:
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return {"error": f"File not found: {path}"}
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            try:
                import fitz  # PyMuPDF
            except ImportError:
                return {"error": "PyMuPDF not installed. Run: pip install PyMuPDF"}
            doc = fitz.open(path)
            text_parts = []
            page_range = range(len(doc))
            if pages:
                if "-" in pages:
                    start, end = pages.split("-", 1)
                    page_range = range(int(start) - 1, min(int(end), len(doc)))
                else:
                    page_range = [int(pages) - 1]
            for i in page_range:
                if 0 <= i < len(doc):
                    text_parts.append(f"--- Page {i+1} ---\n{doc[i].get_text()}")
            doc.close()
            content = "\n".join(text_parts)
        elif ext == ".docx":
            try:
                import docx
            except ImportError:
                return {"error": "python-docx not installed. Run: pip install python-docx"}
            doc = docx.Document(path)
            content = "\n".join(p.text for p in doc.paragraphs)
        elif ext == ".csv":
            import csv
            with open(path, "r", errors="replace") as f:
                reader = csv.reader(f)
                rows = list(reader)[:100]
            content = "\n".join(",".join(row) for row in rows)
        else:
            with open(path, "r", errors="replace") as f:
                content = f.read(50000)
        if len(content) > 15000:
            content = content[:15000] + "\n\n[... truncated ...]"
        return {"path": path, "extension": ext, "content": content, "char_count": len(content)}
    except Exception as e:
        return {"error": f"Failed to read document: {str(e)}"}


def tool_read_notifications(action: str, app_name: str = None) -> dict:
    try:
        if action == "list":
            # Use sqlite3 to read from the notification center database
            import sqlite3
            import glob
            db_pattern = os.path.expanduser(
                "~/Library/Group Containers/group.com.apple.usernoted/db2/db"
            )
            db_paths = glob.glob(db_pattern)
            if not db_paths:
                return {"action": "list", "notifications": [], "message": "No notification database found — this may require Full Disk Access."}
            try:
                conn = sqlite3.connect(f"file:{db_paths[0]}?mode=ro", uri=True)
                cursor = conn.execute(
                    "SELECT app_id, title, subtitle, body, delivered_date "
                    "FROM record ORDER BY delivered_date DESC LIMIT 20"
                )
                notifications = []
                for row in cursor:
                    entry = {"app": row[0] or "Unknown"}
                    if row[1]:
                        entry["title"] = row[1]
                    if row[2]:
                        entry["subtitle"] = row[2]
                    if row[3]:
                        entry["body"] = row[3]
                    notifications.append(entry)
                conn.close()
                if app_name:
                    notifications = [n for n in notifications if app_name.lower() in n.get("app", "").lower()]
                return {"action": "list", "count": len(notifications), "notifications": notifications}
            except Exception as e:
                return {"action": "list", "error": f"Could not read notifications: {str(e)}",
                        "hint": "Grant Full Disk Access to the terminal in System Settings > Privacy."}
        elif action == "clear":
            if not app_name:
                return {"error": "app_name is required for 'clear' action"}
            _check_macos()
            script = f'''
            tell application "System Events"
                try
                    tell process "NotificationCenter"
                        set notifWindows to every window
                        repeat with w in notifWindows
                            try
                                click button "Close" of w
                            end try
                        end repeat
                    end tell
                end try
            end tell
            '''
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            return {"action": "clear", "app_name": app_name, "status": "attempted"}
        else:
            return {"error": f"Unknown action: {action}. Use 'list' or 'clear'."}
    except Exception as e:
        return {"error": f"Notification operation failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "set_reminder": lambda args: tool_set_reminder(args.get("message", "Reminder"), args.get("minutes", 1)),
    "clipboard": lambda args: tool_clipboard(args.get("action", "read"), args.get("text")),
    "file_operations": lambda args: tool_file_operations(
        args.get("action", ""), args.get("path", ""), args.get("content"), args.get("destination")
    ),
    "keyboard": lambda args: tool_keyboard(args.get("action", ""), args.get("text", "")),
    "read_document": lambda args: tool_read_document(args.get("path", ""), args.get("pages")),
    "read_notifications": lambda args: tool_read_notifications(args.get("action", "list"), args.get("app_name")),
}
