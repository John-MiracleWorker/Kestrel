"""
Libre Bird — Voice Input with Wake Word Detection.
Uses Whisper Small for local speech-to-text.
Wake word: "Hey Libre"
All audio is processed locally — nothing is sent to any server.
"""

import io
import logging
import struct
import threading
import time
import wave
from typing import Callable, Optional

logger = logging.getLogger("libre_bird.voice")

# Try to import audio dependencies
AUDIO_AVAILABLE = False
WHISPER_AVAILABLE = False

try:
    import pyaudio
    AUDIO_AVAILABLE = True
except ImportError:
    logger.warning("pyaudio not installed — voice input disabled")

try:
    from whisper_cpp_python import Whisper
    WHISPER_AVAILABLE = True
except ImportError:
    try:
        # Fallback: try mlx-whisper
        import mlx_whisper
        WHISPER_AVAILABLE = True
        logger.info("Using mlx-whisper for transcription")
    except ImportError:
        logger.warning("No whisper backend available — voice input disabled")


# ── Audio Constants ──────────────────────────────────────────────────

RATE = 16000  # 16kHz for Whisper
CHANNELS = 1
FORMAT_PA = 8  # pyaudio.paInt16 = 8
CHUNK = 1024
WAKE_WORD = "hey libre"
SILENCE_THRESHOLD = 500  # RMS amplitude threshold
SILENCE_DURATION = 1.5  # Seconds of silence to stop recording
WAKE_LISTEN_SECONDS = 2  # How many seconds of audio to check for wake word


def _rms(data: bytes) -> float:
    """Calculate RMS amplitude of raw audio bytes."""
    if len(data) < 2:
        return 0
    count = len(data) // 2
    shorts = struct.unpack(f"<{count}h", data[:count * 2])
    sum_sq = sum(s * s for s in shorts)
    return (sum_sq / count) ** 0.5


