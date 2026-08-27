"""
tools/web.py – web search and page scraping tools.

No API keys required: search uses DuckDuckGo's HTML endpoint (no key, no
official API, best-effort — swap in a paid search API here for higher
quality/volume if you have one). Scraping is stdlib-only (urllib + a small
HTML-to-text parser), no BeautifulSoup dependency.
"""

from __future__ import annotations
import json
import re
import urllib.parse
import urllib.request
import urllib.error
from html.parser import HTMLParser

from tools.platform_support import IS_MAC, IS_WINDOWS

# Claiming to be a Mac from a Windows machine is a needless inconsistency with
# the client hints the request carries; state the real platform.
USER_AGENT = (
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) " if IS_MAC else
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) " if IS_WINDOWS else
     "Mozilla/5.0 (X11; Linux x86_64) ")
    + "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 qwen-harness/1.0"
)
REQUEST_TIMEOUT = 15
MAX_SCRAPE_CHARS = 8000


def _fetch(url: str, timeout: int = REQUEST_TIMEOUT) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


class _ResultParser(HTMLParser):
    """Pulls {title, url, snippet} triples out of DuckDuckGo's HTML results page."""

    def __init__(self):
        super().__init__()
        self.results: list[dict] = []
        self._in_result_link = False
        self._in_snippet = False
        self._current: dict = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "a" and "result__a" in classes:
            self._in_result_link = True
            self._current = {"title": "", "url": attrs.get("href", ""), "snippet": ""}
        elif tag == "a" and "result__snippet" in classes:
            self._in_snippet = True

    def handle_endtag(self, tag):
        if tag == "a" and self._in_result_link:
            self._in_result_link = False
            if self._current.get("title"):
                self.results.append(self._current)
        if tag == "a" and self._in_snippet:
            self._in_snippet = False

    def handle_data(self, data):
        if self._in_result_link:
            self._current["title"] += data
        elif self._in_snippet and self.results is not None and self._current:
            self._current["snippet"] = self._current.get("snippet", "") + data


class _TextExtractor(HTMLParser):
    """Strips a page down to its readable text (drops script/style/nav noise)."""

    SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "nav", "header", "footer", "aside", "form", "button"}

    def __init__(self):
        super().__init__()
        self.chunks: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
            return
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self.chunks.append(text)


def web_search(query: str, max_results: int = 5) -> dict:
    """Search the web for `query`. Returns up to `max_results` {title, url, snippet}."""
    try:
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        html = _fetch(url)
        parser = _ResultParser()
        parser.feed(html)
        results = []
        for r in parser.results:
            href = r["url"]
            # Skip sponsored results (routed through DuckDuckGo's ad-click endpoint).
            if "y.js" in href or "ad_domain=" in href:
                continue
            # Organic result links are wrapped in a redirect; unwrap them.
            if href.startswith("//duckduckgo.com/l/"):
                parsed = urllib.parse.urlparse("https:" + href)
                real = urllib.parse.parse_qs(parsed.query).get("uddg", [href])[0]
                href = urllib.parse.unquote(real)
            results.append({
                "title": re.sub(r"\s+", " ", r["title"]).strip(),
                "url": href,
                "snippet": re.sub(r"\s+", " ", r.get("snippet", "")).strip(),
            })
            if len(results) >= max_results:
                break
        return {"status": "ok", "query": query, "results": results}
    except Exception as e:
        return {"error": str(e)}


# Below this, a "successful" scrape returned nothing a model can actually use.
MIN_USEFUL_CHARS = 40

# Sites known to serve an empty shell to a plain HTTP fetch. Listing them lets
# scrape_url skip the pointless static request and render immediately; anything
# NOT on the list still gets the cheap path first and only falls back when the
# extraction actually comes back empty, so the list never needs to be complete.
SPA_DOMAINS = frozenset({
    "instagram.com", "x.com", "twitter.com", "facebook.com", "threads.net",
    "tiktok.com", "linkedin.com", "reddit.com", "medium.com", "notion.so",
    "airbnb.com", "discord.com", "slack.com", "figma.com", "netflix.com",
    "youtube.com", "pinterest.com", "quora.com", "producthunt.com",
})


