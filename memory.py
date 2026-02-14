"""
Libre Bird — RAG Memory Retrieval
Searches historical context snapshots to answer recall questions
without filling the LLM context window.
"""

import re
from datetime import datetime, timedelta
from typing import Optional


# ── Intent Detection ─────────────────────────────────────────────────

RECALL_PATTERNS = [
    # Direct recall questions
    r"\bwhat was i\b",
    r"\bwhat were (you|we) (looking|working|doing)\b",
    r"\bwhat did i\b",
    r"\bwhat have i\b",
    r"\bwhat have been\b",
    r"\bwhat app\b",
    r"\bwhich app\b",
    r"\bwhat project\b",
    r"\bwhat file\b",
    r"\bwhat code\b",
    r"\bwhat site\b",
    r"\bwhat page\b",
    # Time-based
    r"\bthis morning\b",
    r"\bthis afternoon\b",
    r"\bthis evening\b",
    r"\bearlier today\b",
    r"\byesterday\b",
    r"\blast night\b",
    r"\blast hour\b",
    r"\bpast hour\b",
    r"\bover the past\b",
    r"\btoday\b",
    r"\bat \d{1,2}\s?(am|pm|o'clock)\b",
    r"\baround \d{1,2}\b",
    r"\bbefore lunch\b",
    r"\bafter lunch\b",
    # Search-like
    r"\bfind (that|the|my)\b",
    r"\bremember when\b",
    r"\brecall\b",
    r"\bwhen did i\b",
    r"\bwhen was i\b",
    r"\bshow me what\b",
    r"\bwhat happened\b",
    r"\bactivity\b.*\b(today|yesterday|morning|afternoon)\b",
    r"\bhistory\b",
    r"\bworking on\b.*\b(before|earlier|ago|past|lately|recently)\b",
    r"\b(been|have been) working on\b",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in RECALL_PATTERNS]


def detect_recall_intent(message: str) -> bool:
    """Check if the user message is asking about past activity."""
    return any(p.search(message) for p in _COMPILED)


# ── Time Parsing ─────────────────────────────────────────────────────

def extract_time_range(message: str) -> tuple[Optional[str], Optional[str]]:
    """
    Parse relative time references from the message.
    Returns (start, end) as 'YYYY-MM-DD HH:MM:SS' strings matching the DB
    format, or (None, None) if no time hint found.
    """
    FMT = '%Y-%m-%d %H:%M:%S'
    now = datetime.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    msg = message.lower()

    # "this morning" → today 5am-12pm
    if "this morning" in msg or "before lunch" in msg:
        start = today.replace(hour=5)
        end = today.replace(hour=12)
        return start.strftime(FMT), end.strftime(FMT)

    # "this afternoon" / "after lunch" → today 12pm-6pm
    if "this afternoon" in msg or "after lunch" in msg:
        start = today.replace(hour=12)
        end = today.replace(hour=18)
        return start.strftime(FMT), end.strftime(FMT)

    # "this evening" / "tonight" → today 6pm-midnight
    if "this evening" in msg or "tonight" in msg:
        start = today.replace(hour=18)
        end = today.replace(hour=23, minute=59)
        return start.strftime(FMT), end.strftime(FMT)

    # "last night" → yesterday 8pm - today 2am
    if "last night" in msg:
        yesterday = today - timedelta(days=1)
        start = yesterday.replace(hour=20)
        end = today.replace(hour=2)
        return start.strftime(FMT), end.strftime(FMT)

    # "yesterday" → full previous day
    if "yesterday" in msg:
        yesterday = today - timedelta(days=1)
        end = yesterday.replace(hour=23, minute=59, second=59)
        return yesterday.strftime(FMT), end.strftime(FMT)

    # "last hour" / "past hour"
    if "last hour" in msg or "past hour" in msg:
        start = now - timedelta(hours=1)
        return start.strftime(FMT), now.strftime(FMT)

    # "at Xpm" / "at Xam" / "around X" → ±30 minute window
    time_match = re.search(r'at (\d{1,2})\s?(am|pm|o\'clock)', msg)
    if not time_match:
        time_match = re.search(r'around (\d{1,2})\s?(am|pm|o\'clock)?', msg)
    if time_match:
        hour = int(time_match.group(1))
        period = time_match.group(2) if time_match.group(2) else None
        if period and "pm" in period and hour < 12:
            hour += 12
        elif period and "am" in period and hour == 12:
            hour = 0
        target = today.replace(hour=min(hour, 23))
        start = target - timedelta(minutes=30)
        end = target + timedelta(minutes=30)
        return start.strftime(FMT), end.strftime(FMT)

    # "earlier today" / "today" → from midnight to now
    if "earlier" in msg or "today" in msg:
        return today.strftime(FMT), now.strftime(FMT)

    # No specific time detected
    return None, None


