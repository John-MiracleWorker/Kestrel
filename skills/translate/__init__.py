"""
Translation Skill
Translate text between languages using the free MyMemory API.
Uses only stdlib — zero dependencies.
Also supports DeepL if DEEPL_API_KEY is set in .env for higher quality.
"""

import json
import logging
import os
import urllib.request
import urllib.parse
import urllib.error

logger = logging.getLogger("libre_bird.skills.translate")

_MYMEMORY_API = "https://api.mymemory.translated.net/get"
_DEEPL_API = "https://api-free.deepl.com/v2"

# Common language codes for reference
LANGUAGES = {
    "en": "English", "es": "Spanish", "fr": "French", "de": "German",
    "it": "Italian", "pt": "Portuguese", "nl": "Dutch", "ru": "Russian",
    "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "ar": "Arabic",
    "hi": "Hindi", "tr": "Turkish", "pl": "Polish", "sv": "Swedish",
    "da": "Danish", "no": "Norwegian", "fi": "Finnish", "el": "Greek",
    "cs": "Czech", "ro": "Romanian", "hu": "Hungarian", "uk": "Ukrainian",
}


def _translate_deepl(text: str, source: str, target: str) -> dict:
    """Translate using DeepL (if API key available)."""
    api_key = os.environ.get("DEEPL_API_KEY", "")
    if not api_key:
        return None  # Fall back to MyMemory

    data = urllib.parse.urlencode({
        "text": text,
        "target_lang": target.upper(),
        "source_lang": source.upper() if source else "",
        "auth_key": api_key,
    }).encode()

    try:
        req = urllib.request.Request(f"{_DEEPL_API}/translate", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())
            translations = result.get("translations", [])
            if translations:
                return {
                    "translated_text": translations[0]["text"],
                    "detected_source": translations[0].get("detected_source_language", source).lower(),
                    "engine": "DeepL",
                }
    except Exception as e:
        logger.warning(f"DeepL failed, falling back to MyMemory: {e}")
    return None


def _translate_mymemory(text: str, source: str, target: str) -> dict:
    """Translate using free MyMemory API (no key required, 5000 chars/day)."""
    langpair = f"{source}|{target}"
    params = urllib.parse.urlencode({"q": text, "langpair": langpair})
    url = f"{_MYMEMORY_API}?{params}"

    req = urllib.request.Request(url, headers={"User-Agent": "LibreBird/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())

    response_data = result.get("responseData", {})
    translated = response_data.get("translatedText", "")
    match_pct = response_data.get("match", 0)

    return {
        "translated_text": translated,
        "confidence": match_pct,
        "engine": "MyMemory",
    }


def tool_translate_text(args: dict) -> dict:
    """Translate text from one language to another."""
    text = args.get("text", "").strip()
    if not text:
        return {"error": "text is required"}

    source = args.get("source", "en").lower()[:2]
    target = args.get("target", "es").lower()[:2]

    if source == target:
        return {"translated_text": text, "note": "Source and target are the same language"}

    # Truncate very long text
    if len(text) > 3000:
        text = text[:3000]
        truncated = True
    else:
        truncated = False

    try:
        # Try DeepL first (higher quality)
        result = _translate_deepl(text, source, target)
        if result is None:
            result = _translate_mymemory(text, source, target)

        result["source_lang"] = LANGUAGES.get(source, source)
        result["target_lang"] = LANGUAGES.get(target, target)
        result["original_length"] = len(text)
        if truncated:
            result["note"] = "Text was truncated to 3000 characters"
        return result
    except Exception as e:
        return {"error": str(e)}


def tool_detect_language(args: dict) -> dict:
    """Detect the language of a text snippet."""
    text = args.get("text", "").strip()
    if not text:
        return {"error": "text is required"}

    # Use MyMemory with autodetect by translating to English
    sample = text[:200]  # Short sample is enough

    try:
        params = urllib.parse.urlencode({"q": sample, "langpair": "autodetect|en"})
        url = f"{_MYMEMORY_API}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "LibreBird/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read().decode())

        response_data = result.get("responseData", {})
        detected = response_data.get("detectedLanguage", "")

        if isinstance(detected, str):
            lang_code = detected
            confidence = None
        else:
            lang_code = detected.get("language", "unknown") if detected else "unknown"
            confidence = detected.get("confidence", None) if detected else None

        return {
            "detected_language": lang_code,
            "language_name": LANGUAGES.get(lang_code, lang_code),
            "confidence": confidence,
            "sample": sample[:100],
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
            "name": "translate_text",
            "description": "Translate text from one language to another. Uses DeepL (if API key set) or free MyMemory API.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to translate"},
                    "source": {"type": "string", "description": "Source language code (e.g. 'en', 'fr', 'de'). Default 'en'."},
                    "target": {"type": "string", "description": "Target language code (e.g. 'es', 'ja', 'zh'). Default 'es'."},
                },
                "required": ["text", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_language",
            "description": "Detect the language of a text snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to analyze"},
                },
                "required": ["text"],
            },
        },
    },
]

TOOL_HANDLERS = {
    "translate_text": tool_translate_text,
    "detect_language": tool_detect_language,
}
