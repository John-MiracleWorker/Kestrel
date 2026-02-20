"""
Screen Analysis Skill
Read and analyze on-screen content using OCR.

Window title/app detection requires macOS — will raise a clear error on Linux/Docker.
"""

import os
import platform
import shutil
import subprocess
from datetime import datetime


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
            "name": "read_screen",
            "description": "Read all visible text from the user's screen using OCR. Use this when the user asks what's on their screen, wants you to read something they're looking at, or says 'look at this'. The screenshot is captured, OCR'd, and immediately discarded — nothing is stored.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_screen",
            "description": "Capture and analyze what's currently visible on the user's screen. Uses OCR to extract all visible text and describes the screen layout. Can also analyze a specific image file from disk. Use when the user asks 'what's on my screen?', 'what am I looking at?', 'read my screen', or wants you to analyze/describe an image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look for or focus on (e.g., 'error messages', 'the code', 'the email'). Optional — omit for general analysis.",
                    },
                    "image_path": {
                        "type": "string",
                        "description": "Optional path to an image file on disk to analyze instead of capturing the screen.",
                    },
                },
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

def _read_screen():
    try:
        from screen_ocr import read_screen
        return read_screen()
    except ImportError:
        return {"error": "screen_ocr module not available"}


def tool_analyze_screen(query: str = None, image_path: str = None) -> dict:
    if image_path:
        image_path = os.path.expanduser(image_path)
        if not os.path.exists(image_path):
            return {"error": f"Image file not found: {image_path}"}
        ext = os.path.splitext(image_path)[1].lower()
        if ext not in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".gif", ".webp"):
            return {"error": f"Unsupported image format: {ext}"}
        try:
            from screen_ocr import ocr_image, VISION_AVAILABLE
            if not VISION_AVAILABLE:
                return {"error": "macOS Vision framework not available. Install pyobjc-framework-Vision."}
            import Quartz
            from Foundation import NSURL
            file_url = NSURL.fileURLWithPath_(image_path)
            cg_source = Quartz.CGImageSourceCreateWithURL(file_url, None)
            if cg_source is None:
                return {"error": f"Could not load image: {image_path}"}
            cg_image = Quartz.CGImageSourceCreateImageAtIndex(cg_source, 0, None)
            if cg_image is None:
                return {"error": f"Could not decode image: {image_path}"}
            text = ocr_image(cg_image)
            del cg_image
            result = {
                "source": "image_file", "path": image_path,
                "text": text if text else "(No text detected in image)",
                "char_count": len(text) if text else 0,
                "timestamp": datetime.now().isoformat(),
            }
            if query:
                result["focus_query"] = query
                result["note"] = f"The user is specifically asking about: {query}. Focus your analysis on that."
            return result
        except Exception as e:
            return {"error": f"Image analysis failed: {str(e)}"}
    else:
        try:
            from screen_ocr import read_screen
        except ImportError:
            return {"error": "screen_ocr module not available"}
        screen_data = read_screen()
        if not screen_data.get("available"):
            return {
                "error": screen_data.get("error", "Screen capture not available"),
                "hint": "Check System Settings > Privacy & Security > Screen Recording permissions.",
            }
        text = screen_data.get("text", "")
        result = {
            "source": "screen_capture",
            "text": text if text else "(No text detected on screen)",
            "char_count": len(text) if text else 0,
            "timestamp": datetime.now().isoformat(),
        }
        # Active app/window title detection requires macOS + osascript
        if platform.system() == "Darwin" and shutil.which("osascript"):
            try:
                active_app = subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to get name of first application process whose frontmost is true'],
                    capture_output=True, text=True, timeout=3
                )
                if active_app.returncode == 0:
                    result["active_app"] = active_app.stdout.strip()
                window_title = subprocess.run(
                    ["osascript", "-e",
                     'tell application "System Events" to get name of front window of (first application process whose frontmost is true)'],
                    capture_output=True, text=True, timeout=3
                )
                if window_title.returncode == 0:
                    result["window_title"] = window_title.stdout.strip()
            except Exception:
                pass
        if query:
            result["focus_query"] = query
            result["note"] = f"The user is specifically asking about: {query}. Focus your analysis on that."
        else:
            result["note"] = "This is the OCR-extracted text from the user's screen. Describe what you see and answer any questions about it."
        return result


# ---------------------------------------------------------------------------
# Tool Handlers
# ---------------------------------------------------------------------------

TOOL_HANDLERS = {
    "read_screen": lambda args: _read_screen(),
    "analyze_screen": lambda args: tool_analyze_screen(args.get("query"), args.get("image_path")),
}
