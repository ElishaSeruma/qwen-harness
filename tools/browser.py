"""
tools/browser.py – Playwright-based browser control.

The browser is launched lazily on first use and kept alive for the session.
Call close_browser() when done to free resources.
"""

from __future__ import annotations
from pathlib import Path
from typing import Optional

from tools.platform_support import IS_MAC, IS_WINDOWS, harness_temp

# Not Path("/tmp/..."): on Windows that resolves to 	mp\... on the current
# drive and raises at import time, which took the whole tool registry down
# with it. harness_temp() also creates parents, which `mkdir(exist_ok=True)`
# did not.
_SCREENSHOT_DIR = harness_temp("harness_screenshots")
_counter = 0

# Lazy globals
_playwright = None
_browser = None
_page = None


def _get_page():
    """Return the active Playwright page, launching browser if needed."""
    global _playwright, _browser, _page
    if _page is None or _page.is_closed():
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise RuntimeError(
                "playwright is not installed. Run: pip install playwright && playwright install chromium"
            )
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.launch(
            headless=False,    # set True for background/server use
            args=["--no-sandbox"],
        )
        context = _browser.new_context(
            viewport={"width": 1280, "height": 900},
            # Match the host OS. A Mac UA sent from Windows is a mismatch
            # against the client hints Chromium sends alongside it, which is
            # exactly the inconsistency bot-detection looks for.
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " if IS_MAC else
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " if IS_WINDOWS else
                "Mozilla/5.0 (X11; Linux x86_64) "
            ) + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        )
        _page = context.new_page()
    return _page


# ── Navigation ────────────────────────────────────────────────────────────────

def navigate(url: str, timeout: int = 30_000) -> dict:
    """Navigate to a URL and wait for page load."""
    page = _get_page()
    page.goto(url, wait_until="domcontentloaded", timeout=timeout)
    return {"status": "ok", "url": page.url, "title": page.title()}


def go_back() -> dict:
    page = _get_page()
    page.go_back()
    return {"status": "ok", "url": page.url}


def go_forward() -> dict:
    page = _get_page()
    page.go_forward()
    return {"status": "ok", "url": page.url}


def reload() -> dict:
    page = _get_page()
    page.reload()
    return {"status": "ok"}


# ── Interaction ───────────────────────────────────────────────────────────────

def click_element(selector: str, timeout: int = 5_000) -> dict:
    """Click an element matched by CSS selector."""
    page = _get_page()
    page.wait_for_selector(selector, timeout=timeout)
    page.click(selector)
    return {"status": "ok", "clicked": selector}


def type_in_element(selector: str, text: str, timeout: int = 5_000) -> dict:
    """Clear an input and type into it."""
    page = _get_page()
    page.wait_for_selector(selector, timeout=timeout)
    page.fill(selector, text)
    return {"status": "ok", "filled": selector, "text": text}


def press_key_in_browser(key: str) -> dict:
    """Press a key in the browser (e.g., 'Enter', 'Tab', 'Escape')."""
    page = _get_page()
    page.keyboard.press(key)
    return {"status": "ok", "key": key}


def hover(selector: str) -> dict:
    page = _get_page()
    page.hover(selector)
    return {"status": "ok", "hovered": selector}


def select_option(selector: str, value: str) -> dict:
    """Select an option in a <select> element."""
    page = _get_page()
    page.select_option(selector, value)
    return {"status": "ok", "selected": value}


# ── Reading ───────────────────────────────────────────────────────────────────

def get_page_text() -> dict:
    """Return all visible text from the current page."""
    page = _get_page()
    text = page.inner_text("body")
    return {"status": "ok", "url": page.url, "text": text[:10_000]}  # cap at 10k chars


def get_page_html(selector: str = "body") -> dict:
    """Return the outer HTML of a selected element."""
    page = _get_page()
    html = page.outer_html(selector)
    return {"status": "ok", "html": html[:10_000]}


def find_elements(selector: str) -> dict:
    """Return text content of all elements matching a CSS selector."""
    page = _get_page()
    elements = page.query_selector_all(selector)
    texts = [el.inner_text() for el in elements]
    return {"status": "ok", "selector": selector, "count": len(texts), "texts": texts}


def evaluate_js(script: str) -> dict:
    """Execute JavaScript in the browser and return the result."""
    page = _get_page()
    result = page.evaluate(script)
    return {"status": "ok", "result": str(result)}


# ── Screenshots ───────────────────────────────────────────────────────────────

def browser_screenshot(selector: Optional[str] = None) -> dict:
    """
    Screenshot the full browser page (or a specific element).
    Returns the saved file path.
    """
    global _counter
    _counter += 1
    page = _get_page()
    path = _SCREENSHOT_DIR / f"browser_{_counter:04d}.png"
    if selector:
        el = page.query_selector(selector)
        el.screenshot(path=str(path))
    else:
        page.screenshot(path=str(path), full_page=False)
    return {"status": "ok", "path": str(path)}


# ── Headless rendering (for scrape_url's SPA fallback) ───────────────────────
# Deliberately a SEPARATE browser from the interactive one above:
#  * headless, so an automatic fallback never pops a window in your face
#  * its own instance, so rendering a page mid-task cannot navigate away from
#    whatever an agent is driving in the interactive session
_render_playwright = None
_render_browser = None


class RendererUnavailable(RuntimeError):
    """Playwright or its Chromium build is missing."""


def _get_render_browser():
    global _render_playwright, _render_browser
    if _render_browser is not None:
        return _render_browser
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RendererUnavailable(
            "playwright is not installed, so JavaScript-rendered pages cannot be read. "
            "Install it with:  python3 -m pip install --break-system-packages playwright "
            "&& python3 -m playwright install chromium"
        ) from exc
    try:
        _render_playwright = sync_playwright().start()
        _render_browser = _render_playwright.chromium.launch(headless=True, args=["--no-sandbox"])
    except Exception as exc:
        _render_playwright = _render_browser = None
        raise RendererUnavailable(
            f"could not launch headless Chromium: {exc}. "
            "Try:  python3 -m playwright install chromium"
        ) from exc
    return _render_browser


def render_url(url: str, timeout: int = 30_000, max_chars: int = 8_000) -> dict:
    """Load `url` in headless Chromium and return the text the user would see.

    This is what makes an SPA readable: the static HTML of Instagram, X, or any
    React app carries no content, so a plain HTTP fetch returns an empty shell.
    """
    browser = _get_render_browser()
    context = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    )
    page = context.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        # domcontentloaded fires before the app has painted; give the framework
        # a moment to render, but never block the whole timeout on a page that
        # polls forever (chat apps, live feeds).
        try:
            page.wait_for_load_state("networkidle", timeout=min(8_000, timeout))
        except Exception:
            pass
        text = page.inner_text("body")
        return {
            "status": "ok",
            "url": page.url,
            "title": page.title(),
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            "http_status": response.status if response else None,
            "rendered": True,
        }
    finally:
        context.close()


def close_renderer() -> dict:
    global _render_playwright, _render_browser
    if _render_browser:
        _render_browser.close()
        _render_playwright.stop()
        _render_browser = _render_playwright = None
    return {"status": "ok"}


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def close_browser() -> dict:
    global _playwright, _browser, _page
    if _browser:
        _browser.close()
        _playwright.stop()
        _browser = None
        _page = None
        _playwright = None
    return {"status": "ok", "message": "Browser closed"}


def current_url() -> dict:
    page = _get_page()
    return {"url": page.url, "title": page.title()}
