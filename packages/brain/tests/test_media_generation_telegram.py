import uuid
from pathlib import Path

import pytest

from agent.tools.media_gen import _vram_generate_image_handler
from agent.tools.media_gen import vram_orchestrator


@pytest.mark.asyncio
async def test_vram_media_handler_passes_video_type_to_telegram_helper(monkeypatch):
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

        calls = []

        async def fake_send(file_path: str, caption: str, media_type: str | None = None):
            calls.append((file_path, caption, media_type))
            return True

        monkeypatch.setattr(vram_orchestrator, "_send_to_telegram", fake_send)

        result = await _vram_generate_image_handler(
            prompt="make a short video",
            media_type="video",
            send_telegram=True,
        )

        assert result["telegram_sent"] is True
        assert calls == [
            (str(video_path), "make a short video", "video"),
        ]
    finally:
        video_path.unlink(missing_ok=True)
