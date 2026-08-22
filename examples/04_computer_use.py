"""
04_computer_use.py – agent with full computer + browser control.

The agent can:
  • Take screenshots and describe what's on screen
  • Open URLs in a visible browser window
  • Click, type, and scroll on web pages
  • Control the mouse and keyboard on your Mac

⚠️  Grant accessibility access to Terminal in:
    System Settings → Privacy & Security → Accessibility
"""
import sys
sys.path.insert(0, "..")

from agent import Agent
from tools import TOOL_DEFINITIONS

agent = Agent(
    name="computer-agent",
    system_prompt=(
        "You are an autonomous computer-use agent running on macOS. "
        "You can take screenshots to see what's on screen, navigate websites, "
        "click elements, type text, and control the computer. "
        "Always take a screenshot first to understand the current state. "
        "Narrate each step clearly before performing it."
    ),
    tools=TOOL_DEFINITIONS,   # all tools enabled
    thinking=True,            # enable deeper reasoning for complex tasks
)

# ── Demo: open a website and extract info ─────────────────────────────────────

print("Computer-use agent ready.")
print("Example tasks:")
print("  • 'Go to news.ycombinator.com and tell me the top 5 stories'")
print("  • 'Open Google and search for Qwen3 benchmarks'")
print("  • 'Take a screenshot and describe what you see'")
print("\nType 'quit' to exit.\n")

while True:
    user_input = input("Task: ").strip()
    if user_input.lower() in ("quit", "exit", "q"):
        # Clean up browser
        try:
            from tools.browser import close_browser
            close_browser()
        except Exception:
            pass
        break
    if not user_input:
        continue

    print()
    response = agent.chat(user_input)
    print(f"\nAgent: {response}\n")
    print("-" * 60)
