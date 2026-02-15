"""
Apple Notes Skill
Read, create, and search Apple Notes via AppleScript.
"""

import subprocess
from datetime import datetime


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "notes_list",
            "description": "List recent Apple Notes. Shows note titles and modification dates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "description": "Notes folder to list from (optional, default is all folders)"},
                    "limit": {"type": "integer", "description": "Max notes to return (default 15)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_read",
            "description": "Read the content of a specific Apple Note by its title.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Exact title of the note to read"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_create",
            "description": "Create a new Apple Note. Use when the user says 'make a note', 'save this to my notes', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Title for the new note"},
                    "body": {"type": "string", "description": "Content/body of the note"},
                    "folder": {"type": "string", "description": "Notes folder to create in (optional, defaults to 'Notes')"},
                },
                "required": ["title", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "notes_search",
            "description": "Search Apple Notes by keyword. Searches note titles and content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword or phrase"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": ["query"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_notes_list(folder: str = None, limit: int = 15) -> dict:
    try:
        if folder:
            escaped_folder = folder.replace('"', '\\"')
            script = f'''
            tell application "Notes"
                set noteList to {{}}
                try
                    set targetFolder to folder "{escaped_folder}"
                    set noteItems to notes of targetFolder
                    set maxCount to {limit}
                    set counter to 0
                    repeat with n in noteItems
                        if counter >= maxCount then exit repeat
                        set end of noteList to (name of n & "|||" & (modification date of n as string))
                        set counter to counter + 1
                    end repeat
                end try
                set AppleScript's text item delimiters to ":::"
                return noteList as string
            end tell
            '''
        else:
            script = f'''
            tell application "Notes"
                set noteList to {{}}
                set noteItems to every note
                set maxCount to {limit}
                set counter to 0
                repeat with n in noteItems
                    if counter >= maxCount then exit repeat
                    set end of noteList to (name of n & "|||" & (modification date of n as string))
                    set counter to counter + 1
                end repeat
                set AppleScript's text item delimiters to ":::"
                return noteList as string
            end tell
            '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return {"error": f"AppleScript error: {result.stderr.strip()}"}
        output = result.stdout.strip()
        notes = []
        if output:
            for entry in output.split(":::"):
                parts = entry.split("|||")
                if len(parts) >= 2:
                    notes.append({"title": parts[0].strip(), "modified": parts[1].strip()})
                elif parts[0].strip():
                    notes.append({"title": parts[0].strip()})
        return {"notes": notes, "count": len(notes), "folder": folder or "All"}
    except Exception as e:
        return {"error": f"Failed to list notes: {str(e)}"}


def tool_notes_read(title: str) -> dict:
    try:
        escaped_title = title.replace('"', '\\"')
        script = f'''
        tell application "Notes"
            set foundNote to missing value
            set noteItems to every note
            repeat with n in noteItems
                if name of n is "{escaped_title}" then
                    set foundNote to n
                    exit repeat
                end if
            end repeat
            if foundNote is missing value then
                return "NOTE_NOT_FOUND"
            else
                set noteBody to plaintext of foundNote
                set noteDate to modification date of foundNote as string
                return noteBody & "|||DATE:" & noteDate
            end if
        end tell
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return {"error": f"AppleScript error: {result.stderr.strip()}"}
        output = result.stdout.strip()
        if output == "NOTE_NOT_FOUND":
            return {"error": f"Note '{title}' not found. Use notes_list to see available notes."}
        parts = output.rsplit("|||DATE:", 1)
        content = parts[0] if parts else output
        modified = parts[1] if len(parts) > 1 else ""
        if len(content) > 15000:
            content = content[:15000] + "\n\n[... truncated ...]"
        return {"title": title, "content": content, "modified": modified, "char_count": len(content)}
    except Exception as e:
        return {"error": f"Failed to read note: {str(e)}"}


def tool_notes_create(title: str, body: str, folder: str = None) -> dict:
    try:
        escaped_title = title.replace('"', '\\"')
        # Convert body to HTML for Notes
        html_body = body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        html_body = html_body.replace("\n", "<br>")
        escaped_body = html_body.replace('"', '\\"')

        if folder:
            escaped_folder = folder.replace('"', '\\"')
            script = f'''
            tell application "Notes"
                tell folder "{escaped_folder}"
                    make new note with properties {{name:"{escaped_title}", body:"{escaped_body}"}}
                end tell
                return "OK"
            end tell
            '''
        else:
            script = f'''
            tell application "Notes"
                make new note with properties {{name:"{escaped_title}", body:"{escaped_body}"}}
                return "OK"
            end tell
            '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return {"status": "created", "title": title, "folder": folder or "Notes"}
        else:
            return {"error": f"Failed to create note: {result.stderr.strip()}"}
    except Exception as e:
        return {"error": f"Failed to create note: {str(e)}"}


def tool_notes_search(query: str, limit: int = 10) -> dict:
    try:
        escaped_query = query.replace('"', '\\"').lower()
        script = f'''
        tell application "Notes"
            set matchList to {{}}
            set noteItems to every note
            set maxCount to {limit}
            set counter to 0
            repeat with n in noteItems
                if counter >= maxCount then exit repeat
                set noteName to name of n
                set noteText to plaintext of n
                if noteText contains "{escaped_query}" or noteName contains "{escaped_query}" then
                    set end of matchList to (noteName & "|||" & (text 1 thru (min(200, length of noteText)) of noteText))
                    set counter to counter + 1
                end if
            end repeat
            set AppleScript's text item delimiters to ":::"
            return matchList as string
        end tell
        '''
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return {"error": f"AppleScript error: {result.stderr.strip()}"}
        output = result.stdout.strip()
        results = []
        if output:
            for entry in output.split(":::"):
                parts = entry.split("|||")
                if len(parts) >= 2:
                    results.append({"title": parts[0].strip(), "preview": parts[1].strip()})
                elif parts[0].strip():
                    results.append({"title": parts[0].strip()})
        return {"query": query, "results": results, "count": len(results)}
    except Exception as e:
        return {"error": f"Notes search failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "notes_list": lambda args: tool_notes_list(args.get("folder"), args.get("limit", 15)),
    "notes_read": lambda args: tool_notes_read(args.get("title", "")),
    "notes_create": lambda args: tool_notes_create(args.get("title", ""), args.get("body", ""), args.get("folder")),
    "notes_search": lambda args: tool_notes_search(args.get("query", ""), args.get("limit", 10)),
}
