"""
Apple Mail Skill — Read inbox, compose and send emails via AppleScript.
"""

import logging
import subprocess

logger = logging.getLogger("libre_bird.skills.email")


def _run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=20
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript failed")
    return result.stdout.strip()


def tool_check_inbox(args: dict) -> dict:
    """Check recent emails in the inbox."""
    count = min(int(args.get("count", 10)), 25)

    script = f'''
    tell application "Mail"
        set msgList to {{}}
        set inboxMessages to messages of inbox
        set maxCount to {count}
        if (count of inboxMessages) < maxCount then set maxCount to (count of inboxMessages)

        repeat with i from 1 to maxCount
            set msg to item i of inboxMessages
            set msgSender to sender of msg
            set msgSubject to subject of msg
            set msgDate to date received of msg
            set msgRead to read status of msg
            set readFlag to "📩"
            if msgRead then set readFlag to "✅"
            set msgInfo to readFlag & " " & msgSubject & " | " & msgSender & " | " & (msgDate as string)
            set end of msgList to msgInfo
        end repeat
        return msgList
    end tell
    '''
    try:
        raw = _run_applescript(script)
        messages = []
        if raw:
            for line in raw.split(", "):
                line = line.strip()
                if " | " in line:
                    parts = line.split(" | ")
                    messages.append({
                        "subject": parts[0].strip(),
                        "sender": parts[1].strip() if len(parts) > 1 else "",
                        "date": parts[2].strip() if len(parts) > 2 else ""
                    })
        return {"messages": messages, "count": len(messages)}
    except Exception as e:
        return {"error": str(e)}


def tool_read_email(args: dict) -> dict:
    """Read the content of a specific email by subject."""
    subject = args.get("subject", "").strip()
    if not subject:
        return {"error": "subject is required"}

    script = f'''
    tell application "Mail"
        set matchingMsgs to (messages of inbox whose subject contains "{subject}")
        if (count of matchingMsgs) is 0 then
            return "NOT_FOUND"
        end if
        set msg to item 1 of matchingMsgs
        set msgSubject to subject of msg
        set msgSender to sender of msg
        set msgDate to date received of msg
        set msgContent to content of msg
        -- Trim to 2000 chars
        if length of msgContent > 2000 then
            set msgContent to text 1 thru 2000 of msgContent & "... [truncated]"
        end if
        return "SUBJECT: " & msgSubject & "\\nFROM: " & msgSender & "\\nDATE: " & (msgDate as string) & "\\nBODY:\\n" & msgContent
    end tell
    '''
    try:
        raw = _run_applescript(script)
        if raw == "NOT_FOUND":
            return {"error": f"No email found matching '{subject}'"}
        return {"email": raw}
    except Exception as e:
        return {"error": str(e)}


def tool_compose_email(args: dict) -> dict:
    """Compose and optionally send an email."""
    to = args.get("to", "")
    subject = args.get("subject", "")
    body = args.get("body", "")
    send = args.get("send", False)

    if not to:
        return {"error": "to (recipient email) is required"}
    if not subject:
        return {"error": "subject is required"}

    # Escape for AppleScript
    body_escaped = body.replace('"', '\\"').replace('\n', '\\n')
    subject_escaped = subject.replace('"', '\\"')

    send_line = ""
    if send:
        send_line = "send newMsg"

    script = f'''
    tell application "Mail"
        set newMsg to make new outgoing message with properties {{subject:"{subject_escaped}", content:"{body_escaped}", visible:true}}
        tell newMsg
            make new to recipient at end of to recipients with properties {{address:"{to}"}}
        end tell
        {send_line}
        return "Composed email to {to}: {subject_escaped}"
    end tell
    '''
    try:
        result = _run_applescript(script)
        return {"success": True, "message": result, "sent": send}
    except Exception as e:
        return {"error": str(e)}


def tool_unread_count(args: dict) -> dict:
    """Get the count of unread emails."""
    script = '''
    tell application "Mail"
        set unreadCount to unread count of inbox
        return unreadCount as string
    end tell
    '''
    try:
        raw = _run_applescript(script)
        return {"unread_count": int(raw)}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "check_inbox",
            "description": "Check recent emails in the Apple Mail inbox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {"type": "integer", "description": "Number of recent emails to retrieve (default 10, max 25)"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_email",
            "description": "Read the full content of a specific email by subject line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Subject line (or part of it) to search for"}
                },
                "required": ["subject"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "compose_email",
            "description": "Compose a new email. Opens in Mail app by default, set send=true to send immediately.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Recipient email address"},
                    "subject": {"type": "string", "description": "Email subject"},
                    "body": {"type": "string", "description": "Email body text"},
                    "send": {"type": "boolean", "description": "If true, send immediately; otherwise open as draft (default false)"}
                },
                "required": ["to", "subject", "body"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "unread_count",
            "description": "Get the number of unread emails in the inbox.",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
]

TOOL_HANDLERS = {
    "check_inbox": tool_check_inbox,
    "read_email": tool_read_email,
    "compose_email": tool_compose_email,
    "unread_count": tool_unread_count,
}
