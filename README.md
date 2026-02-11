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
| 🔒 **100% Private** | All data stored locally in SQLite. No network requests. |
| 🚀 **Fast** | GPT-OSS 20B MoE: only 3.6B active params, runs on 16GB Macs |

## 🧠 Supported Models

| Model | Type | RAM (Q4) | Best For |
|---|---|---|---|
| **GPT-OSS 20B** | MoE (3.6B active) | ~12GB | Speed + quality (recommended) |
| **Qwen 3 14B** | Dense | ~10GB | Thinking mode, reasoning |

Any GGUF model works — just drop it in the `models/` directory.

## 🚀 Quick Start

### 1. Setup (one time)
```bash
chmod +x setup.sh start.sh
./setup.sh
```

This will:
- Create a Python virtual environment
- Install Python dependencies (with Metal GPU acceleration)
- Install frontend dependencies
- Optionally download the GPT-OSS 20B Q4 model

### 2. Start
```bash
./start.sh
```

Open **http://localhost:5173** in your browser.

### 3. Grant Accessibility Permissions
For screen context awareness:
1. Open **System Settings** → **Privacy & Security** → **Accessibility**
2. Add your terminal app (Terminal, iTerm2, Warp, etc.)

## 📁 Project Structure

```
libre-bird/
├── server.py              # FastAPI backend
├── llm_engine.py          # LLM inference engine (llama-cpp-python)
├── context_collector.py   # macOS screen context reader
├── database.py            # SQLite storage with FTS5
├── requirements.txt       # Python dependencies
├── setup.sh               # One-command setup
├── start.sh               # Launch both servers
├── models/                # Place GGUF model files here
├── libre_bird.db          # Local database (created on first run)
└── frontend/
    ├── index.html         # App shell
    ├── index.css          # Dark glassmorphism design system
    ├── main.js            # Application logic
    ├── package.json       # Vite config
    └── vite.config.js     # Dev server config
```

## 🔧 API

The backend exposes a full REST API at `http://127.0.0.1:8741`:

- `POST /api/chat` — Send a message (SSE streaming response)
- `GET /api/conversations` — List conversations
- `POST /api/journal/generate` — Generate today's journal
- `GET /api/tasks` — List tasks
- `GET /api/models` — List available GGUF models
- `POST /api/models/load` — Load a model
- `GET /api/context/recent` — View recent screen context
- `GET /api/settings` — View settings

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

## 📝 License

MIT — Free for any use.