def _registrable_domain(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    # Good enough for matching a known list: last two labels.
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_spa_domain(url: str) -> bool:
    return _registrable_domain(url) in SPA_DOMAINS


def _render(url: str, max_chars: int, reason: str) -> dict:
    """Read a page with headless Chromium, reporting honestly if it can't."""
    try:
        from tools.browser import render_url, RendererUnavailable
    except ImportError as exc:
        return {"error": f"renderer unavailable: {exc}", "url": url, "readable": False}
    try:
        result = render_url(url, max_chars=max_chars)
    except Exception as exc:
        # Includes RendererUnavailable, whose message carries the install command.
        return {
            "error": (
                f"This page needs JavaScript rendering ({reason}), but it could not be "
                f"rendered: {exc} Do NOT guess what the page said."
            ),
            "url": url,
            "readable": False,
            "needs_rendering": True,
        }
    if len((result.get("text") or "").strip()) < MIN_USEFUL_CHARS:
        return {
            "error": (
                "Rendered the page but it still showed no readable text — it is most "
                "likely behind a login or a bot check. Do NOT guess what it said."
            ),
            "url": url,
            "title": result.get("title", ""),
            "readable": False,
            "rendered": True,
        }
    return {**result, "via": "headless-browser", "reason": reason}


def scrape_url(url: str, max_chars: int = MAX_SCRAPE_CHARS, allow_render: bool = True) -> dict:
    """Fetch `url` and return its readable text content (HTML stripped).

    JavaScript-rendered pages are handled automatically: known SPA domains go
    straight to a headless browser, and anything else falls back to one when
    the static fetch turns up nothing readable. Pass allow_render=False to
    force the cheap static path only.
    """
    if allow_render and is_spa_domain(url):
        # No point fetching static HTML we know will be an empty shell.
        return _render(url, max_chars, "known JavaScript-rendered site")

    try:
        html = _fetch(url)
    except urllib.error.HTTPError as e:
        # A 404/403 is the single most useful signal when checking whether a
        # page exists. Reporting it as a bare exception string buried the fact.
        return {
            "error": f"HTTP {e.code} {e.reason}",
            "http_status": e.code,
            "url": url,
            "exists": False if e.code == 404 else None,
        }
    except Exception as e:
        return {"error": str(e), "url": url}

    parser = _TextExtractor()
    parser.feed(html)
    text = "\n".join(parser.chunks)
    title = re.sub(r"\s+", " ", parser.title).strip()

    # A JS-rendered page (Instagram, X, most SPAs) returns 200 with an empty
    # shell. Reporting that as {"status": "ok", "text": ""} is worse than an
    # error: the model sees success, has no content, and fills the gap by
    # inventing one. Say plainly that nothing was readable.
    if len(text.strip()) < MIN_USEFUL_CHARS:
        # Static HTML had nothing in it. Render it rather than reporting a
        # dead end — this is the common case for any modern framework, not
        # just the domains on the known list.
        if allow_render:
            rendered = _render(url, max_chars, "static HTML had no readable text")
            if rendered.get("status") == "ok":
                return rendered
            return {**rendered, "html_bytes": len(html), "title": title or rendered.get("title", "")}
        return {
            "error": (
                "Fetched the page but found no readable text — it is rendered by "
                "JavaScript, or behind a login or bot wall. Do NOT guess what the "
                "page said; report that it could not be read."
            ),
            "url": url,
            "title": title,
            "http_status": 200,
            "readable": False,
            "needs_rendering": True,
        }

    truncated = len(text) > max_chars
    return {
        "status": "ok",
        "url": url,
        "title": title,
        "text": text[:max_chars],
        "truncated": truncated,
        "readable": True,
        "via": "static-html",
    }


def fetch_data(url: str, method: str = "GET", headers: dict | None = None, body: dict | None = None) -> dict:
    """
    Call a URL/API directly and return its response — for JSON APIs, unlike
    scrape_url (which is for HTML pages). Response is JSON-parsed when
    possible, otherwise returned as raw text (truncated to 8000 chars).
    """
    try:
        req_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req_headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, method=method.upper(), headers=req_headers)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read().decode(charset, errors="replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        status = e.code
    except Exception as e:
        return {"error": str(e), "url": url}

    try:
        parsed = json.loads(raw)
        return {"status": "ok", "http_status": status, "url": url, "json": parsed}
    except json.JSONDecodeError:
        return {"status": "ok", "http_status": status, "url": url, "text": raw[:8000]}
