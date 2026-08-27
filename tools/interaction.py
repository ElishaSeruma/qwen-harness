"""
tools/interaction.py – ask_user and create_display.

ask_user is CLI-only: it blocks on stdin, so it only makes sense in the
terminal example scripts (examples/02_with_tools.py etc.), not inside the
GUI's browser-side chat (which doesn't run this Python tool loop at all —
see RUNBOOK.md for that boundary).

create_display renders a self-contained HTML file into the system temp
directory and opens it with the OS default handler (`open` on macOS,
`os.startfile` on Windows, `xdg-open` on Linux) — a lightweight way for an
agent to hand the user something visual (a table, a small chart, a formatted
report) instead of a wall of text.
"""

from __future__ import annotations
import time
from pathlib import Path

from tools.platform_support import harness_temp, open_path

_DISPLAY_DIR = harness_temp("harness_displays")
_counter = 0


def ask_user(question: str) -> dict:
    """Pause and ask the human a clarifying question. Blocks for terminal input."""
    print(f"\n🤖 {question}")
    try:
        answer = input("You: ").strip()
    except (EOFError, KeyboardInterrupt):
        return {"error": "No input available (not running in an interactive terminal)."}
    return {"status": "ok", "question": question, "answer": answer}


def create_display(html: str, title: str = "Agent display", open_it: bool = True) -> dict:
    """Render `html` (a fragment — no need for <html>/<head>/<body>) as a page and open it."""
    global _counter
    _counter += 1
    path = _DISPLAY_DIR / f"display_{_counter:04d}.html"
    page = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{title}</title>
<style>
  body {{ margin: 0; padding: 24px; background: #0b0e14; color: #eef1f7;
          font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #2a3140; padding: 8px 12px; text-align: left; }}
  th {{ background: #171c27; }}
  h1, h2, h3 {{ margin-top: 0; }}
  code, pre {{ font-family: ui-monospace, SFMono-Regular, Consolas, Menlo, monospace; }}
</style></head>
<body>{html}</body></html>"""
    path.write_text(page, encoding="utf-8")
    opened = open_path(path) if open_it else False
    return {"status": "ok", "path": str(path), "opened": opened}
