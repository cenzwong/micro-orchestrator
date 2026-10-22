#!/usr/bin/env bash
# Quick CLI launcher for Micro-Orchestrator WebUI
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

if [ ! -f ".venv/bin/uvicorn" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
fi

echo "Starting Micro-Orchestrator UI at http://127.0.0.1:8000 ..."
# Open browser if on macOS or Linux with xdg-open
if command -v open >/dev/null 2>&1; then
    (sleep 1 && open "http://127.0.0.1:8000") &
elif command -v xdg-open >/dev/null 2>&1; then
    (sleep 1 && xdg-open "http://127.0.0.1:8000") &
fi

exec .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8000
