"""
Libre Bird — FastAPI Backend Server
All endpoints for chat, context, journal, tasks, and settings.
"""

import asyncio
import json
import os
import logging
from datetime import date, datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from database import Database
from llm_engine import engine as llm_engine
from context_collector import ContextCollector, get_screen_context
from memory import detect_recall_intent, retrieve_context
from notifications import reminder_scheduler, send_notification
from voice_input import voice_listener
from tts import speak as tts_speak, stop_speaking as tts_stop, is_speaking as tts_is_speaking

# ── Logging ──────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("libre_bird")

# ── App Setup ────────────────────────────────────────────────────────

app = FastAPI(
    title="Libre Bird",
    description="Free, offline, privacy-first AI assistant",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

db = Database()
context_collector: Optional[ContextCollector] = None

# Voice transcription queue for SSE streaming
_voice_transcriptions: list[str] = []

# ── Request/Response Models ──────────────────────────────────────────


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[int] = None
    include_context: bool = True
    temperature: float = 0.7
    max_tokens: int = 2048


class TaskCreate(BaseModel):
    title: str
    description: Optional[str] = None
    priority: str = "medium"


class TaskUpdate(BaseModel):
    status: str


class SettingUpdate(BaseModel):
    key: str
    value: str


class ModelLoadRequest(BaseModel):
    model_path: str
    n_ctx: int = 8192


# ── Lifecycle ────────────────────────────────────────────────────────


@app.on_event("startup")
async def startup():
    global context_collector
    await db.connect()
    logger.info("Database connected")

    # Set up context collector
    loop = asyncio.get_event_loop()

    def on_context(ctx):
        """Save context snapshot in the background."""
        asyncio.run_coroutine_threadsafe(
            db.save_context(
                app_name=ctx.get("app_name", ""),
                window_title=ctx.get("window_title", ""),
                focused_text=ctx.get("focused_text", ""),
                bundle_id=ctx.get("bundle_id", ""),
            ),
            loop,
        )

    interval = int(await db.get_setting("context_interval", "30"))
    context_collector = ContextCollector(interval=interval, on_context=on_context)

    # Auto-start if enabled
    auto_collect = await db.get_setting("context_enabled", "true")
    if auto_collect == "true":
        context_collector.start()

    # Start reminder scheduler
    reminder_scheduler.start()

    # Start always-on voice listener
    try:
        voice_listener.start()
    except Exception as e:
        logger.warning(f"Voice listener auto-start failed: {e}")

    # Run data retention cleanup (condense old snapshots → memories)
    try:
        await db.cleanup()
    except Exception as e:
        logger.warning(f"Data cleanup failed: {e}")

    # Auto-load last used model
    model_path = await db.get_setting("model_path")
    if model_path and os.path.exists(model_path):
        try:
            logger.info(f"Auto-loading model: {model_path}")
            n_ctx = int(await db.get_setting("n_ctx", "8192"))
            await llm_engine.load_model(model_path, n_ctx=n_ctx)
            logger.info("Model loaded successfully")
        except Exception as e:
            logger.error(f"Failed to auto-load model: {e}")


@app.on_event("shutdown")
async def shutdown():
    if context_collector:
        context_collector.stop()
    reminder_scheduler.stop()
    voice_listener.stop()
    llm_engine.unload_model()
    await db.close()
    logger.info("Libre Bird shut down")


# ── Health / Status ──────────────────────────────────────────────────


@app.get("/api/status")
async def get_status():
    return {
        "status": "running",
        "model_loaded": llm_engine.is_loaded,
        "model_path": llm_engine.model_path,
        "context_collecting": context_collector.is_running if context_collector else False,
        "context_paused": context_collector.is_paused if context_collector else False,
    }


# ── Chat ─────────────────────────────────────────────────────────────


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Send a message and get a streamed response."""
    if not llm_engine.is_loaded:
        raise HTTPException(400, "No model loaded. Go to Settings to load a model.")

    # Get or create conversation
    conv_id = req.conversation_id
    if not conv_id:
        conv_id = await db.create_conversation()

    # Get conversation history
    history = await db.get_messages(conv_id)

    # Get current screen context if requested
    context = None
    context_id = None
    if req.include_context and context_collector and context_collector.last_context:
        context = context_collector.last_context
        context_id = await db.save_context(
            app_name=context.get("app_name", ""),
            window_title=context.get("window_title", ""),
            focused_text=context.get("focused_text", ""),
            bundle_id=context.get("bundle_id", ""),
        )

    # Save user message
    await db.add_message(conv_id, "user", req.message, context_id)

    # RAG: Retrieve historical memory if the user is asking about past activity
    memory = None
    if detect_recall_intent(req.message):
        logger.info(f"Recall intent detected, retrieving historical context")
        memory = await retrieve_context(db, req.message)
        if memory:
            logger.info(f"Retrieved {len(memory)} chars of historical memory")
        else:
            memory = "[No activity data found for this time period. Tell the user you don't have data for what they're asking about.]"
            logger.info("No historical memory found — sending honest 'no data' signal")

    # Generate title for new conversations
    if len(history) == 0:
        try:
            title = await llm_engine.generate_title(req.message)
            await db.update_conversation_title(conv_id, title)
        except Exception:
            pass

    # Stream the response
    async def event_generator():
        full_response = ""      # everything (for debugging)
        display_response = ""   # only the answer (saved to DB)
        thinking_text = ""      # thinking block content
        in_thinking = False
        think_buffer = ""       # buffer to detect <think> and </think> tags

        try:
            async for tag, content in llm_engine.chat_stream(
                user_message=req.message,
                history=history,
                context=context,
                memory=memory,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            ):
                if tag == "tool":
                    # Tool being called — send indicator
                    yield {"event": "tool", "data": json.dumps({"tool": content})}
                    continue

                # tag == "raw" — stream token with think detection
                full_response += content
                think_buffer += content

                # Process buffer for <think> and </think> tags
                while think_buffer:
                    if in_thinking:
                        # Look for </think>
                        end_idx = think_buffer.find("</think>")
                        if end_idx != -1:
                            # Emit thinking content before the tag
                            thinking_chunk = think_buffer[:end_idx]
                            if thinking_chunk:
                                thinking_text += thinking_chunk
                                yield {"event": "thinking", "data": json.dumps({"token": thinking_chunk})}
                            think_buffer = think_buffer[end_idx + len("</think>"):]
                            in_thinking = False
                            yield {"event": "thinking_done", "data": "{}"}
                        else:
                            # Could be a partial </think> at the end
                            if len(think_buffer) > 8 or "</think>"[:len(think_buffer)] != think_buffer[-len(think_buffer):]:
                                # Safe to emit — not a partial tag
                                thinking_text += think_buffer
                                yield {"event": "thinking", "data": json.dumps({"token": think_buffer})}
                                think_buffer = ""
                            else:
                                break  # Wait for more data
                    else:
                        # Look for <think>
                        start_idx = think_buffer.find("<think>")
                        if start_idx != -1:
                            # Emit any content before <think> as regular tokens
                            before = think_buffer[:start_idx]
                            if before:
                                display_response += before
                                yield {"event": "token", "data": json.dumps({"token": before})}
                            think_buffer = think_buffer[start_idx + len("<think>"):]
                            in_thinking = True
                        else:
                            # Could be a partial <think> at the end
                            # Check if the buffer ends with a prefix of "<think>"
                            partial = False
                            for i in range(1, min(len("<think>"), len(think_buffer) + 1)):
                                if think_buffer.endswith("<think>"[:i]):
                                    # Emit everything except the potential partial tag
                                    safe = think_buffer[:len(think_buffer) - i]
                                    if safe:
                                        display_response += safe
                                        yield {"event": "token", "data": json.dumps({"token": safe})}
                                    think_buffer = think_buffer[len(think_buffer) - i:]
                                    partial = True
                                    break
                            if not partial:
                                # No partial tag — emit everything
                                display_response += think_buffer
                                yield {"event": "token", "data": json.dumps({"token": think_buffer})}
                                think_buffer = ""
                            break  # Wait for more data if partial

            # Flush remaining buffer
            if think_buffer:
                if in_thinking:
                    thinking_text += think_buffer
                    yield {"event": "thinking", "data": json.dumps({"token": think_buffer})}
                else:
                    display_response += think_buffer
                    yield {"event": "token", "data": json.dumps({"token": think_buffer})}

            # Clean the display response
            from llm_engine import LLMEngine
            cleaned = LLMEngine._clean_response(display_response)

            # Save the cleaned response (no thinking)
            await db.add_message(conv_id, "assistant", cleaned)
            yield {
                "event": "done",
                "data": json.dumps({
                    "conversation_id": conv_id,
                    "full_response": cleaned,
                }),
            }
        except Exception as e:
            logger.error(f"Chat error: {e}")
            yield {"event": "error", "data": json.dumps({"error": str(e)})}

    return EventSourceResponse(event_generator())


@app.get("/api/chat/now")
async def get_context_now():
    """Get the current screen context."""
    ctx = get_screen_context()
    return ctx or {"app_name": None, "window_title": None, "focused_text": None}


# ── Conversations ────────────────────────────────────────────────────


@app.get("/api/conversations")
async def list_conversations():
    return await db.list_conversations()


@app.get("/api/conversations/{conv_id}/messages")
async def get_messages(conv_id: int):
    return await db.get_messages(conv_id)


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: int):
    await db.delete_conversation(conv_id)
    return {"ok": True}


# ── Journal ──────────────────────────────────────────────────────────


@app.post("/api/journal/generate")
async def generate_journal(target_date: str = None):
    """Generate a journal entry for a specific date (default: today)."""
    if not llm_engine.is_loaded:
        raise HTTPException(400, "No model loaded")

    d = target_date or date.today().isoformat()
    context_entries = await db.get_context_for_date(d)

    if not context_entries:
        raise HTTPException(404, f"No context data for {d}. Keep Libre Bird running to collect activity data.")

    result = await llm_engine.generate_journal(context_entries)

    # Save journal
    await db.save_journal(
        entry_date=d,
        summary=result.get("summary", ""),
        activities=result.get("activities", []),
        tasks=result.get("tasks", []),
    )

    # Auto-create tasks from journal
    for task_title in result.get("tasks", []):
        if task_title and isinstance(task_title, str):
            await db.create_task(
                title=task_title,
                source="journal",
            )

    return {**result, "date": d}


@app.get("/api/journal")
async def list_journals():
    return await db.list_journals()


@app.get("/api/journal/{entry_date}")
async def get_journal(entry_date: str):
    entry = await db.get_journal(entry_date)
    if not entry:
        raise HTTPException(404, "No journal entry for this date")
    return entry


# ── Tasks ────────────────────────────────────────────────────────────


@app.get("/api/tasks")
async def list_tasks(status: Optional[str] = None):
    return await db.list_tasks(status)


@app.post("/api/tasks")
async def create_task(task: TaskCreate):
    task_id = await db.create_task(
        title=task.title,
        description=task.description,
        priority=task.priority,
        source="manual",
    )
    return {"id": task_id}


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: int, update: TaskUpdate):
    await db.update_task_status(task_id, update.status)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: int):
    await db.delete_task(task_id)
    return {"ok": True}


# ── Context ──────────────────────────────────────────────────────────


@app.get("/api/context/recent")
async def recent_context(limit: int = 20):
    return await db.get_recent_context(limit)


@app.get("/api/context/search")
async def search_context(q: str, limit: int = 20):
    return await db.search_context(q, limit)


@app.post("/api/context/pause")
async def pause_context():
    if context_collector:
        context_collector.pause()
    return {"paused": True}


@app.post("/api/context/resume")
async def resume_context():
    if context_collector:
        context_collector.resume()
    return {"paused": False}


# ── Model Management ────────────────────────────────────────────────


@app.get("/api/models")
async def list_models():
    return {
        "models": llm_engine.get_available_models(),
        "loaded": llm_engine.model_path,
    }


@app.post("/api/models/load")
async def load_model(req: ModelLoadRequest):
    try:
        await llm_engine.load_model(req.model_path, n_ctx=req.n_ctx)
        await db.set_setting("model_path", req.model_path)
        await db.set_setting("n_ctx", str(req.n_ctx))
        return {"ok": True, "model": req.model_path}
    except Exception as e:
        raise HTTPException(500, f"Failed to load model: {e}")


@app.post("/api/models/unload")
async def unload_model():
    llm_engine.unload_model()
    return {"ok": True}


# ── Settings ─────────────────────────────────────────────────────────


@app.get("/api/settings")
async def get_all_settings():
    return {
        "model_path": await db.get_setting("model_path", ""),
        "n_ctx": await db.get_setting("n_ctx", "8192"),
        "context_interval": await db.get_setting("context_interval", "30"),
        "context_enabled": await db.get_setting("context_enabled", "true"),
        "temperature": await db.get_setting("temperature", "0.7"),
        "max_tokens": await db.get_setting("max_tokens", "2048"),
    }


@app.put("/api/settings")
async def update_setting(setting: SettingUpdate):
    await db.set_setting(setting.key, setting.value)

    # Apply live changes
    if setting.key == "context_interval" and context_collector:
        context_collector.interval = int(setting.value)
    elif setting.key == "context_enabled" and context_collector:
        if setting.value == "true":
            context_collector.resume()
        else:
            context_collector.pause()

    return {"ok": True}


# ── Reminders API ────────────────────────────────────────────────────


@app.get("/api/reminders")
async def list_reminders():
    return reminder_scheduler.list_reminders()


@app.delete("/api/reminders/{reminder_id}")
async def cancel_reminder(reminder_id: str):
    success = reminder_scheduler.cancel_reminder(reminder_id)
    if not success:
        raise HTTPException(404, "Reminder not found or already fired")
    return {"ok": True, "id": reminder_id}


# ── Daily Briefing API ───────────────────────────────────────────────


@app.get("/api/briefing")
async def daily_briefing():
    """Assemble a morning briefing: weather, pending tasks, yesterday's journal."""
    from tools import tool_get_weather

    pieces = []

    # Weather (best-effort)
    try:
        weather = tool_get_weather("auto")
        if "error" not in weather:
            pieces.append(
                f"🌤 **Weather**: {weather.get('condition', '?')} "
                f"{weather.get('temp_f', '?')}°F in {weather.get('location', 'your area')}"
            )
    except Exception:
        pass

    # Pending tasks
    try:
        tasks = await db.get_tasks(status="pending")
        if tasks:
            task_list = "\n".join(f"  • {t['title']}" for t in tasks[:10])
            pieces.append(f"📋 **Pending tasks** ({len(tasks)}):\n{task_list}")
        else:
            pieces.append("✅ **Tasks**: All clear — nothing pending!")
    except Exception:
        pass

    # Yesterday's journal
    try:
        from datetime import timedelta
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        entry = await db.get_journal_entry(yesterday)
        if entry:
            snippet = entry.get("content", "")[:300]
            pieces.append(f"📓 **Yesterday's journal**: {snippet}")
    except Exception:
        pass

    # Active reminders
    reminders = reminder_scheduler.list_reminders()
    if reminders:
        rem_list = "\n".join(
            f"  ⏰ {r['message']} (in {r['remaining_seconds'] // 60}m)"
            for r in reminders
        )
        pieces.append(f"⏰ **Reminders**:\n{rem_list}")

    return {
        "briefing": "\n\n".join(pieces)
        if pieces
        else "Good morning! Nothing on the radar today."
    }


# ── Voice Input API ──────────────────────────────────────────────────


@app.post("/api/voice/start")
async def start_voice():
    """Start listening for the 'Hey Libre' wake word."""
    success = voice_listener.start()
    if not success:
        raise HTTPException(500, "Voice input not available (missing pyaudio or whisper)")
    return {"ok": True, "status": "listening"}


@app.post("/api/voice/stop")
async def stop_voice():
    """Stop voice listening."""
    voice_listener.stop()
    return {"ok": True, "status": "stopped"}


@app.get("/api/voice/status")
async def voice_status():
    """Get voice listener status and any pending transcriptions."""
    transcriptions = voice_listener.get_transcriptions()
    return {
        "running": voice_listener.is_running,
        "listening": voice_listener.is_listening,
        "transcriptions": transcriptions,
    }


# ── Text-to-Speech API ───────────────────────────────────────────────


class SpeakRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    rate: int = 190


@app.post("/api/tts/speak")
async def speak_text(req: SpeakRequest):
    """Speak text aloud using macOS neural voices."""
    tts_speak(req.text, voice=req.voice, rate=req.rate)
    return {"ok": True, "status": "speaking"}


@app.post("/api/tts/stop")
async def stop_tts():
    """Stop any current speech output."""
    tts_stop()
    return {"ok": True, "status": "stopped"}


@app.get("/api/tts/status")
async def tts_status():
    return {"speaking": tts_is_speaking()}


# ── Static File Serving (production mode) ────────────────────────────
# Serve the built frontend when running as a native app (no Vite dev server)

_frontend_dist = os.path.join(os.path.dirname(__file__), "frontend", "dist")
if os.path.isdir(_frontend_dist):
    # Serve index.html for the root route
    @app.get("/")
    async def serve_index():
        from fastapi.responses import FileResponse
        return FileResponse(os.path.join(_frontend_dist, "index.html"))

    # Serve static assets
    app.mount("/", StaticFiles(directory=_frontend_dist), name="static")
    logger.info(f"Serving frontend from {_frontend_dist}")
