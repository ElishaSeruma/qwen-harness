#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  Qwen Harness – wake-up: launch everything for a working session.
#  (setup.sh = one-time install. wake-up.sh = every-day launch.)
# ─────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")"

PORT="${1:-8420}"

echo "── 1. Ollama ────────────────────────────────────────────"

# Runtime tuning. These are server-level env vars — the OpenAI-compatible
# /v1/chat/completions endpoint the GUI uses cannot set num_ctx or keep_alive
# per request, so they must be set on the process. They only take effect on a
# fresh `ollama serve`, which is why this block restarts it rather than
# skipping when already running.
#
# CONTEXT_LENGTH: Ollama defaults to 4096 regardless of what a model
#   advertises. 8192 doubles usable context for tool schemas + history while
#   keeping the KV cache affordable on 8 GB. Raise to 16384 only if
#   `sysctl vm.swapusage` stays flat during a real run.
# KEEP_ALIVE: avoids paying the multi-second model reload on every message.
# MAX_LOADED_MODELS=1: on 8 GB, two resident contexts will swap-thrash.
# NUM_PARALLEL=1: gui/server.py's resource monitor pairs loaded models to
#   llama-server processes by list order, which assumes this.
export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-8192}"
export OLLAMA_KEEP_ALIVE="${OLLAMA_KEEP_ALIVE:-30m}"
export OLLAMA_MAX_LOADED_MODELS="${OLLAMA_MAX_LOADED_MODELS:-1}"
export OLLAMA_NUM_PARALLEL="${OLLAMA_NUM_PARALLEL:-1}"

if ! command -v ollama &>/dev/null; then
  echo "Ollama isn't installed. Run ./setup.sh first."
else
  if pgrep -x "ollama" > /dev/null; then
    # Warn loudly if a model is loaded — restarting aborts any in-flight run
    # in another tab or in the desktop app.
    if curl -s --max-time 2 http://localhost:11434/api/ps 2>/dev/null | grep -q '"name"'; then
      echo "⚠  A model is currently loaded. Restarting Ollama will abort any run in progress."
    fi
    echo "Restarting Ollama to apply context=$OLLAMA_CONTEXT_LENGTH keep_alive=$OLLAMA_KEEP_ALIVE"
    pkill -x ollama || true
    # Wait for the port to actually free rather than sleeping and hoping.
    for _ in $(seq 1 20); do
      lsof -i :11434 -sTCP:LISTEN >/dev/null 2>&1 || break
      sleep 0.5
    done
  fi

  ollama serve &>/tmp/ollama.log &
  for _ in $(seq 1 20); do
    curl -s --max-time 1 http://localhost:11434/api/tags >/dev/null 2>&1 && break
    sleep 0.5
  done
  echo "Ollama started (log: /tmp/ollama.log)"

  # A menu-bar Ollama.app can respawn `ollama serve` without this env and win
  # the race, silently reverting the settings. Say so rather than reporting
  # a success that didn't happen.
  if pgrep -f "Ollama.app" >/dev/null 2>&1; then
    echo "⚠  Ollama.app is also running and may have respawned the server without these settings."
    echo "   Quit Ollama.app, or set them via: launchctl setenv OLLAMA_CONTEXT_LENGTH $OLLAMA_CONTEXT_LENGTH"
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
