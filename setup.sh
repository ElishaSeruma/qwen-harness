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
echo "── 3. Pulling the light model roster (8 GB RAM friendly) ──"
echo "Pulling qwen3:4b     (~2.5 GB – primary model)"
ollama pull qwen3:4b

echo ""
echo "Pulling qwen3:0.6b   (~0.5 GB – lightest agent)"
ollama pull qwen3:0.6b

echo ""
echo "Pulling gemma3:1b    (~0.8 GB – light agent)"
ollama pull gemma3:1b

echo ""
echo "Pulling llama3.2:1b  (~1.3 GB – light agent)"
ollama pull llama3.2:1b

echo ""
echo "Pulling gemma2:2b    (~1.6 GB – light agent)"
ollama pull gemma2:2b

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
