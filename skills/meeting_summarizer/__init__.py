"""
Meeting Summarizer Skill
Analyze meeting transcripts to extract summaries, action items, and key decisions.
Works by structuring raw transcript text — the actual summarization
is delegated to the LLM (which calls this tool to parse/chunk the text
and then uses its own reasoning to generate summaries).

Also reads common transcript files and produces the structured output
that the LLM can then refine.
Zero dependencies — uses only stdlib.
"""

import json
import logging
import os
import re
from datetime import datetime

logger = logging.getLogger("libre_bird.skills.meeting_summarizer")

_OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data", "meetings")


def _ensure_dir():
    os.makedirs(_OUTPUT_DIR, exist_ok=True)


def _read_transcript(source: str) -> str:
    """Read a transcript from a file path or raw text."""
    source = source.strip()

    # If it looks like a file path, try to read it
    if os.path.exists(os.path.expanduser(source)):
        path = os.path.expanduser(source)
        ext = os.path.splitext(path)[1].lower()

        if ext in (".txt", ".md", ".vtt", ".srt"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        elif ext == ".json":
            with open(path, "r") as f:
                data = json.load(f)
            # Handle common transcript JSON formats
            if isinstance(data, list):
                return "\n".join(
                    f"{item.get('speaker', 'Unknown')}: {item.get('text', '')}"
                    for item in data if isinstance(item, dict)
                )
            elif isinstance(data, dict) and "text" in data:
                return data["text"]
            else:
                return json.dumps(data, indent=2)
        else:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()

    # Otherwise treat as raw text
    return source


def _clean_vtt_srt(text: str) -> str:
    """Clean WebVTT or SRT timestamp lines."""
    lines = text.split("\n")
    cleaned = []
    for line in lines:
        line = line.strip()
        # Skip timestamp lines (00:00:00.000 --> 00:00:05.000)
        if re.match(r"^\d{2}:\d{2}:\d{2}", line):
            continue
        # Skip numeric-only lines (SRT sequence numbers)
        if re.match(r"^\d+$", line):
            continue
        # Skip WEBVTT header
        if line.upper().startswith("WEBVTT"):
            continue
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


def _extract_speakers(text: str) -> list:
    """Extract unique speaker names from transcript."""
    speakers = set()
    for match in re.finditer(r"^(\w[\w\s]{0,30}):", text, re.MULTILINE):
        name = match.group(1).strip()
        if len(name) > 1 and name.lower() not in ("http", "https", "note", "action"):
            speakers.add(name)
    return sorted(speakers)


def _word_count(text: str) -> int:
    return len(text.split())


def tool_summarize_meeting(args: dict) -> dict:
    """Parse and structure a meeting transcript for summarization."""
    source = args.get("transcript", "")
    title = args.get("title", "Meeting")

    if not source:
        return {"error": "transcript is required — provide file path or paste the text"}

    try:
        raw_text = _read_transcript(source)

        # Clean if VTT/SRT format
        if "WEBVTT" in raw_text[:50] or re.match(r"^\d+\n\d{2}:\d{2}:", raw_text):
            raw_text = _clean_vtt_srt(raw_text)

        speakers = _extract_speakers(raw_text)
        words = _word_count(raw_text)
        estimated_minutes = round(words / 150)  # ~150 words per minute speech

        # Chunk for context window
        max_chars = 12000
        if len(raw_text) > max_chars:
            transcript_text = raw_text[:max_chars] + f"\n\n... [transcript truncated — {len(raw_text)} chars total, showing first {max_chars}]"
        else:
            transcript_text = raw_text

        # Save the full transcript
        _ensure_dir()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        save_path = os.path.join(_OUTPUT_DIR, f"{timestamp}_{title.replace(' ', '_')}.txt")
        with open(save_path, "w") as f:
            f.write(raw_text)

        return {
            "title": title,
            "word_count": words,
            "estimated_duration_minutes": estimated_minutes,
            "speakers": speakers,
            "speaker_count": len(speakers),
            "transcript": transcript_text,
            "saved_to": save_path,
            "instructions_for_llm": (
                "Please analyze this meeting transcript and provide:\n"
                "1. **Executive Summary** (2-3 sentences)\n"
                "2. **Key Discussion Points** (bullet list)\n"
                "3. **Decisions Made** (bullet list)\n"
                "4. **Action Items** (who, what, when)\n"
                "5. **Follow-up Needed** (any unresolved topics)"
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def tool_extract_action_items(args: dict) -> dict:
    """Extract potential action items from meeting text using keyword patterns."""
    source = args.get("transcript", "")
    if not source:
        return {"error": "transcript is required"}

    try:
        text = _read_transcript(source)

        # Pattern-based extraction
        action_patterns = [
            r"(?:action item|todo|to-do|task)[\s:]+(.+?)(?:\n|$)",
            r"(?:will|should|needs? to|has to|must)\s+(.+?)(?:\.|$)",
            r"(?:please|pls)\s+(.+?)(?:\.|$)",
            r"(?:follow up|follow-up|followup)[\s:]+(.+?)(?:\n|$)",
            r"(?:deadline|due|by)\s+(.+?)(?:\.|$)",
        ]

        potential_actions = []
        for pattern in action_patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE | re.MULTILINE):
                item = match.group(1).strip()
                if 5 < len(item) < 200:  # Filter noise
                    potential_actions.append(item)

        # Deduplicate
        seen = set()
        unique_actions = []
        for action in potential_actions:
            key = action.lower()[:50]
            if key not in seen:
                seen.add(key)
                unique_actions.append(action)

        return {
            "action_items_found": len(unique_actions),
            "items": unique_actions[:20],  # Cap at 20
            "note": "These are pattern-based extractions. The LLM should review and refine them.",
        }
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "summarize_meeting",
            "description": "Parse a meeting transcript (from file or pasted text) and structure it for summarization. Supports .txt, .md, .vtt, .srt, and .json formats.",
            "parameters": {
                "type": "object",
                "properties": {
                    "transcript": {"type": "string", "description": "File path to transcript or the raw transcript text"},
                    "title": {"type": "string", "description": "Meeting title (optional)"},
                },
                "required": ["transcript"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_action_items",
            "description": "Extract potential action items and tasks from a meeting transcript using keyword patterns.",
            "parameters": {
                "type": "object",
                "properties": {
                    "transcript": {"type": "string", "description": "File path to transcript or raw text"},
                },
                "required": ["transcript"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "summarize_meeting": tool_summarize_meeting,
    "extract_action_items": tool_extract_action_items,
}
