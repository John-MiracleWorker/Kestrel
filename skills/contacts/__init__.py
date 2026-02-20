"""
Apple Contacts Skill — Search, list, and read contacts via AppleScript.

Requires macOS — will raise a clear error on Linux/Docker.
"""

import json
import logging
import platform
import shutil
import subprocess

logger = logging.getLogger("libre_bird.skills.contacts")


def _check_macos():
    """Raise a clear error if not running on macOS."""
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        raise RuntimeError(
            "This skill requires macOS with osascript. "
            "It cannot run in a Linux/Docker environment."
        )


def _run_applescript(script: str) -> str:
    _check_macos()
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript failed")
    return result.stdout.strip()


def tool_search_contacts(args: dict) -> dict:
    """Search contacts by name."""
    query = args.get("query", "").strip()
    if not query:
        return {"error": "query is required"}

    script = f'''
    tell application "Contacts"
        set matchList to {{}}
        set matchingPeople to (every person whose name contains "{query}")
        repeat with p in matchingPeople
            set pName to name of p
            set pEmail to ""
            set pPhone to ""
            try
                set pEmail to value of first email of p
            end try
            try
                set pPhone to value of first phone of p
            end try
            set pInfo to pName & " | " & pEmail & " | " & pPhone
            set end of matchList to pInfo
        end repeat
        return matchList
    end tell
    '''
    try:
        raw = _run_applescript(script)
        contacts = []
        if raw:
            for line in raw.split(","):
                parts = [p.strip() for p in line.split(" | ")]
                contacts.append({
                    "name": parts[0] if len(parts) > 0 else "",
                    "email": parts[1] if len(parts) > 1 else "",
                    "phone": parts[2] if len(parts) > 2 else ""
                })
        return {"contacts": contacts, "count": len(contacts), "query": query}
    except Exception as e:
        return {"error": str(e)}


def tool_get_contact_details(args: dict) -> dict:
    """Get full details for a specific contact."""
    name = args.get("name", "").strip()
    if not name:
        return {"error": "name is required"}

    script = f'''
    tell application "Contacts"
        set matchingPeople to (every person whose name is "{name}")
        if (count of matchingPeople) is 0 then
            return "NOT_FOUND"
        end if
        set p to item 1 of matchingPeople

        set pName to name of p
        set pOrg to ""
        set pTitle to ""
        set pNote to ""
        set pBday to ""
        try
            set pOrg to organization of p
        end try
        try
            set pTitle to job title of p
        end try
        try
            set pNote to note of p
        end try
        try
            set pBday to (birthday of p) as string
        end try

        set emailList to {{}}
        repeat with e in emails of p
            set end of emailList to (label of e & ": " & value of e)
        end repeat

        set phoneList to {{}}
        repeat with ph in phones of p
            set end of phoneList to (label of ph & ": " & value of ph)
        end repeat

        set addrList to {{}}
        repeat with a in addresses of p
            set end of addrList to (label of a & ": " & formatted address of a)
        end repeat

        set result to "NAME: " & pName & "\\n"
        if pOrg is not "" then set result to result & "ORG: " & pOrg & "\\n"
        if pTitle is not "" then set result to result & "TITLE: " & pTitle & "\\n"
        if pBday is not "" then set result to result & "BIRTHDAY: " & pBday & "\\n"

        set result to result & "EMAILS: " & (emailList as string) & "\\n"
        set result to result & "PHONES: " & (phoneList as string) & "\\n"
        set result to result & "ADDRESSES: " & (addrList as string) & "\\n"
        if pNote is not "" then set result to result & "NOTES: " & pNote

        return result
    end tell
    '''
    try:
        raw = _run_applescript(script)
        if raw == "NOT_FOUND":
            return {"error": f"Contact '{name}' not found"}

        details = {}
        for line in raw.split("\\n"):
            if ": " in line:
                key, val = line.split(": ", 1)
                details[key.lower()] = val
        return {"contact": details}
    except Exception as e:
        return {"error": str(e)}


def tool_create_contact(args: dict) -> dict:
    """Create a new contact."""
    first_name = args.get("first_name", "")
    last_name = args.get("last_name", "")
    email = args.get("email", "")
    phone = args.get("phone", "")
    organization = args.get("organization", "")

    if not first_name:
        return {"error": "first_name is required"}

    props = [f'first name:"{first_name}"']
    if last_name:
        props.append(f'last name:"{last_name}"')
    if organization:
        props.append(f'organization:"{organization}"')

    prop_str = ", ".join(props)

    email_line = ""
    if email:
        email_line = f'make new email at end of emails of newPerson with properties {{label:"work", value:"{email}"}}'

    phone_line = ""
    if phone:
        phone_line = f'make new phone at end of phones of newPerson with properties {{label:"mobile", value:"{phone}"}}'

    script = f'''
    tell application "Contacts"
        set newPerson to make new person with properties {{{prop_str}}}
        {email_line}
        {phone_line}
        save
        return "Created: " & name of newPerson
    end tell
    '''
    try:
        result = _run_applescript(script)
        return {"success": True, "message": result}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_contacts",
            "description": "Search contacts by name. Returns matching names, emails, and phone numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Name or partial name to search for"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_contact_details",
            "description": "Get full details for a specific contact including all emails, phones, addresses, birthday.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Exact full name of the contact"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "create_contact",
            "description": "Create a new contact in Apple Contacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string", "description": "First name (required)"},
                    "last_name": {"type": "string", "description": "Last name"},
                    "email": {"type": "string", "description": "Email address"},
                    "phone": {"type": "string", "description": "Phone number"},
                    "organization": {"type": "string", "description": "Company or organization"}
                },
                "required": ["first_name"]
            }
        }
    },
]

TOOL_HANDLERS = {
    "search_contacts": tool_search_contacts,
    "get_contact_details": tool_get_contact_details,
    "create_contact": tool_create_contact,
}
