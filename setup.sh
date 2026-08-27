#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
#  Qwen Harness – one-shot setup for Apple Silicon Mac
# ─────────────────────────────────────────────────────────────
set -e

echo "── 1. Checking for Ollama ──────────────────────────────"
if ! command -v ollama &>/dev/null; then
  echo "Ollama not found. Installing via Homebrew..."
  brew install ollama
else
  echo "Ollama already installed: $(ollama --version)"
fi

echo ""
echo "── 2. Starting Ollama server ───────────────────────────"
# Start in background if not already running
if ! pgrep -x "ollama" > /dev/null; then
  ollama serve &>/tmp/ollama.log &
  sleep 2
  echo "Ollama started (log: /tmp/ollama.log)"
else
  echo "Ollama already running"
fi

echo ""
echo "── 3. Model roster (8 GB RAM friendly) ─────────────────"
# `ollama pull` on an existing model still re-verifies every layer against the
# registry, so guard on what is already installed and skip it outright —
# re-running setup.sh on a provisioned machine should download nothing.
installed="$(ollama list 2>/dev/null | tail -n +2 | awk '{print $1}')"
ensure_model() {
  local name="$1" note="$2"
  if grep -qx -e "$name" -e "$name:latest" <<<"$installed"; then
    echo "✓ $name already installed — skipping   ($note)"
  else
    echo "↓ Pulling $name   ($note)"
    ollama pull "$name"
  fi
}
ensure_model qwen3:4b    "~2.5 GB – primary model"
ensure_model qwen3:0.6b  "~0.5 GB – lightest agent"
ensure_model gemma3:1b   "~0.8 GB – light agent"
ensure_model llama3.2:1b "~1.3 GB – light agent"
ensure_model gemma2:2b   "~1.6 GB – light agent"

echo ""
echo "── 4. Python dependencies ──────────────────────────────"

# --break-system-packages is NOT unconditional. It is required on a PEP 668
# "externally managed" interpreter (Homebrew python3.12+ refuses to install
# without it) but it does not exist before pip 23, and passing it to an older
# pip is a hard error — Xcode's bundled python3.9 ships pip 21.2.4 and dies
# with "no such option". Detect support instead of assuming either way.
PIP_FLAGS=()
if python3 -m pip install --help 2>/dev/null | grep -q -- '--break-system-packages'; then
  if python3 -c "import os,sysconfig,sys; sys.exit(0 if os.path.exists(os.path.join(sysconfig.get_path('stdlib'),'EXTERNALLY-MANAGED')) else 1)"; then
    PIP_FLAGS+=(--break-system-packages)
    echo "   (externally-managed interpreter — using --break-system-packages)"
  fi
else
  # Old pip: no PEP 668 enforcement either, so a plain --user install is fine.
  PIP_FLAGS+=(--user)
  echo "   (pip is older than 23 — installing with --user)"
fi

python3 -m pip install "${PIP_FLAGS[@]}" \
  openai \
  playwright \
  pyautogui \
  Pillow \
  rich

echo ""
echo "── 5. Playwright browsers ──────────────────────────────"
python3 -m playwright install chromium

echo ""
echo "✅  Setup complete!"
echo ""
echo "Quick start:"
echo "  python examples/01_hello.py"
echo "  python examples/02_with_tools.py"
echo "  python examples/03_multi_agent.py"
echo "  python examples/04_computer_use.py"
