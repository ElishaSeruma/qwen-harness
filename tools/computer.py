"""
tools/computer.py – computer control tools (screen, mouse, keyboard).

Screenshots use the fastest thing the OS provides:
  • macOS   — `screencapture -x -C` (built in, no shutter sound, no cursor)
  • Windows — pyautogui/Pillow's `ImageGrab`, which is the Win32 BitBlt path
  • Linux   — pyautogui (needs scrot/gnome-screenshot present)

Mouse and keyboard are pyautogui everywhere.

Key names are normalised per platform. A model trained largely on macOS text
emits `cmd+c` for copy, and pyautogui on Windows has no `cmd` key at all — it
raises, so *every* shortcut an agent tried on Windows failed. `press_key` now
translates the modifier to the platform's real one (`cmd` -> `ctrl` on
Windows/Linux, `ctrl` -> `cmd` for the clipboard/undo family on macOS) and
reports what it actually pressed, so a translated key is visible rather than
silently different from what was asked.
"""

from __future__ import annotations
import subprocess
from pathlib import Path

from tools.platform_support import IS_MAC, IS_WINDOWS, harness_temp

try:
    import pyautogui
    pyautogui.FAILSAFE = True   # move mouse to top-left corner to abort
    pyautogui.PAUSE = 0.05      # small delay between actions
    _HAS_PYAUTOGUI = True
except Exception:   # ImportError, but also a missing display/X server
    _HAS_PYAUTOGUI = False


_SCREENSHOT_DIR = harness_temp("harness_screenshots")
_counter = 0


def _require_pyautogui():
    if not _HAS_PYAUTOGUI:
        raise RuntimeError(
            "pyautogui is not installed. Run: pip install pyautogui pillow"
        )


# ── Screenshot ────────────────────────────────────────────────────────────────

def screenshot() -> dict:
    """Capture the full screen. Returns the saved file path."""
    global _counter
    _counter += 1
    path = _SCREENSHOT_DIR / f"screen_{_counter:04d}.png"
    if IS_MAC:
        # -x = no shutter sound, -C = no cursor
        subprocess.run(["screencapture", "-x", "-C", str(path)], check=True)
        return {"status": "ok", "path": str(path)}
    # Windows has no screencapture binary. pyautogui.screenshot() goes through
    # Pillow's ImageGrab, which is a native BitBlt of the virtual screen.
    _require_pyautogui()
    try:
        pyautogui.screenshot(str(path))
    except Exception as exc:  # Pillow missing, or no display
        return {"error": f"screenshot failed: {exc}. Install Pillow: pip install Pillow"}
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

# Names pyautogui does not know, mapped to ones it does. `cmd` is the important
# one: it exists only on macOS, and pyautogui raises rather than ignoring an
# unknown key, so one `cmd+c` from a Mac-trained model broke the whole call.
_KEY_ALIASES = {
    "command": "cmd", "apple": "cmd", "meta": "win" if IS_WINDOWS else "cmd",
    "option": "alt", "opt": "alt", "windows": "win", "super": "win",
    "control": "ctrl", "return": "enter", "esc": "escape", "del": "delete",
}


def normalize_key(key: str) -> tuple[list[str], bool]:
    """(parts, translated) — platform-correct key parts for a shortcut string.

    Returns whether anything was changed so the caller can report it: a
    shortcut that quietly became a different shortcut is worse than one that
    failed, because the model believes it did what it asked.
    """
    parts = [_KEY_ALIASES.get(p, p) for p in (key or "").lower().replace(" ", "").split("+") if p]
    translated = False
    if not IS_MAC:
        # cmd does not exist off macOS. ctrl is its counterpart for every
        # shortcut a model reaches for (copy, paste, save, select-all).
        swapped = ["ctrl" if p == "cmd" else p for p in parts]
        translated = swapped != parts
        parts = swapped
    return parts, translated


def press_key(key: str) -> dict:
    """
    Press a key or keyboard shortcut.
    Examples: 'enter', 'tab', 'ctrl+c', 'cmd+c' (translated to ctrl+c off macOS)
    """
    _require_pyautogui()
    parts, translated = normalize_key(key)
    if not parts:
        return {"error": "no key given"}
    try:
        if len(parts) > 1:
            pyautogui.hotkey(*parts)
        else:
            pyautogui.press(parts[0])
    except Exception as exc:
        return {"error": f"could not press {'+'.join(parts)}: {exc}"}
    result = {"status": "ok", "key": key, "pressed": "+".join(parts)}
    if translated:
        result["note"] = f"'{key}' is a macOS shortcut; pressed '{result['pressed']}' on this platform"
    return result


def type_text(text: str) -> dict:
    """Type text at the current cursor position."""
    _require_pyautogui()
    pyautogui.write(text, interval=0.02)
    return {"status": "ok", "typed": text}
