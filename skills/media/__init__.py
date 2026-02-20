"""
Media & Music Skill
Apple Music control, text-to-speech, AI image generation.

Music and TTS features require macOS — will raise a clear error on Linux/Docker.
"""

import json
import os
import platform
import shutil
import subprocess


def _check_macos():
    """Raise a clear error if not running on macOS."""
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        raise RuntimeError(
            "This skill requires macOS with osascript. "
            "It cannot run in a Linux/Docker environment."
        )


# ---------------------------------------------------------------------------
# Tool Definitions
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "music_control",
            "description": "Control Apple Music: play/pause, skip, search songs, get current track, adjust volume, toggle repeat/shuffle. Use when user says 'play music', 'what song is this', 'skip this song', 'pause', 'play [artist/song]', 'turn up the volume', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "toggle", "next", "previous", "status",
                                 "search_play", "volume_up", "volume_down", "set_volume",
                                 "shuffle_on", "shuffle_off", "repeat_one", "repeat_all", "repeat_off",
                                 "queue", "add_to_library"],
                        "description": "The music control action to perform",
                    },
                    "query": {"type": "string", "description": "Search query for 'search_play' action (e.g., 'Bohemian Rhapsody', 'Drake')"},
                    "volume": {"type": "number", "description": "Volume level 0-100 for 'set_volume' action"},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "Speak text aloud using text-to-speech (macOS 'say' command or MLX TTS if available). Use when user asks you to 'say something out loud', 'read this aloud', 'announce', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "The text to speak aloud"},
                    "voice": {"type": "string", "description": "macOS voice name (optional, e.g. 'Samantha', 'Alex', 'Daniel')"},
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_generate",
            "description": "Generate an image from a text description using the local MLX Stable Diffusion pipeline. Takes 30-90 seconds on Apple Silicon. Use when user says 'generate an image of', 'create a picture', 'draw me', etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Detailed description of the image to generate"},
                    "negative_prompt": {"type": "string", "description": "What to avoid in the image (optional)"},
                    "steps": {"type": "integer", "description": "Number of diffusion steps (default 20, more = better quality but slower)"},
                },
                "required": ["prompt"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def tool_music_control(action: str, query: str = None, volume: int = None) -> dict:
    _check_macos()
    try:
        if action == "toggle":
            script = 'tell application "Music" to playpause'
        elif action == "play":
            script = 'tell application "Music" to play'
        elif action == "pause":
            script = 'tell application "Music" to pause'
        elif action == "next":
            script = 'tell application "Music" to next track'
        elif action == "previous":
            script = 'tell application "Music" to previous track'
        elif action == "status":
            script = '''
            tell application "Music"
                if player state is playing then
                    set trackName to name of current track
                    set trackArtist to artist of current track
                    set trackAlbum to album of current track
                    set trackDuration to duration of current track
                    set playerPos to player position
                    set vol to sound volume
                    return "PLAYING|" & trackName & "|" & trackArtist & "|" & trackAlbum & "|" & (round (trackDuration)) & "|" & (round (playerPos)) & "|" & vol
                else
                    return "STOPPED|||||||"
                end if
            end tell
            '''
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                parts = result.stdout.strip().split("|")
                if parts[0] == "PLAYING" and len(parts) >= 7:
                    return {
                        "status": "playing", "track": parts[1], "artist": parts[2],
                        "album": parts[3], "duration_seconds": int(parts[4]) if parts[4] else 0,
                        "position_seconds": int(parts[5]) if parts[5] else 0, "volume": int(parts[6]) if parts[6] else 50,
                    }
                else:
                    return {"status": "stopped"}
            else:
                return {"status": "unavailable", "error": result.stderr.strip()}
        elif action == "search_play":
            if not query:
                return {"error": "query is required for search_play"}
            escaped_query = query.replace('"', '\\"')
            script = f'''
            tell application "Music"
                set searchResults to search playlist "Library" for "{escaped_query}"
                if (count of searchResults) > 0 then
                    play item 1 of searchResults
                    set trackName to name of current track
                    set trackArtist to artist of current track
                    return trackName & " by " & trackArtist
                else
                    return "NOT_FOUND"
                end if
            end tell
            '''
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                output = result.stdout.strip()
                if output == "NOT_FOUND":
                    return {"action": "search_play", "query": query, "found": False,
                            "message": f"No results found in library for '{query}'. Try searching Apple Music."}
                return {"action": "search_play", "query": query, "found": True, "now_playing": output}
            else:
                return {"error": result.stderr.strip()}
        elif action == "volume_up":
            script = 'tell application "Music" to set sound volume to (sound volume + 10)'
        elif action == "volume_down":
            script = 'tell application "Music" to set sound volume to (sound volume - 10)'
        elif action == "set_volume":
            vol = max(0, min(100, volume or 50))
            script = f'tell application "Music" to set sound volume to {vol}'
        elif action == "shuffle_on":
            script = 'tell application "Music" to set shuffle enabled to true'
        elif action == "shuffle_off":
            script = 'tell application "Music" to set shuffle enabled to false'
        elif action == "repeat_one":
            script = 'tell application "Music" to set song repeat to one'
        elif action == "repeat_all":
            script = 'tell application "Music" to set song repeat to all'
        elif action == "repeat_off":
            script = 'tell application "Music" to set song repeat to off'
        elif action == "queue":
            script = '''
            tell application "Music"
                set queueTracks to {}
                try
                    set nextTracks to next tracks
                    repeat with t in nextTracks
                        set end of queueTracks to (name of t & " - " & artist of t)
                    end repeat
                end try
                if (count of queueTracks) = 0 then
                    return "EMPTY"
                else
                    set AppleScript's text item delimiters to "||"
                    return queueTracks as string
                end if
            end tell
            '''
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                output = result.stdout.strip()
                if output == "EMPTY":
                    return {"action": "queue", "tracks": [], "message": "Queue is empty"}
                tracks = output.split("||")
                return {"action": "queue", "tracks": tracks[:10], "count": len(tracks)}
            else:
                return {"error": result.stderr.strip()}
        elif action == "add_to_library":
            return {"action": "add_to_library", "status": "not_implemented",
                    "message": "Adding to library requires the Apple Music API."}
        else:
            return {"error": f"Unknown music action: {action}"}

        if action in ("play", "pause", "next", "previous", "toggle",
                       "volume_up", "volume_down", "set_volume",
                       "shuffle_on", "shuffle_off", "repeat_one", "repeat_all", "repeat_off"):
            result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return {"action": action, "status": "success"}
            else:
                return {"action": action, "error": result.stderr.strip()}
    except Exception as e:
        return {"error": f"Music control failed: {str(e)}"}


def tool_speak(text: str, voice: str = None) -> dict:
    try:
        try:
            from tts_engine import tts_engine
            if tts_engine and hasattr(tts_engine, 'speak'):
                tts_engine.speak(text)
                return {"status": "speaking", "text": text[:100], "engine": "mlx_tts"}
        except ImportError:
            pass
        _check_macos()
        cmd = ["say"]
        if voice:
            cmd.extend(["-v", voice])
        cmd.append(text)
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"status": "speaking", "text": text[:100], "engine": "macos_say", "voice": voice or "default"}
    except Exception as e:
        return {"error": f"TTS failed: {str(e)}"}


def tool_image_generate(prompt: str, negative_prompt: str = None, steps: int = 20) -> dict:
    try:
        try:
            from image_gen import generate_image
        except ImportError:
            return {"error": "Image generation not available. MLX Stable Diffusion not installed."}
        result = generate_image(prompt, negative_prompt=negative_prompt, num_steps=steps)
        return result
    except Exception as e:
        return {"error": f"Image generation failed: {str(e)}"}


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "music_control": lambda args: tool_music_control(
        args.get("action", "status"), args.get("query"), args.get("volume")
    ),
    "speak": lambda args: tool_speak(args.get("text", ""), args.get("voice")),
    "image_generate": lambda args: tool_image_generate(
        args.get("prompt", ""), args.get("negative_prompt"), args.get("steps", 20)
    ),
}
