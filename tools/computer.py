"""
tools/computer.py – macOS computer control tools.

Uses:
  • screencapture (macOS built-in) for screenshots
  • pyautogui for mouse / keyboard
"""

from __future__ import annotations
import subprocess
import time
from pathlib import Path

try:
    import pyautogui
    pyautogui.FAILSAFE = True   # move mouse to top-left corner to abort
    pyautogui.PAUSE = 0.05      # small delay between actions
    _HAS_PYAUTOGUI = True
except ImportError:
    _HAS_PYAUTOGUI = False


_SCREENSHOT_DIR = Path("/tmp/harness_screenshots")
_SCREENSHOT_DIR.mkdir(exist_ok=True)
_counter = 0


def _require_pyautogui():
    if not _HAS_PYAUTOGUI:
        raise RuntimeError(
            "pyautogui is not installed. Run: pip install pyautogui"
        )


# ── Screenshot ────────────────────────────────────────────────────────────────

def screenshot() -> dict:
    """Capture the full screen. Returns the saved file path."""
    global _counter
    _counter += 1
    path = _SCREENSHOT_DIR / f"screen_{_counter:04d}.png"
    # -x = no shutter sound, -C = no cursor
    subprocess.run(["screencapture", "-x", "-C", str(path)], check=True)
    return {"status": "ok", "path": str(path)}


# ── Mouse ─────────────────────────────────────────────────────────────────────

def click(x: int, y: int, button: str = "left") -> dict:
    """Single click at (x, y)."""
    _require_pyautogui()
    pyautogui.click(x, y, button=button)
    return {"status": "ok", "action": f"{button}_click", "x": x, "y": y}


def double_click(x: int, y: int) -> dict:
    """Double-click at (x, y)."""
    _require_pyautogui()
    pyautogui.doubleClick(x, y)
    return {"status": "ok", "action": "double_click", "x": x, "y": y}


def scroll(x: int, y: int, direction: str = "down", clicks: int = 3) -> dict:
    """Scroll at (x, y). direction: 'up' or 'down'."""
    _require_pyautogui()
    amount = -clicks if direction == "down" else clicks
    pyautogui.scroll(amount, x=x, y=y)
    return {"status": "ok", "direction": direction, "clicks": clicks}


def move_mouse(x: int, y: int) -> dict:
    """Move the mouse to (x, y) without clicking."""
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=0.25)
    return {"status": "ok", "moved_to": {"x": x, "y": y}}


# ── Keyboard ──────────────────────────────────────────────────────────────────

def type_text(text: str) -> dict:
    """Type text at the current cursor position."""
    _require_pyautogui()
    pyautogui.write(text, interval=0.02)
    return {"status": "ok", "typed": text}


def press_key(key: str) -> dict:
    """
    Press a key or keyboard shortcut.
    Examples: 'enter', 'tab', 'cmd+c', 'cmd+shift+4'
    """
    _require_pyautogui()
    parts = key.lower().split("+")
    if len(parts) > 1:
        pyautogui.hotkey(*parts)
    else:
        pyautogui.press(parts[0])
    return {"status": "ok", "key": key}
