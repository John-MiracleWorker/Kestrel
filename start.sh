#!/bin/bash
# ============================================================
# Libre Bird — Start Script
# Launches both backend and frontend dev servers
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "  🕊️  Starting Libre Bird..."
echo "  ═══════════════════════════════════════"
echo ""

# Activate virtual environment
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
else
    echo "❌ Virtual environment not found. Run ./setup.sh first."
    exit 1
fi

# Start backend
echo "🔧 Starting backend server on port 8741..."
uvicorn server:app --host 127.0.0.1 --port 8741 --reload &
BACKEND_PID=$!

# Wait for backend to start
sleep 2

# Start frontend
echo "🌐 Starting frontend dev server..."
cd frontend
npm run dev &
FRONTEND_PID=$!
cd ..

echo ""
echo "  ✅ Libre Bird is running!"
echo "  ═══════════════════════════════════════"
echo ""
echo "  🌐 Open: http://localhost:5173"
echo "  🔧 API:  http://127.0.0.1:8741/docs"
echo ""
echo "  Press Ctrl+C to stop."
echo ""

# Handle shutdown
cleanup() {
    echo ""
    echo "  🛑 Shutting down Libre Bird..."
    kill $BACKEND_PID 2>/dev/null
    kill $FRONTEND_PID 2>/dev/null
    wait $BACKEND_PID 2>/dev/null
    wait $FRONTEND_PID 2>/dev/null
    echo "  👋 Goodbye!"
}

trap cleanup EXIT INT TERM
wait
