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

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
    "qwen-harness/1.0"
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


def scrape_url(url: str, max_chars: int = MAX_SCRAPE_CHARS) -> dict:
    """Fetch `url` and return its readable text content (HTML stripped)."""
    try:
        html = _fetch(url)
        parser = _TextExtractor()
        parser.feed(html)
        text = "\n".join(parser.chunks)
        truncated = len(text) > max_chars
        return {
            "status": "ok",
            "url": url,
            "title": re.sub(r"\s+", " ", parser.title).strip(),
            "text": text[:max_chars],
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e), "url": url}


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
