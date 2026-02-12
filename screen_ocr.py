"""
Libre Bird — On-Demand Screen OCR using macOS Vision Framework.
Captures a screenshot, extracts all visible text via Neural Engine OCR,
then discards the image. Zero RAM cost, no ML model needed.
Requires: pyobjc-framework-Vision, pyobjc-framework-Quartz
"""

import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger("libre_bird.ocr")

try:
    import Quartz
    from Foundation import NSArray
    import Vision
    VISION_AVAILABLE = True
except ImportError:
    VISION_AVAILABLE = False
    logger.warning("macOS Vision framework not available — screen OCR disabled")


def capture_screenshot():
    """Capture the full screen as a CGImage. Returns None on failure."""
    if not VISION_AVAILABLE:
        return None

    try:
        # Capture the entire display (all screens composited)
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectInfinite,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        return image
    except Exception as e:
        logger.error(f"Screenshot capture failed: {e}")
        return None


def ocr_image(cg_image) -> str:
    """Run Apple Vision OCR on a CGImage. Returns extracted text."""
    if not VISION_AVAILABLE or cg_image is None:
        return ""

    try:
        # Create a request handler with the screenshot
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            cg_image, None
        )

        # Create a text recognition request
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(
            Vision.VNRequestTextRecognitionLevelAccurate
        )
        request.setUsesLanguageCorrection_(True)

        # Perform OCR
        success = handler.performRequests_error_([request], None)
        if not success[0]:
            logger.error(f"OCR request failed: {success[1]}")
            return ""

        # Extract text from results
        results = request.results()
        if not results:
            return ""

        lines = []
        for observation in results:
            # Each observation is a VNRecognizedTextObservation
            candidates = observation.topCandidates_(1)
            if candidates and len(candidates) > 0:
                lines.append(candidates[0].string())

        return "\n".join(lines)

    except Exception as e:
        logger.error(f"OCR processing failed: {e}")
        return ""


def read_screen() -> dict:
    """
    Capture the screen and extract all visible text via OCR.
    The screenshot is immediately discarded — nothing is stored.

    Returns:
        dict with "text" (extracted content), "timestamp", and "available" flag.
    """
    if not VISION_AVAILABLE:
        return {
            "available": False,
            "text": "",
            "error": "macOS Vision framework not installed. Run: pip install pyobjc-framework-Vision",
        }

    image = capture_screenshot()
    if image is None:
        return {
            "available": True,
            "text": "",
            "error": "Could not capture screenshot. Check screen recording permissions in System Settings > Privacy & Security > Screen Recording.",
        }

    text = ocr_image(image)

    # Image is now garbage collected — nothing stored
    del image

    if not text:
        return {
            "available": True,
            "text": "",
            "note": "Screenshot captured but no text was detected on screen.",
            "timestamp": datetime.now().isoformat(),
        }

    # Limit to ~3000 chars to keep context window reasonable
    if len(text) > 3000:
        text = text[:3000] + "\n[... truncated ...]"

    return {
        "available": True,
        "text": text,
        "char_count": len(text),
        "timestamp": datetime.now().isoformat(),
        "note": "This is OCR-extracted text from the user's screen. It may contain layout artifacts. The screenshot was NOT stored.",
    }
