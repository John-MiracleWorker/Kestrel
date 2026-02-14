"""
Libre Bird — Text-to-Speech via macOS Neural Voices.
Uses the built-in `say` command with high-quality neural voices.
All speech is synthesized locally — nothing leaves the device.
"""

import logging
import subprocess
import threading
from typing import Optional

logger = logging.getLogger("libre_bird.tts")

# Preferred voices in order (neural/premium voices available on macOS 13+)
# Users can install more via System Settings > Accessibility > Spoken Content
PREFERRED_VOICES = [
    "Zoe (Premium)",      # Natural female voice
    "Samantha (Premium)",  # Natural female voice
    "Zoe",
    "Samantha",
    "Karen (Premium)",     # Australian English
    "Daniel (Premium)",    # British English
    "Karen",
    "Samantha",            # Default fallback
]

_selected_voice: Optional[str] = None
_speaking_lock = threading.Lock()
_current_process: Optional[subprocess.Popen] = None


def _find_best_voice() -> str:
    """Find the best available voice on the system."""
    global _selected_voice
    if _selected_voice:
        return _selected_voice

    try:
        result = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        available = result.stdout.lower()

        for voice in PREFERRED_VOICES:
            if voice.lower().split(" (")[0] in available:
                _selected_voice = voice.split(" (")[0]  # Use base name
                logger.info(f"TTS voice selected: {_selected_voice}")
                return _selected_voice

        # Fallback to system default
        _selected_voice = "Samantha"
        return _selected_voice

    except Exception as e:
        logger.error(f"Failed to query voices: {e}")
        _selected_voice = "Samantha"
        return _selected_voice


def speak(text: str, voice: str = None, rate: int = 190) -> bool:
    """
    Speak text aloud using macOS `say` command.

    Args:
        text: The text to speak
        voice: Voice name (None = auto-select best)
        rate: Words per minute (default 190, natural pace)
    """
    global _current_process

    if not text or not text.strip():
        return False

    voice = voice or _find_best_voice()

    # Clean text for speech (remove markdown, emojis, etc.)
    clean = _clean_for_speech(text)
    if not clean:
        return False

    def _speak_async():
        global _current_process
        with _speaking_lock:
            try:
                _current_process = subprocess.Popen(
                    ["say", "-v", voice, "-r", str(rate), clean],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                _current_process.wait()
                _current_process = None
            except Exception as e:
                logger.error(f"TTS error: {e}")
                _current_process = None

    thread = threading.Thread(target=_speak_async, daemon=True)
    thread.start()
    return True


def stop_speaking():
    """Stop any current speech output."""
    global _current_process
    if _current_process:
        try:
            _current_process.terminate()
            _current_process = None
            logger.info("TTS stopped")
        except Exception:
            pass

    # Also kill any orphaned `say` processes
    try:
        subprocess.run(["killall", "say"], capture_output=True, timeout=2)
    except Exception:
        pass


def is_speaking() -> bool:
    """Check if TTS is currently outputting speech."""
    return _current_process is not None and _current_process.poll() is None


def _clean_for_speech(text: str) -> str:
    """Clean text for natural speech output."""
    import re

    # Remove markdown formatting
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)   # bold
    text = re.sub(r"\*(.+?)\*", r"\1", text)        # italic
    text = re.sub(r"`(.+?)`", r"\1", text)          # inline code
    text = re.sub(r"```[\s\S]*?```", "", text)       # code blocks
    text = re.sub(r"#{1,6}\s*", "", text)            # headers
    text = re.sub(r"\[(.+?)\]\(.+?\)", r"\1", text)  # links

    # Remove emojis (basic range)
    text = re.sub(
        r"[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff"
        r"\U0001f900-\U0001f9ff\U00002702-\U000027b0\U0000fe0f]",
        "",
        text,
    )

    # Clean up whitespace
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\n", " ", text)
    text = re.sub(r"\s{2,}", " ", text)

    return text.strip()
