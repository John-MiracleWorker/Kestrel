# 🕊️ Libre Bird

**Free, offline, privacy-first AI assistant for macOS** — a local alternative to [Little Bird](https://littlebird.ai).

All AI processing runs on your Mac using quantized open-source models. **Zero cloud. Zero cost. Zero data leaves your device.**

---

## ✨ Features

| Feature | Description |
|---|---|
| 💬 **AI Chat** | Context-aware conversations powered by local LLMs |
| 👁 **Screen Context** | Reads your active window to provide relevant assistance |
| 📓 **Daily Journal** | Auto-generates activity summaries from your screen context |
| ✅ **Task Manager** | AI-extracted tasks + manual task tracking |
| 🎙️ **Voice Input** | Hands-free with "Hey Libre" wake word (Whisper Small) |
| 🔊 **Text-to-Speech** | Responses read aloud via macOS neural voices |
| � **Notifications & Reminders** | Native macOS notifications with timed reminders |
| 📋 **Clipboard Tool** | AI can read/write your system clipboard |
| 🚀 **App Launcher** | Open any macOS app by name through chat |
| 🌅 **Daily Briefing** | Morning summary: tasks, yesterday's recap, and context |
| 🧠 **Smart Context** | Activity categorization (coding, browsing, writing, etc.) with time tracking |
| ⌨️ **Global Hotkey** | ⌘+Shift+Space to summon Libre Bird from anywhere |
| �🔒 **100% Private** | All data stored locally in SQLite. No network requests. |
| 🎨 **Aurora Theme** | Stunning aurora borealis glassmorphism design |

## 🧠 Supported Models

| Model | Type | RAM (Q4) | Best For |
|---|---|---|---|
| **GPT-OSS 20B** | MoE (3.6B active) | ~12GB | Speed + quality (recommended) |
| **Qwen 3 14B** | Dense | ~10GB | Thinking mode, reasoning |

Any GGUF model works — just drop it in the `models/` directory.

## 🚀 Quick Start

> For a detailed walkthrough, see **[SETUP.md](SETUP.md)**.

### 1. Setup (one time)
```bash
chmod +x setup.sh start.sh
./setup.sh
```

### 2. Start
```bash
./start.sh
```

### 3. Grant Permissions
For full functionality, grant your terminal these macOS permissions:
- **Accessibility** (System Settings → Privacy → Accessibility) — for screen context
- **Microphone** (System Settings → Privacy → Microphone) — for voice input
- **Notifications** — for reminders (auto-prompted)

## 🎙️ Voice Input

Libre Bird listens for the wake word **"Hey Libre"** using OpenAI's Whisper Small model locally. When activated:

1. Click the **🎤 mic button** in the chat input area, or
2. Say **"Hey Libre"** if the voice listener is running

Transcribed speech is inserted into the chat input. Voice processing is 100% local — no audio ever leaves your Mac.

## ⌨️ Global Hotkey

Press **⌘+Shift+Space** from any app to instantly bring Libre Bird to the front.

## 📁 Project Structure

```
libre-bird/
├── server.py              # FastAPI backend (chat, context, journal, tasks, voice, TTS)
├── llm_engine.py          # LLM inference engine (llama-cpp-python / Metal)
├── context_collector.py   # macOS screen context + activity tracking
├── notifications.py       # macOS native notifications + reminder scheduler
├── voice_input.py         # Whisper STT + "Hey Libre" wake word detection
├── tts.py                 # macOS neural text-to-speech
├── hotkey.py              # Global ⌘+Shift+Space hotkey
├── tools.py               # LLM tool definitions (search, clipboard, reminders, etc.)
├── database.py            # SQLite storage with FTS5
├── memory.py              # Semantic memory / recall
├── app.py                 # pywebview native macOS window launcher
├── build_app.py           # Build .app bundle for macOS
├── requirements.txt       # Python dependencies
├── setup.sh               # One-command setup
├── start.sh               # Launch both servers
├── SETUP.md               # Comprehensive setup guide
├── models/                # Place GGUF model files here
├── libre_bird.db          # Local database (created on first run)
└── frontend/
    ├── index.html         # App shell
    ├── index.css          # Aurora borealis glassmorphism design system
    ├── main.js            # Application logic (voice, TTS, chat, etc.)
    ├── package.json       # Vite config
    └── vite.config.js     # Dev server proxy config
```

## 🔧 API

The backend exposes a full REST API at `http://127.0.0.1:8741`:

| Endpoint | Method | Description |
|---|---|---|
| `/api/chat` | POST | Send a message (SSE streaming response) |
| `/api/conversations` | GET | List conversations |
| `/api/journal/generate` | POST | Generate today's journal |
| `/api/tasks` | GET | List tasks |
| `/api/models` | GET | List available GGUF models |
| `/api/models/load` | POST | Load a model |
| `/api/context/recent` | GET | View recent screen context |
| `/api/reminders` | GET | List active reminders |
| `/api/briefing` | GET | Get the daily briefing |
| `/api/voice/start` | POST | Start voice listener |
| `/api/voice/stop` | POST | Stop voice listener |
| `/api/voice/status` | GET | Voice status + transcriptions |
| `/api/tts/speak` | POST | Speak text aloud |
| `/api/tts/stop` | POST | Stop speech |
| `/api/settings` | GET | View settings |

Full interactive docs at **http://127.0.0.1:8741/docs**

## 🔒 Privacy

- **No network requests** — all processing is local
- **No telemetry** — zero tracking or analytics
- **No cloud** — data never leaves your Mac
- **SQLite database** — stored in `libre_bird.db`, easy to inspect or delete
- **Open source** — you can audit every line of code

## ⚙️ Requirements

- macOS with Apple Silicon (M1/M2/M3/M4)
- 16GB RAM (for GPT-OSS 20B Q4)
- Python 3.11+
- Node.js 18+
- ~10GB disk space for the model
- ~300MB additional for Whisper Small model (auto-downloaded on first use)

## 📝 License

MIT — Free for any use.
