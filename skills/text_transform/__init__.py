"""
Text Transform Skill
Convert between formats and transform text.
Uses only Python stdlib — zero dependencies.
"""

import base64
import csv
import html
import io
import json
import logging
import re

logger = logging.getLogger("libre_bird.skills.text_transform")


def tool_markdown_to_html(args: dict) -> dict:
    """Convert Markdown text to HTML (basic subset)."""
    md = args.get("text", "")
    if not md:
        return {"error": "text is required"}

    lines = md.split("\n")
    html_parts = []
    in_code_block = False
    in_list = False

    for line in lines:
        # Code blocks
        if line.strip().startswith("```"):
            if in_code_block:
                html_parts.append("</code></pre>")
                in_code_block = False
            else:
                lang = line.strip().replace("```", "").strip()
                html_parts.append(f'<pre><code class="language-{lang}">' if lang else "<pre><code>")
                in_code_block = True
            continue

        if in_code_block:
            html_parts.append(html.escape(line))
            continue

        # Close list if not a list item
        if in_list and not re.match(r"^[\s]*[-*]\s", line):
            html_parts.append("</ul>")
            in_list = False

        # Headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)", line)
        if heading_match:
            level = len(heading_match.group(1))
            text = heading_match.group(2)
            html_parts.append(f"<h{level}>{text}</h{level}>")
            continue

        # Unordered lists
        list_match = re.match(r"^[\s]*[-*]\s+(.+)", line)
        if list_match:
            if not in_list:
                html_parts.append("<ul>")
                in_list = True
            html_parts.append(f"<li>{list_match.group(1)}</li>")
            continue

        # Horizontal rule
        if re.match(r"^---+$", line.strip()):
            html_parts.append("<hr>")
            continue

        # Empty line
        if not line.strip():
            html_parts.append("")
            continue

        # Inline formatting
        text = line
        text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
        text = re.sub(r"\*(.+?)\*", r"<em>\1</em>", text)
        text = re.sub(r"`(.+?)`", r"<code>\1</code>", text)
        text = re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', text)

        html_parts.append(f"<p>{text}</p>")

    if in_list:
        html_parts.append("</ul>")
    if in_code_block:
        html_parts.append("</code></pre>")

    result = "\n".join(html_parts)
    return {"html": result, "input_length": len(md), "output_length": len(result)}


def tool_json_prettify(args: dict) -> dict:
    """Prettify, minify, or validate JSON."""
    text = args.get("text", "")
    action = args.get("action", "prettify")

    if not text:
        return {"error": "text is required"}

    try:
        parsed = json.loads(text)

        if action == "minify":
            result = json.dumps(parsed, separators=(",", ":"))
        elif action == "sort":
            result = json.dumps(parsed, indent=2, sort_keys=True)
        else:
            result = json.dumps(parsed, indent=2)

        return {
            "result": result,
            "valid": True,
            "type": type(parsed).__name__,
            "keys": list(parsed.keys()) if isinstance(parsed, dict) else None,
            "length": len(parsed) if isinstance(parsed, (list, dict)) else None,
        }
    except json.JSONDecodeError as e:
        return {"valid": False, "error": f"Invalid JSON: {e}", "position": e.pos}


def tool_csv_to_json(args: dict) -> dict:
    """Convert CSV text to a JSON array of objects."""
    text = args.get("text", "")
    delimiter = args.get("delimiter", ",")

    if not text:
        return {"error": "text is required"}

    # If it's a file path, read it
    import os
    if os.path.exists(os.path.expanduser(text)):
        with open(os.path.expanduser(text), "r") as f:
            text = f.read()

    try:
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        rows = [row for row in reader]

        # Cap output
        truncated = len(rows) > 200
        rows = rows[:200]

        return {
            "rows": len(rows),
            "columns": list(rows[0].keys()) if rows else [],
            "data": rows,
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)}


