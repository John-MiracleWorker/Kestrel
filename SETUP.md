# 🕊️ Libre Bird — Setup Guide

Comprehensive guide to get Libre Bird running on your Mac.

---

## Prerequisites

| Requirement | Minimum | Details |
|---|---|---|
| **macOS** | 12+ (Monterey) | Apple Silicon required (M1/M2/M3/M4) |
| **RAM** | 16 GB | For the recommended GPT-OSS 20B Q4 model |
| **Python** | 3.11+ | Install via [python.org](https://www.python.org/downloads/) or `brew install python` |
| **Node.js** | 18+ | Install via [nodejs.org](https://nodejs.org/) or `brew install node` |
| **Disk Space** | ~12 GB | ~10 GB model + ~1.5 GB dependencies + ~300 MB Whisper |
| **Xcode CLI Tools** | Latest | Run `xcode-select --install` if not already installed |

## Step 1: Clone the Repository

```bash
git clone https://github.com/John-MiracleWorker/LibreBird.git
cd LibreBird
```

## Step 2: Run the Setup Script

```bash
chmod +x setup.sh start.sh
./setup.sh
```

This will:
1. Create a Python virtual environment (`venv/`)
2. Install all Python dependencies with Metal GPU acceleration
3. Install frontend dependencies via npm
4. Optionally download the GPT-OSS 20B Q4 GGUF model (~10 GB)

If the setup script fails, see [Manual Setup](#manual-setup) below.

## Step 3: Grant macOS Permissions

Libre Bird uses macOS native APIs for screen context, voice, and notifications. Grant the following permissions:

### Accessibility (Required for Screen Context)
1. Open **System Settings** → **Privacy & Security** → **Accessibility**
2. Click the **+** button
3. Add your terminal app (Terminal, iTerm2, Warp, or VS Code)
4. Restart Libre Bird after granting

### Microphone (Required for Voice Input)
1. Open **System Settings** → **Privacy & Security** → **Microphone**
2. Enable access for your terminal app
3. You'll also be prompted automatically the first time you use voice input

### Notifications (Required for Reminders)
macOS will auto-prompt you to allow notifications the first time a reminder fires.

### Input Monitoring (Required for Global Hotkey)
1. Open **System Settings** → **Privacy & Security** → **Input Monitoring**
2. Add your terminal app
3. This allows the ⌘+Shift+Space hotkey to work globally

## Step 4: Start Libre Bird

### Option A: Browser Mode (Development)
```bash
./start.sh
```
Opens at **http://localhost:5173** with hot-reload.

### Option B: Native macOS App
```bash
source venv/bin/activate
python app.py
```
Opens a native macOS window (pywebview). No browser needed.

### Option C: Build a .app Bundle
```bash
source venv/bin/activate
python build_app.py
```
Creates a `Libre Bird.app` that you can drag to your Applications folder.

## Step 5: Load a Model

1. Go to the **Settings** tab in Libre Bird
2. Your `models/` directory will be scanned for `.gguf` files
3. Click **Load** on the model you want to use
4. Wait for the model to load (usually 5-15 seconds)

### Downloading Models Manually

If you skipped the model download during setup:

```bash
mkdir -p models
# GPT-OSS 20B Q4 (recommended)
huggingface-cli download gpt-oss/gpt-oss-20B-v2-GGUF gpt-oss-20b-v2.Q4_K_M.gguf --local-dir models/

# Or Qwen 3 14B Q4 (alternative)
huggingface-cli download Qwen/Qwen3-14B-GGUF qwen3-14b-q4_k_m.gguf --local-dir models/
```

---

## Feature Configuration

### Voice Input ("Hey Libre")
- Uses **OpenAI Whisper Small** (~300 MB, auto-downloaded on first use)
- Click the **🎤 mic button** or say "Hey Libre" to activate
- All voice processing happens locally — no audio leaves your Mac
- Whisper model is cached in `~/.cache/whisper/`

### Global Hotkey
- **⌘+Shift+Space** summons Libre Bird from any app
- Requires Input Monitoring permission (see Step 3)
- Active only when Libre Bird is running

### Reminders
- Say "Remind me to take a break in 30 minutes" in chat
- The AI will call the `set_reminder` tool automatically
- Native macOS notifications will fire at the scheduled time

### Clipboard
- Ask "What's on my clipboard?" or "Copy this to clipboard"
- The AI can read and write your system clipboard

### App Launcher
- Say "Open Safari" or "Launch Spotify"
- Works with any macOS app by name

### Daily Briefing
- Available via API at `/api/briefing`
- Returns: pending tasks, yesterday's journal recap

### Text-to-Speech
- macOS neural voices read responses aloud
- Configurable via the TTS API endpoints

---

## Manual Setup

If the setup script doesn't work, follow these steps:

```bash
# 1. Create virtual environment
python3 -m venv venv
source venv/bin/activate

# 2. Install Python deps with Metal GPU support
CMAKE_ARGS="-DLLAMA_METAL=on" pip install -r requirements.txt

# 3. Install frontend deps
cd frontend
npm install
cd ..

# 4. Create models directory
mkdir -p models

# 5. Start the backend
source venv/bin/activate
python -m uvicorn server:app --host 127.0.0.1 --port 8741

# 6. Start the frontend (in another terminal)
cd frontend
npm run dev
```

## Troubleshooting

### "No module named 'AppKit'"
Reinstall pyobjc:
```bash
pip install --force-reinstall pyobjc-core pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices
```

### Model loading is slow
- Ensure you're using a Q4_K_M quantization (best speed/quality trade-off)
- Close other memory-heavy apps
- Check Activity Monitor to ensure Metal GPU is being used

### Voice input not detecting speech
1. Verify microphone permission is granted
2. Speak clearly within ~3 feet of the mic
3. The Whisper Small model needs a few seconds to process each chunk
4. Check terminal logs for any errors

### Global hotkey not working
1. Ensure Input Monitoring permission is granted
2. Restart Libre Bird after granting the permission
3. Check if another app is using the same shortcut

### Screen context shows "Context: off"
1. Verify Accessibility permission is granted (Step 3)
2. Restart your terminal after granting
3. Toggle context on/off in the chat input area

### "Could not connect to server"
- Make sure the backend is running on port 8741
- Check terminal output for errors
- Try `curl http://127.0.0.1:8741/api/status`

---

## Updating

```bash
git pull origin main
source venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && cd ..
```

## Uninstalling

```bash
# Remove the app
rm -rf /path/to/libre-bird

# Remove cached Whisper models (optional)
rm -rf ~/.cache/whisper

# Remove the database (optional)
rm libre_bird.db
```
