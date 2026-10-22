#!/bin/bash
# Double-clickable launcher for macOS to view Micro-Orchestrator WebUI
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

echo "=========================================="
echo "⚡ Starting Micro-Orchestrator WebUI"
echo "=========================================="

# Check virtual environment
if [ ! -f ".venv/bin/uvicorn" ]; then
    echo "Virtualenv not found or incomplete. Setting up .venv..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

# Open browser in 1 second
(sleep 1 && open "http://127.0.0.1:8000") &

echo "WebUI running on http://127.0.0.1:8000"
echo "Press Ctrl+C when finished to stop the server."
echo "------------------------------------------"

.venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
