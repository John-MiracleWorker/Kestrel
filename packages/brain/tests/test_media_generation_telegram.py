import uuid
from pathlib import Path

import pytest

from agent.tools.media_gen import _vram_generate_image_handler
from agent.tools.media_gen import vram_orchestrator


@pytest.mark.asyncio
async def test_vram_media_handler_delegates_delivery_to_gateway(monkeypatch):
    temp_dir = Path("packages/brain/tests/.tmp")
    temp_dir.mkdir(parents=True, exist_ok=True)
    video_path = temp_dir / f"clip-{uuid.uuid4().hex}.mp4"
    video_path.write_bytes(b"video-bytes")

    try:
        monkeypatch.setattr(
            vram_orchestrator,
            "generate_image",
            lambda **kwargs: {
                "success": True,
                "file_paths": [str(video_path)],
                "image_urls": ["/media/clip.mp4"],
            },
        )

        result = await _vram_generate_image_handler(
            prompt="make a short video",
            media_type="video",
            send_telegram=True,
        )

        assert result["display_markdown"] == "![Generated video](/media/clip.mp4)"
        assert result["delivery"] == {
            "mode": "channel_event",
            "requested": True,
            "target": "telegram",
            "note": "Direct Brain-to-Telegram delivery is disabled; Gateway delivers shared media artifacts.",
        }
    finally:
        video_path.unlink(missing_ok=True)