def tool_text_case(args: dict) -> dict:
    """Convert text between cases: upper, lower, title, sentence, snake, camel, kebab."""
    text = args.get("text", "")
    case = args.get("case", "title")

    if not text:
        return {"error": "text is required"}

    if case == "upper":
        result = text.upper()
    elif case == "lower":
        result = text.lower()
    elif case == "title":
        result = text.title()
    elif case == "sentence":
        result = ". ".join(s.strip().capitalize() for s in text.split(".") if s.strip())
    elif case == "snake":
        result = re.sub(r"[\s\-]+", "_", text).lower()
        result = re.sub(r"[^\w_]", "", result)
    elif case == "camel":
        words = re.split(r"[\s_\-]+", text)
        result = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    elif case == "kebab":
        result = re.sub(r"[\s_]+", "-", text).lower()
        result = re.sub(r"[^\w\-]", "", result)
    elif case == "constant":
        result = re.sub(r"[\s\-]+", "_", text).upper()
        result = re.sub(r"[^\w_]", "", result)
    else:
        return {"error": f"Unknown case: {case}. Use: upper, lower, title, sentence, snake, camel, kebab, constant"}

    return {"original": text, "result": result, "case": case}


def tool_text_stats(args: dict) -> dict:
    """Get statistics about text: word count, char count, reading time, etc."""
    text = args.get("text", "")
    if not text:
        return {"error": "text is required"}

    words = text.split()
    sentences = re.split(r"[.!?]+", text)
    sentences = [s for s in sentences if s.strip()]
    paragraphs = [p for p in text.split("\n\n") if p.strip()]
    lines = text.count("\n") + 1

    # Unique words
    word_set = set(w.lower().strip(".,!?;:\"'()[]{}") for w in words)

    return {
        "characters": len(text),
        "characters_no_spaces": len(text.replace(" ", "").replace("\n", "")),
        "words": len(words),
        "unique_words": len(word_set),
        "sentences": len(sentences),
        "paragraphs": len(paragraphs),
        "lines": lines,
        "reading_time_minutes": round(len(words) / 200, 1),  # ~200 wpm
        "speaking_time_minutes": round(len(words) / 150, 1),  # ~150 wpm
        "avg_word_length": round(sum(len(w) for w in words) / max(1, len(words)), 1),
        "avg_sentence_length": round(len(words) / max(1, len(sentences)), 1),
    }


def tool_base64_encode_decode(args: dict) -> dict:
    """Encode or decode Base64 text."""
    text = args.get("text", "")
    action = args.get("action", "encode")

    if not text:
        return {"error": "text is required"}

    try:
        if action == "encode":
            result = base64.b64encode(text.encode("utf-8")).decode("ascii")
            return {"original_length": len(text), "encoded": result, "encoded_length": len(result)}
        elif action == "decode":
            result = base64.b64decode(text).decode("utf-8")
            return {"decoded": result, "decoded_length": len(result)}
        else:
            return {"error": "action must be 'encode' or 'decode'"}
    except Exception as e:
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "markdown_to_html",
            "description": "Convert Markdown text to HTML. Supports headings, bold, italic, code blocks, links, and lists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Markdown text to convert"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "json_prettify",
            "description": "Prettify, minify, sort, or validate JSON text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "JSON text"},
                    "action": {"type": "string", "enum": ["prettify", "minify", "sort"], "description": "Action (default prettify)"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "csv_to_json",
            "description": "Convert CSV text or file to a JSON array of objects.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "CSV text or file path"},
                    "delimiter": {"type": "string", "description": "Column delimiter (default comma)"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "text_case",
            "description": "Convert text between cases: upper, lower, title, sentence, snake_case, camelCase, kebab-case, CONSTANT_CASE.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to convert"},
                    "case": {"type": "string", "enum": ["upper", "lower", "title", "sentence", "snake", "camel", "kebab", "constant"], "description": "Target case"},
                },
                "required": ["text", "case"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "text_stats",
            "description": "Get detailed text statistics: word/char/sentence count, reading time, average lengths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to analyze"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "base64_encode_decode",
            "description": "Encode or decode Base64 text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to encode/decode"},
                    "action": {"type": "string", "enum": ["encode", "decode"], "description": "Action (default encode)"},
                },
                "required": ["text"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "markdown_to_html": tool_markdown_to_html,
    "json_prettify": tool_json_prettify,
    "csv_to_json": tool_csv_to_json,
    "text_case": tool_text_case,
    "text_stats": tool_text_stats,
    "base64_encode_decode": tool_base64_encode_decode,
}
