# 🕊️ Libre Bird

**Free, offline, privacy-first AI assistant for macOS** — a local alternative to [Little Bird](https://littlebird.ai).

All AI processing runs on your Mac using quantized open-source models. **Zero cloud. Zero cost. Zero data leaves your device.**

---

## ✨ Features

| Feature | Description |
|---|---|
| 💬 **AI Chat** | Context-aware conversations powered by local LLMs |
| 🧩 **101 Tools / 26 Skills** | Modular skill system — toggle skills on/off from Settings |
| 👁 **Screen Context** | Reads your active window to provide relevant assistance |
| 📓 **Daily Journal** | Auto-generates activity summaries from your screen context |
| ✅ **Task Manager** | AI-extracted tasks + manual task tracking |
| 🎙️ **Voice Input** | Hands-free with "Hey Libre" wake word (Whisper Small) |
| 🔊 **Text-to-Speech** | Responses read aloud via macOS neural voices |
| 🔔 **Notifications & Reminders** | Native macOS notifications with timed reminders |
| 🌅 **Daily Briefing** | Morning summary: tasks, yesterday's recap, and context |
| ⌨️ **Global Hotkey** | ⌘+Shift+Space to summon Libre Bird from anywhere |
| 🔒 **100% Private** | All data stored locally in SQLite. No network requests. |
| 🎨 **Aurora Theme** | Stunning aurora borealis glassmorphism design |

## 🧠 Supported Models

| Model | Type | RAM (Q4) | Best For |
|---|---|---|---|
| **GPT-OSS 20B** | MoE (3.6B active) | ~12GB | Speed + quality (recommended) |
| **Qwen 3 14B** | Dense | ~10GB | Thinking mode, reasoning |

Any GGUF model works — just drop it in the `models/` directory.

## 🧩 Skills System

Libre Bird uses a **modular skills architecture** — 26 skill packs containing 101 tools, all auto-discovered and toggleable from Settings.

### Built-in Skills

| Skill | Icon | Tools | Description |
|---|---|---|---|
| Core Utilities | ⚙️ | 7 | Weather, calculator, datetime, file search, app launcher, system info |
| Screen Analysis | 👁 | 2 | Read & analyze the active screen |
| Productivity | 📋 | 6 | Clipboard, reminders, keyboard control, file operations, document reading |
| Media & Music | 🎵 | 3 | Apple Music control, text-to-speech, image generation |
| Web & Code | 🌐 | 4 | Web search, URL reader, code execution, shell commands |
| Knowledge Base | 🧠 | 2 | Local RAG — save and search personal knowledge |

### Community Skills

| Skill | Icon | Tools | Description |
|---|---|---|---|
| Wikipedia + Wolfram | 📚 | 3 | Encyclopedia lookup + computational answers |
| Task Scheduler | ⏰ | 3 | Cron-style scheduled tasks with JSON persistence |
| Document Intelligence | 📄 | 4 | Parse PDFs, Word docs, and Excel spreadsheets |
| Translation | 🌐 | 2 | Multi-language translation (MyMemory / DeepL) |
| Computer Use | 🖱️ | 6 | Mouse clicks, keyboard typing, hotkeys, screenshots (pyautogui) |
| Focus Timer | 🍅 | 4 | Pomodoro sessions with notifications and productivity stats |
| API Caller | 🔌 | 3 | Generic REST API client (GET/POST/PUT/DELETE) |
| Text Transform | 🔄 | 6 | MD→HTML, JSON prettify, CSV→JSON, case conversion, Base64 |
| Meeting Summarizer | 📝 | 2 | Parse transcripts (VTT/SRT/TXT), extract action items |
| Server SSH/FTP | 🖥️ | 5 | Remote server commands and file transfer via SSH/SFTP |
| Serial / USB | 🔧 | 4 | Communicate with Arduino and USB serial devices |
| Browser Automation | 🌐 | 5 | Navigate, click, type on web pages (Playwright) |
| Daily Digest | 📰 | 4 | RSS/Atom feed reader |
| GitHub | 🐙 | 4 | Repos, issues, PRs, and stats |
| Home Automation | 🏠 | 3 | macOS Shortcuts and HomeKit devices |
| Apple Calendar | 📅 | 4 | List, create, and manage calendar events |
| Apple Contacts | 👥 | 3 | Search, view, and create contacts |
| Apple Mail | 📧 | 4 | Check inbox, read, compose, unread count |
| Apple Notes | 📝 | 4 | List, read, create, and search notes |
| System Monitor | 📊 | 4 | CPU, memory, disk, battery, top processes, network |

### Optional API Keys

Add these to your `.env` file for enhanced features (everything works without them):

| Key | Skill | Notes |
|---|---|---|
| `WOLFRAM_APP_ID` | Wikipedia | Free at [developer.wolframalpha.com](https://developer.wolframalpha.com/) |
| `DEEPL_API_KEY` | Translation | Free tier at [deepl.com/pro-api](https://www.deepl.com/pro-api) |
| `GITHUB_TOKEN` | GitHub | For private repos and higher rate limits |

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
├── server.py              # FastAPI backend
├── llm_engine.py          # LLM inference (llama-cpp-python / Metal)
├── skill_loader.py        # Auto-discovers and manages all skills
├── tools.py               # Compatibility shim → skill_loader
├── context_collector.py   # macOS screen context + activity tracking
├── proactive.py           # Proactive suggestion engine
├── notifications.py       # macOS native notifications
├── voice_input.py         # Whisper STT + "Hey Libre" wake word
├── tts.py                 # macOS neural text-to-speech
├── hotkey.py              # Global ⌘+Shift+Space hotkey
├── database.py            # SQLite storage with FTS5
├── memory.py              # Semantic memory / recall
├── app.py                 # pywebview native macOS window launcher
├── skills/                # ← Modular skill packs (26 skills, 101 tools)
│   ├── core/              # Weather, calculator, datetime, etc.
│   ├── screen/            # Screen reading & analysis
│   ├── productivity/      # Clipboard, keyboard, file ops
│   ├── media/             # Music control, TTS, image gen
│   ├── web/               # Web search, code execution, shell
│   ├── knowledge/         # Local RAG knowledge base
│   ├── wikipedia/         # Wikipedia + Wolfram Alpha
│   ├── scheduler/         # Cron-style task scheduler
│   ├── documents/         # PDF, DOCX, XLSX parser
│   ├── translate/         # Multi-language translation
│   ├── computer_use/      # Mouse & keyboard automation
│   ├── focus_timer/       # Pomodoro timer + stats
│   ├── api_caller/        # Generic REST API client
│   ├── text_transform/    # Format conversion & text tools
│   ├── meeting_summarizer/# Transcript analysis
│   ├── ssh_ftp/           # Remote server SSH/SFTP
│   ├── serial_usb/        # Arduino & USB serial
│   ├── browser_automation/# Playwright browser control
│   ├── calendar/          # Apple Calendar
│   ├── contacts/          # Apple Contacts
│   ├── email/             # Apple Mail
│   ├── notes/             # Apple Notes
│   ├── digest/            # RSS feed reader
│   ├── github/            # GitHub integration
│   ├── home_automation/   # HomeKit + Shortcuts
│   └── system_monitor/    # CPU, memory, disk, battery
├── models/                # Place GGUF model files here
└── frontend/
    ├── index.html         # App shell
    ├── index.css          # Aurora borealis glassmorphism design
    ├── main.js            # Application logic
    └── vite.config.js     # Dev server proxy
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
| `/api/skills` | GET | List all skills and their status |
| `/api/skills/{name}/toggle` | POST | Enable/disable a skill |

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
- Python 3.9+
- Node.js 18+
- ~10GB disk space for the model
- ~300MB additional for Whisper Small model (auto-downloaded on first use)

## 📝 License

MIT — Free for any use.
