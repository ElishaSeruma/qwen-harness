#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  Qwen Harness – wake-up: launch everything for a working session.
#  (setup.sh = one-time install. wake-up.sh = every-day launch.)
# ─────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

PORT="${1:-8420}"

echo "── 1. Ollama ────────────────────────────────────────────"
if ! command -v ollama &>/dev/null; then
  echo "Ollama isn't installed. Run ./setup.sh first."
else
  if ! pgrep -x "ollama" > /dev/null; then
    ollama serve &>/tmp/ollama.log &
    sleep 1
    echo "Ollama started (log: /tmp/ollama.log)"
  else
    echo "Ollama already running"
  fi
fi

echo ""
echo "── 2. Control Room server ──────────────────────────────"
if lsof -i ":$PORT" -sTCP:LISTEN &>/dev/null; then
  echo "Something is already listening on :$PORT — reusing it."
else
  python3 gui/server.py "$PORT" &>/tmp/qwen-harness-gui.log &
  sleep 1
  echo "GUI server started on :$PORT (log: /tmp/qwen-harness-gui.log)"
fi

URL="http://127.0.0.1:$PORT/gui/"
echo ""
echo "── 3. Opening $URL ──────────────────────"
if command -v open &>/dev/null; then
  open "$URL"
else
  echo "Open this URL manually: $URL"
fi

echo ""
echo "✅  qwen-harness is awake → $URL"
