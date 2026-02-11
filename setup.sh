#!/bin/bash
# ============================================================
# Libre Bird — One-Command Setup Script
# Free, offline, privacy-first AI assistant
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  🕊️  Libre Bird Setup"
echo "  ═══════════════════════════════════════"
echo "  Free, offline, privacy-first AI assistant"
echo ""

# ── Check Python ────────────────────────────────────────────
echo "📦 Checking Python..."
if command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "❌ Python 3 is required. Install it from https://python.org"
    exit 1
fi

PY_VERSION=$($PYTHON --version 2>&1 | awk '{print $2}')
echo "   Found Python $PY_VERSION"

# ── Check Node.js ───────────────────────────────────────────
echo "📦 Checking Node.js..."
if ! command -v node &>/dev/null; then
    echo "❌ Node.js is required. Install it from https://nodejs.org"
    exit 1
fi

NODE_VERSION=$(node --version)
echo "   Found Node.js $NODE_VERSION"

# ── Create Python Virtual Environment ───────────────────────
echo ""
echo "🐍 Setting up Python environment..."
if [ ! -d ".venv" ]; then
    $PYTHON -m venv .venv
    echo "   Created virtual environment"
else
    echo "   Virtual environment already exists"
fi

source .venv/bin/activate

# ── Install Python Dependencies ─────────────────────────────
echo "📥 Installing Python dependencies..."
echo "   (This includes llama-cpp-python with Metal GPU support)"

# Install llama-cpp-python with Metal acceleration
CMAKE_ARGS="-DGGML_METAL=on" pip install --upgrade pip
CMAKE_ARGS="-DGGML_METAL=on" pip install -r requirements.txt

echo "   ✅ Python dependencies installed"

# ── Install Frontend Dependencies ───────────────────────────
echo ""
echo "📥 Installing frontend dependencies..."
cd frontend
npm install
cd ..
echo "   ✅ Frontend dependencies installed"

# ── Create Models Directory ─────────────────────────────────
echo ""
echo "📁 Setting up models directory..."
mkdir -p models
echo "   Created models/ directory"

# ── Download Model ──────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo "🧠 Model Download"
echo "═══════════════════════════════════════"
echo ""
echo "Libre Bird needs a GGUF model to run."
echo ""
echo "Recommended: GPT-OSS 20B Q4 (~12GB RAM)"
echo "  This model uses MoE architecture (3.6B active params)"
echo "  making it fast despite being 21B total."
echo ""
echo "Alternative: Qwen 3 14B Q4 (~10GB RAM)"
echo "  Dense model with thinking/non-thinking modes."
echo ""

read -p "Download GPT-OSS 20B Q4 now? [Y/n] " -n 1 -r
echo ""

if [[ $REPLY =~ ^[Yy]$ ]] || [[ -z $REPLY ]]; then
    echo "📥 Downloading GPT-OSS 20B Q4 GGUF..."
    echo "   (This is ~8GB, may take a while)"
    pip install huggingface-hub
    huggingface-cli download \
        OpenAI/gpt-oss-20b-GGUF \
        gpt-oss-20b-Q4_K_M.gguf \
        --local-dir models \
        --local-dir-use-symlinks False \
        2>/dev/null || {
        echo ""
        echo "⚠️  Model download failed or model not available yet."
        echo "   You can manually download a GGUF model and place it in: $SCRIPT_DIR/models/"
        echo "   Recommended models (search on HuggingFace):"
        echo "   • GPT-OSS 20B Q4_K_M"
        echo "   • Qwen3-14B Q4_K_M"
        echo ""
    }
else
    echo ""
    echo "📝 Skipped model download."
    echo "   Place any GGUF model in: $SCRIPT_DIR/models/"
fi

# ── Done! ───────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════"
echo "  🕊️  Setup Complete!"
echo "═══════════════════════════════════════"
echo ""
echo "  To start Libre Bird:"
echo "    ./start.sh"
echo ""
echo "  Then open: http://localhost:5173"
echo ""
echo "  ⚠️  First time: Grant accessibility permissions"
echo "  System Settings → Privacy & Security → Accessibility"
echo "  Add your terminal app (Terminal, iTerm2, etc.)"
echo ""
