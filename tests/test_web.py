from __future__ import annotations

import unittest
from unittest.mock import patch

from tools import web


class ScrapeHonestyTests(unittest.TestCase):
    """A scrape that returned nothing must say so, not report success.

    Reporting {"status": "ok", "text": ""} for a JS-rendered page is worse than
    an error: the model sees success, has no content, and fills the gap by
    inventing one. Observed against instagram.com, which returns 200 with an
    empty shell.
    """

    def test_empty_extraction_never_reports_success(self):
        shell = "<html><head><title>App</title></head><body><div id='root'></div></body></html>"
        with patch.object(web, "_fetch", return_value=shell), \
             patch("tools.browser.render_url", side_effect=RuntimeError("no renderer here")):
            result = web.scrape_url("https://static-only.example/app")

        self.assertNotEqual(result.get("status"), "ok")
        self.assertFalse(result["readable"])
        self.assertIn("Do NOT guess", result["error"])

    def test_a_real_page_still_scrapes(self):
        page = (
            "<html><head><title>Example</title></head><body>"
            "<p>This is a real page with enough readable body text to be useful "
            "to a model reading it.</p></body></html>"
        )
        with patch.object(web, "_fetch", return_value=page):
            result = web.scrape_url("https://example.com")

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["readable"])
        self.assertIn("real page", result["text"])

    def test_http_errors_surface_the_status_code(self):
        import urllib.error

        error = urllib.error.HTTPError("https://x/y", 404, "Not Found", {}, None)
        with patch.object(web, "_fetch", side_effect=error):
            result = web.scrape_url("https://x/y")

        self.assertEqual(result["http_status"], 404)
        self.assertFalse(result["exists"])
        self.assertIn("404", result["error"])


class SpaDomainTests(unittest.TestCase):
    def test_known_spa_domains_are_recognised(self):
        for url in ("https://www.instagram.com/x/", "https://x.com/a",
                    "https://reddit.com/r/y", "https://sub.tiktok.com/z"):
            self.assertTrue(web.is_spa_domain(url), url)

    def test_ordinary_sites_are_not(self):
        for url in ("https://example.com", "https://docs.python.org/3/",
                    "https://news.ycombinator.com"):
            self.assertFalse(web.is_spa_domain(url), url)


class SpaRoutingTests(unittest.TestCase):
    """A JS-rendered page routes to the headless browser automatically."""

    RENDERED = {
        "status": "ok", "url": "u", "title": "T", "rendered": True,
        "text": "real content pulled from the rendered page, long enough to count",
    }

    def test_known_spa_domains_skip_the_static_fetch(self):
        with patch.object(web, "_fetch", side_effect=AssertionError("must not fetch statically")), \
             patch("tools.browser.render_url", return_value=self.RENDERED) as render:
            result = web.scrape_url("https://www.instagram.com/someone/")

        render.assert_called_once()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["via"], "headless-browser")

    def test_unknown_domain_tries_static_first_then_renders(self):
        """The known list never has to be complete — empty extraction triggers it."""
        shell = "<html><head><title>App</title></head><body><div id='root'></div></body></html>"
        with patch.object(web, "_fetch", return_value=shell) as fetch, \
             patch("tools.browser.render_url", return_value=self.RENDERED) as render:
            result = web.scrape_url("https://some-react-app.example/dashboard")

        fetch.assert_called_once()
        render.assert_called_once()
        self.assertEqual(result["via"], "headless-browser")

    def test_a_readable_static_page_never_launches_a_browser(self):
        page = ("<html><head><title>Docs</title></head><body><p>Plenty of readable "
                "static text here, no JavaScript required at all.</p></body></html>")
        with patch.object(web, "_fetch", return_value=page), \
             patch("tools.browser.render_url", side_effect=AssertionError("must not render")):
            result = web.scrape_url("https://docs.python.org/3/")

        self.assertEqual(result["via"], "static-html")

    def test_allow_render_false_forces_the_static_path(self):
        with patch.object(web, "_fetch", return_value="<html><body></body></html>"), \
             patch("tools.browser.render_url", side_effect=AssertionError("must not render")):
            result = web.scrape_url("https://example.com/spa", allow_render=False)

        self.assertTrue(result["needs_rendering"])
        self.assertIn("Do NOT guess", result["error"])

    def test_missing_playwright_is_reported_with_the_install_command(self):
        from tools.browser import RendererUnavailable

        boom = RendererUnavailable(
            "playwright is not installed. Install it with: pip install playwright"
        )
        with patch("tools.browser.render_url", side_effect=boom):
            result = web.scrape_url("https://www.instagram.com/someone/")

        self.assertFalse(result["readable"])
        self.assertIn("pip install playwright", result["error"])
        self.assertIn("Do NOT guess", result["error"])

    def test_login_walled_page_is_not_reported_as_success(self):
        """Rendering succeeded but showed nothing — still not a usable result."""
        empty = {"status": "ok", "url": "u", "title": "Login", "text": "  ", "rendered": True}
        with patch("tools.browser.render_url", return_value=empty):
            result = web.scrape_url("https://www.instagram.com/someone/")

        self.assertNotEqual(result.get("status"), "ok")
        self.assertIn("login", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