class VoiceListener:
    """
    Listens for the wake word "Hey Libre", then records and transcribes
    the user's spoken message.
    """

    def __init__(
        self,
        model_path: str = None,
        on_wake: Callable = None,
        on_transcription: Callable[[str], None] = None,
        on_status: Callable[[str], None] = None,
    ):
        self._model_path = model_path
        self._model = None
        self._stream = None
        self._pa = None
        self._running = False
        self._listening = False
        self._thread: Optional[threading.Thread] = None

        # Callbacks
        self.on_wake = on_wake  # Called when wake word is detected
        self.on_transcription = on_transcription  # Called with transcribed text
        self.on_status = on_status  # Called with status updates

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_listening(self) -> bool:
        return self._listening

    def start(self):
        """Start the voice listener in a background thread."""
        if not AUDIO_AVAILABLE:
            logger.error("Cannot start voice: pyaudio not available")
            return False

        if self._running:
            return True

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("Voice listener started (wake word: 'Hey Libre')")
        return True

    def stop(self):
        """Stop the voice listener."""
        self._running = False
        self._listening = False
        if self._thread:
            self._thread.join(timeout=5)
        self._cleanup_audio()
        logger.info("Voice listener stopped")

    def _load_model(self):
        """Load the Whisper model (lazy initialization)."""
        if self._model is not None:
            return True

        try:
            if "mlx_whisper" in dir():
                # mlx-whisper doesn't need explicit model loading
                self._model = "mlx"
                logger.info("Using mlx-whisper backend")
                return True

            if WHISPER_AVAILABLE and self._model_path:
                self._model = Whisper(self._model_path)
                logger.info(f"Whisper model loaded: {self._model_path}")
                return True

            # Try default path
            import os
            default_paths = [
                os.path.expanduser("~/.libre_bird/models/ggml-small.bin"),
                os.path.expanduser("~/models/ggml-small.bin"),
            ]
            for path in default_paths:
                if os.path.exists(path):
                    self._model = Whisper(path)
                    logger.info(f"Whisper model loaded from: {path}")
                    return True

            logger.error("No Whisper model found")
            return False

        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            return False

    def _init_audio(self):
        """Initialize PyAudio stream."""
        try:
            self._pa = pyaudio.PyAudio()
            self._stream = self._pa.open(
                format=FORMAT_PA,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK,
            )
            return True
        except Exception as e:
            logger.error(f"Failed to initialize audio: {e}")
            return False

    def _cleanup_audio(self):
        """Clean up audio resources."""
        try:
            if self._stream:
                self._stream.stop_stream()
                self._stream.close()
                self._stream = None
            if self._pa:
                self._pa.terminate()
                self._pa = None
        except Exception:
            pass

    def _record_chunk(self, seconds: float) -> bytes:
        """Record N seconds of audio."""
        frames = []
        num_chunks = int(RATE / CHUNK * seconds)
        for _ in range(num_chunks):
            if not self._running:
                break
            try:
                data = self._stream.read(CHUNK, exception_on_overflow=False)
                frames.append(data)
            except Exception:
                break
        return b"".join(frames)

    def _record_until_silence(self, max_seconds: float = 30) -> bytes:
        """Record audio until silence is detected or max duration reached."""
        frames = []
        silent_chunks = 0
        max_silent = int(SILENCE_DURATION * RATE / CHUNK)
        max_chunks = int(max_seconds * RATE / CHUNK)

        for i in range(max_chunks):
            if not self._running:
                break
            try:
                data = self._stream.read(CHUNK, exception_on_overflow=False)
                frames.append(data)

                if _rms(data) < SILENCE_THRESHOLD:
                    silent_chunks += 1
                else:
                    silent_chunks = 0

                # Stop after sustained silence (but only after at least 1 second)
                if silent_chunks >= max_silent and i > int(RATE / CHUNK):
                    break
            except Exception:
                break

        return b"".join(frames)

    def _transcribe(self, audio_data: bytes) -> str:
        """Transcribe audio bytes using Whisper."""
        try:
            if self._model == "mlx":
                # mlx-whisper path
                import mlx_whisper
                import tempfile
                import os

                # Write to temp WAV file
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                    wf = wave.open(f, "wb")
                    wf.setnchannels(CHANNELS)
                    wf.setsampwidth(2)  # 16-bit
                    wf.setframerate(RATE)
                    wf.writeframes(audio_data)
                    wf.close()
                    temp_path = f.name

                result = mlx_whisper.transcribe(
                    temp_path, path_or_hf_repo="mlx-community/whisper-small"
                )
                os.unlink(temp_path)
                return result.get("text", "").strip()

            elif hasattr(self._model, "transcribe"):
                # whisper-cpp-python path
                result = self._model.transcribe(audio_data)
                return result.strip() if result else ""

            return ""

        except Exception as e:
            logger.error(f"Transcription error: {e}")
            return ""

    def _run_loop(self):
        """Main voice listener loop."""
        # Load model first
        if not self._load_model():
            self._emit_status("error: no whisper model")
            self._running = False
            return

        if not self._init_audio():
            self._emit_status("error: no microphone")
            self._running = False
            return

        self._emit_status("listening_for_wake_word")

        while self._running:
            try:
                # Phase 1: Listen for wake word
                audio_chunk = self._record_chunk(WAKE_LISTEN_SECONDS)
                if not self._running:
                    break

                text = self._transcribe(audio_chunk).lower()

                if WAKE_WORD in text:
                    logger.info("Wake word detected!")
                    self._listening = True
                    self._emit_status("wake_word_detected")

                    if self.on_wake:
                        self.on_wake()

                    # Phase 2: Record until silence
                    self._emit_status("recording")
                    message_audio = self._record_until_silence(max_seconds=30)

                    if not self._running:
                        break

                    # Phase 3: Transcribe the full message
                    self._emit_status("transcribing")
                    transcription = self._transcribe(message_audio)

                    if transcription:
                        logger.info(f"Voice transcription: {transcription}")
                        if self.on_transcription:
                            self.on_transcription(transcription)

                    self._listening = False
                    self._emit_status("listening_for_wake_word")

            except Exception as e:
                logger.error(f"Voice listener error: {e}")
                time.sleep(1)

        self._cleanup_audio()

    def _emit_status(self, status: str):
        """Emit a status update."""
        logger.info(f"Voice status: {status}")
        if self.on_status:
            try:
                self.on_status(status)
            except Exception:
                pass


# Singleton instance
voice_listener = VoiceListener()
