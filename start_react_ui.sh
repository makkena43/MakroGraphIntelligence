#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# MakroGraph Intelligence — React UI Startup Script
# Starts FastAPI backend (port 8000) + Vite dev server (port 5173)
# Usage:  ./start_react_ui.sh
# ──────────────────────────────────────────────────────────────────────────────

set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"

PYTHON="$ROOT/.venv/bin/python3"
UVICORN="$ROOT/.venv/bin/uvicorn"

# ── 1. Verify .venv exists ────────────────────────────────────────────────────
if [ ! -f "$PYTHON" ]; then
    echo "❌ .venv not found at $ROOT/.venv — create it first:"
    echo "   python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    exit 1
fi
echo "✅ Python venv: $PYTHON"

# ── 2. Install Python deps if needed ─────────────────────────────────────────
"$PYTHON" -c "import fastapi, uvicorn" 2>/dev/null || \
    "$PYTHON" -m pip install fastapi "uvicorn[standard]" python-multipart --quiet
echo "✅ FastAPI deps OK"

# ── 3. Resolve npm (nvm or system) ───────────────────────────────────────────
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
NPM="$(command -v npm 2>/dev/null)"
if [ -z "$NPM" ]; then
    echo "❌ npm not found. Install Node.js: brew install node  or  nvm install --lts"
    exit 1
fi
echo "✅ npm: $NPM"

# ── 4. Install Node deps if needed ───────────────────────────────────────────
if [ ! -d "$ROOT/frontend/node_modules" ]; then
    echo "📦 Installing Node dependencies…"
    cd "$ROOT/frontend" && "$NPM" install
    cd "$ROOT"
fi
echo "✅ Node deps OK"

# ── 5. Start FastAPI in background ───────────────────────────────────────────
echo ""
echo "🚀 Starting FastAPI backend on http://localhost:8000 …"
cd "$ROOT"
"$UVICORN" backend.main:app --reload --port 8000 --log-level info &
BACKEND_PID=$!
echo "   Backend PID: $BACKEND_PID"

# ── 5. Start Vite dev server ──────────────────────────────────────────────────
echo ""
echo "⚛️  Starting React dev server on http://localhost:5173 …"
cd "$ROOT/frontend"
"$NPM" run dev &
FRONTEND_PID=$!
echo "   Frontend PID: $FRONTEND_PID"

# ── 6. Trap Ctrl-C to kill both ───────────────────────────────────────────────
trap "echo ''; echo 'Stopping servers…'; kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit 0" SIGINT SIGTERM

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  MakroGraph React UI is running!"
echo "  → Frontend:  http://localhost:5173"
echo "  → Backend:   http://localhost:8000"
echo "  → API docs:  http://localhost:8000/docs"
echo "  Press Ctrl+C to stop both servers."
echo "════════════════════════════════════════════════════════════"

wait
