"""The home page's Your rooms section in a real browser against a disposable local server: the empty
hint, rooms kept on this browser, and a signed-in host's rooms. Skips itself when Playwright or
Chromium is missing."""
import tempfile
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

import browser_smoke  # noqa: E402  (needs Playwright, so only after the skip)


def test_your_rooms_on_the_home_page():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as e:  # no browser installed here
            pytest.skip(f"Chromium isn't available: {e}")
        with tempfile.TemporaryDirectory() as out, browser_smoke.preview_server() as base_url:
            browser_smoke.BASE, browser_smoke.OUT, browser_smoke.errors = base_url, Path(out), []
            browser_smoke.check_your_rooms(browser)
            assert not browser_smoke.errors, browser_smoke.errors
        browser.close()
