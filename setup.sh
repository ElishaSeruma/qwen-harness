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
echo "── 3. Pulling Qwen3 models ─────────────────────────────"
echo "Pulling qwen3:8b  (~5 GB – works on 8 GB RAM Macs)"
ollama pull qwen3:8b

echo ""
echo "Pulling qwen3:14b (~9 GB – recommended for 16 GB+ RAM)"
ollama pull qwen3:14b

echo ""
echo "── 4. Python dependencies ──────────────────────────────"
pip install --break-system-packages \
  openai \
  playwright \
  pyautogui \
  Pillow \
  rich

echo ""
echo "── 5. Playwright browsers ──────────────────────────────"
playwright install chromium

echo ""
echo "✅  Setup complete!"
echo ""
echo "Quick start:"
echo "  python examples/01_hello.py"
echo "  python examples/02_with_tools.py"
echo "  python examples/03_multi_agent.py"
echo "  python examples/04_computer_use.py"