# ── Search Term Extraction ───────────────────────────────────────────

def extract_search_terms(message: str) -> Optional[str]:
    """
    Pull meaningful search terms from the message for FTS5 search.
    Strips out common filler words to focus on content keywords.
    """
    STOP_WORDS = {
        "what", "was", "i", "were", "we", "you", "doing", "working",
        "looking", "at", "on", "the", "a", "an", "in", "my", "me",
        "did", "have", "been", "find", "that", "show", "tell",
        "about", "earlier", "before", "after", "this", "morning",
        "afternoon", "evening", "today", "yesterday", "last", "night",
        "hour", "when", "where", "which", "how", "remember", "recall",
        "can", "could", "please", "around", "ago", "recently", "just",
        "history", "activity", "happened", "something", "anything",
    }

    # Tokenize and filter
    words = re.findall(r'\b[a-zA-Z0-9_.-]+\b', message.lower())
    terms = [w for w in words if w not in STOP_WORDS and len(w) > 1]

    if not terms:
        return None

    # FTS5 query: OR together all terms
    return " OR ".join(terms)


# ── Main Retrieval ───────────────────────────────────────────────────

def compress_snapshots(snapshots: list[dict], max_entries: int = 15) -> str:
    """
    Compress context snapshots into a token-efficient string.
    ~30-40 tokens per entry → ~500 tokens for 15 entries.
    """
    if not snapshots:
        return ""

    lines = []
    seen = set()

    for snap in snapshots[:max_entries * 2]:  # Over-fetch to handle dedup
        # Compact representation
        ts = snap.get("timestamp", "?")
        # Extract just the time portion if it's a full datetime
        if "T" in ts:
            ts = ts.split("T")[1][:5]  # "HH:MM"
        elif " " in ts:
            ts = ts.split(" ")[1][:5]

        app = snap.get("app_name", "?")
        window = snap.get("window_title", "")

        # Dedup by app+window
        key = f"{app}|{window}"
        if key in seen:
            continue
        seen.add(key)

        line = f"[{ts}] {app}"
        if window:
            line += f" — {window}"
        lines.append(line)

        if len(lines) >= max_entries:
            break

    return "\n".join(lines)


async def retrieve_context(db, message: str) -> str:
    """
    Search historical context snapshots relevant to the user's question.
    Falls back to long-term memories if no recent snapshots match.
    Returns a compressed, token-efficient summary string.
    """
    results = []

    # 1. Try time-range search on recent snapshots
    start, end = extract_time_range(message)
    if start and end:
        results = await db.get_context_for_timerange(start, end, limit=50)

    # 2. Try FTS keyword search on snapshots
    terms = extract_search_terms(message)
    if terms:
        try:
            fts_results = await db.search_context(terms, limit=30)
            seen_ids = {r["id"] for r in results}
            for r in fts_results:
                if r["id"] not in seen_ids:
                    results.append(r)
        except Exception:
            pass

    # 3. If snapshots found, return compressed version
    if results:
        return compress_snapshots(results)

    # 4. Fallback: search long-term memories
    if terms:
        try:
            memories = await db.search_memories(terms, limit=10)
            if memories:
                lines = []
                for mem in memories:
                    d = mem.get("memory_date", "?")
                    summary = mem.get("summary", "")
                    # Compact format: date + first ~200 chars of summary
                    preview = summary[:200].replace("\n", " | ")
                    lines.append(f"[{d}] {preview}")
                return "Long-term memories:\n" + "\n".join(lines)
        except Exception:
            pass

    # 5. Nothing found — let the model say "I don't know"
    return ""

